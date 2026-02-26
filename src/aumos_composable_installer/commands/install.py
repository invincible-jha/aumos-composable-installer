"""CLI command: aumos install.

Installs the AumOS platform by resolving dependencies, validating license
entitlements, checking for conflicts, and delegating to the Helm deployer.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from aumos_common.observability import get_logger

from aumos_composable_installer.deployer.helm_deployer import HelmDeployer
from aumos_composable_installer.resolver.conflict_detector import ConflictDetector, ConflictSeverity
from aumos_composable_installer.resolver.dependency_graph import DependencyGraph, FOUNDATION_MODULES
from aumos_composable_installer.resolver.module_manifest import ManifestLoader
from aumos_composable_installer.settings import Settings

logger = get_logger(__name__)
console = Console()

app = typer.Typer(
    help="Install the AumOS platform with selected modules.",
    no_args_is_help=True,
)


@app.command("run")
def install_run(
    modules: str = typer.Option(
        "",
        "--modules",
        "-m",
        help="Comma-separated list of optional modules to activate (e.g., data-factory,mlops).",
    ),
    namespace: str = typer.Option("aumos", "--namespace", "-n", help="Kubernetes namespace."),
    release_name: str = typer.Option("aumos", "--release", "-r", help="Helm release name."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Simulate installation without applying changes."),
    skip_health_check: bool = typer.Option(False, "--skip-health-check", help="Skip post-install health verification."),
    output: str = typer.Option("rich", "--output", "-o", help="Output format: rich | json."),
) -> None:
    """Install the AumOS Enterprise platform.

    Resolves all module dependencies (foundation modules are always included),
    validates license entitlements for Tier B/C modules, checks for conflicts,
    and deploys via Helm.

    Args:
        modules: Comma-separated optional module names to activate.
        namespace: Kubernetes namespace for the deployment.
        release_name: Helm release name.
        dry_run: If True, simulate without deploying.
        skip_health_check: Skip post-install health verification.
        output: Output format (rich or json).
    """
    settings = Settings()

    requested_module_list = [m.strip() for m in modules.split(",") if m.strip()]
    all_requested = list(FOUNDATION_MODULES) + requested_module_list

    console.print(Panel("[bold cyan]AumOS Platform Installation[/bold cyan]", expand=False))

    if dry_run:
        console.print("[yellow]DRY RUN — no changes will be applied[/yellow]")

    # Resolve dependencies
    loader = ManifestLoader(settings.manifest_dir)
    graph = DependencyGraph(loader)

    try:
        resolution = graph.resolve(all_requested)
    except KeyError as exc:
        console.print(f"[red]Unknown module:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    # Load license info for conflict checking
    licensed_modules: set[str] = set()
    try:
        from aumos_composable_installer.license.key_manager import KeyManager
        from aumos_composable_installer.license.validator import LicenseValidator
        validator = LicenseValidator(settings.license_public_key_path)
        key_manager = KeyManager(settings.license_key_path, validator)
        if key_manager.is_activated():
            license_info = key_manager.load()
            licensed_modules = license_info.modules
    except Exception as exc:
        logger.warning("Could not load license info", error=str(exc))

    # Conflict detection
    detector = ConflictDetector(loader)
    conflicts = detector.check(resolution.all_modules, licensed_modules)

    if conflicts:
        conflict_table = Table(title="Conflict Report", show_header=True)
        conflict_table.add_column("Severity", style="bold")
        conflict_table.add_column("Modules")
        conflict_table.add_column("Message")
        conflict_table.add_column("Remediation")
        for conflict in conflicts:
            severity_style = "red" if conflict.severity == ConflictSeverity.ERROR else "yellow"
            conflict_table.add_row(
                f"[{severity_style}]{conflict.severity.value.upper()}[/{severity_style}]",
                ", ".join(conflict.modules),
                conflict.message,
                conflict.remediation,
            )
        console.print(conflict_table)

        if detector.has_blocking_conflicts(conflicts):
            console.print("[red]Installation blocked due to conflicts above.[/red]")
            raise typer.Exit(code=1)

    # Show resolution plan
    plan_table = Table(title="Installation Plan", show_header=True)
    plan_table.add_column("Order", justify="right")
    plan_table.add_column("Module")
    plan_table.add_column("Source")
    for idx, module_name in enumerate(resolution.install_order, start=1):
        source = "requested" if module_name in resolution.explicitly_requested else "dependency"
        plan_table.add_row(str(idx), module_name, source)
    console.print(plan_table)

    if not dry_run:
        should_proceed = typer.confirm(f"Deploy {resolution.total_count} module(s) to namespace '{namespace}'?")
        if not should_proceed:
            console.print("[yellow]Installation cancelled.[/yellow]")
            raise typer.Exit(code=0)

    # Deploy
    chart_dir = Path("helm-umbrella") if Path("helm-umbrella").exists() else None
    deployer = HelmDeployer(
        loader=loader,
        release_name=release_name,
        namespace=namespace,
        chart_repository=settings.helm_chart_repository,
        timeout_seconds=settings.helm_timeout_seconds,
        umbrella_chart_dir=chart_dir,
    )

    result = deployer.install(resolution, dry_run=dry_run)

    if result.success:
        console.print(Panel("[bold green]Installation complete.[/bold green]", expand=False))
    else:
        console.print(f"[red]Helm deployment failed:[/red]\n{result.stderr}")
        raise typer.Exit(code=2)

    # Post-install health check
    if not skip_health_check and not dry_run:
        import asyncio
        from aumos_composable_installer.health.checker import HealthChecker
        checker = HealthChecker(
            loader=loader,
            timeout_seconds=settings.health_check_timeout_seconds,
            interval_seconds=settings.health_check_interval_seconds,
        )
        report = asyncio.run(checker.check_all(resolution.install_order))
        if not report.all_healthy:
            console.print(f"[red]Health check failed for: {report.unhealthy_modules}[/red]")
            raise typer.Exit(code=2)
        console.print(f"[green]All {report.healthy_count} module(s) healthy.[/green]")


# Default command alias
@app.callback(invoke_without_command=True)
def _default(ctx: typer.Context) -> None:
    """Show help if no sub-command given.

    Args:
        ctx: Typer context.
    """
    if ctx.invoked_subcommand is None:
        console.print(ctx.get_help())

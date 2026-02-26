"""CLI command: aumos status.

Reports the current activation status of all AumOS modules by querying
the Helm release and (optionally) the module health endpoints.
"""

from __future__ import annotations

import json

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from aumos_common.observability import get_logger

from aumos_composable_installer.deployer.helm_deployer import HelmDeployer
from aumos_composable_installer.resolver.dependency_graph import AUMOS_MODULE_DEPS
from aumos_composable_installer.resolver.module_manifest import ManifestLoader
from aumos_composable_installer.settings import Settings

logger = get_logger(__name__)
console = Console()

app = typer.Typer(
    help="Show the current status of the AumOS platform installation.",
)


@app.command("run")
def status_run(
    namespace: str = typer.Option("aumos", "--namespace", "-n", help="Kubernetes namespace."),
    release_name: str = typer.Option("aumos", "--release", "-r", help="Helm release name."),
    output: str = typer.Option("rich", "--output", "-o", help="Output format: rich | json."),
    with_health: bool = typer.Option(False, "--health", help="Also run health checks on each module."),
) -> None:
    """Show the current status of the AumOS platform installation.

    Queries the Helm release for active modules and optionally performs
    live health checks against each module's health endpoint.

    Args:
        namespace: Kubernetes namespace to query.
        release_name: Helm release name to inspect.
        output: Output format (rich table or JSON).
        with_health: Run live health checks alongside status display.
    """
    settings = Settings()
    loader = ManifestLoader(settings.manifest_dir)

    deployer = HelmDeployer(
        loader=loader,
        release_name=release_name,
        namespace=namespace,
        chart_repository=settings.helm_chart_repository,
        timeout_seconds=settings.helm_timeout_seconds,
    )

    helm_status = deployer.get_release_status()

    # Load license info for tier display
    licensed_modules: set[str] = set()
    license_tier = "A"
    try:
        from aumos_composable_installer.license.key_manager import KeyManager
        from aumos_composable_installer.license.validator import LicenseValidator
        validator = LicenseValidator(settings.license_public_key_path)
        key_manager = KeyManager(settings.license_key_path, validator)
        if key_manager.is_activated():
            license_info = key_manager.load()
            licensed_modules = license_info.modules
            license_tier = license_info.tier
    except Exception as exc:
        logger.debug("Could not load license info for status display", error=str(exc))

    if output == "json":
        status_data = {
            "release": release_name,
            "namespace": namespace,
            "helm_status": helm_status,
            "license_tier": license_tier,
            "known_modules": sorted(AUMOS_MODULE_DEPS.keys()),
        }
        console.print(json.dumps(status_data, indent=2))
        return

    # Rich output
    console.print(Panel("[bold cyan]AumOS Platform Status[/bold cyan]", expand=False))

    if "error" in helm_status:
        console.print(f"[yellow]Helm release '{release_name}' not found or not accessible.[/yellow]")
        console.print(f"[dim]{helm_status.get('error', '')}[/dim]")
    else:
        release_status = helm_status.get("info", {}).get("status", "unknown") if "info" in helm_status else "deployed"
        console.print(f"Release: [bold]{release_name}[/bold]  Namespace: [bold]{namespace}[/bold]  "
                      f"Status: [green]{release_status}[/green]")

    console.print(f"License Tier: [bold cyan]{license_tier}[/bold cyan]")

    module_table = Table(title="Module Registry", show_header=True)
    module_table.add_column("Module", style="bold")
    module_table.add_column("Dependencies")
    module_table.add_column("Licensed")

    for module_name, deps in sorted(AUMOS_MODULE_DEPS.items()):
        is_licensed = module_name in licensed_modules or license_tier == "A"
        licensed_str = "[green]yes[/green]" if is_licensed else "[dim]no[/dim]"
        module_table.add_row(module_name, ", ".join(deps) if deps else "[dim]none[/dim]", licensed_str)

    console.print(module_table)

    if with_health:
        import asyncio
        from aumos_composable_installer.health.checker import HealthChecker
        checker = HealthChecker(
            loader=loader,
            timeout_seconds=30,
            interval_seconds=5,
        )
        module_names = list(AUMOS_MODULE_DEPS.keys())
        report = asyncio.run(checker.check_all(module_names))

        health_table = Table(title="Health Status", show_header=True)
        health_table.add_column("Module")
        health_table.add_column("Status")
        health_table.add_column("HTTP")
        health_table.add_column("Response Time")

        for module_name, result in sorted(report.results.items()):
            status_color = "green" if result.is_healthy else "red"
            health_table.add_row(
                module_name,
                f"[{status_color}]{result.status.value}[/{status_color}]",
                str(result.http_status) if result.http_status else "[dim]N/A[/dim]",
                f"{result.response_time_ms:.0f}ms" if result.response_time_ms else "[dim]N/A[/dim]",
            )
        console.print(health_table)


@app.callback(invoke_without_command=True)
def _default(ctx: typer.Context) -> None:
    """Show status directly when called without sub-command.

    Args:
        ctx: Typer context.
    """
    if ctx.invoked_subcommand is None:
        status_run()

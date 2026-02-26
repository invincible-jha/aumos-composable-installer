"""CLI command: aumos activate.

Activates a single AumOS module on an existing platform installation.
Validates license entitlements, resolves dependencies, checks conflicts,
and delegates to the Helm deployer.
"""

from __future__ import annotations

import typer
from rich.console import Console
from rich.panel import Panel

from aumos_common.observability import get_logger

from aumos_composable_installer.deployer.helm_deployer import HelmDeployer
from aumos_composable_installer.resolver.conflict_detector import ConflictDetector, ConflictSeverity
from aumos_composable_installer.resolver.dependency_graph import DependencyGraph
from aumos_composable_installer.resolver.module_manifest import ManifestLoader
from aumos_composable_installer.settings import Settings

logger = get_logger(__name__)
console = Console()

app = typer.Typer(
    help="Activate a module on an existing AumOS installation.",
    no_args_is_help=True,
)


@app.command("run")
def activate_run(
    module: str = typer.Argument(..., help="Module name to activate (e.g., data-factory)."),
    namespace: str = typer.Option("aumos", "--namespace", "-n", help="Kubernetes namespace."),
    release_name: str = typer.Option("aumos", "--release", "-r", help="Helm release name."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Simulate activation without applying changes."),
) -> None:
    """Activate a module on an existing AumOS installation.

    Resolves transitive dependencies for the module, validates license
    entitlements, checks for conflicts, and enables the module via Helm.

    Args:
        module: The module name to activate.
        namespace: Kubernetes namespace of the deployment.
        release_name: Helm release name.
        dry_run: If True, simulate without applying.
    """
    settings = Settings()

    console.print(Panel(f"[bold cyan]Activating module:[/bold cyan] {module}", expand=False))

    if dry_run:
        console.print("[yellow]DRY RUN — no changes will be applied[/yellow]")

    loader = ManifestLoader(settings.manifest_dir)
    graph = DependencyGraph(loader)

    try:
        resolution = graph.resolve([module])
    except KeyError as exc:
        console.print(f"[red]Unknown module:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    # Load license info
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

    detector = ConflictDetector(loader)
    conflicts = detector.check(resolution.all_modules, licensed_modules)

    for conflict in conflicts:
        severity_color = "red" if conflict.severity == ConflictSeverity.ERROR else "yellow"
        console.print(f"[{severity_color}]{conflict.severity.value.upper()}:[/{severity_color}] {conflict.message}")
        if conflict.remediation:
            console.print(f"  Remediation: {conflict.remediation}")

    if detector.has_blocking_conflicts(conflicts):
        console.print("[red]Activation blocked due to conflicts.[/red]")
        raise typer.Exit(code=1)

    if resolution.auto_included:
        console.print(f"[dim]Auto-including dependencies: {sorted(resolution.auto_included)}[/dim]")

    deployer = HelmDeployer(
        loader=loader,
        release_name=release_name,
        namespace=namespace,
        chart_repository=settings.helm_chart_repository,
        timeout_seconds=settings.helm_timeout_seconds,
    )

    result = deployer.activate_module(module, dry_run=dry_run)

    if result.success:
        console.print(Panel(f"[bold green]Module '{module}' activated.[/bold green]", expand=False))
    else:
        console.print(f"[red]Activation failed:[/red]\n{result.stderr}")
        raise typer.Exit(code=2)


@app.callback(invoke_without_command=True)
def _default(ctx: typer.Context) -> None:
    """Show help if no sub-command given.

    Args:
        ctx: Typer context.
    """
    if ctx.invoked_subcommand is None:
        console.print(ctx.get_help())

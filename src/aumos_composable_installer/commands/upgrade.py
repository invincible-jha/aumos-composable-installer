"""CLI command: aumos upgrade.

Upgrades an existing AumOS platform installation to a newer chart version
or applies updated values while preserving the current module activation set.
"""

from __future__ import annotations

import typer
from rich.console import Console
from rich.panel import Panel

from aumos_common.observability import get_logger

from aumos_composable_installer.deployer.helm_deployer import HelmDeployer
from aumos_composable_installer.resolver.dependency_graph import DependencyGraph, FOUNDATION_MODULES
from aumos_composable_installer.resolver.module_manifest import ManifestLoader
from aumos_composable_installer.settings import Settings

logger = get_logger(__name__)
console = Console()

app = typer.Typer(
    help="Upgrade the AumOS platform to a newer chart version.",
    no_args_is_help=True,
)


@app.command("run")
def upgrade_run(
    modules: str = typer.Option(
        "",
        "--modules",
        "-m",
        help="Comma-separated additional modules to activate during upgrade.",
    ),
    chart_version: str = typer.Option("", "--chart-version", "-c", help="Target chart version (empty = latest)."),
    namespace: str = typer.Option("aumos", "--namespace", "-n", help="Kubernetes namespace."),
    release_name: str = typer.Option("aumos", "--release", "-r", help="Helm release name."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Simulate upgrade without applying changes."),
) -> None:
    """Upgrade the AumOS platform to a newer chart version.

    Runs `helm upgrade` with the specified chart version, preserving
    existing values. Optionally activates additional modules during upgrade.

    Args:
        modules: Additional modules to activate during upgrade.
        chart_version: Target chart version string (empty = latest).
        namespace: Kubernetes namespace of the deployment.
        release_name: Helm release name.
        dry_run: If True, simulate without applying.
    """
    settings = Settings()

    requested_module_list = [m.strip() for m in modules.split(",") if m.strip()]
    all_requested = list(FOUNDATION_MODULES) + requested_module_list

    console.print(Panel("[bold cyan]AumOS Platform Upgrade[/bold cyan]", expand=False))

    if dry_run:
        console.print("[yellow]DRY RUN — no changes will be applied[/yellow]")

    if chart_version:
        console.print(f"Target chart version: [bold]{chart_version}[/bold]")
    else:
        console.print("Target chart version: [dim]latest[/dim]")

    loader = ManifestLoader(settings.manifest_dir)
    graph = DependencyGraph(loader)

    try:
        resolution = graph.resolve(all_requested)
    except KeyError as exc:
        console.print(f"[red]Unknown module:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    if not dry_run:
        should_proceed = typer.confirm(
            f"Upgrade '{release_name}' in namespace '{namespace}' "
            f"with {resolution.total_count} module(s)?"
        )
        if not should_proceed:
            console.print("[yellow]Upgrade cancelled.[/yellow]")
            raise typer.Exit(code=0)

    deployer = HelmDeployer(
        loader=loader,
        release_name=release_name,
        namespace=namespace,
        chart_repository=settings.helm_chart_repository,
        chart_version=chart_version,
        timeout_seconds=settings.helm_timeout_seconds,
    )

    result = deployer.install(resolution, dry_run=dry_run)

    if result.success:
        console.print(Panel("[bold green]Upgrade complete.[/bold green]", expand=False))
    else:
        console.print(f"[red]Upgrade failed:[/red]\n{result.stderr}")
        raise typer.Exit(code=2)


@app.callback(invoke_without_command=True)
def _default(ctx: typer.Context) -> None:
    """Show help if no sub-command given.

    Args:
        ctx: Typer context.
    """
    if ctx.invoked_subcommand is None:
        console.print(ctx.get_help())

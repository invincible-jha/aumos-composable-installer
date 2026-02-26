"""CLI command: aumos deactivate.

Deactivates a module on an existing AumOS platform installation.
Checks for downstream dependents that would break, warns the operator,
and delegates to the Helm deployer.
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
    help="Deactivate a module on an existing AumOS installation.",
    no_args_is_help=True,
)


@app.command("run")
def deactivate_run(
    module: str = typer.Argument(..., help="Module name to deactivate (e.g., data-factory)."),
    namespace: str = typer.Option("aumos", "--namespace", "-n", help="Kubernetes namespace."),
    release_name: str = typer.Option("aumos", "--release", "-r", help="Helm release name."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Simulate deactivation without applying changes."),
    force: bool = typer.Option(False, "--force", help="Deactivate even if dependents exist."),
) -> None:
    """Deactivate a module on an existing AumOS installation.

    Checks that no other active modules depend on this module before
    deactivating. Foundation modules cannot be deactivated.

    Args:
        module: The module name to deactivate.
        namespace: Kubernetes namespace of the deployment.
        release_name: Helm release name.
        dry_run: If True, simulate without applying.
        force: Bypass dependent check (use with caution).
    """
    settings = Settings()

    console.print(Panel(f"[bold cyan]Deactivating module:[/bold cyan] {module}", expand=False))

    if dry_run:
        console.print("[yellow]DRY RUN — no changes will be applied[/yellow]")

    # Prevent deactivation of foundation modules
    if module in FOUNDATION_MODULES:
        console.print(
            f"[red]Cannot deactivate foundation module '{module}'.[/red] "
            "Foundation modules are required for platform operation."
        )
        raise typer.Exit(code=1)

    loader = ManifestLoader(settings.manifest_dir)
    graph = DependencyGraph(loader)
    graph.build()

    # Check for downstream dependents
    dependents = graph.get_dependents(module)
    if dependents and not force:
        console.print(
            f"[yellow]Warning:[/yellow] The following modules depend on '{module}' "
            f"and would be affected: {sorted(dependents)}"
        )
        console.print("Use [bold]--force[/bold] to deactivate anyway, or deactivate the dependents first.")
        raise typer.Exit(code=1)

    if dependents and force:
        console.print(f"[yellow]Force-deactivating '{module}' despite dependents: {sorted(dependents)}[/yellow]")

    deployer = HelmDeployer(
        loader=loader,
        release_name=release_name,
        namespace=namespace,
        chart_repository=settings.helm_chart_repository,
        timeout_seconds=settings.helm_timeout_seconds,
    )

    result = deployer.deactivate_module(module, dry_run=dry_run)

    if result.success:
        console.print(Panel(f"[bold green]Module '{module}' deactivated.[/bold green]", expand=False))
    else:
        console.print(f"[red]Deactivation failed:[/red]\n{result.stderr}")
        raise typer.Exit(code=2)


@app.callback(invoke_without_command=True)
def _default(ctx: typer.Context) -> None:
    """Show help if no sub-command given.

    Args:
        ctx: Typer context.
    """
    if ctx.invoked_subcommand is None:
        console.print(ctx.get_help())

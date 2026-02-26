"""AumOS Composable Installer CLI entry point.

Provides the `aumos` command with sub-commands for installing, activating,
deactivating, upgrading, and diagnosing the AumOS Enterprise platform.
"""

import typer
from rich.console import Console

from aumos_composable_installer import __version__
from aumos_composable_installer.commands import (
    activate,
    deactivate,
    diagnose,
    install,
    status,
    upgrade,
)

app = typer.Typer(
    name="aumos",
    help="AumOS Enterprise Platform Installer — composable, selective-activation deployment.",
    add_completion=True,
    rich_markup_mode="rich",
    no_args_is_help=True,
)

console = Console()

# Register sub-command groups
app.add_typer(install.app, name="install")
app.add_typer(activate.app, name="activate")
app.add_typer(deactivate.app, name="deactivate")
app.add_typer(status.app, name="status")
app.add_typer(upgrade.app, name="upgrade")
app.add_typer(diagnose.app, name="diagnose")


@app.callback(invoke_without_command=True)
def main(
    version: bool = typer.Option(False, "--version", "-v", help="Print version and exit.", is_eager=True),
) -> None:
    """AumOS Enterprise Platform Installer.

    Install AumOS once, then selectively activate modules based on your
    license entitlements and infrastructure needs.

    [bold]Quick start:[/bold]

      aumos install --cloud aws --modules core,auth-gateway,event-bus

      aumos activate --module data-factory

      aumos status

    Args:
        version: If true, print the version string and exit.
    """
    if version:
        console.print(f"aumos-composable-installer [bold cyan]{__version__}[/bold cyan]")
        raise typer.Exit()


if __name__ == "__main__":
    app()

"""CLI command: aumos config — manage installer configuration and telemetry.

Gap #13: Telemetry opt-in/opt-out configuration command.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from aumos_common.observability import get_logger

from aumos_composable_installer.settings import Settings

logger = get_logger(__name__)
console = Console()

app = typer.Typer(
    help="Manage AumOS installer configuration.",
    no_args_is_help=True,
)

_CONFIG_FILE = Path.home() / ".aumos" / "installer-config.json"


def _load_config() -> dict[str, Any]:
    """Load the installer config file from disk.

    Returns:
        Config dict, or empty dict if the file does not exist.
    """
    if _CONFIG_FILE.exists():
        try:
            return json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def _save_config(config: dict[str, Any]) -> None:
    """Save the installer config dict to disk.

    Args:
        config: Config dict to persist.
    """
    _CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    _CONFIG_FILE.write_text(json.dumps(config, indent=2), encoding="utf-8")


@app.command("show")
def config_show() -> None:
    """Display current installer configuration.

    Shows all persistent configuration values including telemetry opt-in status.
    """
    config = _load_config()
    settings = Settings()

    table = Table(title="AumOS Installer Configuration", show_header=True)
    table.add_column("Key", style="bold")
    table.add_column("Value")
    table.add_column("Source")

    table.add_row("telemetry.enabled", str(config.get("telemetry_enabled", False)), "config file")
    table.add_row("default_deploy_mode", settings.default_deploy_mode, "environment / default")
    table.add_row("helm_namespace", settings.helm_namespace, "environment / default")
    table.add_row("helm_chart_repository", settings.helm_chart_repository, "environment / default")
    table.add_row("state_file_path", str(settings.state_file_path), "environment / default")

    console.print(table)


@app.command("telemetry")
def config_telemetry(
    enable: bool = typer.Option(..., "--enable/--disable", help="Enable or disable anonymous telemetry."),
) -> None:
    """Opt in or out of anonymous telemetry reporting.

    Telemetry sends anonymous installation events (no PII, no license data)
    to help improve AumOS. See https://docs.aumos.ai/privacy for full details.

    Args:
        enable: Whether to enable telemetry.
    """
    config = _load_config()
    config["telemetry_enabled"] = enable
    _save_config(config)

    status = "[green]enabled[/green]" if enable else "[yellow]disabled[/yellow]"
    console.print(f"Telemetry {status}.")

    if enable:
        console.print(
            "Anonymous usage data will be sent to improve AumOS.\n"
            "No PII or license key content is ever included.\n"
            "See [link=https://docs.aumos.ai/privacy]https://docs.aumos.ai/privacy[/link] for details."
        )
    else:
        console.print("No telemetry data will be sent.")


@app.command("set")
def config_set(
    key: str = typer.Argument(..., help="Configuration key to set."),
    value: str = typer.Argument(..., help="Value to set."),
) -> None:
    """Set a persistent configuration key.

    Args:
        key: Configuration key.
        value: Value to assign.
    """
    config = _load_config()
    config[key] = value
    _save_config(config)
    console.print(f"[green]Set[/green] {key} = {value}")

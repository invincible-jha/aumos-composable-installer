"""CLI command: aumos diagnose.

Runs a comprehensive diagnostic on the AumOS installation: checks Helm
release state, license validity, module health, and dependency graph integrity.
"""

from __future__ import annotations

import json
import sys

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from aumos_common.observability import get_logger

from aumos_composable_installer.resolver.conflict_detector import ConflictDetector, ConflictSeverity
from aumos_composable_installer.resolver.dependency_graph import AUMOS_MODULE_DEPS, DependencyGraph
from aumos_composable_installer.resolver.module_manifest import ManifestLoader
from aumos_composable_installer.settings import Settings

logger = get_logger(__name__)
console = Console()

app = typer.Typer(
    help="Run diagnostics on the AumOS platform installation.",
)


@app.command("run")
def diagnose_run(
    namespace: str = typer.Option("aumos", "--namespace", "-n", help="Kubernetes namespace."),
    release_name: str = typer.Option("aumos", "--release", "-r", help="Helm release name."),
    output: str = typer.Option("rich", "--output", "-o", help="Output format: rich | json."),
) -> None:
    """Run a comprehensive diagnostic on the AumOS installation.

    Checks the following:
    - Python and tool versions
    - License key validity and entitlements
    - Module manifest loading
    - Dependency graph integrity (no cycles)
    - Helm release status
    - Conflict detection on known modules

    Args:
        namespace: Kubernetes namespace to inspect.
        release_name: Helm release name to inspect.
        output: Output format (rich or JSON).
    """
    settings = Settings()
    diagnostics: list[dict[str, str]] = []

    console.print(Panel("[bold cyan]AumOS Diagnostic Report[/bold cyan]", expand=False))

    # -- Python version --
    python_version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    diagnostics.append({
        "check": "Python version",
        "status": "ok" if sys.version_info >= (3, 11) else "warn",
        "detail": python_version,
    })

    # -- License check --
    license_status = "no_key"
    license_detail = "No license key found"
    licensed_modules: set[str] = set()
    try:
        from aumos_composable_installer.license.key_manager import KeyManager
        from aumos_composable_installer.license.validator import LicenseValidator
        validator = LicenseValidator(settings.license_public_key_path)
        key_manager = KeyManager(settings.license_key_path, validator)
        if key_manager.is_activated():
            license_info = key_manager.load()
            licensed_modules = license_info.modules
            if license_info.is_expired:
                license_status = "expired"
                license_detail = f"Expired on {license_info.expires_at.date()}"
            else:
                license_status = "ok"
                license_detail = (
                    f"Tier {license_info.tier} | {license_info.days_remaining} days remaining | "
                    f"{len(license_info.modules)} module(s) licensed"
                )
        else:
            license_detail = "Not activated — run `aumos license activate --key <KEY>`"
    except Exception as exc:
        license_status = "error"
        license_detail = str(exc)

    diagnostics.append({
        "check": "License",
        "status": license_status,
        "detail": license_detail,
    })

    # -- Manifest loading --
    try:
        loader = ManifestLoader(settings.manifest_dir)
        manifests = loader.load_all()
        diagnostics.append({
            "check": "Module manifests",
            "status": "ok",
            "detail": f"{len(manifests)} manifest(s) loaded from {settings.manifest_dir}",
        })
    except Exception as exc:
        loader = ManifestLoader(settings.manifest_dir)
        diagnostics.append({
            "check": "Module manifests",
            "status": "warn",
            "detail": f"Could not load manifests: {exc}",
        })

    # -- Dependency graph integrity --
    try:
        graph = DependencyGraph(loader)
        graph.build()
        # Try resolving all known modules to check for cycles
        all_modules = list(AUMOS_MODULE_DEPS.keys())
        resolution = graph.resolve(all_modules)
        diagnostics.append({
            "check": "Dependency graph",
            "status": "ok",
            "detail": f"No cycles detected — {resolution.total_count} module(s) resolve cleanly",
        })
    except Exception as exc:
        diagnostics.append({
            "check": "Dependency graph",
            "status": "error",
            "detail": str(exc),
        })

    # -- Conflict detection --
    try:
        detector = ConflictDetector(loader)
        all_module_set = set(AUMOS_MODULE_DEPS.keys())
        conflicts = detector.check(all_module_set, licensed_modules)
        blocking = [c for c in conflicts if c.severity == ConflictSeverity.ERROR]
        diagnostics.append({
            "check": "Conflict detection",
            "status": "error" if blocking else "ok",
            "detail": (
                f"{len(blocking)} blocking conflict(s)" if blocking
                else f"No conflicts ({len(conflicts)} warning(s))"
            ),
        })
    except Exception as exc:
        diagnostics.append({
            "check": "Conflict detection",
            "status": "error",
            "detail": str(exc),
        })

    # -- Helm release --
    try:
        from aumos_composable_installer.deployer.helm_deployer import HelmDeployer
        deployer = HelmDeployer(
            loader=loader,
            release_name=release_name,
            namespace=namespace,
            chart_repository=settings.helm_chart_repository,
            timeout_seconds=30,
        )
        helm_status = deployer.get_release_status()
        if "error" in helm_status:
            diagnostics.append({
                "check": "Helm release",
                "status": "warn",
                "detail": f"Release '{release_name}' not found in namespace '{namespace}'",
            })
        else:
            release_state = helm_status.get("info", {}).get("status", "unknown")
            diagnostics.append({
                "check": "Helm release",
                "status": "ok" if release_state == "deployed" else "warn",
                "detail": f"'{release_name}' — {release_state}",
            })
    except Exception as exc:
        diagnostics.append({
            "check": "Helm release",
            "status": "warn",
            "detail": f"Could not query Helm: {exc}",
        })

    # -- Output --
    if output == "json":
        console.print(json.dumps({"diagnostics": diagnostics}, indent=2))
        return

    diag_table = Table(show_header=True, title="Diagnostic Results")
    diag_table.add_column("Check", style="bold")
    diag_table.add_column("Status")
    diag_table.add_column("Detail")

    overall_ok = True
    for entry in diagnostics:
        status_val = entry["status"]
        if status_val in ("error", "expired", "no_key"):
            status_display = "[red]FAIL[/red]"
            overall_ok = False
        elif status_val == "warn":
            status_display = "[yellow]WARN[/yellow]"
        else:
            status_display = "[green]OK[/green]"
        diag_table.add_row(entry["check"], status_display, entry["detail"])

    console.print(diag_table)

    if overall_ok:
        console.print(Panel("[bold green]All checks passed.[/bold green]", expand=False))
    else:
        console.print(Panel("[bold red]Some checks failed — see table above.[/bold red]", expand=False))
        raise typer.Exit(code=1)


@app.callback(invoke_without_command=True)
def _default(ctx: typer.Context) -> None:
    """Run diagnostics directly when called without sub-command.

    Args:
        ctx: Typer context.
    """
    if ctx.invoked_subcommand is None:
        diagnose_run()

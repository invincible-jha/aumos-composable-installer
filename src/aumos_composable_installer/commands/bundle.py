"""CLI command: aumos bundle — create and load air-gapped installation bundles.

Gap #8: Air-gapped installation support.
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel

from aumos_common.observability import get_logger

from aumos_composable_installer.bundle.creator import BundleCreator
from aumos_composable_installer.bundle.loader import BundleLoader
from aumos_composable_installer.resolver.dependency_graph import DependencyGraph, FOUNDATION_MODULES
from aumos_composable_installer.resolver.module_manifest import ManifestLoader
from aumos_composable_installer.settings import Settings

logger = get_logger(__name__)
console = Console()

app = typer.Typer(
    help="Create and load air-gapped AumOS installation bundles.",
    no_args_is_help=True,
)


@app.command("create")
def bundle_create(
    modules: str = typer.Option(
        "",
        "--modules",
        "-m",
        help="Comma-separated optional modules to include (foundation always included).",
    ),
    output_dir: Path = typer.Option(
        Path("./aumos-bundle"),
        "--output-dir",
        "-o",
        help="Directory to write the bundle archive.",
    ),
    bundle_name: str = typer.Option(
        "aumos-bundle.tar.gz",
        "--name",
        help="Bundle archive filename.",
    ),
    aumos_version: str = typer.Option(
        "latest",
        "--version",
        "-v",
        help="AumOS platform version to bundle.",
    ),
) -> None:
    """Create an air-gapped installation bundle.

    Pulls Helm charts and container images for the selected modules and
    packages them into a portable .tar.gz archive for offline deployment.

    Args:
        modules: Optional modules to include beyond foundation.
        output_dir: Directory to write the bundle archive.
        bundle_name: Filename for the output archive.
        aumos_version: AumOS version to bundle.
    """
    settings = Settings()

    requested = [m.strip() for m in modules.split(",") if m.strip()]
    all_modules = list(FOUNDATION_MODULES) + requested

    console.print(Panel(f"[bold cyan]Creating AumOS Bundle v{aumos_version}[/bold cyan]", expand=False))
    console.print(f"Modules: {', '.join(all_modules)}")

    loader = ManifestLoader(settings.manifest_dir)
    graph = DependencyGraph(loader)
    resolution = graph.resolve(all_modules)

    creator = BundleCreator(
        loader=loader,
        chart_repository=settings.helm_chart_repository,
        output_dir=output_dir,
        aumos_version=aumos_version,
    )

    bundle_path = creator.create(resolution.install_order, bundle_filename=bundle_name)
    console.print(f"[green]Bundle created:[/green] {bundle_path}")
    console.print(f"[green]Modules included:[/green] {resolution.total_count}")


@app.command("load")
def bundle_load(
    bundle_path: Path = typer.Argument(..., help="Path to the bundle .tar.gz archive."),
    local_registry: str = typer.Option(
        "",
        "--registry",
        "-r",
        help="Local container registry to push images to (e.g. localhost:5000).",
    ),
    load_images: bool = typer.Option(True, "--load-images/--no-load-images", help="Load container images."),
) -> None:
    """Load an air-gapped bundle for offline installation.

    Extracts the bundle, loads container images into Docker or a local
    registry, and displays chart paths ready for Helm installation.

    Args:
        bundle_path: Path to the bundle archive.
        local_registry: Optional local registry to push images into.
        load_images: Whether to load/push container images.
    """
    console.print(Panel("[bold cyan]Loading AumOS Bundle[/bold cyan]", expand=False))

    registry = local_registry or None
    bundle_loader = BundleLoader(bundle_path=bundle_path, local_registry=registry)

    manifest = bundle_loader.extract()
    console.print(f"[green]Bundle extracted.[/green] AumOS version: {manifest.aumos_version}")
    console.print(f"Modules: {', '.join(c.module_name for c in manifest.modules)}")

    if load_images:
        console.print("Loading container images...")
        bundle_loader.load_images(manifest)
        console.print("[green]Images loaded.[/green]")

    chart_paths = bundle_loader.get_chart_paths(manifest)
    console.print("\n[bold]Chart paths for offline Helm install:[/bold]")
    for module_name, chart_path in chart_paths.items():
        console.print(f"  {module_name}: {chart_path}")

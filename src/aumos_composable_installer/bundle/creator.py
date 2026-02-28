"""Air-gapped bundle creator for offline AumOS installations.

Pulls all required Helm charts and container images for the selected modules,
saves them to a local tarball archive, and writes a bundle manifest. The resulting
bundle can be transferred to an air-gapped environment and loaded with BundleLoader.
"""

from __future__ import annotations

import subprocess
import tarfile
import tempfile
from pathlib import Path
from typing import Any

from aumos_common.observability import get_logger
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

from aumos_composable_installer.bundle.manifest import BundleManifest, BundledChart, BundledImage
from aumos_composable_installer.resolver.module_manifest import ManifestLoader

logger = get_logger(__name__)
console = Console()


class BundleCreator:
    """Creates air-gapped installation bundles from selected AumOS modules.

    Pulls charts from the OCI registry and images from their registries,
    then packages everything into a single .tar.gz bundle with a manifest.
    """

    def __init__(
        self,
        loader: ManifestLoader,
        chart_repository: str,
        output_dir: Path,
        aumos_version: str = "latest",
    ) -> None:
        """Initialize the BundleCreator.

        Args:
            loader: Module manifest loader.
            chart_repository: OCI registry URL for AumOS Helm charts.
            output_dir: Directory where the bundle archive will be written.
            aumos_version: AumOS platform version tag.
        """
        self._loader = loader
        self._chart_repository = chart_repository
        self._output_dir = output_dir
        self._aumos_version = aumos_version

    def create(self, module_names: list[str], bundle_filename: str = "aumos-bundle.tar.gz") -> Path:
        """Create an air-gapped bundle for the specified modules.

        Args:
            module_names: List of AumOS module names to include.
            bundle_filename: Output filename for the bundle archive.

        Returns:
            Path to the created bundle archive.
        """
        output_dir = self._output_dir
        output_dir.mkdir(parents=True, exist_ok=True)
        bundle_path = output_dir / bundle_filename

        manifest = BundleManifest(aumos_version=self._aumos_version)

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            charts_dir = tmp_path / "charts"
            images_dir = tmp_path / "images"
            charts_dir.mkdir()
            images_dir.mkdir()

            with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"), console=console) as progress:
                for module_name in module_names:
                    task = progress.add_task(f"Pulling {module_name}...", total=None)
                    bundled_chart = self._pull_chart(module_name, charts_dir)
                    bundled_chart.images = self._save_images(module_name, images_dir)
                    manifest.modules.append(bundled_chart)
                    progress.remove_task(task)

            manifest.save(tmp_path / "bundle-manifest.yaml")

            with tarfile.open(bundle_path, "w:gz") as tar:
                tar.add(tmp_path, arcname=".")

            manifest.total_size_bytes = bundle_path.stat().st_size

        logger.info("bundle_created", path=str(bundle_path), modules=len(module_names))
        return bundle_path

    def _pull_chart(self, module_name: str, charts_dir: Path) -> BundledChart:
        """Pull a Helm chart from the OCI registry into the charts directory.

        Args:
            module_name: AumOS module name.
            charts_dir: Directory to save the pulled chart.

        Returns:
            BundledChart record for the manifest.
        """
        chart_ref = f"{self._chart_repository}/{module_name}"
        result = subprocess.run(
            ["helm", "pull", chart_ref, "--destination", str(charts_dir), "--untar=false"],
            capture_output=True,
            text=True,
            check=False,
        )

        chart_version = "unknown"
        filename = f"{module_name}-0.0.0.tgz"

        if result.returncode != 0:
            logger.warning("chart_pull_failed", module=module_name, stderr=result.stderr)
        else:
            tgz_files = list(charts_dir.glob(f"{module_name}-*.tgz"))
            if tgz_files:
                filename = tgz_files[-1].name
                chart_version = filename.replace(f"{module_name}-", "").replace(".tgz", "")

        return BundledChart(module_name=module_name, chart_version=chart_version, filename=filename)

    def _save_images(self, module_name: str, images_dir: Path) -> list[BundledImage]:
        """Save container images for a module to disk as tarballs.

        Args:
            module_name: AumOS module name.
            images_dir: Directory to save image tarballs.

        Returns:
            List of BundledImage records for the manifest.
        """
        try:
            manifest = self._loader.load(module_name)
            container_images: list[Any] = getattr(manifest, "container_images", [])
        except Exception:
            container_images = []

        saved: list[BundledImage] = []
        for image_ref in container_images:
            tag = str(image_ref).split(":")[-1] if ":" in str(image_ref) else "latest"
            repository = str(image_ref).rsplit(":", 1)[0]
            tar_filename = f"{module_name}-{tag}.tar"
            tar_path = images_dir / tar_filename

            result = subprocess.run(
                ["docker", "save", "-o", str(tar_path), str(image_ref)],
                capture_output=True,
                text=True,
                check=False,
            )

            if result.returncode == 0:
                digest = f"sha256:{tar_path.stat().st_size}"
                saved.append(BundledImage(repository=repository, tag=tag, digest=digest, tar_filename=tar_filename))
            else:
                logger.warning("image_save_failed", image=str(image_ref), stderr=result.stderr)

        return saved

"""Air-gapped bundle loader for installing AumOS in offline environments.

Extracts a bundle archive created by BundleCreator, loads container images
into a local registry (or Docker), and provides Helm charts for offline
chart installation.
"""

from __future__ import annotations

import subprocess
import tarfile
import tempfile
from pathlib import Path

from aumos_common.observability import get_logger
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

from aumos_composable_installer.bundle.manifest import BundleManifest, BundledChart

logger = get_logger(__name__)
console = Console()


class BundleLoader:
    """Loads an air-gapped bundle archive for offline AumOS installation.

    Extracts the bundle, reads the manifest, loads images into Docker
    or a local registry, and returns chart paths for Helm to install.
    """

    def __init__(self, bundle_path: Path, local_registry: str | None = None) -> None:
        """Initialize the BundleLoader.

        Args:
            bundle_path: Path to the bundle .tar.gz archive.
            local_registry: Optional local container registry to push images to
                            (e.g., localhost:5000). If None, loads into Docker daemon.
        """
        self._bundle_path = bundle_path
        self._local_registry = local_registry
        self._extract_dir: Path | None = None

    def extract(self) -> BundleManifest:
        """Extract the bundle archive and parse the manifest.

        Returns:
            Parsed BundleManifest from the bundle.

        Raises:
            FileNotFoundError: If the bundle or manifest is missing.
            ValueError: If the manifest cannot be parsed.
        """
        if not self._bundle_path.exists():
            raise FileNotFoundError(f"Bundle not found: {self._bundle_path}")

        self._extract_dir = Path(tempfile.mkdtemp(prefix="aumos-bundle-"))

        with tarfile.open(self._bundle_path, "r:gz") as tar:
            tar.extractall(self._extract_dir)

        manifest_path = self._extract_dir / "bundle-manifest.yaml"
        if not manifest_path.exists():
            raise FileNotFoundError(f"bundle-manifest.yaml not found inside {self._bundle_path}")

        manifest = BundleManifest.load(manifest_path)
        logger.info("bundle_extracted", path=str(self._bundle_path), modules=len(manifest.modules))
        return manifest

    def load_images(self, manifest: BundleManifest) -> None:
        """Load all container images from the bundle into Docker or a local registry.

        Args:
            manifest: The bundle manifest describing included images.

        Raises:
            RuntimeError: If the bundle has not been extracted yet.
        """
        if self._extract_dir is None:
            raise RuntimeError("Call extract() before load_images()")

        images_dir = self._extract_dir / "images"

        with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"), console=console) as progress:
            for bundled_chart in manifest.modules:
                for image in bundled_chart.images:
                    tar_path = images_dir / image.tar_filename
                    if not tar_path.exists():
                        logger.warning("image_tar_missing", filename=image.tar_filename)
                        continue

                    task = progress.add_task(f"Loading {image.repository}:{image.tag}...", total=None)
                    self._load_image(tar_path, image.repository, image.tag)
                    progress.remove_task(task)

    def get_chart_paths(self, manifest: BundleManifest) -> dict[str, Path]:
        """Get local filesystem paths to extracted Helm charts.

        Args:
            manifest: The bundle manifest.

        Returns:
            Dict mapping module_name to local chart .tgz path.

        Raises:
            RuntimeError: If the bundle has not been extracted yet.
        """
        if self._extract_dir is None:
            raise RuntimeError("Call extract() before get_chart_paths()")

        charts_dir = self._extract_dir / "charts"
        result: dict[str, Path] = {}

        for bundled_chart in manifest.modules:
            chart_path = charts_dir / bundled_chart.filename
            if chart_path.exists():
                result[bundled_chart.module_name] = chart_path
            else:
                logger.warning("chart_missing_in_bundle", module=bundled_chart.module_name, filename=bundled_chart.filename)

        return result

    def _load_image(self, tar_path: Path, repository: str, tag: str) -> None:
        """Load a single container image tarball into Docker or push to local registry.

        Args:
            tar_path: Path to the image .tar file.
            repository: Image repository name.
            tag: Image tag.
        """
        load_result = subprocess.run(
            ["docker", "load", "-i", str(tar_path)],
            capture_output=True,
            text=True,
            check=False,
        )

        if load_result.returncode != 0:
            logger.error("docker_load_failed", image=f"{repository}:{tag}", stderr=load_result.stderr)
            return

        if self._local_registry:
            local_ref = f"{self._local_registry}/{repository}:{tag}"
            subprocess.run(["docker", "tag", f"{repository}:{tag}", local_ref], check=False)
            push_result = subprocess.run(["docker", "push", local_ref], capture_output=True, text=True, check=False)
            if push_result.returncode != 0:
                logger.warning("registry_push_failed", ref=local_ref, stderr=push_result.stderr)

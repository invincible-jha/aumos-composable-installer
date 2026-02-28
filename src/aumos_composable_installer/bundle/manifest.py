"""Bundle manifest schema and serialization for air-gapped bundles.

A bundle manifest records which modules are included, their chart digests,
and the container images required for offline installation.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


class BundledImage(BaseModel):
    """A container image included in an air-gapped bundle.

    Attributes:
        repository: Container image repository.
        tag: Image tag.
        digest: Content-addressable image digest (sha256:...).
        tar_filename: Filename of the saved image tarball inside the bundle.
    """

    repository: str
    tag: str
    digest: str
    tar_filename: str


class BundledChart(BaseModel):
    """A Helm chart included in an air-gapped bundle.

    Attributes:
        module_name: AumOS module name (e.g., aumos-platform-core).
        chart_version: Chart semver version string.
        filename: Chart .tgz filename inside the bundle.
        images: Container images required by this chart.
    """

    module_name: str
    chart_version: str
    filename: str
    images: list[BundledImage] = Field(default_factory=list)


class BundleManifest(BaseModel):
    """Root bundle manifest describing an air-gapped AumOS installation bundle.

    Attributes:
        bundle_version: Schema version of this manifest format.
        aumos_version: AumOS platform version string.
        created_at: ISO timestamp of when this bundle was created.
        modules: Helm charts included in the bundle.
        total_size_bytes: Approximate total bundle size in bytes.
    """

    bundle_version: str = "1.0"
    aumos_version: str
    created_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    modules: list[BundledChart] = Field(default_factory=list)
    total_size_bytes: int = 0

    def save(self, path: Path) -> None:
        """Write this manifest as YAML to the given path.

        Args:
            path: Destination file path for the manifest YAML.
        """
        path.write_text(yaml.dump(self.model_dump(), default_flow_style=False), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "BundleManifest":
        """Load a bundle manifest from a YAML file.

        Args:
            path: Path to the manifest YAML file.

        Returns:
            Parsed BundleManifest instance.
        """
        raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
        return cls.model_validate(raw)

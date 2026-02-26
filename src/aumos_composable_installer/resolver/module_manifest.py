"""Module manifest schema and loader.

Defines the Pydantic schema for AumOS module manifests and provides
a loader that reads YAML files from the manifest directory.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator

from aumos_common.observability import get_logger

logger = get_logger(__name__)


class ModuleTier(str, Enum):
    """License tier controlling module availability."""

    A = "A"  # Always on — included with base platform, no license key required
    B = "B"  # Commercial key — requires valid JWT entitlement
    C = "C"  # Proprietary — requires enterprise agreement


class HelmConfig(BaseModel):
    """Helm chart reference for a module."""

    chart: str = Field(..., description="Helm chart name (e.g., aumos-data-factory)")
    repository: str = Field(
        default="oci://registry.aumos.ai/charts",
        description="OCI registry or Helm repo URL",
    )
    version: str = Field(default="", description="Pinned chart version (empty = latest)")
    values_override: dict[str, Any] = Field(
        default_factory=dict,
        description="Values to merge into the chart's default values",
    )


class HealthCheckConfig(BaseModel):
    """Health check endpoint configuration for a module."""

    url: str = Field(..., description="Health check endpoint path (e.g., /api/v1/health)")
    interval_seconds: int = Field(default=30, ge=5, description="Polling interval in seconds")
    timeout_seconds: int = Field(default=10, ge=1, description="Per-check timeout in seconds")
    expected_status: int = Field(default=200, description="Expected HTTP status code")


class ResourceRequirements(BaseModel):
    """Minimum resource requirements for a module."""

    cpu_min: str = Field(default="100m", description="Minimum CPU request (Kubernetes format)")
    memory_min: str = Field(default="256Mi", description="Minimum memory request (Kubernetes format)")
    gpu_required: bool = Field(default=False, description="Whether a GPU is required")
    gpu_type: str = Field(default="", description="GPU type if required (e.g., nvidia-a100)")


class ModuleManifest(BaseModel):
    """Schema for an AumOS module manifest YAML file.

    Each module in the AumOS ecosystem has a manifest that declares its
    identity, dependencies, deployment configuration, and resource requirements.
    """

    name: str = Field(..., description="Unique module identifier (kebab-case)")
    display_name: str = Field(..., description="Human-readable module name")
    version: str = Field(..., description="Module version (semver)")
    tier: ModuleTier = Field(..., description="License tier (A/B/C)")
    description: str = Field(default="", description="Module description")

    dependencies: list[str] = Field(
        default_factory=list,
        description="List of module names this module depends on",
    )
    sub_modules: list[str] = Field(
        default_factory=list,
        alias="modules",
        description="Optional list of sub-module names included in this module",
    )

    helm: HelmConfig = Field(..., description="Helm chart deployment configuration")
    health_check: HealthCheckConfig = Field(..., description="Health check configuration")
    resources: ResourceRequirements = Field(
        default_factory=ResourceRequirements,
        description="Minimum resource requirements",
    )

    # Compatibility
    min_platform_version: str = Field(default="0.1.0", description="Minimum platform-core version required")
    max_platform_version: str = Field(default="", description="Maximum compatible platform-core version (empty=any)")

    model_config = {"populate_by_name": True}

    @field_validator("name")
    @classmethod
    def name_must_be_kebab_case(cls, value: str) -> str:
        """Ensure module name uses kebab-case format.

        Args:
            value: The module name to validate.

        Returns:
            The validated module name.

        Raises:
            ValueError: If the name contains uppercase letters or underscores.
        """
        if not value.replace("-", "").replace(".", "").isalnum():
            raise ValueError(f"Module name '{value}' must be kebab-case alphanumeric")
        if any(c.isupper() for c in value):
            raise ValueError(f"Module name '{value}' must be lowercase")
        return value

    @field_validator("version")
    @classmethod
    def version_must_be_semver(cls, value: str) -> str:
        """Ensure version follows semver (major.minor.patch).

        Args:
            value: The version string to validate.

        Returns:
            The validated version string.

        Raises:
            ValueError: If the version is not valid semver.
        """
        parts = value.split(".")
        if len(parts) != 3 or not all(part.isdigit() for part in parts):
            raise ValueError(f"Version '{value}' must be semver (e.g., 1.0.0)")
        return value


class ManifestLoader:
    """Loads and caches module manifests from the manifest directory.

    Scans a directory for YAML files matching the module manifest schema
    and provides fast lookup by module name.
    """

    def __init__(self, manifest_dir: Path) -> None:
        """Initialize the manifest loader.

        Args:
            manifest_dir: Path to the directory containing manifest YAML files.
        """
        self._manifest_dir = manifest_dir
        self._cache: dict[str, ModuleManifest] = {}
        self._loaded = False

    def load_all(self) -> dict[str, ModuleManifest]:
        """Load all manifests from the manifest directory.

        Returns:
            Dictionary mapping module name to ModuleManifest.

        Raises:
            FileNotFoundError: If the manifest directory does not exist.
            ValueError: If any manifest fails schema validation.
        """
        if self._loaded:
            return self._cache

        if not self._manifest_dir.exists():
            raise FileNotFoundError(f"Manifest directory not found: {self._manifest_dir}")

        manifest_files = list(self._manifest_dir.glob("*.yaml")) + list(self._manifest_dir.glob("*.yml"))
        if not manifest_files:
            logger.warning("No manifest files found", manifest_dir=str(self._manifest_dir))
            return {}

        for manifest_path in manifest_files:
            try:
                manifest = self._load_file(manifest_path)
                self._cache[manifest.name] = manifest
                logger.debug("Loaded manifest", module=manifest.name, version=manifest.version)
            except Exception as exc:
                logger.error("Failed to load manifest", path=str(manifest_path), error=str(exc))
                raise ValueError(f"Invalid manifest at {manifest_path}: {exc}") from exc

        self._loaded = True
        logger.info("Loaded module manifests", count=len(self._cache))
        return self._cache

    def get(self, module_name: str) -> ModuleManifest:
        """Get a specific module manifest by name.

        Args:
            module_name: The module name (e.g., 'data-factory').

        Returns:
            The ModuleManifest for the requested module.

        Raises:
            KeyError: If the module is not found.
        """
        manifests = self.load_all()
        if module_name not in manifests:
            available = sorted(manifests.keys())
            raise KeyError(f"Module '{module_name}' not found. Available: {available}")
        return manifests[module_name]

    def list_names(self) -> list[str]:
        """Return sorted list of all available module names.

        Returns:
            Sorted list of module name strings.
        """
        return sorted(self.load_all().keys())

    def invalidate_cache(self) -> None:
        """Clear the manifest cache, forcing reload on next access."""
        self._cache = {}
        self._loaded = False

    def _load_file(self, path: Path) -> ModuleManifest:
        """Parse a single manifest YAML file.

        Args:
            path: Path to the YAML file.

        Returns:
            Parsed and validated ModuleManifest.
        """
        with path.open("r", encoding="utf-8") as file_handle:
            raw = yaml.safe_load(file_handle)
        return ModuleManifest.model_validate(raw)

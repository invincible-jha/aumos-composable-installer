"""Composable Installer settings extending AumOS base configuration.

All standard AumOS configuration is inherited from AumOSSettings.
Installer-specific settings use the AUMOS_INSTALLER_ env prefix.
"""

from pathlib import Path

from pydantic import Field
from pydantic_settings import SettingsConfigDict

from aumos_common.config import AumOSSettings


class Settings(AumOSSettings):
    """Settings for aumos-composable-installer.

    Inherits all standard AumOS settings and adds installer-specific
    configuration for license management, manifest discovery, and
    deployment backend selection.

    Environment variable prefix: AUMOS_INSTALLER_
    """

    service_name: str = "aumos-composable-installer"

    # Manifest discovery
    manifest_dir: Path = Field(
        default=Path(__file__).parent.parent.parent.parent / "module-manifests",
        description="Directory containing module manifest YAML files",
    )

    # License
    license_key_path: Path = Field(
        default=Path.home() / ".aumos" / "license.key",
        description="Path to stored license JWT key",
    )
    license_public_key_path: Path = Field(
        default=Path(__file__).parent / "license" / "aumos-public.pem",
        description="Path to AumOS license signing public key (PEM)",
    )

    # State tracking
    state_file_path: Path = Field(
        default=Path.home() / ".aumos" / "installer-state.yaml",
        description="Path to local installer state file tracking activated modules",
    )

    # Deployment backend
    default_deploy_mode: str = Field(
        default="helm",
        description="Default deployment backend: helm | argocd | docker-compose",
    )

    # Helm
    helm_namespace: str = Field(default="aumos", description="Kubernetes namespace for AumOS components")
    helm_release_name: str = Field(default="aumos", description="Helm release name")
    helm_chart_repository: str = Field(
        default="oci://registry.aumos.ai/charts",
        description="OCI registry URL for AumOS Helm charts",
    )
    helm_timeout_seconds: int = Field(default=600, description="Helm operation timeout in seconds")

    # ArgoCD
    argocd_server: str = Field(default="", description="ArgoCD server address (host:port)")
    argocd_namespace: str = Field(default="argocd", description="ArgoCD namespace")
    argocd_app_project: str = Field(default="aumos", description="ArgoCD project name")

    # Health check
    health_check_timeout_seconds: int = Field(default=300, description="Post-install health check timeout")
    health_check_interval_seconds: int = Field(default=10, description="Health check polling interval")

    # Batch installation
    max_parallel_deployments: int = Field(
        default=3,
        description="Maximum concurrent service deployments within a batch group",
    )

    # Rollback
    rollback_enabled: bool = Field(
        default=True,
        description="Whether to automatically trigger rollback on post-upgrade health failure",
    )

    # Gap #9: OCI artifact registry support
    oci_registry_enabled: bool = Field(
        default=False,
        description="Whether to pull charts from an OCI artifact registry (GHCR, ECR, GCR, ACR)",
    )
    oci_registry_url: str = Field(
        default="ghcr.io/aumos/helm-charts",
        description="OCI registry URL for AumOS Helm charts",
    )
    oci_registry_username: str = Field(
        default="",
        description="OCI registry username (leave empty for anonymous or IRSA/Workload Identity)",
    )
    oci_registry_password: str = Field(
        default="",
        description="OCI registry password or access token",
    )

    # Gap #12: IaC binary selection (terraform or tofu)
    iac_binary: str = Field(
        default="terraform",
        description="IaC CLI binary to use for infrastructure generation (terraform or tofu)",
    )

    # Gap #13: Telemetry opt-in
    telemetry_enabled: bool = Field(
        default=False,
        description="Whether to send anonymous installation telemetry (opt-in only)",
    )
    telemetry_endpoint: str = Field(
        default="https://telemetry.aumos.ai/v1/events",
        description="Telemetry HTTPS endpoint URL",
    )

    model_config = SettingsConfigDict(env_prefix="AUMOS_INSTALLER_", env_nested_delimiter="__")

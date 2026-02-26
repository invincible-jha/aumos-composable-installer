"""Helm chart deployment orchestration for AumOS modules.

Manages the AumOS umbrella Helm chart, enabling and disabling module
sub-charts via values overrides. Delegates to the `helm` CLI.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console

from aumos_common.observability import get_logger

from aumos_composable_installer.resolver.dependency_graph import ResolutionResult
from aumos_composable_installer.resolver.module_manifest import ManifestLoader

logger = get_logger(__name__)
console = Console()


@dataclass
class HelmDeploymentResult:
    """Result of a Helm deployment operation.

    Attributes:
        success: Whether the operation succeeded.
        release_name: Helm release name.
        namespace: Kubernetes namespace.
        modules_deployed: Modules included in this deployment.
        stdout: Helm command stdout.
        stderr: Helm command stderr.
        return_code: Helm process return code.
    """

    success: bool
    release_name: str
    namespace: str
    modules_deployed: list[str]
    stdout: str = ""
    stderr: str = ""
    return_code: int = 0


class HelmDeployer:
    """Deploys AumOS modules using the Helm umbrella chart.

    The umbrella chart uses conditional sub-chart inclusion controlled
    by values like `modules.data-factory.enabled: true`. This deployer
    constructs the appropriate values overrides and applies them.
    """

    def __init__(
        self,
        loader: ManifestLoader,
        release_name: str = "aumos",
        namespace: str = "aumos",
        chart_repository: str = "oci://registry.aumos.ai/charts",
        chart_name: str = "aumos-platform",
        chart_version: str = "",
        timeout_seconds: int = 600,
        umbrella_chart_dir: Path | None = None,
    ) -> None:
        """Initialize the Helm deployer.

        Args:
            loader: ManifestLoader for module metadata.
            release_name: Helm release name.
            namespace: Kubernetes namespace.
            chart_repository: OCI registry or Helm repo for charts.
            chart_name: Name of the umbrella chart.
            chart_version: Pinned chart version (empty = latest).
            timeout_seconds: Helm operation timeout.
            umbrella_chart_dir: Path to local umbrella chart (overrides registry).
        """
        self._loader = loader
        self._release_name = release_name
        self._namespace = namespace
        self._chart_repository = chart_repository
        self._chart_name = chart_name
        self._chart_version = chart_version
        self._timeout_seconds = timeout_seconds
        self._umbrella_chart_dir = umbrella_chart_dir

    def install(
        self,
        resolution: ResolutionResult,
        dry_run: bool = False,
        extra_values: dict[str, str] | None = None,
    ) -> HelmDeploymentResult:
        """Run `helm upgrade --install` with module activation values.

        Args:
            resolution: Resolved dependency graph result.
            dry_run: If True, pass --dry-run to Helm (no actual deployment).
            extra_values: Additional key=value pairs to pass to Helm.

        Returns:
            HelmDeploymentResult indicating success or failure.
        """
        values = self._build_values(resolution.all_modules)
        if extra_values:
            values.update(extra_values)

        chart_ref = self._chart_ref()
        cmd = self._build_helm_command(
            subcommand="upgrade",
            chart_ref=chart_ref,
            values=values,
            extra_flags=["--install", "--create-namespace", "--wait"],
            dry_run=dry_run,
        )

        logger.info(
            "Running helm upgrade --install",
            release=self._release_name,
            namespace=self._namespace,
            modules=resolution.install_order,
            dry_run=dry_run,
        )

        return self._run_helm(cmd, resolution.install_order)

    def activate_module(
        self,
        module_name: str,
        dry_run: bool = False,
    ) -> HelmDeploymentResult:
        """Enable a single module by updating the umbrella chart values.

        Args:
            module_name: Module to activate.
            dry_run: If True, use --dry-run.

        Returns:
            HelmDeploymentResult.
        """
        values = {f"modules.{module_name}.enabled": "true"}
        cmd = self._build_helm_command(
            subcommand="upgrade",
            chart_ref=self._chart_ref(),
            values=values,
            extra_flags=["--reuse-values", "--wait"],
            dry_run=dry_run,
        )

        logger.info("Activating module via Helm", module=module_name, dry_run=dry_run)
        return self._run_helm(cmd, [module_name])

    def deactivate_module(
        self,
        module_name: str,
        dry_run: bool = False,
    ) -> HelmDeploymentResult:
        """Disable a single module by updating the umbrella chart values.

        Args:
            module_name: Module to deactivate.
            dry_run: If True, use --dry-run.

        Returns:
            HelmDeploymentResult.
        """
        values = {f"modules.{module_name}.enabled": "false"}
        cmd = self._build_helm_command(
            subcommand="upgrade",
            chart_ref=self._chart_ref(),
            values=values,
            extra_flags=["--reuse-values", "--wait"],
            dry_run=dry_run,
        )

        logger.info("Deactivating module via Helm", module=module_name, dry_run=dry_run)
        return self._run_helm(cmd, [module_name])

    def get_release_status(self) -> dict[str, str]:
        """Get the current status of the Helm release.

        Returns:
            Dictionary with status information.
        """
        cmd = [
            "helm", "status", self._release_name,
            "--namespace", self._namespace,
            "--output", "json",
        ]
        result = self._run_helm(cmd, [])
        if result.success:
            try:
                return dict(json.loads(result.stdout))
            except (json.JSONDecodeError, ValueError):
                return {"raw": result.stdout}
        return {"error": result.stderr}

    def _build_values(self, modules: set[str]) -> dict[str, str]:
        """Build Helm --set values for all modules in the activation set.

        Args:
            modules: Set of module names to activate.

        Returns:
            Dictionary of key=value pairs for helm --set flags.
        """
        all_manifests = {}
        try:
            all_manifests = self._loader.load_all()
        except Exception:
            pass

        values: dict[str, str] = {}
        for module_name in all_manifests:
            is_active = module_name in modules
            values[f"modules.{module_name}.enabled"] = str(is_active).lower()

        return values

    def _chart_ref(self) -> str:
        """Return the chart reference for Helm commands.

        Returns:
            Local path if umbrella_chart_dir is set, else OCI reference.
        """
        if self._umbrella_chart_dir is not None:
            return str(self._umbrella_chart_dir)
        ref = f"{self._chart_repository}/{self._chart_name}"
        if self._chart_version:
            ref += f"@{self._chart_version}"
        return ref

    def _build_helm_command(
        self,
        subcommand: str,
        chart_ref: str,
        values: dict[str, str],
        extra_flags: list[str],
        dry_run: bool,
    ) -> list[str]:
        """Construct the helm CLI command list.

        Args:
            subcommand: Helm subcommand (e.g., 'upgrade').
            chart_ref: Chart reference string.
            values: --set key=value pairs.
            extra_flags: Additional flags.
            dry_run: Whether to add --dry-run flag.

        Returns:
            Command as a list of strings.
        """
        cmd = [
            "helm", subcommand, self._release_name, chart_ref,
            "--namespace", self._namespace,
            "--timeout", f"{self._timeout_seconds}s",
        ]

        for key, value in sorted(values.items()):
            cmd.extend(["--set", f"{key}={value}"])

        cmd.extend(extra_flags)

        if dry_run:
            cmd.append("--dry-run")

        return cmd

    def _run_helm(self, cmd: list[str], modules: list[str]) -> HelmDeploymentResult:
        """Execute a helm command and return the result.

        Args:
            cmd: Command as list of strings.
            modules: Modules involved (for result metadata).

        Returns:
            HelmDeploymentResult.
        """
        logger.debug("Executing helm command", cmd=" ".join(cmd))
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self._timeout_seconds + 30,
            )
            success = proc.returncode == 0
            if not success:
                logger.error("Helm command failed", returncode=proc.returncode, stderr=proc.stderr[:500])
            return HelmDeploymentResult(
                success=success,
                release_name=self._release_name,
                namespace=self._namespace,
                modules_deployed=modules,
                stdout=proc.stdout,
                stderr=proc.stderr,
                return_code=proc.returncode,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            logger.error("Helm command failed to execute", error=str(exc))
            return HelmDeploymentResult(
                success=False,
                release_name=self._release_name,
                namespace=self._namespace,
                modules_deployed=modules,
                stderr=str(exc),
                return_code=-1,
            )

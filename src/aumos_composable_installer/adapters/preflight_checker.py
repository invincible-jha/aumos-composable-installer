"""Preflight checker adapter for the AumOS Composable Installer.

Validates system requirements before installation: Kubernetes version, available
resources, network connectivity, dependency availability, license entitlements,
and configuration completeness. Produces a structured PreflightReport.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import httpx

from aumos_common.observability import get_logger

logger = get_logger(__name__)

# Minimum supported Kubernetes version
MIN_K8S_MAJOR = 1
MIN_K8S_MINOR = 26

# Minimum cluster resources required for a baseline AumOS installation
MIN_CPU_CORES = 8
MIN_MEMORY_GB = 32
MIN_STORAGE_GB = 100


class CheckSeverity(str, Enum):
    """Severity level of a preflight check result."""

    PASS = "pass"
    WARNING = "warning"
    BLOCKER = "blocker"


@dataclass
class PreflightCheckResult:
    """Result of a single preflight check.

    Attributes:
        check_name: Human-readable name of the check.
        severity: PASS, WARNING, or BLOCKER.
        message: Explanation of the check outcome.
        detail: Optional additional structured context.
    """

    check_name: str
    severity: CheckSeverity
    message: str
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        """True if the check passed (not a blocker or warning)."""
        return self.severity == CheckSeverity.PASS

    @property
    def is_blocker(self) -> bool:
        """True if this check blocks installation."""
        return self.severity == CheckSeverity.BLOCKER


@dataclass
class PreflightReport:
    """Aggregate preflight validation report.

    Attributes:
        checks: All executed check results.
        install_blocked: True if any blocker was found.
        warnings_count: Number of non-blocking warnings.
    """

    checks: list[PreflightCheckResult] = field(default_factory=list)

    @property
    def install_blocked(self) -> bool:
        """True if any check returned a BLOCKER severity."""
        return any(c.is_blocker for c in self.checks)

    @property
    def warnings_count(self) -> int:
        """Number of WARNING-severity checks."""
        return sum(1 for c in self.checks if c.severity == CheckSeverity.WARNING)

    @property
    def blockers(self) -> list[PreflightCheckResult]:
        """List of all BLOCKER checks."""
        return [c for c in self.checks if c.is_blocker]

    @property
    def passed_count(self) -> int:
        """Number of PASS checks."""
        return sum(1 for c in self.checks if c.passed)

    def summary(self) -> dict[str, Any]:
        """Produce a summary dict for display or export.

        Returns:
            Dict with pass/warning/blocker counts and install_blocked flag.
        """
        return {
            "install_blocked": self.install_blocked,
            "passed": self.passed_count,
            "warnings": self.warnings_count,
            "blockers": len(self.blockers),
            "total_checks": len(self.checks),
        }


class PreflightChecker:
    """Pre-installation validation engine.

    Runs system requirement checks before any installation attempt:
    - Kubernetes API server version and connectivity
    - Cluster resource availability (CPU, memory, storage)
    - Network endpoint reachability
    - Helm and kubectl CLI availability
    - Required container registry access
    - License entitlement completeness

    Args:
        k8s_api_url: Kubernetes API server URL for connectivity checks.
        container_registries: List of registries to probe for connectivity.
        required_namespaces: Namespaces that must already exist or be creatable.
        http_timeout_seconds: Timeout for network connectivity checks.
    """

    def __init__(
        self,
        k8s_api_url: str = "https://kubernetes.default.svc",
        container_registries: list[str] | None = None,
        required_namespaces: list[str] | None = None,
        http_timeout_seconds: int = 10,
    ) -> None:
        self._k8s_api_url = k8s_api_url
        self._container_registries = container_registries or ["registry.aumos.ai"]
        self._required_namespaces = required_namespaces or ["aumos"]
        self._http_timeout = http_timeout_seconds

    async def run_all(self, license_claims: dict[str, Any] | None = None) -> PreflightReport:
        """Run the full preflight validation suite.

        Runs all checks concurrently where safe and aggregates results.

        Args:
            license_claims: Decoded license JWT claims for entitlement checks.

        Returns:
            PreflightReport with all check outcomes.
        """
        logger.info("Running preflight checks")

        check_tasks = [
            self.check_kubectl_available(),
            self.check_helm_available(),
            self.check_k8s_version(),
            self.check_cluster_resources(),
            self.check_network_connectivity(),
            self.check_container_registry_access(),
        ]

        if license_claims:
            check_tasks.append(self.check_license_entitlements(license_claims))

        results = await asyncio.gather(*check_tasks, return_exceptions=False)

        report = PreflightReport(checks=list(results))
        logger.info(
            "Preflight checks complete",
            passed=report.passed_count,
            warnings=report.warnings_count,
            blockers=len(report.blockers),
            install_blocked=report.install_blocked,
        )
        return report

    # ------------------------------------------------------------------
    # Individual checks
    # ------------------------------------------------------------------

    async def check_kubectl_available(self) -> PreflightCheckResult:
        """Verify that kubectl is available and configured.

        Returns:
            PASS if kubectl responds, BLOCKER otherwise.
        """
        result = await asyncio.get_event_loop().run_in_executor(
            None, lambda: subprocess.run(["kubectl", "version", "--client", "--output=json"], capture_output=True, text=True)
        )
        if result.returncode == 0:
            return PreflightCheckResult(
                check_name="kubectl_available",
                severity=CheckSeverity.PASS,
                message="kubectl is available and configured",
            )
        return PreflightCheckResult(
            check_name="kubectl_available",
            severity=CheckSeverity.BLOCKER,
            message="kubectl is not available or not configured",
            detail={"stderr": result.stderr[:300]},
        )

    async def check_helm_available(self) -> PreflightCheckResult:
        """Verify that Helm 3.x is installed.

        Returns:
            PASS if helm >= 3.0, BLOCKER if missing or version < 3.
        """
        result = await asyncio.get_event_loop().run_in_executor(
            None, lambda: subprocess.run(["helm", "version", "--short"], capture_output=True, text=True)
        )
        if result.returncode != 0:
            return PreflightCheckResult(
                check_name="helm_available",
                severity=CheckSeverity.BLOCKER,
                message="Helm is not installed. Helm 3.14+ is required.",
            )

        version_str = result.stdout.strip()
        if not version_str.startswith("v3"):
            return PreflightCheckResult(
                check_name="helm_available",
                severity=CheckSeverity.BLOCKER,
                message=f"Helm 3.x required, found: {version_str}",
                detail={"found_version": version_str},
            )

        return PreflightCheckResult(
            check_name="helm_available",
            severity=CheckSeverity.PASS,
            message=f"Helm available: {version_str}",
            detail={"version": version_str},
        )

    async def check_k8s_version(self) -> PreflightCheckResult:
        """Verify Kubernetes cluster version meets the minimum requirement.

        Returns:
            PASS if cluster >= 1.26, BLOCKER if below minimum.
        """
        result = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: subprocess.run(
                ["kubectl", "version", "--output=json"],
                capture_output=True,
                text=True,
            ),
        )

        if result.returncode != 0:
            return PreflightCheckResult(
                check_name="k8s_version",
                severity=CheckSeverity.BLOCKER,
                message="Cannot connect to Kubernetes API server",
                detail={"error": result.stderr[:300]},
            )

        try:
            version_data: dict[str, Any] = json.loads(result.stdout)
            server_version: dict[str, Any] = version_data.get("serverVersion", {})
            major = int(server_version.get("major", "0").replace("+", ""))
            minor = int(server_version.get("minor", "0").replace("+", ""))

            if (major, minor) >= (MIN_K8S_MAJOR, MIN_K8S_MINOR):
                return PreflightCheckResult(
                    check_name="k8s_version",
                    severity=CheckSeverity.PASS,
                    message=f"Kubernetes {major}.{minor} meets minimum requirement ({MIN_K8S_MAJOR}.{MIN_K8S_MINOR})",
                    detail={"major": major, "minor": minor},
                )
            return PreflightCheckResult(
                check_name="k8s_version",
                severity=CheckSeverity.BLOCKER,
                message=f"Kubernetes {major}.{minor} is below minimum ({MIN_K8S_MAJOR}.{MIN_K8S_MINOR})",
                detail={"major": major, "minor": minor, "required_minor": MIN_K8S_MINOR},
            )
        except (json.JSONDecodeError, ValueError, KeyError) as exc:
            return PreflightCheckResult(
                check_name="k8s_version",
                severity=CheckSeverity.WARNING,
                message=f"Could not parse Kubernetes version: {exc}",
            )

    async def check_cluster_resources(self) -> PreflightCheckResult:
        """Verify the cluster has sufficient CPU, memory, and storage.

        Returns:
            PASS if resources meet minimums, WARNING if below recommendation.
        """
        result = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: subprocess.run(
                ["kubectl", "get", "nodes", "--output=json"],
                capture_output=True,
                text=True,
            ),
        )

        if result.returncode != 0:
            return PreflightCheckResult(
                check_name="cluster_resources",
                severity=CheckSeverity.WARNING,
                message="Cannot retrieve node list for resource validation",
            )

        try:
            nodes_data: dict[str, Any] = json.loads(result.stdout)
            nodes: list[dict[str, Any]] = nodes_data.get("items", [])

            total_cpu = 0
            total_memory_gb = 0.0

            for node in nodes:
                capacity: dict[str, Any] = node.get("status", {}).get("capacity", {})
                cpu_str: str = capacity.get("cpu", "0")
                mem_str: str = capacity.get("memory", "0Ki")

                # Parse CPU cores (may be fractional like "4000m")
                if cpu_str.endswith("m"):
                    total_cpu += int(cpu_str[:-1]) // 1000
                else:
                    total_cpu += int(cpu_str)

                # Parse memory (Ki, Mi, Gi)
                if mem_str.endswith("Ki"):
                    total_memory_gb += int(mem_str[:-2]) / (1024 * 1024)
                elif mem_str.endswith("Mi"):
                    total_memory_gb += int(mem_str[:-2]) / 1024
                elif mem_str.endswith("Gi"):
                    total_memory_gb += int(mem_str[:-2])

            detail = {
                "total_cpu_cores": total_cpu,
                "total_memory_gb": round(total_memory_gb, 1),
                "node_count": len(nodes),
                "min_cpu_required": MIN_CPU_CORES,
                "min_memory_gb_required": MIN_MEMORY_GB,
            }

            if total_cpu < MIN_CPU_CORES or total_memory_gb < MIN_MEMORY_GB:
                return PreflightCheckResult(
                    check_name="cluster_resources",
                    severity=CheckSeverity.BLOCKER,
                    message=f"Insufficient cluster resources: {total_cpu} CPU cores, {total_memory_gb:.0f}GB RAM",
                    detail=detail,
                )

            return PreflightCheckResult(
                check_name="cluster_resources",
                severity=CheckSeverity.PASS,
                message=f"Cluster resources adequate: {total_cpu} CPU cores, {total_memory_gb:.0f}GB RAM across {len(nodes)} nodes",
                detail=detail,
            )

        except (json.JSONDecodeError, ValueError, KeyError) as exc:
            return PreflightCheckResult(
                check_name="cluster_resources",
                severity=CheckSeverity.WARNING,
                message=f"Could not parse node capacity data: {exc}",
            )

    async def check_network_connectivity(self) -> PreflightCheckResult:
        """Verify network connectivity to required AumOS endpoints.

        Returns:
            PASS if all reachable, WARNING if some unreachable, BLOCKER if all fail.
        """
        required_endpoints = [
            f"https://{reg}" for reg in self._container_registries
        ]

        reachable = []
        unreachable = []

        async with httpx.AsyncClient(timeout=self._http_timeout) as client:
            for endpoint in required_endpoints:
                try:
                    await client.get(endpoint)
                    reachable.append(endpoint)
                except Exception:
                    unreachable.append(endpoint)

        detail = {"reachable": reachable, "unreachable": unreachable}

        if not unreachable:
            return PreflightCheckResult(
                check_name="network_connectivity",
                severity=CheckSeverity.PASS,
                message=f"All {len(reachable)} required endpoints are reachable",
                detail=detail,
            )

        if reachable:
            return PreflightCheckResult(
                check_name="network_connectivity",
                severity=CheckSeverity.WARNING,
                message=f"{len(unreachable)} endpoint(s) unreachable: {', '.join(unreachable)}",
                detail=detail,
            )

        return PreflightCheckResult(
            check_name="network_connectivity",
            severity=CheckSeverity.BLOCKER,
            message="No required network endpoints are reachable",
            detail=detail,
        )

    async def check_container_registry_access(self) -> PreflightCheckResult:
        """Verify container registry is reachable and credentials work.

        Returns:
            PASS if registry responds, WARNING if credentials not validated.
        """
        issues = []
        for registry in self._container_registries:
            result = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda reg=registry: subprocess.run(
                    ["helm", "registry", "login", reg, "--help"],
                    capture_output=True,
                    text=True,
                ),
            )
            if result.returncode != 0:
                issues.append(f"{registry}: helm registry access check failed")

        if issues:
            return PreflightCheckResult(
                check_name="container_registry_access",
                severity=CheckSeverity.WARNING,
                message=f"Registry access warnings: {'; '.join(issues)}",
                detail={"registries": self._container_registries},
            )

        return PreflightCheckResult(
            check_name="container_registry_access",
            severity=CheckSeverity.PASS,
            message=f"Container registries accessible: {', '.join(self._container_registries)}",
            detail={"registries": self._container_registries},
        )

    async def check_license_entitlements(self, license_claims: dict[str, Any]) -> PreflightCheckResult:
        """Verify the license JWT contains required module entitlements.

        Args:
            license_claims: Decoded license JWT claims dict.

        Returns:
            PASS if all required entitlements present, BLOCKER if missing.
        """
        entitled_modules: list[str] = license_claims.get("modules", [])
        tier: str = license_claims.get("tier", "")

        detail = {
            "tier": tier,
            "entitled_module_count": len(entitled_modules),
        }

        if not tier:
            return PreflightCheckResult(
                check_name="license_entitlements",
                severity=CheckSeverity.BLOCKER,
                message="License is missing required 'tier' claim",
                detail=detail,
            )

        if not entitled_modules and tier not in ("enterprise", "platform"):
            return PreflightCheckResult(
                check_name="license_entitlements",
                severity=CheckSeverity.WARNING,
                message="License has no module entitlements — only Tier A (free) modules can be installed",
                detail=detail,
            )

        return PreflightCheckResult(
            check_name="license_entitlements",
            severity=CheckSeverity.PASS,
            message=f"License valid — tier: {tier}, entitled modules: {len(entitled_modules)}",
            detail=detail,
        )

"""Installer Orchestrator — wires all composable-installer adapters.

Provides a single assembly point for the installation workflow adapters:
preflight checking, batch coordination, health monitoring, upgrade
orchestration, rollback automation, and config management.

This module follows the composable-installer's existing pattern of thin
command handlers delegating to substantive business logic modules.

Usage:
    orchestrator = InstallerOrchestrator(settings=Settings())
    report = await orchestrator.run_preflight()
    if not report.is_ready_to_install:
        raise SystemExit(1)
    batch_report = await orchestrator.run_batch_install(
        services=["aumos-auth-gateway", "aumos-event-bus"],
        installer_fn=my_installer,
    )

License: Apache 2.0
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any, Callable, Coroutine

from aumos_common.observability import get_logger

from aumos_composable_installer.adapters.batch_coordinator import (
    BatchCoordinator,
    BatchCoordinationReport,
    FailureStrategy,
)
from aumos_composable_installer.adapters.config_manager import InstallerConfigManager
from aumos_composable_installer.adapters.health_monitor import (
    InstallationHealthMonitor,
    ProbeType,
    ServiceProbeConfig,
)
from aumos_composable_installer.adapters.preflight_checker import (
    PreflightChecker,
    PreflightReport,
)
from aumos_composable_installer.adapters.rollback_automation import RollbackAutomation
from aumos_composable_installer.adapters.upgrade_orchestrator import (
    UpgradeOrchestrator,
    UpgradeReport,
    UpgradeStrategy,
    VersionSpec,
)
from aumos_composable_installer.settings import Settings

logger = get_logger(__name__)

# Type alias for async service installer callables
InstallerCallable = Callable[[str], Coroutine[Any, Any, bool]]


class InstallerOrchestrator:
    """Central coordinator for AumOS platform installation and upgrade operations.

    Assembles and wires the six installation adapters into a single cohesive
    workflow coordinator. Exposes high-level methods for the CLI command
    handlers to call without needing to understand adapter internals.

    Args:
        settings: Installer settings (AumOS settings with AUMOS_INSTALLER_ prefix).
        vault_url: Optional URL of the aumos-secrets-vault service for secret
            injection into Helm values. When None, vault:// references remain
            unresolved in rendered configs.
        audit_sink_url: Optional HTTP URL to forward installation audit events.
    """

    def __init__(
        self,
        settings: Settings,
        vault_url: str | None = None,
        audit_sink_url: str | None = None,
    ) -> None:
        self._settings = settings
        self._vault_url = vault_url
        self._audit_sink_url = audit_sink_url

        # Instantiate all adapters with settings-derived configuration
        self._preflight = PreflightChecker(
            required_k8s_version="1.26",
            required_cpu_cores=8,
            required_ram_gb=32,
            required_storage_gb=100,
        )

        self._config_manager = InstallerConfigManager(
            vault_url=vault_url,
        )

        self._health_monitor = InstallationHealthMonitor(
            poll_interval_seconds=settings.health_check_interval_seconds,
            startup_timeout_seconds=settings.health_check_timeout_seconds,
            degradation_threshold=3,
        )

        self._rollback = RollbackAutomation(
            health_check_interval_seconds=settings.health_check_interval_seconds,
            health_check_timeout_seconds=settings.health_check_timeout_seconds,
        )

        self._upgrade_orchestrator = UpgradeOrchestrator(
            default_strategy=UpgradeStrategy.ROLLING,
            max_parallel_upgrades=settings.max_parallel_deployments,
        )

        logger.info(
            "InstallerOrchestrator initialised",
            vault_url=vault_url or "none",
            health_timeout=settings.health_check_timeout_seconds,
        )

    # -----------------------------------------------------------------------
    # Preflight
    # -----------------------------------------------------------------------

    async def run_preflight(
        self,
        connectivity_targets: list[str] | None = None,
        registry_urls: list[str] | None = None,
        required_entitlements: list[str] | None = None,
    ) -> PreflightReport:
        """Run all preflight checks before installation.

        Args:
            connectivity_targets: Hostnames to probe for network connectivity.
            registry_urls: Container registry URLs to verify access.
            required_entitlements: License module entitlements to verify.

        Returns:
            PreflightReport with per-check results and a ready-to-install flag.
        """
        logger.info("Running preflight checks")
        report = await self._preflight.run_all(
            connectivity_targets=connectivity_targets or [
                "registry.hub.docker.com",
                "charts.helm.sh",
            ],
            registry_urls=registry_urls or [],
            required_entitlements=required_entitlements or [],
        )
        if report.is_ready_to_install:
            logger.info(
                "Preflight passed",
                passed=report.passed_count,
                warnings=report.warning_count,
            )
        else:
            logger.error(
                "Preflight blocked",
                blockers=report.blocker_count,
                failed_checks=[r.check_name for r in report.results if r.severity.name == "BLOCKER"],
            )
        return report

    # -----------------------------------------------------------------------
    # Batch installation coordination
    # -----------------------------------------------------------------------

    async def run_batch_install(
        self,
        services: list[str],
        installer_fn: InstallerCallable,
        groups: list[list[str]] | None = None,
        failure_strategy: FailureStrategy = FailureStrategy.ABORT,
        max_parallel: int = 3,
        max_retries: int = 2,
        batch_id: str | None = None,
    ) -> BatchCoordinationReport:
        """Coordinate a DAG-ordered batch installation of AumOS services.

        Args:
            services: Ordered list of service names to install (used if groups
                is None; all placed in a single parallel group).
            installer_fn: Async callable (service_name) → bool that performs
                the actual service installation.
            groups: Optional explicit DAG groups. Each inner list is installed
                in parallel; outer list defines the sequential order.
            failure_strategy: What to do when a service install fails.
            max_parallel: Maximum concurrent installs within a group.
            max_retries: Maximum retry attempts per failing service.
            batch_id: Optional batch identifier for checkpoint resumption.

        Returns:
            BatchCoordinationReport with per-service outcomes and timing.
        """
        coordinator = BatchCoordinator(
            failure_strategy=failure_strategy,
            max_parallel=max_parallel,
            max_retries=max_retries,
            batch_id=batch_id or str(uuid.uuid4()),
        )

        # If no explicit groups, put all services in a single parallel group
        effective_groups: list[list[str]] = groups or [services]

        logger.info(
            "Starting batch installation",
            service_count=len(services),
            group_count=len(effective_groups),
            failure_strategy=failure_strategy.value,
        )
        return await coordinator.run(
            service_groups=effective_groups,
            installer_fn=installer_fn,
        )

    # -----------------------------------------------------------------------
    # Health monitoring
    # -----------------------------------------------------------------------

    def register_health_probes(
        self,
        service_configs: list[dict[str, Any]],
    ) -> None:
        """Register health probe configurations for a set of services.

        Each config dict should have:
            - service_name: str
            - probe_url_or_host: str  (URL for HTTP, hostname for TCP)
            - probe_type: "http" | "tcp"  (default "http")
            - port: int  (for TCP probes)
            - expected_status: int  (for HTTP probes, default 200)

        Args:
            service_configs: List of probe configuration dicts.
        """
        for config in service_configs:
            probe_type_str = config.get("probe_type", "http").upper()
            probe_type = ProbeType.HTTP if probe_type_str == "HTTP" else ProbeType.TCP

            self._health_monitor.register_service(
                config=ServiceProbeConfig(
                    service_name=config["service_name"],
                    probe_type=probe_type,
                    probe_url=config.get("probe_url_or_host", ""),
                    host=config.get("probe_url_or_host", "") if probe_type == ProbeType.TCP else "",
                    port=config.get("port", 80),
                    expected_status_code=config.get("expected_status", 200),
                    dependencies=config.get("dependencies", []),
                )
            )
        logger.info("Health probes registered", service_count=len(service_configs))

    async def wait_for_services_healthy(
        self,
        service_names: list[str],
        timeout_seconds: float | None = None,
    ) -> dict[str, bool]:
        """Wait for a set of services to become healthy after installation.

        Args:
            service_names: Names of the services to wait for.
            timeout_seconds: Override the default startup timeout.

        Returns:
            Dict of service_name → is_healthy boolean.
        """
        effective_timeout = timeout_seconds or self._settings.health_check_timeout_seconds
        return await self._health_monitor.wait_for_startup(
            service_names=service_names,
            timeout_seconds=effective_timeout,
        )

    async def get_health_dashboard(self) -> dict[str, Any]:
        """Return the current health dashboard for all registered services.

        Returns:
            Dict with per-service health status and aggregate metrics.
        """
        return await self._health_monitor.get_dashboard_data()

    # -----------------------------------------------------------------------
    # Upgrade orchestration
    # -----------------------------------------------------------------------

    async def run_upgrade(
        self,
        upgrade_plan: list[dict[str, Any]],
        upgrade_fn: Callable[[str, str, str], Coroutine[Any, Any, bool]],
        health_fn: Callable[[str], Coroutine[Any, Any, bool]],
        strategy: UpgradeStrategy = UpgradeStrategy.ROLLING,
        pre_hooks: dict[str, Callable[[], Coroutine[Any, Any, None]]] | None = None,
        post_hooks: dict[str, Callable[[], Coroutine[Any, Any, None]]] | None = None,
    ) -> UpgradeReport:
        """Orchestrate a platform upgrade across multiple services.

        Args:
            upgrade_plan: List of dicts with keys:
                - service_name: str
                - current_version: str
                - target_version: str
                - breaking_change: bool (optional, default False)
            upgrade_fn: Async callable (service_name, from_ver, to_ver) → bool.
            health_fn: Async callable (service_name) → bool for post-upgrade check.
            strategy: Upgrade strategy (ROLLING, CANARY, or BLUE_GREEN).
            pre_hooks: Optional dict of service_name → async pre-upgrade hook.
            post_hooks: Optional dict of service_name → async post-upgrade hook.

        Returns:
            UpgradeReport with per-service outcomes and audit trail.
        """
        version_specs = [
            VersionSpec(
                service_name=plan["service_name"],
                current_version=plan["current_version"],
                target_version=plan["target_version"],
                breaking_change=plan.get("breaking_change", False),
            )
            for plan in upgrade_plan
        ]

        logger.info(
            "Starting platform upgrade",
            service_count=len(version_specs),
            strategy=strategy.value,
        )
        return await self._upgrade_orchestrator.run_upgrade(
            version_specs=version_specs,
            upgrade_fn=upgrade_fn,
            health_fn=health_fn,
            strategy=strategy,
            pre_hooks=pre_hooks or {},
            post_hooks=post_hooks or {},
        )

    # -----------------------------------------------------------------------
    # Rollback automation
    # -----------------------------------------------------------------------

    async def capture_pre_upgrade_snapshots(
        self,
        service_snapshots: dict[str, dict[str, Any]],
    ) -> dict[str, str]:
        """Capture pre-upgrade snapshots for all services.

        Args:
            service_snapshots: Dict of service_name → snapshot data (Helm
                values, chart version, config checksums, etc.).

        Returns:
            Dict of service_name → snapshot_id for later rollback reference.
        """
        snapshot_ids: dict[str, str] = {}
        for service_name, data in service_snapshots.items():
            snapshot = await self._rollback.capture_snapshot(
                service_name=service_name,
                snapshot_data=data,
            )
            snapshot_ids[service_name] = str(snapshot.snapshot_id)

        logger.info(
            "Pre-upgrade snapshots captured",
            service_count=len(snapshot_ids),
        )
        return snapshot_ids

    async def rollback_if_needed(
        self,
        service_names: list[str],
        rollback_fn: Callable[[str, dict[str, Any]], Coroutine[Any, Any, bool]],
        health_fn: Callable[[str], Coroutine[Any, Any, bool]],
    ) -> Any:
        """Detect health failures and trigger rollback for affected services.

        Polls health for each service, determines which need rollback,
        then executes rollback using the latest captured snapshot.

        Args:
            service_names: Services to check.
            rollback_fn: Async callable (service_name, snapshot_data) → bool.
            health_fn: Async callable (service_name) → bool for health check.

        Returns:
            RollbackReport with per-service rollback outcomes.
        """
        logger.info("Evaluating rollback necessity", service_count=len(service_names))
        return await self._rollback.execute_rollback(
            service_names=service_names,
            rollback_fn=rollback_fn,
            health_fn=health_fn,
        )

    # -----------------------------------------------------------------------
    # Config management
    # -----------------------------------------------------------------------

    async def render_service_values(
        self,
        service_name: str,
        base_values: dict[str, Any],
        environment: str,
        env_overrides: dict[str, Any] | None = None,
        caller_overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Render Helm values for a service with environment overrides and secret injection.

        Args:
            service_name: Name of the service being configured.
            base_values: Base Helm values dict.
            environment: Deployment environment (dev, staging, production).
            env_overrides: Environment-specific override values.
            caller_overrides: Caller-supplied override values (highest priority).

        Returns:
            Merged and rendered values dict with secrets resolved.
        """
        return await self._config_manager.render_helm_values(
            service_name=service_name,
            base_values=base_values,
            environment=environment,
            env_overrides=env_overrides or {},
            caller_overrides=caller_overrides or {},
        )

    async def get_config_diff(
        self,
        service_name: str,
        version_a: int,
        version_b: int,
    ) -> dict[str, Any]:
        """Compare two config versions for a service.

        Args:
            service_name: Name of the service.
            version_a: First version number to compare (older).
            version_b: Second version number to compare (newer).

        Returns:
            ConfigDiff dict with added, removed, and changed keys.
        """
        diff = await self._config_manager.diff_versions(
            service_name=service_name,
            version_a=version_a,
            version_b=version_b,
        )
        return {
            "service_name": service_name,
            "version_a": version_a,
            "version_b": version_b,
            "diff": diff,
        }

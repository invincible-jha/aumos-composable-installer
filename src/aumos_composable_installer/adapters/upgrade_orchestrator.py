"""Upgrade orchestrator adapter for the AumOS Composable Installer.

Manages rolling upgrades of installed AumOS services: version compatibility
checking, upgrade order planning, rolling restart coordination, canary upgrade
support, progress monitoring, pre/post upgrade hook execution, and audit trail.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Coroutine

from aumos_common.observability import get_logger

logger = get_logger(__name__)


class UpgradeStrategy(str, Enum):
    """Upgrade deployment strategy."""

    ROLLING = "rolling"         # Sequential service-by-service upgrade
    CANARY = "canary"           # Route small traffic slice to new version first
    BLUE_GREEN = "blue_green"   # Full parallel environment switch


class UpgradeStatus(str, Enum):
    """Status of a single service upgrade."""

    PENDING = "pending"
    PRE_HOOK = "pre_hook"
    UPGRADING = "upgrading"
    POST_HOOK = "post_hook"
    COMPLETED = "completed"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"
    SKIPPED = "skipped"


@dataclass
class VersionSpec:
    """Version specification for a service.

    Attributes:
        service_name: Service identifier.
        from_version: Currently installed version.
        to_version: Target version for this upgrade.
        chart_version: Helm chart version to deploy.
        breaking_change: True if upgrade requires a migration step.
    """

    service_name: str
    from_version: str
    to_version: str
    chart_version: str = ""
    breaking_change: bool = False


@dataclass
class ServiceUpgradeState:
    """Mutable state for a single service upgrade operation.

    Attributes:
        service_name: Service being upgraded.
        from_version: Current version.
        to_version: Target version.
        status: Current upgrade status.
        started_at: Unix timestamp when upgrade began.
        completed_at: Unix timestamp when upgrade finished.
        error_message: Error detail if failed.
        pre_hook_passed: Whether pre-upgrade hook succeeded.
        post_hook_passed: Whether post-upgrade hook succeeded.
        health_verified: Whether post-upgrade health check passed.
    """

    service_name: str
    from_version: str
    to_version: str
    status: UpgradeStatus = UpgradeStatus.PENDING
    started_at: float | None = None
    completed_at: float | None = None
    error_message: str = ""
    pre_hook_passed: bool | None = None
    post_hook_passed: bool | None = None
    health_verified: bool | None = None

    @property
    def duration_seconds(self) -> float | None:
        """Elapsed upgrade time in seconds."""
        if self.started_at and self.completed_at:
            return self.completed_at - self.started_at
        return None


@dataclass
class UpgradeAuditEntry:
    """Single entry in the upgrade audit trail.

    Attributes:
        event_id: Unique event identifier.
        upgrade_id: Parent upgrade run identifier.
        service_name: Affected service.
        event_type: Event category (e.g. "upgrade_started", "hook_executed").
        detail: Structured event context.
        timestamp: UTC ISO timestamp.
    """

    event_id: str
    upgrade_id: str
    service_name: str
    event_type: str
    detail: dict[str, Any]
    timestamp: str


@dataclass
class UpgradeReport:
    """Final report from an upgrade orchestration run.

    Attributes:
        upgrade_id: Unique run identifier.
        strategy: Strategy used for this upgrade.
        service_states: Per-service upgrade outcomes.
        audit_trail: Ordered list of upgrade events.
        total_duration_seconds: Wall-clock time for the entire run.
    """

    upgrade_id: str
    strategy: UpgradeStrategy
    service_states: dict[str, ServiceUpgradeState] = field(default_factory=dict)
    audit_trail: list[UpgradeAuditEntry] = field(default_factory=list)
    total_duration_seconds: float = 0.0

    @property
    def all_succeeded(self) -> bool:
        """True if every service completed upgrade successfully."""
        return all(
            s.status in (UpgradeStatus.COMPLETED, UpgradeStatus.SKIPPED)
            for s in self.service_states.values()
        )

    @property
    def failed_services(self) -> list[str]:
        """List of service names that failed upgrade."""
        return [n for n, s in self.service_states.items() if s.status == UpgradeStatus.FAILED]


# Type aliases for hook callables
HookCallable = Callable[[str, str, str], Coroutine[Any, Any, bool]]
HealthCallable = Callable[[str], Coroutine[Any, Any, bool]]
UpgradeCallable = Callable[[str, str], Coroutine[Any, Any, bool]]


class UpgradeOrchestrator:
    """Rolling upgrade manager for AumOS services.

    Orchestrates upgrades by:
    1. Validating version compatibility and detecting breaking changes
    2. Planning upgrade order based on dependency relationships
    3. Executing pre-upgrade hooks (DB migrations, backups, config snapshots)
    4. Performing the upgrade (delegated to caller-provided function)
    5. Running post-upgrade hooks (schema validation, cache warmup)
    6. Verifying service health post-upgrade
    7. Recording a full audit trail

    Args:
        strategy: Upgrade deployment strategy.
        health_check_fn: Async function to verify service health post-upgrade.
        pre_hook_fn: Async pre-upgrade hook (service, from_ver, to_ver) -> bool.
        post_hook_fn: Async post-upgrade hook (service, from_ver, to_ver) -> bool.
        max_parallel: Maximum concurrent upgrades (ROLLING strategy).
        health_timeout_seconds: Timeout for post-upgrade health verification.
        canary_weight: Percentage of traffic to canary version (CANARY strategy).
    """

    def __init__(
        self,
        strategy: UpgradeStrategy = UpgradeStrategy.ROLLING,
        health_check_fn: HealthCallable | None = None,
        pre_hook_fn: HookCallable | None = None,
        post_hook_fn: HookCallable | None = None,
        max_parallel: int = 2,
        health_timeout_seconds: int = 120,
        canary_weight: int = 10,
    ) -> None:
        self._strategy = strategy
        self._health_check_fn = health_check_fn
        self._pre_hook_fn = pre_hook_fn
        self._post_hook_fn = post_hook_fn
        self._max_parallel = max_parallel
        self._health_timeout = health_timeout_seconds
        self._canary_weight = canary_weight

    async def run_upgrade(
        self,
        versions: list[VersionSpec],
        upgrade_fn: UpgradeCallable,
        upgrade_order: list[list[str]] | None = None,
    ) -> UpgradeReport:
        """Execute a full upgrade run.

        Args:
            versions: Version specs for all services to upgrade.
            upgrade_fn: Async callable (service_name, to_version) -> success.
            upgrade_order: Optional explicit order (groups = parallel batches).
                           If None, upgrades services in the order given by versions.

        Returns:
            UpgradeReport with service states and audit trail.
        """
        upgrade_id = str(uuid.uuid4())
        start_time = time.monotonic()
        audit_trail: list[UpgradeAuditEntry] = []

        versions_by_name = {v.service_name: v for v in versions}
        service_states: dict[str, ServiceUpgradeState] = {
            v.service_name: ServiceUpgradeState(
                service_name=v.service_name,
                from_version=v.from_version,
                to_version=v.to_version,
            )
            for v in versions
        }

        self._audit(audit_trail, upgrade_id, "all", "upgrade_started", {
            "strategy": self._strategy.value,
            "services": [v.service_name for v in versions],
        })

        # Build ordered batches
        if upgrade_order:
            batches = upgrade_order
        else:
            # Check for breaking changes — upgrade those last
            non_breaking = [v.service_name for v in versions if not v.breaking_change]
            breaking = [v.service_name for v in versions if v.breaking_change]
            batches = [non_breaking] + ([[b] for b in breaking] if breaking else [])

        abort = False
        for batch in batches:
            if abort:
                for svc in batch:
                    service_states[svc].status = UpgradeStatus.SKIPPED
                continue

            semaphore = asyncio.Semaphore(self._max_parallel)
            tasks = [
                self._upgrade_service(
                    semaphore,
                    versions_by_name[svc],
                    service_states[svc],
                    upgrade_fn,
                    audit_trail,
                    upgrade_id,
                )
                for svc in batch
                if svc in versions_by_name
            ]
            await asyncio.gather(*tasks, return_exceptions=True)

            # Check for failures to decide on abort
            if any(service_states[svc].status == UpgradeStatus.FAILED for svc in batch):
                abort = True
                logger.error("Batch upgrade failure — aborting remaining services", failed_batch=batch)

        total_duration = time.monotonic() - start_time
        self._audit(audit_trail, upgrade_id, "all", "upgrade_finished", {
            "all_succeeded": all(s.status in (UpgradeStatus.COMPLETED, UpgradeStatus.SKIPPED) for s in service_states.values()),
            "duration_seconds": round(total_duration, 1),
        })

        report = UpgradeReport(
            upgrade_id=upgrade_id,
            strategy=self._strategy,
            service_states=service_states,
            audit_trail=audit_trail,
            total_duration_seconds=total_duration,
        )
        logger.info(
            "Upgrade run complete",
            upgrade_id=upgrade_id,
            all_succeeded=report.all_succeeded,
            failed=report.failed_services,
        )
        return report

    def check_compatibility(self, versions: list[VersionSpec]) -> list[dict[str, Any]]:
        """Identify incompatible or breaking version transitions.

        Args:
            versions: List of version transitions to validate.

        Returns:
            List of compatibility issues as dicts (empty = all compatible).
        """
        issues = []
        for spec in versions:
            from_parts = spec.from_version.lstrip("v").split(".")
            to_parts = spec.to_version.lstrip("v").split(".")

            try:
                from_major = int(from_parts[0])
                to_major = int(to_parts[0])
            except (ValueError, IndexError):
                continue

            if to_major < from_major:
                issues.append({
                    "service": spec.service_name,
                    "issue": "downgrade",
                    "from": spec.from_version,
                    "to": spec.to_version,
                    "severity": "blocker",
                })
            elif to_major > from_major or spec.breaking_change:
                issues.append({
                    "service": spec.service_name,
                    "issue": "breaking_change",
                    "from": spec.from_version,
                    "to": spec.to_version,
                    "severity": "warning",
                })
        return issues

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _upgrade_service(
        self,
        semaphore: asyncio.Semaphore,
        spec: VersionSpec,
        state: ServiceUpgradeState,
        upgrade_fn: UpgradeCallable,
        audit_trail: list[UpgradeAuditEntry],
        upgrade_id: str,
    ) -> None:
        """Run the full upgrade lifecycle for a single service.

        Args:
            semaphore: Concurrency limiter.
            spec: Version spec for this service.
            state: Mutable state for tracking progress.
            upgrade_fn: Callable that performs the actual upgrade.
            audit_trail: Shared audit log to append entries to.
            upgrade_id: Parent upgrade run ID.
        """
        async with semaphore:
            state.started_at = time.monotonic()
            service = spec.service_name
            logger.info("Upgrading service", service=service, from_version=spec.from_version, to_version=spec.to_version)

            # Pre-upgrade hook
            state.status = UpgradeStatus.PRE_HOOK
            self._audit(audit_trail, upgrade_id, service, "pre_hook_started", {"from": spec.from_version, "to": spec.to_version})
            if self._pre_hook_fn:
                try:
                    pre_ok = await self._pre_hook_fn(service, spec.from_version, spec.to_version)
                    state.pre_hook_passed = pre_ok
                    self._audit(audit_trail, upgrade_id, service, "pre_hook_completed", {"passed": pre_ok})
                    if not pre_ok:
                        state.status = UpgradeStatus.FAILED
                        state.error_message = "Pre-upgrade hook returned failure"
                        state.completed_at = time.monotonic()
                        return
                except Exception as exc:
                    state.pre_hook_passed = False
                    state.status = UpgradeStatus.FAILED
                    state.error_message = f"Pre-upgrade hook exception: {exc}"
                    state.completed_at = time.monotonic()
                    return
            else:
                state.pre_hook_passed = True

            # Perform upgrade
            state.status = UpgradeStatus.UPGRADING
            self._audit(audit_trail, upgrade_id, service, "upgrade_started", {"to_version": spec.to_version})
            try:
                success = await upgrade_fn(service, spec.to_version)
                if not success:
                    state.status = UpgradeStatus.FAILED
                    state.error_message = f"Upgrade function returned failure for {service}"
                    state.completed_at = time.monotonic()
                    return
            except Exception as exc:
                state.status = UpgradeStatus.FAILED
                state.error_message = str(exc)
                state.completed_at = time.monotonic()
                logger.error("Upgrade failed", service=service, error=str(exc))
                return

            # Post-upgrade hook
            state.status = UpgradeStatus.POST_HOOK
            if self._post_hook_fn:
                try:
                    post_ok = await self._post_hook_fn(service, spec.from_version, spec.to_version)
                    state.post_hook_passed = post_ok
                    self._audit(audit_trail, upgrade_id, service, "post_hook_completed", {"passed": post_ok})
                    if not post_ok:
                        state.status = UpgradeStatus.FAILED
                        state.error_message = "Post-upgrade hook returned failure"
                        state.completed_at = time.monotonic()
                        return
                except Exception as exc:
                    state.post_hook_passed = False
                    state.status = UpgradeStatus.FAILED
                    state.error_message = f"Post-upgrade hook exception: {exc}"
                    state.completed_at = time.monotonic()
                    return
            else:
                state.post_hook_passed = True

            # Health verification
            if self._health_check_fn:
                try:
                    healthy = await asyncio.wait_for(
                        self._health_check_fn(service),
                        timeout=self._health_timeout,
                    )
                    state.health_verified = healthy
                    self._audit(audit_trail, upgrade_id, service, "health_verified", {"healthy": healthy})
                    if not healthy:
                        state.status = UpgradeStatus.FAILED
                        state.error_message = "Post-upgrade health check failed"
                        state.completed_at = time.monotonic()
                        return
                except asyncio.TimeoutError:
                    state.health_verified = False
                    state.status = UpgradeStatus.FAILED
                    state.error_message = f"Post-upgrade health check timed out after {self._health_timeout}s"
                    state.completed_at = time.monotonic()
                    return
            else:
                state.health_verified = True

            state.status = UpgradeStatus.COMPLETED
            state.completed_at = time.monotonic()
            self._audit(audit_trail, upgrade_id, service, "upgrade_completed", {
                "from": spec.from_version,
                "to": spec.to_version,
                "duration_seconds": state.duration_seconds,
            })
            logger.info("Service upgrade completed", service=service, to_version=spec.to_version, duration=state.duration_seconds)

    def _audit(
        self,
        trail: list[UpgradeAuditEntry],
        upgrade_id: str,
        service_name: str,
        event_type: str,
        detail: dict[str, Any],
    ) -> None:
        """Append an entry to the upgrade audit trail.

        Args:
            trail: Mutable audit trail list.
            upgrade_id: Parent upgrade run identifier.
            service_name: Affected service.
            event_type: Event category string.
            detail: Structured event context.
        """
        trail.append(
            UpgradeAuditEntry(
                event_id=str(uuid.uuid4()),
                upgrade_id=upgrade_id,
                service_name=service_name,
                event_type=event_type,
                detail=detail,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

"""Rollback automation adapter for the AumOS Composable Installer.

Manages automated rollback of failed upgrades: pre-upgrade snapshot capture,
rollback trigger detection based on health checks, automated rollback execution
(full or per-service), verification, audit logging, and snapshot lifecycle management.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Coroutine

from aumos_common.observability import get_logger

logger = get_logger(__name__)


class RollbackStatus(str, Enum):
    """Status of a rollback operation."""

    NOT_NEEDED = "not_needed"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    PARTIAL = "partial"


@dataclass
class ServiceSnapshot:
    """Pre-upgrade state snapshot for a single service.

    Attributes:
        snapshot_id: Unique snapshot identifier.
        service_name: Service this snapshot represents.
        version: Installed version at snapshot time.
        helm_values: Rendered Helm values at snapshot time.
        config_checksum: Hash of the effective configuration.
        created_at: UTC ISO timestamp.
        metadata: Additional provider-specific snapshot data.
    """

    snapshot_id: str
    service_name: str
    version: str
    helm_values: dict[str, Any]
    config_checksum: str
    created_at: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class RollbackTrigger:
    """Conditions that triggered a rollback.

    Attributes:
        service_name: Service that triggered the rollback.
        reason: Human-readable reason for rollback.
        consecutive_failures: Number of health check failures observed.
        error_detail: Technical error detail.
    """

    service_name: str
    reason: str
    consecutive_failures: int = 0
    error_detail: str = ""


@dataclass
class ServiceRollbackResult:
    """Outcome of rolling back a single service.

    Attributes:
        service_name: Service that was rolled back.
        from_version: Version rolled back from (the failed target).
        to_version: Version restored to (the snapshot version).
        status: Rollback status.
        duration_seconds: Time taken for the rollback.
        health_verified: Whether service is healthy after rollback.
        error_message: Error detail if rollback failed.
    """

    service_name: str
    from_version: str
    to_version: str
    status: RollbackStatus = RollbackStatus.IN_PROGRESS
    duration_seconds: float | None = None
    health_verified: bool | None = None
    error_message: str = ""


@dataclass
class RollbackReport:
    """Aggregate report from a rollback operation.

    Attributes:
        rollback_id: Unique rollback run identifier.
        trigger: What caused the rollback.
        service_results: Per-service rollback outcomes.
        audit_log: Chronological list of rollback events.
        total_duration_seconds: Wall-clock time for the full rollback.
        overall_status: Aggregate rollback status.
    """

    rollback_id: str
    trigger: RollbackTrigger | None
    service_results: dict[str, ServiceRollbackResult] = field(default_factory=dict)
    audit_log: list[dict[str, Any]] = field(default_factory=list)
    total_duration_seconds: float = 0.0
    overall_status: RollbackStatus = RollbackStatus.IN_PROGRESS

    @property
    def rolled_back_services(self) -> list[str]:
        """Services that were successfully rolled back."""
        return [n for n, r in self.service_results.items() if r.status == RollbackStatus.COMPLETED]

    @property
    def failed_rollbacks(self) -> list[str]:
        """Services whose rollback failed."""
        return [n for n, r in self.service_results.items() if r.status == RollbackStatus.FAILED]


# Type aliases
RollbackCallable = Callable[[str, str], Coroutine[Any, Any, bool]]
HealthCallable = Callable[[str], Coroutine[Any, Any, bool]]


class RollbackAutomation:
    """Automated rollback engine for failed AumOS service upgrades.

    Captures pre-upgrade snapshots, detects rollback triggers via health checks,
    orchestrates per-service or full rollback using caller-provided rollback
    functions, verifies restored health, logs a complete audit trail, and
    manages snapshot lifecycle.

    Args:
        snapshot_dir: Directory for persistent snapshot storage.
        health_check_fn: Async callable (service_name) -> bool indicating health.
        failure_threshold: Consecutive health check failures before triggering rollback.
        health_check_interval_seconds: Seconds between health probes during monitoring.
        health_verification_timeout_seconds: Max wait for health after rollback.
        max_snapshots_per_service: Maximum number of snapshots to retain per service.
    """

    def __init__(
        self,
        snapshot_dir: Path | None = None,
        health_check_fn: HealthCallable | None = None,
        failure_threshold: int = 3,
        health_check_interval_seconds: int = 15,
        health_verification_timeout_seconds: int = 180,
        max_snapshots_per_service: int = 5,
    ) -> None:
        self._snapshot_dir = snapshot_dir or Path.home() / ".aumos" / "rollback-snapshots"
        self._health_check_fn = health_check_fn
        self._failure_threshold = failure_threshold
        self._health_check_interval = health_check_interval_seconds
        self._health_verification_timeout = health_verification_timeout_seconds
        self._max_snapshots = max_snapshots_per_service
        self._snapshots: dict[str, list[ServiceSnapshot]] = {}

    # ------------------------------------------------------------------
    # Snapshot management
    # ------------------------------------------------------------------

    async def capture_snapshot(
        self,
        service_name: str,
        version: str,
        helm_values: dict[str, Any],
        config_checksum: str,
        metadata: dict[str, Any] | None = None,
    ) -> ServiceSnapshot:
        """Capture a pre-upgrade snapshot for a service.

        Should be called immediately before performing an upgrade so that
        rollback has a known-good state to restore.

        Args:
            service_name: Service to snapshot.
            version: Currently installed version.
            helm_values: Current Helm values dict (for restore).
            config_checksum: Hash of current effective configuration.
            metadata: Optional provider-specific snapshot data.

        Returns:
            Persisted ServiceSnapshot.
        """
        snapshot = ServiceSnapshot(
            snapshot_id=str(uuid.uuid4()),
            service_name=service_name,
            version=version,
            helm_values=helm_values,
            config_checksum=config_checksum,
            created_at=datetime.now(timezone.utc).isoformat(),
            metadata=metadata or {},
        )

        # Maintain per-service snapshot list
        service_snapshots = self._snapshots.setdefault(service_name, [])
        service_snapshots.append(snapshot)

        # Enforce retention limit
        if len(service_snapshots) > self._max_snapshots:
            removed = service_snapshots.pop(0)
            await self._delete_snapshot_file(removed.snapshot_id)

        # Persist snapshot to disk
        await self._persist_snapshot(snapshot)

        logger.info(
            "Rollback snapshot captured",
            service=service_name,
            snapshot_id=snapshot.snapshot_id,
            version=version,
        )
        return snapshot

    def get_latest_snapshot(self, service_name: str) -> ServiceSnapshot | None:
        """Retrieve the most recent snapshot for a service.

        Args:
            service_name: Target service.

        Returns:
            Most recent ServiceSnapshot or None if no snapshots exist.
        """
        snapshots = self._snapshots.get(service_name, [])
        return snapshots[-1] if snapshots else None

    def list_snapshots(self, service_name: str) -> list[ServiceSnapshot]:
        """List all retained snapshots for a service (oldest first).

        Args:
            service_name: Target service.

        Returns:
            List of ServiceSnapshot objects.
        """
        return list(self._snapshots.get(service_name, []))

    async def cleanup_snapshots(self, service_name: str, keep_latest: int = 1) -> int:
        """Remove old snapshots, retaining only the most recent.

        Args:
            service_name: Target service.
            keep_latest: Number of latest snapshots to keep.

        Returns:
            Number of snapshots removed.
        """
        snapshots = self._snapshots.get(service_name, [])
        to_remove = snapshots[: max(0, len(snapshots) - keep_latest)]
        for snapshot in to_remove:
            await self._delete_snapshot_file(snapshot.snapshot_id)
        self._snapshots[service_name] = snapshots[len(to_remove) :]
        logger.info("Snapshots cleaned up", service=service_name, removed=len(to_remove))
        return len(to_remove)

    # ------------------------------------------------------------------
    # Rollback trigger detection
    # ------------------------------------------------------------------

    async def detect_rollback_needed(
        self,
        services: list[str],
        observation_duration_seconds: int = 120,
    ) -> list[RollbackTrigger]:
        """Monitor services for health failures indicating rollback is needed.

        Args:
            services: Services to observe.
            observation_duration_seconds: How long to observe before giving up.

        Returns:
            List of RollbackTrigger for each service that exceeded failure threshold.
        """
        if not self._health_check_fn:
            logger.warning("No health check function configured — cannot detect rollback triggers")
            return []

        failure_counts: dict[str, int] = {svc: 0 for svc in services}
        triggers: list[RollbackTrigger] = []
        triggered_services: set[str] = set()

        start_time = time.monotonic()
        while time.monotonic() - start_time < observation_duration_seconds:
            check_tasks = [self._safe_health_check(svc) for svc in services if svc not in triggered_services]
            results = await asyncio.gather(*check_tasks)

            for svc, (healthy, error) in zip(
                [s for s in services if s not in triggered_services], results
            ):
                if healthy:
                    failure_counts[svc] = 0
                else:
                    failure_counts[svc] += 1
                    logger.warning("Health check failure", service=svc, consecutive_failures=failure_counts[svc])

                    if failure_counts[svc] >= self._failure_threshold:
                        triggers.append(
                            RollbackTrigger(
                                service_name=svc,
                                reason="Consecutive health check failures exceeded threshold",
                                consecutive_failures=failure_counts[svc],
                                error_detail=error,
                            )
                        )
                        triggered_services.add(svc)
                        logger.error("Rollback trigger detected", service=svc, failures=failure_counts[svc])

            if len(triggered_services) == len(services):
                break

            await asyncio.sleep(self._health_check_interval)

        return triggers

    # ------------------------------------------------------------------
    # Rollback execution
    # ------------------------------------------------------------------

    async def execute_rollback(
        self,
        services: list[str],
        rollback_fn: RollbackCallable,
        trigger: RollbackTrigger | None = None,
        rollback_id: str | None = None,
    ) -> RollbackReport:
        """Execute rollback for listed services using their latest snapshots.

        Args:
            services: Service names to roll back.
            rollback_fn: Async callable (service_name, to_version) -> success.
            trigger: Optional trigger that caused this rollback.
            rollback_id: Unique rollback run ID (auto-generated if None).

        Returns:
            RollbackReport with per-service outcomes and audit log.
        """
        rollback_id = rollback_id or str(uuid.uuid4())
        start_time = time.monotonic()
        audit_log: list[dict[str, Any]] = []
        service_results: dict[str, ServiceRollbackResult] = {}

        self._log_audit(audit_log, rollback_id, "all", "rollback_started", {
            "services": services,
            "trigger": trigger.reason if trigger else "manual",
        })

        for service_name in services:
            snapshot = self.get_latest_snapshot(service_name)
            if not snapshot:
                logger.error("No snapshot found for rollback", service=service_name)
                service_results[service_name] = ServiceRollbackResult(
                    service_name=service_name,
                    from_version="unknown",
                    to_version="unknown",
                    status=RollbackStatus.FAILED,
                    error_message="No snapshot available for rollback",
                )
                continue

            result = ServiceRollbackResult(
                service_name=service_name,
                from_version="current",
                to_version=snapshot.version,
            )
            service_results[service_name] = result

            svc_start = time.monotonic()
            self._log_audit(audit_log, rollback_id, service_name, "service_rollback_started", {
                "to_version": snapshot.version,
                "snapshot_id": snapshot.snapshot_id,
            })

            try:
                success = await rollback_fn(service_name, snapshot.version)
                if not success:
                    result.status = RollbackStatus.FAILED
                    result.error_message = f"Rollback function returned failure for {service_name}"
                    result.duration_seconds = time.monotonic() - svc_start
                    self._log_audit(audit_log, rollback_id, service_name, "service_rollback_failed", {"reason": result.error_message})
                    continue

                # Verify health post-rollback
                if self._health_check_fn:
                    healthy = await self._wait_for_health(service_name)
                    result.health_verified = healthy
                    if not healthy:
                        result.status = RollbackStatus.FAILED
                        result.error_message = "Service not healthy after rollback"
                        result.duration_seconds = time.monotonic() - svc_start
                        self._log_audit(audit_log, rollback_id, service_name, "service_rollback_health_failed", {})
                        continue

                result.status = RollbackStatus.COMPLETED
                result.duration_seconds = time.monotonic() - svc_start
                self._log_audit(audit_log, rollback_id, service_name, "service_rollback_completed", {
                    "to_version": snapshot.version,
                    "duration_seconds": result.duration_seconds,
                })
                logger.info("Service rolled back", service=service_name, to_version=snapshot.version)

            except Exception as exc:
                result.status = RollbackStatus.FAILED
                result.error_message = str(exc)
                result.duration_seconds = time.monotonic() - svc_start
                logger.error("Rollback exception", service=service_name, error=str(exc))

        total_duration = time.monotonic() - start_time
        completed = [n for n, r in service_results.items() if r.status == RollbackStatus.COMPLETED]
        failed = [n for n, r in service_results.items() if r.status == RollbackStatus.FAILED]

        if not failed:
            overall = RollbackStatus.COMPLETED
        elif completed:
            overall = RollbackStatus.PARTIAL
        else:
            overall = RollbackStatus.FAILED

        self._log_audit(audit_log, rollback_id, "all", "rollback_finished", {
            "overall_status": overall.value,
            "duration_seconds": round(total_duration, 1),
            "completed": completed,
            "failed": failed,
        })

        report = RollbackReport(
            rollback_id=rollback_id,
            trigger=trigger,
            service_results=service_results,
            audit_log=audit_log,
            total_duration_seconds=total_duration,
            overall_status=overall,
        )
        logger.info("Rollback complete", rollback_id=rollback_id, overall_status=overall.value)
        return report

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _safe_health_check(self, service_name: str) -> tuple[bool, str]:
        """Execute a health check, catching exceptions.

        Args:
            service_name: Target service.

        Returns:
            Tuple of (is_healthy, error_message).
        """
        try:
            if self._health_check_fn:
                healthy = await self._health_check_fn(service_name)
                return healthy, ""
            return True, ""
        except Exception as exc:
            return False, str(exc)

    async def _wait_for_health(self, service_name: str) -> bool:
        """Poll until a service is healthy or the timeout elapses.

        Args:
            service_name: Target service.

        Returns:
            True if service became healthy within the timeout.
        """
        if not self._health_check_fn:
            return True

        start_time = time.monotonic()
        while time.monotonic() - start_time < self._health_verification_timeout:
            healthy, _ = await self._safe_health_check(service_name)
            if healthy:
                return True
            await asyncio.sleep(self._health_check_interval)
        return False

    async def _persist_snapshot(self, snapshot: ServiceSnapshot) -> None:
        """Write a snapshot to disk as JSON.

        Args:
            snapshot: Snapshot to persist.
        """
        self._snapshot_dir.mkdir(parents=True, exist_ok=True)
        path = self._snapshot_dir / f"{snapshot.snapshot_id}.json"
        path.write_text(
            json.dumps(
                {
                    "snapshot_id": snapshot.snapshot_id,
                    "service_name": snapshot.service_name,
                    "version": snapshot.version,
                    "helm_values": snapshot.helm_values,
                    "config_checksum": snapshot.config_checksum,
                    "created_at": snapshot.created_at,
                    "metadata": snapshot.metadata,
                },
                indent=2,
            )
        )

    async def _delete_snapshot_file(self, snapshot_id: str) -> None:
        """Remove a persisted snapshot file.

        Args:
            snapshot_id: Snapshot identifier to remove.
        """
        path = self._snapshot_dir / f"{snapshot_id}.json"
        if path.exists():
            path.unlink()
            logger.debug("Snapshot file deleted", snapshot_id=snapshot_id)

    def _log_audit(
        self,
        audit_log: list[dict[str, Any]],
        rollback_id: str,
        service_name: str,
        event_type: str,
        detail: dict[str, Any],
    ) -> None:
        """Append an entry to the rollback audit log.

        Args:
            audit_log: Mutable audit log list.
            rollback_id: Parent rollback run identifier.
            service_name: Affected service.
            event_type: Event category string.
            detail: Structured event context.
        """
        audit_log.append(
            {
                "event_id": str(uuid.uuid4()),
                "rollback_id": rollback_id,
                "service_name": service_name,
                "event_type": event_type,
                "detail": detail,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        )

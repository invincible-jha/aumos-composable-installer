"""Post-installation health monitor for the AumOS Composable Installer.

Polls installed services via HTTP health endpoints and TCP probes, monitors
startup sequences, verifies dependency chain health, detects degradation,
provides dashboard data, fires alerts, and stores historical health records.
"""

from __future__ import annotations

import asyncio
import socket
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

import httpx

from aumos_common.observability import get_logger

logger = get_logger(__name__)

_MAX_HISTORY_PER_SERVICE = 100


class ProbeType(str, Enum):
    """Type of health probe to perform."""

    HTTP = "http"
    TCP = "tcp"


class ServiceHealthStatus(str, Enum):
    """Health status of a monitored service."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    STARTING = "starting"
    UNKNOWN = "unknown"


@dataclass
class ServiceProbeConfig:
    """Configuration for probing a single service.

    Attributes:
        service_name: Human-readable service identifier.
        probe_type: HTTP or TCP.
        host: Hostname or IP.
        port: Port number.
        path: HTTP path for health endpoint (ignored for TCP).
        expected_status: Expected HTTP status code (HTTP probes only).
        timeout_seconds: Per-probe timeout.
        depends_on: List of service names this service depends on.
    """

    service_name: str
    probe_type: ProbeType
    host: str
    port: int
    path: str = "/api/v1/health"
    expected_status: int = 200
    timeout_seconds: int = 5
    depends_on: list[str] = field(default_factory=list)


@dataclass
class HealthSnapshot:
    """A single health probe result.

    Attributes:
        timestamp: UTC ISO timestamp.
        status: Health status at this moment.
        latency_ms: Probe round-trip time.
        status_code: HTTP status code (0 for TCP or connection failure).
        error: Error message if probe failed.
    """

    timestamp: str
    status: ServiceHealthStatus
    latency_ms: float
    status_code: int = 0
    error: str = ""


@dataclass
class ServiceHealthRecord:
    """Aggregated health tracking for a single service.

    Attributes:
        service_name: Service identifier.
        current_status: Most recent health status.
        consecutive_failures: Count of consecutive failed probes.
        history: Ring buffer of recent HealthSnapshots.
        first_healthy_at: Timestamp of first successful probe.
        last_healthy_at: Timestamp of most recent successful probe.
    """

    service_name: str
    current_status: ServiceHealthStatus = ServiceHealthStatus.UNKNOWN
    consecutive_failures: int = 0
    history: deque[HealthSnapshot] = field(default_factory=lambda: deque(maxlen=_MAX_HISTORY_PER_SERVICE))
    first_healthy_at: str | None = None
    last_healthy_at: str | None = None

    @property
    def uptime_percent(self) -> float:
        """Percentage of healthy probes in recorded history."""
        if not self.history:
            return 0.0
        healthy = sum(1 for h in self.history if h.status == ServiceHealthStatus.HEALTHY)
        return round(healthy / len(self.history) * 100, 1)

    @property
    def avg_latency_ms(self) -> float:
        """Average probe latency in milliseconds over recorded history."""
        latencies = [h.latency_ms for h in self.history if h.latency_ms > 0]
        return round(sum(latencies) / len(latencies), 2) if latencies else 0.0


class InstallationHealthMonitor:
    """Post-install health monitoring for AumOS services.

    Provides:
    - HTTP and TCP health probing of installed services
    - Startup sequence monitoring (tracks transition from STARTING to HEALTHY)
    - Dependency chain verification (checks upstream services first)
    - Degradation detection (threshold-based consecutive failure counting)
    - Dashboard data aggregation across all monitored services
    - Alert callback invocation on status transitions
    - Historical health record retention

    Args:
        probe_interval_seconds: Seconds between polling cycles.
        degraded_threshold: Consecutive failures before marking DEGRADED.
        unhealthy_threshold: Consecutive failures before marking UNHEALTHY.
        startup_timeout_seconds: Max seconds to wait for STARTING -> HEALTHY.
        alert_callback: Async callable invoked on health degradation events.
    """

    def __init__(
        self,
        probe_interval_seconds: int = 30,
        degraded_threshold: int = 2,
        unhealthy_threshold: int = 5,
        startup_timeout_seconds: int = 300,
        alert_callback: Any | None = None,
    ) -> None:
        self._probe_interval = probe_interval_seconds
        self._degraded_threshold = degraded_threshold
        self._unhealthy_threshold = unhealthy_threshold
        self._startup_timeout = startup_timeout_seconds
        self._alert_callback = alert_callback

        self._service_configs: dict[str, ServiceProbeConfig] = {}
        self._health_records: dict[str, ServiceHealthRecord] = {}
        self._monitoring = False

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def register_service(self, config: ServiceProbeConfig) -> None:
        """Register a service for health monitoring.

        Args:
            config: Probe configuration for the service.
        """
        self._service_configs[config.service_name] = config
        self._health_records[config.service_name] = ServiceHealthRecord(
            service_name=config.service_name,
            current_status=ServiceHealthStatus.STARTING,
        )
        logger.info("Service registered for health monitoring", service=config.service_name, probe_type=config.probe_type)

    def register_services(self, configs: list[ServiceProbeConfig]) -> None:
        """Register multiple services for health monitoring.

        Args:
            configs: List of probe configurations.
        """
        for config in configs:
            self.register_service(config)

    # ------------------------------------------------------------------
    # Startup monitoring
    # ------------------------------------------------------------------

    async def wait_for_startup(
        self,
        services: list[str],
        timeout_seconds: int | None = None,
    ) -> dict[str, bool]:
        """Poll services until all are HEALTHY or timeout elapses.

        Args:
            services: Service names to wait for.
            timeout_seconds: Override the default startup timeout.

        Returns:
            Dict mapping service name to bool (True = reached HEALTHY).
        """
        timeout = timeout_seconds or self._startup_timeout
        start_time = time.monotonic()
        pending = set(services)
        results: dict[str, bool] = {}

        logger.info("Waiting for services to start", services=services, timeout_seconds=timeout)

        while pending and (time.monotonic() - start_time) < timeout:
            probe_tasks = [self._probe_service(svc) for svc in list(pending)]
            snapshots = await asyncio.gather(*probe_tasks)

            for service_name, snapshot in zip(list(pending), snapshots):
                if snapshot.status == ServiceHealthStatus.HEALTHY:
                    results[service_name] = True
                    pending.discard(service_name)
                    logger.info("Service startup confirmed healthy", service=service_name)

            if pending:
                await asyncio.sleep(min(self._probe_interval, 10))

        for service_name in pending:
            results[service_name] = False
            logger.warning("Service startup timed out", service=service_name, timeout_seconds=timeout)

        return results

    # ------------------------------------------------------------------
    # Dependency chain verification
    # ------------------------------------------------------------------

    async def verify_dependency_chain(self, root_service: str) -> dict[str, ServiceHealthStatus]:
        """Recursively probe a service and all its declared dependencies.

        Args:
            root_service: Starting service for the dependency traversal.

        Returns:
            Dict mapping each service name to its health status.
        """
        visited: set[str] = set()
        chain_status: dict[str, ServiceHealthStatus] = {}

        async def probe_recursive(service_name: str) -> None:
            if service_name in visited:
                return
            visited.add(service_name)

            config = self._service_configs.get(service_name)
            if config:
                # Probe dependencies first
                dep_tasks = [probe_recursive(dep) for dep in config.depends_on]
                await asyncio.gather(*dep_tasks)

                snapshot = await self._probe_service(service_name)
                chain_status[service_name] = snapshot.status
                self._update_record(service_name, snapshot)
            else:
                chain_status[service_name] = ServiceHealthStatus.UNKNOWN

        await probe_recursive(root_service)
        logger.info("Dependency chain verified", root=root_service, chain_length=len(chain_status))
        return chain_status

    # ------------------------------------------------------------------
    # Continuous monitoring
    # ------------------------------------------------------------------

    async def start_monitoring(self) -> None:
        """Begin continuous health monitoring of all registered services.

        Runs until stop_monitoring() is called.
        """
        self._monitoring = True
        logger.info("Health monitoring started", services=list(self._service_configs))

        while self._monitoring:
            await self._poll_all_services()
            await asyncio.sleep(self._probe_interval)

    def stop_monitoring(self) -> None:
        """Signal the monitoring loop to exit."""
        self._monitoring = False
        logger.info("Health monitoring stopped")

    async def poll_once(self) -> dict[str, ServiceHealthStatus]:
        """Run a single polling cycle across all registered services.

        Returns:
            Dict mapping service name to current health status.
        """
        return await self._poll_all_services()

    # ------------------------------------------------------------------
    # Dashboard data
    # ------------------------------------------------------------------

    def get_dashboard_data(self) -> dict[str, Any]:
        """Aggregate health data for dashboard display.

        Returns:
            Dict with summary statistics and per-service health records.
        """
        total = len(self._health_records)
        healthy = sum(1 for r in self._health_records.values() if r.current_status == ServiceHealthStatus.HEALTHY)
        unhealthy = sum(1 for r in self._health_records.values() if r.current_status == ServiceHealthStatus.UNHEALTHY)
        degraded = sum(1 for r in self._health_records.values() if r.current_status == ServiceHealthStatus.DEGRADED)

        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "summary": {
                "total_services": total,
                "healthy": healthy,
                "degraded": degraded,
                "unhealthy": unhealthy,
                "unknown": total - healthy - degraded - unhealthy,
                "overall_status": "healthy" if healthy == total else ("degraded" if healthy > 0 else "unhealthy"),
            },
            "services": {
                name: {
                    "status": record.current_status.value,
                    "uptime_percent": record.uptime_percent,
                    "avg_latency_ms": record.avg_latency_ms,
                    "consecutive_failures": record.consecutive_failures,
                    "last_healthy_at": record.last_healthy_at,
                }
                for name, record in self._health_records.items()
            },
        }

    def get_service_history(self, service_name: str) -> list[dict[str, Any]]:
        """Retrieve health probe history for a service.

        Args:
            service_name: Target service.

        Returns:
            List of health snapshot dicts, newest first.
        """
        record = self._health_records.get(service_name)
        if not record:
            return []
        return [
            {
                "timestamp": snap.timestamp,
                "status": snap.status.value,
                "latency_ms": snap.latency_ms,
                "status_code": snap.status_code,
                "error": snap.error,
            }
            for snap in reversed(list(record.history))
        ]

    # ------------------------------------------------------------------
    # Internal probing
    # ------------------------------------------------------------------

    async def _poll_all_services(self) -> dict[str, ServiceHealthStatus]:
        """Probe all registered services concurrently.

        Returns:
            Dict of service name to current status.
        """
        service_names = list(self._service_configs)
        probe_tasks = [self._probe_service(name) for name in service_names]
        snapshots = await asyncio.gather(*probe_tasks)

        current_statuses: dict[str, ServiceHealthStatus] = {}
        for service_name, snapshot in zip(service_names, snapshots):
            prior_status = self._health_records[service_name].current_status
            self._update_record(service_name, snapshot)
            current_statuses[service_name] = snapshot.status

            # Fire alert on degradation
            if prior_status == ServiceHealthStatus.HEALTHY and snapshot.status != ServiceHealthStatus.HEALTHY:
                await self._fire_alert(service_name, prior_status, snapshot.status, snapshot.error)

        return current_statuses

    async def _probe_service(self, service_name: str) -> HealthSnapshot:
        """Execute a health probe for a single service.

        Args:
            service_name: Target service name.

        Returns:
            HealthSnapshot with probe results.
        """
        config = self._service_configs.get(service_name)
        if not config:
            return HealthSnapshot(
                timestamp=datetime.now(timezone.utc).isoformat(),
                status=ServiceHealthStatus.UNKNOWN,
                latency_ms=0.0,
                error="Service not registered",
            )

        if config.probe_type == ProbeType.HTTP:
            return await self._http_probe(config)
        return await self._tcp_probe(config)

    async def _http_probe(self, config: ServiceProbeConfig) -> HealthSnapshot:
        """Execute an HTTP health probe.

        Args:
            config: Probe configuration.

        Returns:
            HealthSnapshot from the HTTP response.
        """
        url = f"http://{config.host}:{config.port}{config.path}"
        start = time.monotonic()
        timestamp = datetime.now(timezone.utc).isoformat()

        try:
            async with httpx.AsyncClient(timeout=config.timeout_seconds) as client:
                response = await client.get(url)
                latency_ms = (time.monotonic() - start) * 1000

                if response.status_code == config.expected_status:
                    return HealthSnapshot(
                        timestamp=timestamp,
                        status=ServiceHealthStatus.HEALTHY,
                        latency_ms=round(latency_ms, 2),
                        status_code=response.status_code,
                    )
                return HealthSnapshot(
                    timestamp=timestamp,
                    status=ServiceHealthStatus.UNHEALTHY,
                    latency_ms=round(latency_ms, 2),
                    status_code=response.status_code,
                    error=f"Unexpected status {response.status_code}",
                )
        except Exception as exc:
            latency_ms = (time.monotonic() - start) * 1000
            return HealthSnapshot(
                timestamp=timestamp,
                status=ServiceHealthStatus.UNHEALTHY,
                latency_ms=round(latency_ms, 2),
                error=str(exc),
            )

    async def _tcp_probe(self, config: ServiceProbeConfig) -> HealthSnapshot:
        """Execute a TCP connectivity probe.

        Args:
            config: Probe configuration.

        Returns:
            HealthSnapshot from the TCP connection attempt.
        """
        start = time.monotonic()
        timestamp = datetime.now(timezone.utc).isoformat()

        def _attempt_connect() -> bool:
            try:
                with socket.create_connection((config.host, config.port), timeout=config.timeout_seconds):
                    return True
            except (socket.timeout, ConnectionRefusedError, OSError):
                return False

        try:
            connected = await asyncio.get_event_loop().run_in_executor(None, _attempt_connect)
            latency_ms = (time.monotonic() - start) * 1000
            status = ServiceHealthStatus.HEALTHY if connected else ServiceHealthStatus.UNHEALTHY
            return HealthSnapshot(
                timestamp=timestamp,
                status=status,
                latency_ms=round(latency_ms, 2),
                error="" if connected else f"TCP connection refused to {config.host}:{config.port}",
            )
        except Exception as exc:
            latency_ms = (time.monotonic() - start) * 1000
            return HealthSnapshot(
                timestamp=timestamp,
                status=ServiceHealthStatus.UNHEALTHY,
                latency_ms=round(latency_ms, 2),
                error=str(exc),
            )

    def _update_record(self, service_name: str, snapshot: HealthSnapshot) -> None:
        """Update the health record for a service with a new snapshot.

        Computes consecutive_failures and promotes status to DEGRADED or UNHEALTHY
        based on configured thresholds.

        Args:
            service_name: Target service.
            snapshot: Probe result snapshot.
        """
        record = self._health_records.get(service_name)
        if not record:
            return

        record.history.append(snapshot)

        if snapshot.status == ServiceHealthStatus.HEALTHY:
            record.consecutive_failures = 0
            record.current_status = ServiceHealthStatus.HEALTHY
            record.last_healthy_at = snapshot.timestamp
            if not record.first_healthy_at:
                record.first_healthy_at = snapshot.timestamp
        else:
            record.consecutive_failures += 1
            if record.consecutive_failures >= self._unhealthy_threshold:
                record.current_status = ServiceHealthStatus.UNHEALTHY
            elif record.consecutive_failures >= self._degraded_threshold:
                record.current_status = ServiceHealthStatus.DEGRADED

    async def _fire_alert(
        self,
        service_name: str,
        previous: ServiceHealthStatus,
        current: ServiceHealthStatus,
        error: str,
    ) -> None:
        """Invoke the alert callback on health status transitions.

        Args:
            service_name: Affected service.
            previous: Prior health status.
            current: New health status.
            error: Error description from the probe.
        """
        if not self._alert_callback:
            return

        alert_payload = {
            "service": service_name,
            "previous_status": previous.value,
            "current_status": current.value,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "error": error,
        }

        try:
            if asyncio.iscoroutinefunction(self._alert_callback):
                await self._alert_callback(alert_payload)
            else:
                self._alert_callback(alert_payload)
        except Exception as exc:
            logger.warning("Alert callback failed", service=service_name, error=str(exc))

        logger.warning(
            "Health alert fired",
            service=service_name,
            previous=previous.value,
            current=current.value,
        )

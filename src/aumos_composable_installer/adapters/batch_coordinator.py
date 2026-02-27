"""Batch installation coordinator for the AumOS Composable Installer.

Manages multi-service installation with dependency-ordered sequencing, parallel
installation where safe, per-service progress tracking, failure handling
strategies, partial installation state, checkpoint resume, and coordination
reporting.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Coroutine

from aumos_common.observability import get_logger

logger = get_logger(__name__)


class InstallationStatus(str, Enum):
    """Status of a single service installation."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class FailureStrategy(str, Enum):
    """Strategy when a service installation fails."""

    ABORT = "abort"           # Stop all remaining installations
    CONTINUE = "continue"     # Skip failed service, continue with others
    RETRY = "retry"           # Retry up to max_retries times


@dataclass
class ServiceInstallationState:
    """Tracks the installation state of a single service.

    Attributes:
        service_name: Service identifier.
        status: Current installation status.
        started_at: Unix timestamp when installation began.
        completed_at: Unix timestamp when installation finished (or failed).
        error_message: Error details if status is FAILED.
        retry_count: Number of retries attempted.
        checkpoint_data: Arbitrary checkpoint data for resume.
    """

    service_name: str
    status: InstallationStatus = InstallationStatus.PENDING
    started_at: float | None = None
    completed_at: float | None = None
    error_message: str = ""
    retry_count: int = 0
    checkpoint_data: dict[str, Any] = field(default_factory=dict)

    @property
    def duration_seconds(self) -> float | None:
        """Elapsed installation time in seconds."""
        if self.started_at and self.completed_at:
            return self.completed_at - self.started_at
        return None


@dataclass
class BatchCoordinationReport:
    """Final report from a batch installation run.

    Attributes:
        batch_id: Unique ID for this installation run.
        service_states: Per-service installation outcomes.
        total_duration_seconds: Wall-clock time for the entire batch.
        failure_strategy: Strategy that was in effect.
        checkpoint_path: Path to the saved checkpoint file (if any).
    """

    batch_id: str
    service_states: dict[str, ServiceInstallationState] = field(default_factory=dict)
    total_duration_seconds: float = 0.0
    failure_strategy: FailureStrategy = FailureStrategy.ABORT
    checkpoint_path: str | None = None

    @property
    def completed_services(self) -> list[str]:
        """Names of successfully installed services."""
        return [n for n, s in self.service_states.items() if s.status == InstallationStatus.COMPLETED]

    @property
    def failed_services(self) -> list[str]:
        """Names of services that failed installation."""
        return [n for n, s in self.service_states.items() if s.status == InstallationStatus.FAILED]

    @property
    def all_succeeded(self) -> bool:
        """True if every service installed successfully."""
        return all(s.status in (InstallationStatus.COMPLETED, InstallationStatus.SKIPPED) for s in self.service_states.values())

    def summary(self) -> dict[str, Any]:
        """Produce a structured summary for display or storage.

        Returns:
            Dict with counts, flags, and service-level outcomes.
        """
        return {
            "batch_id": self.batch_id,
            "all_succeeded": self.all_succeeded,
            "completed": len(self.completed_services),
            "failed": len(self.failed_services),
            "total": len(self.service_states),
            "duration_seconds": round(self.total_duration_seconds, 1),
            "failure_strategy": self.failure_strategy.value,
            "services": {
                name: {
                    "status": s.status.value,
                    "duration_seconds": s.duration_seconds,
                    "error": s.error_message or None,
                }
                for name, s in self.service_states.items()
            },
        }


# Type alias for the installer callable each service runs
InstallerCallable = Callable[[str], Coroutine[Any, Any, bool]]


class BatchCoordinator:
    """Multi-service installation coordinator with DAG ordering.

    Resolves the installation order from a dependency graph, runs services in
    dependency order (parallel where no ordering constraint exists), tracks
    progress, handles failures per configured strategy, and persists checkpoint
    state so installations can resume after interruption.

    Args:
        checkpoint_dir: Directory for checkpoint state files.
        failure_strategy: What to do when a service install fails.
        max_retries: Retry attempts when strategy is RETRY.
        max_parallel: Maximum concurrent installations.
    """

    def __init__(
        self,
        checkpoint_dir: Path | None = None,
        failure_strategy: FailureStrategy = FailureStrategy.ABORT,
        max_retries: int = 2,
        max_parallel: int = 4,
    ) -> None:
        self._checkpoint_dir = checkpoint_dir or Path.home() / ".aumos" / "checkpoints"
        self._failure_strategy = failure_strategy
        self._max_retries = max_retries
        self._max_parallel = max_parallel

    async def run(
        self,
        install_order: list[list[str]],
        installer: InstallerCallable,
        batch_id: str | None = None,
        resume_from_checkpoint: bool = False,
    ) -> BatchCoordinationReport:
        """Execute a batch installation in dependency order.

        Args:
            install_order: List of service groups; services within each group
                can be installed in parallel, groups must be sequential.
            installer: Async callable accepting a service name, returning True on success.
            batch_id: Unique batch identifier (auto-generated if None).
            resume_from_checkpoint: If True, load prior state and skip COMPLETED services.

        Returns:
            BatchCoordinationReport with outcomes for all services.
        """
        batch_id = batch_id or str(uuid.uuid4())
        flat_services = [svc for group in install_order for svc in group]
        start_time = time.monotonic()

        # Load checkpoint if resuming
        prior_states: dict[str, ServiceInstallationState] = {}
        if resume_from_checkpoint:
            prior_states = await self._load_checkpoint(batch_id)
            logger.info("Resuming from checkpoint", batch_id=batch_id, prior_completed=len(prior_states))

        # Initialize states
        service_states: dict[str, ServiceInstallationState] = {}
        for service_name in flat_services:
            if service_name in prior_states:
                service_states[service_name] = prior_states[service_name]
            else:
                service_states[service_name] = ServiceInstallationState(service_name=service_name)

        abort_requested = False

        for group in install_order:
            if abort_requested:
                for service_name in group:
                    if service_states[service_name].status == InstallationStatus.PENDING:
                        service_states[service_name].status = InstallationStatus.SKIPPED
                continue

            # Skip already-completed services from checkpoint
            pending_in_group = [
                svc for svc in group
                if service_states[svc].status != InstallationStatus.COMPLETED
            ]

            if not pending_in_group:
                logger.info("Skipping group — all services already installed", group=group)
                continue

            logger.info("Installing service group", group=pending_in_group, parallel=len(pending_in_group) > 1)

            semaphore = asyncio.Semaphore(self._max_parallel)
            tasks = [
                self._install_with_semaphore(semaphore, service_name, service_states[service_name], installer)
                for service_name in pending_in_group
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            # Handle results
            for service_name, result in zip(pending_in_group, results):
                state = service_states[service_name]
                if isinstance(result, Exception):
                    state.status = InstallationStatus.FAILED
                    state.error_message = str(result)
                    state.completed_at = time.monotonic()

                if state.status == InstallationStatus.FAILED:
                    logger.error("Service installation failed", service=service_name, error=state.error_message)
                    if self._failure_strategy == FailureStrategy.ABORT:
                        abort_requested = True
                        break

            # Save checkpoint after each group
            checkpoint_path = await self._save_checkpoint(batch_id, service_states)

        report = BatchCoordinationReport(
            batch_id=batch_id,
            service_states=service_states,
            total_duration_seconds=time.monotonic() - start_time,
            failure_strategy=self._failure_strategy,
            checkpoint_path=str(checkpoint_path) if checkpoint_path else None,
        )

        logger.info(
            "Batch installation complete",
            batch_id=batch_id,
            completed=len(report.completed_services),
            failed=len(report.failed_services),
            duration_seconds=round(report.total_duration_seconds, 1),
        )
        return report

    async def _install_with_semaphore(
        self,
        semaphore: asyncio.Semaphore,
        service_name: str,
        state: ServiceInstallationState,
        installer: InstallerCallable,
    ) -> None:
        """Run a single service installation under a concurrency semaphore.

        Args:
            semaphore: Concurrency limiter.
            service_name: Service to install.
            state: Mutable state tracker for this service.
            installer: Async installer callable.
        """
        async with semaphore:
            state.status = InstallationStatus.IN_PROGRESS
            state.started_at = time.monotonic()
            logger.info("Installing service", service=service_name)

            attempt = 0
            while attempt <= self._max_retries:
                try:
                    success = await installer(service_name)
                    if success:
                        state.status = InstallationStatus.COMPLETED
                        state.completed_at = time.monotonic()
                        logger.info(
                            "Service installed successfully",
                            service=service_name,
                            duration_seconds=state.duration_seconds,
                        )
                        return
                    else:
                        state.error_message = f"Installer returned failure for {service_name}"
                except Exception as exc:
                    state.error_message = str(exc)
                    state.retry_count += 1
                    logger.warning(
                        "Service installation attempt failed",
                        service=service_name,
                        attempt=attempt + 1,
                        error=str(exc),
                    )

                attempt += 1
                if attempt <= self._max_retries and self._failure_strategy == FailureStrategy.RETRY:
                    await asyncio.sleep(min(2 ** attempt, 30))  # Exponential backoff, max 30s
                else:
                    break

            state.status = InstallationStatus.FAILED
            state.completed_at = time.monotonic()

    async def _save_checkpoint(
        self, batch_id: str, service_states: dict[str, ServiceInstallationState]
    ) -> Path:
        """Persist the current installation state to a checkpoint file.

        Args:
            batch_id: Batch identifier used for the checkpoint filename.
            service_states: Current service states to persist.

        Returns:
            Path to the saved checkpoint file.
        """
        self._checkpoint_dir.mkdir(parents=True, exist_ok=True)
        checkpoint_path = self._checkpoint_dir / f"{batch_id}.json"

        checkpoint_data = {
            "batch_id": batch_id,
            "services": {
                name: {
                    "status": state.status.value,
                    "started_at": state.started_at,
                    "completed_at": state.completed_at,
                    "error_message": state.error_message,
                    "retry_count": state.retry_count,
                }
                for name, state in service_states.items()
            },
        }

        checkpoint_path.write_text(json.dumps(checkpoint_data, indent=2))
        logger.debug("Checkpoint saved", path=str(checkpoint_path))
        return checkpoint_path

    async def _load_checkpoint(self, batch_id: str) -> dict[str, ServiceInstallationState]:
        """Load previously persisted installation state from checkpoint.

        Args:
            batch_id: Batch identifier to load checkpoint for.

        Returns:
            Dict of service name to ServiceInstallationState. Empty if no checkpoint found.
        """
        checkpoint_path = self._checkpoint_dir / f"{batch_id}.json"
        if not checkpoint_path.exists():
            logger.warning("No checkpoint found for batch", batch_id=batch_id)
            return {}

        try:
            checkpoint_data: dict[str, Any] = json.loads(checkpoint_path.read_text())
            states: dict[str, ServiceInstallationState] = {}
            for service_name, service_data in checkpoint_data.get("services", {}).items():
                state = ServiceInstallationState(service_name=service_name)
                state.status = InstallationStatus(service_data.get("status", "pending"))
                state.started_at = service_data.get("started_at")
                state.completed_at = service_data.get("completed_at")
                state.error_message = service_data.get("error_message", "")
                state.retry_count = service_data.get("retry_count", 0)
                states[service_name] = state
            logger.info("Checkpoint loaded", batch_id=batch_id, service_count=len(states))
            return states
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("Failed to load checkpoint", batch_id=batch_id, error=str(exc))
            return {}

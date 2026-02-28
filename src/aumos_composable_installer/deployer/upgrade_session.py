"""Upgrade session management with pre-upgrade snapshot and rollback support.

Gap #10: Upgrade rollback — captures a pre-upgrade state snapshot and provides
rollback to the previous Helm release revision on health check failure.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from aumos_common.observability import get_logger

logger = get_logger(__name__)


@dataclass
class UpgradeSnapshot:
    """Pre-upgrade state snapshot for rollback purposes.

    Attributes:
        release_name: Helm release name.
        namespace: Kubernetes namespace.
        previous_revision: Helm revision number before the upgrade.
        previous_chart_version: Chart version before the upgrade.
        snapshot_taken_at: ISO timestamp when the snapshot was taken.
        values_snapshot: Helm values at the time of the snapshot.
    """

    release_name: str
    namespace: str
    previous_revision: int
    previous_chart_version: str
    snapshot_taken_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    values_snapshot: dict[str, Any] = field(default_factory=dict)


class UpgradeSession:
    """Manages an upgrade operation with pre-upgrade snapshot and rollback capability.

    Takes a snapshot of the current Helm release before upgrade. If the post-upgrade
    health check fails, rolls back to the previous revision atomically.
    """

    def __init__(self, release_name: str, namespace: str) -> None:
        """Initialize the UpgradeSession.

        Args:
            release_name: Helm release name.
            namespace: Kubernetes namespace.
        """
        self._release_name = release_name
        self._namespace = namespace
        self._snapshot: UpgradeSnapshot | None = None

    def take_snapshot(self) -> UpgradeSnapshot:
        """Capture the current Helm release state before upgrade.

        Returns:
            UpgradeSnapshot with the pre-upgrade state.

        Raises:
            RuntimeError: If the current Helm release cannot be inspected.
        """
        history_result = subprocess.run(
            [
                "helm",
                "history",
                self._release_name,
                "--namespace",
                self._namespace,
                "--max",
                "1",
                "--output",
                "json",
            ],
            capture_output=True,
            text=True,
            check=False,
        )

        previous_revision = 1
        previous_chart_version = "unknown"

        if history_result.returncode == 0 and history_result.stdout.strip():
            import json

            try:
                history = json.loads(history_result.stdout)
                if history:
                    latest = history[-1]
                    previous_revision = int(latest.get("revision", 1))
                    previous_chart_version = latest.get("chart", "unknown")
            except (json.JSONDecodeError, KeyError, ValueError):
                logger.warning("could_not_parse_helm_history", release=self._release_name)

        values_result = subprocess.run(
            ["helm", "get", "values", self._release_name, "--namespace", self._namespace, "--output", "json"],
            capture_output=True,
            text=True,
            check=False,
        )
        values_snapshot: dict[str, Any] = {}
        if values_result.returncode == 0:
            import json

            try:
                values_snapshot = json.loads(values_result.stdout) or {}
            except json.JSONDecodeError:
                pass

        self._snapshot = UpgradeSnapshot(
            release_name=self._release_name,
            namespace=self._namespace,
            previous_revision=previous_revision,
            previous_chart_version=previous_chart_version,
            values_snapshot=values_snapshot,
        )

        logger.info(
            "upgrade_snapshot_taken",
            release=self._release_name,
            revision=previous_revision,
            chart=previous_chart_version,
        )
        return self._snapshot

    def rollback(self) -> bool:
        """Roll back to the pre-upgrade Helm revision.

        Returns:
            True if the rollback succeeded, False otherwise.

        Raises:
            RuntimeError: If take_snapshot() was not called before rollback().
        """
        if self._snapshot is None:
            raise RuntimeError("Cannot rollback: no snapshot was taken. Call take_snapshot() first.")

        logger.info(
            "upgrade_rollback_initiated",
            release=self._release_name,
            target_revision=self._snapshot.previous_revision,
        )

        result = subprocess.run(
            [
                "helm",
                "rollback",
                self._release_name,
                str(self._snapshot.previous_revision),
                "--namespace",
                self._namespace,
                "--wait",
                "--timeout",
                "5m",
            ],
            capture_output=True,
            text=True,
            check=False,
        )

        success = result.returncode == 0
        if success:
            logger.info("upgrade_rollback_succeeded", release=self._release_name)
        else:
            logger.error("upgrade_rollback_failed", release=self._release_name, stderr=result.stderr)

        return success

    @property
    def snapshot(self) -> UpgradeSnapshot | None:
        """The pre-upgrade snapshot, or None if not yet taken.

        Returns:
            The UpgradeSnapshot or None.
        """
        return self._snapshot

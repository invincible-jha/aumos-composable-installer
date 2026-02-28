"""Opt-in telemetry collector for installer usage analytics.

Gap #13: Telemetry/usage reporting.

Telemetry is NEVER sent without explicit opt-in. When enabled, the collector
sends anonymous installation events (no PII, no license key content) to the
AumOS telemetry endpoint. All data is described in the privacy notice at
https://docs.aumos.ai/privacy.
"""

from __future__ import annotations

import asyncio
import platform
import uuid
from datetime import datetime
from typing import Any

import httpx
from pydantic import BaseModel, Field

from aumos_common.observability import get_logger

logger = get_logger(__name__)

_TELEMETRY_ENDPOINT = "https://telemetry.aumos.ai/v1/events"
_TELEMETRY_TIMEOUT_SECONDS = 5.0


class TelemetryEvent(BaseModel):
    """A single telemetry event payload.

    No PII is included. The session_id is ephemeral and not linked to any account.

    Attributes:
        event_type: Event name (e.g., install_started, install_completed).
        aumos_version: AumOS version being installed.
        python_version: Python runtime version.
        os_name: Operating system name.
        modules_count: Number of modules installed.
        duration_seconds: Operation duration in seconds.
        success: Whether the operation succeeded.
        session_id: Ephemeral random UUID for this CLI session (not stored).
        timestamp: ISO timestamp of the event.
    """

    event_type: str
    aumos_version: str = "unknown"
    python_version: str = Field(default_factory=lambda: platform.python_version())
    os_name: str = Field(default_factory=lambda: platform.system())
    modules_count: int = 0
    duration_seconds: float = 0.0
    success: bool = True
    session_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: str = Field(default_factory=lambda: datetime.utcnow().isoformat())


class TelemetryCollector:
    """Sends anonymous installer usage events to the AumOS telemetry service.

    Telemetry is fire-and-forget — failures are logged at DEBUG level and
    never propagated to the caller to avoid disrupting installations.
    """

    def __init__(self, enabled: bool, endpoint: str = _TELEMETRY_ENDPOINT) -> None:
        """Initialize the TelemetryCollector.

        Args:
            enabled: Whether telemetry is enabled. Must be True to send events.
            endpoint: Telemetry HTTPS endpoint URL.
        """
        self._enabled = enabled
        self._endpoint = endpoint

    def send(self, event: TelemetryEvent) -> None:
        """Send a telemetry event asynchronously (fire-and-forget).

        Args:
            event: The event to send. Silently dropped if telemetry is disabled.
        """
        if not self._enabled:
            return

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(self._post(event))
            else:
                asyncio.run(self._post(event))
        except Exception as exc:
            logger.debug("telemetry_send_skipped", error=str(exc))

    async def _post(self, event: TelemetryEvent) -> None:
        """POST the event payload to the telemetry endpoint.

        Args:
            event: Telemetry event to post.
        """
        try:
            async with httpx.AsyncClient(timeout=_TELEMETRY_TIMEOUT_SECONDS) as client:
                response = await client.post(
                    self._endpoint,
                    json=event.model_dump(),
                    headers={"Content-Type": "application/json", "User-Agent": "aumos-installer/1.0"},
                )
                logger.debug("telemetry_sent", status=response.status_code)
        except Exception as exc:
            logger.debug("telemetry_post_failed", error=str(exc))

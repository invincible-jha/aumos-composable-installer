"""Post-install health verification for AumOS modules.

After deployment, the health checker polls each module's health endpoint
until all modules report healthy or the timeout elapses.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum

import httpx
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

from aumos_common.observability import get_logger

from aumos_composable_installer.resolver.module_manifest import ManifestLoader

logger = get_logger(__name__)
console = Console()


class HealthStatus(str, Enum):
    """Health status for a single module."""

    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    TIMEOUT = "timeout"
    UNKNOWN = "unknown"


@dataclass
class ModuleHealthResult:
    """Health check result for a single module.

    Attributes:
        module_name: Name of the module checked.
        status: Overall health status.
        http_status: HTTP status code returned (0 if connection failed).
        response_time_ms: Time taken for the health check in milliseconds.
        error_message: Error message if unhealthy or timed out.
        url: Full URL that was checked.
    """

    module_name: str
    status: HealthStatus
    http_status: int = 0
    response_time_ms: float = 0.0
    error_message: str = ""
    url: str = ""

    @property
    def is_healthy(self) -> bool:
        """Whether this module passed health check."""
        return self.status == HealthStatus.HEALTHY


@dataclass
class HealthReport:
    """Aggregate health report for all checked modules.

    Attributes:
        results: Individual results keyed by module name.
        total_duration_seconds: Total time taken for all checks.
    """

    results: dict[str, ModuleHealthResult] = field(default_factory=dict)
    total_duration_seconds: float = 0.0

    @property
    def all_healthy(self) -> bool:
        """True if every module passed health check."""
        return all(r.is_healthy for r in self.results.values())

    @property
    def healthy_count(self) -> int:
        """Number of healthy modules."""
        return sum(1 for r in self.results.values() if r.is_healthy)

    @property
    def unhealthy_modules(self) -> list[str]:
        """List of module names that failed health check."""
        return sorted(name for name, r in self.results.items() if not r.is_healthy)


class HealthChecker:
    """Post-install health verification for AumOS modules.

    Polls each activated module's health endpoint and reports status.
    Supports configurable timeout and polling interval.
    """

    def __init__(
        self,
        loader: ManifestLoader,
        base_url_template: str = "http://{module}.aumos.svc.cluster.local",
        timeout_seconds: int = 300,
        interval_seconds: int = 10,
    ) -> None:
        """Initialize the health checker.

        Args:
            loader: ManifestLoader to get health check URLs from manifests.
            base_url_template: URL template where {module} is replaced with module name.
            timeout_seconds: Maximum time to wait for all modules to become healthy.
            interval_seconds: Time between health check polls.
        """
        self._loader = loader
        self._base_url_template = base_url_template
        self._timeout_seconds = timeout_seconds
        self._interval_seconds = interval_seconds

    async def check_all(self, modules: list[str]) -> HealthReport:
        """Check health of all listed modules, polling until healthy or timeout.

        Args:
            modules: List of module names to health-check.

        Returns:
            HealthReport with results for all modules.
        """
        start_time = time.monotonic()
        pending = set(modules)
        results: dict[str, ModuleHealthResult] = {}

        logger.info("Starting post-install health checks", modules=modules, timeout=self._timeout_seconds)

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            TimeElapsedColumn(),
            console=console,
        ) as progress:
            task = progress.add_task(f"Checking health of {len(modules)} module(s)...", total=len(modules))

            while pending and (time.monotonic() - start_time) < self._timeout_seconds:
                check_results = await self._check_batch(list(pending))

                for result in check_results:
                    if result.is_healthy:
                        results[result.module_name] = result
                        pending.discard(result.module_name)
                        progress.advance(task)
                        logger.info("Module healthy", module=result.module_name)
                    else:
                        logger.debug(
                            "Module not yet healthy",
                            module=result.module_name,
                            status=result.http_status,
                            error=result.error_message,
                        )

                if pending:
                    await asyncio.sleep(self._interval_seconds)

        # Mark any remaining pending modules as timed out
        for module_name in pending:
            results[module_name] = ModuleHealthResult(
                module_name=module_name,
                status=HealthStatus.TIMEOUT,
                error_message=f"Health check timed out after {self._timeout_seconds}s",
                url=self._build_url(module_name, ""),
            )
            logger.warning("Module health check timed out", module=module_name)

        report = HealthReport(
            results=results,
            total_duration_seconds=time.monotonic() - start_time,
        )
        logger.info(
            "Health check complete",
            healthy=report.healthy_count,
            total=len(modules),
            duration_seconds=round(report.total_duration_seconds, 1),
        )
        return report

    async def check_single(self, module_name: str) -> ModuleHealthResult:
        """Perform a single health check for one module (no retry).

        Args:
            module_name: Name of the module to check.

        Returns:
            ModuleHealthResult for the single check attempt.
        """
        try:
            manifest = self._loader.get(module_name)
            health_path = manifest.health_check.url
            timeout = manifest.health_check.timeout_seconds
        except KeyError:
            health_path = "/api/v1/health"
            timeout = 10

        url = self._build_url(module_name, health_path)
        return await self._do_check(module_name, url, timeout)

    async def _check_batch(self, modules: list[str]) -> list[ModuleHealthResult]:
        """Run health checks for multiple modules concurrently.

        Args:
            modules: List of module names.

        Returns:
            List of ModuleHealthResult, one per module.
        """
        tasks = [self.check_single(module_name) for module_name in modules]
        return list(await asyncio.gather(*tasks, return_exceptions=False))

    def _build_url(self, module_name: str, path: str) -> str:
        """Build the full health check URL for a module.

        Args:
            module_name: Module name (used in URL template substitution).
            path: Health endpoint path.

        Returns:
            Full URL string.
        """
        base = self._base_url_template.format(module=module_name)
        return f"{base.rstrip('/')}{path}"

    async def _do_check(self, module_name: str, url: str, timeout: int) -> ModuleHealthResult:
        """Execute a single HTTP health check.

        Args:
            module_name: Name of the module.
            url: Full health check URL.
            timeout: Request timeout in seconds.

        Returns:
            ModuleHealthResult with the check outcome.
        """
        start = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.get(url)
                elapsed_ms = (time.monotonic() - start) * 1000

                if response.status_code == 200:
                    return ModuleHealthResult(
                        module_name=module_name,
                        status=HealthStatus.HEALTHY,
                        http_status=response.status_code,
                        response_time_ms=elapsed_ms,
                        url=url,
                    )
                return ModuleHealthResult(
                    module_name=module_name,
                    status=HealthStatus.UNHEALTHY,
                    http_status=response.status_code,
                    response_time_ms=elapsed_ms,
                    error_message=f"Unexpected status {response.status_code}",
                    url=url,
                )
        except httpx.RequestError as exc:
            elapsed_ms = (time.monotonic() - start) * 1000
            return ModuleHealthResult(
                module_name=module_name,
                status=HealthStatus.UNHEALTHY,
                http_status=0,
                response_time_ms=elapsed_ms,
                error_message=str(exc),
                url=url,
            )

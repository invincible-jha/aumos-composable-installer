"""Deployer protocol (interface) for the AumOS composable installer plugin system.

Gap #14: Plugin system for custom deployers.

All deployer backends must implement the IDeployer protocol. Custom deployers
can be registered via Python entry_points under the group 'aumos.deployers'.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from aumos_composable_installer.resolver.dependency_graph import ResolutionResult


@runtime_checkable
class IDeployer(Protocol):
    """Protocol that all deployer backends must implement.

    Custom deployers registered via entry_points inherit this interface.
    All methods must be synchronous; async deployers should run their event
    loop internally.
    """

    def install(self, resolution: ResolutionResult, dry_run: bool = False) -> Any:
        """Install the resolved module set.

        Args:
            resolution: Resolved dependency order and module list.
            dry_run: If True, simulate without making changes.

        Returns:
            A result object with at minimum a `success: bool` attribute.
        """
        ...

    def upgrade(
        self,
        resolution: ResolutionResult,
        chart_version: str = "",
        dry_run: bool = False,
    ) -> Any:
        """Upgrade an existing installation.

        Args:
            resolution: Resolved module set to upgrade.
            chart_version: Target version string (empty = latest).
            dry_run: If True, simulate without making changes.

        Returns:
            A result object with at minimum a `success: bool` attribute.
        """
        ...

    def uninstall(self, release_name: str, namespace: str, dry_run: bool = False) -> Any:
        """Uninstall an existing release.

        Args:
            release_name: The release to uninstall.
            namespace: Kubernetes namespace.
            dry_run: If True, simulate without making changes.

        Returns:
            A result object with at minimum a `success: bool` attribute.
        """
        ...

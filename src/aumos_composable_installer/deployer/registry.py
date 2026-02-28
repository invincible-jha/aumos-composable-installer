"""Deployer plugin registry — discovers and loads custom deployer backends.

Gap #14: Plugin system for custom deployers.

Custom deployers are registered via Python entry_points under the group
'aumos.deployers'. This allows third-party plugins to add new deployment
backends (e.g., Flux, Crossplane, custom GitOps) without modifying the
core installer.

Entry point format in pyproject.toml:
  [project.entry-points."aumos.deployers"]
  mybackend = "my_package.my_module:MyDeployer"
"""

from __future__ import annotations

from importlib.metadata import entry_points
from typing import Any

from aumos_common.observability import get_logger

from aumos_composable_installer.deployer.interfaces import IDeployer

logger = get_logger(__name__)

_ENTRY_POINT_GROUP = "aumos.deployers"


class DeployerRegistry:
    """Registry of available deployer backends.

    Discovers built-in deployers and any third-party plugins registered
    via the 'aumos.deployers' entry point group. Validates that each
    discovered backend implements IDeployer.
    """

    def __init__(self) -> None:
        """Initialize the DeployerRegistry with built-in deployers pre-registered."""
        self._backends: dict[str, type[Any]] = {}
        self._register_builtins()
        self._discover_plugins()

    def _register_builtins(self) -> None:
        """Register the built-in deployer backends."""
        from aumos_composable_installer.deployer.helm_deployer import HelmDeployer
        from aumos_composable_installer.deployer.argocd_deployer import ArgoCDDeployer
        from aumos_composable_installer.deployer.docker_compose_deployer import DockerComposeDeployer

        self._backends["helm"] = HelmDeployer
        self._backends["argocd"] = ArgoCDDeployer
        self._backends["docker-compose"] = DockerComposeDeployer

        logger.debug("builtins_registered", backends=list(self._backends.keys()))

    def _discover_plugins(self) -> None:
        """Discover and register third-party deployer plugins via entry_points."""
        try:
            eps = entry_points(group=_ENTRY_POINT_GROUP)
        except Exception as exc:
            logger.warning("entry_point_discovery_failed", error=str(exc))
            return

        for ep in eps:
            try:
                deployer_cls = ep.load()
                if not isinstance(deployer_cls, type):
                    logger.warning("entry_point_not_a_class", name=ep.name)
                    continue
                self._backends[ep.name] = deployer_cls
                logger.info("plugin_deployer_registered", name=ep.name, cls=deployer_cls.__qualname__)
            except Exception as exc:
                logger.warning("plugin_load_failed", name=ep.name, error=str(exc))

    def get(self, backend_name: str, **kwargs: Any) -> IDeployer:
        """Instantiate and return a deployer backend by name.

        Args:
            backend_name: Backend name (e.g., 'helm', 'argocd', 'docker-compose').
            **kwargs: Constructor keyword arguments passed to the deployer.

        Returns:
            An instantiated deployer implementing IDeployer.

        Raises:
            KeyError: If the backend name is not registered.
            TypeError: If the loaded class does not implement IDeployer.
        """
        if backend_name not in self._backends:
            available = ", ".join(sorted(self._backends))
            raise KeyError(f"Unknown deployer backend '{backend_name}'. Available: {available}")

        deployer_cls = self._backends[backend_name]
        instance = deployer_cls(**kwargs)

        if not isinstance(instance, IDeployer):
            raise TypeError(f"Deployer '{backend_name}' does not implement the IDeployer protocol.")

        return instance

    def list_backends(self) -> list[str]:
        """Return a list of all registered backend names.

        Returns:
            Sorted list of backend name strings.
        """
        return sorted(self._backends.keys())

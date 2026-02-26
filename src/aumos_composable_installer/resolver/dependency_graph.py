"""DAG-based dependency resolver with topological sort.

Builds a directed acyclic graph of module dependencies and computes
a valid installation order using Kahn's topological sort algorithm.
Detects circular dependencies and raises ConflictError.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from aumos_common.errors import ConflictError
from aumos_common.observability import get_logger

from aumos_composable_installer.resolver.module_manifest import ManifestLoader, ModuleManifest

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# AumOS module dependency graph — encoded from the platform specification.
# This defines the canonical dependency order for all AumOS modules.
# ---------------------------------------------------------------------------
AUMOS_MODULE_DEPS: dict[str, list[str]] = {
    # Tier A — always on, these form the foundation
    "core-platform": [],
    "event-bus": ["core-platform"],
    "data-layer": ["core-platform"],
    "observability": ["core-platform"],
    "secrets-vault": ["core-platform"],
    "auth-gateway": ["core-platform"],
    # Tier B — optional commercial modules
    "data-factory": ["core-platform", "auth-gateway", "event-bus", "data-layer", "observability", "secrets-vault"],
    "governance": ["core-platform", "auth-gateway", "event-bus", "data-layer", "observability", "secrets-vault"],
    "security": ["core-platform", "auth-gateway", "event-bus", "data-layer", "observability", "secrets-vault"],
    "mlops": ["core-platform", "auth-gateway", "event-bus", "data-layer", "observability", "secrets-vault"],
    "marketplace": [
        "core-platform",
        "auth-gateway",
        "event-bus",
        "data-layer",
        "observability",
        "secrets-vault",
        "data-factory",
    ],
}

# Modules that are always activated as part of the base platform install
FOUNDATION_MODULES: frozenset[str] = frozenset({
    "core-platform",
    "auth-gateway",
    "event-bus",
    "data-layer",
    "observability",
    "secrets-vault",
})


@dataclass
class ResolutionResult:
    """Result of dependency resolution for a set of requested modules.

    Attributes:
        install_order: Modules in topological order (dependencies first).
        all_modules: Complete set of modules including auto-resolved dependencies.
        explicitly_requested: Modules that were explicitly requested by the user.
        auto_included: Modules auto-included to satisfy dependencies.
    """

    install_order: list[str]
    all_modules: set[str]
    explicitly_requested: set[str]
    auto_included: set[str] = field(default_factory=set)

    @property
    def total_count(self) -> int:
        """Total number of modules to install."""
        return len(self.install_order)


class DependencyGraph:
    """Directed acyclic graph for AumOS module dependencies.

    Builds the graph from module manifests and the canonical AumOS
    dependency map, then performs topological sort to determine
    installation order.
    """

    def __init__(self, loader: ManifestLoader) -> None:
        """Initialize the dependency graph.

        Args:
            loader: ManifestLoader to fetch module metadata.
        """
        self._loader = loader
        self._adjacency: dict[str, list[str]] = {}
        self._built = False

    def build(self) -> None:
        """Build the adjacency list from manifests and the canonical dep map.

        Merges dependencies declared in module manifests with the canonical
        AUMOS_MODULE_DEPS map. Manifest declarations take precedence for
        additional dependencies not in the canonical map.
        """
        manifests = self._loader.load_all()

        # Start with canonical deps
        self._adjacency = {name: list(deps) for name, deps in AUMOS_MODULE_DEPS.items()}

        # Merge manifest-declared deps
        for name, manifest in manifests.items():
            if name not in self._adjacency:
                self._adjacency[name] = []
            for dep in manifest.dependencies:
                if dep not in self._adjacency[name]:
                    self._adjacency[name].append(dep)

        self._built = True
        logger.debug("Dependency graph built", modules=list(self._adjacency.keys()))

    def resolve(self, requested_modules: list[str]) -> ResolutionResult:
        """Resolve dependencies for the requested modules.

        Expands the requested set to include all transitive dependencies,
        then computes a valid topological installation order.

        Args:
            requested_modules: Module names explicitly requested by the user.

        Returns:
            ResolutionResult with install order and dependency metadata.

        Raises:
            KeyError: If a requested module is not in the graph.
            ConflictError: If circular dependencies are detected.
        """
        if not self._built:
            self.build()

        requested_set = set(requested_modules)
        unknown = requested_set - set(self._adjacency.keys())
        if unknown:
            raise KeyError(f"Unknown modules: {sorted(unknown)}. Available: {sorted(self._adjacency.keys())}")

        # Expand to full dependency closure
        all_modules = self._compute_closure(requested_set)
        auto_included = all_modules - requested_set

        # Topological sort on the closure subgraph
        install_order = self._topological_sort(all_modules)

        if auto_included:
            logger.info(
                "Auto-included dependency modules",
                auto_included=sorted(auto_included),
            )

        result = ResolutionResult(
            install_order=install_order,
            all_modules=all_modules,
            explicitly_requested=requested_set,
            auto_included=auto_included,
        )
        logger.info(
            "Dependency resolution complete",
            requested=len(requested_set),
            total=result.total_count,
            install_order=install_order,
        )
        return result

    def get_dependents(self, module_name: str) -> list[str]:
        """Find all modules that depend on the given module.

        Useful for deactivation — determines which modules will be
        broken if the given module is deactivated.

        Args:
            module_name: Module whose reverse dependencies to find.

        Returns:
            List of module names that depend on the given module.
        """
        if not self._built:
            self.build()
        return [
            name
            for name, deps in self._adjacency.items()
            if module_name in deps
        ]

    def _compute_closure(self, modules: set[str]) -> set[str]:
        """Compute the transitive dependency closure of a module set.

        Args:
            modules: Initial set of modules.

        Returns:
            Expanded set including all transitive dependencies.

        Raises:
            KeyError: If a dependency references an unknown module.
        """
        closure: set[str] = set()
        queue = deque(modules)

        while queue:
            module = queue.popleft()
            if module in closure:
                continue
            closure.add(module)
            for dep in self._adjacency.get(module, []):
                if dep not in self._adjacency:
                    raise KeyError(
                        f"Module '{module}' declares dependency '{dep}' which is not in the graph"
                    )
                if dep not in closure:
                    queue.append(dep)

        return closure

    def _topological_sort(self, modules: set[str]) -> list[str]:
        """Kahn's algorithm for topological sort on a subgraph.

        Args:
            modules: Set of modules to sort (must be a valid closure).

        Returns:
            Modules in dependency order (dependencies come before dependents).

        Raises:
            ConflictError: If a cycle is detected in the dependency graph.
        """
        # Build in-degree map restricted to the subgraph
        in_degree: dict[str, int] = {module: 0 for module in modules}
        subgraph: dict[str, list[str]] = {module: [] for module in modules}

        for module in modules:
            for dep in self._adjacency.get(module, []):
                if dep in modules:
                    subgraph[dep].append(module)
                    in_degree[module] += 1

        # Start with all zero-in-degree nodes
        queue: deque[str] = deque(
            sorted(module for module, degree in in_degree.items() if degree == 0)
        )
        sorted_order: list[str] = []

        while queue:
            node = queue.popleft()
            sorted_order.append(node)
            for dependent in sorted(subgraph.get(node, [])):
                in_degree[dependent] -= 1
                if in_degree[dependent] == 0:
                    queue.append(dependent)

        if len(sorted_order) != len(modules):
            # Cycle detected — find it for a helpful error message
            cyclic = sorted(modules - set(sorted_order))
            raise ConflictError(
                f"Circular dependency detected among modules: {cyclic}. "
                "Cannot determine installation order."
            )

        return sorted_order

"""Dependency resolution engine for AumOS module activation."""

from aumos_composable_installer.resolver.conflict_detector import ConflictDetector
from aumos_composable_installer.resolver.dependency_graph import DependencyGraph, ResolutionResult
from aumos_composable_installer.resolver.module_manifest import ManifestLoader, ModuleManifest

__all__ = [
    "ConflictDetector",
    "DependencyGraph",
    "ManifestLoader",
    "ModuleManifest",
    "ResolutionResult",
]

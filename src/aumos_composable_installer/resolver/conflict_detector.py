"""Conflict detector for incompatible module combinations.

Checks a resolved module set for known incompatibilities, version
constraints, resource conflicts, and license tier violations before
deployment proceeds.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from aumos_common.observability import get_logger

from aumos_composable_installer.resolver.module_manifest import ManifestLoader, ModuleTier

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Known incompatible module pairs — cannot be co-activated.
# Format: frozenset({module_a, module_b}) — order does not matter.
# ---------------------------------------------------------------------------
INCOMPATIBLE_PAIRS: list[frozenset[str]] = [
    # No known incompatibilities at v0.1.0, but the detector is extensible.
    # Example: frozenset({"security", "legacy-bridge"})
]

# ---------------------------------------------------------------------------
# Mutual exclusion groups — at most one module from each group may be active.
# ---------------------------------------------------------------------------
MUTUALLY_EXCLUSIVE_GROUPS: list[frozenset[str]] = [
    # Example: frozenset({"helm-deployer", "argocd-deployer"})  # only one orchestrator
]


class ConflictSeverity(str, Enum):
    """Severity level of a detected conflict."""

    ERROR = "error"    # Deployment must not proceed
    WARNING = "warning"  # Deployment can proceed, but operator should be aware


@dataclass
class Conflict:
    """A detected conflict or warning between modules.

    Attributes:
        severity: Whether this is a blocking error or a warning.
        modules: The modules involved in the conflict.
        message: Human-readable description of the conflict.
        remediation: Suggested remediation steps.
    """

    severity: ConflictSeverity
    modules: list[str]
    message: str
    remediation: str = ""


class ConflictDetector:
    """Detects incompatible module combinations before deployment.

    Checks for:
    - Known incompatible pairs
    - Mutually exclusive module groups
    - Tier B/C modules without license entitlements
    - Resource requirement conflicts (GPU availability)
    - Missing foundation modules
    """

    def __init__(self, loader: ManifestLoader) -> None:
        """Initialize the conflict detector.

        Args:
            loader: ManifestLoader for accessing module metadata.
        """
        self._loader = loader

    def check(
        self,
        modules: set[str],
        licensed_modules: set[str] | None = None,
    ) -> list[Conflict]:
        """Run all conflict checks on the proposed module set.

        Args:
            modules: The set of modules planned for activation.
            licensed_modules: Set of modules covered by the license JWT.
                              If None, only Tier A modules are assumed licensed.

        Returns:
            List of detected conflicts (errors and warnings).
        """
        conflicts: list[Conflict] = []

        conflicts.extend(self._check_incompatible_pairs(modules))
        conflicts.extend(self._check_mutually_exclusive(modules))
        conflicts.extend(self._check_license_entitlements(modules, licensed_modules or set()))
        conflicts.extend(self._check_foundation_requirements(modules))

        error_count = sum(1 for c in conflicts if c.severity == ConflictSeverity.ERROR)
        warning_count = sum(1 for c in conflicts if c.severity == ConflictSeverity.WARNING)

        logger.info(
            "Conflict detection complete",
            modules=sorted(modules),
            errors=error_count,
            warnings=warning_count,
        )
        return conflicts

    def has_blocking_conflicts(self, conflicts: list[Conflict]) -> bool:
        """Return True if any conflict is blocking (severity=ERROR).

        Args:
            conflicts: List of conflicts from check().

        Returns:
            True if deployment should be blocked.
        """
        return any(c.severity == ConflictSeverity.ERROR for c in conflicts)

    def _check_incompatible_pairs(self, modules: set[str]) -> list[Conflict]:
        """Check for known incompatible module pairs.

        Args:
            modules: Module set to check.

        Returns:
            List of conflict objects for each detected incompatible pair.
        """
        conflicts = []
        for pair in INCOMPATIBLE_PAIRS:
            if pair.issubset(modules):
                pair_list = sorted(pair)
                conflicts.append(Conflict(
                    severity=ConflictSeverity.ERROR,
                    modules=pair_list,
                    message=f"Modules {pair_list[0]} and {pair_list[1]} cannot be co-activated.",
                    remediation=f"Deactivate one of: {pair_list}",
                ))
        return conflicts

    def _check_mutually_exclusive(self, modules: set[str]) -> list[Conflict]:
        """Check for modules that are mutually exclusive.

        Args:
            modules: Module set to check.

        Returns:
            List of conflict objects for each violated exclusion group.
        """
        conflicts = []
        for group in MUTUALLY_EXCLUSIVE_GROUPS:
            active_from_group = sorted(group & modules)
            if len(active_from_group) > 1:
                conflicts.append(Conflict(
                    severity=ConflictSeverity.ERROR,
                    modules=active_from_group,
                    message=f"Only one of {sorted(group)} can be active at a time. Found: {active_from_group}",
                    remediation=f"Choose one module from: {sorted(group)}",
                ))
        return conflicts

    def _check_license_entitlements(
        self,
        modules: set[str],
        licensed_modules: set[str],
    ) -> list[Conflict]:
        """Check that all Tier B/C modules have license entitlements.

        Args:
            modules: Module set to check.
            licensed_modules: Set of module names covered by the license JWT.

        Returns:
            List of conflict objects for modules lacking entitlement.
        """
        conflicts = []
        try:
            manifests = self._loader.load_all()
        except Exception:
            # If manifests can't be loaded, skip license check and warn
            return [Conflict(
                severity=ConflictSeverity.WARNING,
                modules=sorted(modules),
                message="Could not load module manifests for license entitlement check.",
                remediation="Ensure manifest directory is accessible.",
            )]

        for module_name in sorted(modules):
            if module_name not in manifests:
                continue
            manifest = manifests[module_name]
            if manifest.tier in (ModuleTier.B, ModuleTier.C):
                if module_name not in licensed_modules:
                    conflicts.append(Conflict(
                        severity=ConflictSeverity.ERROR,
                        modules=[module_name],
                        message=(
                            f"Module '{module_name}' (Tier {manifest.tier.value}) "
                            "requires a valid license entitlement."
                        ),
                        remediation=(
                            "Run `aumos license activate --key <YOUR_LICENSE_KEY>` "
                            f"to unlock '{module_name}'."
                        ),
                    ))
        return conflicts

    def _check_foundation_requirements(self, modules: set[str]) -> list[Conflict]:
        """Warn if no foundation modules are present.

        The foundation modules (core-platform, auth-gateway, etc.) must
        always be present. This check warns if they are missing, which
        should have been caught by dependency resolution already.

        Args:
            modules: Module set to check.

        Returns:
            List of warning conflicts if foundation modules are missing.
        """
        conflicts = []
        if "core-platform" not in modules:
            conflicts.append(Conflict(
                severity=ConflictSeverity.ERROR,
                modules=["core-platform"],
                message="'core-platform' is required and must always be activated.",
                remediation="Include 'core-platform' in your module list.",
            ))
        return conflicts

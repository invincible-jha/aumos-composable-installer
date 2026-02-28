"""FastAPI router for the AumOS GUI installer backend.

Gap #11: GUI installer backend API — exposes REST endpoints that a web-based
installer UI can call to orchestrate AumOS installation without requiring
CLI access. The GUI installer is a single-page app that calls these APIs.

All endpoints are unauthenticated by design — the installer runs locally
on the operator's workstation and binds to localhost only.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from aumos_common.observability import get_logger

from aumos_composable_installer.resolver.dependency_graph import DependencyGraph, FOUNDATION_MODULES
from aumos_composable_installer.resolver.module_manifest import ManifestLoader
from aumos_composable_installer.settings import Settings

logger = get_logger(__name__)
installer_router = APIRouter(prefix="/api/installer", tags=["installer"])

_settings = Settings()
_loader = ManifestLoader(_settings.manifest_dir)
_graph = DependencyGraph(_loader)


# ─── Request / Response Schemas ───────────────────────────────────────────────


class ModuleInfo(BaseModel):
    """Information about a single AumOS module.

    Attributes:
        name: Module slug.
        display_name: Human-readable name.
        tier: License tier (A, B, or C).
        description: Short description.
        is_foundation: Whether this is a required foundation module.
        dependencies: List of module names this module depends on.
    """

    name: str
    display_name: str
    tier: str
    description: str
    is_foundation: bool
    dependencies: list[str] = Field(default_factory=list)


class InstallRequest(BaseModel):
    """Request body for triggering an installation via the GUI.

    Attributes:
        modules: Optional module names to activate (foundation always included).
        namespace: Kubernetes namespace.
        release_name: Helm release name.
        dry_run: If True, simulate without applying changes.
    """

    modules: list[str] = Field(default_factory=list)
    namespace: str = Field(default="aumos")
    release_name: str = Field(default="aumos")
    dry_run: bool = False


class InstallResponse(BaseModel):
    """Response from an installation request.

    Attributes:
        success: Whether the installation succeeded.
        modules_installed: List of installed module names.
        message: Human-readable status message.
    """

    success: bool
    modules_installed: list[str]
    message: str


class ResolutionResponse(BaseModel):
    """Dependency resolution result for a module selection.

    Attributes:
        install_order: Topologically sorted list of modules to install.
        total_count: Total modules including transitive dependencies.
        explicitly_requested: Modules explicitly requested by the user.
    """

    install_order: list[str]
    total_count: int
    explicitly_requested: list[str]


# ─── Endpoints ────────────────────────────────────────────────────────────────


@installer_router.get(
    "/modules",
    response_model=list[ModuleInfo],
    summary="List available modules",
    description="Returns all AumOS modules with metadata for the GUI module selector.",
)
async def list_modules() -> list[ModuleInfo]:
    """Return all available AumOS modules.

    Returns:
        List of ModuleInfo for all modules discovered in the manifest directory.
    """
    modules: list[ModuleInfo] = []
    foundation_set = set(FOUNDATION_MODULES)

    try:
        manifest_names = _loader.list_available()
    except Exception:
        manifest_names = list(foundation_set)

    for module_name in manifest_names:
        try:
            manifest = _loader.load(module_name)
            modules.append(
                ModuleInfo(
                    name=module_name,
                    display_name=getattr(manifest, "display_name", module_name),
                    tier=getattr(manifest, "tier", "A"),
                    description=getattr(manifest, "description", ""),
                    is_foundation=module_name in foundation_set,
                    dependencies=getattr(manifest, "dependencies", []),
                )
            )
        except Exception as exc:
            logger.warning("module_manifest_load_failed", module=module_name, error=str(exc))

    return modules


@installer_router.post(
    "/resolve",
    response_model=ResolutionResponse,
    summary="Resolve module dependencies",
    description="Returns the full dependency-resolved installation order for a module selection.",
)
async def resolve_modules(modules: list[str]) -> ResolutionResponse:
    """Resolve dependencies for a module selection.

    Args:
        modules: List of optional module names to include.

    Returns:
        ResolutionResponse with topologically sorted install order.
    """
    all_requested = list(FOUNDATION_MODULES) + [m for m in modules if m not in FOUNDATION_MODULES]

    try:
        resolution = _graph.resolve(all_requested)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Dependency resolution failed: {exc}",
        ) from exc

    return ResolutionResponse(
        install_order=resolution.install_order,
        total_count=resolution.total_count,
        explicitly_requested=list(resolution.explicitly_requested),
    )


@installer_router.post(
    "/install",
    response_model=InstallResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Trigger installation",
    description="Starts an AumOS installation with the selected modules. Returns immediately; poll /status for progress.",
)
async def trigger_install(request: InstallRequest) -> InstallResponse:
    """Trigger an AumOS installation via the GUI.

    Args:
        request: Installation parameters including module selection.

    Returns:
        InstallResponse with success status and installed module list.
    """
    all_requested = list(FOUNDATION_MODULES) + request.modules

    try:
        resolution = _graph.resolve(all_requested)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Dependency resolution failed: {exc}",
        ) from exc

    if request.dry_run:
        return InstallResponse(
            success=True,
            modules_installed=resolution.install_order,
            message=f"Dry run: would install {resolution.total_count} module(s)",
        )

    from aumos_composable_installer.deployer.helm_deployer import HelmDeployer

    deployer = HelmDeployer(
        loader=_loader,
        release_name=request.release_name,
        namespace=request.namespace,
        chart_repository=_settings.helm_chart_repository,
        timeout_seconds=_settings.helm_timeout_seconds,
    )
    result = deployer.install(resolution, dry_run=False)

    return InstallResponse(
        success=result.success,
        modules_installed=resolution.install_order if result.success else [],
        message="Installation complete." if result.success else f"Installation failed: {result.stderr}",
    )


@installer_router.get(
    "/status",
    summary="Get installation status",
    description="Returns the current state of the AumOS installation from the state file.",
)
async def get_status() -> dict[str, Any]:
    """Return the current installation state.

    Returns:
        Dict with installed modules, state file path, and readiness.
    """
    state_path = _settings.state_file_path
    if state_path.exists():
        import yaml

        raw = yaml.safe_load(state_path.read_text(encoding="utf-8")) or {}
        return {"state": raw, "state_file": str(state_path), "ready": True}

    return {"state": {}, "state_file": str(state_path), "ready": False}

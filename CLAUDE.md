# CLAUDE.md — AumOS Composable Installer

## Project Overview

AumOS Enterprise is a composable enterprise AI platform with 9 products + 2 services
across 75 repositories. This repo (`aumos-composable-installer`) is part of **Tier 1: Foundation Infrastructure**:
One-installation, selective-activation platform installer for the entire AumOS ecosystem.

**Release Tier:** A (Fully Open)
**Product Mapping:** Foundation — Platform Installer
**Phase:** 0A

## Repo Purpose

The composable installer provides a single CLI (`aumos`) that deploys the entire AumOS platform
with selective module activation. Customers install once and activate only the modules they are
licensed and ready for, using Helm umbrella charts, ArgoCD ApplicationSets, or Docker Compose
for local development. The installer manages dependency resolution, license validation, and
post-install health verification.

## Architecture Position

```
aumos-composable-installer (CLI / orchestration layer)
    ├── → aumos-platform-core   (Helm sub-chart, always activated)
    ├── → aumos-auth-gateway    (Helm sub-chart, activated with core)
    ├── → aumos-event-bus       (Helm sub-chart, activated with core)
    ├── → aumos-data-layer      (Helm sub-chart, activated with core)
    ├── → aumos-observability   (Helm sub-chart, activated with core)
    ├── → aumos-secrets-vault   (Helm sub-chart, activated with core)
    └── → optional modules      (data-factory, governance, security, mlops, marketplace)
```

**Upstream dependencies (this repo IMPORTS from):**
- `aumos-common` — config, errors, observability (logging)
- Module manifests from each AumOS repo's `module-manifests/` directory

**Downstream dependents (other repos IMPORT from this):**
- All AumOS repos — this installer deploys them

## Tech Stack (DO NOT DEVIATE)

| Component | Version | Purpose |
|-----------|---------|---------|
| Python | 3.11+ | Runtime |
| Typer | 0.12+ | CLI framework (modern Click alternative) |
| Rich | 13.7+ | Terminal output formatting |
| PyYAML | 6.0+ | Module manifest parsing |
| Pydantic | 2.6+ | Data validation and settings |
| PyJWT | 2.8+ | License token validation |
| httpx | 0.27+ | Health check HTTP client |
| Jinja2 | 3.1+ | Docker Compose template generation |
| structlog | 24.1+ | Structured JSON logging |
| ruff | 0.3+ | Linting and formatting |
| mypy | 1.8+ | Type checking |

## Coding Standards

### ABSOLUTE RULES

1. **Import aumos-common, never reimplement.** Use `aumos_common.config.AumOSSettings`,
   `aumos_common.errors`, `aumos_common.observability.get_logger`.

2. **Type hints on EVERY function.** No exceptions.

3. **Pydantic models for ALL manifest schemas.** Never use raw dicts to represent manifests.

4. **CLI commands are thin.** All business logic lives in resolver/, deployer/, license/, health/.

5. **Structured logging via structlog.** Never use print() except for Rich console output.

6. **Dependency graph must detect cycles.** The topological sort MUST raise ConflictError on
   circular dependencies before attempting deployment.

7. **License validation is blocking.** Never deploy a Tier B or C module without a valid JWT
   entitlement for that module.

### Style Rules

- Max line length: **120 characters**
- Import order: stdlib → third-party → aumos-common → local
- Linter: `ruff` (select E, W, F, I, N, UP, ANN, B, A, COM, C4, PT, RUF)
- Type checker: `mypy` strict mode
- Formatter: `ruff format`

### File Structure Convention

```
src/aumos_composable_installer/
├── __init__.py
├── main.py                   # Typer CLI entry point
├── settings.py               # Extends AumOSSettings
├── commands/                 # Thin CLI command handlers
│   ├── install.py            # aumos install
│   ├── activate.py           # aumos activate
│   ├── deactivate.py         # aumos deactivate
│   ├── status.py             # aumos status
│   ├── upgrade.py            # aumos upgrade
│   └── diagnose.py           # aumos diagnose
├── resolver/                 # Dependency resolution engine
│   ├── dependency_graph.py   # DAG + topological sort
│   ├── conflict_detector.py  # Conflict detection
│   └── module_manifest.py    # Manifest schema + loader
├── deployer/                 # Deployment backends
│   ├── helm_deployer.py      # Helm chart orchestration
│   ├── argocd_deployer.py    # ArgoCD ApplicationSet
│   └── docker_compose_deployer.py  # Local dev mode
├── license/                  # License management
│   ├── validator.py          # JWT validation
│   └── key_manager.py        # Key storage/retrieval
└── health/                   # Health verification
    └── checker.py            # Post-install health checks
```

## CLI Conventions

- All commands under `aumos` app (Typer)
- Use Rich for formatted terminal output (tables, progress bars, panels)
- Exit code 0 = success, 1 = user error, 2 = system error
- `--dry-run` flag available on all mutating commands
- `--output json` flag for machine-readable output

## Module Manifest Schema

Module manifests are YAML files following the schema defined in `resolver/module_manifest.py`.
Manifests live in `module-manifests/` and are loaded by the resolver at runtime.

## Repo-Specific Context

- This is NOT a FastAPI service — it is a CLI tool and Helm/ArgoCD orchestration layer
- No database is used directly — state is tracked via Kubernetes ConfigMaps or a local state file
- The dependency graph is encoded in `resolver/dependency_graph.py` and reflects the AumOS
  module dependency tree exactly as specified in the implementation plan
- License tiers: A=always-on (free), B=commercial key required, C=proprietary (contact sales)
- Helm umbrella chart uses `condition:` fields in Chart.yaml to conditionally include sub-charts
- Docker Compose mode is for local development only — not production

## What Claude Code Should NOT Do

1. **Do NOT use print().** Use Rich console or `get_logger(__name__)`.
2. **Do NOT skip type hints.** Every function signature must be typed.
3. **Do NOT deploy without dependency resolution.** Always run the resolver first.
4. **Do NOT deploy Tier B/C modules without license validation.**
5. **Do NOT hardcode cloud provider specifics.** Use abstraction layers.
6. **Do NOT put logic in CLI commands.** Delegate to resolver/deployer/license/health.
7. **Do NOT bypass the conflict detector.** Always check for conflicts before deploying.

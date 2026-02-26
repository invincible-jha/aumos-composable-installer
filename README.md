# aumos-composable-installer

**AumOS Enterprise Platform Installer** — one-installation, selective-activation deployment CLI for the entire AumOS ecosystem.

Install the platform once and selectively activate modules based on your license entitlements and infrastructure needs. Dependency resolution, license validation, and post-install health verification are all handled automatically.

---

## Architecture

```
aumos-composable-installer (CLI / orchestration layer)
    │
    ├── resolver/          DAG-based dependency resolver with conflict detection
    ├── deployer/          Helm umbrella chart orchestration (+ ArgoCD, Docker Compose)
    ├── license/           JWT license validation (offline, RSA RS256)
    └── health/            Async post-install health verification
          │
          ▼
aumos-platform-core        (Helm sub-chart, always activated — Tier A)
aumos-auth-gateway         (Helm sub-chart, always activated — Tier A)
aumos-event-bus            (Helm sub-chart, always activated — Tier A)
aumos-data-layer           (Helm sub-chart, always activated — Tier A)
aumos-observability        (Helm sub-chart, always activated — Tier A)
aumos-secrets-vault        (Helm sub-chart, always activated — Tier A)
aumos-data-factory         (optional — Tier B, requires license key)
aumos-governance           (optional — Tier B, requires license key)
aumos-security             (optional — Tier B, requires license key)
aumos-mlops                (optional — Tier B, requires license key)
aumos-marketplace          (optional — Tier B, requires license key)
```

### Module Tiers

| Tier | Description | License Required |
|------|-------------|-----------------|
| A    | Foundation modules — always activated with every install | No |
| B    | Commercial optional modules | JWT license key |
| C    | Proprietary enterprise modules | Enterprise agreement |

---

## Quick Start

### Prerequisites

- Python 3.11+
- `helm` v3.14+ on your PATH
- Kubernetes cluster access (`~/.kube/config` or `KUBECONFIG`)
- AumOS license key (for Tier B/C modules)

### Install the CLI

```bash
pip install aumos-composable-installer
```

Or from source:

```bash
git clone https://github.com/MuVeraAI/aumos-composable-installer
cd aumos-composable-installer
pip install -e ".[dev]"
```

### Install the platform

```bash
# Install with foundation modules only (Tier A — no license required)
aumos install run

# Install with optional modules
aumos install run --modules data-factory,mlops

# Dry run to preview changes
aumos install run --modules data-factory --dry-run
```

### Activate a module after initial install

```bash
aumos activate run data-factory
```

### Deactivate a module

```bash
aumos deactivate run data-factory
```

### Check platform status

```bash
aumos status run

# With live health checks
aumos status run --health

# JSON output for scripting
aumos status run --output json
```

### Upgrade the platform

```bash
# Upgrade to latest chart version
aumos upgrade run

# Upgrade to a specific version
aumos upgrade run --chart-version 1.2.0
```

### Run diagnostics

```bash
aumos diagnose run
```

---

## CLI Reference

| Command | Description |
|---------|-------------|
| `aumos install run` | Install the platform with selected modules |
| `aumos activate run <module>` | Activate a module on an existing installation |
| `aumos deactivate run <module>` | Deactivate a module |
| `aumos status run` | Show current installation status |
| `aumos upgrade run` | Upgrade to a newer chart version |
| `aumos diagnose run` | Run comprehensive diagnostics |
| `aumos --version` | Print version and exit |

### Common flags

| Flag | Description |
|------|-------------|
| `--dry-run` | Simulate without applying changes (all mutating commands) |
| `--namespace <ns>` | Kubernetes namespace (default: `aumos`) |
| `--release <name>` | Helm release name (default: `aumos`) |
| `--output json` | Machine-readable JSON output |
| `--modules <list>` | Comma-separated module list |

---

## Configuration

All settings can be set via environment variables. Create a `.env` file from `.env.example`:

```bash
cp .env.example .env
```

| Variable | Default | Description |
|----------|---------|-------------|
| `AUMOS_SERVICE_NAME` | `aumos-composable-installer` | Service identifier |
| `AUMOS_ENVIRONMENT` | `development` | Runtime environment |
| `AUMOS_DEBUG` | `false` | Enable debug logging |
| `AUMOS_INSTALLER_HELM_CHART_REPOSITORY` | `oci://registry.aumos.ai/charts` | Helm chart OCI registry |
| `AUMOS_INSTALLER_HELM_NAMESPACE` | `aumos` | Kubernetes namespace |
| `AUMOS_INSTALLER_HELM_TIMEOUT_SECONDS` | `600` | Helm operation timeout |
| `AUMOS_INSTALLER_ARGOCD_SERVER` | `` | ArgoCD server address |
| `AUMOS_INSTALLER_LICENSE_KEY_PATH` | `~/.aumos/license.key` | License key file path |
| `AUMOS_INSTALLER_STATE_FILE_PATH` | `~/.aumos/installer-state.yaml` | State tracking file |
| `AUMOS_INSTALLER_HEALTH_CHECK_TIMEOUT_SECONDS` | `300` | Health check timeout |

---

## Development

### Setup

```bash
git clone https://github.com/MuVeraAI/aumos-composable-installer
cd aumos-composable-installer
make install
```

### Run tests

```bash
make test

# Quick run (stop on first failure)
make test-quick
```

### Lint and format

```bash
make lint
make format
```

### Type check

```bash
make typecheck
```

### Docker

```bash
# Build the image
make docker-build

# Run with local kubeconfig
docker-compose -f docker-compose.dev.yml run installer --help
```

---

## License

Apache 2.0 — see [LICENSE](LICENSE).

Part of the [AumOS Enterprise Platform](https://aumos.io).

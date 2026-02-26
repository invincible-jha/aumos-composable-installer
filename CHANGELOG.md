# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2025-01-01

### Added

- Initial CLI implementation with Typer (`aumos install`, `activate`, `deactivate`, `status`, `upgrade`, `diagnose`)
- DAG-based dependency resolver with topological sort (Kahn's algorithm) and cycle detection
- Module manifest schema with Pydantic v2 (`ModuleManifest`, `ManifestLoader`)
- Conflict detector for incompatible module combinations and license tier enforcement
- Helm deployer for module installation via `helm upgrade --install` with conditional sub-chart activation
- License validator using offline RSA RS256 JWT verification (`LicenseValidator`, `KeyManager`)
- Async post-install health checker polling module health endpoints (`HealthChecker`)
- Settings extending `AumOSSettings` with `AUMOS_INSTALLER_` prefix
- Rich terminal output with tables, panels, and progress indicators
- Structured JSON logging via structlog throughout all modules
- Multi-stage Docker image (Python 3.11-slim, non-root `aumos` user)
- `--dry-run` flag on all mutating commands
- `--output json` flag for machine-readable output on status and diagnose commands
- Apache 2.0 license

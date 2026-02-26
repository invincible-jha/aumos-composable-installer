# Contributing to aumos-composable-installer

Thank you for your interest in contributing to the AumOS Enterprise Platform Installer.

---

## Code of Conduct

All contributors are expected to uphold our [Code of Conduct](CODE_OF_CONDUCT.md). Be respectful,
constructive, and collaborative.

---

## Development Standards

This project follows the AumOS Engineering Standards defined in `CLAUDE.md`. Key points:

- **Python 3.11+** with type hints on every function signature (no exceptions)
- **Pydantic v2** for all data validation — never use raw dicts for structured data
- **Ruff** for linting and formatting (`make lint`, `make format`)
- **mypy strict mode** for type checking (`make typecheck`)
- **Max line length: 120 characters**
- **Google-style docstrings** on all public functions and classes
- **No print()** — use Rich console for user output, structlog for logging
- **CLI commands are thin** — all business logic lives in `resolver/`, `deployer/`, `license/`, `health/`

---

## Commit Convention

We use [Conventional Commits](https://www.conventionalcommits.org/):

```
feat:      New feature or command
fix:       Bug fix
refactor:  Code restructuring without behavior change
docs:      Documentation changes
test:      Test additions or modifications
chore:     Build, CI, dependency updates
```

Commit messages should explain **why**, not just what.

---

## License Restrictions

**Do NOT introduce AGPL or GPL licensed dependencies.** This is a commercial product and
viral copyleft licenses are incompatible with our distribution model. All new dependencies
must be Apache 2.0, MIT, BSD, or equivalent permissive licenses.

---

## Pull Request Process

1. Fork the repository and create a feature branch from `main`:
   ```bash
   git checkout -b feature/your-feature-name
   ```

2. Make your changes following the standards above.

3. Run the full check suite:
   ```bash
   make all
   ```

4. Push your branch and open a Pull Request against `main`.

5. PR title must follow conventional commit format.

6. At least one maintainer review is required before merge.

7. PRs are squash-merged to keep the history clean.

---

## Adding a New CLI Command

1. Create `src/aumos_composable_installer/commands/<command>.py`
2. Define a `typer.Typer()` app and register sub-commands
3. Register in `main.py` via `app.add_typer(...)`
4. Export from `commands/__init__.py`
5. Keep commands thin — delegate logic to `resolver/`, `deployer/`, etc.

---

## Reporting Bugs

Please open a GitHub Issue with:
- Steps to reproduce
- Expected vs actual behavior
- `aumos diagnose run --output json` output
- Python version and OS

---

## Security Vulnerabilities

See [SECURITY.md](SECURITY.md) for responsible disclosure policy.

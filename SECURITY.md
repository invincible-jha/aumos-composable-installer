# Security Policy

## Reporting a Vulnerability

The AumOS team takes security vulnerabilities seriously. We appreciate responsible disclosure.

**Do NOT open a public GitHub issue for security vulnerabilities.**

### How to Report

Email: **security@aumos.io**

Please include:
- Description of the vulnerability
- Steps to reproduce
- Potential impact assessment
- Any proposed mitigations (optional)

### Response Timeline

| Milestone | Target |
|-----------|--------|
| Acknowledgment | 48 hours |
| Initial assessment | 5 business days |
| Status update | 10 business days |
| Fix or mitigation | Depends on severity (see below) |

### Severity and Response

| Severity | Definition | Fix Target |
|----------|------------|------------|
| Critical | Remote code execution, license bypass, privilege escalation | 72 hours |
| High | Data exposure, authentication bypass | 7 days |
| Medium | Limited impact vulnerabilities | 30 days |
| Low | Informational, best practice issues | Next release |

---

## Scope

### In scope

- `aumos-composable-installer` package and CLI
- License JWT validation logic (`src/aumos_composable_installer/license/`)
- Dependency resolution and conflict detection
- Helm deployer command injection risks
- Docker image security

### Out of scope

- Third-party dependencies (report upstream)
- Kubernetes cluster security (report to cluster vendor)
- Social engineering attacks
- Physical access attacks

---

## Supported Versions

| Version | Supported |
|---------|-----------|
| 0.1.x   | Yes       |

---

## Disclosure Policy

We follow coordinated disclosure. Once a fix is available, we will:

1. Release a patched version
2. Publish a security advisory on GitHub
3. Credit the reporter (unless anonymity is requested)

We ask that you do not disclose the vulnerability publicly until we have released a fix
or 90 days have elapsed from your initial report, whichever comes first.

---

## Security Design Notes

- License tokens are RS256 JWT validated offline using a bundled public key
- License key file is stored with `0o600` permissions (`~/.aumos/license.key`)
- Helm commands are constructed as argument lists (no shell=True) to prevent injection
- No secrets are logged — structlog redacts sensitive fields

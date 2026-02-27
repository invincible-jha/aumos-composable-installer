"""Configuration manager adapter for the AumOS Composable Installer.

Manages Helm values and Kubernetes manifest configuration: template rendering,
secret injection from aumos-secrets-vault, environment-specific overrides,
schema validation, diff generation, version tracking, and config export/import.
"""

from __future__ import annotations

import copy
import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import yaml

from aumos_common.observability import get_logger

logger = get_logger(__name__)


@dataclass
class ConfigVersion:
    """A versioned configuration snapshot.

    Attributes:
        version_id: Unique version identifier.
        service_name: Service this config belongs to.
        config_data: Full rendered configuration dict.
        checksum: SHA-256 of the serialized config.
        created_at: UTC ISO timestamp.
        environment: Target environment (dev, staging, prod).
        applied: Whether this version was actually deployed.
        description: Human-readable change description.
    """

    version_id: str
    service_name: str
    config_data: dict[str, Any]
    checksum: str
    created_at: str
    environment: str
    applied: bool = False
    description: str = ""


@dataclass
class ConfigDiff:
    """Diff between two configuration versions.

    Attributes:
        from_version_id: Source version identifier.
        to_version_id: Target version identifier.
        service_name: Affected service.
        added: Paths present in to_version but not from_version.
        removed: Paths present in from_version but not to_version.
        modified: Paths present in both with different values.
    """

    from_version_id: str
    to_version_id: str
    service_name: str
    added: list[str]
    removed: list[str]
    modified: list[tuple[str, Any, Any]]   # (path, from_value, to_value)

    @property
    def has_changes(self) -> bool:
        """True if any differences exist between the versions."""
        return bool(self.added or self.removed or self.modified)

    @property
    def change_count(self) -> int:
        """Total number of individual changes."""
        return len(self.added) + len(self.removed) + len(self.modified)


class InstallerConfigManager:
    """Installation configuration management for AumOS services.

    Provides:
    - Helm values template rendering with environment-specific overrides
    - Secret injection from the aumos-secrets-vault REST API
    - Configuration validation against a JSON schema
    - Diff generation between config versions
    - Version history tracking per service
    - Config export/import in YAML and JSON formats

    Args:
        base_config_dir: Directory containing base config templates.
        secrets_vault_url: AumOS secrets vault REST API URL.
        secrets_vault_token: Authentication token for the secrets vault.
        max_versions_per_service: Maximum config history to retain.
        secrets_timeout_seconds: HTTP timeout for vault API calls.
    """

    def __init__(
        self,
        base_config_dir: Path | None = None,
        secrets_vault_url: str | None = None,
        secrets_vault_token: str | None = None,
        max_versions_per_service: int = 10,
        secrets_timeout_seconds: int = 10,
    ) -> None:
        self._base_config_dir = base_config_dir or Path(__file__).parent.parent / "config-templates"
        self._secrets_vault_url = secrets_vault_url
        self._secrets_vault_token = secrets_vault_token
        self._max_versions = max_versions_per_service

        self._config_history: dict[str, list[ConfigVersion]] = {}
        self._current_versions: dict[str, str] = {}   # service_name -> version_id

        self._vault_client: httpx.AsyncClient | None = None
        if secrets_vault_url:
            headers = {}
            if secrets_vault_token:
                headers["Authorization"] = f"Bearer {secrets_vault_token}"
            self._vault_client = httpx.AsyncClient(
                base_url=secrets_vault_url.rstrip("/"),
                timeout=httpx.Timeout(secrets_timeout_seconds),
                headers=headers,
            )

    # ------------------------------------------------------------------
    # Template rendering
    # ------------------------------------------------------------------

    async def render_helm_values(
        self,
        service_name: str,
        environment: str,
        overrides: dict[str, Any] | None = None,
        inject_secrets: bool = True,
    ) -> dict[str, Any]:
        """Render the effective Helm values for a service and environment.

        Loads the base template, applies environment overrides, and optionally
        injects secrets from the vault.

        Args:
            service_name: Target service identifier.
            environment: Target environment (dev, staging, prod).
            overrides: Additional key-value overrides (highest priority).
            inject_secrets: Whether to resolve secret references from vault.

        Returns:
            Merged and rendered Helm values dict.
        """
        # Load base template
        base = await self._load_base_template(service_name)

        # Apply environment-specific overrides from template file if present
        env_overrides = await self._load_env_overrides(service_name, environment)
        merged = self._deep_merge(base, env_overrides)

        # Apply caller-supplied overrides
        if overrides:
            merged = self._deep_merge(merged, overrides)

        # Inject secrets
        if inject_secrets and self._vault_client:
            merged = await self._inject_secrets(merged)

        logger.info(
            "Helm values rendered",
            service=service_name,
            environment=environment,
            keys=len(merged),
        )
        return merged

    async def render_k8s_manifest(
        self,
        template_path: str,
        variables: dict[str, Any],
    ) -> dict[str, Any]:
        """Render a Kubernetes manifest template with variable substitution.

        Args:
            template_path: Path to a YAML manifest template (relative to base_config_dir).
            variables: Variable substitution dict. Variables are referenced as
                       {variable_name} in the template.

        Returns:
            Rendered manifest as a dict.
        """
        full_path = self._base_config_dir / template_path
        if not full_path.exists():
            raise FileNotFoundError(f"Template not found: {full_path}")

        template_text = full_path.read_text()

        # Simple variable substitution
        for key, value in variables.items():
            template_text = template_text.replace(f"{{{key}}}", str(value))

        manifest: dict[str, Any] = yaml.safe_load(template_text)
        logger.debug("K8s manifest rendered", template=template_path)
        return manifest

    # ------------------------------------------------------------------
    # Secret injection
    # ------------------------------------------------------------------

    async def inject_secret(self, config: dict[str, Any], secret_path: str, config_key: str) -> dict[str, Any]:
        """Fetch a secret from the vault and inject it into a config dict.

        Args:
            config: Config dict to inject into (modified in-place copy returned).
            secret_path: Secret path in the vault (e.g. "aumos/database/password").
            config_key: Dot-notation key path in config to set (e.g. "db.password").

        Returns:
            New config dict with secret injected.

        Raises:
            RuntimeError: If vault client is not configured.
        """
        if not self._vault_client:
            raise RuntimeError("Secrets vault client is not configured")

        secret_value = await self._fetch_secret(secret_path)
        result = copy.deepcopy(config)
        self._set_nested(result, config_key.split("."), secret_value)
        logger.info("Secret injected", config_key=config_key, secret_path=secret_path)
        return result

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def validate_config(self, config: dict[str, Any], schema: dict[str, Any]) -> list[str]:
        """Validate a config dict against a JSON schema.

        Args:
            config: Configuration dict to validate.
            schema: JSON Schema dict defining required fields and types.

        Returns:
            List of validation error strings (empty = valid).
        """
        errors: list[str] = []
        required_fields: list[str] = schema.get("required", [])

        for field_path in required_fields:
            parts = field_path.split(".")
            current: Any = config
            for part in parts:
                if not isinstance(current, dict) or part not in current:
                    errors.append(f"Required field missing: {field_path}")
                    break
                current = current[part]

        properties: dict[str, Any] = schema.get("properties", {})
        for key, type_spec in properties.items():
            if key in config:
                expected_type = type_spec.get("type")
                value = config[key]
                if not self._check_type(value, expected_type):
                    errors.append(f"Type mismatch at '{key}': expected {expected_type}, got {type(value).__name__}")

        if errors:
            logger.warning("Config validation failed", errors=errors)
        return errors

    # ------------------------------------------------------------------
    # Version tracking
    # ------------------------------------------------------------------

    def save_version(
        self,
        service_name: str,
        config_data: dict[str, Any],
        environment: str,
        description: str = "",
    ) -> ConfigVersion:
        """Persist a configuration version for a service.

        Args:
            service_name: Service the config belongs to.
            config_data: Effective configuration dict.
            environment: Target environment.
            description: Human-readable description of changes.

        Returns:
            ConfigVersion with assigned version_id and checksum.
        """
        checksum = self._compute_checksum(config_data)
        version = ConfigVersion(
            version_id=str(uuid.uuid4()),
            service_name=service_name,
            config_data=copy.deepcopy(config_data),
            checksum=checksum,
            created_at=datetime.now(timezone.utc).isoformat(),
            environment=environment,
            description=description,
        )

        history = self._config_history.setdefault(service_name, [])
        history.append(version)

        # Enforce retention limit
        if len(history) > self._max_versions:
            history.pop(0)

        self._current_versions[service_name] = version.version_id

        logger.info(
            "Config version saved",
            service=service_name,
            version_id=version.version_id,
            checksum=checksum[:12],
        )
        return version

    def mark_applied(self, version_id: str) -> bool:
        """Mark a config version as having been applied to the cluster.

        Args:
            version_id: Target version to mark.

        Returns:
            True if found and marked, False if not found.
        """
        for versions in self._config_history.values():
            for version in versions:
                if version.version_id == version_id:
                    version.applied = True
                    return True
        return False

    def get_version_history(self, service_name: str) -> list[ConfigVersion]:
        """Retrieve all retained config versions for a service (oldest first).

        Args:
            service_name: Target service.

        Returns:
            List of ConfigVersion objects.
        """
        return list(self._config_history.get(service_name, []))

    def get_current_version(self, service_name: str) -> ConfigVersion | None:
        """Retrieve the most recently saved version for a service.

        Args:
            service_name: Target service.

        Returns:
            Most recent ConfigVersion or None.
        """
        version_id = self._current_versions.get(service_name)
        if not version_id:
            return None
        for version in reversed(self._config_history.get(service_name, [])):
            if version.version_id == version_id:
                return version
        return None

    # ------------------------------------------------------------------
    # Diff generation
    # ------------------------------------------------------------------

    def diff_versions(self, service_name: str, from_version_id: str, to_version_id: str) -> ConfigDiff:
        """Generate a diff between two config versions.

        Args:
            service_name: Target service.
            from_version_id: Source version.
            to_version_id: Target version.

        Returns:
            ConfigDiff with added, removed, and modified paths.

        Raises:
            ValueError: If either version is not found.
        """
        history = self._config_history.get(service_name, [])
        from_ver = next((v for v in history if v.version_id == from_version_id), None)
        to_ver = next((v for v in history if v.version_id == to_version_id), None)

        if not from_ver or not to_ver:
            raise ValueError(f"Version(s) not found: from={from_version_id}, to={to_version_id}")

        added, removed, modified = self._compute_diff(from_ver.config_data, to_ver.config_data)
        return ConfigDiff(
            from_version_id=from_version_id,
            to_version_id=to_version_id,
            service_name=service_name,
            added=added,
            removed=removed,
            modified=modified,
        )

    # ------------------------------------------------------------------
    # Export / Import
    # ------------------------------------------------------------------

    def export_config(self, service_name: str, output_format: str = "yaml") -> str:
        """Export the current config for a service to a serialized string.

        Args:
            service_name: Target service.
            output_format: "yaml" or "json".

        Returns:
            Serialized config string.

        Raises:
            ValueError: If no current version exists or format is unsupported.
        """
        current = self.get_current_version(service_name)
        if not current:
            raise ValueError(f"No current config version for service '{service_name}'")

        if output_format == "yaml":
            return yaml.dump(current.config_data, default_flow_style=False)
        elif output_format == "json":
            return json.dumps(current.config_data, indent=2)
        else:
            raise ValueError(f"Unsupported export format '{output_format}'. Use 'yaml' or 'json'.")

    def import_config(
        self,
        service_name: str,
        config_text: str,
        input_format: str = "yaml",
        environment: str = "prod",
        description: str = "imported",
    ) -> ConfigVersion:
        """Import a config from a serialized string and save as a new version.

        Args:
            service_name: Target service.
            config_text: Serialized config string.
            input_format: "yaml" or "json".
            environment: Target environment for this config.
            description: Description for the new version.

        Returns:
            Saved ConfigVersion.

        Raises:
            ValueError: If format is unsupported or parsing fails.
        """
        if input_format == "yaml":
            config_data: dict[str, Any] = yaml.safe_load(config_text) or {}
        elif input_format == "json":
            config_data = json.loads(config_text)
        else:
            raise ValueError(f"Unsupported import format '{input_format}'. Use 'yaml' or 'json'.")

        return self.save_version(service_name, config_data, environment, description)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _load_base_template(self, service_name: str) -> dict[str, Any]:
        """Load the base Helm values template for a service.

        Args:
            service_name: Service to load template for.

        Returns:
            Parsed template dict (empty dict if template not found).
        """
        template_path = self._base_config_dir / f"{service_name}-values.yaml"
        if template_path.exists():
            parsed: dict[str, Any] = yaml.safe_load(template_path.read_text()) or {}
            return parsed
        logger.debug("No base template found, using empty config", service=service_name)
        return {}

    async def _load_env_overrides(self, service_name: str, environment: str) -> dict[str, Any]:
        """Load environment-specific overrides for a service.

        Args:
            service_name: Target service.
            environment: Environment name (dev, staging, prod).

        Returns:
            Parsed overrides dict (empty dict if file not found).
        """
        override_path = self._base_config_dir / f"{service_name}-values-{environment}.yaml"
        if override_path.exists():
            parsed: dict[str, Any] = yaml.safe_load(override_path.read_text()) or {}
            return parsed
        return {}

    async def _inject_secrets(self, config: dict[str, Any]) -> dict[str, Any]:
        """Resolve secret references in a config by fetching from vault.

        Secret references are identified by values matching the pattern
        "vault://secret/path". They are replaced with the fetched value.

        Args:
            config: Config dict potentially containing vault references.

        Returns:
            Config dict with vault references replaced by actual secret values.
        """
        return await self._resolve_vault_refs(copy.deepcopy(config))

    async def _resolve_vault_refs(self, obj: Any) -> Any:
        """Recursively resolve vault:// references in a config structure.

        Args:
            obj: Object to traverse (dict, list, or scalar).

        Returns:
            Object with vault:// references replaced by fetched values.
        """
        if isinstance(obj, dict):
            resolved: dict[str, Any] = {}
            for key, value in obj.items():
                resolved[key] = await self._resolve_vault_refs(value)
            return resolved
        elif isinstance(obj, list):
            return [await self._resolve_vault_refs(item) for item in obj]
        elif isinstance(obj, str) and obj.startswith("vault://"):
            secret_path = obj[len("vault://"):]
            try:
                return await self._fetch_secret(secret_path)
            except Exception as exc:
                logger.warning("Failed to fetch vault secret, leaving reference intact", path=secret_path, error=str(exc))
                return obj
        return obj

    async def _fetch_secret(self, secret_path: str) -> str:
        """Fetch a secret value from the AumOS secrets vault.

        Args:
            secret_path: Vault secret path (e.g. "aumos/db/password").

        Returns:
            Secret value string.

        Raises:
            RuntimeError: If vault is unreachable or secret not found.
        """
        if not self._vault_client:
            raise RuntimeError("Vault client not configured")

        try:
            response = await self._vault_client.get(f"/api/v1/secrets/{secret_path}")
        except httpx.ConnectError as exc:
            raise RuntimeError(f"Vault unreachable: {exc}") from exc

        if response.status_code == 404:
            raise RuntimeError(f"Secret not found: {secret_path}")
        if response.status_code != 200:
            raise RuntimeError(f"Vault returned {response.status_code} for {secret_path}")

        data: dict[str, Any] = response.json()
        value: str = data.get("value", "")
        return value

    @staticmethod
    def _deep_merge(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
        """Recursively merge overrides into base dict.

        Args:
            base: Base configuration dict.
            overrides: Override values (take priority).

        Returns:
            New merged dict without modifying inputs.
        """
        result = copy.deepcopy(base)
        for key, value in overrides.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = InstallerConfigManager._deep_merge(result[key], value)
            else:
                result[key] = copy.deepcopy(value)
        return result

    @staticmethod
    def _compute_checksum(config: dict[str, Any]) -> str:
        """Compute a SHA-256 checksum of a config dict.

        Args:
            config: Config dict to hash.

        Returns:
            Hex-encoded SHA-256 string.
        """
        serialized = json.dumps(config, sort_keys=True).encode("utf-8")
        return hashlib.sha256(serialized).hexdigest()

    @staticmethod
    def _set_nested(obj: dict[str, Any], path: list[str], value: Any) -> None:
        """Set a value in a nested dict by path.

        Args:
            obj: Dict to modify in-place.
            path: List of keys forming the path.
            value: Value to set.
        """
        for key in path[:-1]:
            obj = obj.setdefault(key, {})
        obj[path[-1]] = value

    @staticmethod
    def _check_type(value: Any, expected_type: str | None) -> bool:
        """Check if a value matches an expected JSON schema type.

        Args:
            value: Value to type-check.
            expected_type: JSON schema type string (string, integer, boolean, etc.).

        Returns:
            True if type matches.
        """
        type_map: dict[str, type] = {
            "string": str,
            "integer": int,
            "number": (int, float),  # type: ignore[dict-item]
            "boolean": bool,
            "array": list,
            "object": dict,
        }
        expected = type_map.get(expected_type or "")
        if not expected:
            return True  # Unknown type — pass through
        return isinstance(value, expected)

    def _compute_diff(
        self,
        from_config: dict[str, Any],
        to_config: dict[str, Any],
        prefix: str = "",
    ) -> tuple[list[str], list[str], list[tuple[str, Any, Any]]]:
        """Recursively compute differences between two config dicts.

        Args:
            from_config: Source configuration.
            to_config: Target configuration.
            prefix: Current key path prefix for recursion.

        Returns:
            Tuple of (added paths, removed paths, modified (path, from, to) tuples).
        """
        added: list[str] = []
        removed: list[str] = []
        modified: list[tuple[str, Any, Any]] = []

        all_keys = set(from_config) | set(to_config)
        for key in sorted(all_keys):
            full_key = f"{prefix}.{key}".lstrip(".")
            in_from = key in from_config
            in_to = key in to_config

            if not in_from:
                added.append(full_key)
            elif not in_to:
                removed.append(full_key)
            else:
                from_val = from_config[key]
                to_val = to_config[key]
                if isinstance(from_val, dict) and isinstance(to_val, dict):
                    sub_added, sub_removed, sub_modified = self._compute_diff(from_val, to_val, full_key)
                    added.extend(sub_added)
                    removed.extend(sub_removed)
                    modified.extend(sub_modified)
                elif from_val != to_val:
                    modified.append((full_key, from_val, to_val))

        return added, removed, modified

    async def close(self) -> None:
        """Release the vault HTTP client."""
        if self._vault_client:
            await self._vault_client.aclose()

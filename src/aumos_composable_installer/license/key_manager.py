"""License key storage and retrieval for the AumOS installer.

Manages the lifecycle of the license JWT token on disk:
- Storing a new key
- Loading the stored key
- Revoking (removing) the key
- Displaying key info without revealing the full token
"""

from __future__ import annotations

from pathlib import Path

from aumos_common.observability import get_logger

from aumos_composable_installer.license.validator import LicenseInfo, LicenseValidator

logger = get_logger(__name__)


class KeyManager:
    """Manages AumOS license key persistence on the local filesystem.

    The license key is stored in a user-writable directory (~/.aumos/).
    Permissions are set to 0o600 (owner read/write only) to prevent
    unauthorized access.
    """

    def __init__(self, key_path: Path, validator: LicenseValidator) -> None:
        """Initialize the key manager.

        Args:
            key_path: Path where the license JWT is stored.
            validator: LicenseValidator to use for token validation.
        """
        self._key_path = key_path
        self._validator = validator

    def store(self, token: str) -> LicenseInfo:
        """Validate and store a license token to disk.

        Validates the token before storing to prevent storing invalid keys.

        Args:
            token: Raw JWT license token string.

        Returns:
            Decoded LicenseInfo from the validated token.

        Raises:
            ValueError: If the token is invalid.
            OSError: If the key file cannot be written.
        """
        # Validate before storing
        license_info = self._validator.validate(token)

        # Ensure directory exists with secure permissions
        self._key_path.parent.mkdir(parents=True, exist_ok=True)

        # Write with restrictive permissions
        self._key_path.write_text(token.strip(), encoding="utf-8")

        try:
            import stat
            self._key_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except (AttributeError, OSError):
            # chmod may not work on all platforms (e.g. Windows)
            pass

        logger.info(
            "License key stored",
            key_path=str(self._key_path),
            customer_id=license_info.customer_id,
            tier=license_info.tier,
        )
        return license_info

    def load(self) -> LicenseInfo:
        """Load and validate the stored license token.

        Returns:
            Decoded and validated LicenseInfo.

        Raises:
            FileNotFoundError: If no license key is stored.
            ValueError: If the stored token is invalid or expired.
        """
        if not self._key_path.exists():
            raise FileNotFoundError(
                f"No license key found at {self._key_path}. "
                "Run `aumos license activate --key <YOUR_LICENSE_KEY>` to activate."
            )

        token = self._key_path.read_text(encoding="utf-8").strip()
        return self._validator.validate(token)

    def revoke(self) -> None:
        """Remove the stored license key from disk.

        Raises:
            FileNotFoundError: If no license key is stored.
        """
        if not self._key_path.exists():
            raise FileNotFoundError(f"No license key found at {self._key_path}")

        self._key_path.unlink()
        logger.info("License key revoked", key_path=str(self._key_path))

    def is_activated(self) -> bool:
        """Check whether a license key is currently stored.

        Returns:
            True if a license key file exists (not validated, only presence check).
        """
        return self._key_path.exists()

    def get_token_raw(self) -> str | None:
        """Return the raw stored JWT token, or None if not activated.

        Returns:
            Raw JWT string, or None if not present.
        """
        if not self._key_path.exists():
            return None
        return self._key_path.read_text(encoding="utf-8").strip()

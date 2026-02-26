"""JWT-based license key validation for AumOS module entitlements.

AumOS license keys are JWT tokens signed with AumOS's RSA private key.
The public key is bundled with the installer for offline validation.

Token claims:
  - sub: customer_id (UUID)
  - iss: "aumos-licensing"
  - iat: issued at (Unix timestamp)
  - exp: expiry (Unix timestamp)
  - modules: list of licensed module names
  - tier: highest tier unlocked (A, B, or C)
  - seats: number of licensed seats
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import jwt
from jwt.exceptions import InvalidTokenError

from aumos_common.observability import get_logger

logger = get_logger(__name__)

EXPECTED_ISSUER = "aumos-licensing"
EXPECTED_ALGORITHM = "RS256"


@dataclass
class LicenseInfo:
    """Decoded and validated license information.

    Attributes:
        customer_id: UUID of the licensed customer.
        modules: Set of module names this license entitles.
        tier: Highest tier unlocked (A, B, or C).
        seats: Number of licensed seats.
        expires_at: Expiry datetime (UTC).
        issued_at: Issue datetime (UTC).
        is_expired: True if the license has expired.
        days_remaining: Days until expiry (negative if expired).
    """

    customer_id: str
    modules: set[str]
    tier: str
    seats: int
    expires_at: datetime
    issued_at: datetime

    @property
    def is_expired(self) -> bool:
        """Whether the license has passed its expiry date."""
        return datetime.now(timezone.utc) > self.expires_at

    @property
    def days_remaining(self) -> int:
        """Days remaining until expiry."""
        delta = self.expires_at - datetime.now(timezone.utc)
        return delta.days

    def is_entitled_to(self, module_name: str) -> bool:
        """Check if this license grants access to a specific module.

        Tier A modules are always accessible.
        Tier B/C modules require explicit entitlement.

        Args:
            module_name: Module name to check.

        Returns:
            True if this license entitles the given module.
        """
        return module_name in self.modules


class LicenseValidator:
    """Validates AumOS JWT license tokens using the bundled public key.

    Supports offline validation — no network call required.
    The public key is distributed with the installer package.
    """

    def __init__(self, public_key_path: Path) -> None:
        """Initialize the validator with the AumOS RSA public key.

        Args:
            public_key_path: Path to the RSA public key PEM file.
        """
        self._public_key_path = public_key_path
        self._public_key: str | None = None

    def _load_public_key(self) -> str:
        """Load the public key from disk (cached after first load).

        Returns:
            PEM-encoded public key string.

        Raises:
            FileNotFoundError: If the public key file does not exist.
        """
        if self._public_key is None:
            if not self._public_key_path.exists():
                raise FileNotFoundError(
                    f"AumOS license public key not found: {self._public_key_path}. "
                    "Re-install the aumos-composable-installer package."
                )
            self._public_key = self._public_key_path.read_text(encoding="utf-8")
        return self._public_key

    def validate(self, token: str) -> LicenseInfo:
        """Validate a license JWT token and return decoded license info.

        Args:
            token: Raw JWT token string.

        Returns:
            Decoded and validated LicenseInfo.

        Raises:
            ValueError: If the token is invalid, expired, or malformed.
            FileNotFoundError: If the public key is not available.
        """
        public_key = self._load_public_key()

        try:
            payload: dict[str, Any] = jwt.decode(
                token,
                public_key,
                algorithms=[EXPECTED_ALGORITHM],
                options={"require": ["sub", "iss", "iat", "exp", "modules", "tier"]},
            )
        except InvalidTokenError as exc:
            logger.warning("License token validation failed", error=str(exc))
            raise ValueError(f"Invalid license token: {exc}") from exc

        if payload.get("iss") != EXPECTED_ISSUER:
            raise ValueError(
                f"License token has unexpected issuer: '{payload.get('iss')}'. "
                f"Expected: '{EXPECTED_ISSUER}'"
            )

        modules_raw = payload.get("modules", [])
        if isinstance(modules_raw, str):
            # Support comma-delimited string as fallback
            modules_set = {m.strip() for m in modules_raw.split(",")}
        else:
            modules_set = set(modules_raw)

        expires_at = datetime.fromtimestamp(payload["exp"], tz=timezone.utc)
        issued_at = datetime.fromtimestamp(payload["iat"], tz=timezone.utc)

        license_info = LicenseInfo(
            customer_id=payload["sub"],
            modules=modules_set,
            tier=payload.get("tier", "A"),
            seats=int(payload.get("seats", 1)),
            expires_at=expires_at,
            issued_at=issued_at,
        )

        logger.info(
            "License validated",
            customer_id=license_info.customer_id,
            tier=license_info.tier,
            modules=sorted(license_info.modules),
            days_remaining=license_info.days_remaining,
        )

        if license_info.is_expired:
            logger.warning(
                "License has expired",
                customer_id=license_info.customer_id,
                expired_at=expires_at.isoformat(),
            )

        return license_info

    def validate_file(self, token_path: Path) -> LicenseInfo:
        """Validate a license token stored in a file.

        Args:
            token_path: Path to the file containing the JWT token.

        Returns:
            Decoded and validated LicenseInfo.

        Raises:
            FileNotFoundError: If the token file does not exist.
            ValueError: If the token is invalid.
        """
        if not token_path.exists():
            raise FileNotFoundError(f"License token file not found: {token_path}")
        token = token_path.read_text(encoding="utf-8").strip()
        return self.validate(token)

    def decode_unverified(self, token: str) -> dict[str, Any]:
        """Decode a token without signature verification (for inspection only).

        WARNING: Do NOT use this for authorization decisions.
        Use validate() for all security-sensitive checks.

        Args:
            token: Raw JWT token string.

        Returns:
            Decoded payload dictionary.
        """
        header = jwt.get_unverified_header(token)
        payload = jwt.decode(token, options={"verify_signature": False})
        return {"header": header, "payload": payload}


def generate_dev_token(
    customer_id: str,
    modules: list[str],
    tier: str = "B",
    private_key_pem: str = "",
) -> str:
    """Generate a development license token (for testing only).

    This function requires the AumOS private key and should NEVER be
    called in production. It is provided for local development and
    integration testing only.

    Args:
        customer_id: Customer UUID.
        modules: List of module names to entitle.
        tier: License tier (A, B, or C).
        private_key_pem: RSA private key in PEM format.

    Returns:
        Signed JWT token string.

    Raises:
        ValueError: If private_key_pem is empty.
    """
    if not private_key_pem:
        raise ValueError("private_key_pem is required to generate tokens")

    import time

    payload = {
        "sub": customer_id,
        "iss": EXPECTED_ISSUER,
        "iat": int(time.time()),
        "exp": int(time.time()) + (365 * 24 * 3600),  # 1 year
        "modules": modules,
        "tier": tier,
        "seats": 10,
    }

    return jwt.encode(payload, private_key_pem, algorithm=EXPECTED_ALGORITHM)

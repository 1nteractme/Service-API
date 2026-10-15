from __future__ import annotations

import base64
import hashlib
import json
import logging
import os

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)


class SessionDecryptionError(ValueError):
    """Raised when a stored browser session is not readable anymore."""


class SessionCipher:
    def __init__(
        self,
        configured_key: str | None = None,
        secret: str | None = None,
    ) -> None:
        configured_key = (
            configured_key
            if configured_key is not None
            else os.getenv("SERVICE_SESSION_ENCRYPTION_KEY", "").strip()
        )
        secret = (
            secret
            if secret is not None
            else os.getenv("SERVICE_SESSION_ENCRYPTION_SECRET", "").strip()
        )
        if configured_key:
            key = configured_key.encode("ascii")
        elif secret:
            key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
        else:
            if os.getenv("SERVICE_ENV", "development").casefold() == "production":
                raise RuntimeError(
                    "SERVICE_SESSION_ENCRYPTION_KEY is required in production"
                )
            # A production deployment must set a stable key or sessions cannot
            # survive a restart. The fallback keeps local development usable.
            logger.warning("SERVICE_SESSION_ENCRYPTION_KEY is not configured")
            key = Fernet.generate_key()
        try:
            self._fernet = Fernet(key)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "SERVICE_SESSION_ENCRYPTION_KEY is not a valid Fernet key"
            ) from exc

    def encrypt(self, session: dict) -> str:
        payload = json.dumps(
            session,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        return self._fernet.encrypt(payload).decode("ascii")

    def decrypt(self, value: str) -> dict:
        try:
            payload = self._fernet.decrypt(value.encode("ascii"))
            result = json.loads(payload)
        except (InvalidToken, UnicodeError, json.JSONDecodeError, TypeError) as exc:
            raise SessionDecryptionError(
                "Stored Service session cannot be decrypted"
            ) from exc
        if not isinstance(result, dict):
            raise SessionDecryptionError(
                "Stored Service session has an invalid format"
            )
        return result

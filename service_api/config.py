from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    """Read a positive integer and fail early on a broken deployment setting."""
    value = os.getenv(name, str(default))
    try:
        result = int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if result < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return result


@dataclass(frozen=True, slots=True)
class Settings:
    """Application settings loaded once when the process starts."""

    database_path: Path
    session_encryption_key: str | None
    session_encryption_secret: str | None
    auth_requests_per_10_minutes: int
    challenge_ttl_seconds: int
    auth_max_attempts: int
    api_token_ttl_seconds: int
    service_base_url: str
    service_headless: bool
    sync_timeout_seconds: int
    chat_amount_limit: int
    message_amount_limit: int
    sqlite_timeout_seconds: int
    test_mode: bool
    test_sms_code: str
    sync_fixture_path: Path | None
    timezone: str

    @classmethod
    def from_env(cls) -> "Settings":
        fixture = os.getenv("SERVICE_SYNC_FIXTURE_PATH", "").strip()
        environment = os.getenv("SERVICE_ENV", "development").casefold()
        test_mode = os.getenv("SERVICE_TEST_MODE") == "1"
        if environment == "production" and test_mode:
            raise ValueError(
                "SERVICE_TEST_MODE cannot be enabled in production"
            )
        service_base_url = os.getenv("SERVICE_BASE_URL", "").strip().rstrip("/")
        parsed_base_url = urlparse(service_base_url)
        if (
            parsed_base_url.scheme not in {"http", "https"}
            or not parsed_base_url.netloc
        ):
            raise ValueError(
                "SERVICE_BASE_URL must be an absolute HTTP(S) URL"
            )
        return cls(
            database_path=Path(
                os.getenv("SERVICE_DB_PATH", "data/service.sqlite3")
            ),
            session_encryption_key=os.getenv(
                "SERVICE_SESSION_ENCRYPTION_KEY"
            ),
            session_encryption_secret=os.getenv(
                "SERVICE_SESSION_ENCRYPTION_SECRET"
            ),
            auth_requests_per_10_minutes=_env_int(
                "SERVICE_AUTH_REQUESTS_PER_10_MINUTES", 3, minimum=0
            ),
            challenge_ttl_seconds=_env_int(
                "SERVICE_AUTH_CHALLENGE_TTL_SECONDS", 300
            ),
            auth_max_attempts=_env_int("SERVICE_AUTH_MAX_ATTEMPTS", 5),
            api_token_ttl_seconds=_env_int(
                "SERVICE_API_TOKEN_TTL_SECONDS", 30 * 24 * 60 * 60
            ),
            service_base_url=service_base_url,
            service_headless=os.getenv("SERVICE_HEADLESS", "1") != "0",
            sync_timeout_seconds=_env_int("SERVICE_SYNC_TIMEOUT_SECONDS", 300),
            chat_amount_limit=_env_int("CHAT_AMOUNT_LIMIT", 20, minimum=0),
            message_amount_limit=_env_int(
                "MESSAGE_AMOUNT_LIMIT", 50, minimum=0
            ),
            sqlite_timeout_seconds=_env_int(
                "SERVICE_SQLITE_TIMEOUT_SECONDS", 30
            ),
            test_mode=test_mode,
            test_sms_code=os.getenv("SERVICE_TEST_SMS_CODE", "1234"),
            sync_fixture_path=Path(fixture) if fixture else None,
            timezone=os.getenv("SERVICE_TIMEZONE", "Europe/Moscow"),
        )

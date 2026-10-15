from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
from datetime import UTC, datetime, timedelta

from .config import Settings
from .db import Database
from .session_crypto import SessionCipher, SessionDecryptionError
from .service import (
    ServiceAuthError,
    ServiceBrowserAuthenticator,
    ServiceChatScraper,
    ServiceSessionExpired,
    ServiceSyncError,
    normalize_phone,
)

logger = logging.getLogger(__name__)


class AuthServiceError(RuntimeError):
    """Base error for expected authentication failures."""


class InvalidChallengeError(AuthServiceError):
    pass


class InvalidSmsCodeError(AuthServiceError):
    pass


class RateLimitError(AuthServiceError):
    pass


class AuthProviderError(AuthServiceError):
    pass


class AuthService:
    """Coordinates phone login, Service browser sessions, and API tokens."""

    def __init__(
        self,
        database: Database,
        cipher: SessionCipher,
        authenticator: ServiceBrowserAuthenticator,
        settings: Settings,
    ) -> None:
        self.database = database
        self.cipher = cipher
        self.authenticator = authenticator
        self.settings = settings

    async def request_code(self, raw_phone: str) -> str:
        try:
            phone = normalize_phone(raw_phone)
        except ValueError as exc:
            raise InvalidChallengeError(str(exc)) from exc

        since = datetime.now(UTC) - timedelta(minutes=10)
        recent = await asyncio.to_thread(
            self.database.count_recent_challenges,
            phone,
            since.isoformat(),
        )
        if recent >= self.settings.auth_requests_per_10_minutes:
            raise RateLimitError(
                "Слишком много запросов SMS-кода"
            )

        user = await asyncio.to_thread(self.database.get_or_create_user, phone)
        challenge_id = secrets.token_urlsafe(24)
        expires_at = datetime.now(UTC) + timedelta(
            seconds=self.settings.challenge_ttl_seconds
        )
        await asyncio.to_thread(
            self.database.create_challenge,
            challenge_id,
            user["id"],
            phone,
            expires_at.isoformat(),
            self.settings.auth_max_attempts,
        )

        try:
            await self.authenticator.request_code(challenge_id, phone)
        except ServiceAuthError as exc:
            # Не оставляем challenge, для которого SMS
            # фактически не был отправлен.
            await self.authenticator.discard(challenge_id)
            await asyncio.to_thread(
                self.database.delete_challenge,
                challenge_id,
            )
            logger.warning(
                "Не удалось запросить SMS-код Service: %s",
                exc,
            )
            raise AuthProviderError(
                "Не удалось запросить SMS-код"
            ) from exc
        return challenge_id

    async def verify_code(
        self,
        challenge_id: str,
        code: str,
        device_name: str | None,
    ) -> tuple[str, dict]:
        challenge = await asyncio.to_thread(
            self.database.get_challenge, challenge_id
        )
        if not challenge or challenge["used_at"] or self._is_expired(challenge):
            await self.authenticator.discard(challenge_id)
            raise InvalidChallengeError(
                "Срок действия challenge истёк"
            )
        if challenge["attempts"] >= challenge["max_attempts"]:
            raise RateLimitError(
                "Слишком много попыток ввода кода"
            )

        attempt = await asyncio.to_thread(
            self.database.register_challenge_attempt, challenge_id
        )
        if attempt is None:
            await self.authenticator.discard(challenge_id)
            raise RateLimitError("Слишком много попыток ввода кода")
        try:
            session = await self.authenticator.verify_code(challenge_id, code)
        except ServiceAuthError as exc:
            current = await asyncio.to_thread(
                self.database.get_challenge, challenge_id
            )
            if current and current["attempts"] >= current["max_attempts"]:
                await self.authenticator.discard(challenge_id)
                raise RateLimitError(
                    "Слишком много попыток ввода кода"
                ) from exc
            raise InvalidSmsCodeError("Неверный SMS-код") from exc

        await asyncio.to_thread(
            self.database.save_session,
            challenge["user_id"],
            self.cipher.encrypt(session),
            "active",
        )
        await asyncio.to_thread(self.database.use_challenge, challenge_id)

        access_token = secrets.token_urlsafe(48)
        await asyncio.to_thread(
            self.database.create_token,
            challenge["user_id"],
            self.hash_token(access_token),
            device_name,
        )
        user = await asyncio.to_thread(
            self.database.get_user, challenge["user_id"]
        )
        return access_token, user

    async def authenticate(self, access_token: str) -> tuple[dict, str] | None:
        token_hash = self.hash_token(access_token)
        token = await asyncio.to_thread(
            self.database.get_active_token, token_hash
        )
        if token is None:
            return None
        user = await asyncio.to_thread(self.database.get_user, token["user_id"])
        return (user, token_hash) if user else None

    async def logout(self, token_hash: str) -> None:
        await asyncio.to_thread(self.database.revoke_token, token_hash)

    async def revoke_all(self, user_id: int) -> int:
        return await asyncio.to_thread(self.database.revoke_all_tokens, user_id)

    @staticmethod
    def hash_token(access_token: str) -> str:
        return hashlib.sha256(access_token.encode("utf-8")).hexdigest()

    @staticmethod
    def _is_expired(challenge: dict) -> bool:
        try:
            expires_at = datetime.fromisoformat(challenge["expires_at"])
        except (KeyError, TypeError, ValueError):
            return True
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        return expires_at <= datetime.now(UTC)


class SyncService:
    """Runs at most one synchronization for each user at a time."""

    def __init__(
        self,
        database: Database,
        cipher: SessionCipher,
        scraper: ServiceChatScraper,
    ) -> None:
        self.database = database
        self.cipher = cipher
        self.scraper = scraper
        self._tasks: dict[int, asyncio.Task[None]] = {}

    async def start(self, user_id: int) -> tuple[str, int | None]:
        session = await asyncio.to_thread(self.database.get_session, user_id)
        if not session or session["status"] != "active":
            return "reauth_required", None

        current_task = self._tasks.get(user_id)
        if current_task and not current_task.done():
            return "already_running", None

        run_id = await asyncio.to_thread(self.database.create_sync_run, user_id)
        self._tasks[user_id] = asyncio.create_task(self._run(user_id, run_id))
        return "started", run_id

    async def shutdown(self) -> None:
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _run(self, user_id: int, run_id: int) -> None:
        try:
            session = await asyncio.to_thread(self.database.get_session, user_id)
            if not session or session["status"] != "active":
                await self._finish(run_id, "reauth_required")
                return

            state = self.cipher.decrypt(session["encrypted_session"])
            chats = await asyncio.to_thread(self.scraper.sync, state)
            chats_processed, messages_added = await asyncio.to_thread(
                self.database.upsert_chat_bundle,
                user_id,
                chats,
            )
            await self._finish(
                run_id,
                "completed",
                chats_processed=chats_processed,
                messages_added=messages_added,
            )
            await asyncio.to_thread(
                self.database.update_session_status, user_id, "active"
            )
        except ServiceSessionExpired as exc:
            await asyncio.to_thread(
                self.database.update_session_status,
                user_id,
                "reauth_required",
            )
            await self._finish(run_id, "reauth_required", error=str(exc))
        except SessionDecryptionError:
            await asyncio.to_thread(
                self.database.update_session_status,
                user_id,
                "reauth_required",
            )
            await self._finish(
                run_id,
                "reauth_required",
                error=(
                    "Сохранённая сессия Service "
                    "недействительна"
                ),
            )
        except ServiceSyncError as exc:
            logger.warning(
                "Ошибка синхронизации пользователя "
                "%s: %s",
                user_id,
                exc,
            )
            await self._finish(run_id, "failed", error=str(exc))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Непредвиденная ошибка синхронизации "
                "пользователя %s",
                user_id,
            )
            await self._finish(
                run_id,
                "failed",
                error=(
                    "Непредвиденная ошибка "
                    "синхронизации"
                ),
            )
        finally:
            self._tasks.pop(user_id, None)

    async def _finish(
        self,
        run_id: int,
        run_status: str,
        *,
        error: str | None = None,
        chats_processed: int = 0,
        messages_added: int = 0,
    ) -> None:
        await asyncio.to_thread(
            self.database.finish_sync_run,
            run_id,
            run_status,
            error=error,
            chats_processed=chats_processed,
            messages_added=messages_added,
        )

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Iterable


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class Database:
    """Small SQLite repository with explicit user scoping on every query."""

    def __init__(
        self,
        path: str | Path,
        *,
        timeout_seconds: int = 30,
        token_ttl_seconds: int = 30 * 24 * 60 * 60,
    ):
        self.path = Path(path)
        self.timeout_seconds = timeout_seconds
        self.token_ttl_seconds = token_ttl_seconds

    def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            self._migrate_session_table(connection)
            connection.executescript(
                """
                PRAGMA journal_mode = WAL;
                PRAGMA foreign_keys = ON;

                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    phone TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS auth_challenges (
                    id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    phone TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 5,
                    expires_at TEXT NOT NULL,
                    used_at TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_auth_challenges_phone_created
                    ON auth_challenges(phone, created_at DESC);

                CREATE TABLE IF NOT EXISTS service_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL UNIQUE
                        REFERENCES users(id) ON DELETE CASCADE,
                    encrypted_session TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    last_checked_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS api_tokens (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    token_hash TEXT NOT NULL UNIQUE,
                    device_name TEXT,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    last_used_at TEXT,
                    revoked_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_api_tokens_user_active
                    ON api_tokens(user_id, revoked_at);

                CREATE TABLE IF NOT EXISTS chats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    external_chat_id TEXT NOT NULL,
                    chat_index INTEGER NOT NULL DEFAULT 0,
                    name TEXT NOT NULL,
                    source TEXT NOT NULL,
                    last_message_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(user_id, external_chat_id)
                );
                CREATE INDEX IF NOT EXISTS idx_chats_user_activity
                    ON chats(user_id, last_message_at DESC, chat_index ASC);

                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
                    external_message_id TEXT NOT NULL,
                    sender TEXT,
                    message_text TEXT NOT NULL,
                    message_date TEXT,
                    position INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    UNIQUE(chat_id, external_message_id)
                );
                CREATE INDEX IF NOT EXISTS idx_messages_chat_date
                    ON messages(chat_id, message_date ASC, position ASC);

                CREATE TABLE IF NOT EXISTS sync_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    status TEXT NOT NULL,
                    error TEXT,
                    chats_processed INTEGER NOT NULL DEFAULT 0,
                    messages_added INTEGER NOT NULL DEFAULT 0,
                    started_at TEXT NOT NULL,
                    finished_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_sync_runs_user_started
                    ON sync_runs(user_id, started_at DESC);
                """
            )
            self._migrate_api_token_expiry(connection)

    @staticmethod
    def _migrate_session_table(connection: sqlite3.Connection) -> None:
        """Rename the pre-Service session table without dropping its data."""
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name IN (?, ?) ",
                ("tetrika_sessions", "service_sessions"),
            )
        }
        if "tetrika_sessions" not in tables:
            return
        if "service_sessions" in tables:
            raise RuntimeError(
                "Both legacy tetrika_sessions and service_sessions exist; "
                "merge them manually before starting the API"
            )
        connection.execute(
            "ALTER TABLE tetrika_sessions RENAME TO service_sessions"
        )

    def _migrate_api_token_expiry(self, connection: sqlite3.Connection) -> None:
        """Add expiry to databases created before token TTL was introduced."""
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(api_tokens)")
        }
        if "expires_at" not in columns:
            connection.execute("ALTER TABLE api_tokens ADD COLUMN expires_at TEXT")

        rows = connection.execute(
            "SELECT id, created_at FROM api_tokens WHERE expires_at IS NULL"
        ).fetchall()
        for row in rows:
            try:
                created_at = datetime.fromisoformat(row[1])
                if created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=UTC)
                expires_at = created_at + timedelta(
                    seconds=self.token_ttl_seconds
                )
            except (TypeError, ValueError):
                # An unreadable timestamp must fail closed instead of creating
                # a token that never expires.
                expires_at = datetime.now(UTC)
            connection.execute(
                "UPDATE api_tokens SET expires_at = ? WHERE id = ?",
                (expires_at.isoformat(), row[0]),
            )

    def get_or_create_user(self, phone: str) -> dict:
        now = utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO users(phone, created_at, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(phone) DO UPDATE SET updated_at = excluded.updated_at
                """,
                (phone, now, now),
            )
            row = connection.execute(
                "SELECT id, phone, created_at, updated_at FROM users WHERE phone = ?",
                (phone,),
            ).fetchone()
        return dict(row)

    def get_user(self, user_id: int) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT id, phone, created_at, updated_at FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
        return dict(row) if row else None

    def create_challenge(
        self,
        challenge_id: str,
        user_id: int,
        phone: str,
        expires_at: str,
        max_attempts: int,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO auth_challenges
                    (id, user_id, phone, expires_at, max_attempts, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (challenge_id, user_id, phone, expires_at, max_attempts, utc_now()),
            )

    def get_challenge(self, challenge_id: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM auth_challenges WHERE id = ?", (challenge_id,)
            ).fetchone()
        return dict(row) if row else None

    def delete_challenge(self, challenge_id: str) -> None:
        """Remove a challenge when the external SMS request did not start."""
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM auth_challenges WHERE id = ?",
                (challenge_id,),
            )

    def count_recent_challenges(self, phone: str, since: str) -> int:
        with self._connect() as connection:
            return int(
                connection.execute(
                    "SELECT COUNT(*) FROM auth_challenges "
                    "WHERE phone = ? AND created_at >= ?",
                    (phone, since),
                ).fetchone()[0]
            )

    def register_challenge_attempt(self, challenge_id: str) -> dict | None:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE auth_challenges
                SET attempts = attempts + 1
                WHERE id = ?
                  AND used_at IS NULL
                  AND expires_at > ?
                  AND attempts < max_attempts
                """,
                (challenge_id, utc_now()),
            )
            if cursor.rowcount != 1:
                return None
            row = connection.execute(
                "SELECT * FROM auth_challenges WHERE id = ?", (challenge_id,)
            ).fetchone()
        return dict(row) if row else None

    def use_challenge(self, challenge_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE auth_challenges SET used_at = ? WHERE id = ?",
                (utc_now(), challenge_id),
            )

    def save_session(
        self,
        user_id: int,
        encrypted_session: str,
        session_status: str = "active",
    ) -> None:
        now = utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO service_sessions
                    (
                        user_id,
                        encrypted_session,
                        status,
                        last_checked_at,
                        created_at,
                        updated_at
                    )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    encrypted_session = excluded.encrypted_session,
                    status = excluded.status,
                    last_checked_at = excluded.last_checked_at,
                    updated_at = excluded.updated_at
                """,
                (user_id, encrypted_session, session_status, now, now, now),
            )

    def get_session(self, user_id: int) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM service_sessions WHERE user_id = ?", (user_id,)
            ).fetchone()
        return dict(row) if row else None

    def update_session_status(self, user_id: int, session_status: str) -> None:
        now = utc_now()
        with self._connect() as connection:
            connection.execute(
                "UPDATE service_sessions "
                "SET status = ?, last_checked_at = ?, updated_at = ? "
                "WHERE user_id = ?",
                (session_status, now, now, user_id),
            )

    def create_token(
        self,
        user_id: int,
        token_hash: str,
        device_name: str | None,
    ) -> dict:
        created_at = datetime.now(UTC)
        expires_at = created_at + timedelta(seconds=self.token_ttl_seconds)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO api_tokens(
                    user_id, token_hash, device_name, created_at, expires_at
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    token_hash,
                    device_name,
                    created_at.isoformat(),
                    expires_at.isoformat(),
                ),
            )
            return {
                "id": cursor.lastrowid,
                "user_id": user_id,
                "device_name": device_name,
                "created_at": created_at.isoformat(),
                "expires_at": expires_at.isoformat(),
            }

    def get_active_token(self, token_hash: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT id, user_id, device_name, created_at, last_used_at
                FROM api_tokens
                WHERE token_hash = ?
                  AND revoked_at IS NULL
                  AND expires_at IS NOT NULL
                  AND expires_at > ?
                """,
                (token_hash, utc_now()),
            ).fetchone()
            if row:
                connection.execute(
                    "UPDATE api_tokens SET last_used_at = ? WHERE id = ?",
                    (utc_now(), row["id"]),
                )
        return dict(row) if row else None

    def revoke_token(self, token_hash: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE api_tokens SET revoked_at = ? WHERE token_hash = ?",
                (utc_now(), token_hash),
            )

    def revoke_all_tokens(self, user_id: int) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE api_tokens SET revoked_at = ? "
                "WHERE user_id = ? AND revoked_at IS NULL",
                (utc_now(), user_id),
            )
            return cursor.rowcount

    def create_sync_run(self, user_id: int) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO sync_runs(user_id, status, started_at) "
                "VALUES (?, 'started', ?)",
                (user_id, utc_now()),
            )
            return int(cursor.lastrowid)

    def finish_sync_run(
        self,
        run_id: int,
        run_status: str,
        *,
        error: str | None = None,
        chats_processed: int = 0,
        messages_added: int = 0,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE sync_runs
                SET status = ?, error = ?, chats_processed = ?,
                    messages_added = ?, finished_at = ?
                WHERE id = ?
                """,
                (
                    run_status,
                    error,
                    chats_processed,
                    messages_added,
                    utc_now(),
                    run_id,
                ),
            )

    def get_sync_run(self, user_id: int, run_id: int | None = None) -> dict | None:
        query = "SELECT * FROM sync_runs WHERE user_id = ?"
        params: list[object] = [user_id]
        if run_id is not None:
            query += " AND id = ?"
            params.append(run_id)
        query += " ORDER BY started_at DESC, id DESC LIMIT 1"
        with self._connect() as connection:
            row = connection.execute(query, params).fetchone()
        return dict(row) if row else None

    def upsert_chat_bundle(
        self,
        user_id: int,
        chats: Iterable[dict],
    ) -> tuple[int, int]:
        chat_count = 0
        message_count = 0
        now = utc_now()
        with self._connect() as connection:
            for chat in chats:
                messages = list(chat.get("messages") or [])
                last_message_at = max(
                    (
                        message.get("date")
                        for message in messages
                        if message.get("date")
                    ),
                    default=None,
                )
                connection.execute(
                    """
                    INSERT INTO chats
                        (user_id, external_chat_id, chat_index, name, source,
                         last_message_at, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(user_id, external_chat_id) DO UPDATE SET
                        chat_index = excluded.chat_index,
                        name = excluded.name,
                        source = excluded.source,
                        last_message_at = COALESCE(
                            excluded.last_message_at,
                            chats.last_message_at
                        ),
                        updated_at = excluded.updated_at
                    """,
                    (
                        user_id,
                        str(chat["chat_id"]),
                        int(chat.get("chat_index", 0)),
                        str(chat.get("chat_name") or "Без названия"),
                        str(chat.get("source") or "https://tetrika-school.ru/lk/chat"),
                        last_message_at,
                        now,
                        now,
                    ),
                )
                chat_row = connection.execute(
                    "SELECT id FROM chats WHERE user_id = ? AND external_chat_id = ?",
                    (user_id, str(chat["chat_id"])),
                ).fetchone()
                for message in messages:
                    cursor = connection.execute(
                        """
                        INSERT OR IGNORE INTO messages
                            (chat_id, external_message_id, sender, message_text,
                             message_date, position, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            chat_row[0],
                            str(message["message_id"]),
                            message.get("sender"),
                            str(message.get("message") or ""),
                            message.get("date"),
                            int(message.get("position", 0)),
                            now,
                        ),
                    )
                    message_count += int(cursor.rowcount == 1)
                chat_count += 1
        return chat_count, message_count

    def list_chats(
        self,
        user_id: int,
        limit: int | None,
        offset: int,
    ) -> tuple[list[dict], int]:
        with self._connect() as connection:
            query = """
                SELECT c.id, c.external_chat_id AS chat_id, c.chat_index,
                       c.name AS chat_name, c.source, c.last_message_at,
                       COALESCE((SELECT m.message_text FROM messages m
                                 WHERE m.chat_id = c.id
                                 ORDER BY m.message_date DESC,
                                          m.position DESC,
                                          m.id DESC LIMIT 1), '')
                           AS last_message,
                       0 AS unread_count
                FROM chats c
                WHERE c.user_id = ?
                ORDER BY c.last_message_at DESC, c.chat_index ASC, c.id ASC
            """
            params: list[object] = [user_id]
            if limit is not None:
                query += " LIMIT ? OFFSET ?"
                params.extend([limit, offset])
            rows = connection.execute(query, params).fetchall()
            total = connection.execute(
                "SELECT COUNT(*) FROM chats WHERE user_id = ?", (user_id,)
            ).fetchone()[0]
        return [dict(row) for row in rows], int(total)

    def get_chat(self, user_id: int, external_chat_id: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT id, external_chat_id AS chat_id, chat_index,
                       name AS chat_name, source, last_message_at
                FROM chats WHERE user_id = ? AND external_chat_id = ?
                """,
                (user_id, external_chat_id),
            ).fetchone()
        return dict(row) if row else None

    def list_messages(
        self,
        user_id: int,
        external_chat_id: str,
        limit: int | None,
        offset: int,
    ) -> tuple[dict | None, list[dict], int]:
        with self._connect() as connection:
            chat = connection.execute(
                """
                SELECT id, external_chat_id AS chat_id, chat_index,
                       name AS chat_name, source, last_message_at
                FROM chats WHERE user_id = ? AND external_chat_id = ?
                """,
                (user_id, external_chat_id),
            ).fetchone()
            if not chat:
                return None, [], 0
            query = """
                SELECT external_message_id AS message_id, sender,
                       message_text AS message, message_date AS date, position
                FROM messages WHERE chat_id = ?
                ORDER BY message_date ASC, position ASC, id ASC
            """
            params: list[object] = [chat["id"]]
            if limit is not None:
                query += " LIMIT ? OFFSET ?"
                params.extend([limit, offset])
            rows = connection.execute(query, params).fetchall()
            total = connection.execute(
                "SELECT COUNT(*) FROM messages WHERE chat_id = ?", (chat["id"],)
            ).fetchone()[0]
        return dict(chat), [dict(row) for row in rows], int(total)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=self.timeout_seconds,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

from __future__ import annotations

import json
import os

from .config import Settings
from .db import Database
from .session_crypto import SessionCipher
from .service import ServiceChatScraper


def main() -> None:
    user_id_value = os.getenv("SERVICE_USER_ID")
    if not user_id_value:
        raise SystemExit("Set SERVICE_USER_ID to run a one-off user synchronization")
    settings = Settings.from_env()
    database = Database(
        settings.database_path,
        timeout_seconds=settings.sqlite_timeout_seconds,
        token_ttl_seconds=settings.api_token_ttl_seconds,
    )
    database.init()
    user_id = int(user_id_value)
    session = database.get_session(user_id)
    if not session or session["status"] != "active":
        raise SystemExit("The user has no active Service session")
    state = SessionCipher(
        settings.session_encryption_key,
        settings.session_encryption_secret,
    ).decrypt(session["encrypted_session"])
    chats = ServiceChatScraper(
        service_base_url=settings.service_base_url,
        fixture_path=settings.sync_fixture_path,
        sync_timeout_seconds=settings.sync_timeout_seconds,
        chat_amount_limit=settings.chat_amount_limit,
        message_amount_limit=settings.message_amount_limit,
        timezone=settings.timezone,
    ).sync(state)
    chats_processed, messages_added = database.upsert_chat_bundle(user_id, chats)
    print(
        json.dumps(
            {
                "status": "completed",
                "chats_processed": chats_processed,
                "messages_added": messages_added,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()

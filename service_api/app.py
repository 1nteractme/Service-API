from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from .config import Settings
from .db import Database
from .services import (
    AuthProviderError,
    AuthService,
    InvalidChallengeError,
    InvalidSmsCodeError,
    RateLimitError,
    SyncService,
)
from .session_crypto import SessionCipher
from .service import ServiceBrowserAuthenticator, ServiceChatScraper

# Dependencies are assembled in one place. Routes below only deal with HTTP
# input/output and do not know how SQLite or Playwright work internally.
settings = Settings.from_env()
database = Database(
    settings.database_path,
    timeout_seconds=settings.sqlite_timeout_seconds,
    token_ttl_seconds=settings.api_token_ttl_seconds,
)
session_cipher = SessionCipher(
    settings.session_encryption_key,
    settings.session_encryption_secret,
)
authenticator = ServiceBrowserAuthenticator(
    service_base_url=settings.service_base_url,
    headless=settings.service_headless,
    test_mode=settings.test_mode,
    test_sms_code=settings.test_sms_code,
)
scraper = ServiceChatScraper(
    service_base_url=settings.service_base_url,
    fixture_path=settings.sync_fixture_path,
    sync_timeout_seconds=settings.sync_timeout_seconds,
    chat_amount_limit=settings.chat_amount_limit,
    message_amount_limit=settings.message_amount_limit,
    timezone=settings.timezone,
)
auth_service = AuthService(database, session_cipher, authenticator, settings)
sync_service = SyncService(database, session_cipher, scraper)
bearer = HTTPBearer(auto_error=False)


class User(BaseModel):
    id: int
    phone: str


class RequestCodeRequest(BaseModel):
    phone: str = Field(min_length=10, max_length=32)


class RequestCodeResponse(BaseModel):
    challenge_id: str
    message: str


class VerifyCodeRequest(BaseModel):
    challenge_id: str
    code: str = Field(pattern=r"^\d{4,8}$")
    device_name: str | None = Field(default=None, max_length=100)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "Bearer"
    user: User


class ChatSummary(BaseModel):
    chat_id: str
    chat_index: int
    chat_name: str
    source: str
    last_message: str
    last_message_at: str | None
    unread_count: int = 0


class ChatPage(BaseModel):
    items: list[ChatSummary]
    total: int


class ChatMessage(BaseModel):
    message_id: str
    sender: str | None
    date: str | None
    message: str
    position: int


class ChatMessagesResponse(BaseModel):
    chat_id: str
    chat_name: str
    source: str
    messages: list[ChatMessage]
    total: int
    limit: int
    offset: int


class SyncStartResponse(BaseModel):
    status: str
    sync_run_id: int | None = None


class SyncRequest(BaseModel):
    chat_amount_limit: int | None = Field(default=None, ge=0)
    message_amount_limit: int | None = Field(default=None, ge=0)


class SyncStatusResponse(BaseModel):
    status: str
    sync_run_id: int | None = None
    error: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    chats_processed: int = 0
    messages_added: int = 0


class HealthResponse(BaseModel):
    status: str


@dataclass(frozen=True, slots=True)
class AuthContext:
    user: dict
    token_hash: str


async def require_auth(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> AuthContext:
    """Validate the Bearer token before entering a protected route."""
    if credentials is None or credentials.scheme.casefold() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Требуется Bearer-токен",
            headers={"WWW-Authenticate": "Bearer"},
        )

    authenticated = await auth_service.authenticate(credentials.credentials)
    if authenticated is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Токен недействителен или отозван",
            headers={"WWW-Authenticate": "Bearer"},
        )
    user, token_hash = authenticated
    return AuthContext(user=user, token_hash=token_hash)


Auth = Annotated[AuthContext, Depends(require_auth)]


def _effective_limit(requested: int | None, configured: int) -> int | None:
    """Apply the configured limit; zero means that the limit is disabled."""
    if requested is None:
        return configured or None
    if configured == 0:
        return requested
    return min(requested, configured)


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Prepare the database and stop background work on graceful shutdown."""
    database.init()
    try:
        yield
    finally:
        await sync_service.shutdown()


app = FastAPI(
    title="Service Chat API",
    version="1.0.0",
    description=(
        "API для синхронизации чатов Service "
        "по пользователям."
    ),
    lifespan=lifespan,
)


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok")


@app.post("/auth/request-code", response_model=RequestCodeResponse)
async def request_code(payload: RequestCodeRequest) -> RequestCodeResponse:
    try:
        challenge_id = await auth_service.request_code(payload.phone)
    except InvalidChallengeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RateLimitError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except AuthProviderError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return RequestCodeResponse(
        challenge_id=challenge_id,
        message="Код отправлен",
    )


@app.post("/auth/verify-code", response_model=TokenResponse)
async def verify_code(payload: VerifyCodeRequest) -> TokenResponse:
    try:
        access_token, user = await auth_service.verify_code(
            payload.challenge_id,
            payload.code,
            payload.device_name,
        )
    except InvalidChallengeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except InvalidSmsCodeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RateLimitError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    return TokenResponse(
        access_token=access_token,
        user=User.model_validate(user),
    )


@app.get("/me", response_model=User)
async def me(auth: Auth) -> User:
    return User.model_validate(auth.user)


@app.post("/auth/logout", status_code=204)
async def logout(auth: Auth) -> None:
    await auth_service.logout(auth.token_hash)


@app.post("/auth/revoke-all", response_model=dict[str, int])
async def revoke_all(auth: Auth) -> dict[str, int]:
    revoked = await auth_service.revoke_all(auth.user["id"])
    return {"revoked": revoked}


@app.get("/chats", response_model=ChatPage)
async def list_chats(
    auth: Auth,
    limit: int | None = Query(None, ge=1),
    offset: int = Query(0, ge=0),
) -> ChatPage:
    effective_limit = _effective_limit(limit, settings.chat_amount_limit)
    rows, total = await asyncio.to_thread(
        database.list_chats,
        auth.user["id"],
        effective_limit,
        offset,
    )
    return ChatPage(
        items=[ChatSummary.model_validate(row) for row in rows],
        total=total,
    )


@app.get("/chats/{chat_id}/messages", response_model=ChatMessagesResponse)
async def list_chat_messages(
    chat_id: str,
    auth: Auth,
    limit: int | None = Query(None, ge=1),
    offset: int = Query(0, ge=0),
) -> ChatMessagesResponse:
    effective_limit = _effective_limit(limit, settings.message_amount_limit)
    chat, rows, total = await asyncio.to_thread(
        database.list_messages,
        auth.user["id"],
        chat_id,
        effective_limit,
        offset,
    )
    if chat is None:
        raise HTTPException(status_code=404, detail="Чат не найден")
    return ChatMessagesResponse(
        chat_id=chat["chat_id"],
        chat_name=chat["chat_name"],
        source=chat["source"],
        messages=[ChatMessage.model_validate(row) for row in rows],
        total=total,
        limit=effective_limit or 0,
        offset=offset,
    )


@app.post("/sync", response_model=SyncStartResponse)
async def start_sync(
    auth: Auth,
    payload: SyncRequest | None = None,
) -> SyncStartResponse | JSONResponse:
    sync_status, sync_run_id = await sync_service.start(
        auth.user["id"],
        chat_amount_limit=payload.chat_amount_limit if payload else None,
        message_amount_limit=(
            payload.message_amount_limit if payload else None
        ),
    )
    if sync_status != "started":
        return JSONResponse(status_code=409, content={"status": sync_status})
    return SyncStartResponse(status=sync_status, sync_run_id=sync_run_id)


@app.get("/sync/status", response_model=SyncStatusResponse)
async def sync_status(
    auth: Auth,
    sync_run_id: int | None = Query(None, ge=1),
) -> SyncStatusResponse:
    run = await asyncio.to_thread(
        database.get_sync_run,
        auth.user["id"],
        sync_run_id,
    )
    if run is None:
        return SyncStatusResponse(status="never_started")
    return SyncStatusResponse(
        status=run["status"],
        sync_run_id=run["id"],
        error=run["error"],
        started_at=run["started_at"],
        finished_at=run["finished_at"],
        chats_processed=run["chats_processed"],
        messages_added=run["messages_added"],
    )

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


class ServiceAuthError(RuntimeError):
    pass


class ServiceSessionExpired(ServiceAuthError):
    pass


class ServiceSyncError(RuntimeError):
    pass


def _parse_date(value: str | None, timezone: ZoneInfo | None = None) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    local_timezone = timezone or ZoneInfo(
        os.getenv("SERVICE_TIMEZONE", "Europe/Moscow")
    )
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        parsed = None

    if parsed is None:
        now_local = datetime.now(local_timezone)
        relative = re.fullmatch(
            r"(Сегодня|Вчера)\s+(\d{1,2}):(\d{2})",
            text,
            re.IGNORECASE,
        )
        if relative:
            days_ago = 1 if relative.group(1).casefold() == "вчера" else 0
            day = now_local - timedelta(days=days_ago)
            parsed = day.replace(
                hour=int(relative.group(2)),
                minute=int(relative.group(3)),
                second=0,
                microsecond=0,
            )

        russian_months = {
            "января": 1,
            "февраля": 2,
            "марта": 3,
            "апреля": 4,
            "мая": 5,
            "июня": 6,
            "июля": 7,
            "августа": 8,
            "сентября": 9,
            "октября": 10,
            "ноября": 11,
            "декабря": 12,
        }
        match = re.fullmatch(
            r"(\d{1,2})\s+([А-Яа-я]+)(?:\s+(\d{4}))?\s+"
            r"(\d{1,2}):(\d{2})(?::(\d{2}))?",
            text,
        )
        if parsed is None and match and match.group(2).casefold() in russian_months:
            parsed = datetime(
                int(match.group(3) or now_local.year),
                russian_months[match.group(2).casefold()],
                int(match.group(1)),
                int(match.group(4)),
                int(match.group(5)),
                int(match.group(6) or 0),
                tzinfo=local_timezone,
            )
            if not match.group(3) and parsed > now_local + timedelta(days=1):
                parsed = parsed.replace(year=parsed.year - 1)

    if parsed is None:
        for pattern in ("%d.%m.%Y %H:%M", "%d.%m.%Y", "%Y-%m-%d %H:%M:%S"):
            try:
                parsed = datetime.strptime(text, pattern).replace(tzinfo=UTC)
                break
            except ValueError:
                continue
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.isoformat()


class ServiceBrowserAuthenticator:
    """Keeps the short-lived browser context between the two auth requests."""

    def __init__(
        self,
        *,
        service_base_url: str,
        headless: bool | None = None,
        test_mode: bool | None = None,
        test_sms_code: str | None = None,
    ) -> None:
        self._pending: dict[str, dict] = {}
        self.service_base_url = service_base_url.rstrip("/")
        self.login_url = f"{self.service_base_url}/auth/login"
        self.headless = (
            headless
            if headless is not None
            else os.getenv("SERVICE_HEADLESS", "1") != "0"
        )
        self.test_mode = (
            test_mode
            if test_mode is not None
            else os.getenv("SERVICE_TEST_MODE") == "1"
        )
        self.test_sms_code = test_sms_code or os.getenv(
            "SERVICE_TEST_SMS_CODE", "1234"
        )

    async def request_code(self, challenge_id: str, phone: str) -> None:
        if self.test_mode:
            self._pending[challenge_id] = {"phone": phone, "test": True}
            return
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise ServiceAuthError(
                "Playwright не установлен на сервере"
            ) from exc

        playwright = await async_playwright().start()
        browser = await playwright.chromium.launch(
            headless=self.headless
        )
        context = await browser.new_context()
        page = await context.new_page()
        try:
            await page.goto(
                self.login_url,
                wait_until="domcontentloaded",
                timeout=60_000,
            )
            await self._fill_first(page, self._phone_selectors(), phone)
            await self._request_sms_code(page)
            await self._wait_for_code_inputs(page)
        except ServiceAuthError:
            await self._close_resources(playwright, browser, context)
            raise
        except Exception as exc:
            await self._close_resources(playwright, browser, context)
            raise ServiceAuthError(
                "Не удалось запросить SMS-код Service"
            ) from exc
        self._pending[challenge_id] = {
            "playwright": playwright,
            "browser": browser,
            "context": context,
            "page": page,
            "phone": phone,
        }

    async def _request_sms_code(self, page) -> None:
        """Submit the phone form and wait for the site's SMS request."""
        try:
            async with page.expect_response(
                lambda response: "/api/v1/auth/sms_confirmation/"
                in response.url,
                timeout=30_000,
            ) as response_info:
                await self._click_first_button(
                    page,
                    ["Войти", "Получить код", "Продолжить"],
                )

            response = await response_info.value
            if response.ok:
                return

            try:
                detail = (await response.text()).strip()[:300]
            except Exception:
                detail = ""
            suffix = f": {detail}" if detail else ""
            raise ServiceAuthError(
                "Внешний сервис отклонил запрос SMS "
                f"(HTTP {response.status}){suffix}"
            )
        except ServiceAuthError:
            raise
        except Exception as exc:
            raise ServiceAuthError(
                "Не удалось дождаться запроса отправки SMS"
            ) from exc

    async def verify_code(self, challenge_id: str, code: str) -> dict:
        pending = self._pending.get(challenge_id)
        if not pending:
            raise ServiceAuthError(
                "Сессия ввода SMS-кода истекла"
            )
        if pending.get("test"):
            expected = self.test_sms_code
            if code != expected:
                raise ServiceAuthError("Неверный SMS-код")
            self._pending.pop(challenge_id, None)
            return {"cookies": [], "origins": [], "test": True}

        page = pending["page"]
        try:
            inputs = await self._wait_for_code_inputs(page)
            if len(inputs) == 1:
                await inputs[0].fill(code)
            elif len(inputs) >= len(code):
                for field, digit in zip(inputs, code):
                    await field.fill(digit)
            else:
                raise ServiceAuthError(
                    "Форма SMS-кода имеет неверный формат"
                )
            await self._click_first_button(
                page,
                ["Войти", "Подтвердить", "Продолжить"],
            )
            await page.wait_for_url(re.compile(r"/lk/"), timeout=30_000)
            state = await pending["context"].storage_state()
        except ServiceAuthError:
            raise
        except Exception as exc:
            raise ServiceAuthError(
                "Неверный SMS-код или вход в Service "
                "не выполнен"
            ) from exc
        self._pending.pop(challenge_id, None)
        await self._close_resources(
            pending["playwright"], pending["browser"], pending["context"]
        )
        return state

    async def discard(self, challenge_id: str) -> None:
        pending = self._pending.pop(challenge_id, None)
        if pending and not pending.get("test"):
            await self._close_resources(
                pending["playwright"], pending["browser"], pending["context"]
            )

    async def _fill_first(self, page, selectors: list[str], value: str) -> None:
        for _ in range(120):
            for selector in selectors:
                locator = page.locator(selector)
                for index in range(await locator.count()):
                    candidate = locator.nth(index)
                    if await candidate.is_visible() and await candidate.is_enabled():
                        await candidate.fill(value)
                        return
            await page.wait_for_timeout(250)
        raise ServiceAuthError("Поле телефона не найдено")

    async def _wait_for_code_inputs(self, page):
        selectors = [
            "input[autocomplete='one-time-code']",
            "input[inputmode='numeric']",
            "input[name*='code' i]",
            "input[placeholder*='код' i]",
        ]
        for _ in range(120):
            for selector in selectors:
                locator = page.locator(selector)
                visible = [
                    locator.nth(index)
                    for index in range(await locator.count())
                    if await locator.nth(index).is_visible()
                ]
                if visible:
                    return visible
            await page.wait_for_timeout(250)
        raise ServiceAuthError("Поле SMS-кода не найдено")

    async def _click_first_button(self, page, names: list[str]) -> None:
        for _ in range(120):
            for name in names:
                button = page.get_by_role(
                    "button", name=re.compile(f"^{re.escape(name)}$", re.IGNORECASE)
                )
                for index in range(await button.count()):
                    candidate = button.nth(index)
                    if await candidate.is_visible() and await candidate.is_enabled():
                        await candidate.click()
                        return
            await page.wait_for_timeout(250)
        raise ServiceAuthError(
            "Кнопка отправки формы не найдена"
        )

    @staticmethod
    def _phone_selectors() -> list[str]:
        return [
            "input[type='tel']",
            "input[inputmode='tel']",
            "input[autocomplete='tel']",
            "input[name*='phone' i]",
            "input[placeholder*='телефон' i]",
            "input[aria-label*='телефон' i]",
            "input:not([type='hidden'])",
        ]

    @staticmethod
    async def _close_resources(playwright, browser, context) -> None:
        try:
            await context.close()
        finally:
            try:
                await browser.close()
            finally:
                await playwright.stop()


class ServiceChatScraper:
    """Runs the existing Scrapy/Playwright spider for one user's session."""

    def __init__(
        self,
        project_root: str | Path | None = None,
        *,
        service_base_url: str | None = None,
        fixture_path: str | Path | None = None,
        sync_timeout_seconds: int | None = None,
        chat_amount_limit: int | None = None,
        message_amount_limit: int | None = None,
        timezone: str | None = None,
    ) -> None:
        self.project_root = Path(project_root or Path(__file__).parents[1])
        self.service_base_url = (
            service_base_url or os.getenv("SERVICE_BASE_URL", "")
        ).strip().rstrip("/")
        if not self.service_base_url:
            raise ValueError("SERVICE_BASE_URL is required")
        self.chat_url = f"{self.service_base_url}/lk/chat"
        configured_fixture = fixture_path or os.getenv(
            "SERVICE_SYNC_FIXTURE_PATH", ""
        )
        self.fixture_path = Path(configured_fixture) if configured_fixture else None
        self.sync_timeout_seconds = sync_timeout_seconds or int(
            os.getenv("SERVICE_SYNC_TIMEOUT_SECONDS", "300")
        )
        self.chat_amount_limit = (
            chat_amount_limit
            if chat_amount_limit is not None
            else self._read_non_negative_int("CHAT_AMOUNT_LIMIT", 20)
        )
        self.message_amount_limit = (
            message_amount_limit
            if message_amount_limit is not None
            else self._read_non_negative_int("MESSAGE_AMOUNT_LIMIT", 50)
        )
        if self.chat_amount_limit < 0:
            raise ValueError("CHAT_AMOUNT_LIMIT must be non-negative")
        if self.message_amount_limit < 0:
            raise ValueError("MESSAGE_AMOUNT_LIMIT must be non-negative")
        self.timezone = ZoneInfo(
            timezone or os.getenv("SERVICE_TIMEZONE", "Europe/Moscow")
        )

    def sync(
        self,
        session: dict,
        *,
        chat_amount_limit: int | None = None,
        message_amount_limit: int | None = None,
    ) -> list[dict]:
        chat_limit = (
            self.chat_amount_limit
            if chat_amount_limit is None
            else chat_amount_limit
        )
        message_limit = (
            self.message_amount_limit
            if message_amount_limit is None
            else message_amount_limit
        )
        if chat_limit < 0:
            raise ValueError("chat_amount_limit must be non-negative")
        if message_limit < 0:
            raise ValueError("message_amount_limit must be non-negative")

        if self.fixture_path:
            return self._load_payloads(
                self.fixture_path,
                self.timezone,
                chat_limit=chat_limit,
                message_limit=message_limit,
                source_default=self.chat_url,
            )

        with tempfile.TemporaryDirectory(prefix="service-sync-") as directory:
            workdir = Path(directory)
            state_path = workdir / "storage-state.json"
            output_path = workdir / "messages.jsonl"
            state_path.write_text(
                json.dumps(session, ensure_ascii=False),
                encoding="utf-8",
            )
            # Playwright reads a plain JSON state, but it only exists inside
            # this private temporary directory and is removed after the run.
            state_path.chmod(0o600)
            environment = os.environ.copy()
            environment.update(
                {
                    "SERVICE_STORAGE_STATE": str(state_path),
                    "CHAT_AMOUNT_LIMIT": str(chat_limit),
                    "MESSAGE_AMOUNT_LIMIT": str(message_limit),
                    "SERVICE_HEADLESS": "1",
                }
            )
            command = [
                sys.executable,
                "-m",
                "scrapy",
                "crawl",
                "service_chat",
                "-O",
                str(output_path),
            ]
            try:
                completed = subprocess.run(
                    command,
                    cwd=self.project_root,
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=self.sync_timeout_seconds,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise ServiceSyncError(
                    "Не удалось запустить парсер Service"
                ) from exc
            diagnostics = f"{completed.stdout}\n{completed.stderr}".casefold()
            if completed.returncode != 0:
                if "expired" in diagnostics or "unauthorized" in diagnostics:
                    raise ServiceSessionExpired(
                        "Сессия Service истекла"
                    )
                raise ServiceSyncError(
                    "Парсер Service завершился с ошибкой"
                )
            return self._load_payloads(
                output_path,
                self.timezone,
                chat_limit=chat_limit,
                message_limit=message_limit,
                source_default=self.chat_url,
            )

    @staticmethod
    def _read_non_negative_int(name: str, default: int) -> int:
        value = os.getenv(name, str(default))
        try:
            result = int(value)
        except ValueError as exc:
            raise ValueError(f"{name} must be an integer") from exc
        if result < 0:
            raise ValueError(f"{name} must be non-negative")
        return result

    @staticmethod
    def _load_payloads(
        path: Path,
        timezone: ZoneInfo | None = None,
        *,
        chat_limit: int = 0,
        message_limit: int = 0,
        source_default: str = "",
    ) -> list[dict]:
        if not path.is_file():
            raise ServiceSyncError(
                "Парсер не создал файл сообщений"
            )
        raw = path.read_text(encoding="utf-8").strip()
        if not raw:
            return []
        payloads: list[dict] = []
        try:
            decoded = json.loads(raw)
            payloads.extend(decoded if isinstance(decoded, list) else [decoded])
        except json.JSONDecodeError:
            decoder = json.JSONDecoder()
            offset = 0
            while offset < len(raw):
                while offset < len(raw) and raw[offset].isspace():
                    offset += 1
                if offset >= len(raw):
                    break
                try:
                    payload, offset = decoder.raw_decode(raw, offset)
                except json.JSONDecodeError as exc:
                    raise ServiceSyncError(
                        "Парсер создал некорректный JSON"
                    ) from exc
                payloads.extend(payload if isinstance(payload, list) else [payload])
        if chat_limit:
            payloads = payloads[:chat_limit]
        return [
            ServiceChatScraper._normalize_chat(
                payload,
                timezone,
                message_limit=message_limit,
                source_default=source_default,
            )
            for payload in payloads
            if isinstance(payload, dict)
        ]

    @staticmethod
    def _normalize_chat(
        payload: dict,
        timezone: ZoneInfo | None = None,
        *,
        message_limit: int = 0,
        source_default: str = "",
    ) -> dict:
        name = str(payload.get("chat_name") or "Без названия")
        source = str(payload.get("source") or source_default)
        generated_chat_id = hashlib.sha256(
            f"{source}|{name}".encode()
        ).hexdigest()[:20]
        chat_id = str(
            payload.get("chat_id") or f"chat-{generated_chat_id}"
        )
        messages = []
        for index, item in enumerate(payload.get("messages") or []):
            if not isinstance(item, dict):
                continue
            message = str(item.get("message") or "")
            generated_message_id = hashlib.sha256(
                f"{chat_id}|{index}|{message}".encode()
            ).hexdigest()[:20]
            message_id = str(
                item.get("message_id") or f"msg-{generated_message_id}"
            )
            messages.append(
                {
                    "message_id": message_id,
                    "sender": item.get("sender"),
                    "date": _parse_date(item.get("date"), timezone),
                    "message": message,
                    "position": int(item.get("position", index)),
                }
            )
        if message_limit:
            messages = messages[:message_limit]
        return {
            "chat_id": chat_id,
            "chat_index": int(payload.get("chat_index", 0)),
            "chat_name": name,
            "source": source,
            "messages": messages,
        }

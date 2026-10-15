import asyncio
import getpass
import hashlib
import os
import re
import sys
import time
from pathlib import Path

import scrapy
from scrapy.exceptions import CloseSpider
from scrapy.http import Response


class ServiceChatSpider(scrapy.Spider):
    name = "service_chat"
    allowed_domains = ["tetrika-school.ru"]

    login_url = "https://tetrika-school.ru/auth/login"
    chat_url = "https://tetrika-school.ru/lk/chat"

    default_message_selector = (
        "[class^='message_']"
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.phone = os.getenv("SERVICE_PHONE")
        self.sms_code = os.getenv("SERVICE_SMS_CODE")
        self.storage_state = Path(
            os.getenv("SERVICE_STORAGE_STATE", "state/tetrika.json")
        )
        self.message_selector = os.getenv(
            "SERVICE_MESSAGE_SELECTOR", self.default_message_selector
        )
        self.chat_item_selector = os.getenv("SERVICE_CHAT_ITEM_SELECTOR", "").strip()
        try:
            self.chat_limit = int(os.getenv("CHAT_AMOUNT_LIMIT", "20"))
            self.message_amount_limit = int(
                os.getenv("MESSAGE_AMOUNT_LIMIT", "50")
            )
        except ValueError as exc:
            raise CloseSpider(
                "CHAT_AMOUNT_LIMIT and MESSAGE_AMOUNT_LIMIT must be integers."
            ) from exc
        if self.chat_limit < 0 or self.message_amount_limit < 0:
            raise CloseSpider(
                "CHAT_AMOUNT_LIMIT and MESSAGE_AMOUNT_LIMIT must be non-negative."
            )
        self.debug_artifacts = os.getenv("SERVICE_DEBUG_ARTIFACTS", "0") == "1"

    def _initial_request(self):
        if self.storage_state.is_file():
            return self._playwright_request(
                self.chat_url,
                self.parse_chat,
                storage_state=self.storage_state,
            )

        if not self.phone:
            raise CloseSpider(
                "Set SERVICE_PHONE, or provide a saved "
                "SERVICE_STORAGE_STATE file."
            )
        self.phone_form_value = self._normalize_phone_for_form(self.phone)

        return self._playwright_request(self.login_url, self.parse_login)

    async def start(self):
        yield self._initial_request()

    def start_requests(self):
        """Compatibility with Scrapy versions before 2.13."""
        yield self._initial_request()

    def _playwright_request(self, url, callback, storage_state=None):
        meta = {
            "playwright": True,
            "playwright_include_page": True,
            "playwright_page_goto_kwargs": {
                "wait_until": "domcontentloaded",
            },
        }
        if storage_state:
            meta["playwright_context"] = "service-authenticated"
            meta["playwright_context_kwargs"] = {
                "storage_state": str(storage_state),
            }

        return scrapy.Request(
            url,
            callback=callback,
            errback=self.errback_close_page,
            meta=meta,
        )

    async def parse_login(self, response: Response):
        page = response.meta["playwright_page"]
        try:
            await self._fill_first(
                page,
                [
                    "input[type='tel']",
                    "input[inputmode='tel']",
                    "input[autocomplete='tel']",
                    "input[name*='phone' i]",
                    "input[placeholder*='телефон' i]",
                    "input[aria-label*='телефон' i]",
                    "input:not([type='hidden'])",
                ],
                self.phone_form_value,
            )

            await self._click_first_button(
                page,
                ["Войти", "Получить код", "Продолжить"],
            )
            code_inputs = await self._wait_for_code_inputs(
                page,
                [
                    "input[autocomplete='one-time-code']",
                    "input[inputmode='numeric']",
                    "input[name*='code' i]",
                    "input[placeholder*='код' i]",
                ],
            )
            sms_code = await self._get_sms_code()
            if len(code_inputs) == 1:
                await code_inputs[0].fill(sms_code)
            elif len(code_inputs) >= len(sms_code):
                for input_field, digit in zip(code_inputs, sms_code):
                    await input_field.fill(digit)
            else:
                raise CloseSpider(
                    "The SMS code form has fewer fields than the code."
                )
            await self._click_first_button(
                page,
                ["Войти", "Подтвердить", "Продолжить"],
            )
            await page.wait_for_url(re.compile(r"/lk/"), timeout=30_000)

            self.storage_state.parent.mkdir(parents=True, exist_ok=True)
            await page.context.storage_state(path=str(self.storage_state))
            self.logger.info("Saved authenticated session to %s", self.storage_state)

            await page.goto(
                self.chat_url,
                wait_until="domcontentloaded",
                timeout=60_000,
            )
            async for chat in self._collect_recent_chats(page):
                yield chat
        except CloseSpider:
            raise
        except Exception as exc:
            if self.debug_artifacts:
                await self._save_debug_artifacts(page, "login")
            raise CloseSpider(f"Login failed: {exc}") from exc
        finally:
            await page.close()

    async def parse_chat(self, response: Response):
        page = response.meta["playwright_page"]
        try:
            if "/lk/" not in page.url:
                raise CloseSpider(
                    "The saved session is expired or unauthorized. Remove "
                    f"{self.storage_state} and run the spider again."
                )

            async for chat in self._collect_recent_chats(page):
                yield chat
        finally:
            await page.close()

    async def _collect_recent_chats(self, page):
        await self._wait_for_chat_list(page)
        targets = await page.evaluate(
            FIND_CHAT_TARGETS_SCRIPT,
            {"itemSelector": self.chat_item_selector},
        )

        if not targets:
            self.logger.warning(
                "Chat list was not detected; falling back to the currently open chat. "
                "Set SERVICE_DEBUG_ARTIFACTS=1 to inspect the rendered page."
            )
            context = await page.evaluate(GET_CHAT_CONTEXT_SCRIPT)
            chat = await self._collect_chat(
                page,
                chat_index=0,
                chat_id=context.get("id") or self._stable_chat_id(context.get("name")),
                chat_name=context.get("name"),
                allow_empty=False,
            )
            if chat:
                yield chat
            return

        target_count = (
            len(targets)
            if self.chat_limit == 0
            else min(len(targets), self.chat_limit)
        )
        self.logger.info(
            "Detected %s chats; collecting the first %s.",
            len(targets),
            target_count,
        )

        for index in range(target_count):
            targets = await page.evaluate(
                FIND_CHAT_TARGETS_SCRIPT,
                {"itemSelector": self.chat_item_selector},
            )
            if index >= len(targets):
                self.logger.warning(
                    "Chat list changed while scraping; stopped at %s chats.", index
                )
                break

            target_name = targets[index].get("name")
            target_id = targets[index].get("id")
            clicked = await page.evaluate(
                CLICK_CHAT_TARGET_SCRIPT,
                {
                    "itemSelector": self.chat_item_selector,
                    "index": index,
                },
            )
            if not clicked:
                self.logger.warning("Could not click chat list item %s.", index)
                continue

            await self._wait_for_chat_content(page)
            context = await page.evaluate(GET_CHAT_CONTEXT_SCRIPT)
            current_name = context.get("name")
            current_id = context.get("id")
            if target_name and current_name and target_name != current_name:
                self.logger.debug(
                    "Chat item %s is %r; page reports %r.",
                    index,
                    target_name,
                    current_name,
                )

            self.logger.info(
                "Collecting chat %s/%s: %s",
                index + 1,
                target_count,
                current_name or target_name or "unknown",
            )
            chat = await self._collect_chat(
                page,
                chat_index=index,
                chat_id=(
                    current_id
                    or target_id
                    or self._stable_chat_id(current_name or target_name)
                ),
                chat_name=current_name or target_name,
                allow_empty=True,
            )
            if chat:
                yield chat

    async def _collect_chat(
        self,
        page,
        *,
        chat_index,
        chat_id,
        chat_name,
        allow_empty,
    ):
        messages = []
        async for message in self._collect_messages(
            page,
            chat_index=chat_index,
            chat_id=chat_id,
            chat_name=chat_name,
            allow_empty=allow_empty,
        ):
            messages.append(message)
            if (
                self.message_amount_limit
                and len(messages) >= self.message_amount_limit
            ):
                break

        if not messages:
            return None

        return {
            "chat_id": chat_id,
            "chat_index": chat_index,
            "chat_name": chat_name,
            "source": self.chat_url,
            "messages": messages,
        }

    async def _wait_for_chat_list(self, page):
        try:
            await page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            self.logger.debug("Chat did not reach networkidle; continuing.")
        await page.wait_for_timeout(2_000)

    async def _wait_for_chat_content(self, page):
        try:
            await page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception:
            self.logger.debug("Chat switch did not reach networkidle; continuing.")
        await page.wait_for_timeout(1_000)

    async def _collect_messages(
        self,
        page,
        *,
        chat_index=0,
        chat_id=None,
        chat_name=None,
        allow_empty=False,
    ):
        try:
            await page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            self.logger.debug("Chat did not reach networkidle; continuing.")

        await page.wait_for_timeout(2_000)
        messages = await page.evaluate(
            EXTRACT_MESSAGES_SCRIPT,
            {
                "selector": self.message_selector,
                "chatIndex": chat_index,
                "chatId": chat_id,
                "chatName": chat_name,
                "messageLimit": self.message_amount_limit,
            },
        )

        if not messages:
            if self.debug_artifacts:
                await self._save_debug_artifacts(page, f"chat-{chat_index}")
            if allow_empty:
                self.logger.info("Chat %s contains no detected messages.", chat_index)
                return
            raise CloseSpider(
                "No messages detected. Set SERVICE_DEBUG_ARTIFACTS=1 and "
                "inspect artifacts/chat-0.html for selector adjustments."
            )

        for message in messages:
            yield message

    @staticmethod
    def _normalize_phone_for_form(phone):
        digits = re.sub(r"\D", "", phone)
        if len(digits) == 11 and digits[0] in {"7", "8"}:
            digits = digits[1:]
        if not 10 <= len(digits) <= 15:
            raise CloseSpider(
                "SERVICE_PHONE must contain a valid phone number with 10 to 15 digits."
            )
        return digits

    @staticmethod
    def _stable_chat_id(name):
        value = (name or "unknown").strip().encode("utf-8")
        return "chat-" + hashlib.sha256(value).hexdigest()[:20]

    async def _fill_first(self, page, selectors, value, timeout_ms=30_000):
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            for selector in selectors:
                locator = page.locator(selector)
                for index in range(await locator.count()):
                    candidate = locator.nth(index)
                    if await candidate.is_visible() and await candidate.is_enabled():
                        await candidate.fill(value)
                        return

            # Some builds expose the phone control only through its accessible role.
            textboxes = page.get_by_role("textbox")
            for index in range(await textboxes.count()):
                candidate = textboxes.nth(index)
                if await candidate.is_visible() and await candidate.is_enabled():
                    await candidate.fill(value)
                    return

            await page.wait_for_timeout(250)

        raise CloseSpider(f"Input not found for selectors: {selectors}")

    async def _wait_for_code_inputs(self, page, selectors, timeout_ms=30_000):
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            for selector in selectors:
                locator = page.locator(selector)
                visible = []
                for index in range(await locator.count()):
                    candidate = locator.nth(index)
                    if await candidate.is_visible():
                        visible.append(candidate)
                if visible:
                    return visible
            await page.wait_for_timeout(250)
        raise CloseSpider(f"SMS code input did not appear: {selectors}")

    async def _click_first_button(self, page, names, timeout_ms=30_000):
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
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

        raise CloseSpider(f"Submit button not found: {names}")

    async def _get_sms_code(self):
        if self.sms_code:
            code = self.sms_code.strip()
        elif sys.stdin.isatty():
            code = await asyncio.to_thread(
                getpass.getpass, "Enter the Service SMS code: "
            )
        else:
            raise CloseSpider(
                "Set SERVICE_SMS_CODE for non-interactive login."
            )

        if not re.fullmatch(r"\d{4,8}", code):
            raise CloseSpider("SERVICE_SMS_CODE must contain 4 to 8 digits.")
        return code

    async def _save_debug_artifacts(self, page, name):
        artifact_dir = Path("artifacts")
        artifact_dir.mkdir(parents=True, exist_ok=True)
        (artifact_dir / f"{name}.html").write_text(
            await page.content(), encoding="utf-8"
        )
        await page.screenshot(
            path=str(artifact_dir / f"{name}.png"), full_page=True
        )

    async def errback_close_page(self, failure):
        page = failure.request.meta.get("playwright_page")
        if page:
            await page.close()
        self.logger.error("Request failed: %s", failure.getErrorMessage())


FIND_CHAT_TARGETS_SCRIPT = r"""
({itemSelector}) => {
    const textOf = (element) => (element?.innerText || "").trim();
    const isVisible = (element) => {
        if (!element) return false;
        const rect = element.getBoundingClientRect();
        const style = window.getComputedStyle(element);
        return (
            rect.width > 0 &&
            rect.height > 0 &&
            style.display !== "none" &&
            style.visibility !== "hidden"
        );
    };
    const hasClassPrefix = (element, prefix) =>
        Array.from(element?.classList || []).some((name) =>
            name.startsWith(prefix)
        );
    const chatSection = document.querySelector("section[class^='chat_']");
    const names = itemSelector
        ? Array.from(document.querySelectorAll(itemSelector))
        : Array.from(document.querySelectorAll("[class^='name_']"));
    const candidates = [];
    const seen = new Set();

    for (const nameElement of names) {
        if (!isVisible(nameElement)) continue;
        if (chatSection?.contains(nameElement)) continue;

        let candidate = nameElement;
        for (let depth = 0; candidate && depth < 7; depth += 1) {
            const className = Array.from(candidate.classList || [])
                .join(" ")
                .toLowerCase();
            const tagName = candidate.tagName.toLowerCase();
            const interactive =
                tagName === "a" ||
                tagName === "button" ||
                candidate.getAttribute("role") === "button" ||
                candidate.hasAttribute("data-chat-id");
            const likelyChatItem =
                /chat|dialog|conversation|contact|teacher|student|item/.test(
                    className
                );
            const nestedNames = candidate.querySelectorAll("[class^='name_']").length;

            if (
                candidate !== nameElement &&
                (interactive || likelyChatItem) &&
                nestedNames <= 1
            ) {
                break;
            }
            candidate = candidate.parentElement;
        }

        if (!candidate || !isVisible(candidate) || seen.has(candidate)) continue;
        if (chatSection?.contains(candidate)) continue;
        if (candidate.querySelectorAll("[class^='name_']").length > 1) continue;

        const name = textOf(
            candidate.matches("[class^='name_']")
                ? candidate
                : candidate.querySelector("[class^='name_']") || nameElement
        );
        if (!name) continue;

        const id =
            candidate.getAttribute("data-chat-id") ||
            candidate.getAttribute("data-dialog-id") ||
            candidate.getAttribute("data-id") ||
            nameElement.getAttribute("data-chat-id") ||
            nameElement.getAttribute("data-dialog-id") ||
            nameElement.getAttribute("data-id") ||
            candidate.getAttribute("href") ||
            null;

        seen.add(candidate);
        candidates.push({
            id,
            name,
            text: textOf(candidate),
        });
    }

    return candidates;
}
""";

CLICK_CHAT_TARGET_SCRIPT = r"""
({itemSelector, index}) => {
    const isVisible = (element) => {
        if (!element) return false;
        const rect = element.getBoundingClientRect();
        const style = window.getComputedStyle(element);
        return (
            rect.width > 0 &&
            rect.height > 0 &&
            style.display !== "none" &&
            style.visibility !== "hidden"
        );
    };
    const chatSection = document.querySelector("section[class^='chat_']");
    const names = itemSelector
        ? Array.from(document.querySelectorAll(itemSelector))
        : Array.from(document.querySelectorAll("[class^='name_']"));
    const candidates = [];
    const seen = new Set();

    for (const nameElement of names) {
        if (!isVisible(nameElement)) continue;
        if (chatSection?.contains(nameElement)) continue;

        let candidate = nameElement;
        for (let depth = 0; candidate && depth < 7; depth += 1) {
            const className = Array.from(candidate.classList || [])
                .join(" ")
                .toLowerCase();
            const tagName = candidate.tagName.toLowerCase();
            const interactive =
                tagName === "a" ||
                tagName === "button" ||
                candidate.getAttribute("role") === "button" ||
                candidate.hasAttribute("data-chat-id");
            const likelyChatItem =
                /chat|dialog|conversation|contact|teacher|student|item/.test(
                    className
                );
            const nestedNames = candidate.querySelectorAll("[class^='name_']").length;

            if (
                candidate !== nameElement &&
                (interactive || likelyChatItem) &&
                nestedNames <= 1
            ) {
                break;
            }
            candidate = candidate.parentElement;
        }

        if (!candidate || !isVisible(candidate) || seen.has(candidate)) continue;
        if (chatSection?.contains(candidate)) continue;
        if (candidate.querySelectorAll("[class^='name_']").length > 1) continue;
        seen.add(candidate);
        candidates.push(candidate);
    }

    const target = candidates[index];
    if (!target) return false;
    target.scrollIntoView({block: "center", inline: "nearest"});
    target.click();
    return true;
}
""";

GET_CHAT_CONTEXT_SCRIPT = r"""
() => {
    const textOf = (element) => (element?.innerText || "").trim();
    const chatSection = document.querySelector("section[class^='chat_']");
    const nameElement = chatSection?.querySelector("[class^='name_']");
    const idElement = chatSection?.closest(
        "[data-chat-id], [data-dialog-id], [data-id]"
    );
    return {
        id:
            idElement?.getAttribute("data-chat-id") ||
            idElement?.getAttribute("data-dialog-id") ||
            idElement?.getAttribute("data-id") ||
            null,
        name: textOf(nameElement) || null,
    };
}
""";

EXTRACT_MESSAGES_SCRIPT = r"""
({selector, chatIndex, chatId, chatName, messageLimit}) => {
    const textOf = (element) => (element?.innerText || "").trim();
    const hasClassPrefix = (element, prefix) =>
        Array.from(element?.classList || []).some((name) =>
            name.startsWith(prefix)
        );
    const rootSelector = selector || "[class^='message_']";
    const roots = Array.from(document.querySelectorAll(rootSelector)).filter(
        (element) =>
            hasClassPrefix(element, "message_") &&
            !hasClassPrefix(element, "message-wrap_") &&
            !hasClassPrefix(element, "message-text_")
    );
    const chatSection = document.querySelector("section[class^='chat_']");
    const chatHeaderName = chatSection?.querySelector("[class^='name_']");
    const peerName = textOf(chatHeaderName) || null;
    const hash = (value) => {
        let result = 2166136261;
        for (const character of value) {
            result ^= character.charCodeAt(0);
            result = Math.imul(result, 16777619);
        }
        return (result >>> 0).toString(16);
    };

    const messages = roots.flatMap((root, index) => {
        const messageElement = root.querySelector("[class^='message-text_']");
        const timeElement = root.querySelector("[class^='time_']");
        const wrapper = root.closest("[class^='message-wrap_']");
        let dateElement = wrapper?.previousElementSibling || null;
        while (dateElement && !hasClassPrefix(dateElement, "date-title_")) {
            dateElement = dateElement.previousElementSibling;
        }

        const text = textOf(messageElement || root);
        const time = textOf(timeElement);
        const day = textOf(dateElement);
        if (!text) return [];

        const isIncoming = hasClassPrefix(root, "sender-message_");
        const sender = isIncoming ? peerName : "Я";
        const date = [day, time].filter(Boolean).join(" ") || null;
        const messageId =
            "chat-" +
            (chatId || chatIndex || 0) + "-" +
            hash((sender || "") + "|" + (date || "") + "|" + text);

        return [{
            message_id: messageId,
            sender,
            date,
            message: text,
            position: index,
        }];
    });
    return messageLimit > 0 ? messages.slice(0, messageLimit) : messages;
}
""";

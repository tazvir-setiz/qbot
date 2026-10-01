"""Optional AI assistant for Telegram groups.

The assistant is intentionally provider-agnostic: any OpenAI-compatible chat-completions
endpoint can be configured from the admin panel. Secrets are never logged.
"""
from __future__ import annotations

import json
import logging
import re
import time
from collections import defaultdict, deque
from dataclasses import dataclass

import httpx
from telegram.constants import ChatAction
from telegram.error import TelegramError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AIReply:
    text: str = ""
    media_key: str | None = None


class AIService:
    def __init__(self, repo):
        self.repo = repo
        self._history: dict[int, deque[tuple[str, str]]] = defaultdict(lambda: deque(maxlen=8))
        self._last_request: dict[tuple[int, int], float] = {}
        self._bot_username: str | None = None
        self._bot_id: int | None = None

    async def initialize(self, bot):
        me = await bot.get_me()
        self._bot_id = me.id
        self._bot_username = (me.username or "").lower()

    def _is_triggered(self, message) -> bool:
        text = (message.text or message.caption or "")
        mentioned = bool(self._bot_username and f"@{self._bot_username}" in text.lower())
        replied = bool(
            message.reply_to_message
            and message.reply_to_message.from_user
            and self._bot_id is not None
            and message.reply_to_message.from_user.id == self._bot_id
        )
        return mentioned or replied

    def _question(self, message) -> str:
        text = (message.text or message.caption or "").strip()
        if self._bot_username:
            text = re.sub(rf"@{re.escape(self._bot_username)}\b", "", text, flags=re.IGNORECASE).strip()
        if not text and message.reply_to_message:
            text = (message.reply_to_message.text or message.reply_to_message.caption or "").strip()
        return text

    @staticmethod
    def _endpoint(base_url: str) -> str:
        url = (base_url or "").strip().rstrip("/")
        if url.endswith("/chat/completions"):
            return url
        return url + "/chat/completions"

    @staticmethod
    def _parse_reply(content: str) -> AIReply:
        raw = (content or "").strip()
        if not raw:
            return AIReply()
        candidate = raw
        if "```" in raw:
            match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL | re.IGNORECASE)
            if match:
                candidate = match.group(1)
        try:
            data = json.loads(candidate)
            if isinstance(data, dict):
                return AIReply(
                    text=str(data.get("text") or "").strip(),
                    media_key=str(data.get("media_key") or "").strip() or None,
                )
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
        return AIReply(text=raw)

    def _messages(self, chat_id: int, question: str, system_prompt: str, media: list[dict]) -> list[dict]:
        media_catalog = "\n".join(
            f"- key={item['media_key']} | type={item['media_type']} | {item.get('description') or 'بدون توضیح'}"
            for item in media
        ) or "(هیچ رسانه‌ای ثبت نشده)"
        system = (
            (system_prompt or "تو دستیار دوستانه و دقیق یک گروه تلگرامی فارسی‌زبان هستی.").strip()
            + "\n\nپاسخ را کوتاه، کاربردی و طبیعی بنویس. از ادعای قطعی بدون اطمینان خودداری کن."
            + "\nاگر یکی از رسانه‌های زیر واقعاً به پاسخ کمک می‌کند، media_key همان مورد را انتخاب کن؛ در غیر این صورت null."
            + "\nفقط JSON معتبر با این ساختار برگردان: "
              '{"text":"متن پاسخ","media_key":null}'
            + "\nرسانه‌های قابل استفاده:\n" + media_catalog
        )
        messages = [{"role": "system", "content": system}]
        for q, a in self._history[chat_id]:
            messages.extend(({"role": "user", "content": q}, {"role": "assistant", "content": a}))
        messages.append({"role": "user", "content": question})
        return messages

    async def _complete(self, row: dict, messages: list[dict]) -> AIReply:
        headers = {"Authorization": f"Bearer {row['ai_api_key']}", "Content-Type": "application/json"}
        payload = {
            "model": row["ai_model"],
            "messages": messages,
            "temperature": 0.7,
        }
        timeout = httpx.Timeout(45.0, connect=15.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(self._endpoint(row["ai_base_url"]), headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError("AI provider returned an unexpected response") from exc
        return self._parse_reply(content)

    async def _send_media(self, message, media: dict, text: str) -> bool:
        media_type = media["media_type"]
        file_id = media["file_id"]
        caption = text[:1024] if text and media_type in {"photo", "animation"} else None
        if media_type == "photo":
            await message.reply_photo(photo=file_id, caption=caption)
        elif media_type == "animation":
            await message.reply_animation(animation=file_id, caption=caption)
        elif media_type == "sticker":
            await message.reply_sticker(sticker=file_id)
        else:
            return False
        if text and not caption:
            await message.reply_text(text)
        return True

    async def handle_group_message(self, update, context):
        message = update.effective_message
        chat = update.effective_chat
        user = update.effective_user
        if not message or not chat or not user or user.is_bot or not self._is_triggered(message):
            return

        row = await self.repo.find_ai_owner(chat.id)
        if not row:
            return

        question = self._question(message)
        if not question:
            await message.reply_text("سؤالت را کنار منشن من بنویس 🙂")
            return

        key = (chat.id, user.id)
        now = time.monotonic()
        if now - self._last_request.get(key, 0) < 3:
            return
        self._last_request[key] = now

        media = await self.repo.list_ai_media(row["chat_id"])
        try:
            await context.bot.send_chat_action(chat_id=chat.id, action=ChatAction.TYPING)
            reply = await self._complete(
                row,
                self._messages(chat.id, question, row.get("ai_system_prompt") or "", media),
            )
            if not reply.text and not reply.media_key:
                return
            selected = next((item for item in media if item["media_key"] == reply.media_key), None)
            sent_media = False
            if selected:
                sent_media = await self._send_media(message, selected, reply.text)
            if reply.text and not sent_media:
                await message.reply_text(reply.text)
            self._history[chat.id].append((question, reply.text or "[media]"))
        except (httpx.HTTPError, ValueError, TelegramError) as exc:
            logger.warning("AI reply failed for chat %s (%s)", chat.id, type(exc).__name__)
            try:
                await message.reply_text("الان نتونستم از هوش مصنوعی جواب بگیرم؛ کمی بعد دوباره امتحان کن.")
            except TelegramError:
                pass

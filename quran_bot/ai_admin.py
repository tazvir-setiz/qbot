"""Admin panel integration for the optional group AI assistant."""
from __future__ import annotations

from html import escape

from .menus import keyboard, render


class AIAdmin:
    def __init__(self, repo):
        self.repo = repo

    async def panel(self, update, *, notice=None, edit=False):
        owner_id = update.effective_chat.id
        row = await self.repo.get(owner_id) or {}
        media = await self.repo.list_ai_media(owner_id)
        enabled = bool(row.get("ai_enabled"))
        ready = all((row.get("destination_id"), row.get("ai_base_url"), row.get("ai_api_key"), row.get("ai_model")))
        key_status = "تنظیم شده" if row.get("ai_api_key") else "تنظیم نشده"
        text = (
            "<b>🤖 دستیار هوش مصنوعی گروه</b>\n\n"
            + ("🟢 فعال" if enabled else "⚪ غیرفعال")
            + (" · آمادهٔ پاسخ‌گویی" if ready else " · تنظیمات ناقص")
            + "\n\n👥 گروه: " + escape(row.get("destination_title") or str(row.get("destination_id") or "انتخاب نشده"))
            + "\n🌐 API: " + escape(row.get("ai_base_url") or "تنظیم نشده")
            + "\n🧠 مدل: " + escape(row.get("ai_model") or "تنظیم نشده")
            + "\n🔑 کلید API: " + key_status
            + f"\n🎞 رسانه‌های ثبت‌شده: {len(media)}"
            + "\n\nوقتی فعال باشد، بات فقط وقتی در گروه منشن شود یا کاربر روی پاسخ قبلی بات ریپلای کند جواب می‌دهد."
        )
        if notice:
            text += "\n\n" + escape(notice)
        toggle = ("⏹ خاموش کردن AI", "ai_disable", "danger") if enabled else ("▶️ فعال کردن AI", "ai_enable", "success")
        await render(update.effective_message, text, keyboard([
            toggle,
            ("🌐 API Endpoint", "ai_set_url"), ("🔑 API Key", "ai_set_key"),
            ("🧠 مدل", "ai_set_model"), ("📝 شخصیت / پرامپت", "ai_set_prompt"),
            ("➕ افزودن عکس/GIF/استیکر", "ai_add_media"), ("🗂 رسانه‌ها", "ai_media_list"),
            ("‹ خانه", "menu_back"),
        ], 2), edit=edit)

    async def begin(self, update, context, data):
        prompts = {
            "ai_set_url": ("ai_url", "آدرس پایهٔ API سازگار با OpenAI را بفرستید؛ نمونه: https://example.com/v1"),
            "ai_set_key": ("ai_key", "API Key را بفرستید. این مقدار در پیام‌های پنل نمایش داده نمی‌شود."),
            "ai_set_model": ("ai_model", "نام دقیق مدل را بفرستید؛ همان مقداری که سرویس‌دهنده اعلام کرده است."),
            "ai_set_prompt": ("ai_prompt", "پرامپت شخصیت و سبک پاسخ‌گویی بات را بفرستید."),
            "ai_add_media": ("ai_media", "یک عکس، GIF یا استیکر بفرستید. برای عکس/GIF کپشن را توضیح کاربرد آن بنویسید."),
        }
        state, prompt = prompts[data]
        context.user_data["state"] = state
        await render(update.effective_message, "<b>🤖 تنظیم AI</b>\n" + escape(prompt) + "\n\nلغو: /cancel",
                     keyboard([("‹ تنظیمات AI", "menu_ai")]), edit=True)

    async def receive(self, update, context):
        state = context.user_data.get("state")
        if not state or not state.startswith("ai_"):
            return False
        message = update.effective_message
        owner_id = update.effective_chat.id
        text = (message.text or "").strip()
        if state == "ai_url":
            if not (text.startswith("https://") or text.startswith("http://")):
                await message.reply_text("❌ آدرس باید با http:// یا https:// شروع شود.")
                return True
            await self.repo.update(owner_id, ai_base_url=text.rstrip("/"))
        elif state == "ai_key":
            if len(text) < 8:
                await message.reply_text("❌ کلید API خیلی کوتاه است.")
                return True
            await self.repo.update(owner_id, ai_api_key=text)
        elif state == "ai_model":
            if not text or len(text) > 200:
                await message.reply_text("❌ نام مدل نامعتبر است.")
                return True
            await self.repo.update(owner_id, ai_model=text)
        elif state == "ai_prompt":
            if not text or len(text) > 4000:
                await message.reply_text("❌ پرامپت باید بین ۱ تا ۴۰۰۰ نویسه باشد.")
                return True
            await self.repo.update(owner_id, ai_system_prompt=text)
        elif state == "ai_media":
            media_type = None
            file_id = None
            if message.photo:
                media_type, file_id = "photo", message.photo[-1].file_id
            elif message.animation:
                media_type, file_id = "animation", message.animation.file_id
            elif message.sticker:
                media_type, file_id = "sticker", message.sticker.file_id
            if not media_type:
                await message.reply_text("❌ فقط عکس، GIF یا استیکر بفرستید.")
                return True
            existing = await self.repo.list_ai_media(owner_id)
            media_key = f"media_{len(existing) + 1}"
            description = (message.caption or "").strip() or ("استیکر مناسب گفتگو" if media_type == "sticker" else "رسانه مناسب گفتگو")
            await self.repo.save_ai_media(owner_id, media_key, media_type, file_id, description[:500])
        context.user_data.pop("state", None)
        await self.panel(update, notice="✅ تنظیم AI ذخیره شد.")
        return True

    async def callback(self, update, context, data):
        owner_id = update.effective_chat.id
        if data == "menu_ai":
            await self.panel(update, edit=True)
            return True
        if data in {"ai_set_url", "ai_set_key", "ai_set_model", "ai_set_prompt", "ai_add_media"}:
            await self.begin(update, context, data)
            return True
        if data in {"ai_enable", "ai_disable"}:
            row = await self.repo.get(owner_id) or {}
            if data == "ai_enable":
                missing = []
                if not row.get("destination_id"): missing.append("گروه")
                if not row.get("ai_base_url"): missing.append("API")
                if not row.get("ai_api_key"): missing.append("API Key")
                if not row.get("ai_model"): missing.append("مدل")
                if missing:
                    await self.panel(update, notice="قبل از فعال‌سازی این موارد را تکمیل کنید: " + "، ".join(missing), edit=True)
                    return True
                await self.repo.update(owner_id, ai_enabled=1)
                await self.panel(update, notice="✅ AI فعال شد. حالا در گروه بات را منشن کنید.", edit=True)
            else:
                await self.repo.update(owner_id, ai_enabled=0)
                await self.panel(update, notice="AI خاموش شد.", edit=True)
            return True
        if data == "ai_media_list":
            media = await self.repo.list_ai_media(owner_id)
            if not media:
                await self.panel(update, notice="هنوز رسانه‌ای ثبت نشده است.", edit=True)
                return True
            items = [(f"🗑 {item['media_key']}", f"ai_del_media:{item['media_key']}", "danger") for item in media[:20]]
            await render(update.effective_message, "<b>🗂 رسانه‌های AI</b>\nبرای حذف روی مورد بزنید.\n\n" +
                         "\n".join(f"{escape(item['media_key'])} — {escape(item.get('description') or '')}" for item in media[:20]),
                         keyboard(items + [("‹ تنظیمات AI", "menu_ai")], 1), edit=True)
            return True
        if data.startswith("ai_del_media:"):
            await self.repo.delete_ai_media(owner_id, data.split(":", 1)[1])
            await self.panel(update, notice="رسانه حذف شد.", edit=True)
            return True
        return False

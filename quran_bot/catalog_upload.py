"""Authenticated, previewed catalog replacement. No uploaded file is executed."""
from html import escape
from io import BytesIO
import secrets
import time

from .catalog import export_surahs, parse_surahs, validate_replacement
from .menus import keyboard, render

MAX_BYTES = 64 * 1024


class CatalogUpload:
    def __init__(self, handlers):
        self.handlers = handlers

    async def receive(self, update, context):
        message = update.effective_message
        context.user_data.pop("pending_catalog", None)
        document = getattr(message, "document", None)
        if not document or not (document.file_name or "").lower().endswith(".txt"):
            await message.reply_text("فایل متنی .txt را به‌صورت File ارسال کنید؛ فرمت هر سطر: نام سوره|صفحه شروع|صفحه پایان")
            return
        if document.file_size is None or not 0 < document.file_size <= MAX_BYTES:
            await message.reply_text("اندازهٔ فایل باید مشخص و بین ۱ بایت تا ۶۴ کیلوبایت باشد.")
            return
        file = await document.get_file()
        content = await file.download_as_bytearray()
        try:
            if len(content) > MAX_BYTES:
                raise ValueError("فایل بزرگ‌تر از ۶۴ کیلوبایت است.")
            rows = parse_surahs(content.decode("utf-8-sig"))
            validate_replacement(rows, self.handlers.surahs)
        except UnicodeDecodeError:
            await message.reply_text("کدگذاری فایل باید UTF-8 باشد؛ فایل الگو را ویرایش کنید.")
            return
        except ValueError as exc:
            await message.reply_text("❌ " + str(exc))
            return
        nonce = secrets.token_hex(8)
        context.user_data["pending_catalog"] = dict(rows=rows, nonce=nonce,
            revision=self.handlers.service.catalog_revision, expires=time.monotonic() + 600)
        lines = [f"{i + 1}. {escape(s['name'])} — {s['start_page']} تا {s['end_page']}" for i, s in enumerate(rows[:8])]
        text = (f"<b>پیش‌نمایش فهرست جدید · {len(rows)} سوره</b>\n" + "\n".join(lines)
                + ("\n…" if len(rows) > 8 else "")
                + "\n\nترتیب ارسال دقیقاً ترتیب سطرهای فایل خواهد بود."
                + "\nاین تغییر برای همهٔ مدیران است. همهٔ برنامه‌ها متوقف و وضعیت پایان‌یافته پاک می‌شود."
                + "\nسوره با نام تطبیق داده می‌شود؛ صفحهٔ خارج از بازه به شروع جدید و روز آن به ۱ برمی‌گردد."
                + "\nپس از بررسی، برنامه را دوباره فعال کنید. تأیید تا ۱۰ دقیقه معتبر است.")
        await render(message, text, keyboard([("✅ اعمال فهرست", f"catalog_confirm:{nonce}", "success"),
                                              ("لغو", "menu_back")], 2))

    async def callback(self, update, context, data):
        if data == "menu_catalog":
            context.user_data.pop("pending_catalog", None)
            await render(update.effective_message,
                "<b>📂 فهرست سوره‌ها</b>\nفایل فعلی را بگیرید، سطرها و شمارهٔ صفحات را ویرایش و فایل کامل را ارسال کنید."
                + "\nفرمت UTF-8 / TXT، حداکثر ۶۴ کیلوبایت:\n<code>فاتحه|1|1\nبقره|2|49</code>"
                + "\nاعداد فارسی نیز پذیرفته می‌شوند. نام سوره‌ها نباید حذف یا تکرار شود."
                + "\nاین فهرست بین همهٔ مدیران مشترک است و تنها بعد از تأیید اعمال می‌شود.",
                keyboard([("📥 دریافت فایل فعلی / الگو", "catalog_template"),
                          ("📤 ارسال فایل جدید", "catalog_receive", "primary"), ("‹ خانه", "menu_back")]), edit=True)
        elif data == "catalog_template":
            output = BytesIO(export_surahs(self.handlers.surahs).encode("utf-8"))
            output.name = "surah-list.txt"
            await update.effective_message.reply_document(document=output, filename=output.name,
                caption="فایل کامل فعلی؛ ترتیب سطرها = ترتیب مطالعه. پس از ویرایش «ارسال فایل جدید» را بزنید.")
        elif data == "catalog_receive":
            context.user_data.pop("pending_catalog", None)
            context.user_data["state"] = "catalog_upload"
            await render(update.effective_message, "فایل کامل .txt با کدگذاری UTF-8 را ارسال کنید.\nلغو: /cancel",
                         keyboard([("‹ فهرست سوره‌ها", "menu_catalog")]), edit=True)
        else:
            pending = context.user_data.get("pending_catalog")
            if not pending or pending["nonce"] != data.split(":", 1)[1] or pending["expires"] < time.monotonic():
                await self.handlers.panel(update, notice="تأیید منقضی شده یا قبلاً استفاده شده؛ فایل را دوباره ارسال کنید.", edit=True)
                return
            await self.handlers.service.replace_catalog_locked(pending["rows"], pending["revision"])
            context.user_data.pop("pending_catalog", None)
            await self.handlers.panel(update, daily=True, notice="✅ فهرست جدید ذخیره شد. برنامه‌ها متوقف‌اند؛ سوره و صفحه را بررسی و شروع ارسال را بزنید.", edit=True)

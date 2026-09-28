import hmac
import logging
import time
import secrets
from html import escape
from datetime import datetime

import pytz
from telegram import KeyboardButton, KeyboardButtonRequestChat, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.error import Conflict, TelegramError

from .catalog import normalize_name
from .catalog_upload import CatalogUpload
from .destination import error_text
from .formatting import build_daily_message, jalali_now, to_persian_number
from .menus import (HELP, INTERVALS, TIMES, back_markup, keyboard, presets,
                    preview_markup, render, show_menu, surah_picker)

logger = logging.getLogger(__name__)
PROMPTS = {
    "menu_set_interval": ("interval", "دوره را به شکل روز:ساعت:دقیقه:ثانیه وارد کنید؛ مثال 1:0:0:0. مقدار باید بیشتر از صفر باشد."),
    "menu_set_time": ("time", "ساعت اولین ارسال را به شکل HH:MM وارد کنید؛ مثال 09:30."),
    "menu_set_day": ("day", "شماره روز را وارد کنید؛ عدد مثبت."),
    "menu_choose_page": ("page", "شماره صفحه را وارد کنید."),
    "menu_choose_start": ("surah", "نام سوره را مطابق فهرست وارد کنید."),
    "menu_send_custom": ("media", "پیام، عکس، ویدئو، فایل یا رسانه را بفرستید. برای لغو /cancel را بزنید."),
}


class Handlers:
    def __init__(self, config, repo, service):
        self.config, self.repo, self.service = config, repo, service
        self.surahs = service.surahs
        self.attempts = {}
        self.catalog_upload = CatalogUpload(self)

    def validate_catalog_button(self, revision):
        if int(revision) != self.service.catalog_revision:
            raise ValueError("فهرست تغییر کرده؛ دوباره از منوی جدید سوره را انتخاب کنید.")

    async def panel(self, update, daily=False, notice=None, edit=False):
        row = await self.repo.get(update.effective_chat.id)
        if row is not None:
            row["destination_id"] = self.service.destination.from_row(row)
            if update.effective_chat.id in self.service.storage_errors:
                row.update(active=0, last_error=self.service.storage_errors[update.effective_chat.id])
        await show_menu(update.effective_message, daily=daily, row=row, surahs=self.surahs,
                        timezone=self.config.timezone, edit=edit, notice=notice)

    async def clear_group_picker(self, update, context):
        if context.user_data.pop("group_request", None) is not None:
            await update.effective_message.reply_text("انتخاب گروه بسته شد.", reply_markup=ReplyKeyboardRemove())

    async def group(self, update, context):
        context.user_data.pop("pending_catalog", None)
        if not self.allowed(update):
            return
        if not context.user_data.get("authenticated"):
            await update.effective_message.reply_text("برای تنظیم گروه ابتدا /start را بزنید و وارد شوید.")
            return
        await self.clear_group_picker(update, context)
        context.user_data.pop("state", None)
        context.user_data.pop("pending_media", None)
        await self.group_panel(update)

    async def group_panel(self, update, notice=None, edit=False):
        row = await self.repo.get(update.effective_chat.id) or {}
        target = self.service.destination.from_row(row)
        title = row.get("destination_title") or "نام گروه هنوز بررسی نشده"
        text = ("<b>👥 گروه مقصد</b>\n\n" + escape(title) + "\nشناسه: <code>" + str(target or "تنظیم نشده") + "</code>"
                + "\n\n۱. بات را به گروه اضافه کنید و اجازهٔ ارسال بدهید.\n۲. گروه را انتخاب کنید یا شناسهٔ منفی / @username گروه عمومی را بنویسید."
                + "\n۳. «ارسال آزمایشی» را بزنید و سپس برنامهٔ مطالعه را فعال کنید.")
        if notice:
            text += "\n\n" + escape(notice)
        if row.get("last_error"):
            text += "\n\n⚠️ آخرین خطا: " + escape(row["last_error"])
        await render(update.effective_message, text, keyboard([
            ("👥 انتخاب گروه", "group_choose", "primary"), ("✍️ ورود شناسه", "group_manual"),
            ("🔎 بررسی دسترسی", "group_check"), ("📨 ارسال آزمایشی", "group_test", "success"),
            ("⚙️ برنامهٔ مطالعه", "menu_daily_settings"), ("‹ خانه", "menu_back")], 2), edit=edit)

    async def save_group(self, update, context, target):
        owner = update.effective_chat.id
        try:
            chat = await self.service.destination.validate(target)
        except (TelegramError, ValueError) as exc:
            await update.effective_message.reply_text("❌ " + error_text(exc))
            return
        async with self.service.locks[owner]:
            await self.service.flush_pending(owner)
            await self.repo.update(owner, destination_id=chat.id, destination_title=chat.title,
                                   active=0, next_run=None, last_error=None)
            await self.service.refresh(owner)
        context.user_data.pop("state", None)
        context.user_data.pop("pending_media", None)
        await self.clear_group_picker(update, context)
        await self.group_panel(update, notice="✅ گروه ذخیره شد. ارسال زمان‌بندی‌شده متوقف است؛ پس از آزمایش، «شروع ارسال» را بزنید.")

    async def menu(self, update, context):
        context.user_data.pop("pending_catalog", None)
        if not self.allowed(update):
            return
        if not context.user_data.get("authenticated"):
            await update.effective_message.reply_text("🔑 برای ورود /start را بزنید.")
            return
        await self.clear_group_picker(update, context)
        context.user_data.pop("state", None)
        context.user_data.pop("pending_media", None)
        await self.panel(update)

    async def help(self, update, context):
        if self.allowed(update):
            await render(update.effective_message, HELP, keyboard([("🌿 خانه", "menu_back")]))

    def allowed(self, update):
        return (update.effective_chat is not None and update.effective_chat.type == "private"
                and update.effective_user is not None
                and (not self.config.admin_ids or update.effective_user.id in self.config.admin_ids))

    async def start(self, update, context):
        if not self.allowed(update):
            return
        if context.user_data.get("authenticated"):
            await self.menu(update, context)
            return
        context.user_data.clear()
        await self.repo.ensure(update.effective_chat.id)
        await render(update.effective_message, "<b>🌿 به مُرسِل پیام خوش آمدید</b>\nهر روز، یک قدم با قرآن\n\n🔑 برای ورود به پنل مدیریت، رمز را بنویسید.")

    async def cancel(self, update, context):
        context.user_data.pop("pending_catalog", None)
        if not self.allowed(update):
            return
        await self.clear_group_picker(update, context)
        context.user_data.pop("state", None)
        context.user_data.pop("pending_media", None)
        if context.user_data.get("authenticated"):
            await self.panel(update, notice="ورودی لغو شد.")
        else:
            await update.effective_message.reply_text("ابتدا /start را بزنید و رمز را وارد کنید.")

    async def authenticate(self, update, context):
        if context.user_data.get("authenticated"):
            return True
        user_id = update.effective_user.id
        count, until = self.attempts.get(user_id, (0, 0))
        now = time.monotonic()
        if until > now:
            await update.effective_message.reply_text("تلاش‌های ناموفق زیاد است؛ چند دقیقه دیگر امتحان کنید.")
            return False
        if until:
            count = 0
        text = update.effective_message.text or ""
        if hmac.compare_digest(text.strip().encode(), self.config.password.encode()):
            self.attempts.pop(user_id, None)
            context.user_data.clear()
            context.user_data["authenticated"] = True
            await self.repo.ensure(update.effective_chat.id)
            await self.panel(update, notice="✅ خوش آمدید؛ ورود موفق بود.")
        else:
            count += 1
            self.attempts[user_id] = (count, now + 300 if count >= 5 else 0)
            await update.effective_message.reply_text("❌ رمز اشتباه است؛ دوباره وارد کنید.")
        return False

    async def change(self, chat_id, **values):
        async with self.service.locks[chat_id]:
            await self.service.flush_pending(chat_id)
            await self.repo.update(chat_id, **values)
            await self.service.refresh(chat_id)

    async def select(self, chat_id, index, page=None):
        if not 0 <= index < len(self.surahs):
            raise ValueError("سوره نامعتبر است.")
        surah = self.surahs[index]
        page = surah["start_page"] if page is None else page
        if not surah["start_page"] <= page <= surah["end_page"]:
            raise ValueError("صفحه متعلق به این سوره نیست.")
        await self.change(chat_id, current_index=index, current_page=page, current_day=1,
                          is_first_message=1, next_run=None, completed=0, delivery_pending=0)

    async def message(self, update, context):
        async with self.service.catalog_lock:
            await self._message(update, context)

    async def _message(self, update, context):
        if not self.allowed(update) or not await self.authenticate(update, context):
            return
        message = update.effective_message
        chat_id = update.effective_chat.id
        text = (message.text or "").strip()
        state = context.user_data.get("state")
        if state == "catalog_upload":
            await self.catalog_upload.receive(update, context)
            return
        shared = getattr(message, "chat_shared", None)
        if shared:
            if state == "group" and shared.request_id == context.user_data.get("group_request"):
                await self.save_group(update, context, shared.chat_id)
            else:
                await message.reply_text("این انتخاب گروه منقضی شده است؛ دوباره /group را بزنید.")
            return
        try:
            if state == "group":
                target = text if text.startswith("@") else int(text)
                if isinstance(target, int) and target >= 0:
                    raise ValueError
                await self.save_group(update, context, target)
                return
            elif state == "interval":
                parts = text.split(":")
                if len(parts) != 4:
                    raise ValueError("فرمت صحیح: روز:ساعت:دقیقه:ثانیه")
                days, hours, minutes, seconds = map(int, parts)
                if not (0 <= days <= 3650 and 0 <= hours <= 23 and 0 <= minutes <= 59 and 0 <= seconds <= 59) or not any((days, hours, minutes, seconds)):
                    raise ValueError("دوره باید مثبت باشد؛ ساعت ۰ تا ۲۳ و دقیقه و ثانیه ۰ تا ۵۹.")
                await self.change(chat_id, interval_days=days, interval_hours=hours, interval_minutes=minutes,
                                  interval_seconds=seconds, is_first_message=1, next_run=None)
            elif state == "time":
                parts = text.split(":")
                if len(parts) != 2:
                    raise ValueError("فرمت صحیح: HH:MM")
                hour, minute = map(int, parts)
                if not (0 <= hour <= 23 and 0 <= minute <= 59):
                    raise ValueError("ساعت یا دقیقه نامعتبر است.")
                await self.change(chat_id, hour=hour, minute=minute, is_first_message=1, next_run=None)
            elif state == "day":
                day = int(text)
                if not 1 <= day <= 1000000:
                    raise ValueError("روز باید بین ۱ و ۱۰۰۰۰۰۰ باشد.")
                await self.change(chat_id, current_day=day)
            elif state == "page":
                page = int(text)
                indices = [i for i, surah in enumerate(self.surahs) if surah["start_page"] <= page <= surah["end_page"]]
                if not indices:
                    raise ValueError("صفحه‌ای در فهرست پیدا نشد.")
                if len(indices) > 1:
                    await message.reply_text("سوره را انتخاب کنید:", reply_markup=keyboard([
                        (self.surahs[i]["name"], f"select_surah_page_{page}_{i}_{self.service.catalog_revision}") for i in indices]))
                    context.user_data.pop("state", None)
                    return
                await self.select(chat_id, indices[0], page)
            elif state == "surah":
                index = next((i for i, s in enumerate(self.surahs) if normalize_name(s["name"]) == normalize_name(text)), None)
                if index is None:
                    search = normalize_name(text)
                    matches = [(s["name"], f"surah_pick:{i}:{self.service.catalog_revision}") for i, s in enumerate(self.surahs)
                               if search and search in normalize_name(s["name"])]
                    if matches and len(matches) <= 12:
                        await render(message, "<b>🔎 نتیجهٔ جست‌وجو</b>\nسوره را انتخاب کنید یا نام دقیق‌تری بنویسید.",
                                     keyboard(matches + [("‹ تنظیمات", "menu_daily_settings")], 2))
                        return
                    await message.reply_text("نام دقیق‌تری بنویسید یا از فهرست سوره‌ها انتخاب کنید.")
                    return
                await self.select(chat_id, index)
            elif state == "media":
                if not any((message.text, message.photo, message.video, message.animation, message.sticker,
                            message.audio, message.voice, message.document, message.video_note, message.contact, message.location)):
                    raise ValueError("این نوع پیام پشتیبانی نمی‌شود؛ متن یا رسانه بفرستید.")
                if message.media_group_id:
                    await message.reply_text("لطفاً رسانه‌ها را تکی بفرستید؛ ارسال آلبوم در این بخش پشتیبانی نمی‌شود.")
                    return
                nonce = secrets.token_hex(8)
                try:
                    target = await self.service.destination.get(chat_id)
                except ValueError as exc:
                    await self.group_panel(update, notice=error_text(exc))
                    return
                context.user_data["pending_media"] = dict(nonce=nonce, message_id=message.message_id,
                    chat_id=chat_id, target=target, expires=time.monotonic() + 600)
                context.user_data.pop("state", None)
                await render(message, "<b>✉️ آمادهٔ ارسال</b>\nهمین پیام شما به گروه مقصد ارسال شود؟\nاین تأیید تا ۱۰ دقیقه معتبر است.",
                             keyboard([("ارسال به گروه", f"media_confirm:{nonce}", "success"), ("لغو", "menu_back")], 2))
                return
            else:
                await self.panel(update)
                return
        except ValueError:
            await message.reply_text("❌ ورودی نامعتبر است؛ دوباره طبق راهنما وارد کنید یا /cancel را بزنید.")
            return
        context.user_data.pop("state", None)
        await self.panel(update, daily=True, notice="✅ تنظیمات ذخیره شد.")

    async def callback(self, update, context):
        async with self.service.catalog_lock:
            await self._callback(update, context)

    async def _callback(self, update, context):
        query = update.callback_query
        if not self.allowed(update):
            await query.answer("دسترسی مجاز نیست.", show_alert=True)
            return
        if not context.user_data.get("authenticated"):
            await query.answer("ابتدا /start را بزنید و وارد شوید.", show_alert=True)
            return
        await query.answer()
        message, data = update.effective_message, query.data or ""
        chat_id = update.effective_chat.id
        if data == "noop":
            return
        await self.clear_group_picker(update, context)
        context.user_data.pop("state", None)
        if not data.startswith("media_confirm:"):
            context.user_data.pop("pending_media", None)
        if not data.startswith("catalog_confirm:"):
            context.user_data.pop("pending_catalog", None)
        sent_to_group = False
        try:
            if data == "menu_catalog" or data.startswith("catalog_"):
                await self.catalog_upload.callback(update, context, data)
            elif data == "menu_group":
                await self.group_panel(update, edit=True)
            elif data in ("group_choose", "group_manual"):
                context.user_data["state"] = "group"
                if data == "group_choose":
                    request_id = secrets.randbelow(2**31)
                    context.user_data["group_request"] = request_id
                    markup = ReplyKeyboardMarkup([[KeyboardButton("👥 انتخاب گروه مقصد", request_chat=KeyboardButtonRequestChat(
                        request_id=request_id, chat_is_channel=False, bot_is_member=True, request_title=True))]],
                        resize_keyboard=True, one_time_keyboard=True, input_field_placeholder="گروه را انتخاب کنید یا شناسه بفرستید")
                    await message.reply_text("از دکمهٔ پایین گروه را انتخاب کنید. ابتدا بات باید عضو گروه باشد.\nورود دستی: شناسهٔ منفی یا @username گروه عمومی\nلغو: /cancel", reply_markup=markup)
                else:
                    await render(message, "<b>✍️ شناسهٔ گروه</b>\nشناسهٔ عددی منفی (مثل <code>-1001234567890</code>) یا @username گروه عمومی را بفرستید.\nلینک دعوت خصوصی قابل استفاده نیست.\nلغو: /cancel", keyboard([("‹ گروه مقصد", "menu_group")]), edit=True)
            elif data in ("group_check", "group_test"):
                async with self.service.locks[chat_id]:
                    await self.service.destination.check(chat_id)
                    if data == "group_test":
                        await self.service.destination.send(chat_id, "✅ پیام آزمایشی مُرسِل پیام\nاتصال ربات به این گروه برقرار است.")
                        sent_to_group = True
                        await self.repo.update(chat_id, last_error=None)
                await self.group_panel(update, notice="✅ پیام آزمایشی ارسال شد؛ برای ارسال روزانه برنامه را فعال کنید." if data == "group_test" else "✅ گروه در دسترس است و بات اجازهٔ ارسال متن دارد.", edit=True)
            elif data == "menu_choose_start" or data.startswith("surahs:"):
                page = 0 if data == "menu_choose_start" else int(data.split(":")[1])
                text, markup = surah_picker(self.surahs, page, self.service.catalog_revision)
                await render(message, text, markup, edit=True)
            elif data.startswith("surah_pick:"):
                parts = data.split(":")
                self.validate_catalog_button(parts[2] if len(parts) == 3 else 0)
                await self.select(chat_id, int(data.split(":")[1]))
                await self.panel(update, daily=True, notice="✅ سورهٔ شروع انتخاب شد.", edit=True)
            elif data in ("menu_set_interval", "menu_set_time"):
                state = "interval" if data == "menu_set_interval" else "time"
                context.user_data["state"] = state
                text, markup = presets(state)
                if state == "time":
                    text += f"\nمنطقهٔ زمانی: {escape(self.config.timezone)}"
                await render(message, text, markup, edit=True)
            elif data.startswith("interval:"):
                values = INTERVALS[data.split(":")[1]]
                await self.change(chat_id, **dict(zip(("interval_days", "interval_hours", "interval_minutes", "interval_seconds"), values)),
                                  is_first_message=1, next_run=None)
                await self.panel(update, daily=True, notice="✅ دورهٔ ارسال ذخیره شد.", edit=True)
            elif data.startswith("time:"):
                value = data.removeprefix("time:")
                if value not in TIMES:
                    raise ValueError
                hour, minute = map(int, value.split(":"))
                await self.change(chat_id, hour=hour, minute=minute, is_first_message=1, next_run=None)
                await self.panel(update, daily=True, notice="✅ ساعت شروع ذخیره شد.", edit=True)
            elif data == "surah_search" or data in PROMPTS:
                state, prompt = PROMPTS["menu_choose_start" if data == "surah_search" else data]
                context.user_data["state"] = state
                await render(message, f"<b>✍️ {escape(prompt)}</b>\n\nلغو: /cancel", back_markup(), edit=True)
            elif data.startswith("select_surah_page_"):
                parts = data.split("_")
                if len(parts) not in (5, 6):
                    raise ValueError
                _, _, _, page, index = parts[:5]
                self.validate_catalog_button(parts[5] if len(parts) == 6 else 0)
                await self.select(chat_id, int(index), int(page))
                await self.panel(update, daily=True, notice="✅ سوره و صفحه ذخیره شد.", edit=True)
            elif data == "menu_preview":
                row = await self.repo.get(chat_id)
                index = row["current_index"] if row else None
                if index is None or not 0 <= index < len(self.surahs) or row["current_page"] is None:
                    await self.panel(update, daily=True, notice="ابتدا سوره یا صفحه را انتخاب کنید.", edit=True)
                    return
                text = build_daily_message(self.surahs[index], row["current_page"], row["current_day"], self.config.timezone)
                await render(message, "<b>👁 پیش‌نمایش پیام</b>\n<i>فقط برای شما؛ با تاریخ امروز</i>\n\n" + escape(text), preview_markup(text), edit=True)
            elif data.startswith("media_confirm:"):
                pending = context.user_data.get("pending_media")
                if not pending or pending["nonce"] != data.split(":")[1] or pending["expires"] < time.monotonic():
                    await self.panel(update, notice="این تأیید منقضی شده یا قبلاً استفاده شده است.", edit=True)
                    return
                # Consume before sending: duplicate clicks and uncertain network responses must not resend.
                context.user_data.pop("pending_media", None)
                async with self.service.locks[chat_id]:
                    target = await self.service.destination.get(chat_id)
                    if pending.get("target", target) != target:
                        await self.panel(update, notice="گروه مقصد تغییر کرده؛ پیام را دوباره انتخاب کنید.", edit=True)
                        return
                    await self.service.destination.copy(chat_id, from_chat_id=pending["chat_id"], message_id=pending["message_id"])
                    sent_to_group = True
                await self.panel(update, notice="✅ پیام به گروه ارسال شد.", edit=True)
            elif data == "menu_logout":
                context.user_data.clear()
                await render(message, "<b>🔒 از پنل خارج شدید</b>\nورود دوباره: /start\nزمان‌بندی‌های فعال ادامه دارند.", edit=True)
            elif data == "menu_resume":
                async with self.service.locks[chat_id]:
                    await self.service.flush_pending(chat_id)
                    row = await self.repo.get(chat_id)
                    if row and row.get("completed"):
                        await self.panel(update, daily=True, notice="✅ مطالعه پایان یافته است؛ برای شروع دوباره سوره یا صفحه را انتخاب کنید.", edit=True)
                        return
                    if row and row.get("delivery_pending"):
                        await self.panel(update, daily=True, notice="نتیجهٔ ارسال قبلی نامشخص است؛ گروه را بررسی و صفحهٔ شروع را دوباره انتخاب کنید.", edit=True)
                        return
                    if not self.service.ready(row):
                        await self.panel(update, daily=True, notice="برای شروع، سوره/صفحه، دوره و ساعت را تکمیل کنید.", edit=True)
                        return
                    await self.service.destination.check(chat_id)
                    if not row["active"]:
                        await self.repo.update(chat_id, active=1, next_run=None, is_first_message=1, last_error=None)
                        await self.service.refresh(chat_id)
                await self.panel(update, daily=True, notice="▶️ ارسال فعال است.", edit=True)
            elif data == "menu_stop":
                await self.change(chat_id, active=0, next_run=None)
                await self.panel(update, daily=True, notice="⏸ ارسال متوقف شد؛ پیشرفت شما محفوظ است.", edit=True)
            elif data == "menu_show_time":
                weekday, date = jalali_now(self.config.timezone)
                clock = datetime.now(pytz.timezone(self.config.timezone)).strftime("%H:%M:%S")
                await render(message, f"<b>📅 {escape(weekday)}، {date}</b>\n\n⏰ {to_persian_number(clock)}\n{escape(self.config.timezone)}",
                             keyboard([("🔄 تازه‌سازی", "menu_show_time"), ("‹ خانه", "menu_back")], 2), edit=True)
            elif data == "menu_help":
                await render(message, HELP, keyboard([("‹ خانه", "menu_back")]), edit=True)
            elif data == "menu_send_hello":
                # Retained for buttons sent by older versions.
                await self.service.destination.send(chat_id, "سلام 👋")
                sent_to_group = True
                await self.panel(update, notice="✅ ارسال شد.", edit=True)
            else:
                await self.panel(update, daily=data == "menu_daily_settings", edit=True)
        except TelegramError as exc:
            if sent_to_group:
                logger.warning("Group delivery succeeded; panel update failed (%s)", type(exc).__name__)
                try:
                    await message.reply_text("✅ پیام به گروه ارسال شد؛ به‌روزرسانی پنل انجام نشد. برای نمایش وضعیت /menu را بزنید.")
                except TelegramError:
                    logger.warning("Could not acknowledge successful group delivery")
                return
            await self.repo.update(chat_id, last_error=error_text(exc))
            await self.group_panel(update, notice="❌ عملیات انجام نشد: " + error_text(exc), edit=True)
        except ValueError as exc:
            if data.startswith("catalog_"):
                await self.panel(update, notice=str(exc), edit=True)
            elif data.startswith("group_") or data in ("menu_resume", "menu_send_hello") or data.startswith("media_confirm:"):
                await self.group_panel(update, notice="❌ " + error_text(exc), edit=True)
            else:
                await self.panel(update, daily=True, notice="گزینه نامعتبر است؛ دوباره از منو انتخاب کنید.", edit=True)
        except KeyError:
            await self.panel(update, daily=True, notice="گزینه نامعتبر است؛ دوباره از منو انتخاب کنید.", edit=True)

    async def error(self, update, context):
        # Do not log Update objects, passwords, or request URLs containing bot tokens.
        logger.error("Update failed (%s)", type(context.error).__name__)
        if isinstance(context.error, Conflict):
            logger.error("Another bot instance is polling with this token. Stopping.")
            context.application.stop_running()
        elif update and self.allowed(update) and update.effective_message:
            try:
                await update.effective_message.reply_text("❌ عملیات انجام نشد؛ اتصال و دسترسی بات به گروه را بررسی و دوباره تلاش کنید.")
            except TelegramError:
                logger.warning("Could not notify the administrator")


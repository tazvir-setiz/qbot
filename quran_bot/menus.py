"""Telegram panels; API button styles are passed through for PTB 22.3."""
from datetime import datetime
from html import escape

import jdatetime
import pytz
from telegram import CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest

from .formatting import to_persian_number as fa


def button(text, data, style=None):
    return InlineKeyboardButton(text, callback_data=data, api_kwargs={"style": style} if style else None)


def keyboard(items, columns=1):
    buttons = [button(*item) for item in items]
    return InlineKeyboardMarkup([buttons[i:i + columns] for i in range(0, len(buttons), columns)])


def back_markup():
    return keyboard([("‹ بازگشت به تنظیمات", "menu_daily_settings")])


async def render(message, text, markup=None, *, edit=False):
    options = dict(parse_mode="HTML", reply_markup=markup)
    if edit:
        try:
            return await message.edit_text(text, **options)
        except BadRequest as exc:
            reason = str(exc).lower()
            if "message is not modified" in reason:
                return None
            if not any(value in reason for value in ("message can't be edited", "message to edit not found", "no text in the message")):
                raise
    return await message.reply_text(text, **options)


def duration(row):
    parts = [f"{fa(row.get(key) or 0)} {label}" for key, label in (
        ("interval_days", "روز"), ("interval_hours", "ساعت"),
        ("interval_minutes", "دقیقه"), ("interval_seconds", "ثانیه")) if row.get(key)]
    return " و ".join(parts) if parts else "تنظیم نشده"


def dashboard(row, surahs, timezone, daily=False, notice=None):
    row = row or {}
    index = row.get("current_index")
    selected = isinstance(index, int) and 0 <= index < len(surahs)
    surah = surahs[index] if selected else None
    hour, minute = row.get("hour"), row.get("minute")
    clock = fa(f"{hour:02}:{minute:02}") if hour is not None and minute is not None else "تنظیم نشده"
    next_time = "—"
    if row.get("next_run"):
        date = datetime.fromtimestamp(row["next_run"], pytz.timezone(timezone))
        next_time = fa(jdatetime.datetime.fromgregorian(datetime=date).strftime("%Y/%m/%d · %H:%M"))
    title = "⚙️ برنامهٔ مطالعه" if daily else "🌿 همراه تدبر"
    lines = [f"<b>{title}</b>", "<i>هر روز، یک قدم با قرآن</i>", ""]
    if notice:
        lines.extend([escape(notice), ""])
    lines.extend([
        f"{'✅ پایان‌یافته' if row.get('completed') else ('🟢 فعال' if row.get('active') else '⏸ متوقف')}  ·  روز {fa(row.get('current_day') or 1)}",
        f"👥 مقصد: {escape(row.get('destination_title') or str(row.get('destination_id') or 'انتخاب نشده'))}",
        f"📖 سوره: <b>{escape(surah['name']) if surah else 'انتخاب نشده'}</b>",
        f"📄 صفحهٔ بعدی: {fa(row.get('current_page') or '—')}",
        f"🔁 هر {duration(row)}" if any(row.get(k) for k in ('interval_days', 'interval_hours', 'interval_minutes', 'interval_seconds')) else "🔁 دوره: تنظیم نشده",
        f"⏰ ساعت شروع: {clock}", f"🗓 ارسال بعدی: {next_time}",
    ])
    if row.get("last_error"):
        lines.extend(["", "⚠️ " + escape(row["last_error"])])
    if row.get("last_sent_at"):
        sent = datetime.fromtimestamp(row["last_sent_at"], pytz.timezone(timezone))
        lines.append("✅ آخرین ارسال روزانه: " + fa(jdatetime.datetime.fromgregorian(datetime=sent).strftime("%Y/%m/%d · %H:%M")))
    if selected:
        total = surah['end_page'] - surah['start_page'] + 1
        done = max(0, min(total, (row.get('current_page') or surah['start_page']) - surah['start_page']))
        filled = int(done / total * 8)
        lines.extend(["", f"{'▰' * filled}{'▱' * (8 - filled)}  {fa(done)} از {fa(total)} صفحهٔ سوره"])
    if daily:
        lines.extend(["", "<b>راه‌اندازی در سه قدم</b>",
                      f"{'✓' if selected else '○'} انتخاب سوره یا صفحه  →  {'✓' if any(row.get(k) for k in ('interval_days','interval_hours','interval_minutes','interval_seconds')) else '○'} دوره  →  {'✓' if hour is not None else '○'} ساعت",
                      "سپس «شروع ارسال» را بزنید."])
    return "\n".join(lines)


async def show_menu(message, daily=False, *, row=None, surahs=(), timezone="Asia/Tehran", edit=False, notice=None):
    active = bool(row and row.get('active'))
    toggle = ("⏸ توقف ارسال", "menu_stop", "danger") if active else ("▶ شروع ارسال", "menu_resume", "success")
    if daily:
        items = [("📖 انتخاب سوره", "menu_choose_start"), ("📄 انتخاب صفحه", "menu_choose_page"),
                 ("🔁 دورهٔ ارسال", "menu_set_interval"), ("⏰ ساعت شروع", "menu_set_time"),
                 ("📅 شمارهٔ روز", "menu_set_day"), ("👁 پیش‌نمایش", "menu_preview"),
                 ("👥 تنظیم گروه", "menu_group", "primary"), toggle, ("‹ خانه", "menu_back")]
    else:
        items = [("⚙️ برنامهٔ مطالعه", "menu_daily_settings", "primary"), ("👁 پیش‌نمایش", "menu_preview"),
                 ("✉️ ارسال پیام", "menu_send_custom"), ("🔄 تازه‌سازی", "menu_status"),
                 toggle, ("📅 تاریخ و ساعت", "menu_show_time"),
                 ("👥 تنظیم گروه", "menu_group", "primary"), ("❔ راهنما", "menu_help"), ("🔒 خروج", "menu_logout")]
    return await render(message, dashboard(row, surahs, timezone, daily, notice), keyboard(items, 2), edit=edit)


def surah_picker(surahs, page):
    size = 12
    pages = max(1, (len(surahs) + size - 1) // size)
    if not 0 <= page < pages:
        raise ValueError("Invalid surah page")
    # Display in mushaf order without changing persistent catalog indices.
    ordered = sorted(enumerate(surahs), key=lambda item: (item[1]['start_page'], item[0]))
    items = [(surah['name'], f"surah_pick:{index}") for index, surah in ordered[page * size:(page + 1) * size]]
    rows = list(keyboard(items, 2).inline_keyboard)
    navigation = []
    if page:
        navigation.append(button("‹ قبلی", f"surahs:{page - 1}"))
    navigation.append(button(f"{fa(page + 1)} / {fa(pages)}", "noop"))
    if page + 1 < pages:
        navigation.append(button("بعدی ›", f"surahs:{page + 1}"))
    rows.extend([navigation, [button("🔎 جست‌وجوی نام", "surah_search"), button("‹ تنظیمات", "menu_daily_settings")]])
    return "<b>📖 سورهٔ شروع را انتخاب کنید</b>\nفهرست بر اساس صفحه‌های قرآن مرتب شده است.", InlineKeyboardMarkup(rows)


INTERVALS = {"daily": (1, 0, 0, 0), "twodays": (2, 0, 0, 0), "weekly": (7, 0, 0, 0), "hourly": (0, 1, 0, 0)}
TIMES = {"06:00", "09:00", "12:00", "18:00", "21:00"}


def presets(kind):
    if kind == "interval":
        items = [("هر روز", "interval:daily"), ("هر دو روز", "interval:twodays"), ("هر هفته", "interval:weekly"), ("هر ساعت", "interval:hourly")]
        title = "<b>🔁 فاصلهٔ بین پیام‌ها</b>\nیک گزینه انتخاب کنید یا دوره را به شکل <code>1:0:0:0</code> بنویسید.\nترتیب: روز:ساعت:دقیقه:ثانیه"
    else:
        items = [(fa(value), f"time:{value}") for value in sorted(TIMES)]
        title = "<b>⏰ ساعت اولین ارسال</b>\nانتخاب کنید یا ساعت دلخواه را به شکل <code>09:30</code> بنویسید."
    rows = list(keyboard(items, 2).inline_keyboard)
    rows.append([button("‹ لغو و بازگشت", "menu_daily_settings")])
    return title, InlineKeyboardMarkup(rows)


def preview_markup(text):
    rows = []
    if 1 <= len(text) <= 256:
        rows.append([InlineKeyboardButton("📋 کپی متن پیام", copy_text=CopyTextButton(text))])
    rows.append([button("‹ بازگشت", "menu_daily_settings")])
    return InlineKeyboardMarkup(rows)


HELP = """<b>🌿 راهنمای همراه تدبر</b>

<b>ابتدا:</b> از «تنظیم گروه» مقصد را انتخاب کنید و یک پیام آزمایشی بفرستید.
<b>۱.</b> سوره یا صفحهٔ شروع را انتخاب کنید.
<b>۲.</b> فاصلهٔ پیام‌ها و ساعت اولین ارسال را تعیین کنید.
<b>۳.</b> پیش‌نمایش را ببینید و «شروع ارسال» را بزنید.

<blockquote expandable>اولین پیام در نزدیک‌ترین نوبت ساعت انتخاب‌شده ارسال می‌شود. پس از آن، فاصلهٔ تعیین‌شده اعمال می‌شود.
پیش‌نمایش فقط برای شماست و پیشرفت را تغییر نمی‌دهد.
تغییر تنظیمات یک برنامهٔ متوقف آن را فعال نمی‌کند.
خروج از حساب، زمان‌بندی را متوقف نمی‌کند.
ارسال پیام دلخواه پس از تأیید شما انجام می‌شود.</blockquote>

/group تنظیم گروه · /menu خانه · /status وضعیت
/cancel لغو ورودی · /help راهنما"""

"""One destination resolver for scheduled, custom, and test messages."""
from telegram.error import BadRequest, ChatMigrated, Forbidden, NetworkError, RetryAfter


def error_text(exc):
    if isinstance(exc, Forbidden):
        return "بات به گروه دسترسی ندارد؛ آن را دوباره به گروه اضافه کنید و اجازهٔ ارسال بدهید."
    if isinstance(exc, BadRequest):
        reason = str(exc).lower()
        if "chat not found" in reason:
            return "گروه پیدا نشد؛ شناسه اشتباه است یا بات عضو گروه نیست. از «تنظیم گروه» مقصد را دوباره انتخاب کنید."
        if any(word in reason for word in ("not enough rights", "chat_write_forbidden", "send_messages_forbidden")):
            return "بات اجازهٔ ارسال ندارد؛ دسترسی ارسال پیام در گروه را فعال کنید."
        return "تلگرام ارسال را نپذیرفت؛ گروه مقصد و مجوز ارسال این نوع پیام را بررسی کنید."
    if isinstance(exc, RetryAfter):
        return "محدودیت موقت تلگرام؛ کمی بعد دوباره تلاش کنید."
    if isinstance(exc, NetworkError):
        return "ارتباط با تلگرام برقرار نشد؛ اتصال اینترنت یا پروکسی محیط اجرای بات را بررسی کنید."
    if isinstance(exc, ValueError):
        return str(exc)
    return "ارسال انجام نشد؛ تنظیم گروه و دسترسی بات را بررسی کنید."


class Destination:
    def __init__(self, config, repo, bot):
        self.config, self.repo, self.bot = config, repo, bot

    def from_row(self, row):
        return (row or {}).get("destination_id") or self.config.group_id

    async def get(self, owner):
        target = self.from_row(await self.repo.get(owner))
        if target is None:
            raise ValueError("ابتدا از «تنظیم گروه» گروه مقصد را انتخاب کنید.")
        return target

    async def validate(self, target):
        try:
            chat = await self.bot.get_chat(target)
        except ChatMigrated as exc:
            chat = await self.bot.get_chat(exc.new_chat_id)
        if chat.type not in ("group", "supergroup"):
            raise ValueError("مقصد باید گروه یا سوپرگروه باشد، نه کاربر یا کانال.")
        member = await self.bot.get_chat_member(chat.id, self.bot.id)
        if member.status in ("left", "kicked") or (member.status == "restricted" and not member.is_member):
            raise ValueError("بات عضو گروه نیست؛ ابتدا آن را به گروه اضافه کنید.")
        if member.status == "restricted" and not member.can_send_messages:
            raise ValueError("بات در این گروه اجازهٔ ارسال پیام ندارد.")
        permissions = getattr(chat, "permissions", None)
        if member.status == "member" and permissions and permissions.can_send_messages is False:
            raise ValueError("ارسال پیام برای اعضای گروه بسته است؛ به بات دسترسی ارسال بدهید.")
        return chat

    async def check(self, owner):
        chat = await self.validate(await self.get(owner))
        await self.repo.update(owner, destination_id=chat.id, destination_title=chat.title)
        return chat

    async def _call(self, owner, method, **kwargs):
        target = await self.get(owner)
        try:
            return await method(chat_id=target, **kwargs)
        except ChatMigrated as exc:
            # Telegram explicitly rejected the old ID; retrying with its replacement is safe.
            await self.repo.update(owner, destination_id=exc.new_chat_id)
            return await method(chat_id=exc.new_chat_id, **kwargs)

    async def send(self, owner, text):
        return await self._call(owner, self.bot.send_message, text=text)

    async def copy(self, owner, from_chat_id, message_id):
        return await self._call(owner, self.bot.copy_message, from_chat_id=from_chat_id, message_id=message_id)

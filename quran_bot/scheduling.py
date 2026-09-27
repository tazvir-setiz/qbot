"""Persist the next delivery; serialize edits and sends per administrator."""
import asyncio
import logging
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TelegramError

from .formatting import build_daily_message
from .destination import Destination, error_text

logger = logging.getLogger(__name__)


def interval_seconds(row):
    return sum((row.get(key) or 0) * multiplier for key, multiplier in (
        ("interval_days", 86400), ("interval_hours", 3600),
        ("interval_minutes", 60), ("interval_seconds", 1)))


class DeliveryService:
    def __init__(self, config, repository, surahs, bot):
        self.config, self.repo, self.surahs, self.bot = config, repository, surahs, bot
        self.scheduler = AsyncIOScheduler(timezone=config.timezone)
        self.locks = defaultdict(asyncio.Lock)
        self.destination = Destination(config, repository, bot)
        self.pending_updates = {}
        self.storage_errors = {}

    async def persist_delivery(self, chat_id, **values):
        # Keep the exact result until SQLite confirms it; never resend to recover a write.
        self.pending_updates[chat_id] = values
        await self.flush_pending(chat_id)

    async def flush_pending(self, chat_id):
        if chat_id in self.pending_updates:
            await self.repo.update(chat_id, **self.pending_updates[chat_id])
            self.pending_updates.pop(chat_id)
        self.storage_errors.pop(chat_id, None)

    def ready(self, row):
        if not row or row.get("completed") or row.get("delivery_pending") or interval_seconds(row) <= 0:
            return False
        index, page = row.get("current_index"), row.get("current_page")
        return (isinstance(index, int) and 0 <= index < len(self.surahs)
                and isinstance(page, int)
                and self.surahs[index]["start_page"] <= page <= self.surahs[index]["end_page"]
                and isinstance(row.get("hour"), int) and 0 <= row["hour"] <= 23
                and isinstance(row.get("minute"), int) and 0 <= row["minute"] <= 59)

    def remove(self, chat_id):
        job_id = f"daily_{chat_id}"
        if self.scheduler.get_job(job_id):
            self.scheduler.remove_job(job_id)

    def queue(self, chat_id, timestamp):
        self.scheduler.add_job(self.deliver, "date", run_date=datetime.fromtimestamp(timestamp, timezone.utc),
                               args=[chat_id], id=f"daily_{chat_id}", replace_existing=True,
                               misfire_grace_time=None, max_instances=1)

    async def refresh(self, chat_id):
        """Caller holds the chat lock during edits or delivery."""
        self.remove(chat_id)
        row = await self.repo.get(chat_id)
        if not row or not row["active"]:
            return
        if row.get("delivery_pending"):
            await self.repo.update(chat_id, active=0, next_run=None,
                                   last_error="نتیجهٔ ارسال قبلی پس از قطع برنامه نامشخص است؛ گروه را بررسی و صفحهٔ شروع را دوباره انتخاب کنید.")
            return
        if not self.ready(row):
            await self.repo.update(chat_id, active=0, next_run=None,
                                   last_error="تنظیمات برنامه ناقص است؛ سوره، صفحه، دوره و ساعت را بررسی کنید.")
            return
        now = datetime.now(timezone.utc)
        timestamp = row["next_run"]
        if timestamp is None:
            if row["is_first_message"]:
                trigger = CronTrigger(hour=row["hour"], minute=row["minute"],
                                      timezone=pytz.timezone(self.config.timezone))
                timestamp = trigger.get_next_fire_time(None, now).timestamp()
            else:
                timestamp = now.timestamp() + interval_seconds(row)
            await self.repo.update(chat_id, next_run=timestamp)
        # After downtime send at most one overdue message, with no catch-up burst.
        self.queue(chat_id, max(timestamp, now.timestamp() + 1))

    async def start(self):
        self.scheduler.start(paused=True)
        for chat_id in await self.repo.active_ids():
            async with self.locks[chat_id]:
                await self.refresh(chat_id)
        self.scheduler.resume()

    async def stop(self):
        if self.scheduler.running:
            self.scheduler.pause()
            # Finish in-flight sends before shutting down the Telegram client.
            for lock in list(self.locks.values()):
                async with lock:
                    pass
            self.scheduler.shutdown(wait=True)
            await asyncio.sleep(0)

    async def deliver(self, chat_id):
        try:
            await self._deliver(chat_id)
        except sqlite3.Error:
            self.storage_errors[chat_id] = "خطای ذخیره‌سازی؛ بازیابی اطلاعات در حال تلاش مجدد است."
            logger.error("Database failure for schedule %s; retrying persistence in 30 seconds", chat_id)
            self.queue(chat_id, datetime.now(timezone.utc).timestamp() + 30)

    async def _deliver(self, chat_id):
        async with self.locks[chat_id]:
            if chat_id in self.pending_updates:
                await self.flush_pending(chat_id)
                await self.refresh(chat_id)
                return
            row = await self.repo.get(chat_id)
            self.storage_errors.pop(chat_id, None)
            if not row or not row["active"]:
                self.remove(chat_id)
                return
            if row.get("delivery_pending"):
                await self.refresh(chat_id)
                return
            if not self.ready(row):
                await self.repo.update(chat_id, active=0, next_run=None,
                                       last_error="تنظیمات برنامه ناقص است؛ سوره، صفحه، دوره و ساعت را بررسی کنید.")
                self.remove(chat_id)
                return
            # A queued old job may have waited while a user changed its schedule.
            now = datetime.now(timezone.utc).timestamp()
            if row["next_run"] and row["next_run"] > now:
                self.queue(chat_id, row["next_run"])
                return
            index, page = row["current_index"], row["current_page"]
            message = build_daily_message(self.surahs[index], page, row["current_day"], self.config.timezone)
            # Survives a process exit between Telegram delivery and progress commit.
            await self.repo.update(chat_id, delivery_pending=1)
            try:
                await self.destination.send(chat_id, message)
            except (BadRequest, Forbidden, ValueError) as exc:
                await self.persist_delivery(chat_id, active=0, next_run=None, delivery_pending=0, last_error=error_text(exc))
                self.remove(chat_id)
                logger.error("Destination rejected delivery; schedule %s stopped. Check group ID and permissions.", chat_id)
                await self.notify_failure(chat_id, exc)
                return
            except (NetworkError, RetryAfter) as exc:
                delay = 60
                if isinstance(exc, RetryAfter):
                    delay = exc.retry_after
                    if isinstance(delay, timedelta):
                        delay = delay.total_seconds()
                    delay = max(1, delay) + 1
                timestamp = datetime.now(timezone.utc).timestamp() + delay
                await self.persist_delivery(chat_id, next_run=timestamp, delivery_pending=0, last_error=error_text(exc))
                self.queue(chat_id, timestamp)
                logger.warning("Delivery deferred for chat %s (%s)", chat_id, type(exc).__name__)
                return
            except TelegramError as exc:
                await self.persist_delivery(chat_id, active=0, next_run=None, delivery_pending=0, last_error=error_text(exc))
                self.remove(chat_id)
                logger.error("Telegram rejected delivery; schedule %s stopped", chat_id)
                await self.notify_failure(chat_id, exc)
                return
            sent_at = datetime.now(timezone.utc).timestamp()
            page += 1
            if page > self.surahs[index]["end_page"]:
                index += 1
                if index == len(self.surahs):
                    await self.persist_delivery(chat_id, active=0, next_run=None, is_first_message=0,
                                                last_error=None, last_sent_at=sent_at, completed=1, delivery_pending=0)
                    self.remove(chat_id)
                    return
                page = self.surahs[index]["start_page"]
            timestamp = datetime.now(timezone.utc).timestamp() + interval_seconds(row)
            await self.persist_delivery(chat_id, current_index=index, current_page=page,
                                   current_day=row["current_day"] + 1, is_first_message=0, next_run=timestamp,
                                   last_error=None, last_sent_at=sent_at, delivery_pending=0)
            self.queue(chat_id, timestamp)

    async def notify_failure(self, chat_id, exc):
        try:
            await self.bot.send_message(chat_id=chat_id, text="⚠️ ارسال روزانه متوقف شد.\n" + error_text(exc) + "\nتنظیم گروه: /group")
        except TelegramError:
            logger.warning("Could not notify owner of failed delivery")

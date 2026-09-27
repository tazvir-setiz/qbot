import asyncio
import json
import io
import logging
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import anyio
from apscheduler.events import EVENT_JOB_EXECUTED
from telegram.error import BadRequest, ChatMigrated, NetworkError
from telegram.request import HTTPXRequest

from quran_bot.application import build_application
from quran_bot.catalog import load_surahs
from quran_bot.config import Config, ROOT
from quran_bot.database import Repository
from quran_bot.handlers import Handlers
from quran_bot.menus import button, dashboard, preview_markup, render, surah_picker
from quran_bot.scheduling import DeliveryService


class BotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "test.db"
        self.repo = Repository(self.path)
        await self.repo.initialize()
        self.config = SimpleNamespace(group_id=-123, timezone="Asia/Tehran", password="secret", admin_ids=frozenset())
        self.bot = SimpleNamespace(send_message=AsyncMock(), copy_message=AsyncMock(), id=123, get_chat=AsyncMock(), get_chat_member=AsyncMock())
        self.bot.get_chat.return_value = SimpleNamespace(id=-456, title="گروه آزمایش", type="supergroup", permissions=None)
        self.bot.get_chat_member.return_value = SimpleNamespace(status="administrator")
        self.surahs = [dict(name="الف", start_page=1, end_page=2), dict(name="ب", start_page=3, end_page=3)]
        self.service = DeliveryService(self.config, self.repo, self.surahs, self.bot)
        self.handlers = Handlers(self.config, self.repo, self.service)
        self.service.queue = lambda chat_id, timestamp: None
        await self.repo.update(1, current_index=0, current_page=1, current_day=1,
                               interval_seconds=5, hour=9, minute=0, active=1, next_run=time.time() - 1)

    def update(self, text="", data=None):
        message = SimpleNamespace(text=text, reply_text=AsyncMock(), edit_text=AsyncMock())
        query = SimpleNamespace(data=data, answer=AsyncMock())
        return SimpleNamespace(effective_message=message, effective_chat=SimpleNamespace(id=1, type="private"),
                               effective_user=SimpleNamespace(id=1), callback_query=query)

    async def test_review_database_failure_recovers_without_resending(self):
        original = self.repo.update
        failed = False
        async def fail_once(owner, **values):
            nonlocal failed
            if "current_page" in values and not failed:
                failed = True
                raise sqlite3.OperationalError("temporary write failure")
            await original(owner, **values)
        queued = []
        self.service.queue = lambda owner, timestamp: queued.append(timestamp)
        self.repo.update = fail_once
        await self.service.deliver(1)
        self.assertTrue(queued, "A database failure must schedule recovery")
        await self.service.deliver(1)
        self.bot.send_message.assert_awaited_once()
        row = await self.repo.get(1)
        self.assertEqual(row["current_page"], 2)
        self.assertIsNotNone(row["next_run"])

    async def test_review_completed_reading_cannot_resume(self):
        await self.repo.update(1, current_index=1, current_page=3)
        await self.service.deliver(1)
        await self.handlers.callback(self.update(data="menu_resume"), SimpleNamespace(user_data={"authenticated": True}))
        row = await self.repo.get(1)
        self.assertEqual(row["active"], 0)
        self.assertTrue(row.get("completed"))
        await self.handlers.select(1, 0)
        self.assertFalse((await self.repo.get(1))["completed"])

    async def test_review_successful_send_survives_panel_failure(self):
        update = self.update(data="media_confirm:abc")
        update.effective_message.edit_text.side_effect = [NetworkError("panel unavailable"), None]
        context = SimpleNamespace(user_data={"authenticated": True, "pending_media":
            dict(nonce="abc", target=-123, expires=time.monotonic() + 60, chat_id=1, message_id=42)})
        await self.handlers.callback(update, context)
        self.bot.copy_message.assert_awaited_once()
        self.assertIsNone((await self.repo.get(1))["last_error"])
        self.assertIn("ارسال شد", update.effective_message.reply_text.call_args.args[0])

    async def test_recovery_job_survives_repeated_database_failures(self):
        service = DeliveryService(self.config, self.repo, self.surahs, self.bot)
        original = self.repo.update
        remaining = 2
        async def fail_progress(owner, **values):
            nonlocal remaining
            if "current_page" in values and remaining:
                remaining -= 1
                raise sqlite3.OperationalError("temporary database failure")
            await original(owner, **values)
        self.repo.update = fail_progress
        finished = asyncio.Event()
        service.scheduler.add_listener(lambda event: finished.set(), EVENT_JOB_EXECUTED)
        try:
            await service.start()
            await asyncio.wait_for(finished.wait(), 5)
            self.assertIsNotNone(service.scheduler.get_job("daily_1"))
            self.assertIn(1, service.storage_errors)
            await service.deliver(1)
            self.assertIsNotNone(service.scheduler.get_job("daily_1"))
            await service.deliver(1)
            self.bot.send_message.assert_awaited_once()
            self.assertEqual((await self.repo.get(1))["current_page"], 2)
            self.assertNotIn(1, service.storage_errors)
        finally:
            await service.stop()

    async def test_restart_pauses_uncertain_delivery_instead_of_resending(self):
        await self.repo.update(1, delivery_pending=1)
        restarted = DeliveryService(self.config, Repository(self.path), self.surahs, self.bot)
        try:
            await restarted.start()
            row = await self.repo.get(1)
            self.assertEqual(row["active"], 0)
            self.assertIn("نامشخص", row["last_error"])
            self.assertFalse(restarted.scheduler.get_jobs())
            await restarted.deliver(1)
            self.bot.send_message.assert_not_awaited()
        finally:
            await restarted.stop()

    async def test_database_failure_before_send_does_not_send(self):
        original = self.repo.update
        async def fail_marker(owner, **values):
            if values.get("delivery_pending") == 1:
                raise sqlite3.OperationalError("cannot write marker")
            await original(owner, **values)
        self.repo.update = fail_marker
        queued = []
        self.service.queue = lambda owner, timestamp: queued.append(timestamp)
        await self.service.deliver(1)
        self.bot.send_message.assert_not_awaited()
        self.assertTrue(queued)

    async def test_recovery_commits_completion_without_resending(self):
        await self.repo.update(1, current_index=1, current_page=3)
        original = self.repo.update
        failed = False
        async def fail_completion(owner, **values):
            nonlocal failed
            if values.get("completed") and not failed:
                failed = True
                raise sqlite3.OperationalError("failed final commit")
            await original(owner, **values)
        self.repo.update = fail_completion
        await self.service.deliver(1)
        await self.service.deliver(1)
        self.bot.send_message.assert_awaited_once()
        row = await self.repo.get(1)
        self.assertEqual(row["completed"], 1)
        self.assertEqual(row["active"], 0)

    async def test_failed_panel_and_ack_never_repeat_successful_send(self):
        update = self.update(data="media_confirm:abc")
        update.effective_message.edit_text.side_effect = NetworkError("panel unavailable")
        update.effective_message.reply_text.side_effect = NetworkError("ack unavailable")
        context = SimpleNamespace(user_data={"authenticated": True, "pending_media":
            dict(nonce="abc", target=-123, expires=time.monotonic() + 60, chat_id=1, message_id=42)})
        await self.handlers.callback(update, context)
        self.assertNotIn("pending_media", context.user_data)
        self.bot.copy_message.assert_awaited_once()
        self.assertIsNone((await self.repo.get(1))["last_error"])

    async def test_authentication_does_not_reset_progress(self):
        await self.repo.update(1, current_day=15, current_page=2)
        context = SimpleNamespace(user_data={})
        await self.handlers.authenticate(self.update("secret"), context)
        row = await self.repo.get(1)
        self.assertEqual((row["current_day"], row["current_page"]), (15, 2))
        self.assertTrue(context.user_data["authenticated"])

    async def test_successful_delivery_advances_and_persists_next_run(self):
        await self.service.deliver(1)
        row = await self.repo.get(1)
        self.assertEqual((row["current_page"], row["current_day"]), (2, 2))
        self.assertGreater(row["next_run"], time.time())
        self.bot.send_message.assert_awaited_once()

    async def test_failed_delivery_does_not_advance(self):
        self.bot.send_message.side_effect = NetworkError("offline")
        await self.service.deliver(1)
        row = await self.repo.get(1)
        self.assertEqual((row["current_page"], row["current_day"]), (1, 1))
        self.assertGreater(row["next_run"], time.time())

    async def test_stopped_schedule_never_sends(self):
        await self.handlers.change(1, active=0, next_run=None)
        await self.service.deliver(1)
        self.bot.send_message.assert_not_awaited()

    async def test_final_page_stops(self):
        await self.repo.update(1, current_index=1, current_page=3)
        await self.service.deliver(1)
        row = await self.repo.get(1)
        self.assertEqual(row["active"], 0)
        self.assertIsNone(row["next_run"])

    async def test_permanent_destination_error_stops_schedule(self):
        self.bot.send_message.side_effect = BadRequest("Chat not found")
        await self.service.deliver(1)
        row = await self.repo.get(1)
        self.assertEqual(row["active"], 0)
        self.assertEqual(row["current_page"], 1)

    async def test_surah_boundary(self):
        await self.repo.update(1, current_page=2)
        await self.service.deliver(1)
        row = await self.repo.get(1)
        self.assertEqual((row["current_index"], row["current_page"]), (1, 3))

    async def test_concurrent_deliveries_do_not_duplicate(self):
        await asyncio.gather(self.service.deliver(1), self.service.deliver(1))
        self.bot.send_message.assert_awaited_once()

    async def test_one_second_interval_does_not_send_early_or_duplicate(self):
        await self.repo.update(1, interval_seconds=1)
        await asyncio.gather(self.service.deliver(1), self.service.deliver(1))
        self.bot.send_message.assert_awaited_once()
        self.assertEqual((await self.repo.get(1))["current_page"], 2)

    async def test_invalid_schedule_is_not_reported_as_active(self):
        await self.repo.update(1, hour=None)
        await self.service.deliver(1)
        row = await self.repo.get(1)
        self.assertEqual(row["active"], 0)
        self.assertIsNotNone(row["last_error"])
        self.bot.send_message.assert_not_awaited()

    async def test_select_group_resume_restart_and_deliver(self):
        context = SimpleNamespace(user_data={"authenticated": True, "state": "group"})
        await self.handlers.message(self.update("-456"), context)
        await self.handlers.callback(self.update(data="menu_resume"), context)
        row = await self.repo.get(1)
        self.assertEqual(row["active"], 1)
        self.assertIsNotNone(row["next_run"])
        restarted = DeliveryService(self.config, Repository(self.path), self.surahs, self.bot)
        queued = []
        restarted.queue = lambda owner, timestamp: queued.append((owner, timestamp))
        await restarted.refresh(1)
        self.assertEqual(queued[0][1], row["next_run"])
        await self.repo.update(1, next_run=time.time() - 1)
        await restarted.deliver(1)
        self.assertEqual(self.bot.send_message.call_args.kwargs["chat_id"], -456)
        self.assertEqual((await self.repo.get(1))["current_page"], 2)

    async def test_stopped_schedule_stays_stopped_after_selection(self):
        await self.repo.update(1, active=0)
        await self.handlers.select(1, 1)
        self.assertEqual((await self.repo.get(1))["active"], 0)

    async def test_unauthenticated_callback_does_not_modify_database(self):
        await self.handlers.callback(self.update(data="menu_stop"), SimpleNamespace(user_data={}))
        self.assertEqual((await self.repo.get(1))["active"], 1)

    async def test_real_scheduler_runs_and_stops(self):
        service = DeliveryService(self.config, self.repo, self.surahs, self.bot)
        delivered = asyncio.Event()
        async def sent(**kwargs):
            delivered.set()
        self.bot.send_message.side_effect = sent
        try:
            await service.start()
            await asyncio.wait_for(delivered.wait(), timeout=5)
        finally:
            await service.stop()
        row = await self.repo.get(1)
        self.assertEqual(row["current_page"], 2)
        self.assertFalse(service.scheduler.running)

    async def test_restart_preserves_next_run(self):
        future = time.time() + 3600
        await self.repo.update(1, next_run=future)
        queued = []
        self.service.queue = lambda chat_id, timestamp: queued.append(timestamp)
        await self.service.refresh(1)
        self.assertEqual(queued, [future])

    async def test_invalid_input_keeps_prompt_and_settings(self):
        for value in ("0:0:0:0", "0:25:0:0", "invalid"):
            context = SimpleNamespace(user_data={"authenticated": True, "state": "interval"})
            await self.handlers.message(self.update(value), context)
            self.assertEqual(context.user_data["state"], "interval")
            self.assertEqual((await self.repo.get(1))["interval_seconds"], 5)

    async def test_navigation_cancels_old_prompt(self):
        context = SimpleNamespace(user_data={"authenticated": True, "state": "media"})
        await self.handlers.callback(self.update(data="menu_back"), context)
        self.assertNotIn("state", context.user_data)

    async def test_callback_edits_panel_without_new_message(self):
        update = self.update(data="menu_daily_settings")
        await self.handlers.callback(update, SimpleNamespace(user_data={"authenticated": True}))
        update.effective_message.edit_text.assert_awaited_once()
        update.effective_message.reply_text.assert_not_awaited()

    async def test_save_group_persists_and_pauses_existing_schedule(self):
        context = SimpleNamespace(user_data={"authenticated": True, "state": "group"})
        await self.handlers.message(self.update("-456"), context)
        row = await self.repo.get(1)
        self.assertEqual(row["destination_id"], -456)
        self.assertEqual(row["active"], 0)
        self.assertIsNone(row["next_run"])
        self.assertNotIn("state", context.user_data)
        self.bot.send_message.assert_not_awaited()

    async def test_invalid_group_keeps_previous_destination(self):
        await self.repo.update(1, destination_id=-789)
        self.bot.get_chat.side_effect = BadRequest("Chat not found")
        context = SimpleNamespace(user_data={"authenticated": True, "state": "group"})
        await self.handlers.message(self.update("-456"), context)
        self.assertEqual((await self.repo.get(1))["destination_id"], -789)
        self.assertEqual(context.user_data["state"], "group")

    async def test_native_group_picker_saves_matching_request_only(self):
        context = SimpleNamespace(user_data={"authenticated": True, "state": "group", "group_request": 55})
        update = self.update()
        update.effective_message.chat_shared = SimpleNamespace(request_id=54, chat_id=-456)
        await self.handlers.message(update, context)
        self.assertIsNone((await self.repo.get(1))["destination_id"])
        update.effective_message.chat_shared.request_id = 55
        await self.handlers.message(update, context)
        self.assertEqual((await self.repo.get(1))["destination_id"], -456)
        self.assertNotIn("group_request", context.user_data)

    async def test_group_cannot_be_changed_without_login(self):
        context = SimpleNamespace(user_data={"state": "group"})
        await self.handlers.message(self.update("-456"), context)
        self.assertIsNone((await self.repo.get(1))["destination_id"])
        self.bot.get_chat.assert_not_awaited()

    async def test_restricted_bot_cannot_be_selected(self):
        self.bot.get_chat_member.return_value = SimpleNamespace(status="restricted", is_member=True, can_send_messages=False)
        context = SimpleNamespace(user_data={"authenticated": True, "state": "group"})
        await self.handlers.message(self.update("-456"), context)
        self.assertIsNone((await self.repo.get(1))["destination_id"])

    async def test_scheduled_and_custom_sends_use_saved_group(self):
        await self.repo.update(1, destination_id=-456)
        await self.service.deliver(1)
        self.assertEqual(self.bot.send_message.call_args.kwargs["chat_id"], -456)
        await self.service.destination.copy(1, 1, 42)
        self.bot.copy_message.assert_awaited_once_with(chat_id=-456, from_chat_id=1, message_id=42)

    async def test_migrated_group_is_saved_and_retried(self):
        self.bot.send_message.side_effect = [ChatMigrated(-100456), None]
        await self.service.deliver(1)
        row = await self.repo.get(1)
        self.assertEqual(row["destination_id"], -100456)
        self.assertEqual(row["current_page"], 2)
        self.assertEqual(self.bot.send_message.call_args.kwargs["chat_id"], -100456)

    async def test_group_test_sends_without_advancing_reading(self):
        context = SimpleNamespace(user_data={"authenticated": True})
        before = await self.repo.get(1)
        await self.handlers.callback(self.update(data="group_test"), context)
        row = await self.repo.get(1)
        self.assertEqual((row["current_page"], row["next_run"]), (before["current_page"], before["next_run"]))
        self.bot.send_message.assert_awaited_once()
        self.assertEqual(self.bot.send_message.call_args.kwargs["chat_id"], -456)

    async def test_resume_checks_destination_before_activation(self):
        await self.repo.update(1, active=0, next_run=None)
        self.bot.get_chat.side_effect = BadRequest("Chat not found")
        await self.handlers.callback(self.update(data="menu_resume"), SimpleNamespace(user_data={"authenticated": True}))
        row = await self.repo.get(1)
        self.assertEqual(row["active"], 0)
        self.assertIn("گروه پیدا نشد", row["last_error"])

    async def test_changed_destination_invalidates_pending_media(self):
        await self.repo.update(1, destination_id=-456)
        context = SimpleNamespace(user_data={"authenticated": True, "pending_media":
            dict(nonce="abc", target=-123, expires=time.monotonic() + 60, chat_id=1, message_id=42)})
        await self.handlers.callback(self.update(data="media_confirm:abc"), context)
        self.bot.copy_message.assert_not_awaited()

    async def test_permanent_error_records_reason_and_notifies_owner(self):
        self.bot.send_message.side_effect = [BadRequest("Chat not found"), None]
        await self.service.deliver(1)
        row = await self.repo.get(1)
        self.assertIn("گروه پیدا نشد", row["last_error"])
        self.assertEqual(self.bot.send_message.call_args.kwargs["chat_id"], 1)

    async def test_interval_and_time_presets(self):
        context = SimpleNamespace(user_data={"authenticated": True})
        await self.handlers.callback(self.update(data="interval:daily"), context)
        await self.handlers.callback(self.update(data="time:21:00"), context)
        row = await self.repo.get(1)
        self.assertEqual((row["interval_days"], row["interval_seconds"], row["hour"]), (1, 0, 21))

    async def test_preview_does_not_send_or_advance(self):
        before = await self.repo.get(1)
        update = self.update(data="menu_preview")
        await self.handlers.callback(update, SimpleNamespace(user_data={"authenticated": True}))
        self.assertEqual(await self.repo.get(1), before)
        self.bot.send_message.assert_not_awaited()
        self.assertIn("پیش‌نمایش", update.effective_message.edit_text.call_args.args[0])

    async def test_media_confirmation_is_one_use(self):
        context = SimpleNamespace(user_data={"authenticated": True, "pending_media":
            dict(nonce="abc", expires=time.monotonic() + 60, chat_id=1, message_id=42)},
            bot=self.bot)
        await self.handlers.callback(self.update(data="media_confirm:abc"), context)
        await self.handlers.callback(self.update(data="media_confirm:abc"), context)
        context.bot.copy_message.assert_awaited_once_with(chat_id=-123, from_chat_id=1, message_id=42)

    async def test_media_input_waits_for_confirmation(self):
        update = self.update("یک پیام آزمایشی")
        message = update.effective_message
        for field in ("photo", "video", "animation", "sticker", "audio", "voice", "document",
                      "video_note", "contact", "location", "media_group_id"):
            setattr(message, field, None)
        message.message_id = 42
        context = SimpleNamespace(user_data={"authenticated": True, "state": "media"},
                                  bot=self.bot)
        await self.handlers.message(update, context)
        self.assertEqual(context.user_data["pending_media"]["message_id"], 42)
        self.assertNotIn("state", context.user_data)
        context.bot.copy_message.assert_not_awaited()

    async def test_cancel_invalidates_pending_send(self):
        context = SimpleNamespace(user_data={"authenticated": True, "pending_media":
            dict(nonce="abc", expires=time.monotonic() + 60, chat_id=1, message_id=42)},
            bot=self.bot)
        await self.handlers.cancel(self.update(), context)
        await self.handlers.callback(self.update(data="media_confirm:abc"), context)
        context.bot.copy_message.assert_not_awaited()

    async def test_partial_surah_search_keeps_input_active(self):
        context = SimpleNamespace(user_data={"authenticated": True, "state": "surah"})
        update = self.update("ال")
        await self.handlers.message(update, context)
        self.assertEqual(context.user_data["state"], "surah")
        markup = update.effective_message.reply_text.call_args.kwargs["reply_markup"]
        self.assertEqual(markup.inline_keyboard[0][0].callback_data, "surah_pick:0")

    async def test_expired_media_confirmation_does_not_send(self):
        context = SimpleNamespace(user_data={"authenticated": True, "pending_media":
            dict(nonce="abc", expires=time.monotonic() - 1, chat_id=1, message_id=42)},
            bot=self.bot)
        await self.handlers.callback(self.update(data="media_confirm:abc"), context)
        context.bot.copy_message.assert_not_awaited()

    async def test_render_ignores_unchanged_message(self):
        message = self.update().effective_message
        message.edit_text.side_effect = BadRequest("Message is not modified")
        await render(message, "same", edit=True)
        message.reply_text.assert_not_awaited()

    async def test_render_falls_back_for_uneditable_message(self):
        message = self.update().effective_message
        message.edit_text.side_effect = BadRequest("Message can't be edited")
        await render(message, "new", edit=True)
        message.reply_text.assert_awaited_once()

    async def test_invalid_callback_does_not_change_selection(self):
        context = SimpleNamespace(user_data={"authenticated": True})
        await self.handlers.callback(self.update(data="select_surah_page_999_999"), context)
        self.assertEqual((await self.repo.get(1))["current_index"], 0)

    async def test_password_throttle(self):
        context = SimpleNamespace(user_data={})
        for _ in range(5):
            await self.handlers.authenticate(self.update("wrong"), context)
        await self.handlers.authenticate(self.update("secret"), context)
        self.assertFalse(context.user_data.get("authenticated", False))

    async def test_native_asyncio_anyio(self):
        self.assertIsNotNone(asyncio.current_task())
        with anyio.CancelScope(shield=True):
            await asyncio.sleep(0)

    async def test_migration_preserves_legacy_data(self):
        path = Path(self.temp.name) / "legacy.db"
        with closing(sqlite3.connect(path)) as db:
            with db:
                db.execute("CREATE TABLE settings(chat_id INTEGER PRIMARY KEY, current_index INTEGER)")
                db.execute("INSERT INTO settings VALUES (7, 2)")
        legacy = Repository(path)
        await legacy.initialize()
        await legacy.initialize()
        row = await legacy.get(7)
        self.assertEqual(row["current_index"], 2)
        self.assertEqual(row["current_day"], 1)
        self.assertIsNone(row["next_run"])


class ConfigTests(unittest.TestCase):
    def test_review_debug_does_not_log_telegram_passwords(self):
        from quran_bot.application import main
        output = io.StringIO()
        handler = logging.StreamHandler(output)
        root = logging.getLogger()
        previous = root.level
        loggers = [logging.getLogger(name) for name in ("telegram", "httpx", "httpcore")]
        levels = [logger.level for logger in loggers]
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        app = Mock()
        app.run_polling.side_effect = lambda **kwargs: logging.getLogger("telegram.ext.Application").debug("Processing update password=private-test-password")
        try:
            with patch("quran_bot.application.Config.load", return_value=SimpleNamespace(log_level="DEBUG")), patch("quran_bot.application.build_application", return_value=app):
                main()
            self.assertNotIn("private-test-password", output.getvalue())
        finally:
            root.removeHandler(handler)
            root.setLevel(previous)
            for logger, level in zip(loggers, levels):
                logger.setLevel(level)
    def test_colored_button_serialization(self):
        payload = button("شروع", "menu_resume", "success").to_dict()
        self.assertEqual(payload["style"], "success")

    def test_picker_covers_catalog_without_changing_indices(self):
        surahs = load_surahs(ROOT / "surah_list.txt")
        indices = []
        for page in range((len(surahs) + 11) // 12):
            _, markup = surah_picker(surahs, page)
            for row in markup.inline_keyboard:
                for item in row:
                    self.assertLessEqual(len(item.callback_data.encode()), 64)
                    if item.callback_data.startswith("surah_pick:"):
                        index = int(item.callback_data.split(":")[1])
                        self.assertEqual(item.text, surahs[index]["name"])
                        indices.append(index)
        self.assertEqual(sorted(indices), list(range(len(surahs))))

    def test_copy_button_respects_telegram_limit(self):
        self.assertIsNotNone(preview_markup("short").inline_keyboard[0][0].copy_text)
        self.assertIsNone(preview_markup("x" * 257).inline_keyboard[0][0].copy_text)

    def test_dashboard_escapes_catalog_html(self):
        text = dashboard(dict(current_index=0, current_page=1),
                         [dict(name="<b>test</b>", start_page=1, end_page=2)], "Asia/Tehran")
        self.assertIn("&lt;b&gt;test&lt;/b&gt;", text)

    def test_catalog_loads_existing_order(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "surahs.txt"
            path.write_text("علق|597|597\nفاتحه|1|1\n", encoding="utf-8")
            surahs = load_surahs(path)
            self.assertEqual([surah["name"] for surah in surahs], ["علق", "فاتحه"])

    def test_env_and_relative_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text('BOT_TOKEN=123:fake\nGROUP_ID=-123\nADMIN_PASSWORD="رمز"\n', encoding="utf-8-sig")
            with patch.dict("os.environ", {}, clear=True):
                config = Config.load(root)
            self.assertEqual(config.db_path, root / "bot_data.db")
            self.assertEqual(config.password, "رمز")

    def test_env_group_is_optional(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text("BOT_TOKEN=123:fake\nADMIN_PASSWORD=test\n", encoding="utf-8")
            with patch.dict("os.environ", {}, clear=True):
                self.assertIsNone(Config.load(root).group_id)

    def test_full_polling_lifecycle_without_network(self):
        calls = []
        async def response(request, url, method, **kwargs):
            endpoint = url.rsplit("/", 1)[-1]
            calls.append(endpoint)
            if endpoint == "getMe":
                result = dict(id=123, is_bot=True, first_name="Test", username="test_bot")
            elif endpoint == "getUpdates":
                await asyncio.sleep(0.02)
                result = []
            else:
                result = True
            return 200, json.dumps(dict(ok=True, result=result)).encode()
        with tempfile.TemporaryDirectory() as directory:
            config = Config("123:fake", -123, "secret", "Asia/Tehran", Path(directory) / "test.db",
                            ROOT / "surah_list.txt", frozenset())
            with asyncio.Runner() as runner, patch.object(HTTPXRequest, "do_request", response):
                runner.get_loop()
                app = build_application(config)
                original = app.post_init
                async def startup(application):
                    await original(application)
                    asyncio.get_running_loop().call_later(0.2, application.stop_running)
                app.post_init = startup
                app.run_polling(close_loop=False, stop_signals=None)
                self.assertFalse(app.running)
                self.assertFalse(app.bot_data["service"].scheduler.running)
            self.assertIn("getMe", calls)
            self.assertIn("deleteWebhook", calls)
            self.assertIn("getUpdates", calls)
            self.assertIn("setMyCommands", calls)
            self.assertIn("setChatMenuButton", calls)


if __name__ == "__main__":
    unittest.main()

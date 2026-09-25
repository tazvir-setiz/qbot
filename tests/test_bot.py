import asyncio
import json
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import anyio
from telegram.error import BadRequest, NetworkError
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
        self.bot = SimpleNamespace(send_message=AsyncMock())
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
            bot=SimpleNamespace(copy_message=AsyncMock()))
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
                                  bot=SimpleNamespace(copy_message=AsyncMock()))
        await self.handlers.message(update, context)
        self.assertEqual(context.user_data["pending_media"]["message_id"], 42)
        self.assertNotIn("state", context.user_data)
        context.bot.copy_message.assert_not_awaited()

    async def test_cancel_invalidates_pending_send(self):
        context = SimpleNamespace(user_data={"authenticated": True, "pending_media":
            dict(nonce="abc", expires=time.monotonic() + 60, chat_id=1, message_id=42)},
            bot=SimpleNamespace(copy_message=AsyncMock()))
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
            bot=SimpleNamespace(copy_message=AsyncMock()))
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
        surahs = load_surahs(ROOT / "surah_list.txt")
        self.assertEqual(surahs[0]["name"], "علق")
        self.assertGreater(len(surahs), 0)

    def test_env_and_relative_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text('BOT_TOKEN=123:fake\nGROUP_ID=-123\nADMIN_PASSWORD="رمز"\n', encoding="utf-8-sig")
            with patch.dict("os.environ", {}, clear=True):
                config = Config.load(root)
            self.assertEqual(config.db_path, root / "bot_data.db")
            self.assertEqual(config.password, "رمز")

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

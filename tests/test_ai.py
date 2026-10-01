import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from quran_bot.ai import AIService
from quran_bot.database import Repository


class AITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Repository(Path(self.temp.name) / "ai.db")
        await self.repo.initialize()

    async def test_ai_settings_are_disabled_by_default(self):
        await self.repo.ensure(1)
        row = await self.repo.get(1)
        self.assertEqual(row["ai_enabled"], 0)
        self.assertIsNone(await self.repo.find_ai_owner(-1001))

    async def test_enabled_destination_can_be_resolved(self):
        await self.repo.update(
            1,
            destination_id=-1001,
            ai_enabled=1,
            ai_base_url="https://provider.example/v1",
            ai_api_key="secret-key",
            ai_model="model-name",
        )
        row = await self.repo.find_ai_owner(-1001)
        self.assertIsNotNone(row)
        self.assertEqual(row["chat_id"], 1)

    async def test_media_library_round_trip(self):
        await self.repo.save_ai_media(1, "media_1", "sticker", "file-id", "خوشحالی")
        media = await self.repo.list_ai_media(1)
        self.assertEqual(len(media), 1)
        self.assertEqual(media[0]["description"], "خوشحالی")
        await self.repo.delete_ai_media(1, "media_1")
        self.assertEqual(await self.repo.list_ai_media(1), [])

    def test_parse_structured_reply(self):
        reply = AIService._parse_reply('{"text":"سلام","media_key":"media_1"}')
        self.assertEqual(reply.text, "سلام")
        self.assertEqual(reply.media_key, "media_1")

    def test_mention_is_removed_from_question(self):
        service = AIService(self.repo)
        service._bot_username = "MorselBot"
        message = SimpleNamespace(text="@MorselBot سوال من چیه؟", caption=None, reply_to_message=None)
        self.assertEqual(service._question(message), "سوال من چیه؟")


if __name__ == "__main__":
    unittest.main()

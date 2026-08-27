import asyncio
import logging
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite

try:
    import astrbot
except ImportError:
    import types
    astrbot_module = types.ModuleType("astrbot")
    astrbot_api_module = types.ModuleType("astrbot.api")
    astrbot_api_module.logger = logging.getLogger("astrbot")
    astrbot_module.api = astrbot_api_module
    sys.modules["astrbot"] = astrbot_module
    sys.modules["astrbot.api"] = astrbot_api_module

from core.database import AvatarCacheDB
from core.prompts import get_avatar_caption_prompt
from core.avatar_fetcher import AvatarFetcher
from core.giftia_sync import sync_avatar_to_giftia
from main import GiftiaExTool


class AvatarCacheDBTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "test_avatar.db"
        self.db = AvatarCacheDB(self.db_path)
        await self.db.initialize()

    async def asyncTearDown(self):
        await self.db.close()
        self.tmp_dir.cleanup()

    async def test_cache_miss_and_hit(self):
        # 1. Miss
        desc = await self.db.get_avatar_description("user_123")
        self.assertIsNone(desc)

        # 2. Save
        await self.db.save_avatar_description(
            user_id="user_123",
            avatar_description="二次元金发少女，戴着红帽子",
            avatar_url="https://q1.qlogo.cn/fake",
        )

        # 3. Hit
        cached = await self.db.get_avatar_description("user_123")
        self.assertEqual(cached, "二次元金发少女，戴着红帽子")

    async def test_cache_upsert_overwrite(self):
        await self.db.save_avatar_description(
            user_id="user_123",
            avatar_description="旧描述",
        )
        await self.db.save_avatar_description(
            user_id="user_123",
            avatar_description="新描述内容",
        )
        cached = await self.db.get_avatar_description("user_123")
        self.assertEqual(cached, "新描述内容")


class AvatarFetcherTests(unittest.TestCase):
    def test_parse_caption_text_json(self):
        raw_json = '```json\n{\n  "avatar_description": "黑猫趴在窗台上晒太阳"\n}\n```'
        parsed = AvatarFetcher._parse_caption_text(raw_json)
        self.assertEqual(parsed, "黑猫趴在窗台上晒太阳")

    def test_parse_caption_text_plain(self):
        raw_text = "这是一张二次元机甲头像，科技感很强"
        parsed = AvatarFetcher._parse_caption_text(raw_text)
        self.assertEqual(parsed, "这是一张二次元机甲头像，科技感很强")


class GiftiaExToolMainTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.mock_context = MagicMock()
        self.config = {
            "inspect_user_avatar_enabled": True,
            "caption_provider_ids": ["provider_1", "provider_2"],
            "auto_sync_to_giftia": True,
        }
        with patch("astrbot.api.star.StarTools.get_data_dir", return_value=self.tmp_dir.name):
            self.plugin = GiftiaExTool(self.mock_context, self.config)
            await self.plugin.initialize()

    async def asyncTearDown(self):
        await self.plugin.terminate()
        self.tmp_dir.cleanup()

    async def test_tool_cached_flow(self):
        # Seed cache
        await self.plugin.db.save_avatar_description(
            user_id="10001",
            avatar_description="戴着耳机的电竞少年",
        )

        mock_event = MagicMock(spec=["get_sender_id", "get_group_id", "platform_meta"])
        mock_event.get_sender_id.return_value = "10001"
        mock_event.get_group_id.return_value = "group_1"
        mock_event.platform_meta.id = "aiocqhttp"

        result = await self.plugin.inspect_user_avatar(
            event=mock_event,
            user_id="10001",
            force_refresh=False,
        )

        self.assertIn("本地缓存", result)
        self.assertIn("戴着耳机的电竞少年", result)

    async def test_tool_uncached_download_and_caption_flow(self):
        mock_event = MagicMock(spec=["get_sender_id", "get_group_id", "platform_meta"])
        mock_event.get_sender_id.return_value = "10002"
        mock_event.get_group_id.return_value = "group_1"
        mock_event.platform_meta.id = "aiocqhttp"

        # Mock download and LLM
        self.plugin.fetcher.download_avatar_bytes = AsyncMock(
            return_value=(b"fake_image_bytes_250_len_here", "https://q1.qlogo.cn/avatar")
        )
        self.plugin.fetcher.caption_avatar_with_llm = AsyncMock(
            return_value="阳光开朗的柴犬特写，笑容灿烂"
        )

        result = await self.plugin.inspect_user_avatar(
            event=mock_event,
            user_id="10002",
            force_refresh=False,
        )

        self.assertIn("阳光开朗的柴犬特写，笑容灿烂", result)
        # Check SQLite cached
        cached = await self.plugin.db.get_avatar_description("10002")
        self.assertEqual(cached, "阳光开朗的柴犬特写，笑容灿烂")

    async def test_tool_disabled_check(self):
        self.plugin.conf["inspect_user_avatar_enabled"] = False
        res = await self.plugin.inspect_user_avatar(event=MagicMock(), user_id="123")
        self.assertIn("未启用", res)


if __name__ == "__main__":
    unittest.main()

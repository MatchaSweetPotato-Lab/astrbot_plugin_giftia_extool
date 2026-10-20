from __future__ import annotations

import asyncio
import hashlib
from contextlib import suppress
from datetime import datetime
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core import AstrBotConfig

try:
    from .core.avatar_fetcher import AvatarFetcher
    from .core.avatar_monitor import CHECK_TIMEZONE, check_active_avatars
    from .core.database import AvatarCacheDB
    from .core.giftia_sync import get_giftia_instance, sync_avatar_to_giftia
except ImportError:
    from core.avatar_fetcher import AvatarFetcher
    from core.avatar_monitor import CHECK_TIMEZONE, check_active_avatars
    from core.database import AvatarCacheDB
    from core.giftia_sync import get_giftia_instance, sync_avatar_to_giftia


@register("giftia_extool", "e.e.", "支持 giftia 的更多 llm 工具", "1.0.0")
class GiftiaExTool(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.conf = config or {}
        data_dir = Path(StarTools.get_data_dir("astrbot_plugin_giftia_extool"))
        self.db = AvatarCacheDB(data_dir / "avatar_cache.db")
        self.fetcher = AvatarFetcher(context)
        self._daily_task: asyncio.Task | None = None

    def _get_avatar_conf(self) -> dict:
        """Read the avatar configuration group.

        Returns:
            Avatar options, or an empty mapping when unconfigured.
        """
        if isinstance(self.conf, dict):
            return self.conf.get("avatar_config", {})
        return {}

    def _is_tool_enabled(self) -> bool:
        avatar_conf = self._get_avatar_conf()
        if "inspect_user_avatar_enabled" in avatar_conf:
            return bool(avatar_conf.get("inspect_user_avatar_enabled", True))
        return bool(self.conf.get("inspect_user_avatar_enabled", True))

    def _get_caption_provider_ids(self) -> list[str]:
        avatar_conf = self._get_avatar_conf()
        if "caption_provider_ids" in avatar_conf:
            return avatar_conf.get("caption_provider_ids", [])
        return self.conf.get("caption_provider_ids", [])

    def _is_auto_sync_enabled(self) -> bool:
        avatar_conf = self._get_avatar_conf()
        if "auto_sync_to_giftia" in avatar_conf:
            return bool(avatar_conf.get("auto_sync_to_giftia", True))
        return bool(self.conf.get("auto_sync_to_giftia", True))

    async def initialize(self):
        """Initialize the cache, tool registration and optional daily task."""
        await self.db.initialize()
        if self._is_tool_enabled():
            self.context.activate_llm_tool("inspect_user_avatar")
            logger.info("[Giftia ExTool] Avatar tool enabled.")
        else:
            self.context.deactivate_llm_tool("inspect_user_avatar")
            logger.info("[Giftia ExTool] Avatar tool disabled.")

        if self._get_avatar_conf().get("daily_avatar_check_enabled", False):
            self._daily_task = asyncio.create_task(
                self._daily_avatar_loop(), name="giftia-daily-avatar-check"
            )

    async def _daily_avatar_loop(self) -> None:
        """Run one pass per date after 01:00 UTC+08:00, including startup catch-up."""
        while True:
            delay = 60.0
            try:
                now = datetime.now(CHECK_TIMEZONE)
                due = now.replace(hour=1, minute=0, second=0, microsecond=0)
                enabled = self._get_avatar_conf().get(
                    "daily_avatar_check_enabled", False
                )
                if enabled and self._is_auto_sync_enabled() and now >= due:
                    today = now.date().isoformat()
                    last_date = await self.get_kv_data("daily_avatar_check_date", "")
                    if last_date != today:
                        stats = await check_active_avatars(
                            self.context,
                            self.db,
                            self.fetcher,
                            self._get_caption_provider_ids(),
                            now,
                        )
                        if stats is not None:
                            await self.put_kv_data("daily_avatar_check_date", today)
                            logger.info(
                                "[Giftia ExTool] Daily avatar check complete: "
                                "date=%s checked=%s filled=%s changed=%s "
                                "baselined=%s unchanged=%s failed=%s",
                                today,
                                stats["checked"],
                                stats["filled"],
                                stats["changed"],
                                stats["baselined"],
                                stats["unchanged"],
                                stats["failed"],
                            )
                elif now < due:
                    delay = min(delay, (due - now).total_seconds())
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "[Giftia ExTool] Daily avatar check interrupted; retrying."
                )
            await asyncio.sleep(delay)

    @filter.llm_tool(name="inspect_user_avatar")
    async def inspect_user_avatar(
        self,
        event: AstrMessageEvent,
        user_id: str,
        force_refresh: bool = False,
    ) -> str:
        """Inspect a user's avatar, reusing a cached visual description by default.

        Args:
            user_id(string): User ID or QQ account; empty uses the sender's ID.
            force_refresh(boolean): Download and caption again, default false.

        Returns:
            Avatar description or an explanation of the failure.
        """
        clean_user_id = str(user_id or "").strip()
        if not clean_user_id and event:
            clean_user_id = str(event.get_sender_id() or "").strip()
        if not clean_user_id:
            return "查看头像失败：未提供有效的 user_id"
        if not self._is_tool_enabled():
            return "头像查看工具当前未启用。"

        giftia = get_giftia_instance()
        bot_name = ""
        conversation_id = ""
        cache_key = clean_user_id
        if event:
            conversation_id = str(event.get_group_id() or event.get_sender_id() or "")
            platform_meta = getattr(event, "platform_meta", None)
            platform_id = getattr(platform_meta, "id", "")
            if giftia:
                bot_name = giftia.adapter_id_map.get(platform_id, "")
            platform_name = getattr(platform_meta, "name", "")
            if isinstance(platform_name, str) and platform_name not in (
                "",
                "aiocqhttp",
            ):
                cache_key = f"{platform_name}:{platform_id}:{clean_user_id}"

        async with self.fetcher.user_locks[cache_key]:
            cached = await self.db.get_avatar_record(cache_key, bot_name)
            from_cache = bool(
                cached and cached["avatar_description"] and not force_refresh
            )
            if from_cache:
                description = cached["avatar_description"]
                avatar_url = cached["avatar_url"] or ""
                avatar_hash = cached["avatar_hash"]
            else:
                image_bytes, avatar_url = await self.fetcher.download_avatar_bytes(
                    clean_user_id, event=event
                )
                if not image_bytes:
                    return f"获取用户 {clean_user_id} 的头像图片失败（可能受网络或平台限制影响）。"
                avatar_hash = hashlib.sha256(image_bytes).hexdigest()
                description = await self.fetcher.caption_avatar_with_llm(
                    image_bytes=image_bytes,
                    provider_ids=self._get_caption_provider_ids(),
                )
                if not description:
                    return f"用户 {clean_user_id} 的头像转述失败（视觉大模型未返回有效描述）。"
                await self.db.save_avatar_description(
                    cache_key, description, avatar_url, avatar_hash
                )

            if self._is_auto_sync_enabled() and bot_name:
                success = await sync_avatar_to_giftia(
                    user_id=clean_user_id,
                    avatar_description=description,
                    bot_name=bot_name,
                    group_or_user_id=conversation_id,
                    giftia=giftia,
                )
                if success and avatar_hash:
                    await self.db.save_avatar_description(
                        cache_key,
                        description,
                        avatar_url,
                        avatar_hash,
                        synced_bot_name=bot_name,
                    )

            label = "头像描述（本地缓存）" if from_cache else "头像转述结果"
            return f"用户 {clean_user_id} 的{label}:\n{description}"

    async def terminate(self):
        """Cancel maintenance before closing the avatar database."""
        if self._daily_task is not None:
            self._daily_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._daily_task
            self._daily_task = None
        await self.db.close()
        logger.info("[Giftia ExTool] Plugin stopped; avatar cache closed.")

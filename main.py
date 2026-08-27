from __future__ import annotations

from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core import AstrBotConfig

try:
    from .core.avatar_fetcher import AvatarFetcher
    from .core.database import AvatarCacheDB
    from .core.giftia_sync import sync_avatar_to_giftia
except ImportError:
    from core.avatar_fetcher import AvatarFetcher
    from core.database import AvatarCacheDB
    from core.giftia_sync import sync_avatar_to_giftia


@register("giftia_extool", "e.e.", "支持 giftia 的更多 llm 工具", "1.0.0")
class GiftiaExTool(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.context: Context = context
        self.conf = config or {}

        # 数据目录与 SQLite 缓存初始化
        data_dir = Path(StarTools.get_data_dir("astrbot_plugin_giftia_extool"))
        db_path = data_dir / "avatar_cache.db"
        self.db = AvatarCacheDB(db_path)
        self.fetcher = AvatarFetcher(self.context)

    def _get_avatar_conf(self) -> dict:
        """获取头像工具相关配置"""
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
        """插件初始化"""
        await self.db.initialize()

        # 根据配置动态开启/关闭工具
        enabled = self._is_tool_enabled()
        if enabled:
            if hasattr(self.context, "activate_llm_tool"):
                try:
                    self.context.activate_llm_tool("inspect_user_avatar")
                except Exception:
                    pass
            logger.info("[Giftia ExTool] 已激活 inspect_user_avatar 工具。")
        else:
            if hasattr(self.context, "deactivate_llm_tool"):
                try:
                    self.context.deactivate_llm_tool("inspect_user_avatar")
                except Exception:
                    pass
            logger.info("[Giftia ExTool] inspect_user_avatar 工具已按配置停用。")

    @filter.llm_tool(name="inspect_user_avatar")
    async def inspect_user_avatar(
        self,
        event: AstrMessageEvent,
        user_id: str,
        force_refresh: bool = False,
    ) -> str:
        """查看并分析指定用户的头像视觉特征。若之前已分析过且未指定强制刷新，将直接复用本地缓存。

        Args:
            user_id(string): 目标用户的数字ID/QQ号/用户唯一标识。
            force_refresh(boolean): 是否强制重新下载头像并调用大模型重新转述（为 false 且已有缓存时直接返回缓存），默认 false。
        """
        clean_user_id = str(user_id or "").strip()
        if not clean_user_id:
            if event and hasattr(event, "get_sender_id"):
                clean_user_id = str(event.get_sender_id() or "").strip()
        if not clean_user_id:
            return "查看头像失败：未提供有效的 user_id"

        if not self._is_tool_enabled():
            return "头像查看工具当前未启用。"

        # 尝试解析会话信息（用于 Giftia 画像同步）
        bot_name = ""
        group_or_user_id = ""
        if event:
            if hasattr(event, "get_group_id") and hasattr(event, "get_sender_id"):
                group_or_user_id = event.get_group_id() or event.get_sender_id() or ""
            platform_id = getattr(getattr(event, "platform_meta", None), "id", None)
            if platform_id:
                bot_name = str(platform_id)

        # 1. 优先读取 SQLite 缓存
        if not force_refresh:
            cached_desc = await self.db.get_avatar_description(clean_user_id)
            if cached_desc:
                logger.info(f"[Giftia ExTool] 命中本地头像缓存: user_id={clean_user_id}")
                # 尝试同步至 Giftia 画像
                if self._is_auto_sync_enabled():
                    await sync_avatar_to_giftia(
                        user_id=clean_user_id,
                        avatar_description=cached_desc,
                        bot_name=bot_name,
                        group_or_user_id=group_or_user_id,
                    )
                return f"用户 {clean_user_id} 的头像描述（本地缓存）:\n{cached_desc}"

        # 2. 下载头像图片
        logger.info(f"[Giftia ExTool] 开始下载用户 {clean_user_id} 的头像 (force_refresh={force_refresh})...")
        image_bytes, avatar_url = await self.fetcher.download_avatar_bytes(
            clean_user_id, event=event
        )
        if not image_bytes:
            return f"获取用户 {clean_user_id} 的头像图片失败（可能受网络或平台限制影响）。"

        # 3. 调用视觉大模型转述
        providers = self._get_caption_provider_ids()
        desc = await self.fetcher.caption_avatar_with_llm(
            image_bytes=image_bytes,
            provider_ids=providers,
        )
        if not desc:
            return f"用户 {clean_user_id} 的头像转述失败（视觉大模型未返回有效描述）。"

        # 4. 写入本地 SQLite 缓存
        await self.db.save_avatar_description(
            user_id=clean_user_id,
            avatar_description=desc,
            avatar_url=avatar_url,
        )
        logger.info(f"[Giftia ExTool] 用户 {clean_user_id} 的头像描述已更新至本地缓存表。")

        # 5. 自动同步写入 Giftia 用户画像
        if self._is_auto_sync_enabled():
            await sync_avatar_to_giftia(
                user_id=clean_user_id,
                avatar_description=desc,
                bot_name=bot_name,
                group_or_user_id=group_or_user_id,
            )

        return f"用户 {clean_user_id} 的头像转述结果:\n{desc}"

    async def terminate(self):
        """插件销毁"""
        await self.db.close()
        logger.info("[Giftia ExTool] 插件已卸载，数据库连接已关闭。")

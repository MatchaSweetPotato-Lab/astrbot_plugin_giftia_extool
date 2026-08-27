from __future__ import annotations

from typing import Any
from astrbot.api import logger


def get_giftia_instance() -> Any | None:
    """安全获取运行中的 Giftia 插件实例"""
    try:
        from astrbot.core.star.star_manager import star_map
        if "giftia" in star_map:
            return star_map["giftia"]
        if "astrbot_plugin_giftia" in star_map:
            return star_map["astrbot_plugin_giftia"]
        for inst in star_map.values():
            if hasattr(inst, "data_cache") and hasattr(inst.data_cache, "set_user_profile"):
                return inst
    except Exception as e:
        logger.debug(f"[Giftia ExTool] 查找 Giftia 实例异常: {e}")
    return None


async def sync_avatar_to_giftia(
    user_id: str,
    avatar_description: str,
    bot_name: str = "",
    group_or_user_id: str = "",
) -> bool:
    """将头像描述同步写入 Giftia 的用户画像库中"""
    giftia = get_giftia_instance()
    if not giftia:
        logger.debug("[Giftia ExTool] 未检测到运行中的 Giftia 插件，跳过画像同步。")
        return False

    try:
        if hasattr(giftia, "data_cache") and hasattr(giftia.data_cache, "set_user_profile"):
            await giftia.data_cache.set_user_profile(
                bot_name=bot_name,
                group_or_user_id=group_or_user_id,
                user_id=user_id,
                profile_fields={"avatar_description": avatar_description},
                alias_increment_count=False,
            )
            logger.info(f"[Giftia ExTool] 已成功将用户 {user_id} 的头像描述同步写入 Giftia 画像库。")
            return True
    except Exception as e:
        logger.warning(f"[Giftia ExTool] 同步头像描述至 Giftia 画像库失败: {e}")
    return False

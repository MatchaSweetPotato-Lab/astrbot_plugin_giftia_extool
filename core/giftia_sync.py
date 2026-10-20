from __future__ import annotations

from typing import Any

from astrbot.api import logger


def get_giftia_instance() -> Any | None:
    """Find the active Giftia instance in AstrBot's plugin metadata.

    Returns:
        Running Giftia plugin, or None when it is not ready.
    """
    try:
        from astrbot.core.star.star_manager import star_map

        for metadata in star_map.values():
            if (
                metadata.name not in ("giftia", "astrbot_plugin_giftia")
                or not metadata.activated
            ):
                continue
            instance = metadata.star_cls
            if instance is not None and getattr(instance, "data_cache", None):
                return instance
    except Exception as e:
        logger.debug("[Giftia ExTool] Giftia lookup failed: %s", e)
    return None


async def sync_avatar_to_giftia(
    user_id: str,
    avatar_description: str,
    bot_name: str = "",
    group_or_user_id: str = "",
    existing_only: bool = False,
    giftia: Any | None = None,
) -> bool:
    """Write an avatar field through Giftia's shared-profile cache interface.

    Args:
        user_id: Target user account ID.
        avatar_description: Caption to save.
        bot_name: Giftia bot configuration name, not an AstrBot adapter ID.
        group_or_user_id: Source conversation, empty for shared-profile upkeep.
        existing_only: Whether to skip users whose profiles have been deleted.
        giftia: Running instance already resolved by a maintenance job.

    Returns:
        Whether the shared profile was successfully updated.
    """
    if giftia is None:
        giftia = get_giftia_instance()
    if giftia is None or not bot_name:
        logger.debug(
            "[Giftia ExTool] Giftia or bot mapping unavailable; skipping sync."
        )
        return False

    try:
        if existing_only:
            async with giftia.db.conn.execute(
                "SELECT 1 FROM user_profiles WHERE bot_name = ? AND user_id = ?",
                (bot_name, user_id),
            ) as cursor:
                if await cursor.fetchone() is None:
                    return False
        if hasattr(giftia, "data_cache") and hasattr(
            giftia.data_cache, "set_user_profile"
        ):
            await giftia.data_cache.set_user_profile(
                bot_name=bot_name,
                group_or_user_id=group_or_user_id,
                user_id=user_id,
                profile_fields={"avatar_description": avatar_description},
                alias_increment_count=False,
            )
            logger.info(
                "[Giftia ExTool] Avatar synced: bot=%s user=%s", bot_name, user_id
            )
            return True
    except Exception as e:
        logger.warning("[Giftia ExTool] Avatar sync failed: %s", e)
    return False

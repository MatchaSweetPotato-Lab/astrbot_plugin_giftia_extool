from __future__ import annotations

import asyncio
import hashlib
from collections import defaultdict
from datetime import datetime, time, timedelta, timezone

from astrbot.api import logger
from astrbot.api.star import Context

from .avatar_fetcher import AvatarFetcher
from .database import AvatarCacheDB
from .giftia_sync import get_giftia_instance, sync_avatar_to_giftia

CHECK_TIMEZONE = timezone(timedelta(hours=8))


async def check_active_avatars(
    context: Context,
    cache: AvatarCacheDB,
    fetcher: AvatarFetcher,
    provider_ids: list[str],
    now: datetime,
) -> dict[str, int] | None:
    """Refresh existing shared profiles belonging to yesterday's active QQ users.

    Args:
        context: AstrBot context used to resolve OneBot adapters.
        cache: Shared avatar cache with per-bot synchronization fingerprints.
        fetcher: Downloader and captioner, also owning per-user operation locks.
        provider_ids: Ordered vision provider IDs.
        now: Current aware time; the activity window uses UTC+08:00.

    Returns:
        Counts for the completed pass, or None when Giftia or QQ is not ready.
    """
    giftia = get_giftia_instance()
    if giftia is None or not getattr(getattr(giftia, "db", None), "conn", None):
        return None

    stats = dict.fromkeys(
        ("checked", "filled", "changed", "baselined", "unchanged", "failed"), 0
    )
    bot_names = []
    platforms = {}
    for bot_name, bot_config in giftia.bot_map.items():
        adapter_ids = bot_config.get("adapter_ids", [])
        if not adapter_ids:
            continue
        bot_platforms = [context.get_platform_inst(aid) for aid in adapter_ids]
        if any(platform is None for platform in bot_platforms):
            return None
        # Giftia history has no platform column. Mixed-platform IDs are ambiguous.
        if any(platform.meta().name != "aiocqhttp" for platform in bot_platforms):
            logger.debug("[Giftia ExTool] Skipping non-OneBot/mixed bot: %s", bot_name)
            continue
        bot_names.append(bot_name)
        platforms.update(zip(adapter_ids, bot_platforms))
    if not bot_names:
        return stats

    today = now.astimezone(CHECK_TIMEZONE).date()
    start = datetime.combine(today - timedelta(days=1), time.min, CHECK_TIMEZONE)
    end = datetime.combine(today, time.min, CHECK_TIMEZONE)
    # Giftia persists naive server-local timestamps, with either a space or T.
    local_start = start.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    local_end = end.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    placeholders = ",".join("?" for _ in bot_names)
    async with giftia.db.conn.execute(
        f"""
        SELECT DISTINCT up.bot_name, up.user_id
        FROM chat_history ch
        JOIN user_profiles up
            ON up.bot_name = ch.bot_name AND up.user_id = ch.user_id
        WHERE ch.bot_name IN ({placeholders})
          AND COALESCE(NULLIF(ch.role, ''), 'message') = 'message'
          AND replace(ch.created_at, 'T', ' ') >= ?
          AND replace(ch.created_at, 'T', ' ') < ?
        """,
        (*bot_names, local_start, local_end),
    ) as cursor:
        rows = await cursor.fetchall()
    if not rows:
        return stats

    self_ids = set()
    try:
        for platform in platforms.values():
            login = await asyncio.wait_for(
                platform.get_client().call_action("get_login_info"), timeout=10
            )
            self_id = str(login.get("user_id") or "")
            if not self_id:
                return None
            self_ids.add(self_id)
    except Exception as exc:
        logger.debug("[Giftia ExTool] OneBot login information unavailable: %s", exc)
        return None

    targets = defaultdict(list)
    for row in rows:
        user_id = str(row["user_id"])
        if user_id.isascii() and user_id.isdecimal() and user_id not in self_ids:
            targets[user_id].append(row["bot_name"])

    for user_id, owning_bots in targets.items():
        async with fetcher.user_locks[user_id]:
            stats["checked"] += 1
            try:
                previous = await cache.get_avatar_record(user_id)
                image_bytes, avatar_url = await fetcher.download_avatar_bytes(user_id)
                if not image_bytes:
                    raise ValueError("Avatar download failed")
                avatar_hash = hashlib.sha256(image_bytes).hexdigest()
                if (
                    previous
                    and previous["avatar_hash"] == avatar_hash
                    and previous["avatar_description"].strip()
                ):
                    description = previous["avatar_description"]
                else:
                    description = await fetcher.caption_avatar_with_llm(
                        image_bytes, provider_ids
                    )
                    if not description or not description.strip():
                        raise ValueError("Avatar caption failed")
                    await cache.save_avatar_description(
                        user_id, description, avatar_url, avatar_hash
                    )
            except Exception as exc:
                stats["failed"] += len(owning_bots)
                logger.warning(
                    "[Giftia ExTool] Avatar check failed: user=%s: %s", user_id, exc
                )
                continue

            for bot_name in owning_bots:
                try:
                    async with giftia.db.conn.execute(
                        "SELECT avatar_description FROM user_profiles "
                        "WHERE bot_name = ? AND user_id = ?",
                        (bot_name, user_id),
                    ) as cursor:
                        profile = await cursor.fetchone()
                    if profile is None:
                        continue
                    old_description = str(profile["avatar_description"] or "").strip()
                    record = await cache.get_avatar_record(user_id, bot_name)
                    synced_hash = record["synced_hash"] if record else ""
                    if old_description and synced_hash == avatar_hash:
                        stats["unchanged"] += 1
                        continue

                    if old_description != description:
                        success = await sync_avatar_to_giftia(
                            user_id,
                            description,
                            bot_name=bot_name,
                            existing_only=True,
                            giftia=giftia,
                        )
                        if not success:
                            stats["failed"] += 1
                            continue
                    await cache.save_avatar_description(
                        user_id,
                        description,
                        avatar_url,
                        avatar_hash,
                        synced_bot_name=bot_name,
                    )
                    if not old_description:
                        stats["filled"] += 1
                    elif synced_hash and synced_hash != avatar_hash:
                        stats["changed"] += 1
                    elif previous and previous["avatar_hash"] not in ("", avatar_hash):
                        stats["changed"] += 1
                    else:
                        stats["baselined"] += 1
                except Exception as exc:
                    stats["failed"] += 1
                    logger.warning(
                        "[Giftia ExTool] Profile update failed: bot=%s user=%s: %s",
                        bot_name,
                        user_id,
                        exc,
                    )
    return stats

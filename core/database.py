from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

import aiosqlite

from astrbot.api import logger


class AvatarCacheDB:
    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        """Initialize the cache and migrate legacy rows without fingerprints."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = await aiosqlite.connect(str(self.db_path))
        self.conn.row_factory = aiosqlite.Row

        async with self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS avatar_cache (
                user_id TEXT PRIMARY KEY,
                avatar_url TEXT,
                avatar_description TEXT NOT NULL,
                avatar_hash TEXT NOT NULL DEFAULT '',
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            );
            """
        ):
            pass

        async with self.conn.execute("PRAGMA table_info(avatar_cache)") as cursor:
            columns = {row["name"] for row in await cursor.fetchall()}
        if "avatar_hash" not in columns:
            await self.conn.execute(
                "ALTER TABLE avatar_cache ADD COLUMN avatar_hash TEXT NOT NULL DEFAULT ''"
            )
        await self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS avatar_sync_state (
                bot_name TEXT NOT NULL,
                user_id TEXT NOT NULL,
                avatar_hash TEXT NOT NULL,
                synced_at TEXT NOT NULL,
                PRIMARY KEY (bot_name, user_id)
            )
            """
        )
        await self.conn.commit()
        logger.info("[Giftia ExTool] Avatar cache ready: %s", self.db_path)

    async def get_avatar_record(self, user_id: str, bot_name: str = "") -> dict | None:
        """Read a cached caption and the specified bot's last synced fingerprint.

        Args:
            user_id: Avatar cache key, using the QQ account ID for OneBot users.
            bot_name: Giftia bot whose synchronization state should be returned.

        Returns:
            Cached avatar data and synced_hash, or None when no cache exists.
        """
        if not self.conn or not str(user_id or "").strip():
            return None
        async with self._lock:
            async with self.conn.execute(
                """
                SELECT ac.*, COALESCE(s.avatar_hash, '') AS synced_hash
                FROM avatar_cache ac
                LEFT JOIN avatar_sync_state s
                    ON s.user_id = ac.user_id AND s.bot_name = ?
                WHERE ac.user_id = ?
                """,
                (bot_name, str(user_id).strip()),
            ) as cursor:
                row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_avatar_description(self, user_id: str) -> str | None:
        """Read a caption from the cache.

        Args:
            user_id: Avatar cache key.

        Returns:
            Cached caption, or None when it is missing.
        """
        row = await self.get_avatar_record(user_id)
        if not row:
            return None
        return str(row["avatar_description"]).strip() or None

    async def save_avatar_description(
        self,
        user_id: str,
        avatar_description: str,
        avatar_url: str = "",
        avatar_hash: str = "",
        synced_bot_name: str = "",
    ) -> None:
        """Save a caption and optionally record a confirmed Giftia write.

        Args:
            user_id: Avatar cache key.
            avatar_description: Caption describing this exact image.
            avatar_url: URL used to download the image.
            avatar_hash: SHA-256 of the complete downloaded image, if known.
            synced_bot_name: Bot that has successfully received this caption.
        """
        if not self.conn:
            return
        clean_user_id = str(user_id or "").strip()
        clean_desc = str(avatar_description or "").strip()
        if not clean_user_id or not clean_desc:
            return

        now_str = datetime.now().isoformat()
        async with self._lock:
            async with self.conn.execute(
                """
                INSERT INTO avatar_cache (user_id, avatar_url, avatar_description, avatar_hash, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    avatar_url = excluded.avatar_url,
                    avatar_description = excluded.avatar_description,
                    avatar_hash = excluded.avatar_hash,
                    updated_at = excluded.updated_at
                """,
                (
                    clean_user_id,
                    avatar_url or "",
                    clean_desc,
                    avatar_hash,
                    now_str,
                    now_str,
                ),
            ):
                pass
            if synced_bot_name and avatar_hash:
                await self.conn.execute(
                    """
                    INSERT INTO avatar_sync_state (bot_name, user_id, avatar_hash, synced_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(bot_name, user_id) DO UPDATE SET
                        avatar_hash = excluded.avatar_hash,
                        synced_at = excluded.synced_at
                    """,
                    (synced_bot_name, clean_user_id, avatar_hash, now_str),
                )
            await self.conn.commit()

    async def close(self) -> None:
        """Close the SQLite connection."""
        if self.conn:
            await self.conn.close()
            self.conn = None

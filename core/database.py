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
        """初始化 SQLite 数据库及缓存表结构"""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = await aiosqlite.connect(str(self.db_path))
        self.conn.row_factory = aiosqlite.Row

        async with self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS avatar_cache (
                user_id TEXT PRIMARY KEY,
                avatar_url TEXT,
                avatar_description TEXT NOT NULL,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            );
            """
        ):
            pass

        await self.conn.commit()
        logger.info(f"[Giftia ExTool] 头像缓存数据库已就绪: {self.db_path}")

    async def get_avatar_description(self, user_id: str) -> str | None:
        """从 SQLite 缓存表中获取头像描述"""
        if not self.conn:
            return None
        clean_user_id = str(user_id or "").strip()
        if not clean_user_id:
            return None

        async with self._lock:
            async with self.conn.execute(
                "SELECT avatar_description FROM avatar_cache WHERE user_id = ?",
                (clean_user_id,),
            ) as cursor:
                row = await cursor.fetchone()
                if row and row["avatar_description"]:
                    return str(row["avatar_description"]).strip()
        return None

    async def save_avatar_description(
        self,
        user_id: str,
        avatar_description: str,
        avatar_url: str = "",
    ) -> None:
        """将头像描述持久化保存/更新至 SQLite 缓存表"""
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
                INSERT INTO avatar_cache (user_id, avatar_url, avatar_description, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    avatar_url = excluded.avatar_url,
                    avatar_description = excluded.avatar_description,
                    updated_at = excluded.updated_at
                """,
                (clean_user_id, avatar_url or "", clean_desc, now_str, now_str),
            ):
                pass
            await self.conn.commit()

    async def close(self) -> None:
        """关闭数据库连接"""
        if self.conn:
            await self.conn.close()
            self.conn = None

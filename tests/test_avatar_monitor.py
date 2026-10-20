from __future__ import annotations

import asyncio
import hashlib
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import pytest
import pytest_asyncio
from aiosqlite.context import contextmanager
from PIL import Image

from core.avatar_fetcher import AvatarFetcher
from core.avatar_monitor import CHECK_TIMEZONE, check_active_avatars
from core.database import AvatarCacheDB
from core.giftia_sync import get_giftia_instance, sync_avatar_to_giftia
from main import GiftiaExTool


@pytest_asyncio.fixture
async def runtime(tmp_path, monkeypatch):
    cache = AvatarCacheDB(tmp_path / "cache.db")
    await cache.initialize()
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.executescript(
        """
        CREATE TABLE user_profiles (
            bot_name TEXT, user_id TEXT, avatar_description TEXT,
            personality TEXT DEFAULT 'keep this field',
            PRIMARY KEY (bot_name, user_id)
        );
        CREATE TABLE chat_history (
            bot_name TEXT, user_id TEXT, group_or_user_id TEXT,
            role TEXT, created_at TEXT
        );
        """
    )
    client = SimpleNamespace(call_action=AsyncMock(return_value={"user_id": "90000"}))
    qq_platform = SimpleNamespace(
        meta=lambda: SimpleNamespace(name="aiocqhttp"), get_client=lambda: client
    )
    other_platform = SimpleNamespace(meta=lambda: SimpleNamespace(name="telegram"))
    platforms = {"qq-a": qq_platform, "qq-b": qq_platform, "telegram": other_platform}
    context = MagicMock()
    context.get_platform_inst.side_effect = platforms.get
    fetcher = AvatarFetcher(context)
    fetcher.download_avatar_bytes = AsyncMock(return_value=(b"image-v1", "qq-avatar"))
    fetcher.caption_avatar_with_llm = AsyncMock(return_value="current avatar")

    async def write_profile(**kwargs):
        await conn.execute(
            "INSERT INTO user_profiles (bot_name, user_id, avatar_description) "
            "VALUES (?, ?, ?) ON CONFLICT(bot_name, user_id) DO UPDATE SET "
            "avatar_description=excluded.avatar_description",
            (
                kwargs["bot_name"],
                kwargs["user_id"],
                kwargs["profile_fields"]["avatar_description"],
            ),
        )
        await conn.commit()

    writer = AsyncMock(side_effect=write_profile)
    giftia = SimpleNamespace(
        db=SimpleNamespace(conn=conn, query=conn.execute),
        data_cache=SimpleNamespace(set_user_profile=writer),
        bot_map={
            "bot-a": {"adapter_ids": ["qq-a"]},
            "bot-b": {"adapter_ids": ["qq-b"]},
            "other": {"adapter_ids": ["telegram"]},
            "mixed": {"adapter_ids": ["qq-a", "telegram"]},
        },
        adapter_id_map={"qq-a": "bot-a", "qq-b": "bot-b"},
    )
    monkeypatch.setattr("core.avatar_monitor.get_giftia_instance", lambda: giftia)
    now = datetime(2026, 10, 20, 1, tzinfo=CHECK_TIMEZONE)

    async def seed(
        user_id,
        bot_name="bot-a",
        description="",
        when=None,
        group="group-1",
        role="message",
        with_profile=True,
        with_message=True,
        separator="T",
    ):
        if with_profile:
            await conn.execute(
                "INSERT OR IGNORE INTO user_profiles "
                "(bot_name, user_id, avatar_description) VALUES (?, ?, ?)",
                (bot_name, user_id, description),
            )
        if with_message:
            timestamp = (when or now - timedelta(hours=2)).astimezone()
            await conn.execute(
                "INSERT INTO chat_history VALUES (?, ?, ?, ?, ?)",
                (
                    bot_name,
                    user_id,
                    group,
                    role,
                    timestamp.replace(tzinfo=None).isoformat(sep=separator),
                ),
            )
        await conn.commit()

    async def run_check(at=None):
        return await check_active_avatars(
            context, cache, fetcher, ["vision"], at or now
        )

    try:
        yield SimpleNamespace(
            cache=cache,
            conn=conn,
            fetcher=fetcher,
            context=context,
            client=client,
            giftia=giftia,
            writer=writer,
            write_profile=write_profile,
            seed=seed,
            run_check=run_check,
            now=now,
            tmp_path=tmp_path,
        )
    finally:
        await cache.close()
        await conn.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reader_prefix",
    [
        "SELECT DISTINCT up.bot_name, up.user_id",
        "SELECT avatar_description FROM user_profiles",
        "SELECT 1 FROM user_profiles",
    ],
)
async def test_shared_writer_waits_until_avatar_reader_closes(
    runtime, monkeypatch, reader_prefix
):
    """Keep concurrent writes outside each avatar reader's cursor lifetime.

    Args:
        runtime: Avatar cache and Giftia database test runtime.
        monkeypatch: Fixture for installing the serialized query interface.
        reader_prefix: Avatar query whose open cursor should block the writer.
    """
    rt = runtime
    await rt.seed("10001")
    lock = asyncio.Lock()
    reading, release = asyncio.Event(), asyncio.Event()
    writer_started, writer_entered = asyncio.Event(), asyncio.Event()
    original_execute = rt.conn.execute

    @contextmanager
    async def paused_execute(sql, parameters=()):
        cursor = await original_execute(sql, parameters)
        if sql.lstrip().startswith(reader_prefix):
            reading.set()
            await release.wait()
        return cursor

    @asynccontextmanager
    async def query(sql, parameters=()):
        # Model Giftia's query ownership through cursor closure.
        async with lock:
            async with rt.conn.execute(sql, parameters) as cursor:
                yield cursor

    async def write_profile():
        writer_started.set()
        async with lock:
            writer_entered.set()
            await rt.write_profile(
                bot_name="bot-a",
                user_id="10002",
                profile_fields={"avatar_description": "concurrent writer"},
            )

    monkeypatch.setattr(rt.conn, "execute", paused_execute)
    monkeypatch.setattr(rt.giftia.db, "query", query)
    operation = (
        sync_avatar_to_giftia(
            "10001", "caption", "bot-a", existing_only=True, giftia=rt.giftia
        )
        if reader_prefix == "SELECT 1 FROM user_profiles"
        else rt.run_check()
    )
    reader = asyncio.create_task(operation)
    writer = None
    try:
        await asyncio.wait_for(reading.wait(), timeout=2)
        writer = asyncio.create_task(write_profile())
        await asyncio.wait_for(writer_started.wait(), timeout=2)
        assert not writer_entered.is_set()
    finally:
        release.set()
        results = await asyncio.wait_for(
            asyncio.gather(reader, *([writer] if writer else [])), timeout=2
        )

    assert writer_entered.is_set()
    if reader_prefix == "SELECT 1 FROM user_profiles":
        assert results[0] is True
    else:
        assert results[0]["filled"] == 1 and results[0]["failed"] == 0
    async with rt.conn.execute(
        "SELECT avatar_description FROM user_profiles WHERE user_id='10002'"
    ) as cursor:
        assert (await cursor.fetchone())[0] == "concurrent writer"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "server_timezone", ["UTC", "Asia/Shanghai", "America/New_York"]
)
@pytest.mark.skipif(not hasattr(time, "tzset"), reason="tzset is unavailable")
async def test_yesterday_window_handles_server_timezone_and_timestamp_formats(
    runtime, monkeypatch, server_timezone
):
    rt = runtime
    start = rt.now.replace(hour=0) - timedelta(days=1)
    end = rt.now.replace(hour=0)
    try:
        with monkeypatch.context() as local_patch:
            local_patch.setenv("TZ", server_timezone)
            time.tzset()
            await rt.seed("10001", when=start, separator=" ")
            await rt.seed("10001", when=start + timedelta(hours=1), group="group-2")
            await rt.seed("10002", when=start - timedelta(microseconds=1))
            await rt.seed("10003", when=end - timedelta(microseconds=1))
            await rt.seed("10004", when=end)
            result = await rt.run_check()
    finally:
        time.tzset()
    assert result["checked"] == 2
    assert result["filled"] == 2
    assert {
        call.args[0] for call in rt.fetcher.download_avatar_bytes.await_args_list
    } == {"10001", "10003"}


@pytest.mark.asyncio
async def test_only_existing_active_qq_profiles_are_selected(runtime):
    rt = runtime
    await rt.seed("10001")
    await rt.seed("10002", with_profile=False)
    await rt.seed("10003", with_message=False)
    await rt.seed("10004", role="system")
    await rt.seed("10005", role="operation_log")
    await rt.seed("90000")
    await rt.seed("open-id")
    await rt.seed("10006", bot_name="other")
    await rt.seed("10007", bot_name="mixed")
    result = await rt.run_check()
    assert result["checked"] == 1
    rt.fetcher.download_avatar_bytes.assert_awaited_once_with("10001")
    async with rt.conn.execute(
        "SELECT COUNT(*) FROM user_profiles WHERE user_id='10002'"
    ) as cursor:
        assert (await cursor.fetchone())[0] == 0


@pytest.mark.asyncio
async def test_shared_user_is_captioned_once_and_synced_once_per_bot(runtime):
    rt = runtime
    await rt.seed("10001")
    await rt.seed("10001", group="group-2")
    await rt.seed("10001", bot_name="bot-b")
    result = await rt.run_check()
    assert result["checked"] == 1 and result["filled"] == 2
    rt.fetcher.caption_avatar_with_llm.assert_awaited_once()
    assert rt.writer.await_count == 2
    assert {call.kwargs["bot_name"] for call in rt.writer.await_args_list} == {
        "bot-a",
        "bot-b",
    }
    assert all(
        call.kwargs["group_or_user_id"] == "" for call in rt.writer.await_args_list
    )
    async with rt.conn.execute("SELECT personality FROM user_profiles") as cursor:
        assert all(row[0] == "keep this field" for row in await cursor.fetchall())

    rt.fetcher.caption_avatar_with_llm.reset_mock()
    rt.writer.reset_mock()
    result = await rt.run_check()
    assert result["unchanged"] == 2
    rt.fetcher.caption_avatar_with_llm.assert_not_awaited()
    rt.writer.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_description_reuses_verified_image_cache(runtime):
    rt = runtime
    await rt.seed("10001")
    fingerprint = hashlib.sha256(b"image-v1").hexdigest()
    await rt.cache.save_avatar_description(
        "10001", "cached caption", "url", fingerprint
    )
    result = await rt.run_check()
    assert result["filled"] == 1
    rt.fetcher.caption_avatar_with_llm.assert_not_awaited()
    assert rt.writer.await_args.kwargs["profile_fields"] == {
        "avatar_description": "cached caption"
    }


@pytest.mark.asyncio
async def test_legacy_caption_without_fingerprint_is_rebaselined(runtime):
    rt = runtime
    await rt.seed("10001", description="legacy caption")
    await rt.cache.save_avatar_description("10001", "legacy caption")
    result = await rt.run_check()
    assert result["baselined"] == 1 and result["changed"] == 0
    rt.fetcher.caption_avatar_with_llm.assert_awaited_once()
    cached = await rt.cache.get_avatar_record("10001", "bot-a")
    assert cached["avatar_hash"] == cached["synced_hash"] != ""


@pytest.mark.asyncio
async def test_change_detection_hashes_bytes_beyond_the_first_512(runtime):
    rt = runtime
    before = b"x" * 512 + b"before"
    after = b"x" * 512 + b"after"
    await rt.seed("10001", description="old caption")
    await rt.cache.save_avatar_description(
        "10001", "old caption", "url", hashlib.sha256(before).hexdigest(), "bot-a"
    )
    rt.fetcher.download_avatar_bytes.return_value = (after, "url")
    result = await rt.run_check()
    assert result["changed"] == 1
    rt.fetcher.caption_avatar_with_llm.assert_awaited_once_with(after, ["vision"])
    assert (await rt.cache.get_avatar_record("10001", "bot-a"))["synced_hash"] == (
        hashlib.sha256(after).hexdigest()
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["download", "caption"])
async def test_failed_fetch_or_caption_preserves_old_data(runtime, failure):
    rt = runtime
    await rt.seed("10001", description="old caption")
    old_hash = hashlib.sha256(b"old-image").hexdigest()
    await rt.cache.save_avatar_description(
        "10001", "old caption", "url", old_hash, "bot-a"
    )
    if failure == "download":
        rt.fetcher.download_avatar_bytes.return_value = (None, "")
    else:
        rt.fetcher.caption_avatar_with_llm.return_value = None
    result = await rt.run_check()
    assert result["failed"] == 1
    cached = await rt.cache.get_avatar_record("10001", "bot-a")
    assert cached["avatar_description"] == "old caption"
    assert cached["avatar_hash"] == cached["synced_hash"] == old_hash
    rt.writer.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_sync_reuses_caption_on_retry(runtime):
    rt = runtime
    await rt.seed("10001", description="old caption")
    rt.writer.side_effect = RuntimeError("Giftia write unavailable")
    first = await rt.run_check()
    assert first["failed"] == 1
    cached = await rt.cache.get_avatar_record("10001", "bot-a")
    assert cached["avatar_hash"] and cached["synced_hash"] == ""
    rt.writer.side_effect = rt.write_profile
    second = await rt.run_check()
    assert second["failed"] == 0
    rt.fetcher.caption_avatar_with_llm.assert_awaited_once()
    cached = await rt.cache.get_avatar_record("10001", "bot-a")
    assert cached["avatar_hash"] == cached["synced_hash"]


@pytest.mark.asyncio
async def test_another_bot_gets_cached_update_when_it_becomes_active(runtime):
    rt = runtime
    await rt.seed("10001")
    await rt.seed(
        "10001", bot_name="bot-b", description="old caption", with_message=False
    )
    await rt.run_check()
    assert rt.writer.await_count == 1
    await rt.seed("10001", bot_name="bot-b", when=rt.now + timedelta(hours=5))
    await rt.run_check(rt.now + timedelta(days=1))
    rt.fetcher.caption_avatar_with_llm.assert_awaited_once()
    assert rt.writer.await_count == 2
    assert rt.writer.await_args.kwargs["bot_name"] == "bot-b"


@pytest.mark.asyncio
async def test_one_user_failure_does_not_block_other_users(runtime):
    rt = runtime
    await rt.seed("10001")
    await rt.seed("10002")
    rt.fetcher.caption_avatar_with_llm.side_effect = [None, "valid caption"]
    result = await rt.run_check()
    assert result["failed"] == 1 and result["filled"] == 1
    assert rt.writer.await_count == 1


@pytest.mark.asyncio
async def test_deleted_profile_is_not_recreated(runtime):
    rt = runtime
    await rt.seed("10001")

    async def download_after_deletion(user_id):
        await rt.conn.execute("DELETE FROM user_profiles WHERE user_id=?", (user_id,))
        await rt.conn.commit()
        return b"image-v1", "url"

    rt.fetcher.download_avatar_bytes.side_effect = download_after_deletion
    await rt.run_check()
    rt.writer.assert_not_awaited()
    assert not await sync_avatar_to_giftia(
        "10001", "caption", "bot-a", existing_only=True, giftia=rt.giftia
    )


@pytest.mark.asyncio
async def test_unready_giftia_or_onebot_does_not_finish_a_pass(runtime, monkeypatch):
    rt = runtime
    await rt.seed("10001")
    rt.client.call_action.side_effect = RuntimeError("NapCat offline")
    assert await rt.run_check() is None
    rt.fetcher.download_avatar_bytes.assert_not_awaited()
    monkeypatch.setattr("core.avatar_monitor.get_giftia_instance", lambda: None)
    assert await rt.run_check() is None


@pytest.mark.asyncio
async def test_legacy_database_migration_preserves_caption_and_sync_state(tmp_path):
    path = tmp_path / "legacy.db"
    async with aiosqlite.connect(path) as conn:
        await conn.execute(
            "CREATE TABLE avatar_cache (user_id TEXT PRIMARY KEY, avatar_url TEXT, "
            "avatar_description TEXT NOT NULL, created_at TEXT, updated_at TEXT)"
        )
        await conn.execute(
            "INSERT INTO avatar_cache VALUES ('10001', 'url', 'legacy', '2026-01-01', '2026-01-01')"
        )
        await conn.commit()
    cache = AvatarCacheDB(path)
    try:
        await cache.initialize()
        record = await cache.get_avatar_record("10001")
        assert record["avatar_description"] == "legacy" and record["avatar_hash"] == ""
        await cache.save_avatar_description("10001", "new", "url", "new-hash", "bot-a")
        await cache.close()
        await cache.initialize()
        assert (await cache.get_avatar_record("10001", "bot-a"))[
            "synced_hash"
        ] == "new-hash"
        assert (await cache.get_avatar_record("10001", "bot-b"))["synced_hash"] == ""
    finally:
        await cache.close()


@pytest.mark.parametrize("plugin_name", ["giftia", "astrbot_plugin_giftia"])
def test_giftia_lookup_unwraps_active_plugin_metadata(plugin_name):
    from astrbot.core.star.star import star_map

    instance = SimpleNamespace(data_cache=object())
    metadata = SimpleNamespace(name=plugin_name, activated=True, star_cls=instance)
    with patch.dict(
        star_map, {"data.plugins.astrbot_plugin_giftia.main": metadata}, clear=True
    ):
        assert get_giftia_instance() is instance
        metadata.activated = False
        assert get_giftia_instance() is None


@pytest.mark.asyncio
async def test_tool_uses_giftia_bot_mapping_and_records_sync_fingerprint(
    runtime, monkeypatch
):
    rt = runtime
    monkeypatch.setattr("main.get_giftia_instance", lambda: rt.giftia)
    monkeypatch.setattr(
        "astrbot.api.star.StarTools.get_data_dir", lambda *args: rt.tmp_path / "tool"
    )
    plugin = GiftiaExTool(
        rt.context, {"avatar_config": {"caption_provider_ids": ["vision"]}}
    )
    plugin.db = rt.cache
    plugin.fetcher = rt.fetcher
    event = SimpleNamespace(
        get_sender_id=lambda: "10001",
        get_group_id=lambda: "group-1",
        platform_meta=SimpleNamespace(id="qq-a", name="aiocqhttp"),
    )
    result = await plugin.inspect_user_avatar(event, "10001")
    assert "current avatar" in result
    assert rt.writer.await_args.kwargs["bot_name"] == "bot-a"
    record = await rt.cache.get_avatar_record("10001", "bot-a")
    assert (
        record["avatar_hash"]
        == record["synced_hash"]
        == hashlib.sha256(b"image-v1").hexdigest()
    )


@pytest.mark.asyncio
async def test_tool_waits_for_inflight_daily_check_and_reuses_its_caption(
    runtime, monkeypatch
):
    rt = runtime
    await rt.seed("10001")
    monkeypatch.setattr("main.get_giftia_instance", lambda: rt.giftia)
    monkeypatch.setattr(
        "astrbot.api.star.StarTools.get_data_dir", lambda *args: rt.tmp_path
    )
    plugin = GiftiaExTool(rt.context, {})
    plugin.db = rt.cache
    plugin.fetcher = rt.fetcher
    started = asyncio.Event()
    release = asyncio.Event()

    async def pending_download(user_id):
        started.set()
        await release.wait()
        return b"image-v1", "url"

    rt.fetcher.download_avatar_bytes.side_effect = pending_download
    event = SimpleNamespace(
        get_sender_id=lambda: "10001",
        get_group_id=lambda: "group-1",
        platform_meta=SimpleNamespace(id="qq-a", name="aiocqhttp"),
    )
    daily = asyncio.create_task(rt.run_check())
    await started.wait()
    tool = asyncio.create_task(plugin.inspect_user_avatar(event, "10001"))
    try:
        await asyncio.sleep(0)
        assert rt.fetcher.download_avatar_bytes.await_count == 1
    finally:
        release.set()
        results = await asyncio.gather(daily, tool)
    assert "本地缓存" in results[1]
    rt.fetcher.caption_avatar_with_llm.assert_awaited_once()
    rt.fetcher.download_avatar_bytes.assert_awaited_once()


@pytest.mark.asyncio
async def test_tool_force_refresh_updates_the_shared_image_fingerprint(
    runtime, monkeypatch
):
    rt = runtime
    await rt.cache.save_avatar_description(
        "10001", "old caption", "url", "old-hash", "bot-a"
    )
    monkeypatch.setattr("main.get_giftia_instance", lambda: rt.giftia)
    monkeypatch.setattr(
        "astrbot.api.star.StarTools.get_data_dir", lambda *args: rt.tmp_path
    )
    plugin = GiftiaExTool(rt.context, {})
    plugin.db = rt.cache
    plugin.fetcher = rt.fetcher
    event = SimpleNamespace(
        get_sender_id=lambda: "10001",
        get_group_id=lambda: "group-1",
        platform_meta=SimpleNamespace(id="qq-a", name="aiocqhttp"),
    )
    assert "current avatar" in await plugin.inspect_user_avatar(event, "10001", True)
    rt.fetcher.caption_avatar_with_llm.assert_awaited_once()
    record = await rt.cache.get_avatar_record("10001", "bot-a")
    assert record["avatar_hash"] == record["synced_hash"] != "old-hash"


@pytest.mark.asyncio
async def test_other_platform_numeric_id_does_not_reuse_qq_caption(
    runtime, monkeypatch
):
    rt = runtime
    await rt.cache.save_avatar_description("10001", "QQ caption", "url", "qq-hash")
    monkeypatch.setattr("main.get_giftia_instance", lambda: rt.giftia)
    monkeypatch.setattr(
        "astrbot.api.star.StarTools.get_data_dir", lambda *args: rt.tmp_path
    )
    plugin = GiftiaExTool(rt.context, {})
    plugin.db = rt.cache
    plugin.fetcher = rt.fetcher
    event = SimpleNamespace(
        get_sender_id=lambda: "10001",
        get_group_id=lambda: "",
        platform_meta=SimpleNamespace(id="telegram", name="telegram"),
    )
    assert "current avatar" in await plugin.inspect_user_avatar(event, "10001")
    assert await rt.cache.get_avatar_description("10001") == "QQ caption"
    assert (
        await rt.cache.get_avatar_description("telegram:telegram:10001")
        == "current avatar"
    )


@pytest.mark.asyncio
async def test_loop_catchup_gates_and_persisted_date(runtime, monkeypatch):
    rt = runtime
    monkeypatch.setattr(
        "astrbot.api.star.StarTools.get_data_dir", lambda *args: rt.tmp_path
    )
    config = {
        "avatar_config": {
            "daily_avatar_check_enabled": True,
            "inspect_user_avatar_enabled": False,
        }
    }
    plugin = GiftiaExTool(rt.context, config)
    plugin.db = rt.cache
    plugin.get_kv_data = AsyncMock(return_value="")
    plugin.put_kv_data = AsyncMock()
    stats = dict.fromkeys(
        ("checked", "filled", "changed", "baselined", "unchanged", "failed"), 0
    )
    check = AsyncMock(return_value=stats)
    monkeypatch.setattr("main.check_active_avatars", check)
    clock = MagicMock()
    clock.now.return_value = rt.now.replace(hour=8)
    monkeypatch.setattr("main.datetime", clock)
    with patch("main.asyncio.sleep", new=AsyncMock(side_effect=asyncio.CancelledError)):
        with pytest.raises(asyncio.CancelledError):
            await plugin._daily_avatar_loop()
    check.assert_awaited_once()
    plugin.put_kv_data.assert_awaited_once_with("daily_avatar_check_date", "2026-10-20")

    for last_date, at, overrides in (
        ("2026-10-20", rt.now.replace(hour=8), {}),
        ("", rt.now.replace(hour=0), {}),
        ("", rt.now, {"auto_sync_to_giftia": False}),
        ("", rt.now, {"daily_avatar_check_enabled": False}),
    ):
        check.reset_mock()
        plugin.get_kv_data.return_value = last_date
        clock.now.return_value = at
        original = dict(config["avatar_config"])
        config["avatar_config"].update(overrides)
        with patch(
            "main.asyncio.sleep", new=AsyncMock(side_effect=asyncio.CancelledError)
        ):
            with pytest.raises(asyncio.CancelledError):
                await plugin._daily_avatar_loop()
        config["avatar_config"] = original
        check.assert_not_awaited()


@pytest.mark.asyncio
async def test_loop_retries_unready_dependency_without_advancing_date(
    runtime, monkeypatch
):
    rt = runtime
    monkeypatch.setattr(
        "astrbot.api.star.StarTools.get_data_dir", lambda *args: rt.tmp_path
    )
    plugin = GiftiaExTool(
        rt.context, {"avatar_config": {"daily_avatar_check_enabled": True}}
    )
    plugin.db = rt.cache
    plugin.get_kv_data = AsyncMock(return_value="")
    plugin.put_kv_data = AsyncMock()
    clock = MagicMock()
    clock.now.return_value = rt.now
    monkeypatch.setattr("main.datetime", clock)
    check = AsyncMock(return_value=None)
    monkeypatch.setattr("main.check_active_avatars", check)
    with patch(
        "main.asyncio.sleep", new=AsyncMock(side_effect=[None, asyncio.CancelledError])
    ):
        with pytest.raises(asyncio.CancelledError):
            await plugin._daily_avatar_loop()
    assert check.await_count == 2
    plugin.put_kv_data.assert_not_awaited()


@pytest.mark.asyncio
async def test_enabled_task_is_cancelled_before_database_close(runtime, monkeypatch):
    rt = runtime
    monkeypatch.setattr(
        "astrbot.api.star.StarTools.get_data_dir", lambda *args: rt.tmp_path / "task"
    )
    plugin = GiftiaExTool(
        rt.context, {"avatar_config": {"daily_avatar_check_enabled": True}}
    )
    entered = asyncio.Event()

    async def blocked_loop():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            assert plugin.db.conn is not None

    monkeypatch.setattr(plugin, "_daily_avatar_loop", blocked_loop)
    await plugin.initialize()
    task = plugin._daily_task
    await entered.wait()
    await plugin.terminate()
    assert task.cancelled()
    assert plugin.db.conn is None


@pytest.mark.asyncio
async def test_default_provider_resolution_uses_provider_metadata():
    context = SimpleNamespace(
        get_using_provider_async=AsyncMock(
            return_value=SimpleNamespace(
                meta=lambda: SimpleNamespace(id="default-vision")
            )
        ),
        llm_generate=AsyncMock(
            return_value=SimpleNamespace(
                completion_text='{"avatar_description":"a cat"}'
            )
        ),
    )
    assert await AvatarFetcher(context).caption_avatar_with_llm(b"image") == "a cat"
    assert (
        context.llm_generate.await_args.kwargs["chat_provider_id"] == "default-vision"
    )


@pytest.mark.asyncio
async def test_empty_json_caption_falls_back_to_the_next_provider():
    context = SimpleNamespace(
        llm_generate=AsyncMock(
            side_effect=[
                SimpleNamespace(completion_text='{"avatar_description": ""}'),
                SimpleNamespace(completion_text='{"avatar_description": "a cat"}'),
            ]
        )
    )
    assert (
        await AvatarFetcher(context).caption_avatar_with_llm(
            b"image", ["first", "second"]
        )
        == "a cat"
    )
    assert context.llm_generate.await_args.kwargs["chat_provider_id"] == "second"


@pytest.mark.asyncio
@pytest.mark.parametrize("valid", [True, False])
async def test_downloader_validates_images_and_keeps_one_qq_size(valid):
    stream = BytesIO()
    Image.new("RGB", (8, 8), "black").save(stream, format="PNG")
    payload = stream.getvalue() if valid else b"<html>upstream error</html>" * 30
    assert len(stream.getvalue()) < 200
    response = SimpleNamespace(status=200, read=AsyncMock(return_value=payload))
    request = MagicMock()
    request.__aenter__ = AsyncMock(return_value=response)
    session = MagicMock()
    session.get.return_value = request
    session_context = MagicMock()
    session_context.__aenter__ = AsyncMock(return_value=session)
    with patch(
        "core.avatar_fetcher.aiohttp.ClientSession", return_value=session_context
    ):
        image, url = await AvatarFetcher(MagicMock()).download_avatar_bytes("10001")
    assert image == (payload if valid else None)
    if valid:
        assert url.endswith("s=640")
    session.get.assert_called_once()

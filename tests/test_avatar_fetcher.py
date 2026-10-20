from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from core.avatar_fetcher import AvatarFetcher
from PIL import Image


@pytest.fixture
def avatar_download(monkeypatch):
    stream = BytesIO()
    Image.new("RGB", (8, 8), "black").save(stream, format="PNG")
    payload = stream.getvalue()
    response = SimpleNamespace(status=200, read=AsyncMock(return_value=payload))
    request = MagicMock()
    request.__aenter__ = AsyncMock(return_value=response)
    session = MagicMock()
    session.get.return_value = request
    session_context = MagicMock()
    session_context.__aenter__ = AsyncMock(return_value=session)
    session_factory = MagicMock(return_value=session_context)
    monkeypatch.setattr("core.avatar_fetcher.aiohttp.ClientSession", session_factory)
    return SimpleNamespace(
        payload=payload,
        response=response,
        session=session,
        session_factory=session_factory,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("platform_name", ["qq_official", "qq_official_webhook"])
@pytest.mark.parametrize("user_id", ["member-openid", "10001"])
async def test_official_qq_uses_appid_and_target_user_id(
    avatar_download, platform_name, user_id
):
    event = SimpleNamespace(
        platform_meta=SimpleNamespace(name=platform_name),
        bot=SimpleNamespace(platform=SimpleNamespace(appid="123456")),
        message_obj=SimpleNamespace(
            sender=SimpleNamespace(user_id="another-user", avatar=None)
        ),
    )

    result = await AvatarFetcher(MagicMock()).download_avatar_bytes(
        f" {user_id} ", event=event
    )

    expected_url = f"https://q.qlogo.cn/qqapp/123456/{user_id}/0"
    assert result == (avatar_download.payload, expected_url)
    avatar_download.session.get.assert_called_once_with(expected_url)


@pytest.mark.asyncio
@pytest.mark.parametrize("platform_name", ["qq_official", "qq_official_webhook"])
@pytest.mark.parametrize(
    "bot",
    [None, SimpleNamespace(), SimpleNamespace(platform=SimpleNamespace(appid=""))],
)
async def test_official_qq_without_appid_does_not_request_an_invalid_url(
    avatar_download, platform_name, bot
):
    event = SimpleNamespace(platform_meta=SimpleNamespace(name=platform_name))
    if bot is not None:
        event.bot = bot

    assert await AvatarFetcher(MagicMock()).download_avatar_bytes(
        "member-openid", event=event
    ) == (None, "")
    avatar_download.session_factory.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("platform_name", ["qq_official", "qq_official_webhook"])
@pytest.mark.parametrize("user_id", ["", " "])
async def test_official_qq_without_user_id_skips_download(
    avatar_download, platform_name, user_id
):
    event = SimpleNamespace(
        platform_meta=SimpleNamespace(name=platform_name),
        bot=SimpleNamespace(platform=SimpleNamespace(appid="123456")),
    )

    assert await AvatarFetcher(MagicMock()).download_avatar_bytes(
        user_id, event=event
    ) == (None, "")
    avatar_download.session_factory.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("platform_name", ["qq_official", "qq_official_webhook"])
async def test_official_qq_rejects_non_image_responses(avatar_download, platform_name):
    avatar_download.response.read.return_value = b"<html>upstream error</html>"
    event = SimpleNamespace(
        platform_meta=SimpleNamespace(name=platform_name),
        bot=SimpleNamespace(platform=SimpleNamespace(appid="123456")),
    )

    assert await AvatarFetcher(MagicMock()).download_avatar_bytes(
        "member-openid", event=event
    ) == (None, "")


@pytest.mark.asyncio
@pytest.mark.parametrize("platform_name", ["qq_official", "telegram"])
async def test_sender_avatar_remains_available_without_appid(
    avatar_download, platform_name
):
    sender_avatar = "https://example.com/avatar.png"
    event = SimpleNamespace(
        platform_meta=SimpleNamespace(name=platform_name),
        message_obj=SimpleNamespace(
            sender=SimpleNamespace(user_id="member-openid", avatar=sender_avatar)
        ),
    )

    assert await AvatarFetcher(MagicMock()).download_avatar_bytes(
        "member-openid", event=event
    ) == (avatar_download.payload, sender_avatar)
    avatar_download.session.get.assert_called_once_with(sender_avatar)

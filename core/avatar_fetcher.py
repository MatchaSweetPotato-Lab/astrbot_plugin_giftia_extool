from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
from collections import defaultdict
from io import BytesIO

import aiohttp
from PIL import Image

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import Context

from .prompts import get_avatar_caption_prompt


class AvatarFetcher:
    def __init__(self, context: Context):
        self.context = context
        # Serialize tool calls and nightly checks for the same account.
        self.user_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def download_avatar_bytes(
        self,
        user_id: str,
        event: AstrMessageEvent | None = None,
    ) -> tuple[bytes | None, str]:
        """Download and validate an avatar, using one stable QQ image size.

        Args:
            user_id: Target account ID.
            event: Optional source event for platforms exposing an avatar URL.

        Returns:
            Image bytes and source URL, or (None, "") on failure.
        """
        clean_user_id = str(user_id or "").strip()
        avatar_urls = []

        platform_name = getattr(getattr(event, "platform_meta", None), "name", "")
        if clean_user_id.isdecimal() and (
            event is None or platform_name == "aiocqhttp"
        ):
            avatar_urls.append(f"https://q1.qlogo.cn/g?b=qq&nk={clean_user_id}&s=640")
        elif event and hasattr(event, "message_obj"):
            sender = getattr(event.message_obj, "sender", None)
            if sender and str(getattr(sender, "user_id", "")) == clean_user_id:
                sender_avatar = getattr(sender, "avatar", None)
                if sender_avatar:
                    avatar_urls.append(sender_avatar)

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
        }
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            for url in avatar_urls:
                try:
                    async with session.get(url) as resp:
                        if resp.status == 200:
                            data = await resp.read()
                            # Reject error pages without rejecting valid small images.
                            with Image.open(BytesIO(data)) as image:
                                image.verify()
                            return data, url
                except Exception as e:
                    logger.debug(
                        "[Giftia ExTool] Avatar download failed: %s: %s", url, e
                    )

        return None, ""

    async def caption_avatar_with_llm(
        self,
        image_bytes: bytes,
        provider_ids: list[str] | None = None,
    ) -> str | None:
        """Caption an image with the configured providers in priority order.

        Args:
            image_bytes: Complete avatar image.
            provider_ids: Ordered provider IDs, falling back to the default.

        Returns:
            Parsed caption, or None when every provider fails.
        """
        b64_str = base64.b64encode(image_bytes).decode("utf-8")
        image_url = f"base64://{b64_str}"
        img_hash = hashlib.sha256(image_bytes).hexdigest()
        prompt = get_avatar_caption_prompt(fingerprint=img_hash)

        providers = [p for p in (provider_ids or []) if p]
        if not providers:
            try:
                provider = await self.context.get_using_provider_async()
                if provider:
                    providers = [provider.meta().id]
            except Exception as e:
                logger.debug("[Giftia ExTool] Default provider lookup failed: %s", e)

        if not providers:
            logger.warning("[Giftia ExTool] No avatar caption provider available.")
            return None

        for pid in providers:
            try:
                logger.info("[Giftia ExTool] Captioning avatar with provider %s", pid)
                llm_resp = await self.context.llm_generate(
                    chat_provider_id=pid,
                    prompt=prompt,
                    image_urls=[image_url],
                )
                if not llm_resp or not getattr(llm_resp, "completion_text", None):
                    logger.warning(
                        "[Giftia ExTool] Empty caption from provider %s", pid
                    )
                    continue

                raw_text = str(llm_resp.completion_text).strip()
                description = self._parse_caption_text(raw_text)
                if description:
                    logger.info(
                        "[Giftia ExTool] Avatar caption ready: provider=%s", pid
                    )
                    return description
            except Exception as e:
                logger.error(
                    "[Giftia ExTool] Caption provider %s failed: %s",
                    pid,
                    e,
                    exc_info=True,
                )
                continue

        return None

    @staticmethod
    def _parse_caption_text(raw: str) -> str:
        """Parse a caption from JSON or plain text.

        Args:
            raw: Provider completion text.

        Returns:
            Cleaned caption, or an empty string when no content is available.
        """
        if not raw:
            return ""

        # Prefer the caption field when the model returns JSON.
        json_match = re.search(r"\{.*?\}", raw, re.DOTALL)
        if json_match:
            try:
                data = json.loads(json_match.group(0))
                for key in ("avatar_description", "caption", "description", "summary"):
                    caption = data.get(key)
                    if isinstance(caption, str) and caption.strip():
                        return caption.strip()
                return ""
            except Exception:
                pass

        # Strip Markdown fences from plain-text responses.
        cleaned = raw.strip("` \n\r\t")
        if cleaned.startswith("json"):
            cleaned = cleaned[4:].strip()
        return cleaned

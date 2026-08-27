from __future__ import annotations

import base64
import hashlib
import json
import re
from typing import Any

import aiohttp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import Context

from .prompts import get_avatar_caption_prompt


class AvatarFetcher:
    def __init__(self, context: Context):
        self.context = context

    async def download_avatar_bytes(
        self,
        user_id: str,
        event: AstrMessageEvent | None = None,
    ) -> tuple[bytes | None, str]:
        """
        下载用户头像的二进制数据。
        返回 (image_bytes, avatar_url)。
        """
        clean_user_id = str(user_id or "").strip()
        avatar_urls = []

        # 若传入的 event 中发件人与目标用户一致且有 avatar 属性
        if event and hasattr(event, "message_obj"):
            sender = getattr(event.message_obj, "sender", None)
            if sender and str(getattr(sender, "user_id", "")) == clean_user_id:
                sender_avatar = getattr(sender, "avatar", None)
                if sender_avatar:
                    avatar_urls.append(sender_avatar)

        # 默认 QQ 高清与标清头像地址
        avatar_urls.append(f"https://q1.qlogo.cn/g?b=qq&nk={clean_user_id}&s=640")
        avatar_urls.append(f"https://q1.qlogo.cn/g?b=qq&nk={clean_user_id}&s=100")

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
                            # 确保下载到真实图片，微型占位图通常小于 200 字节
                            if data and len(data) > 200:
                                return data, url
                except Exception as e:
                    logger.debug(f"[Giftia ExTool] 尝试从 {url} 下载头像失败: {e}")

        return None, ""

    async def caption_avatar_with_llm(
        self,
        image_bytes: bytes,
        provider_ids: list[str] | None = None,
    ) -> str | None:
        """
        调用视觉大模型对头像图片进行一句话特征转述。
        支持多模型提供商按优先级降级重试。
        """
        b64_str = base64.b64encode(image_bytes).decode("utf-8")
        image_url = f"base64://{b64_str}"
        img_hash = hashlib.md5(image_bytes[:512]).hexdigest()
        prompt = get_avatar_caption_prompt(fingerprint=img_hash)

        # 确定提供商列表
        providers = [p for p in (provider_ids or []) if p]
        if not providers:
            default_provider = None
            if hasattr(self.context, "get_using_provider"):
                try:
                    p = self.context.get_using_provider()
                    if p and hasattr(p, "id"):
                        default_provider = p.id
                except Exception:
                    pass
            if not default_provider and hasattr(self.context, "get_current_chat_provider_id"):
                try:
                    default_provider = self.context.get_current_chat_provider_id()
                except Exception:
                    pass
            if default_provider:
                providers = [default_provider]

        if not providers:
            logger.warning("[Giftia ExTool] 未配置视觉模型提供商，且无法获取默认提供商。")
            return None

        for pid in providers:
            try:
                logger.info(f"[Giftia ExTool] 尝试调用视觉大模型 ({pid}) 进行头像转述...")
                llm_resp = await self.context.llm_generate(
                    chat_provider_id=pid,
                    prompt=prompt,
                    image_urls=[image_url],
                )
                if not llm_resp or not getattr(llm_resp, "completion_text", None):
                    logger.warning(f"[Giftia ExTool] 提供商 {pid} 返回为空，尝试下一个提供商...")
                    continue

                raw_text = str(llm_resp.completion_text).strip()
                description = self._parse_caption_text(raw_text)
                if description:
                    logger.info(f"[Giftia ExTool] 头像转述成功 ({pid}): {description}")
                    return description
            except Exception as e:
                logger.error(f"[Giftia ExTool] 提供商 {pid} 转述失败: {e}，尝试降级...", exc_info=True)
                continue

        return None

    @staticmethod
    def _parse_caption_text(raw: str) -> str:
        """解析并清洗大模型返回的 JSON 或纯文本"""
        if not raw:
            return ""

        # 1. 尝试正则提取 JSON
        json_match = re.search(r"\{.*?\}", raw, re.DOTALL)
        if json_match:
            try:
                data = json.loads(json_match.group(0))
                for key in ("avatar_description", "caption", "description", "summary"):
                    if data.get(key):
                        return str(data[key]).strip()
            except Exception:
                pass

        # 2. 回退处理去除 markdown 代码块标记
        cleaned = raw.strip("` \n\r\t")
        if cleaned.startswith("json"):
            cleaned = cleaned[4:].strip()
        return cleaned

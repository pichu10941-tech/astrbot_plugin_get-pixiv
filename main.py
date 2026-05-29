"""
Pixiv 图片获取与发送修复插件

功能一：get_pixiv_image LLM 工具
  LLM 可通过 artwork ID 或 pixiv.net/artworks/xxx URL 获取图片直链。
  使用 Pixiv Ajax API（无需登录），返回 i.pximg.net 原图直链列表。

功能二：i.pximg.net 反代修复
  Monkey-patch aiocqhttp 平台的 send_by_session，
  将消息链中所有 i.pximg.net URL 替换为反代域名 i.pixiv.re，
  绕过 NapCat/LLOB 下载时因缺少 Referer 导致的 Forbidden 错误。
  覆盖所有发送路径（handler yield + LLM tool context.send_message）。
"""

import asyncio
import re
from typing import Optional

import aiohttp

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star

_PIXIV_IMG_HOST = "i.pximg.net"
_PIXIV_PROXY_HOST = "i.pixiv.re"

_PIXIV_AJAX_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.pixiv.net/",
}

_ARTWORK_ID_RE = re.compile(r"(?:artworks/|illust_id=)(\d+)|^(\d+)$")


def _extract_artwork_id(id_or_url: str) -> Optional[str]:
    m = _ARTWORK_ID_RE.search(id_or_url.strip())
    if m:
        return m.group(1) or m.group(2)
    return None


def _rewrite_pixiv_url(url: str) -> str:
    if _PIXIV_IMG_HOST in url:
        return url.replace(_PIXIV_IMG_HOST, _PIXIV_PROXY_HOST, 1)
    return url


def _patch_chain(chain: list) -> None:
    for i, comp in enumerate(chain):
        if isinstance(comp, Comp.Image):
            src = comp.file or ""
            rewritten = _rewrite_pixiv_url(src)
            if rewritten != src:
                chain[i] = Comp.Image.fromURL(rewritten)
                logger.debug(f"[pixiv] Image URL 已替换为反代: {rewritten[:80]}")
        elif isinstance(comp, Comp.File):
            src = comp.url or ""
            rewritten = _rewrite_pixiv_url(src)
            if rewritten != src:
                comp.url = rewritten
                logger.debug(f"[pixiv] File URL 已替换为反代: {rewritten[:80]}")


class PixivPlugin(Star):
    """
    Pixiv 图片获取工具 + NapCat/LLOB 发送修复。
    提供 get_pixiv_image LLM 工具，并自动修复 i.pximg.net 防盗链问题。
    """

    def __init__(self, context: Context):
        super().__init__(context)
        self._patched_platforms: list = []
        self._http_session: Optional[aiohttp.ClientSession] = None
        asyncio.get_event_loop().call_soon(
            lambda: asyncio.ensure_future(self._patch_platforms())
        )

    def _get_session(self) -> aiohttp.ClientSession:
        if self._http_session is None or self._http_session.closed:
            self._http_session = aiohttp.ClientSession()
        return self._http_session

    async def _fetch_pixiv_urls(self, artwork_id: str) -> list[str]:
        """
        通过 Pixiv Ajax API 获取作品所有分页的原图直链。
        单页作品返回 1 条，多页作品返回全部。
        无需登录，但需要 Referer: https://www.pixiv.net/
        """
        session = self._get_session()
        timeout = aiohttp.ClientTimeout(total=10)

        async with session.get(
            f"https://www.pixiv.net/ajax/illust/{artwork_id}/pages",
            headers=_PIXIV_AJAX_HEADERS,
            timeout=timeout,
        ) as resp:
            if resp.status != 200:
                raise ValueError(f"Pixiv API 返回 HTTP {resp.status}")
            data = await resp.json()

        if data.get("error"):
            raise ValueError(f"Pixiv API 错误: {data.get('message')}")

        return [page["urls"]["original"] for page in data["body"]]

    @filter.llm_tool(name="get_pixiv_image")
    async def get_pixiv_image(self, event: AstrMessageEvent, artwork_id_or_url: str):
        """获取 Pixiv 作品的图片直链，供后续发送给用户。支持作品 ID（数字）或 pixiv.net/artworks/xxx 格式的 URL。

        Args:
            artwork_id_or_url(string): Pixiv 作品 ID（如 127565524）或作品页 URL（如 https://www.pixiv.net/artworks/127565524）
        """
        artwork_id = _extract_artwork_id(artwork_id_or_url)
        if not artwork_id:
            yield event.plain_result(f"无法解析 artwork ID: {artwork_id_or_url}")
            return

        try:
            urls = await self._fetch_pixiv_urls(artwork_id)
        except Exception as e:
            logger.warning(f"[pixiv] 获取作品 {artwork_id} 失败: {e}")
            yield event.plain_result(f"获取 Pixiv 作品 {artwork_id} 失败: {e}")
            return

        logger.info(f"[pixiv] 作品 {artwork_id} 共 {len(urls)} 张图片")
        yield event.plain_result(
            "\n".join(f"p{i}: {url}" for i, url in enumerate(urls))
        )

    async def _patch_platforms(self) -> None:
        from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_platform_adapter import AiocqhttpAdapter

        platforms = self.context.platform_manager.get_insts()
        for platform in platforms:
            if not isinstance(platform, AiocqhttpAdapter):
                continue
            if getattr(platform, "_img_fix_patched", False):
                continue

            original_send = platform.send_by_session

            async def patched_send(session, message_chain, _orig=original_send):
                if message_chain and message_chain.chain:
                    _patch_chain(message_chain.chain)
                await _orig(session, message_chain)

            platform.send_by_session = patched_send
            platform._img_fix_patched = True
            self._patched_platforms.append((platform, original_send))
            logger.info(f"[pixiv] 已 patch 平台: {platform.meta().id}")

    @filter.on_decorating_result()
    async def _ensure_patched(self, event: AstrMessageEvent):
        if not self._patched_platforms:
            await self._patch_platforms()

    async def terminate(self):
        for platform, original_send in self._patched_platforms:
            platform.send_by_session = original_send
            platform._img_fix_patched = False
        self._patched_platforms.clear()
        if self._http_session and not self._http_session.closed:
            await self._http_session.close()
        logger.info("[pixiv] 已还原所有平台 patch")

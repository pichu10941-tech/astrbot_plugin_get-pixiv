"""
Pixiv / Danbooru 图片获取与发送修复插件

功能一：get_pixiv_image LLM 工具
  LLM 通过 artwork ID 或 pixiv.net/artworks/xxx URL 获取图片。
  使用 pixiv.re 反代直接构造图片 URL，无需调用 Pixiv API，无需代理。

功能二：get_booru_image LLM 工具
  LLM 通过 post ID 或页面 URL 从 danbooru 获取图片直链。

功能三：i.pximg.net 反代修复（兜底）
  Monkey-patch aiocqhttp 平台的 send_by_session，在消息发出前：
  - 将 pixiv.net/artworks/xxx 作品页 URL 替换为 pixiv.re 反代 URL
  - 将 i.pximg.net 直链替换为 i.pixiv.re 反代
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

_ARTWORK_ID_RE = re.compile(r"(?:artworks/|illust_id=)(\d+)|^(\d+)$")
_ARTWORK_URL_RE = re.compile(r"pixiv\.net/(?:\w+/)?artworks/(\d+)")
_DANBOORU_POST_RE = re.compile(r"danbooru\.donmai\.us/posts/(\d+)|^(\d+)$")


def _extract_artwork_id(s: str) -> Optional[str]:
    m = _ARTWORK_ID_RE.search(s.strip())
    return (m.group(1) or m.group(2)) if m else None


def _extract_danbooru_id(s: str) -> Optional[str]:
    m = _DANBOORU_POST_RE.search(s.strip())
    return (m.group(1) or m.group(2)) if m else None


def _pixiv_re_url(artwork_id: str, page: int = 1) -> str:
    """pixiv.re 反代格式：/{id}-{page}.png（page 从 1 开始）"""
    return f"https://pixiv.re/{artwork_id}-{page}.png"


def _rewrite_pixiv_url(url: str) -> str:
    if _PIXIV_IMG_HOST in url:
        return url.replace(_PIXIV_IMG_HOST, _PIXIV_PROXY_HOST, 1)
    return url


def _patch_chain(chain: list) -> None:
    for i, comp in enumerate(chain):
        if isinstance(comp, Comp.Image):
            src = comp.file or ""
            m = _ARTWORK_URL_RE.search(src)
            if m:
                chain[i] = Comp.Image.fromURL(_pixiv_re_url(m.group(1)))
            elif _PIXIV_IMG_HOST in src:
                chain[i] = Comp.Image.fromURL(_rewrite_pixiv_url(src))
        elif isinstance(comp, Comp.File):
            src = comp.url or ""
            m = _ARTWORK_URL_RE.search(src)
            if m:
                comp.url = _pixiv_re_url(m.group(1))
            elif _PIXIV_IMG_HOST in src:
                comp.url = _rewrite_pixiv_url(src)


class PixivPlugin(Star):
    """
    Pixiv / Danbooru 图片获取工具 + NapCat/LLOB 发送修复。
    提供 get_pixiv_image、get_booru_image LLM 工具，并自动修复 i.pximg.net 防盗链问题。
    """

    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self._config = config
        self._patched_platforms: list = []
        self._http_session: Optional[aiohttp.ClientSession] = None
        asyncio.get_event_loop().call_soon(
            lambda: asyncio.ensure_future(self._patch_platforms())
        )

    def _get_session(self) -> aiohttp.ClientSession:
        if self._http_session is None or self._http_session.closed:
            self._http_session = aiohttp.ClientSession()
        return self._http_session

    async def _fetch_danbooru_url(self, post_id: str) -> str:
        """
        通过 danbooru JSON API 获取 post 的图片直链。
        需要用户提供 login + api_key（danbooru 有 Cloudflare 保护）。
        """
        login = self._config.get("danbooru_login", "") if self._config else ""
        api_key = self._config.get("danbooru_api_key", "") if self._config else ""
        if not login or not api_key:
            raise ValueError("请在插件配置中填写 danbooru_login 和 danbooru_api_key")
        session = self._get_session()
        async with session.get(
            f"https://danbooru.donmai.us/posts/{post_id}.json",
            params={"login": login, "api_key": api_key},
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                raise ValueError(f"danbooru API 返回 HTTP {resp.status}")
            data = await resp.json()
        url = data.get("file_url") or data.get("large_file_url")
        if not url:
            raise ValueError(f"danbooru post {post_id} 无可用图片 URL")
        return url

    @filter.llm_tool(name="get_pixiv_image")
    async def get_pixiv_image(self, event: AstrMessageEvent, artwork_id_or_url: str, page_count: int = 1):
        """获取 Pixiv 作品的图片 URL，供后续发送给用户。使用 pixiv.re 反代，无需代理即可下载。

        Args:
            artwork_id_or_url(string): Pixiv 作品 ID（如 127565524）或作品页 URL（如 https://www.pixiv.net/artworks/127565524）
            page_count(number): 作品页数，默认为 1。多页作品请指定页数以获取所有页面。
        """
        artwork_id = _extract_artwork_id(artwork_id_or_url)
        if not artwork_id:
            yield event.plain_result(f"无法解析 artwork ID: {artwork_id_or_url}")
            return
        if page_count < 1:
            page_count = 1
        urls = [_pixiv_re_url(artwork_id, p) for p in range(1, page_count + 1)]
        logger.info(f"[pixiv] 作品 {artwork_id} 生成 {len(urls)} 个 pixiv.re URL")
        yield event.plain_result("\n".join(urls))

    @filter.llm_tool(name="get_booru_image")
    async def get_booru_image(self, event: AstrMessageEvent, post_id_or_url: str):
        """从 danbooru 获取图片直链，供后续发送给用户。

        Args:
            post_id_or_url(string): danbooru post ID（如 8988430）或页面 URL（如 https://danbooru.donmai.us/posts/8988430）
        """
        post_id = _extract_danbooru_id(post_id_or_url)
        if not post_id:
            yield event.plain_result(f"无法解析 danbooru post ID: {post_id_or_url}")
            return
        try:
            url = await self._fetch_danbooru_url(post_id)
        except Exception as e:
            logger.warning(f"[booru] danbooru {post_id} 失败: {e}")
            yield event.plain_result(f"获取 danbooru post {post_id} 失败: {e}")
            return
        logger.info(f"[booru] danbooru post {post_id}: {url}")
        yield event.plain_result(url)

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

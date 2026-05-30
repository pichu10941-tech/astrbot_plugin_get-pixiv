"""
Pixiv / Danbooru 图片获取与发送插件

核心方案：
  NapCat 无法下载 pixiv.re / i.pximg.net 等域名的图片（被墙或防盗链）。
  因此由 AstrBot 侧下载图片，转为 base64，再通过 NapCat 发送。
  NapCat 收到 base64 数据后直接发送，无需自行下载任何外部 URL。

功能一：get_pixiv_image LLM 工具
  LLM 通过 artwork ID 或 URL 获取 Pixiv 图片，插件从 pixiv.re 下载后
  直接以 base64 图片发送给用户，不返回 URL。

功能二：get_booru_image LLM 工具
  LLM 通过 post ID 从 danbooru 获取图片并直接发送。

功能三：兜底 patch
  拦截消息链中的 i.pximg.net / pixiv.net/artworks/ URL，
  由 AstrBot 下载转 base64 后替换，确保 NapCat 能发送。
"""

import asyncio
import base64
import re
from typing import Optional

import aiohttp

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star

_PIXIV_IMG_HOST = "i.pximg.net"
_PIXIV_PROXY_HOST = "i.pixiv.re"
_DOWNLOAD_TIMEOUT = 30

_DOWNLOAD_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
}

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
    return f"https://pixiv.re/{artwork_id}-{page}.png"


class PixivPlugin(Star):
    """
    Pixiv / Danbooru 图片获取与发送插件。
    由 AstrBot 侧下载图片转 base64，绕过 NapCat 无法访问外部图片域名的限制。
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

    async def _download_as_base64(self, url: str) -> Optional[str]:
        """
        下载图片并返回纯 base64 字符串（不含 base64:// 前缀）。
        失败返回 None。
        """
        session = self._get_session()
        try:
            async with session.get(
                url,
                headers=_DOWNLOAD_HEADERS,
                timeout=aiohttp.ClientTimeout(total=_DOWNLOAD_TIMEOUT),
                allow_redirects=True,
            ) as resp:
                if resp.status != 200:
                    logger.warning(f"[pixiv] 下载失败 HTTP {resp.status}: {url}")
                    return None
                data = await resp.read()
                return base64.b64encode(data).decode()
        except Exception as e:
            logger.warning(f"[pixiv] 下载异常: {url} — {e}")
            return None

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
    async def get_pixiv_image(self, event: AstrMessageEvent, artwork_id_or_url: str):
        """获取并发送 Pixiv 作品图片。插件会自动下载图片并发送，无需再调用 send_message_to_user。

        Args:
            artwork_id_or_url(string): Pixiv 作品 ID（如 127565524）或作品页 URL（如 https://www.pixiv.net/artworks/127565524）
        """
        artwork_id = _extract_artwork_id(artwork_id_or_url)
        if not artwork_id:
            yield event.plain_result(f"无法解析 artwork ID: {artwork_id_or_url}")
            return

        url = _pixiv_re_url(artwork_id, 1)
        logger.info(f"[pixiv] 开始下载作品 {artwork_id}: {url}")

        b64 = await self._download_as_base64(url)
        if not b64:
            yield event.plain_result(f"下载 Pixiv 作品 {artwork_id} 失败，请稍后重试")
            return

        yield event.image_result(f"base64://{b64}")

    @filter.llm_tool(name="get_booru_image")
    async def get_booru_image(self, event: AstrMessageEvent, post_id_or_url: str):
        """获取并发送 danbooru 图片。插件会自动下载图片并发送，无需再调用 send_message_to_user。

        Args:
            post_id_or_url(string): danbooru post ID（如 8988430）或页面 URL（如 https://danbooru.donmai.us/posts/8988430）
        """
        post_id = _extract_danbooru_id(post_id_or_url)
        if not post_id:
            yield event.plain_result(f"无法解析 danbooru post ID: {post_id_or_url}")
            return

        try:
            img_url = await self._fetch_danbooru_url(post_id)
        except Exception as e:
            logger.warning(f"[booru] danbooru {post_id} 失败: {e}")
            yield event.plain_result(f"获取 danbooru post {post_id} 失败: {e}")
            return

        logger.info(f"[booru] 开始下载 danbooru {post_id}: {img_url}")
        b64 = await self._download_as_base64(img_url)
        if not b64:
            yield event.plain_result(f"下载 danbooru post {post_id} 图片失败")
            return

        yield event.image_result(f"base64://{b64}")

    async def _resolve_and_download(self, url: str) -> Optional[str]:
        """
        将 pixiv 相关 URL 解析为可下载地址，下载后返回 base64。
        支持：pixiv.net/artworks/xxx、i.pximg.net 直链。
        对 i.pximg.net 直链：先尝试 i.pixiv.re 反代，失败则提取 artwork ID 用 pixiv.re/{id}-1.png 重试。
        """
        m = _ARTWORK_URL_RE.search(url)
        if m:
            download_url = _pixiv_re_url(m.group(1), 1)
            return await self._download_as_base64(download_url)

        if _PIXIV_IMG_HOST in url:
            proxy_url = url.replace(_PIXIV_IMG_HOST, _PIXIV_PROXY_HOST, 1)
            b64 = await self._download_as_base64(proxy_url)
            if b64:
                return b64
            # i.pixiv.re 404（日期路径错误），从 URL 提取 artwork ID 用 pixiv.re 重试
            id_match = re.search(r"/(\d+)_p\d+", url)
            if id_match:
                fallback_url = _pixiv_re_url(id_match.group(1), 1)
                logger.info(f"[pixiv] i.pixiv.re 失败，尝试 pixiv.re 回退: {fallback_url}")
                return await self._download_as_base64(fallback_url)
            return None

        return None

    async def _patch_chain_async(self, chain: list) -> None:
        pending: list[tuple[int, str]] = []
        for i, comp in enumerate(chain):
            if isinstance(comp, Comp.Image):
                src = comp.file or ""
                if _PIXIV_IMG_HOST in src or "pixiv.net/artworks/" in src:
                    pending.append((i, src))
            elif isinstance(comp, Comp.File):
                src = comp.url or ""
                if _PIXIV_IMG_HOST in src or "pixiv.net/artworks/" in src:
                    pending.append((i, src))

        if not pending:
            return

        results = await asyncio.gather(
            *[self._resolve_and_download(src) for _, src in pending]
        )
        for (i, original), b64 in zip(pending, results):
            if b64:
                chain[i] = Comp.Image.fromBase64(b64)
                logger.info(f"[pixiv] 已下载并转 base64: {original[:60]}...")
            else:
                logger.warning(f"[pixiv] 下载失败，保留原始 URL: {original}")

    async def _patch_platforms(self) -> None:
        from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_platform_adapter import AiocqhttpAdapter

        platforms = self.context.platform_manager.get_insts()
        for platform in platforms:
            if not isinstance(platform, AiocqhttpAdapter):
                continue
            if getattr(platform, "_img_fix_patched", False):
                continue

            original_send = platform.send_by_session

            async def patched_send(session, message_chain, _orig=original_send, _self=self):
                if message_chain and message_chain.chain:
                    await _self._patch_chain_async(message_chain.chain)
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

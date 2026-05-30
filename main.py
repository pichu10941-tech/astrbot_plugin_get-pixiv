"""
Pixiv / Danbooru 图片获取与发送插件

核心方案：
  NapCat 无法下载 pixiv.re / i.pximg.net 等域名的图片（被墙或防盗链）。
  因此由 AstrBot 侧下载图片保存为临时文件，再通过本地文件路径发送。
  NapCat 读取本地文件直接发送，无需自行访问任何外部 URL。

功能一：get_pixiv_image LLM 工具
  LLM 通过 artwork ID 或 URL 获取 Pixiv 图片，插件从 pixiv.re 下载后
  直接以本地图片发送给用户，不返回 URL。

功能二：get_booru_image LLM 工具
  LLM 通过 post ID 从 danbooru 获取图片并直接发送。

功能三：兜底 patch
  拦截消息链中的 i.pximg.net / pixiv.net/artworks/ URL，
  由 AstrBot 下载为临时文件后替换，确保 NapCat 能发送。
"""

import asyncio
import os
import re
import uuid
from typing import Optional

import aiohttp
from curl_cffi import requests as cffi_requests

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star

_PIXIV_IMG_HOST = "i.pximg.net"
_PIXIV_PROXY_HOST = "i.pixiv.re"
_DOWNLOAD_TIMEOUT = 30
_TEMP_DIR = os.path.join("data", "pixiv_cache")

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


def _pixiv_re_url(artwork_id: str) -> str:
    return f"https://pixiv.re/{artwork_id}.png"


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

    async def _download_to_file(self, url: str) -> Optional[str]:
        """
        下载图片并保存为临时文件，返回文件绝对路径。
        失败返回 None。
        """
        os.makedirs(_TEMP_DIR, exist_ok=True)
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
                ext = ".jpg"
                ct = resp.content_type or ""
                if "png" in ct:
                    ext = ".png"
                elif "gif" in ct:
                    ext = ".gif"
                elif "webp" in ct:
                    ext = ".webp"
                file_path = os.path.join(_TEMP_DIR, f"{uuid.uuid4().hex}{ext}")
                with open(file_path, "wb") as f:
                    f.write(data)
                return os.path.abspath(file_path)
        except Exception as e:
            logger.warning(f"[pixiv] 下载异常: {url} — {e}")
            return None

    async def _cffi_download_to_file(self, url: str) -> Optional[str]:
        """
        使用 curl_cffi 下载文件（绕过 Cloudflare），适用于 Danbooru CDN。
        同步调用通过 asyncio.to_thread 包装为异步。
        """
        os.makedirs(_TEMP_DIR, exist_ok=True)

        def _sync_download() -> Optional[str]:
            try:
                resp = cffi_requests.get(
                    url,
                    impersonate="chrome",
                    timeout=_DOWNLOAD_TIMEOUT,
                    allow_redirects=True,
                )
                if resp.status_code != 200:
                    logger.warning(f"[booru] cffi 下载失败 HTTP {resp.status_code}: {url}")
                    return None
                data = resp.content
                ct = resp.headers.get("content-type", "")
                ext = ".jpg"
                if "png" in ct:
                    ext = ".png"
                elif "gif" in ct:
                    ext = ".gif"
                elif "webp" in ct:
                    ext = ".webp"
                elif "mp4" in ct:
                    ext = ".mp4"
                file_path = os.path.join(_TEMP_DIR, f"{uuid.uuid4().hex}{ext}")
                with open(file_path, "wb") as f:
                    f.write(data)
                return os.path.abspath(file_path)
            except Exception as e:
                logger.warning(f"[booru] cffi 下载异常: {url} — {e}")
                return None

        return await asyncio.to_thread(_sync_download)

    async def _fetch_danbooru_url(self, post_id: str) -> str:
        """
        通过 danbooru JSON API 获取 post 的图片直链。
        使用 curl_cffi impersonate 绕过 Cloudflare，API key 可选。
        """
        login = self._config.get("danbooru_login", "") if self._config else ""
        api_key = self._config.get("danbooru_api_key", "") if self._config else ""

        def _sync_fetch() -> str:
            params: dict = {}
            auth = None
            if login and api_key:
                auth = (login, api_key)
            resp = cffi_requests.get(
                f"https://danbooru.donmai.us/posts/{post_id}.json",
                params=params,
                auth=auth,
                impersonate="chrome",
                headers={"Accept": "application/json"},
                timeout=15,
            )
            if resp.status_code != 200:
                raise ValueError(f"danbooru API 返回 HTTP {resp.status_code}")
            data = resp.json()
            url = data.get("file_url") or data.get("large_file_url")
            if not url:
                raise ValueError(f"danbooru post {post_id} 无可用图片 URL")
            return url

        return await asyncio.to_thread(_sync_fetch)

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

        url = _pixiv_re_url(artwork_id)
        logger.info(f"[pixiv] 开始下载作品 {artwork_id}: {url}")

        file_path = await self._download_to_file(url)
        if not file_path:
            yield event.plain_result(f"下载 Pixiv 作品 {artwork_id} 失败，请稍后重试")
            return

        yield event.image_result(file_path)

    @filter.llm_tool(name="get_booru_image")
    async def get_booru_image(self, event: AstrMessageEvent, post_id_or_url: str):
        """仅当用户提供了具体的 danbooru post ID 或 danbooru URL 时使用。插件会自动下载图片并发送，无需再调用 send_message_to_user。

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
        file_path = await self._cffi_download_to_file(img_url)
        if not file_path:
            yield event.plain_result(f"下载 danbooru post {post_id} 图片失败")
            return

        yield event.image_result(file_path)

    @filter.llm_tool(name="search_booru_image")
    async def search_booru_image(self, event: AstrMessageEvent, tags: str):
        """仅当用户明确提到 danbooru 或 booru 时使用。按标签从 danbooru.donmai.us 搜索图片并发送。不要在用户只说"找图"或"发涩图"时调用此工具，那些请求应使用其他搜索方式。插件会自动下载图片并发送，无需再调用 send_message_to_user。

        Args:
            tags(string): danbooru 搜索标签，空格分隔，如 "1girl blue_eyes" 或 "rating:sensitive 1girl"
        """
        import random

        def _sync_search() -> str:
            page = random.randint(1, 30)
            resp = cffi_requests.get(
                "https://danbooru.donmai.us/posts.json",
                params={"tags": tags, "limit": "1", "page": str(page)},
                impersonate="chrome",
                headers={"Accept": "application/json"},
                timeout=15,
            )
            if resp.status_code != 200:
                raise ValueError(f"danbooru 搜索返回 HTTP {resp.status_code}")
            posts = resp.json()
            if not posts:
                raise ValueError(f"danbooru 未找到匹配 '{tags}' 的图片")
            post = posts[0]
            url = post.get("file_url") or post.get("large_file_url")
            if not url:
                raise ValueError(f"danbooru post {post.get('id')} 无可用图片 URL")
            return url

        try:
            img_url = await asyncio.to_thread(_sync_search)
        except Exception as e:
            logger.warning(f"[booru] danbooru 搜索失败: {e}")
            yield event.plain_result(f"danbooru 搜索失败: {e}")
            return

        logger.info(f"[booru] 搜索 '{tags}' 命中，开始下载: {img_url}")
        file_path = await self._cffi_download_to_file(img_url)
        if not file_path:
            yield event.plain_result("下载 danbooru 图片失败")
            return

        yield event.image_result(file_path)

    async def _resolve_and_download(self, url: str) -> Optional[str]:
        """
        将 pixiv/danbooru 相关 URL 解析为可下载地址，下载后返回临时文件路径。
        Pixiv: 统一使用 pixiv.re/{id}.png 格式下载。
        Danbooru: 使用 curl_cffi 直接下载 CDN 链接。
        """
        if "cdn.donmai.us/" in url:
            return await self._cffi_download_to_file(url)

        if "danbooru.donmai.us/" in url:
            post_match = re.search(r"(?:posts/|post/show/)(\d+)", url)
            if post_match:
                try:
                    img_url = await self._fetch_danbooru_url(post_match.group(1))
                    return await self._cffi_download_to_file(img_url)
                except Exception as e:
                    logger.warning(f"[booru] 兜底下载 danbooru {post_match.group(1)} 失败: {e}")
                    return None
            # LLM 可能编造了假直链（如 /originals/xx/xx/id.jpg），尝试提取数字 ID
            id_match = re.search(r"/(\d{6,})", url)
            if id_match:
                try:
                    img_url = await self._fetch_danbooru_url(id_match.group(1))
                    return await self._cffi_download_to_file(img_url)
                except Exception as e:
                    logger.warning(f"[booru] 兜底下载 danbooru (猜测ID {id_match.group(1)}) 失败: {e}")
                    return None
            return await self._cffi_download_to_file(url)

        m = _ARTWORK_URL_RE.search(url)
        if m:
            return await self._download_to_file(_pixiv_re_url(m.group(1)))

        if _PIXIV_IMG_HOST in url:
            id_match = re.search(r"/(\d+)_p\d+", url)
            if id_match:
                return await self._download_to_file(_pixiv_re_url(id_match.group(1)))
            proxy_url = url.replace(_PIXIV_IMG_HOST, _PIXIV_PROXY_HOST, 1)
            return await self._download_to_file(proxy_url)

        return None

    def _needs_download(self, src: str) -> bool:
        """判断 URL 是否需要由插件下载后替换为本地文件。"""
        return bool(
            _PIXIV_IMG_HOST in src
            or "pixiv.net/artworks/" in src
            or "danbooru.donmai.us/" in src
            or "cdn.donmai.us/" in src
        )

    async def _patch_chain_async(self, chain: list) -> None:
        pending: list[tuple[int, str]] = []
        for i, comp in enumerate(chain):
            if isinstance(comp, Comp.Image):
                src = comp.file or ""
                if self._needs_download(src):
                    pending.append((i, src))
            elif isinstance(comp, Comp.File):
                src = comp.url or ""
                if self._needs_download(src):
                    pending.append((i, src))

        if not pending:
            return

        results = await asyncio.gather(
            *[self._resolve_and_download(src) for _, src in pending]
        )
        for (i, original), file_path in zip(pending, results):
            if file_path:
                chain[i] = Comp.Image.fromFileSystem(file_path)
                logger.info(f"[pixiv] 已下载为本地文件: {original[:60]}...")
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

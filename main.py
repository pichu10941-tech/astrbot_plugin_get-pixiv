"""
Pixiv / Booru 图片获取与发送修复插件

功能一：get_pixiv_image LLM 工具
  LLM 可通过 artwork ID 或 pixiv.net/artworks/xxx URL 获取图片直链。
  使用 Pixiv Ajax API（无需登录），返回 i.pximg.net 原图直链列表。

功能二：get_booru_image LLM 工具
  LLM 可通过 post ID 或页面 URL 从 danbooru/gelbooru 获取图片直链。
  需要在插件配置中填写对应站点的 API key。

功能三：i.pximg.net 反代修复（兜底）
  Monkey-patch aiocqhttp 平台的 send_by_session，在消息发出前：
  - 将 pixiv.net/artworks/xxx 作品页 URL 自动解析为 i.pximg.net 直链
  - 将 i.pximg.net 直链替换为反代域名 i.pixiv.re
  覆盖所有发送路径（handler yield + LLM tool context.send_message）。
  即使 LLM 没有调用 get_pixiv_image 工具，直接把作品页 URL 发出去也能正常显示。
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
_ARTWORK_URL_RE = re.compile(r"pixiv\.net/(?:\w+/)?artworks/(\d+)")
_DANBOORU_POST_RE = re.compile(r"danbooru\.donmai\.us/posts/(\d+)|^(\d+)$")
_GELBOORU_POST_RE = re.compile(r"gelbooru\.com/.*[?&]id=(\d+)|^(\d+)$")


def _extract_artwork_id(s: str) -> Optional[str]:
    m = _ARTWORK_ID_RE.search(s.strip())
    return (m.group(1) or m.group(2)) if m else None


def _extract_danbooru_id(s: str) -> Optional[str]:
    m = _DANBOORU_POST_RE.search(s.strip())
    return (m.group(1) or m.group(2)) if m else None


def _extract_gelbooru_id(s: str) -> Optional[str]:
    m = _GELBOORU_POST_RE.search(s.strip())
    return (m.group(1) or m.group(2)) if m else None


def _rewrite_pixiv_url(url: str) -> str:
    if _PIXIV_IMG_HOST in url:
        return url.replace(_PIXIV_IMG_HOST, _PIXIV_PROXY_HOST, 1)
    return url


class PixivPlugin(Star):
    """
    Pixiv / Booru 图片获取工具 + NapCat/LLOB 发送修复。
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

    async def _fetch_pixiv_urls(self, artwork_id: str) -> list[str]:
        """
        通过 Pixiv Ajax API 获取作品所有分页的原图直链。
        无需登录，但需要 Referer: https://www.pixiv.net/
        """
        session = self._get_session()
        async with session.get(
            f"https://www.pixiv.net/ajax/illust/{artwork_id}/pages",
            headers=_PIXIV_AJAX_HEADERS,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                raise ValueError(f"Pixiv API 返回 HTTP {resp.status}")
            data = await resp.json()
        if data.get("error"):
            raise ValueError(f"Pixiv API 错误: {data.get('message')}")
        return [page["urls"]["original"] for page in data["body"]]

    async def _fetch_danbooru_url(self, post_id: str) -> str:
        """
        通过 danbooru JSON API 获取 post 的图片直链。
        需要用户提供 login + api_key（danbooru 有 Cloudflare 保护）。
        """
        login = self._config.get("danbooru_login", "")
        api_key = self._config.get("danbooru_api_key", "")
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
            raise ValueError(f"danbooru post {post_id} 无可用图片 URL（可能需要登录或图片已删除）")
        return url

    async def _fetch_gelbooru_url(self, post_id: str) -> str:
        """
        通过 gelbooru JSON API 获取 post 的图片直链。
        需要用户提供 api_key 和 user_id（匿名访问返回 401）。
        """
        api_key = self._config.get("gelbooru_api_key", "")
        user_id = self._config.get("gelbooru_user_id", "")
        if not api_key or not user_id:
            raise ValueError("请在插件配置中填写 gelbooru_api_key 和 gelbooru_user_id")
        session = self._get_session()
        async with session.get(
            "https://gelbooru.com/index.php",
            params={
                "page": "dapi", "s": "post", "q": "index",
                "id": post_id, "json": "1",
                "api_key": api_key, "user_id": user_id,
            },
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                raise ValueError(f"gelbooru API 返回 HTTP {resp.status}")
            data = await resp.json()
        posts = data.get("post", [])
        if not posts:
            raise ValueError(f"gelbooru post {post_id} 不存在")
        url = posts[0].get("file_url")
        if not url:
            raise ValueError(f"gelbooru post {post_id} 无可用图片 URL")
        return url

    @filter.llm_tool(name="get_pixiv_image")
    async def get_pixiv_image(self, event: AstrMessageEvent, artwork_id_or_url: str):
        """获取 Pixiv 作品的图片直链，供后续发送给用户。支持作品 ID 或 pixiv.net/artworks/xxx 格式的 URL。

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

    @filter.llm_tool(name="get_booru_image")
    async def get_booru_image(self, event: AstrMessageEvent, site: str, post_id_or_url: str):
        """从 danbooru 或 gelbooru 获取图片直链，供后续发送给用户。

        Args:
            site(string): 图站名称，只能是 "danbooru" 或 "gelbooru"
            post_id_or_url(string): post ID（如 8988430）或页面 URL（如 https://danbooru.donmai.us/posts/8988430）
        """
        site = site.lower().strip()
        if site == "danbooru":
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
        elif site == "gelbooru":
            post_id = _extract_gelbooru_id(post_id_or_url)
            if not post_id:
                yield event.plain_result(f"无法解析 gelbooru post ID: {post_id_or_url}")
                return
            try:
                url = await self._fetch_gelbooru_url(post_id)
            except Exception as e:
                logger.warning(f"[booru] gelbooru {post_id} 失败: {e}")
                yield event.plain_result(f"获取 gelbooru post {post_id} 失败: {e}")
                return
        else:
            yield event.plain_result(f"不支持的站点: {site}，请使用 danbooru 或 gelbooru")
            return
        logger.info(f"[booru] {site} post {post_id}: {url}")
        yield event.plain_result(url)

    async def _resolve_artwork_url(self, url: str) -> Optional[str]:
        """
        检测到 pixiv.net/artworks/xxx 时，解析出第一张图的反代直链。
        作为 patch 的兜底，处理 LLM 直接发作品页 URL 的情况。
        """
        m = _ARTWORK_URL_RE.search(url)
        if not m:
            return None
        artwork_id = m.group(1)
        try:
            urls = await self._fetch_pixiv_urls(artwork_id)
            return _rewrite_pixiv_url(urls[0]) if urls else None
        except Exception as e:
            logger.warning(f"[pixiv] patch 兜底解析 {artwork_id} 失败: {e}")
            return None

    async def _patch_chain_async(self, chain: list) -> None:
        artwork_tasks = []
        for i, comp in enumerate(chain):
            if isinstance(comp, Comp.Image):
                src = comp.file or ""
                if "pixiv.net/artworks/" in src:
                    artwork_tasks.append((i, "image", src))
                elif _PIXIV_IMG_HOST in src:
                    chain[i] = Comp.Image.fromURL(_rewrite_pixiv_url(src))
            elif isinstance(comp, Comp.File):
                src = comp.url or ""
                if "pixiv.net/artworks/" in src:
                    artwork_tasks.append((i, "file", src))
                elif _PIXIV_IMG_HOST in src:
                    comp.url = _rewrite_pixiv_url(src)

        if not artwork_tasks:
            return

        resolved = await asyncio.gather(
            *[self._resolve_artwork_url(src) for _, _, src in artwork_tasks],
        )
        for (i, kind, original), direct_url in zip(artwork_tasks, resolved):
            if not direct_url:
                logger.warning(f"[pixiv] 作品页 URL 解析失败，保留原始: {original}")
                continue
            if kind == "image":
                chain[i] = Comp.Image.fromURL(direct_url)
            else:
                chain[i].url = direct_url
            logger.info(f"[pixiv] 作品页 URL 已解析为反代直链: {direct_url[:80]}")

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

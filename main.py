"""
图片发送修复插件

问题背景：
  NapCat/LLOB 在收到图片 URL 时，会由协议端自行下载图片再发送。
  Pixiv 图片（i.pximg.net）有防盗链限制，协议端下载时缺少正确 Referer，
  导致 retcode=1200 "rich media transfer failed" / "下载文件失败: Forbidden" 错误。

解决方案：
  Monkey-patch aiocqhttp 平台实例的 send_by_session 方法，
  在消息发出前将消息链中所有 i.pximg.net 图片/文件 URL 替换为反代域名 i.pixiv.re，
  NapCat 访问反代时无需 Referer 即可下载，绕过防盗链。

  相比 on_decorating_result 钩子，patch send_by_session 能同时覆盖：
  - handler yield 的消息
  - LLM tool（send_message_to_user）通过 context.send_message 发出的消息

延迟优化：
  仅替换 URL 域名，不在 Python 侧下载图片，零额外延迟。
  下载由 NapCat 完成，访问 i.pixiv.re 反代无需 Referer，不会被 403。
"""

import asyncio
from typing import Optional

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star

_PIXIV_IMG_HOST = "i.pximg.net"
_PIXIV_PROXY_HOST = "i.pixiv.re"


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
                logger.debug(f"[img-fix] Image URL 已替换为反代: {rewritten[:80]}")
        elif isinstance(comp, Comp.File):
            src = comp.url or ""
            rewritten = _rewrite_pixiv_url(src)
            if rewritten != src:
                comp.url = rewritten
                logger.debug(f"[img-fix] File URL 已替换为反代: {rewritten[:80]}")


class ImageSendFix(Star):
    """
    修复 NapCat/LLOB 发送 Pixiv 图片时因防盗链导致的下载失败错误。
    通过 patch aiocqhttp send_by_session，将 i.pximg.net URL 替换为 i.pixiv.re 反代。
    """

    def __init__(self, context: Context):
        super().__init__(context)
        self._patched_platforms: list = []

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
            logger.info(f"[img-fix] 已 patch 平台: {platform.meta().id}")

    @filter.on_decorating_result()
    async def _ensure_patched(self, event: AstrMessageEvent):
        """首次消息时确保 patch 已注入（平台实例在插件初始化后才完全就绪）"""
        if not self._patched_platforms:
            await self._patch_platforms()

    async def terminate(self):
        for platform, original_send in self._patched_platforms:
            platform.send_by_session = original_send
            platform._img_fix_patched = False
        self._patched_platforms.clear()
        logger.info("[img-fix] 已还原所有平台 patch")

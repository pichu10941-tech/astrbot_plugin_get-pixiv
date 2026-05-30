# Changelog

## 0.1.0

- 初始版本
- `get_pixiv_image` LLM 工具：通过 artwork ID 或 URL 获取 Pixiv 图片，AstrBot 侧下载转 base64 直接发送
- `get_booru_image` LLM 工具：从 danbooru 获取图片并直接发送（需配置 API key）
- 兜底 patch：拦截消息链中的 `i.pximg.net` / `pixiv.net/artworks/` URL，自动下载转 base64
- i.pixiv.re 404 回退：当 LLM 构造的日期路径错误时，提取 artwork ID 用 `pixiv.re/{id}-1.png` 重试
- 覆盖所有发送路径（handler yield + LLM tool `send_message_to_user`）

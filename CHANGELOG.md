# Changelog

## 0.1.0

- 初始版本
- `get_pixiv_image` LLM 工具：通过 artwork ID 或 URL 获取 Pixiv 图片，使用 pixiv.re 反代，无需代理
- `get_booru_image` LLM 工具：从 danbooru 获取图片直链（需配置 API key）
- i.pximg.net 反代修复：自动将 `i.pximg.net` 替换为 `i.pixiv.re`
- 作品页 URL 兜底：`pixiv.net/artworks/xxx` 自动替换为 `pixiv.re` 反代 URL
- 覆盖所有发送路径（handler yield + LLM tool `send_message_to_user`）

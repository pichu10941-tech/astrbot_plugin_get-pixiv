# astrbot_plugin_get_pixiv

为 AstrBot 提供 Pixiv / Danbooru 图片获取能力，并修复 NapCat/LLOB 发送 Pixiv 图片时的防盗链错误。

## 功能

### 1. `get_pixiv_image` LLM 工具

LLM 可通过作品 ID 或 `pixiv.net/artworks/xxx` URL 获取图片。直接使用 `pixiv.re` 反代构造 URL，**无需调用 Pixiv API，无需代理**。

多页作品通过 `page_count` 参数指定页数。

### 2. `get_booru_image` LLM 工具

LLM 可从 danbooru 获取图片直链。需要在插件配置中填写 `danbooru_login` 和 `danbooru_api_key`。

### 3. i.pximg.net 反代修复（兜底）

NapCat/LLOB 发送 Pixiv 图片时，协议端自行下载 `i.pximg.net` 的图片，因缺少 `Referer` 头被 403。

插件在底层 patch `send_by_session`，在消息发出前自动处理：

- `pixiv.net/artworks/xxx` 作品页 URL → 替换为 `pixiv.re` 反代 URL
- `i.pximg.net` 直链 → 替换为 `i.pixiv.re` 反代

覆盖所有发送路径（handler yield + LLM tool），**即使 LLM 没有调用 `get_pixiv_image` 工具，直接把作品页 URL 发出去也能正常显示**。

## 安装

在 AstrBot 插件市场搜索 `astrbot_plugin_get_pixiv`，或手动克隆到插件目录：

```bash
git clone https://github.com/pichu10941-tech/astrbot_plugin_get-pixiv
```

## 配置

在 AstrBot WebUI 的插件配置页面填写（均为可选，不填则 danbooru 功能不可用）：

| 配置项 | 说明 |
|---|---|
| `danbooru_login` | danbooru.donmai.us 用户名 |
| `danbooru_api_key` | danbooru API Key（个人设置页面生成） |

Pixiv 功能无需任何配置，开箱即用。

## 使用示例

> 帮我发一张 pixiv.net/artworks/127565524 的图

> 把 pixiv 作品 128290291 发给我

> 从 danbooru post 8988430 发一张图

## 适用平台

- OneBot v11（NapCat、LLOB、Lagrange 等）

其他平台不受反代修复影响，LLM 工具在所有平台可用。

## 依赖

`aiohttp`（AstrBot 已内置，无需额外安装）

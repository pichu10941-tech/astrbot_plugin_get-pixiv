# astrbot_plugin_get_pixiv

为 AstrBot 提供 Pixiv / Danbooru 图片获取能力，并修复 NapCat/LLOB 发送 Pixiv 图片时的防盗链错误。

## 功能

### 1. `get_pixiv_image` LLM 工具

LLM 可通过作品 ID 或 `pixiv.net/artworks/xxx` URL 直接获取图片直链，无需用户手动找图。使用 Pixiv Ajax API，**无需登录**，支持多页作品（返回全部分页直链）。

R-18 作品的 `/pages` 端点在未登录状态下返回 404，插件会自动从 `/illust` 的缩略图 URL 推导原图路径作为兜底。

### 2. `get_booru_image` LLM 工具

LLM 可从 danbooru 获取图片直链。需要在插件配置中填写 `danbooru_login` 和 `danbooru_api_key`。

### 3. i.pximg.net 反代修复（兜底）

NapCat/LLOB 发送 Pixiv 图片时，协议端自行下载 `i.pximg.net` 的图片，因缺少 `Referer` 头被 403，导致：

```
retcode=1200, message='rich media transfer failed'
retcode=1200, message='下载文件失败: Forbidden'
```

插件在底层 patch `send_by_session`，在消息发出前自动处理：

- `pixiv.net/artworks/xxx` 作品页 URL → 自动调 Pixiv API 解析为图片直链
- `i.pximg.net` 直链 → 替换为反代域名 `i.pixiv.re`

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

## 使用示例

> 帮我发一张 pixiv.net/artworks/127565524 的图

> 把 pixiv 作品 128290291 发给我（R-18 作品也支持）

> 从 danbooru post 8988430 发一张图

## 适用平台

- OneBot v11（NapCat、LLOB、Lagrange 等）

其他平台不受反代修复影响，LLM 工具在所有平台可用。

## 依赖

`aiohttp`（AstrBot 已内置，无需额外安装）

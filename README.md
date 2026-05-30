# astrbot_plugin_get_pixiv

为 AstrBot 提供 Pixiv / Danbooru 图片获取能力，解决 NapCat/LLOB 无法发送外部图片的问题。

## 原理

NapCat 无法下载 `pixiv.re`、`i.pximg.net`、`danbooru.donmai.us` 等域名的图片（被墙或防盗链），导致 `rich media transfer failed`。

本插件由 **AstrBot 侧下载图片并转为 base64**，NapCat 收到 base64 数据后直接发送，无需自行访问任何外部 URL。

## 功能

### 1. `get_pixiv_image` LLM 工具

LLM 调用此工具后，插件自动从 `pixiv.re` 下载图片并直接发送给用户，**无需 LLM 再调用 `send_message_to_user`**。

### 2. `get_booru_image` LLM 工具

从 danbooru 获取并直接发送图片。需要在插件配置中填写 `danbooru_login` 和 `danbooru_api_key`。

### 3. 兜底 patch

拦截消息链中的 Pixiv 相关 URL，由 AstrBot 下载转 base64 后替换：

- `pixiv.net/artworks/xxx` → 用 `pixiv.re/{id}-1.png` 下载
- `i.pximg.net/...` → 先尝试 `i.pixiv.re` 反代，404 时提取 artwork ID 用 `pixiv.re/{id}-1.png` 回退

覆盖所有发送路径（handler yield + LLM tool），即使 LLM 不调工具直接发 URL 也能兜住。

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

## 前置条件

AstrBot 容器需要能访问 `pixiv.re`（通过代理或直连）。NapCat 侧无需任何网络配置。

## 适用平台

- OneBot v11（NapCat、LLOB、Lagrange 等）

其他平台不受 patch 影响，LLM 工具在所有平台可用。

## 依赖

`aiohttp`（AstrBot 已内置，无需额外安装）

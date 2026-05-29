# astrbot_plugin_get_pixiv

为 AstrBot 提供 Pixiv 图片获取能力，并修复 NapCat/LLOB 发送 Pixiv 图片时的防盗链错误。

## 功能

### 1. `get_pixiv_image` LLM 工具

LLM 可通过作品 ID 或 `pixiv.net/artworks/xxx` URL 直接获取图片直链，无需用户手动找图。

使用 Pixiv Ajax API，无需登录，支持多页作品（返回全部分页直链）。

### 2. i.pximg.net 反代修复

NapCat/LLOB 发送 Pixiv 图片时，协议端自行下载 `i.pximg.net` 的图片，因缺少 `Referer` 头被 403，导致：

```
retcode=1200, message='rich media transfer failed'
retcode=1200, message='下载文件失败: Forbidden'
```

插件在底层将所有 `i.pximg.net` URL 替换为反代域名 `i.pixiv.re`，NapCat 访问反代无需 Referer，绕过防盗链。覆盖所有发送路径（handler yield + LLM tool）。

## 安装

在 AstrBot 插件市场搜索 `astrbot_plugin_get_pixiv`，或手动克隆到插件目录：

```bash
git clone https://github.com/pichu10941-tech/astrbot_plugin_get-pixiv
```

## 使用

安装后无需配置。LLM 会自动获得 `get_pixiv_image` 工具，可以这样触发：

> 帮我发一张 pixiv.net/artworks/127565524 的图

> 把 pixiv 作品 119939498 发给我

## 适用平台

- OneBot v11（NapCat、LLOB、Lagrange 等）

其他平台不受反代修复影响，`get_pixiv_image` 工具在所有平台可用。

## 依赖

`aiohttp`（AstrBot 已内置，无需额外安装）

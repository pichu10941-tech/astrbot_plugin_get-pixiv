# astrbot_plugin_get_pixiv

修复 NapCat/LLOB 在发送 Pixiv 图片时因防盗链导致的发送失败问题。

## 问题

AstrBot 通过 NapCat/LLOB（OneBot v11）发送 Pixiv 图片时，协议端会自行下载 `i.pximg.net` 的图片再转发。由于 Pixiv CDN 有防盗链限制，协议端下载时缺少正确的 `Referer` 头，导致以下错误：

```
retcode=1200, message='rich media transfer failed'
retcode=1200, message='下载文件失败: Forbidden'
```

## 解决方案

将消息链中所有 `i.pximg.net` 的图片/文件 URL 替换为反代域名 `i.pixiv.re`。NapCat 访问反代时无需 `Referer` 即可下载，绕过防盗链限制。

插件通过 patch `aiocqhttp` 平台的 `send_by_session` 方法实现拦截，覆盖所有发送路径：

- handler `yield` 的消息
- LLM tool（`send_message_to_user`）通过 `context.send_message` 发出的消息

仅做 URL 域名替换，不在 Python 侧下载图片，**零额外延迟**。

## 安装

在 AstrBot 插件市场搜索 `astrbot_plugin_get_pixiv`，或手动克隆到插件目录：

```bash
git clone https://github.com/pichu10941-tech/astrbot_plugin_get-pixiv
```

## 适用平台

- OneBot v11（NapCat、LLOB、Lagrange 等）

其他平台不受影响，插件会自动跳过非 `aiocqhttp` 平台。

## 依赖

无额外依赖，`aiohttp` 已由 AstrBot 提供。

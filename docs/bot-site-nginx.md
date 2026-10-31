# bot.oneweblog.cn 公开站点 — nginx 部署示例

面向普通用户的只读静态站（使用说明 + 演示 + 自动同步的更新记录），站点源文件在
仓库 [`public_site/`](../public_site/)（含本地预览与素材补充说明）。

> nginx/反代由**服务器侧**管理，仓库内不放任何 nginx 配置；本文仅作示例参考。
> 面板（admin WebUI，`webui.oneweblog.cn`）与本公开站**相互独立**：本站无登录、
> 无后端、纯静态只读，不暴露任何管理端点。

## 数据流

```
WebUI 面板保存更新日志
  → webui/data_reader.py::_export_public_changelog()   （脱敏：仅 version/date/changes）
  → 原子写 <PUBLIC_SITE_DIR>/changelog.json
  → nginx 静态服务该目录
  → 浏览器 fetch ./changelog.json 渲染「更新记录」区
```

`PUBLIC_SITE_DIR` 经 `webui/config.py::bot_env()` 读取，默认 `<repo>/public_site`；
生产部署把 `WEBUI_PUBLIC_SITE_DIR=/var/www/bot-oneweblog` 写进 **gitignore 的
`QQBot/.env`**——部署专属路径既不硬编码进任何启动脚本、也不进版本库。**面板进程需
对该目录有写权限**（导出用）。

## 部署二选一

1. **nginx root 直接指向仓库目录**（最简单，仅适合开发/单机）：
   `root` 设为 `<repo>/public_site`。面板保存即写到这里，刷新即见。
   ⚠️ nginx worker（`www-data`）需能读穿仓库路径——若仓库在 `/home/<user>`（常为
   `750`）下则读不到（403），生产不适用。
2. **同步到独立 nginx root（生产采用）**：静态文件同步到 `/var/www/bot-oneweblog`，
   并在 `QQBot/.env` 设 `WEBUI_PUBLIC_SITE_DIR=/var/www/bot-oneweblog`，面板保存时
   changelog.json 即导出到该目录。静态文件用仓库根的 **`deploy_public_site.sh`**
   一键同步（见下节）——它与仪表盘 `start_webui.sh` **完全独立**，两个站点各管各的。

## 展示站部署脚本 deploy_public_site.sh

展示站是纯静态站、无守护进程，故没有"启动/停止"，只有"同步部署"：

```bash
bash deploy_public_site.sh              # 同步 public_site → nginx root + 修权限 + reload nginx
bash deploy_public_site.sh --no-reload  # 只同步，不动 nginx
```

- 目标目录从 `QQBot/.env` 的 `WEBUI_PUBLIC_SITE_DIR` 读取（与面板导出目录**同源**，
  避免两处配置漂移），缺省 `/var/www/bot-oneweblog`。
- 只同步 `index.html / style.css / app.js / assets`，**保留** `changelog.json`
  （那是面板的产物，脚本绝不覆盖或删除）。
- 权限修正为 `ubuntu:www-data` + 目录 755 / 文件 644：面板（ubuntu）可写、
  nginx（www-data）可读。

## nginx 示例（生产实况）

生产用 `certbot --nginx -d bot.oneweblog.cn --redirect` 签发了 **bot 子域的独立
证书**（不复用 `oneweblog.cn` apex 证书），并由 certbot 自动托管 443 的 SSL 行与
80→443 跳转。手写部分只有 `root` 与三个 `location` 缓存策略：

```nginx
# 80：certbot --redirect 自动生成，仅做跳转
server {
    listen 80;
    listen [::]:80;
    server_name bot.oneweblog.cn;
    return 301 https://$host$request_uri;   # managed by Certbot
}

server {
    server_name bot.oneweblog.cn;

    # 生产采用「方式 2」：独立 nginx root（= QQBot/.env 的 WEBUI_PUBLIC_SITE_DIR）
    root /var/www/bot-oneweblog;
    index index.html;

    # changelog.json 由面板保存时重写 → 禁止缓存，保证保存后刷新即见
    location = /changelog.json {
        add_header Cache-Control "no-cache, no-store, must-revalidate";
        add_header Pragma "no-cache";
        try_files $uri =404;
    }

    # 演示素材长缓存
    location /assets/ {
        add_header Cache-Control "public, max-age=86400";
        try_files $uri =404;
    }

    # 单页站点：其余请求回落 index.html
    location / {
        add_header Cache-Control "no-cache";
        try_files $uri $uri/ /index.html;
    }

    # ↓↓↓ 以下 SSL 行由 certbot 自动注入/续期，勿手改 ↓↓↓
    listen [::]:443 ssl ipv6only=on; # managed by Certbot
    listen 443 ssl; # managed by Certbot
    ssl_certificate     /etc/letsencrypt/live/bot.oneweblog.cn/fullchain.pem; # managed by Certbot
    ssl_certificate_key /etc/letsencrypt/live/bot.oneweblog.cn/privkey.pem; # managed by Certbot
    include /etc/letsencrypt/options-ssl-nginx.conf; # managed by Certbot
    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem; # managed by Certbot
}
```

> 证书路径为 `live/bot.oneweblog.cn/`（独立证书），非 apex 的 `live/oneweblog.cn/`。
> 续期由 certbot 的 systemd timer 自动完成，`nginx -t && systemctl reload nginx` 即可验证。

## 校验清单

- `curl -I https://bot.oneweblog.cn/` → 200，`Content-Type: text/html`
- `curl -I https://bot.oneweblog.cn/changelog.json` → 200（首次 WebUI 保存后生成；
  保存前为 404，页面优雅降级显示「暂无更新记录」）
- `curl -I https://bot.oneweblog.cn/changelog.json` 响应头含 `Cache-Control: no-cache`
- 在 WebUI 改一条更新日志并保存 → 刷新站点「更新记录」区立即看到新内容
- 确认公开 `changelog.json` **不含** `created_at`/`broadcast_at`/群列表/用户等内部字段

## 安全红线

- 公开 `changelog.json` **只含** version/date/changes；绝不导出
  `known_groups.json`/`pending_broadcast.json`/`broadcast_status.json`/任何用户或群数据。
- 本 server 块**不要**反代到面板端口（8090）或 bot 端口（8081）；纯静态即可。
- 指路短链经 `.env` 可覆盖：bot 侧 `BOT_PUBLIC_SITE_URL`、面板侧
  `WEBUI_PUBLIC_SITE_URL`，默认均为 `https://bot.oneweblog.cn`。

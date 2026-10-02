# public_site — Roxy 公开使用站点

面向**普通用户**的只读静态站（`https://bot.oneweblog.cn`）：使用说明 + 演示 +
自动同步的更新记录。**独立于需登录的 admin WebUI**——无后端、无登录、纯静态。

更新记录数据源 `changelog.json` **不由人工维护**：WebUI 面板每次保存更新日志时，
`webui/data_reader.py::_export_public_changelog()` 会自动把**脱敏后**的
（仅 version/date/changes）快照原子写入本目录。首次保存即生成。

## 目录

```
public_site/
├── index.html      单页（hero / 场景 / 怎么用 / 演示 / 更新记录 / 反馈）
├── style.css       暗色主题（调色板与 QQ 卡片一致）
├── app.js          chip 复制 + 演示降级 + 拉取渲染 changelog.json
├── changelog.json  ← 自动生成（WebUI 保存时导出），勿手改
└── assets/         演示截图/GIF（见下）
```

## 本地预览

```bash
cd public_site
python -m http.server 8000
# 浏览器打开 http://127.0.0.1:8000
```

删除 `changelog.json` 再刷新，页面应显示「暂无更新记录」而非报错（优雅降级）。

## 部署（nginx）

nginx/反代由服务器侧管理，仓库不放 nginx 配置；示例见
[`docs/bot-site-nginx.md`](../docs/bot-site-nginx.md)。两种任选：

1. **nginx root 直接指向本目录**（仓库内）：
   `root /path/to/repo/public_site;`
2. **同步到独立 nginx root**：把站点文件拷到服务器目录，并设
   `WEBUI_PUBLIC_SITE_DIR=/that/dir`，面板保存时会导出 changelog.json 到那里。

面板进程需对 `PUBLIC_SITE_DIR` 有**写权限**（导出 changelog.json 用）。

环境变量（均可选，见 `webui/config.py`）：

| 变量 | 默认 | 作用 |
|------|------|------|
| `WEBUI_PUBLIC_SITE_DIR` | `<repo>/public_site` | changelog.json 导出目录（= nginx root） |
| `WEBUI_PUBLIC_SITE_URL` | `https://bot.oneweblog.cn` | 面板展示的站点地址 |
| `BOT_PUBLIC_SITE_URL`（bot 侧） | `https://bot.oneweblog.cn` | QQ 更新公告文本/卡片里的指路短链 |

## 补演示素材

把 QQ 对话截图/GIF 放进 `assets/`，然后在 `app.js` 的 `DEMOS` 数组里登记
`{ src, caption }`。文件缺失时该格自动降级为占位说明，不影响其余部分。

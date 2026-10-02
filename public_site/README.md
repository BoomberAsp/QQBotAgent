# public_site — Roxy 公开使用站点

面向**普通用户**的只读静态站（`https://bot.oneweblog.cn`）：介绍 + 使用说明 +
录屏案例 + 命令大全 + 自动同步的更新日志 + 反馈/招募入口。
**独立于需登录的 admin WebUI**——无后端、无登录、纯静态。

更新日志数据源 `changelog.json` **不由人工维护**：WebUI 面板每次保存更新日志时，
`webui/data_reader.py::_export_public_changelog()` 会自动把**脱敏后**的
（仅 version/date/changes）快照原子写入本目录。首次保存即生成；缺失时页面优雅降级。

## 目录

```
public_site/
├── index.html          单页（hero / 介绍 / 使用说明 / 案例 / 命令 / 更新日志 / 反馈 / 招募）
├── style.css           深色科技感主题 + 动效（滚动入场 / 光晕 / 导航高亮）
├── app.js              导航高亮、chip 复制、视频互斥播放、拉取渲染 changelog.json
├── changelog.json      ← 自动生成（WebUI 保存时导出），勿手改
└── examples/
    ├── mkv/            原始录屏（1920x1080，内容在左上 610x704 区域）
    ├── mp4/            裁边转码后的网页播放版本（H.264，站点引用这里）
    └── posters/        视频封面帧（jpg）
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
注意 `examples/` 目录（视频约 2MB）需一并同步。

环境变量（均可选，见 `webui/config.py`）：

| 变量 | 默认 | 作用 |
|------|------|------|
| `WEBUI_PUBLIC_SITE_DIR` | `<repo>/public_site` | changelog.json 导出目录（= nginx root） |
| `WEBUI_PUBLIC_SITE_URL` | `https://bot.oneweblog.cn` | 面板展示的站点地址 |
| `BOT_PUBLIC_SITE_URL`（bot 侧） | `https://bot.oneweblog.cn` | QQ 更新公告文本/卡片里的指路短链 |

## 重新处理录屏素材

原始录屏画布 1920x1080，QQ 窗口只占左上 `610x704`。重录后重新生成：

```bash
cd public_site/examples
# 1. 裁边 + 转 MP4（浏览器可播）
ffmpeg -y -i "mkv/<名称>.mkv" -vf "crop=610:704:0:0" \
  -c:v libx264 -crf 23 -preset veryfast -pix_fmt yuv420p -movflags +faststart -an \
  "mp4/<名称>.mp4"
# 2. 封面帧（t 取内容有代表性的秒数）
ffmpeg -y -ss <t> -i "mp4/<名称>.mp4" -frames:v 1 -q:v 3 "posters/<名称>.jpg"
```

若新录屏的窗口位置/尺寸变了，先用
`ffmpeg -ss <t> -i <file> -vf cropdetect -f null -` 重新探测裁剪框。

新增案例：把 mp4/posters 放进去，在 `index.html` 的 `#demos` 区块照抄一个
`<figure class="demo-card">` 即可（视频比例在 CSS 里按 610/704 固定）。

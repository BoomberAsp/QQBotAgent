#!/bin/bash
# ============================================================
# Roxy 公开展示站（bot.oneweblog.cn）—— 部署 / 同步脚本
#
# 与仪表盘启动脚本 start_webui.sh 完全独立：本脚本只负责把 public_site/
# 的静态文件同步到 nginx root 并修正权限。展示站是纯静态站，无守护进程，
# 由 nginx 直接服务，因此这里没有"启动/停止"，只有"同步部署"。
#
# changelog.json 不在本脚本职责内 —— 它由 WebUI 面板每次保存更新记录时
# 自动脱敏导出到同一目录（见 docs/bot-site-nginx.md）。本脚本同步时会
# **保留**已存在的 changelog.json，绝不覆盖或删除。
#
# 用法:
#   bash deploy_public_site.sh            # 同步文件 + 修权限 + reload nginx
#   bash deploy_public_site.sh --no-reload  # 只同步，不动 nginx
# ============================================================
set -euo pipefail

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info() { echo -e "${GREEN}[OK]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
err()  { echo -e "${RED}[ERROR]${NC} $1"; }

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
SRC="$REPO_DIR/public_site"

# nginx root：优先取 QQBot/.env 里的 WEBUI_PUBLIC_SITE_DIR（与面板导出目录保持
# 一致），缺省回落到 /var/www/bot-oneweblog。这样"面板往哪写"和"nginx 从哪读"
# 永远同源，避免两处配置漂移。
ENV_FILE="$REPO_DIR/QQBot/.env"
DST="/var/www/bot-oneweblog"
if [ -f "$ENV_FILE" ]; then
    _v="$(grep -E '^[[:space:]]*WEBUI_PUBLIC_SITE_DIR=' "$ENV_FILE" | tail -1 | cut -d= -f2- | tr -d '"' | tr -d "'" | sed 's/[[:space:]]*$//')"
    [ -n "${_v:-}" ] && DST="$_v"
fi

RELOAD=1
[ "${1:-}" = "--no-reload" ] && RELOAD=0

# 需要同步的静态条目（不含 changelog.json —— 那是面板的产物；
# examples 只同步网页播放用的 mp4 与封面，原始 mkv 不上站）
ITEMS=(index.html style.css app.js assets examples/mp4 examples/posters)

echo "源目录:   $SRC"
echo "nginx root: $DST"

if [ ! -d "$SRC" ]; then
    err "源目录不存在: $SRC"; exit 1
fi

# 创建目标目录（首次部署时）
if [ ! -d "$DST" ]; then
    warn "目标目录不存在，创建: $DST"
    sudo mkdir -p "$DST"
fi

# 同步静态文件（保留 changelog.json 等面板产物）
for item in "${ITEMS[@]}"; do
    if [ -e "$SRC/$item" ]; then
        sudo mkdir -p "$DST/$(dirname "$item")"
        sudo cp -a "$SRC/$item" "$DST/$(dirname "$item")/"
        info "同步 $item"
    else
        warn "源缺少 $item，跳过"
    fi
done

# 权限：ubuntu(面板) 可写、www-data(nginx) 可读
sudo chown -R ubuntu:www-data "$DST"
sudo find "$DST" -type d -exec chmod 755 {} \;
sudo find "$DST" -type f -exec chmod 644 {} \;
info "权限已修正 (ubuntu:www-data, 755/644)"

# nginx 校验 + reload
if [ "$RELOAD" = "1" ]; then
    if sudo nginx -t; then
        sudo systemctl reload nginx
        info "nginx 已 reload"
    else
        err "nginx 配置校验失败，未 reload"; exit 1
    fi
fi

echo
info "部署完成 → https://bot.oneweblog.cn"
echo "  提示：更新记录区的数据来自 changelog.json，由 WebUI 面板保存更新日志时自动生成。"

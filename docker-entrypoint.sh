#!/bin/bash

set -e

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log() {
    echo -e "${GREEN}[$(date +'%Y-%m-%d %H:%M:%S')]${NC} $1"
}

error() {
    echo -e "${RED}[$(date +'%Y-%m-%d %H:%M:%S')] ERROR:${NC} $1" >&2
}

warn() {
    echo -e "${YELLOW}[$(date +'%Y-%m-%d %H:%M:%S')] WARN:${NC} $1"
}

# 从环境变量读取配置
QQ_ACCOUNT="${QQ_ACCOUNT:-}"
QQ_PASSWORD="${QQ_PASSWORD:-}"
NAPCAT_ACCESS_TOKEN="${NAPCAT_ACCESS_TOKEN:-3((oY)?_-$jne##5}"

# 更新 Napcat 配置
update_napcat_config() {
    log "正在配置 Napcat..."

    local config_dir="${HOME}/.config/QQ/napcat"
    mkdir -p "${config_dir}"

    # 创建 Napcat 配置文件
    cat > "${config_dir}/config.json" << EOF
{
    "ws_reverse_url": "ws://127.0.0.1:8080/onebot/v11/ws",
    "ws_reverse_reconnect_interval": 5000,
    "ws_reverse_heartbeat_interval": 5000,
    "access_token": "${NAPCAT_ACCESS_TOKEN}"
}
EOF

    log "Napcat 配置完成"
}

# 更新 NoneBot 配置
update_nonebot_config() {
    log "正在配置 NoneBot..."

    # 更新 .env.prod 文件
    cat > /app/QQBot/.env.prod << EOF
DRIVER=~fastapi
HOST=0.0.0.0
PORT=8081
SUPERUSERS=["${QQ_ACCOUNT:-971561405}"]
ENABLED_GROUPS=["718734404"]
NICKNAME=["Roxy"]
ONEBOT_ACCESS_TOKEN="${NAPCAT_ACCESS_TOKEN}"
EOF

    log "NoneBot 配置完成"
}

# 启动 Napcat
start_napcat() {
    log "正在启动 Napcat (QQ NT)..."

    export XDG_RUNTIME_DIR="/tmp/runtime-${USER}"
    mkdir -p "${XDG_RUNTIME_DIR}"

    # 使用 xvfb-run 运行无头模式
    xvfb-run -a /opt/Napcat/qq --no-sandbox &
    NAPCAT_PID=$!

    log "Napcat 启动成功 (PID: ${NAPCAT_PID})"

    # 等待 Napcat 完全启动
    sleep 5
}

# 启动 NoneBot
start_nonebot() {
    log "正在启动 NoneBot..."

    cd /app/QQBot

    # 激活虚拟环境并启动
    source /app/venv/bin/activate
    nb run --env prod &
    NONEBOT_PID=$!

    log "NoneBot 启动成功 (PID: ${NONEBOT_PID})"
}

# 主函数
main() {
    log "========================================="
    log "QQBot 启动脚本"
    log "========================================="

    # 检查环境变量
    if [ -z "${QQ_ACCOUNT}" ]; then
        warn "未设置 QQ_ACCOUNT 环境变量，使用默认配置"
    else
        log "QQ 账号：${QQ_ACCOUNT}"
    fi

    # 更新配置
    update_napcat_config
    update_nonebot_config

    # 启动服务
    start_napcat
    start_nonebot

    log "========================================="
    log "所有服务已启动"
    log "Napcat: 监听 WebSocket 反向连接"
    log "NoneBot: http://0.0.0.0:8081"
    log "========================================="

    # 等待进程
    wait
}

# 捕获信号进行优雅关闭
trap 'log "收到终止信号，正在关闭..."; kill -TERM $(jobs -p) 2>/dev/null; exit 0' TERM INT

main "$@"

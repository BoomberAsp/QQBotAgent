#!/bin/bash
# ============================================================
# QQBot Agent — Start Bot Only (NoneBot)
# 用于 SearXNG 和 NapCat 已在运行时的快速启动
# ============================================================
set -e

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR/QQBot"

echo -e "${BLUE}=========================================${NC}"
echo -e "${BLUE}   QQBot Agent — 启动 NoneBot${NC}"
echo -e "${BLUE}=========================================${NC}"

# 激活虚拟环境
if [ -f "$HOME/.virtualenvs/QQBotAgent/bin/activate" ]; then
    source "$HOME/.virtualenvs/QQBotAgent/bin/activate"
elif [ -f "../.venv/bin/activate" ]; then
    source "../.venv/bin/activate"
else
    echo -e "${YELLOW}[ERROR]${NC} 未找到 Python 虚拟环境!"
    echo "请先运行: bash setup.sh"
    exit 1
fi

echo -e "${GREEN}[INFO]${NC} 启动 NoneBot (FastAPI + WebSocket)..."
echo -e "  HTTP API: http://0.0.0.0:8081"
echo -e "  WebSocket: ws://0.0.0.0:8081/onebot/v11/ws"
echo ""

nb run

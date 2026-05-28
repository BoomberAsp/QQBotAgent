#!/bin/bash
# ============================================================
# QQBot Agent — Stop All Services
# 停止 NoneBot + SearXNG 容器
# ============================================================

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo -e "${RED}=========================================${NC}"
echo -e "${RED}   QQBot Agent — 停止所有服务${NC}"
echo -e "${RED}=========================================${NC}"

# ── 1. 停止 NoneBot ─────────────────────────────────────────
echo -e "${YELLOW}[1/3]${NC} 停止 NoneBot..."
# Find and kill NoneBot process
NONEBOT_PIDS=$(pgrep -f "nb run" 2>/dev/null || true)
if [ -n "$NONEBOT_PIDS" ]; then
    echo "$NONEBOT_PIDS" | xargs kill 2>/dev/null
    echo -e "${GREEN}[OK]${NC} NoneBot 已停止"
else
    echo -e "${YELLOW}[INFO]${NC} 未找到运行中的 NoneBot 进程"
fi

# Also check for uvicorn (NoneBot's underlying server)
UVICORN_PIDS=$(pgrep -f "uvicorn.*8081" 2>/dev/null || true)
if [ -n "$UVICORN_PIDS" ]; then
    echo "$UVICORN_PIDS" | xargs kill 2>/dev/null
fi

# ── 2. 停止 SearXNG ─────────────────────────────────────────
echo -e "${YELLOW}[2/3]${NC} 停止 SearXNG..."
cd "$SCRIPT_DIR"
if command -v docker &> /dev/null && docker info &> /dev/null 2>&1; then
    docker compose stop searxng 2>/dev/null && \
        echo -e "${GREEN}[OK]${NC} SearXNG 已停止" || \
        echo -e "${YELLOW}[INFO]${NC} SearXNG 容器未运行"
else
    echo -e "${YELLOW}[INFO]${NC} Docker 不可用"
fi

# ── 3. 可选: 停止 NapCat ─────────────────────────────────────
echo -e "${YELLOW}[3/3]${NC} NapCat: 需要手动停止"
echo -e "  如需停止 NapCat, 请关闭 QQ 进程或使用 NapCat WebUI"

echo ""
echo -e "${GREEN}所有服务已停止${NC}"

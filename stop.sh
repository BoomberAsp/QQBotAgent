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
# 真实入口（已在部署服务器核实）：`cd QQBot && nb run`。`nb run` 是启动器，
# 它会派生一个 `python3 -c "...nonebot.load_from_toml..."` 子进程真正监听 8081，
# 所以两者都要匹配，否则杀掉 nb 父进程会留下占着端口的孤儿 worker。
# 明确不匹配 8090 面板进程（webui.main:app），面板需单独用
# `bash start_webui.sh stop` 停止。
echo -e "${YELLOW}[1/3]${NC} 停止 NoneBot..."
_nb_pids() { { pgrep -f "bin/nb run"; pgrep -f "nb run"; pgrep -f "nonebot\.load_from_toml"; pgrep -f "python.*bot\.py$"; } 2>/dev/null | sort -u || true; }
NONEBOT_PIDS=$(_nb_pids)
if [ -n "$NONEBOT_PIDS" ]; then
    echo "$NONEBOT_PIDS" | xargs kill 2>/dev/null
    sleep 1
    # 仍未退出则强制结束
    LEFT=$(_nb_pids)
    if [ -n "$LEFT" ]; then
        echo "$LEFT" | xargs kill -9 2>/dev/null
    fi
    echo -e "${GREEN}[OK]${NC} NoneBot 已停止"
    rm -f "$SCRIPT_DIR/webui/data/nonebot.pid"
else
    echo -e "${YELLOW}[INFO]${NC} 未找到运行中的 NoneBot 进程"
    rm -f "$SCRIPT_DIR/webui/data/nonebot.pid"
fi

# Also check for uvicorn (NoneBot's underlying server) — 仅 8081 端口，
# 面板的 8090 绝不在匹配范围内
UVICORN_PIDS=$(pgrep -f "uvicorn.*8081" 2>/dev/null || true)
if [ -n "$UVICORN_PIDS" ]; then
    echo "$UVICORN_PIDS" | xargs kill 2>/dev/null
fi

# 同步面板看门狗状态：显式停止不应在 30 秒后被看门狗拉起
WD_STATE="$SCRIPT_DIR/webui/data/watchdog_state.json"
if [ -f "$WD_STATE" ]; then
    WD_PY="$HOME/.virtualenvs/QQBotAgent/bin/python"
    [ -x "$WD_PY" ] || WD_PY="$(command -v python3)"
    "$WD_PY" - "$WD_STATE" <<'PYEOF' 2>/dev/null || true
import json, sys, pathlib
p = pathlib.Path(sys.argv[1])
try:
    s = json.loads(p.read_text())
    s.setdefault("processes", {}).setdefault("nonebot", {})["should_run"] = False
    p.write_text(json.dumps(s, indent=2))
except Exception:
    pass
PYEOF
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

#!/bin/bash
# ============================================================
# QQBotAgent WebUI — 管理面板 启动/停止/状态
# 面板仅绑定 127.0.0.1，通过 SSH 隧道访问：
#   ssh -L 8090:127.0.0.1:8090 user@server
# 用法:
#   bash start_webui.sh            # 启动（等价于 start）
#   bash start_webui.sh start|stop|restart|status
# ============================================================

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

WEBUI_HOST="${WEBUI_HOST:-127.0.0.1}"
WEBUI_PORT="${WEBUI_PORT:-8090}"

# Python 解释器：优先 venv
if [ -x "$HOME/.virtualenvs/QQBotAgent/bin/python" ]; then
    PYTHON="$HOME/.virtualenvs/QQBotAgent/bin/python"
elif [ -f ".venv/bin/python" ]; then
    PYTHON="$PWD/.venv/bin/python"
else
    PYTHON="$(command -v python3)"
fi

PID_FILE="$SCRIPT_DIR/webui/data/webui.pid"
LOG_FILE="$SCRIPT_DIR/webui/data/logs/webui.log"
mkdir -p "$SCRIPT_DIR/webui/data/logs"

is_running() {
    if [ -f "$PID_FILE" ]; then
        local pid
        pid=$(cat "$PID_FILE" 2>/dev/null)
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            return 0
        fi
    fi
    # 兜底：匹配监听面板端口的 uvicorn 进程（排除 8081 机器人端口）
    pgrep -f "uvicorn webui.main:app" > /dev/null 2>&1
}

start_panel() {
    if is_running; then
        echo -e "${YELLOW}[INFO]${NC} 面板已在运行"
        return 0
    fi
    echo "[$(date +'%F %T')] ---- WebUI panel starting ----" >> "$LOG_FILE"
    WEBUI_HOST="$WEBUI_HOST" WEBUI_PORT="$WEBUI_PORT" \
        nohup "$PYTHON" -m uvicorn webui.main:app \
            --host "$WEBUI_HOST" --port "$WEBUI_PORT" \
            >> "$LOG_FILE" 2>&1 &
    echo $! > "$PID_FILE"
    sleep 1.5
    if is_running; then
        echo -e "${GREEN}[OK]${NC} WebUI 面板已启动: ${WEBUI_HOST}:${WEBUI_PORT} (PID $(cat "$PID_FILE"))"
        echo -e "  访问: ssh -L ${WEBUI_PORT}:127.0.0.1:${WEBUI_PORT} user@server"
        echo -e "  日志: $LOG_FILE"
    else
        echo -e "${RED}[ERROR]${NC} 面板启动失败，请查看日志: $LOG_FILE"
        return 1
    fi
}

stop_panel() {
    if [ -f "$PID_FILE" ]; then
        local pid
        pid=$(cat "$PID_FILE" 2>/dev/null)
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null
            sleep 1
            kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null
            echo -e "${GREEN}[OK]${NC} WebUI 面板已停止"
        else
            echo -e "${YELLOW}[INFO]${NC} PID 文件存在但进程未运行"
        fi
        rm -f "$PID_FILE"
    else
        # 兜底：按命令行模式停止（绝不匹配机器人端口 8081）
        local pids
        pids=$(pgrep -f "uvicorn webui.main:app" 2>/dev/null || true)
        if [ -n "$pids" ]; then
            echo "$pids" | xargs kill 2>/dev/null
            echo -e "${GREEN}[OK]${NC} WebUI 面板已停止"
        else
            echo -e "${YELLOW}[INFO]${NC} 面板未在运行"
        fi
    fi
}

case "${1:-start}" in
    start)   start_panel ;;
    stop)    stop_panel ;;
    restart) stop_panel; sleep 1; start_panel ;;
    status)
        if is_running; then
            echo -e "${GREEN}运行中${NC} — ${WEBUI_HOST}:${WEBUI_PORT}"
        else
            echo -e "${RED}未运行${NC}"
        fi
        ;;
    *) echo "用法: bash start_webui.sh [start|stop|restart|status]"; exit 1 ;;
esac

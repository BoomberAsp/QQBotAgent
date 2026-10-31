#!/bin/bash
# ============================================================
# QQBot Agent — Start All Services
# 启动 SearXNG (Docker) + 运行 NoneBot + NapCat 检查
# ============================================================
set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

log()  { echo -e "${GREEN}[$(date +'%H:%M:%S')]${NC} $1"; }
warn() { echo -e "${YELLOW}[$(date +'%H:%M:%S')] WARN:${NC} $1"; }
err()  { echo -e "${RED}[$(date +'%H:%M:%S')] ERROR:${NC} $1"; }

cleanup() {
    log "正在关闭所有服务..."
    if [ -n "$NONEBOT_PID" ]; then
        kill "$NONEBOT_PID" 2>/dev/null
    fi
    log "已关闭"
}
trap cleanup EXIT INT TERM

echo -e "${BLUE}=========================================${NC}"
echo -e "${BLUE}   QQBot Agent — 启动所有服务${NC}"
echo -e "${BLUE}=========================================${NC}"

# ── 1. 检查 .env 配置 ───────────────────────────────────────
log "检查配置..."
ENV_FILE="QQBot/.env"
if [ ! -f "$ENV_FILE" ]; then
    err ".env 文件不存在! 请先运行: bash setup.sh"
    exit 1
fi

# 检查关键配置项
check_env() {
    local key=$1
    local file=$2
    if ! grep -q "^${key}=" "$file" 2>/dev/null; then
        warn "${key} 未在 .env 中配置"
        return 1
    fi
    return 0
}

WARNINGS=0
for key in DEEPSEEK_API_KEY ONEBOT_ACCESS_TOKEN; do
    if ! check_env "$key" "$ENV_FILE"; then
        WARNINGS=$((WARNINGS + 1))
    fi
done
if [ $WARNINGS -gt 0 ]; then
    echo -e "${YELLOW}[!!!]${NC} 有 ${WARNINGS} 个配置项缺失，某些功能可能不可用"
fi

# ── 2. 启动 SearXNG ─────────────────────────────────────────
log "启动 SearXNG 搜索引擎..."
SEARXNG_STARTED=false
if command -v docker &> /dev/null && docker info &> /dev/null 2>&1; then
    cd "$SCRIPT_DIR"
    if docker compose up -d searxng 2>/dev/null; then
        log "SearXNG 已启动 (http://localhost:8082)"
        SEARXNG_STARTED=true
    else
        warn "SearXNG 启动失败"
        warn "搜索功能将不可用。修复后运行: docker compose up -d searxng"
    fi
else
    warn "Docker 不可用，SearXNG 未启动。搜索功能将不可用。"
fi

# ── 3. 检查 NapCat ──────────────────────────────────────────
log "检查 NapCat 状态..."
NAPCAT_RUNNING=false
if pgrep -f "napcat" > /dev/null 2>&1 || pgrep -f "/opt/Napcat/qq" > /dev/null 2>&1 || pgrep -f "QQ/qq" > /dev/null 2>&1; then
    log "NapCat 进程已运行"
    NAPCAT_RUNNING=true
else
    warn "NapCat 未运行，机器人将无法接收 QQ 消息!"
    echo -e "  启动 NapCat 的方法:"
    echo -e "    方式1: 使用 WebUI 启动 (推荐)"
    echo -e "    方式2: ${YELLOW}xvfb-run -a ~/Napcat/opt/QQ/qq --no-sandbox${NC}"
    echo -e ""
    read -p "继续启动 NoneBot? (仅 API 可用，无法收发 QQ 消息) [Y/n]: " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Nn]$ ]]; then
        exit 0
    fi
fi

# ── 4. 验证 SearXNG 可用 ────────────────────────────────────
if [ "$SEARXNG_STARTED" = true ]; then
    log "验证 SearXNG 连接..."
    # Wait for SearXNG to be ready
    for i in $(seq 1 10); do
        if curl -s -o /dev/null -w "%{http_code}" "http://localhost:8082/search?format=json&q=test" 2>/dev/null | grep -q "200\|403"; then
            log "SearXNG 响应正常"
            break
        fi
        sleep 2
    done
fi

# ── 5. 启动 NoneBot ─────────────────────────────────────────
# 真实入口（已在部署服务器核实）：`cd QQBot && nb run`。
# nb run 读取 QQBot/pyproject.toml（plugin_dirs=["plugins"]），cwd=QQBot
# 才能让 `from agent.agent import Agent` / lib.* / tools.* 作为顶层包导入；
# 端口 8081 来自 QQBot/.env（HOST/PORT）。切勿改成 `python bot.py`（仓库根），
# 那会因 QQBot/ 不在 sys.path 而 ModuleNotFoundError: No module named 'agent'。
log "启动 NoneBot Agent..."
cd "$SCRIPT_DIR/QQBot"

# 激活虚拟环境
if [ -f "$HOME/.virtualenvs/QQBotAgent/bin/activate" ]; then
    source "$HOME/.virtualenvs/QQBotAgent/bin/activate"
elif [ -f "../.venv/bin/activate" ]; then
    source "../.venv/bin/activate"
elif command -v nb &> /dev/null; then
    : # nb command is already available
else
    err "未找到 Python 虚拟环境! 请先运行: bash setup.sh"
    exit 1
fi

# ── 5.1 启动前清理存量 NoneBot 实例 ─────────────────────────────
# 现象（2026-09-22 服务器排查证实）：已有实例占着 8081 时，`nb run` 插件
# 加载日志一切正常，直到 uvicorn bind 才报 [Errno 98] address already in use
# 退出；随后 `wait` 返回、脚本静默结束——观感即「WebUI 起来了、bot 起不来，
# 必须在面板里重启」。故启动前按 stop.sh 同款匹配逻辑清掉存量实例。
_nb_pids() { { pgrep -f "bin/nb run"; pgrep -f "nb run"; pgrep -f "nonebot\.load_from_toml"; pgrep -f "python.*bot\.py$"; pgrep -f "uvicorn.*8081"; } 2>/dev/null | sort -u || true; }
OLD_PIDS=$(_nb_pids)
if [ -n "$OLD_PIDS" ]; then
    warn "检测到已在运行的 NoneBot 实例 (PID: $(echo $OLD_PIDS | tr '\n' ' '))，先停止..."
    echo "$OLD_PIDS" | xargs kill 2>/dev/null || true
    sleep 1
    LEFT=$(_nb_pids)
    if [ -n "$LEFT" ]; then
        warn "仍未退出，强制结束: $(echo $LEFT | tr '\n' ' ')"
        echo "$LEFT" | xargs kill -9 2>/dev/null || true
    fi
    log "旧 NoneBot 实例已停止"
    # 同步面板状态：清 PID 文件 + 关看门狗 should_run，
    # 避免面板看门狗与本轮启动竞争（与 stop.sh 行为一致）
    rm -f "$SCRIPT_DIR/webui/data/nonebot.pid"
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
fi

# 等待 8081 端口真正释放（最长 10 秒）；仍被占用则明确报错退出，
# 绝不再让 `nb run` 静默 bind 失败
_port_8081_busy() { (exec 3<>/dev/tcp/127.0.0.1/8081) 2>/dev/null; }
for _i in $(seq 1 10); do
    _port_8081_busy || break
    sleep 1
done
if _port_8081_busy; then
    err "端口 8081 仍被占用，无法启动 NoneBot。请排查: ss -tlnp | grep 8081"
    exit 1
fi

# 启动
nb run &
NONEBOT_PID=$!
log "NoneBot 启动中 (PID: $NONEBOT_PID)"

# 等待启动完成
sleep 3

# ── 5.5 重启 WebUI 管理面板（独立进程，失败不影响机器人）──
# 用 restart 而非 start：面板已在运行时也强制重启，保证 git pull 后
# 面板代码即时生效，且面板以干净状态重新收养本轮拉起的 NoneBot。
# 面板未运行时 restart 等价于 start（stop 为空操作）。
log "重启 WebUI 管理面板..."
if bash "$SCRIPT_DIR/start_webui.sh" restart; then
    :
else
    warn "WebUI 面板启动失败（不影响机器人）"
fi

# ── 6. 状态展示 ─────────────────────────────────────────────
echo ""
echo -e "${BLUE}=========================================${NC}"
echo -e "${GREEN}   QQBot Agent 运行中${NC}"
echo -e "${BLUE}=========================================${NC}"
echo -e "  NoneBot API:  ${GREEN}http://0.0.0.0:8081${NC}"
echo -e "  WebUI 面板:   ${GREEN}http://127.0.0.1:8090${NC} (ssh -L 8090:127.0.0.1:8090 user@server)"
echo -e "  SearXNG:      ${GREEN}http://localhost:8082${NC} $([ "$SEARXNG_STARTED" = true ] && echo '(运行中)' || echo '(未启动)')"
echo -e "  NapCat:       $([ "$NAPCAT_RUNNING" = true ] && echo -e "${GREEN}(运行中)${NC}" || echo -e "${RED}(未运行)${NC}")"
echo -e ""
echo -e "  在 QQ 中发送: ${YELLOW}@Roxy /status${NC} 验证机器人状态"
echo -e "  查看日志:     ${YELLOW}tail -f QQBot/logs/*.log${NC}"
echo -e "  停止服务:     ${YELLOW}bash stop.sh${NC} 或按 Ctrl+C"
echo -e "${BLUE}=========================================${NC}"

# 等待 NoneBot 进程
wait $NONEBOT_PID

#!/bin/bash
# ============================================================
# QQBot Agent — Run Test Suite
# 运行 38 项单元测试，覆盖 Agent / 会话 / 记忆 / 工具等
# ============================================================
set -e

GREEN='\033[0;32m'
RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR/QQBot"

echo -e "${BLUE}=========================================${NC}"
echo -e "${BLUE}   QQBot Agent — 运行测试套件${NC}"
echo -e "${BLUE}=========================================${NC}"

# 激活虚拟环境
if [ -f "$HOME/.virtualenvs/QQBotAgent/bin/activate" ]; then
    source "$HOME/.virtualenvs/QQBotAgent/bin/activate"
elif [ -f "../.venv/bin/activate" ]; then
    source "../.venv/bin/activate"
fi

echo -e "${GREEN}[INFO]${NC} 运行所有测试..."
echo ""

python test_agent.py

EXIT_CODE=$?
echo ""
if [ $EXIT_CODE -eq 0 ]; then
    echo -e "${GREEN}=========================================${NC}"
    echo -e "${GREEN}   全部测试通过 ✓${NC}"
    echo -e "${GREEN}=========================================${NC}"
else
    echo -e "${RED}=========================================${NC}"
    echo -e "${RED}   测试失败 ✗ (退出码: $EXIT_CODE)${NC}"
    echo -e "${RED}=========================================${NC}"
fi

exit $EXIT_CODE

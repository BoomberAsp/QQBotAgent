#!/bin/bash
# ============================================================
# QQBot Agent — Run Test Suite
# 运行全部单元测试，覆盖 Agent / 会话 / 记忆 / 任务封装 / 工具等
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
AGENT_EXIT=$?

python test_workspace.py
WS_EXIT=$?

# 离线测速管线测试（从仓库根目录运行）
cd "$SCRIPT_DIR"
python test/test_ag_skill_index.py
L1_EXIT=$?
python test/test_ag_trigger_engine.py
ENGINE_EXIT=$?
python test/test_path_resolve.py
PATH_EXIT=$?
python test/test_acting_value.py
ACTING_EXIT=$?
python test/test_aliases.py
ALIAS_EXIT=$?
python test/test_card_renderer_images.py
CARDIMG_EXIT=$?
python test/test_task_record.py
TASKREC_EXIT=$?
python test/test_character_lookup_and_heal.py
CHARHEAL_EXIT=$?
cd "$SCRIPT_DIR/QQBot"

EXIT_CODE=0
if [ $AGENT_EXIT -ne 0 ]; then EXIT_CODE=$AGENT_EXIT; fi
if [ $WS_EXIT -ne 0 ]; then EXIT_CODE=$WS_EXIT; fi
if [ $L1_EXIT -ne 0 ]; then EXIT_CODE=$L1_EXIT; fi
if [ $ENGINE_EXIT -ne 0 ]; then EXIT_CODE=$ENGINE_EXIT; fi
if [ $PATH_EXIT -ne 0 ]; then EXIT_CODE=$PATH_EXIT; fi
if [ $ACTING_EXIT -ne 0 ]; then EXIT_CODE=$ACTING_EXIT; fi
if [ $ALIAS_EXIT -ne 0 ]; then EXIT_CODE=$ALIAS_EXIT; fi
if [ $CARDIMG_EXIT -ne 0 ]; then EXIT_CODE=$CARDIMG_EXIT; fi
if [ $TASKREC_EXIT -ne 0 ]; then EXIT_CODE=$TASKREC_EXIT; fi
if [ $CHARHEAL_EXIT -ne 0 ]; then EXIT_CODE=$CHARHEAL_EXIT; fi
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

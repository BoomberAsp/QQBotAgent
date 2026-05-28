#!/bin/bash
# ============================================================
# QQBot Agent — Quick Setup Script
# 一键安装所有依赖: 系统库, Python虚拟环境, Pip包, NapCat, SearXNG
# ============================================================
set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo -e "${BLUE}=========================================${NC}"
echo -e "${BLUE}   QQBot Agent — 环境安装${NC}"
echo -e "${BLUE}=========================================${NC}"

# ── 0. 检测系统类型 ──────────────────────────────────────────
detect_os() {
    if [ -f /etc/os-release ]; then
        . /etc/os-release
        OS=$ID
    else
        OS=$(uname -s)
    fi
    echo -e "${GREEN}[INFO]${NC} 检测到系统: ${OS}"
}

# ── 1. 安装系统依赖 ─────────────────────────────────────────
install_system_deps() {
    echo -e "\n${YELLOW}[1/5]${NC} 安装系统依赖..."

    case "$OS" in
        ubuntu|debian)
            sudo apt-get update -qq

            # Static packages (no t64 variants)
            STATIC_PKGS="python3.12 python3.12-venv python3-pip \
                xvfb xauth curl jq \
                libnss3 libgbm1 libxss1 libxrandr2 libxcomposite1 \
                libxcursor1 libxdamage1 libxfixes3 libxext6 \
                libxrender1 libxkbcommon0 libcairo2 libdrm2 \
                fonts-liberation fonts-noto-color-emoji \
                git docker.io docker-compose-v2 ffmpeg"

            # Packages that may have t64 variants on Ubuntu 24.04+
            PKGS_TO_CHECK=(
                "libglib2.0-0"
                "libatk1.0-0"
                "libatspi2.0-0"
                "libgtk-3-0"
                "libasound2"
                "libpango-1.0-0"
            )

            echo -e "${GREEN}[INFO]${NC} 检测系统库版本 (t64)..."
            RESOLVED_PKGS=""
            for pkg_base in "${PKGS_TO_CHECK[@]}"; do
                t64_variant="${pkg_base}t64"
                if apt-cache show "${t64_variant}" >/dev/null 2>&1; then
                    echo -e "${GREEN}[INFO]${NC}   使用 ${t64_variant}"
                    RESOLVED_PKGS="${RESOLVED_PKGS} ${t64_variant}"
                else
                    echo -e "${GREEN}[INFO]${NC}   使用 ${pkg_base}"
                    RESOLVED_PKGS="${RESOLVED_PKGS} ${pkg_base}"
                fi
            done

            sudo apt-get install -y -qq ${STATIC_PKGS} ${RESOLVED_PKGS}
            sudo systemctl enable docker --now
            ;;
#        *)  echo -e "Try to update apt first. Use sudo apt update"
#            ;;
        arch|manjaro)
            sudo pacman -S --noconfirm --needed \
                python python-pip python-virtualenv \
                xvfb curl jq git docker docker-compose ffmpeg
            ;;
        *)
            echo -e "${YELLOW}[WARN]${NC} 未识别的系统，请手动安装: python3.12, xvfb, curl, git, docker"
            ;;
    esac
    echo -e "${GREEN}[OK]${NC} 系统依赖安装完成"
}

# ── 2. 创建 Python 虚拟环境 ─────────────────────────────────
setup_venv() {
    echo -e "\n${YELLOW}[2/5]${NC} 创建 Python 虚拟环境..."

    VENV_DIR="$HOME/.virtualenvs/QQBotAgent"

    if [ -d "$VENV_DIR" ]; then
        echo -e "${GREEN}[INFO]${NC} 虚拟环境已存在: $VENV_DIR"
    else
        python3.12 -m venv "$VENV_DIR"
        echo -e "${GREEN}[OK]${NC} 虚拟环境创建完成: $VENV_DIR"
    fi

    # 激活并升级 pip
    source "$VENV_DIR/bin/activate"
    pip install --upgrade pip -q
    echo -e "${GREEN}[OK]${NC} pip 已升级"
}

# ── 3. 安装 Python 依赖 ─────────────────────────────────────
install_python_deps() {
    echo -e "\n${YELLOW}[3/5]${NC} 安装 Python 依赖..."

    source "$HOME/.virtualenvs/QQBotAgent/bin/activate"
    pip install -r QQBot/requirements.txt -q
    echo -e "${GREEN}[OK]${NC} Python 依赖安装完成"
}

# ── 4. 配置 NoneBot 环境变量 ─────────────────────────────────
setup_env() {
    echo -e "\n${YELLOW}[4/5]${NC} 配置 NoneBot 环境变量..."

    ENV_FILE="QQBot/.env"
    if [ -f "$ENV_FILE" ]; then
        echo -e "${GREEN}[INFO]${NC} .env 文件已存在，跳过创建"
        echo -e "${YELLOW}[WARN]${NC} 请确保 $ENV_FILE 包含以下必要配置:"
        echo "  DRIVER=~fastapi"
        echo "  HOST=0.0.0.0"
        echo "  PORT=8081"
        echo "  ONEBOT_ACCESS_TOKEN=<你的Token>"
        echo "  SUPERUSERS=[\"你的QQ号\"]"
        echo "  DEEPSEEK_API_KEY=<你的DeepSeek API Key>"
        echo "  DEEPSEEK_API_BASE=https://api.deepseek.com"
        echo "  SEARXNG_ENDPOINT=http://localhost:8082"
        echo "  AMAP_API_KEY=<你的高德地图Key> (可选, 用于地图工具)"
        echo "  USER_DATA_ROOT=/data/qqbot/users (用户数据根目录)"
        echo "  MAX_SPECIAL_SESSIONS=3 (每用户最大特殊会话数)"
        echo "  USER_WORKSPACE_QUOTA_MB=500 (每用户工作区配额)"
    else
        cat > "$ENV_FILE" << 'EOF'
DRIVER=~fastapi
HOST=0.0.0.0
PORT=8081
ONEBOT_ACCESS_TOKEN=请修改为你的Token
SUPERUSERS=["你的QQ号"]
COMMAND_START=["/", ""]
COMMAND_SEP=[" "]
DEEPSEEK_API_KEY=请修改为你的DeepSeek API Key （或其它厂商提供的LLM API key，记得删除括号）
DEEPSEEK_API_BASE=https://api.deepseek.com （或其它厂商提供的LLM base url，记得删除括号）
SEARXNG_ENDPOINT=http://localhost:8082
# 高德地图 (可选, 用于地图/天气/路径规划)
AMAP_API_KEY=
# ── 用户数据 & 特殊会话 (v2.13) ──
USER_DATA_ROOT=/data/qqbot/users
MAX_SPECIAL_SESSIONS=3
USER_WORKSPACE_QUOTA_MB=500
EOF
        echo -e "${GREEN}[OK]${NC} .env 模板已创建"
        echo -e "${RED}[!!!]${NC} 请编辑 QQBot/.env 填入你的配置:"
        echo -e "  - ONEBOT_ACCESS_TOKEN (与 NapCat WebUI 中一致)"
        echo -e "  - SUPERUSERS (你的 QQ 号)"
        echo -e "  - DEEPSEEK_API_KEY (DeepSeek API 密钥)"
    fi
}

# ── 5. 配置多模型 (可选) ─────────────────────────────────────
setup_models() {
    echo -e "\n${YELLOW}[5/5]${NC} 配置多模型 (可选)..."

    MODELS_FILE="QQBot/config/models_settings.json"
    if [ -f "$MODELS_FILE" ]; then
        echo -e "${GREEN}[INFO]${NC} models_settings.json 已存在"
        # Check if it has actual values
        if grep -q '"api_key": ""' "$MODELS_FILE" 2>/dev/null; then
            echo -e "${YELLOW}[WARN]${NC} 模型配置为空，将使用 .env 中的默认 DeepSeek 配置"
            echo -e "${YELLOW}[WARN]${NC} 要启用多模型路由，请编辑: $MODELS_FILE"
        fi
    else
        # Copy from example
        if [ -f "QQBot/config/models_settings_example.json" ]; then
            cp QQBot/config/models_settings_example.json "$MODELS_FILE"
            echo -e "${GREEN}[OK]${NC} 已从模板创建 models_settings.json (使用默认配置)"
        else
            echo -e "${YELLOW}[WARN]${NC} 未找到 models_settings_example.json，跳过"
        fi
    fi
}

# ── 6. 配置 Docker 国内镜像源 ──────────────────────────────────
setup_docker_mirror() {
    echo -e "\n${YELLOW}[可选]${NC} 配置 Docker 国内镜像源..."

    if ! command -v docker &> /dev/null || ! docker info &> /dev/null 2>&1; then
        echo -e "${YELLOW}[INFO]${NC} Docker 不可用，跳过镜像源配置"
        return
    fi

    DOCKER_CONFIG="/etc/docker/daemon.json"
    MIRROR_URL="https://mirror.ccs.tencentyun.com"

    if [ -f "$DOCKER_CONFIG" ]; then
        # Check if already has registry-mirrors
        if grep -q "registry-mirrors" "$DOCKER_CONFIG" 2>/dev/null; then
            echo -e "${GREEN}[INFO]${NC} Docker 镜像源已配置，跳过"
            return
        fi
        # Merge: add registry-mirrors to existing config
        echo -e "${GREEN}[INFO]${NC} 更新现有 Docker 配置..."
        # Use python to merge JSON (reliable across platforms)
        sudo python3 -c "
import json
with open('$DOCKER_CONFIG') as f:
    cfg = json.load(f)
cfg['registry-mirrors'] = ['$MIRROR_URL']
with open('$DOCKER_CONFIG', 'w') as f:
    json.dump(cfg, f, indent=2)
"
    else
        echo -e "${GREEN}[INFO]${NC} 创建 Docker 镜像源配置..."
        sudo mkdir -p /etc/docker
        sudo tee "$DOCKER_CONFIG" > /dev/null << EOF
{
  "registry-mirrors": ["$MIRROR_URL"]
}
EOF
    fi

    # Restart Docker to apply
    sudo systemctl restart docker 2>/dev/null && \
        echo -e "${GREEN}[OK]${NC} Docker 镜像源已配置: $MIRROR_URL" || \
        echo -e "${YELLOW}[WARN]${NC} Docker 配置已写入但重启失败，请手动重启 docker 服务"
}

# ── 7. 启动 SearXNG ──────────────────────────────────────────
start_searxng() {
    echo -e "\n${YELLOW}[可选]${NC} 启动 SearXNG 搜索引擎..."

    if ! command -v docker &> /dev/null || ! docker info &> /dev/null 2>&1; then
        echo -e "${YELLOW}[WARN]${NC} Docker 不可用，跳过 SearXNG 启动"
        return
    fi

    cd "$SCRIPT_DIR"

    # Pull image (may use configured mirror)
    echo -e "${GREEN}[INFO]${NC} 拉取 SearXNG 镜像..."
    docker pull searxng/searxng:latest 2>/dev/null && \
        echo -e "${GREEN}[OK]${NC} 镜像拉取完成" || \
        echo -e "${YELLOW}[WARN]${NC} 镜像拉取失败，将尝试使用本地缓存"

    # Start container
    docker compose up -d searxng 2>/dev/null && \
        echo -e "${GREEN}[OK]${NC} SearXNG 容器已启动" || {
        echo -e "${RED}[ERROR]${NC} SearXNG 启动失败，搜索功能将不可用"
        return
    }

    # Verify SearXNG is responding
    echo -e "${GREEN}[INFO]${NC} 验证 SearXNG 连接 (最多等待 30s)..."
    for i in $(seq 1 15); do
        STATUS=$(curl -s -o /dev/null -w "%{http_code}" "http://localhost:8082/search?format=json&q=test" 2>/dev/null || echo "000")
        if [ "$STATUS" = "200" ] || [ "$STATUS" = "403" ]; then
            echo -e "${GREEN}[OK]${NC} SearXNG 响应正常 (HTTP $STATUS, http://localhost:8082)"
            return
        fi
        sleep 2
    done

    echo -e "${RED}[ERROR]${NC} SearXNG 启动后无响应"
    echo ""
    echo -e "${YELLOW}排查建议:${NC}"
    echo "  1. 查看容器日志: docker logs searxng --tail 30"
    echo "  2. 检查 settings.yml 语法: docker exec searxng cat /etc/searxng/settings.yml"
    echo "  3. 测试容器内出站网络: docker exec searxng curl -I https://www.bing.com"
    echo "  4. 如果 DNS 解析失败, 检查 docker-compose.yml 中的 dns 配置"
}

# ── Main ─────────────────────────────────────────────────────
main() {
    detect_os

    # 询问是否安装系统依赖
    read -p "是否安装系统依赖？(需要 sudo) [y/N]: " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        install_system_deps
        setup_docker_mirror
        start_searxng
    fi

    setup_venv
    install_python_deps
    setup_env
    setup_models

    echo -e "\n${BLUE}=========================================${NC}"
    echo -e "${GREEN}  安装完成!${NC}"
    echo -e "${BLUE}=========================================${NC}"
    echo ""
    echo -e "下一步:"
    echo -e "  1. 编辑 ${YELLOW}QQBot/.env${NC} 填入你的配置密钥"
    echo -e "  2. (可选) 编辑 ${YELLOW}QQBot/config/models_settings.json${NC} 配置多模型"
    echo -e "  3. 安装 NapCat: ${YELLOW}bash napcat.sh --docker n${NC}"
    echo -e "  4. 在 NapCat WebUI 中配置反向 WebSocket 连接到 ws://127.0.0.1:8081/onebot/v11/ws"
    echo -e "  5. 启动: ${YELLOW}bash start.sh${NC}"
    echo ""
}

main "$@"

# QQBot Dockerfile
# 基于 Ubuntu + Napcat + NoneBot2 的 QQ 机器人
# 支持 NVIDIA GPU (vLLM 推理)

FROM nvidia/cuda:12.1-cudnn8-runtime-ubuntu:22.04

LABEL maintainer="Roxy"
LABEL description="QQBot based on Napcat + NoneBot2 with GPU support (vLLM)"

# 设置环境变量
ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# 设置工作目录
WORKDIR /app

# 安装系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3.12 \
    python3.12-venv \
    python3-pip \
    curl \
    jq \
    xvfb \
    xauth \
    procps \
    libnss3 \
    libgbm1 \
    libglib2.0-0 \
    libatk1.0-0 \
    libatspi2.0-0 \
    libgtk-3-0 \
    libasound2 \
    libxss1 \
    libxrandr2 \
    libxcomposite1 \
    libxcursor1 \
    libxdamage1 \
    libxfixes3 \
    libxext6 \
    libxrender1 \
    libxkbcommon0 \
    libpango-1.0-0 \
    libcairo2 \
    libdrm2 \
    fonts-liberation \
    fonts-noto-color-emoji \
    xdg-utils \
    git \
    && rm -rf /var/lib/apt/lists/*

# 设置 vLLM 和 PyTorch 环境变量
ENV PIP_EXTRA_INDEX_URL=https://download.pytorch.org/whl/cu121

# 创建 Napcat 安装目录
RUN mkdir -p /opt/Napcat/opt/QQ

# 下载并安装 Linux QQ (NT 架构)
RUN curl -L -o /tmp/QQ.deb https://dldir1.qq.com/qqfile/qq/QQNT/c773cdf7/linuxqq_3.2.19-39038_amd64.deb \
    && dpkg -x /tmp/QQ.deb /opt/Napcat \
    && rm /tmp/QQ.deb

# 设置 QQ 基础路径
ENV QQ_BASE_PATH=/opt/Napcat
ENV TARGET_FOLDER=/opt/Napcat/resources/app/app_launcher

# 下载并安装 NapCat
RUN curl -L -o /tmp/NapCat.Shell.zip https://github.com/NapNeko/NapCatQQ/releases/latest/download/NapCat.Shell.zip \
    && unzip -q /tmp/NapCat.Shell.zip -d /tmp/NapCat \
    && mkdir -p ${TARGET_FOLDER}/napcat \
    && cp -r /tmp/NapCat/* ${TARGET_FOLDER}/napcat/ \
    && chmod -R +x ${TARGET_FOLDER}/napcat/ \
    && rm -rf /tmp/NapCat.Shell.zip /tmp/NapCat

# 配置 QQ 启动器使用 NapCat
RUN echo '(async () => {await import("file://'"${TARGET_FOLDER}"'/napcat/napcat.mjs");})();' > ${QQ_BASE_PATH}/resources/app/loadNapCat.js \
    && jq '.main = "./loadNapCat.js"' ${QQ_BASE_PATH}/resources/app/package.json > /tmp/package.json.tmp \
    && mv /tmp/package.json.tmp ${QQ_BASE_PATH}/resources/app/package.json

# 复制 QQBot 项目文件
COPY QQBot/ /app/QQBot/

# 创建 Python 虚拟环境并安装依赖
RUN python3.12 -m venv /app/venv \
    && /app/venv/bin/pip install --upgrade pip \
    && /app/venv/bin/pip install -r /app/QQBot/requirements.txt

# 创建数据目录
RUN mkdir -p /app/data/images /app/data/videos /app/logs /app/models

# 创建模型目录软链接（方便挂载）
RUN ln -sf /app/models /app/QQBot/models 2>/dev/null || true

# 创建启动脚本
COPY docker-entrypoint.sh /app/docker-entrypoint.sh
COPY vllm-start.sh /app/vllm-start.sh
RUN chmod +x /app/docker-entrypoint.sh /app/vllm-start.sh

# 暴露端口
# 8080: Napcat WebSocket 反向连接端口
# 8081: NoneBot HTTP API 端口
# 8000: vLLM 推理服务端口
# 6099: Napcat WebUI
EXPOSE 8080 8081 8000 6099

# 设置入口点
ENTRYPOINT ["/app/docker-entrypoint.sh"]

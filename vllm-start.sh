#!/bin/bash
# vLLM 推理服务启动脚本

set -e

# 模型配置
MODEL_NAME="${VLLM_MODEL:-Qwen/Qwen2.5-3B-Instruct}"
MAX_MODEL_LEN="${VLLM_MAX_MODEL_LEN:-8192}"
TENSOR_PARALLEL_SIZE="${VLLM_TENSOR_PARALLEL_SIZE:-1}"
GPU_MEMORY_UTILIZATION="${VLLM_GPU_MEMORY_UTILIZATION:-0.8}"

# 模型目录
MODEL_PATH="/app/models/${MODEL_NAME//\//_}"

echo "========================================="
echo "vLLM 推理服务"
echo "========================================="
echo "模型：${MODEL_NAME}"
echo "模型路径：${MODEL_PATH}"
echo "最大序列长度：${MAX_MODEL_LEN}"
echo "张量并行度：${TENSOR_PARALLEL_SIZE}"
echo "显存利用率：${GPU_MEMORY_UTILIZATION}"
echo "========================================="

# 激活虚拟环境
source /app/venv/bin/activate

# 检查模型是否存在，不存在则下载
if [ ! -d "${MODEL_PATH}" ]; then
    echo "模型不存在，正在下载：${MODEL_NAME}"
    mkdir -p "${MODEL_PATH}"
    huggingface-cli download "${MODEL_NAME}" --local-dir "${MODEL_PATH}"
fi

# 启动 vLLM 服务
python -m vllm.entrypoints.api_server \
    --model "${MODEL_PATH}" \
    --host 0.0.0.0 \
    --port 8000 \
    --served-name "${MODEL_NAME}" \
    --max-model-len "${MAX_MODEL_LEN}" \
    --tensor-parallel-size "${TENSOR_PARALLEL_SIZE}" \
    --gpu-memory-utilization "${GPU_MEMORY_UTILIZATION}" \
    --dtype float16 \
    --api-key "${VLLM_API_KEY:-sk-qqbot-vllm}"

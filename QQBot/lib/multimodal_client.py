"""
Multimodal LLM Client — Vision/image understanding backend.

Supports OpenAI-compatible vision API format (GPT-4V, Claude Vision,
local vLLM with vision models, etc.).

Config is stored in a git-ignored JSON file (QQBot/config/multimodal.json).
When config is empty/invalid, the client degrades gracefully.
"""

import base64
import json
import os
from typing import Dict, Optional

import httpx


class MultimodalClient:
    """Async HTTP client for multimodal LLM (image understanding).

    Uses OpenAI-compatible vision API format (GPT-4V style).
    Reads API credentials from a JSON config file.
    """

    def __init__(self, config_path: str = None):
        if config_path is None:
            # Prefer new unified config, fall back to legacy multimodal.json
            _project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            models_path = os.path.join(_project_dir, "config", "models_settings.json")
            legacy_path = os.path.join(_project_dir, "config", "multimodal.json")
            if os.path.exists(models_path):
                config_path = models_path
            else:
                config_path = legacy_path
        self._config_path = config_path
        self._config = None
        self._load_config()

    def _load_config(self):
        """Load multimodal config from JSON file. Never raises.

        When loaded from models_settings.json, extracts only the
        MULTIMODAL_MODEL subsection. Falls back cleanly if config
        is missing or malformed.
        """
        try:
            if not os.path.exists(self._config_path):
                self._config = None
                return
            with open(self._config_path, "r", encoding="utf-8") as f:
                raw_config = json.load(f)

            # If loaded from models_settings.json, extract MULTIMODAL_MODEL
            if "MULTIMODAL_MODEL" in raw_config:
                self._config = raw_config["MULTIMODAL_MODEL"]
            else:
                self._config = raw_config
        except (json.JSONDecodeError, IOError, OSError):
            self._config = None

    # ── Availability Check ──────────────────────────────────────────

    def is_available(self) -> bool:
        """Return True if multimodal LLM is properly configured."""
        if not self._config:
            return False
        api_key = self._config.get("api_key", "")
        api_base = self._config.get("api_base", "")
        return bool(api_key and api_base)

    # ── Image Analysis ──────────────────────────────────────────────

    async def analyze_image(
        self,
        image_path: str,
        prompt: str = None,
        timeout: float = 60.0,
    ) -> str:
        """Analyze an image using the multimodal LLM.

        Args:
            image_path: Absolute path to the image file.
            prompt: Text prompt to accompany the image.
                    Default: Chinese instruction to describe the image.
            timeout: Request timeout in seconds.

        Returns:
            Analysis text string, or an error message if unavailable/failed.
        """
        if not self.is_available():
            return (
                "[多模态] 图片分析功能未配置。请联系管理员编辑以下文件:\n"
                "  QQBot/config/models_settings.json的MULTIMODAL_MODEL部分\n\n"
                "需要填写:\n"
                "  - api_key: 多模态 LLM 的 API 密钥\n"
                "  - api_base: API 端点地址\n"
                "  - model: 视觉模型名称 (如 gpt-4o, claude-3-opus 等)\n\n"
                "配置文件填写后重启机器人即可启用图片分析。\n\n"
                "当前仅提供图片基础信息。"
            )

        if prompt is None:
            prompt = "请详细描述这张图片的内容。如果有文字，请识别并提取文字内容。使用中文回复。"

        # Read and encode image
        try:
            if not os.path.exists(image_path):
                return f"[多模态] 图片文件不存在: {image_path}"

            with open(image_path, "rb") as f:
                image_data = f.read()

            # Detect MIME type from extension
            ext = os.path.splitext(image_path)[1].lower()
            mime_map = {
                ".png": "image/png",
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".gif": "image/gif",
                ".bmp": "image/bmp",
                ".webp": "image/webp",
            }
            mime_type = mime_map.get(ext, "image/png")

            encoded = base64.b64encode(image_data).decode("utf-8")
            data_uri = f"data:{mime_type};base64,{encoded}"

        except IOError as e:
            return f"[多模态] 无法读取图片文件: {e}"
        except Exception as e:
            return f"[多模态] 编码图片时出错: {e}"

        # Build request
        api_base = self._config["api_base"]
        model = self._config.get("model", "")
        max_tokens = self._config.get("max_tokens", 2048)
        temperature = self._config.get("temperature", 0.7)

        if not model:
            return (
                "[多模态] 未指定 model。请在 QQBot/config/models_settings.json "
                "的 MULTIMODAL_MODEL 部分填写视觉模型名称 (如 gpt-4o, claude-3-opus 等)。"
            )

        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_uri}},
                ],
            }
        ]

        data = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

        headers = {
            "Authorization": f"Bearer {self._config['api_key']}",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(
                    f"{api_base}/chat/completions",
                    headers=headers,
                    json=data,
                    timeout=timeout,
                )
                response.raise_for_status()
                result = response.json()
                message = result["choices"][0]["message"]
                content = message.get("content", "") or "[多模态] API 返回了空内容。"

                # Include reasoning_content if present (some models return it)
                reasoning = message.get("reasoning_content", "")
                if reasoning:
                    content = f"[思考]\n{reasoning}\n\n[回复]\n{content}"

                return content

            except httpx.ConnectTimeout:
                return f"[多模态] 连接超时 ({timeout}秒)。请检查 api_base 地址和网络连通性。"
            except httpx.ReadTimeout:
                return f"[多模态] 响应超时 ({timeout}秒)。图片可能过大，请尝试压缩后重试。"
            except httpx.HTTPStatusError as e:
                return (
                    f"[多模态] API HTTP 错误 ({e.response.status_code}): "
                    f"{e.response.text[:300]}"
                )
            except httpx.InvalidURL:
                return (
                    f"[多模态] 无效的 API 地址: {api_base}\n"
                    "请检查 QQBot/config/models_settings.json 中 MULTIMODAL_MODEL 的 api_base 配置。"
                )
            except Exception as e:
                return f"[多模态] 调用 API 时出错: {str(e)}"


# Global singleton
try:
    multimodal_client = MultimodalClient()
except Exception:
    multimodal_client = MultimodalClient()

"""
Multimodal LLM Client — Vision and audio understanding backend.

Supports OpenAI-compatible vision API format (GPT-4V, Claude Vision,
local vLLM with vision models, etc.) and input_audio format for
voice/audio analysis.

Config is stored in models_settings.json (MULTIMODAL_MODEL + AUDIO_MODEL).
When config is empty/invalid, the client degrades gracefully.
"""

import base64
import json
import os
import subprocess
from typing import Dict, Optional

import httpx


class MultimodalClient:
    """Async HTTP client for multimodal LLM (image + audio understanding).

    Uses OpenAI-compatible vision API and input_audio API formats.
    Reads API credentials from models_settings.json.
    """

    def __init__(self, config_path: str = None):
        if config_path is None:
            _project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            models_path = os.path.join(_project_dir, "config", "models_settings.json")
            legacy_path = os.path.join(_project_dir, "config", "multimodal.json")
            if os.path.exists(models_path):
                config_path = models_path
            else:
                config_path = legacy_path
        self._config_path = config_path
        self._config = None
        self._audio_config = None
        self._load_config()

    def _load_config(self):
        """Load multimodal config from JSON file. Never raises.

        Extracts MULTIMODAL_MODEL and AUDIO_MODEL sections from
        models_settings.json. Falls back cleanly if config is
        missing or malformed.
        """
        try:
            if not os.path.exists(self._config_path):
                self._config = None
                self._audio_config = None
                return
            with open(self._config_path, "r", encoding="utf-8") as f:
                raw_config = json.load(f)

            if "MULTIMODAL_MODEL" in raw_config:
                self._config = raw_config["MULTIMODAL_MODEL"]
            else:
                self._config = raw_config

            if "AUDIO_MODEL" in raw_config:
                self._audio_config = raw_config["AUDIO_MODEL"]
            else:
                self._audio_config = None
        except (json.JSONDecodeError, IOError, OSError):
            self._config = None
            self._audio_config = None

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


    # ── Audio Availability ───────────────────────────────────────────

    def is_audio_available(self) -> bool:
        """Return True if audio model is properly configured.

        Checks AUDIO_MODEL first, falls back to MULTIMODAL_MODEL.
        """
        config = self._audio_config or self._config
        if not config:
            return False
        api_key = config.get("api_key", "")
        api_base = config.get("api_base", "")
        return bool(api_key and api_base)

    # ── Audio Format Conversion ──────────────────────────────────────

    @staticmethod
    def _convert_audio_format(input_path: str) -> str:
        """Convert audio to a widely-supported format using ffmpeg.

        QQ voice messages use .amr or .silk codecs which most multimodal
        models don't support. This converts them to 16kHz mono WAV.

        Returns path to converted file, or original path if conversion
        is not needed or fails.
        """
        ext = os.path.splitext(input_path)[1].lower()
        widely_supported = {".wav", ".mp3", ".m4a", ".ogg", ".flac", ".aac"}

        if ext in widely_supported:
            return input_path

        output_path = os.path.splitext(input_path)[0] + ".wav"
        try:
            result = subprocess.run(
                [
                    "ffmpeg", "-y", "-i", input_path,
                    "-acodec", "pcm_s16le",
                    "-ar", "16000", "-ac", "1",
                    output_path,
                ],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode == 0 and os.path.exists(output_path):
                return output_path
            return input_path
        except Exception:
            return input_path

    # ── Audio Metadata ───────────────────────────────────────────────

    @staticmethod
    def _get_audio_metadata(file_path: str) -> dict:
        """Extract basic audio metadata using ffprobe."""
        try:
            result = subprocess.run(
                [
                    "ffprobe", "-v", "quiet", "-print_format", "json",
                    "-show_format", "-show_streams", file_path,
                ],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                data = json.loads(result.stdout)
                fmt = data.get("format", {})
                streams = data.get("streams", [])
                audio_stream = next(
                    (s for s in streams if s.get("codec_type") == "audio"), {}
                )
                return {
                    "duration_seconds": float(fmt.get("duration", 0)),
                    "sample_rate": int(audio_stream.get("sample_rate", 0)),
                    "channels": int(audio_stream.get("channels", 0)),
                    "codec": audio_stream.get("codec_name", "unknown"),
                    "bit_rate": int(fmt.get("bit_rate", 0)),
                    "file_size": int(fmt.get("size", 0)),
                    "error": None,
                }
        except Exception:
            pass

        try:
            return {"file_size": os.path.getsize(file_path), "error": None}
        except Exception as e:
            return {"error": str(e)}

    # ── Audio Analysis ───────────────────────────────────────────────

    async def analyze_audio(
        self,
        audio_path: str,
        prompt: str = None,
        timeout: float = 120.0,
    ) -> str:
        """Analyze an audio file using the multimodal LLM.

        Captures: speech-to-text transcription, tone/emotion, speaker
        characteristics, background sounds, and environmental context.

        Uses AUDIO_MODEL from config if available, falls back to
        MULTIMODAL_MODEL (if that model also supports audio).

        Args:
            audio_path: Absolute path to the audio file.
            prompt: Custom analysis prompt. Default: comprehensive
                    Chinese instruction for full analysis.
            timeout: Request timeout in seconds (longer for audio).

        Returns:
            Analysis text, or error message if unavailable/failed.
        """
        audio_config = self._audio_config or self._config
        if not audio_config:
            return self._build_audio_not_configured()

        api_base = audio_config.get("api_base", "")
        model = audio_config.get("model", "")
        api_key = audio_config.get("api_key", "")

        if not all([api_base, model, api_key]):
            return self._build_audio_not_configured()

        if prompt is None:
            prompt = (
                "请全面分析这段音频。包括：\n"
                "1) 语音转文字：准确转录所有说话内容\n"
                "2) 语气和情绪：说话人的情感状态（平静/兴奋/愤怒/悲伤等）\n"
                "3) 声线特征：性别、年龄估计、声音特质\n"
                "4) 背景声音：环境音、音乐、噪音等\n"
                "5) 整体场景描述\n"
                "使用中文回复。"
            )

        if not os.path.exists(audio_path):
            return f"[音频分析] 文件不存在: {audio_path}"

        # Convert audio format if needed (amr/silk → wav)
        converted_path = self._convert_audio_format(audio_path)

        try:
            with open(converted_path, "rb") as f:
                audio_data = f.read()

            ext = os.path.splitext(converted_path)[1].lower().lstrip(".")
            format_map = {
                "wav": "wav", "mp3": "mp3", "m4a": "mp4",
                "ogg": "ogg", "flac": "flac", "aac": "aac",
                "amr": "amr", "opus": "opus",
            }
            audio_format = format_map.get(ext, "wav")

            encoded = base64.b64encode(audio_data).decode("utf-8")

            # Clean up converted file
            if converted_path != audio_path:
                try:
                    os.remove(converted_path)
                except Exception:
                    pass

        except IOError as e:
            return f"[音频分析] 无法读取音频文件: {e}"
        except Exception as e:
            return f"[音频分析] 编码音频时出错: {e}"

        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "input_audio",
                        "input_audio": {
                            "data": encoded,
                            "format": audio_format,
                        },
                    },
                ],
            }
        ]

        max_tokens = audio_config.get("max_tokens", 2048)
        temperature = audio_config.get("temperature", 0.7)

        data = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

        headers = {
            "Authorization": f"Bearer {api_key}",
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
                content = message.get("content", "") or "[音频分析] API 返回了空内容。"

                reasoning = message.get("reasoning_content", "")
                if reasoning:
                    content = f"[思考]\n{reasoning}\n\n[回复]\n{content}"

                return content

            except httpx.ConnectTimeout:
                return f"[音频分析] 连接超时 ({timeout}秒)。音频可能过大，请压缩后重试。"
            except httpx.ReadTimeout:
                return f"[音频分析] 响应超时 ({timeout}秒)。音频可能过长，请缩短后重试。"
            except httpx.HTTPStatusError as e:
                error_text = e.response.text[:300]
                return (
                    f"[音频分析] API HTTP 错误 ({e.response.status_code}): "
                    f"{error_text}"
                )
            except httpx.InvalidURL:
                return (
                    f"[音频分析] 无效的 API 地址: {api_base}\n"
                    "请检查 QQBot/config/models_settings.json 中 AUDIO_MODEL 的 api_base 配置。"
                )
            except Exception as e:
                return f"[音频分析] 调用 API 时出错: {str(e)}"

    def _build_audio_not_configured(self) -> str:
        """Return setup instructions for audio analysis."""
        return (
            "[音频分析] 音频分析功能未配置。请联系管理员编辑:\n"
            "  QQBot/config/models_settings.json 的 AUDIO_MODEL 部分\n\n"
            "需要填写:\n"
            "  - api_key: 支持音频的多模态 LLM API 密钥\n"
            "  - api_base: API 端点地址\n"
            "  - model: 支持音频的模型名称 (如 gpt-4o-audio-preview)\n\n"
            "配置文件填写后重启机器人即可启用音频分析。\n"
            "如果不配置 AUDIO_MODEL，会尝试使用 MULTIMODAL_MODEL（如果该模型支持音频）。"
        )


# Global singleton
try:
    multimodal_client = MultimodalClient()
except Exception:
    multimodal_client = MultimodalClient()

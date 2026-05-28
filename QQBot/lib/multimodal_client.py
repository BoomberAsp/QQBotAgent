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
import wave
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
        """Convert audio to 16kHz mono WAV (PCM S16LE).

        QQ voice messages use SILK_V3 codec (misnamed as .amr by NapCat).
        SILK is decoded via pilk, then all audio goes through ffmpeg to
        guarantee PCM S16LE 16kHz mono.

        Returns path to converted WAV file.
        Raises RuntimeError if conversion fails.
        """
        work_path = input_path

        # ── SILK_V3 detection (QQ voice, misnamed .amr) ──────────
        try:
            with open(input_path, "rb") as f:
                header = f.read(16)
            if b"#!SILK_V3" in header:
                import pilk
                silk_wav = os.path.splitext(input_path)[0] + "_silk.wav"
                pilk.silk_to_wav(input_path, silk_wav, rate=16000)
                if os.path.exists(silk_wav) and os.path.getsize(silk_wav) > 0:
                    work_path = silk_wav
                else:
                    raise RuntimeError("pilk SILK 解码后输出文件为空")
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"SILK 解码失败: {e}")

        # ── ffmpeg: ensure PCM S16LE 16kHz mono ──────────────────
        output_path = os.path.splitext(input_path)[0] + "_pcm.wav"
        try:
            result = subprocess.run(
                [
                    "ffmpeg", "-y", "-i", work_path,
                    "-acodec", "pcm_s16le",
                    "-ar", "16000", "-ac", "1",
                    output_path,
                ],
                capture_output=True, text=True, timeout=30,
            )
            # Clean up intermediate SILK-decoded WAV
            if work_path != input_path and work_path != output_path:
                try:
                    os.remove(work_path)
                except Exception:
                    pass
            if result.returncode != 0:
                stderr_tail = result.stderr.strip().split("\n")[-3:]
                raise RuntimeError(
                    f"ffmpeg 转换失败 (exit {result.returncode}): "
                    + "; ".join(stderr_tail)
                )
            if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
                raise RuntimeError("ffmpeg 转换后输出文件为空或不存在")
            return output_path
        except subprocess.TimeoutExpired:
            raise RuntimeError("ffmpeg 转换超时 (30秒)")
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"ffmpeg 转换异常: {e}")

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

    # ── Raw PCM Extraction ────────────────────────────────────────────

    @staticmethod
    def _extract_raw_pcm(wav_path: str) -> bytes:
        """Extract raw PCM samples from a WAV file.

        DashScope native multimodal API expects raw PCM base64
        (no WAV container, no data URI prefix).
        """
        with wave.open(wav_path, "rb") as wf:
            return wf.readframes(wf.getnframes())

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

        # Always convert to 16kHz mono WAV first (QQ uses AMR/SILK)
        try:
            converted_path = self._convert_audio_format(audio_path)
        except RuntimeError as e:
            return f"[音频分析] 音频格式转换失败: {e}"

        # ── Encode audio for API ───────────────────────────────────
        try:
            is_dashscope = "dashscope.aliyuncs.com" in api_base

            # DEBUG: file sizes
            src_size = os.path.getsize(audio_path)
            wav_size = os.path.getsize(converted_path)

            if is_dashscope:
                # DashScope native API: raw PCM base64, no container
                raw_pcm = self._extract_raw_pcm(converted_path)
                audio_b64 = base64.b64encode(raw_pcm).decode("utf-8")
                debug_info = (
                    f"[DEBUG] 源文件: {src_size}B, WAV: {wav_size}B, "
                    f"PCM: {len(raw_pcm)}B, base64: {len(audio_b64)}字符, "
                    f"前80字符: {audio_b64[:80]}"
                )
            else:
                # Generic API: data URI (video_url fallback)
                with open(converted_path, "rb") as f:
                    audio_data = f.read()
                encoded = base64.b64encode(audio_data).decode("utf-8")
                data_uri = f"data:audio/wav;base64,{encoded}"
                debug_info = (
                    f"[DEBUG] 源文件: {src_size}B, WAV: {wav_size}B, "
                    f"data_uri长度: {len(data_uri)}, "
                    f"前100字符: {data_uri[:100]}"
                )

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

        max_tokens = audio_config.get("max_tokens", 2048)
        temperature = audio_config.get("temperature", 0.7)

        if is_dashscope:
            # ── DashScope native multimodal API ────────────────────
            # Uses input.audios with raw PCM base64 (no data URI).
            # Compatible-mode /chat/completions does NOT support audio.
            messages = [
                {
                    "role": "user",
                    "content": [{"text": prompt}],
                }
            ]

            request_body = {
                "model": model,
                "input": {
                    "messages": messages,
                    "audios": [audio_b64],
                },
                "parameters": {
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                },
            }

            endpoint = "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"

            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }

            async with httpx.AsyncClient() as client:
                try:
                    response = await client.post(
                        endpoint,
                        headers=headers,
                        json=request_body,
                        timeout=timeout,
                    )
                    response.raise_for_status()
                    result = response.json()
                    output = result.get("output", {})
                    choices = output.get("choices", [])
                    if not choices:
                        return (
                            f"{debug_info}\n"
                            f"[音频分析] API 返回了空结果: "
                            f"{json.dumps(result, ensure_ascii=False)[:500]}"
                        )
                    message = choices[0].get("message", {})
                    raw_content = message.get("content", "")

                    if isinstance(raw_content, list):
                        text_parts = []
                        for block in raw_content:
                            if isinstance(block, dict) and "text" in block:
                                text_parts.append(block["text"])
                        if text_parts:
                            return debug_info + "\n\n" + "\n".join(text_parts)
                        return f"{debug_info}\n[音频分析] 返回非文本内容: {json.dumps(raw_content, ensure_ascii=False)[:300]}"
                    elif isinstance(raw_content, str) and raw_content:
                        return debug_info + "\n\n" + raw_content
                    else:
                        return f"{debug_info}\n[音频分析] 无法识别的格式: {str(raw_content)[:300]}"

                except httpx.ConnectTimeout:
                    return f"{debug_info}\n[音频分析] 连接超时 ({timeout}秒)"
                except httpx.ReadTimeout:
                    return f"{debug_info}\n[音频分析] 响应超时 ({timeout}秒)"
                except httpx.HTTPStatusError as e:
                    return f"{debug_info}\n[音频分析] API HTTP {e.response.status_code}: {e.response.text[:300]}"
                except Exception as e:
                    return f"{debug_info}\n[音频分析] 调用异常: {str(e)}"

        else:
            # ── Generic OpenAI-compatible API ──────────────────────
            # Uses video_url as fallback since chat/completions lacks
            # a standard audio content type.
            messages = [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "video_url", "video_url": {"url": data_uri}},
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

                    return debug_info + "\n\n" + content

                except httpx.ConnectTimeout:
                    return f"{debug_info}\n[音频分析] 连接超时 ({timeout}秒)"
                except httpx.ReadTimeout:
                    return f"{debug_info}\n[音频分析] 响应超时 ({timeout}秒)"
                except httpx.HTTPStatusError as e:
                    return f"{debug_info}\n[音频分析] API HTTP {e.response.status_code}: {e.response.text[:300]}"
                except httpx.InvalidURL:
                    return f"{debug_info}\n[音频分析] 无效 API 地址: {api_base}"
                except Exception as e:
                    return f"{debug_info}\n[音频分析] 调用异常: {str(e)}"

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

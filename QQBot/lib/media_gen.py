"""Shared media-generation runner — DashScope qwen-image + qwen-audio-tts.

Bond-System-Design.md §7.3（P4）：与 lib/browser_fetch.py 同款共享运行器
约定——进程级并发闸 + 硬超时 + 一次重试 + 任何失败优雅返回 None（永不
把异常抛给调用方）。计费敏感：图像按张计费、TTS-plus 按字符计费，所以
运行器保持"哑"——门控/配额/去重全部由调用方负责（agent/turn.py 的
bond 三重门 + agent/asset_memory.py 的 prompt_hash 去重）。

端点（DashScope 原生格式，2026-10 验证）：
  * 图像  POST {IMAGE_GEN_MODEL.api_base}
          = /api/v1/services/aigc/multimodal-generation/generation
          文生图与带参考图的图像编辑共用此端点（参考图 = 视觉一致性
          杠杆，§7.4）；同步返回，output.choices[0].message.content
          里带 {"image": url}，需立即下载落盘。
  * TTS   POST {TTS_MODEL.api_base}
          = /api/v1/services/audio/tts/SpeechSynthesizer
          body {"model", "input": {"text","voice","format","sample_rate"}}；
          响应 output.audio.url 有效期仅 24h —— 必须立即下载落盘。

配置读取：config/models_settings.json 的 IMAGE_GEN_MODEL / TTS_MODEL 段
（每次调用现读，兼容面板热重载；与 multimodal_client.get_section 同源）。

本模块不依赖 nonebot；httpx 之外无三方依赖，可离线单测（mock httpx）。
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import mimetypes
import os
import sys
from typing import Any, Dict, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

_PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SETTINGS_PATH = os.path.join(_PROJECT_DIR, "config", "models_settings.json")

# 进程级并发闸（lazily created，绑定运行中的 event loop——browser_fetch 同款）
_IMAGE_SEM: asyncio.Semaphore | None = None
_TTS_SEM: asyncio.Semaphore | None = None

# 下载安全上限（防异常响应撑爆磁盘）
_MAX_IMAGE_BYTES = 20 * 1024 * 1024
_MAX_AUDIO_BYTES = 10 * 1024 * 1024

# TTS 文本硬上限（plus 按字符计费；配置 media_tts_max_chars 可再收紧，
# 这里兜底防配置缺失）
_TTS_HARD_CHAR_LIMIT = 600


def _log(msg: str) -> None:
    print(f"[MediaGen] {msg}", file=sys.stderr, flush=True)


def _load_section(name: str) -> Optional[dict]:
    """Fresh-read a models_settings.json section. Never raises."""
    try:
        with open(_SETTINGS_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        section = raw.get(name)
        return section if isinstance(section, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _configured(section: Optional[dict]) -> bool:
    return bool(section and section.get("api_key") and section.get("api_base")
                and section.get("model"))


def is_image_available() -> bool:
    return _configured(_load_section("IMAGE_GEN_MODEL"))


def is_tts_available() -> bool:
    return _configured(_load_section("TTS_MODEL"))


def get_voice_pool() -> Tuple[list, str]:
    """(voice_pool, default_voice) from the TTS section — 角色 voice_id
    确定性分配的池子（character_registry.get_voice_id 用）。"""
    section = _load_section("TTS_MODEL") or {}
    pool = section.get("voice_pool") or []
    if not isinstance(pool, list):
        pool = []
    pool = [str(v).strip() for v in pool if str(v).strip()]
    default = str(section.get("default_voice") or "").strip()
    return pool, default


def get_model_name(kind: str) -> str:
    """Configured model name for "image" | "voice" (asset card 的 model 字段)。"""
    section = _load_section(
        "IMAGE_GEN_MODEL" if kind == "image" else "TTS_MODEL"
    ) or {}
    return str(section.get("model") or "")


def _sem(kind: str) -> asyncio.Semaphore:
    global _IMAGE_SEM, _TTS_SEM
    if kind == "image":
        if _IMAGE_SEM is None:
            section = _load_section("IMAGE_GEN_MODEL") or {}
            try:
                cap = max(1, int(section.get("max_concurrency", 2)))
            except (TypeError, ValueError):
                cap = 2
            _IMAGE_SEM = asyncio.Semaphore(cap)
        return _IMAGE_SEM
    if _TTS_SEM is None:
        section = _load_section("TTS_MODEL") or {}
        try:
            cap = max(1, int(section.get("max_concurrency", 2)))
        except (TypeError, ValueError):
            cap = 2
        _TTS_SEM = asyncio.Semaphore(cap)
    return _TTS_SEM


def _record_usage(section: dict, result: dict, purpose: str, extra: Dict[str, Any]) -> None:
    """Best-effort cost tracking into the shared token ledger. Never raises."""
    try:
        from .token_ledger import token_ledger
        if token_ledger is None:
            return
        usage = (result or {}).get("usage") or {}
        token_ledger.record(
            model=section.get("model", ""), purpose=purpose,
            usage={"input_tokens": int(usage.get("input_tokens") or 0),
                   "output_tokens": int(usage.get("output_tokens") or 0)},
            extra={**extra, **{k: v for k, v in usage.items()
                               if isinstance(v, (int, float))}},
        )
    except Exception:
        pass


async def _atomic_download(url: str, out_path: str, timeout: float,
                           max_bytes: int) -> bool:
    """Download url → out_path (tmp + os.replace). True on success."""
    if not url:
        return False
    tmp = out_path + ".part"
    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            async with client.stream("GET", url, timeout=timeout) as resp:
                resp.raise_for_status()
                total = 0
                os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
                with open(tmp, "wb") as f:
                    async for chunk in resp.aiter_bytes(65536):
                        total += len(chunk)
                        if total > max_bytes:
                            _log(f"download exceeded {max_bytes}B cap: {url[:80]}")
                            return False
                        f.write(chunk)
        if total <= 0:
            return False
        os.replace(tmp, out_path)
        return True
    except Exception as e:  # noqa: BLE001
        _log(f"download failed ({type(e).__name__}: {e}): {url[:80]}")
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


def _guess_ext(url: str, content_type: str, fallback: str) -> str:
    for source in (content_type, url.split("?", 1)[0]):
        ext = mimetypes.guess_extension(source.split(";")[0].strip())
        if ext:
            return ext
    return fallback


# ── 图像生成（qwen-image，同步 multimodal-generation 端点）────────────


async def generate_image(
    prompt: str,
    *,
    out_path: str,
    reference_image_path: Optional[str] = None,
    negative_prompt: str = "",
    size: str = "",
    timeout: Optional[float] = None,
) -> Optional[str]:
    """Generate one image; download it to out_path; return the path or None.

    reference_image_path 存在时走图像编辑（同端点，content 里附
    {"image": data_uri}）——角色参考立绘的面部一致性通路（§7.4）。
    失败（未配置/超时/审核拒绝/下载失败）一律返回 None，不抛异常。
    超时/网络/5xx 重试一次；4xx（审核、参数错）不重试。
    """
    section = _load_section("IMAGE_GEN_MODEL")
    if not _configured(section):
        _log("image generation not configured (IMAGE_GEN_MODEL)")
        return None
    prompt = (prompt or "").strip()
    if not prompt:
        return None
    if timeout is None:
        timeout = float(section.get("timeout", 60))
    max_retries = int(section.get("max_retries", 1))

    content: list = []
    if reference_image_path and os.path.isfile(reference_image_path):
        try:
            with open(reference_image_path, "rb") as f:
                encoded = base64.b64encode(f.read()).decode("utf-8")
            mime = mimetypes.guess_type(reference_image_path)[0] or "image/png"
            content.append({"image": f"data:{mime};base64,{encoded}"})
        except OSError as e:
            _log(f"cannot read reference image ({e}); falling back to T2I")
    content.append({"text": prompt[:4000]})

    parameters: Dict[str, Any] = {"watermark": False}
    if negative_prompt:
        parameters["negative_prompt"] = negative_prompt[:500]
    if size:
        parameters["size"] = size
    body = {
        "model": section.get("model"),
        "input": {"messages": [{"role": "user", "content": content}]},
        "parameters": parameters,
    }
    headers = {
        "Authorization": f"Bearer {section.get('api_key')}",
        "Content-Type": "application/json",
    }

    async with _sem("image"):
        for attempt in range(max_retries + 1):
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.post(
                        section.get("api_base"), headers=headers,
                        json=body, timeout=timeout,
                    )
                if resp.status_code >= 500 and attempt < max_retries:
                    _log(f"image gen HTTP {resp.status_code}; retrying once")
                    await asyncio.sleep(1.0)
                    continue
                if resp.status_code >= 400:
                    # 4xx = 审核拒绝/参数错/配额——不重试（重试只会重复计费）
                    _log(f"image gen rejected HTTP {resp.status_code}: "
                         f"{resp.text[:200]}")
                    return None
                result = resp.json()
                _record_usage(section, result, "image_gen", {"kind": "image"})
                image_url = _extract_image_url(result)
                if not image_url:
                    _log(f"image gen returned no url: "
                         f"{json.dumps(result, ensure_ascii=False)[:300]}")
                    return None
                if await _atomic_download(image_url, out_path, timeout=60.0,
                                          max_bytes=_MAX_IMAGE_BYTES):
                    return out_path
                return None
            except (httpx.TimeoutException, httpx.NetworkError) as e:
                if attempt < max_retries:
                    _log(f"image gen {type(e).__name__}; retrying once")
                    await asyncio.sleep(1.0)
                    continue
                _log(f"image gen failed after retry: {type(e).__name__}: {e}")
                return None
            except Exception as e:  # noqa: BLE001
                _log(f"image gen error: {type(e).__name__}: {e}")
                return None
    return None


def _extract_image_url(result: dict) -> str:
    """output.choices[0].message.content 里找 {"image": url}（列表或字符串
    content 两种形态都兜住）。"""
    try:
        choices = (result or {}).get("output", {}).get("choices", [])
        if not choices:
            return ""
        content = choices[0].get("message", {}).get("content", "")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("image"):
                    return str(block["image"])
        elif isinstance(content, str) and content.startswith("http"):
            return content
    except Exception:  # noqa: BLE001
        pass
    return ""


# ── 语音合成（qwen-audio-tts，SpeechSynthesizer 端点）────────────────


async def generate_speech(
    text: str,
    voice: str,
    *,
    out_path: str,
    max_chars: int = 300,
    timeout: Optional[float] = None,
) -> Optional[str]:
    """Synthesize speech; download the 24h-expiring url; return path or None.

    text 截断到 max_chars（plus 按字符计费——调用方另可配置更紧的
    media_tts_max_chars）。voice 必须是当前模型支持的音色 ID（系统音色
    或复刻 voice_id；不能跨模型混用）。
    """
    section = _load_section("TTS_MODEL")
    if not _configured(section):
        _log("tts not configured (TTS_MODEL)")
        return None
    text = (text or "").strip()
    if not text:
        return None
    voice = (voice or section.get("default_voice") or "").strip()
    if not voice:
        _log("tts voice missing (no voice arg, no default_voice configured)")
        return None
    limit = min(int(max_chars or 300), _TTS_HARD_CHAR_LIMIT)
    text = text[:limit]
    if timeout is None:
        timeout = float(section.get("timeout", 30))
    max_retries = int(section.get("max_retries", 1))

    body = {
        "model": section.get("model"),
        "input": {
            "text": text,
            "voice": voice,
            "format": section.get("format", "mp3"),
            "sample_rate": int(section.get("sample_rate", 22050)),
        },
    }
    headers = {
        "Authorization": f"Bearer {section.get('api_key')}",
        "Content-Type": "application/json",
    }

    async with _sem("tts"):
        for attempt in range(max_retries + 1):
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.post(
                        section.get("api_base"), headers=headers,
                        json=body, timeout=timeout,
                    )
                if resp.status_code >= 500 and attempt < max_retries:
                    _log(f"tts HTTP {resp.status_code}; retrying once")
                    await asyncio.sleep(1.0)
                    continue
                if resp.status_code >= 400:
                    _log(f"tts rejected HTTP {resp.status_code}: "
                         f"{resp.text[:200]}")
                    return None
                result = resp.json()
                _record_usage(section, result, "tts", {"kind": "voice"})
                audio_url = ((result or {}).get("output", {})
                             .get("audio", {}).get("url", ""))
                if not audio_url:
                    _log(f"tts returned no audio url: "
                         f"{json.dumps(result, ensure_ascii=False)[:300]}")
                    return None
                # url 24h 过期 —— 立即下载落盘，只存本地路径
                if await _atomic_download(audio_url, out_path, timeout=60.0,
                                          max_bytes=_MAX_AUDIO_BYTES):
                    return out_path
                return None
            except (httpx.TimeoutException, httpx.NetworkError) as e:
                if attempt < max_retries:
                    _log(f"tts {type(e).__name__}; retrying once")
                    await asyncio.sleep(1.0)
                    continue
                _log(f"tts failed after retry: {type(e).__name__}: {e}")
                return None
            except Exception as e:  # noqa: BLE001
                _log(f"tts error: {type(e).__name__}: {e}")
                return None
    return None


# ── 生成侧内容过滤（前置，§8.4：UGC 卡可能诱导擦边 prompt）──────────

_REVIEW_SYSTEM = (
    "你是内容安全审查员。给你一段将要用于 AI 图像生成或语音合成的文本，"
    "请判断其是否包含违规内容：色情或性化描写（尤其涉及未成年人）、"
    "极端血腥暴力、真实人物影射诋毁、仇恨言论、政治敏感煽动、教唆现实违法。"
    "虚构角色的一般性亲密/战斗/情感描写不算违规。"
    "只输出一行：APPROVE 或 REJECT: <简要理由>。"
)

# 与 delegation._LLM_ERROR_PREFIXES 同源（chat_completion 把 API 层错误
# 当普通字符串返回，审查层须识别并 fail-open）
_LLM_ERROR_PREFIXES = (
    "思考超时", "响应超时", "调用DeepSeek API时出错", "API HTTP错误",
)


async def review_prompt(text: str, review_client, *,
                        timeout: float = 20.0) -> Tuple[bool, str]:
    """Pre-generation content filter. Returns (ok, reason).

    显式 REJECT = fail-closed（拒绝生成）；review_client 缺失或基础设施
    错误 = fail-open + log（与 creation_tools.review_card 同一取舍：
    API 抖动不能把正常玩法卡死；平台侧还有第二道审核兜底）。
    """
    text = (text or "").strip()
    if not text:
        return False, "内容为空"
    if review_client is None:
        return True, ""
    try:
        raw = await review_client.chat_completion(
            message=f"待审查的生成文本：\n\n{text[:2000]}",
            history=[{"role": "system", "content": _REVIEW_SYSTEM}],
            timeout_set=timeout,
            purpose="media_review",
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("media review call failed (fail-open): %s", e)
        return True, ""
    verdict = (raw or "").strip()
    if not verdict or verdict.startswith(_LLM_ERROR_PREFIXES):
        logger.warning("media review infra error (fail-open): %s", verdict[:80])
        return True, ""
    first = verdict.split("\n", 1)[0].strip()
    if first.upper().startswith("REJECT"):
        reason = first.split(":", 1)[1].strip() if ":" in first else first
        return False, reason or "内容未通过安全审查"
    return True, ""

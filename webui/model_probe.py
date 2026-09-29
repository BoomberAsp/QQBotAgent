"""
Model API availability probe for the panel's structured model-config form.

Sends a minimal OpenAI-compatible ``chat/completions`` request (max_tokens=8)
to verify that api_key / api_base / model actually work *before* the config
is written to disk.  URL joining matches deepseek_client.py exactly
(``f"{api_base}/chat/completions"``, no rstrip) so the probe validates the
same endpoint the bot will call.

Graded verdict: text models (REASONING/FLASH/MULTIMODAL) must return 200.
AUDIO/OCR models may legitimately reject a plain-text request (omni models
often require streaming; OCR models require image input), so a 400/422 from
them still proves the credentials and endpoint are valid → ok_with_warning.
"""

import time

import httpx

# Sections whose models may reject plain-text probes even when fully valid.
_LENIENT_SECTIONS = {"AUDIO_MODEL", "OCR_MODEL"}

PROBE_TIMEOUT = 15.0
PROBE_MAX_TOKENS = 8


async def probe_model(api_key: str, api_base: str, model: str,
                      *, section: str = "", timeout: float = PROBE_TIMEOUT) -> dict:
    """Probe one model section. Never raises.

    Returns ``{"ok": bool, "status": str, "elapsed_ms": int, "detail": str}``
    where status is one of:
      ok | ok_with_warning | auth_failed | unreachable | bad_path |
      rejected | missing_config
    """
    api_key = (api_key or "").strip()
    api_base = (api_base or "").strip()
    model = (model or "").strip()
    if not api_key or not api_base or not model:
        return {"ok": False, "status": "missing_config", "elapsed_ms": 0,
                "detail": "api_key / api_base / model 不能为空"}
    if "***" in api_key:
        # Masked placeholder leaked through (frontend convention broken) —
        # never send it to a real endpoint.
        return {"ok": False, "status": "missing_config", "elapsed_ms": 0,
                "detail": "api_key 仍是脱敏占位值，无法验证"}

    url = f"{api_base}/chat/completions"  # same join as deepseek_client.py
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "Hi"}],
        "max_tokens": PROBE_MAX_TOKENS,
        "stream": False,
    }
    headers = {"Authorization": f"Bearer {api_key}",
               "Content-Type": "application/json"}

    started = time.monotonic()
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, headers=headers, json=payload,
                                     timeout=timeout)
        elapsed = int((time.monotonic() - started) * 1000)
        code = resp.status_code
        if code == 200:
            return {"ok": True, "status": "ok", "elapsed_ms": elapsed,
                    "detail": "连接成功"}
        if code in (401, 403):
            return {"ok": False, "status": "auth_failed", "elapsed_ms": elapsed,
                    "detail": f"API Key 无效或权限不足 (HTTP {code})"}
        if code == 404:
            return {"ok": False, "status": "bad_path", "elapsed_ms": elapsed,
                    "detail": "API 路径错误，请检查 api_base (HTTP 404)"}
        if code in (400, 422) and section in _LENIENT_SECTIONS:
            return {"ok": True, "status": "ok_with_warning", "elapsed_ms": elapsed,
                    "detail": f"凭据与端点有效；模型不接受纯文本探测（HTTP {code}，"
                              f"音频/OCR 模型的预期行为）"}
        body = resp.text[:200].replace("\n", " ")
        return {"ok": False, "status": "rejected", "elapsed_ms": elapsed,
                "detail": f"API 拒绝了请求 (HTTP {code}): {body}"}
    except (httpx.ConnectError, httpx.ConnectTimeout) as e:
        elapsed = int((time.monotonic() - started) * 1000)
        return {"ok": False, "status": "unreachable", "elapsed_ms": elapsed,
                "detail": f"无法连接到 API 地址: {type(e).__name__}"}
    except httpx.TimeoutException:
        elapsed = int((time.monotonic() - started) * 1000)
        return {"ok": False, "status": "unreachable", "elapsed_ms": elapsed,
                "detail": f"请求超时（>{timeout:.0f}s），端点可能不可用"}
    except Exception as e:
        elapsed = int((time.monotonic() - started) * 1000)
        return {"ok": False, "status": "unreachable", "elapsed_ms": elapsed,
                "detail": f"探测异常: {type(e).__name__}: {e}"}


def section_needs_probe(new_section: dict, old_section: dict) -> bool:
    """True when api_key/api_base/model changed (after unmask).

    max_tokens/temperature are pure client-side parameters — changing only
    those cannot break API availability, so the probe is skipped.
    """
    for key in ("api_key", "api_base", "model"):
        if str(new_section.get(key, "") or "") != str(old_section.get(key, "") or ""):
            return True
    return False

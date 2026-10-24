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

Media-generation sections (IMAGE_GEN_MODEL / TTS_MODEL, Bond P4) are NOT
chat/completions APIs: their api_base is the full DashScope-native endpoint
URL (…/multimodal-generation/generation, …/tts/SpeechSynthesizer) and they
bill per image / per character — a real generation call is never an
acceptable probe. They get a zero-cost empty-input POST instead: the
platform validates the API key first, so 401/403 = bad credentials while
400/422 (invalid empty parameter) proves key + endpoint are live.
"""

import time

import httpx

# Sections whose models may reject plain-text probes even when fully valid.
_LENIENT_SECTIONS = {"AUDIO_MODEL", "OCR_MODEL"}

# Sections that talk to DashScope-native endpoints (full URL in api_base,
# per-unit billing). Probe with an empty input → guaranteed rejection
# before any billable generation happens.
MEDIA_SECTIONS = {"IMAGE_GEN_MODEL", "TTS_MODEL"}

# Minimal zero-cost bodies: syntactically shaped like lib/media_gen.py's
# real requests but with empty content the API must reject with 4xx.
_MEDIA_PROBE_BODIES = {
    "IMAGE_GEN_MODEL": {"input": {"messages": []}},
    "TTS_MODEL": {"input": {"text": ""}},
}

PROBE_TIMEOUT = 15.0
PROBE_MAX_TOKENS = 8


async def _post(url: str, headers: dict, payload: dict, timeout: float,
                grade) -> dict:
    """POST once and pass (status_code, body_text, elapsed_ms) to ``grade``.

    Shared transport + exception ladder for both probe flavors; never raises.
    """
    started = time.monotonic()
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, headers=headers, json=payload,
                                     timeout=timeout)
        elapsed = int((time.monotonic() - started) * 1000)
        return grade(resp.status_code, resp.text[:200].replace("\n", " "),
                     elapsed)
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


async def probe_model(api_key: str, api_base: str, model: str,
                      *, section: str = "", timeout: float = PROBE_TIMEOUT) -> dict:
    """Probe one model section. Never raises.

    Dispatches by section: chat models → ``{api_base}/chat/completions``
    with a minimal message; media sections (MEDIA_SECTIONS) → api_base
    verbatim with an empty-input body (zero billable generation).

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

    headers = {"Authorization": f"Bearer {api_key}",
               "Content-Type": "application/json"}

    if section in MEDIA_SECTIONS:
        # api_base IS the full endpoint URL (lib/media_gen.py POSTs to it
        # verbatim) — never append /chat/completions here.
        url = api_base
        payload = {"model": model, **_MEDIA_PROBE_BODIES[section]}

        def grade(code, body, elapsed):
            if code in (401, 403):
                return {"ok": False, "status": "auth_failed",
                        "elapsed_ms": elapsed,
                        "detail": f"API Key 无效或权限不足 (HTTP {code})"}
            if code == 404:
                return {"ok": False, "status": "bad_path",
                        "elapsed_ms": elapsed,
                        "detail": "端点 URL 不存在，请检查 api_base 是否为完整"
                                  f"端点地址 (HTTP 404): {body}"}
            if code in (400, 422, 429):
                # Auth passed, the deliberately-empty input got rejected
                # before any generation → credentials + endpoint proven,
                # nothing billed.
                return {"ok": True, "status": "ok_with_warning",
                        "elapsed_ms": elapsed,
                        "detail": f"凭据与端点有效（空参数探测被平台拒绝 "
                                  f"HTTP {code}，未产生生成调用/计费）"}
            if code == 200:
                return {"ok": True, "status": "ok", "elapsed_ms": elapsed,
                        "detail": "连接成功（探测请求被接受，未下载产物）"}
            return {"ok": False, "status": "rejected", "elapsed_ms": elapsed,
                    "detail": f"API 拒绝了请求 (HTTP {code}): {body}"}

        return await _post(url, headers, payload, timeout, grade)

    url = f"{api_base}/chat/completions"  # same join as deepseek_client.py
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "Hi"}],
        "max_tokens": PROBE_MAX_TOKENS,
        "stream": False,
    }

    def grade_chat(code, body, elapsed):
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
            return {"ok": True, "status": "ok_with_warning",
                    "elapsed_ms": elapsed,
                    "detail": f"凭据与端点有效；模型不接受纯文本探测（HTTP {code}，"
                              f"音频/OCR 模型的预期行为）"}
        return {"ok": False, "status": "rejected", "elapsed_ms": elapsed,
                "detail": f"API 拒绝了请求 (HTTP {code}): {body}"}

    return await _post(url, headers, payload, timeout, grade_chat)


def section_needs_probe(new_section: dict, old_section: dict) -> bool:
    """True when api_key/api_base/model changed (after unmask).

    max_tokens/temperature are pure client-side parameters — changing only
    those cannot break API availability, so the probe is skipped.
    """
    for key in ("api_key", "api_base", "model"):
        if str(new_section.get(key, "") or "") != str(old_section.get(key, "") or ""):
            return True
    return False

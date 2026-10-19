"""
Error classification and safe messaging (改进方案 P1-2).

Two rules:

1. Raw exception text is NEVER sent to QQ users or fed back to the LLM.
   Exception strings can carry server paths, env var names, internal API
   URLs, or credential fragments — and the model will copy whatever it
   sees into user-visible replies.
2. Suppressed exceptions stay debuggable: each one is logged with its full
   traceback under a short diagnostic code, and the code is what the
   user/model sees. ``grep 'diag:<code>'`` in the bot log finds the
   original failure.

Usage:
    # Tool authors: raise a classified error with self-authored safe text
    raise ToolError("not_found", "未找到角色「阿米娅」，请检查名称后重试。")

    # Top-level handlers: suppress + log + get the code
    code = log_exception("agent message handling failed", exc)
    await send(f"处理消息时出现内部错误（诊断编号 {code}）。")
"""

from __future__ import annotations

import hashlib
import time
import traceback

try:  # prefer the bot logger; fall back to stdlib for standalone/test use
    from nonebot import logger as _logger
except Exception:  # pragma: no cover
    import logging

    _logger = logging.getLogger("qqbot.errors")


# Error kinds modeled on the improvement plan §P1-2. Safe text for each
# kind is authored here — never interpolated from exception messages.
TOOL_ERROR_MESSAGES = {
    "invalid_args": "参数不符合该工具要求，请检查字段名与取值后重试。",
    "not_found": "未找到符合条件的记录。",
    "unavailable": "该数据源当前不可用，请稍后重试或改用其他方式。",
    "internal": "工具执行失败，已记录诊断编号 {code}。",
}


class ToolError(Exception):
    """Tool-facing error carrying a safe, self-authored message.

    Args:
        kind: one of ``TOOL_ERROR_MESSAGES`` keys ('invalid_args',
            'not_found', 'unavailable', 'internal'). Unknown kinds are
            downgraded to 'internal'.
        safe_message: optional custom text shown to the model/user. It
            MUST be authored by the tool writer — never built from raw
            exception strings, file paths, or other uncontrolled content.
    """

    def __init__(self, kind: str = "internal", safe_message: str | None = None):
        if kind not in TOOL_ERROR_MESSAGES:
            kind = "internal"
        self.kind = kind
        self.safe_message = safe_message or TOOL_ERROR_MESSAGES[kind]
        super().__init__(self.safe_message)


def make_diagnostic_code(exc: BaseException) -> str:
    """Return an 8-hex-char code uniquely identifying this failure in logs."""
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    return hashlib.sha256(f"{time.time_ns()}|{tb}".encode("utf-8")).hexdigest()[:8]


def log_exception(context: str, exc: BaseException) -> str:
    """Log the full traceback under a fresh diagnostic code; return the code.

    This is the ONLY place the raw exception text may appear: the bot log.
    """
    code = make_diagnostic_code(exc)
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    _logger.error(f"[diag:{code}] {context}: {type(exc).__name__}\n{tb}")
    return code


# Class-name substrings suggesting transient network/datasource problems.
# Matches httpx.ReadTimeout, aiohttp.ClientConnectionError, etc. without
# importing those packages here.
_UNAVAILABLE_NAME_HINTS = ("timeout", "connection", "connect", "network")


def classify_exception(exc: BaseException) -> str:
    """Map an exception to a safe error kind WITHOUT reading its message.

    Returns:
        'unavailable' for timeout/connection failures (retry later makes
        sense), 'internal' for everything else, or the ToolError's own kind.
    """
    if isinstance(exc, ToolError):
        return exc.kind
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return "unavailable"
    name = type(exc).__name__.lower()
    if any(hint in name for hint in _UNAVAILABLE_NAME_HINTS):
        return "unavailable"
    return "internal"

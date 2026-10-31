"""
Log reading for the panel — tail / search / clear across four sources:

    nonebot | napcat | webui   → plain log files
    searxng                    → `docker logs` subprocess

All output passes through the same redaction filters so API keys never
reach the browser.
"""

import os
import re
import subprocess

from . import config

MAX_SCAN_BYTES = 8 * 1024 * 1024   # how far back a single tail may scan

# ── Redaction ─────────────────────────────────────────────────────

_REDACTIONS = [
    # sk-xxxx API keys (OpenAI / DeepSeek / 各类网关)
    (re.compile(r"sk-[A-Za-z0-9_\-]{20,}"), "sk-***"),
    # Authorization: Bearer ...
    (re.compile(r"(?i)Bearer\s+[A-Za-z0-9._\-]+"), "Bearer ***"),
    # SOME_API_KEY=... / api_key: "..."（含 .env 与 JSON 配置回显）
    (re.compile(r"(?i)((?:[a-z0-9_]*api[_-]?key|token|secret)\s*[=:]\s*)[\"']?[A-Za-z0-9._\-]{8,}"),
     r"\1***"),
]


def redact(line: str) -> str:
    for pat, repl in _REDACTIONS:
        line = pat.sub(repl, line)
    return line


def redact_lines(lines: list[str]) -> list[str]:
    return [redact(ln) for ln in lines]


# ── File tail ─────────────────────────────────────────────────────

def _tail_file(path: str, lines: int, before: int = 0) -> tuple[list[str], int]:
    """Return (last `lines` lines skipping the newest `before`, file size)."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return [], 0
    needed = lines + before
    data = b""
    try:
        with open(path, "rb") as f:
            pos = size
            while pos > 0 and data.count(b"\n") <= needed:
                step = min(64 * 1024, pos)
                pos -= step
                f.seek(pos)
                data = f.read(step) + data
                if size - pos >= MAX_SCAN_BYTES:
                    break
    except OSError:
        return [], size
    all_lines = data.decode("utf-8", errors="replace").splitlines()
    if before:
        all_lines = all_lines[:-before] if before < len(all_lines) else []
    return all_lines[-lines:], size


def _tail_docker(container: str, lines: int, before: int = 0) -> list[str]:
    need = lines + before
    try:
        r = subprocess.run(
            ["docker", "logs", "--tail", str(min(need, 5000)), container],
            capture_output=True, text=True, timeout=15,
        )
    except FileNotFoundError:
        return ["[docker 不可用]"]
    except subprocess.TimeoutExpired:
        return ["[docker logs 超时]"]
    out = (r.stdout + r.stderr).splitlines()  # docker logs 混在两个流里
    if before:
        out = out[:-before] if before < len(out) else []
    return out[-lines:]


# ── Public API ────────────────────────────────────────────────────

def read(source: str, lines: int = 200, before: int = 0, q: str = "") -> dict:
    lines = max(1, min(int(lines), 2000))
    before = max(0, int(before))
    sources = config.log_sources()
    if source not in sources:
        return {"error": f"未知日志源: {source}"}
    target = sources[source]

    if target.startswith("docker:"):
        raw = _tail_docker(target.split(":", 1)[1], lines * 4 if q else lines, before)
        size = None
    else:
        # Search scans a bigger window so filtered results still fill the page
        raw, size = _tail_file(target, lines * 4 if q else lines, before)

    if q:
        ql = q.lower()
        raw = [ln for ln in raw if ql in ln.lower()][-lines:]

    return {
        "source": source,
        "lines": redact_lines(raw),
        "count": len(raw),
        "size": size,
    }


def read_new(source: str, offset: int) -> dict:
    """Incremental read for the websocket tail: bytes after `offset`.

    Returns {"lines", "offset"} where offset is the new cursor.
    A shrunk file (rotated/truncated) resets the cursor to 0.
    """
    sources = config.log_sources()
    target = sources.get(source, "")
    if target.startswith("docker:") or not target:
        # Docker has no cheap offset tracking — the ws handler falls back
        # to polling full tails; report nothing here.
        return {"lines": [], "offset": offset}
    try:
        size = os.path.getsize(target)
    except OSError:
        return {"lines": [], "offset": 0}
    if size < offset:
        offset = 0
    if size == offset:
        return {"lines": [], "offset": offset}
    try:
        with open(target, "rb") as f:
            f.seek(offset)
            chunk = f.read(512 * 1024)  # 单次最多推 512KB，防刷屏
        new_offset = offset + len(chunk)
    except OSError:
        return {"lines": [], "offset": offset}
    lines = chunk.decode("utf-8", errors="replace").splitlines()
    return {"lines": redact_lines(lines), "offset": new_offset}


def clear(source: str) -> dict:
    sources = config.log_sources()
    if source not in sources:
        return {"error": f"未知日志源: {source}"}
    target = sources[source]
    if target.startswith("docker:"):
        return {"error": "docker 日志无法清空（可用 docker compose restart 重置）"}
    try:
        with open(target, "w", encoding="utf-8"):
            pass
        return {"ok": True}
    except OSError as e:
        return {"error": str(e)}

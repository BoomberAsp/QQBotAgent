"""
TaskRecord — structured encapsulation of tool-heavy subtasks.

Multi-turn agent flows (gacha, battle speed-check, ...) produce verbose raw
output. When such a subtask completes, that raw output should NOT be flattened
into subsequent context verbatim. Instead it is folded into a compact,
structured "result/state" record; the full detail is persisted to a per-user
task log for later traceability.

The record schema covers the post-task essentials:
  ① subtask goal            ② final result
  ③ key params / constraints / referenced IDs
  ④ success vs failure type  ⑤ unfinished items / follow-up suggestions
  ⑥ index for tracing the original logs (audit log + task log)
"""

import json
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# Per-user task logs live under data/task_log/ (sibling of data/audit/).
_TASK_LOG_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "task_log",
)

# Compact-line truncation so a single folded record stays small in context.
_RESULT_CHARS = 500
_PARAM_VAL_CHARS = 200


def new_task_id() -> str:
    return uuid.uuid4().hex[:12]


def _default_audit_ref() -> str:
    """Point at today's audit log, where every raw tool call/return is stored."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"data/audit/tool_calls_{today}.jsonl"


def _slim_params(params: Optional[dict]) -> dict:
    """Shrink param values so a record never drags large blobs into context."""
    out = {}
    for k, v in (params or {}).items():
        s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
        if len(s) > _PARAM_VAL_CHARS:
            s = s[:_PARAM_VAL_CHARS] + "…"
        out[str(k)] = s
    return out


def build_record(
    goal: str,
    result: str,
    tool: str = "",
    params: Optional[dict] = None,
    refs: Optional[list] = None,
    status: str = "success",
    failure: str = "",
    follow_ups: Optional[list] = None,
    tool_calls: Optional[list] = None,
    audit_ref: str = "",
) -> dict:
    """Build a full TaskRecord dict (the canonical schema)."""
    return {
        "task_id": new_task_id(),
        "ts": time.time(),
        "goal": (goal or "").strip(),
        "tool": tool or "",
        "result": (result or "").strip(),
        "params": _slim_params(params),
        "refs": [str(r) for r in (refs or [])],
        "status": status if status in ("success", "partial", "failed") else "success",
        "failure": (failure or "").strip(),
        "follow_ups": [str(x) for x in (follow_ups or [])],
        "tool_calls": tool_calls or [],
        "audit_ref": audit_ref or _default_audit_ref(),
    }


def append_task_log(user_id: str, record: dict) -> Optional[str]:
    """Append a record to data/task_log/{user_id}.jsonl. Returns path or None."""
    try:
        os.makedirs(_TASK_LOG_DIR, exist_ok=True)
        path = os.path.join(_TASK_LOG_DIR, f"{user_id}.jsonl")
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        return path
    except Exception:
        return None


def build_compact_line(record: dict) -> str:
    """Build the single-line structured summary persisted into session context.

    This is what subsequent turns see instead of the raw verbose output. The
    full result / tool returns remain retrievable via ``audit_ref`` + task log.
    """
    parts = [f"[子任务记录] 目标: {record.get('goal', '')}"]
    if record.get("tool"):
        parts.append(f"工具: {record['tool']}")
    result = record.get("result", "")
    if len(result) > _RESULT_CHARS:
        result = result[:_RESULT_CHARS] + "…"
    parts.append(f"结果: {result}")
    if record.get("params"):
        kv = ", ".join(f"{k}={v}" for k, v in record["params"].items())
        parts.append(f"参数: {kv}")
    if record.get("refs"):
        parts.append("引用: " + "、".join(record["refs"]))
    status = record.get("status", "success")
    if status == "failed":
        parts.append(f"状态: failed({record.get('failure', '') or '未知原因'})")
    elif status != "success":
        parts.append(f"状态: {status}")
    if record.get("follow_ups"):
        parts.append("后续: " + "; ".join(record["follow_ups"]))
    parts.append(f"追溯: {record.get('audit_ref', '')}#{record.get('task_id', '')}")
    return " | ".join(parts)


def build_auto_record(
    user_message: str,
    final_content: str,
    tool_calls: List[dict],
) -> dict:
    """Build a degraded TaskRecord for the auto-compression fallback.

    Used when a turn was tool-heavy and long but the agent did not call
    ``finalize_subtask`` explicitly. The primary tool is heuristically the first
    call; status reflects whether all calls succeeded.
    """
    primary = tool_calls[0].get("name", "") if tool_calls else ""
    params = tool_calls[0].get("args", {}) if tool_calls else {}
    all_ok = all(tc.get("success", True) for tc in tool_calls)
    return build_record(
        goal=(user_message or "")[:200],
        result=final_content or "",
        tool=primary,
        params=params,
        status="success" if all_ok else "partial",
        tool_calls=tool_calls,
    )

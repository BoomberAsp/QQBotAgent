"""
Token Usage Ledger — 全量 LLM 调用的 token 计量总表.

每次 LLM API 调用成功后记录一条明细（JSONL，按日分文件），并增量更新
汇总表 ``totals.json``。四个核心计量维度：

- ``input_tokens``          输入 token 总量
- ``output_tokens``         输出 token 总量
- ``cached_input_tokens``   命中提供商上下文缓存（前缀缓存）的输入 token
- ``uncached_input_tokens`` 未命中缓存、按全价计费的输入 token

汇总按三个维度聚合：全局总计 (``totals``)、按模型 (``by_model``)、
按用途 (``by_purpose``，如 agent_loop / triage / profile / multimodal_*)，
另附按日明细 (``daily``)。

用途：评估系统提示词分层、窗口修剪等缓存优化的实际收益。

存储位置：``QQBot/data/token_usage/``
- ``usage_YYYY-MM-DD.jsonl``  每次调用一行（保留最近 90 天）
- ``totals.json``             增量维护的汇总表

所有写操作都在 try/except 内 —— 计量失败绝不能影响主流程。
"""

import contextvars
import json
import os
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

# 保留最近多少天的明细文件与汇总条目
_RETENTION_DAYS = 90

# ── 请求归属上下文 ────────────────────────────────────────────────
# 由 agent_router 在每个消息 handler 开头（triage 之前）设置，
# 使同一条消息触发的所有 LLM 调用（triage / agent_loop / profile 抽取）
# 都能归属到同一个用户/群。asyncio.create_task 创建的后台任务
# （如 profile 抽取）会自动继承创建时的上下文。
usage_context: contextvars.ContextVar[Dict[str, str]] = (
    contextvars.ContextVar("token_usage_context", default={})
)


def set_usage_context(
    user_id: str = "", group_id: str = "", chat_type: str = "",
) -> None:
    """Set the attribution context for subsequent LLM calls."""
    usage_context.set({
        "user_id": str(user_id or ""),
        "group_id": str(group_id or ""),
        "chat_type": chat_type or "",
    })


# ── usage 解析 ────────────────────────────────────────────────────

def extract_usage(result: dict) -> Dict[str, int]:
    """Parse token usage from an OpenAI-compatible (or dashscope-native)
    API response into the four ledger dimensions.

    Handles:
    - OpenAI format: ``usage.prompt_tokens`` / ``usage.completion_tokens``
      with cache info in ``usage.prompt_tokens_details.cached_tokens``
      (DeepSeek official API, dashscope compatible-mode).
    - dashscope native: ``usage.input_tokens`` / ``usage.output_tokens``,
      cache hit in ``usage.prompt_tokens_details.cached_tokens`` or
      ``usage.prompt_cache_hit_tokens``.

    Missing/partial usage yields zeros — never raises.
    """
    usage = result.get("usage") or {} if isinstance(result, dict) else {}

    def _int(v) -> int:
        try:
            return max(0, int(v))
        except (TypeError, ValueError):
            return 0

    input_tokens = _int(usage.get("prompt_tokens", usage.get("input_tokens")))
    output_tokens = _int(usage.get("completion_tokens", usage.get("output_tokens")))

    details = usage.get("prompt_tokens_details") or {}
    if not isinstance(details, dict):
        details = {}
    cached = _int(details.get("cached_tokens"))
    if not cached:
        cached = _int(usage.get("prompt_cache_hit_tokens"))
    cached = min(cached, input_tokens)

    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cached_input_tokens": cached,
        "uncached_input_tokens": input_tokens - cached,
    }


# ── Ledger ────────────────────────────────────────────────────────

_EMPTY_BUCKET = (
    "requests", "input_tokens", "output_tokens",
    "cached_input_tokens", "uncached_input_tokens",
)


def _empty_bucket() -> Dict[str, int]:
    return {k: 0 for k in _EMPTY_BUCKET}


def _add_bucket(target: Dict[str, int], usage: Dict[str, int]) -> None:
    target["requests"] = target.get("requests", 0) + 1
    for key in ("input_tokens", "output_tokens",
                "cached_input_tokens", "uncached_input_tokens"):
        target[key] = target.get(key, 0) + usage.get(key, 0)


class TokenLedger:
    """Append-only token usage ledger with an incremental summary table."""

    def __init__(self, data_dir: str = None):
        if data_dir is None:
            _project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            data_dir = os.path.join(_project_dir, "data", "token_usage")
        self.data_dir = data_dir
        try:
            os.makedirs(self.data_dir, exist_ok=True)
        except OSError:
            pass

    # ── Paths ────────────────────────────────────────────────────

    def _daily_path(self, date_str: str) -> str:
        return os.path.join(self.data_dir, f"usage_{date_str}.jsonl")

    @property
    def _summary_path(self) -> str:
        return os.path.join(self.data_dir, "totals.json")

    # ── Recording ────────────────────────────────────────────────

    def record(
        self,
        model: str,
        purpose: str,
        usage: Dict[str, int],
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Record one LLM call. Never raises.

        Args:
            model: Model name (e.g. "deepseek-v4-pro").
            purpose: Call purpose tag (agent_loop / triage / profile /
                     multimodal_image / multimodal_audio / ag_resolver / chat).
            usage: Dict from :func:`extract_usage`.
            extra: Optional extra fields merged into the JSONL entry.
        """
        try:
            now = datetime.now()
            date_str = now.strftime("%Y-%m-%d")
            ctx = usage_context.get()

            entry = {
                "timestamp": now.isoformat(timespec="seconds"),
                "date": date_str,
                "model": model or "",
                "purpose": purpose or "chat",
                "user_id": ctx.get("user_id", ""),
                "group_id": ctx.get("group_id", ""),
                "chat_type": ctx.get("chat_type", ""),
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
                "cached_input_tokens": usage.get("cached_input_tokens", 0),
                "uncached_input_tokens": usage.get("uncached_input_tokens", 0),
            }
            if extra:
                entry.update(extra)

            # 1) Detail JSONL
            with open(self._daily_path(date_str), "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")

            # 2) Incremental summary table
            self._update_summary(entry, date_str)

            # 3) Occasional retention cleanup
            self._cleanup_old(now)
        except Exception:
            pass  # Metering must never break the main flow

    def _update_summary(self, entry: Dict[str, Any], date_str: str) -> None:
        summary = self.read_summary()
        for bucket_name in ("totals",):
            _add_bucket(summary[bucket_name], entry)
        _add_bucket(
            summary["by_model"].setdefault(entry["model"], _empty_bucket()),
            entry,
        )
        _add_bucket(
            summary["by_purpose"].setdefault(entry["purpose"], _empty_bucket()),
            entry,
        )
        _add_bucket(
            summary["daily"].setdefault(date_str, _empty_bucket()),
            entry,
        )
        # Trim daily entries beyond retention window
        cutoff = _retention_cutoff_str()
        summary["daily"] = {
            d: v for d, v in summary["daily"].items() if d >= cutoff
        }
        summary["updated_at"] = datetime.now().isoformat(timespec="seconds")

        tmp_path = self._summary_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, self._summary_path)

    def _cleanup_old(self, now: datetime) -> None:
        """Drop detail files older than the retention window.

        Runs at most once per hour per process to keep it cheap.
        """
        if now.strftime("%Y-%m-%d %H") == getattr(self, "_last_cleanup", ""):
            return
        self._last_cleanup = now.strftime("%Y-%m-%d %H")
        cutoff = _retention_cutoff_str()
        for name in os.listdir(self.data_dir):
            if name.startswith("usage_") and name.endswith(".jsonl"):
                day = name[len("usage_"):-len(".jsonl")]
                if day < cutoff:
                    try:
                        os.remove(os.path.join(self.data_dir, name))
                    except OSError:
                        pass

    # ── Reading ──────────────────────────────────────────────────

    def read_summary(self) -> Dict[str, Any]:
        """Load the summary table (totals.json). Returns a fresh empty
        structure when the file is missing or corrupt."""
        try:
            with open(self._summary_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for key in ("totals", "by_model", "by_purpose", "daily"):
                data.setdefault(key, {})
            data["totals"].setdefault("requests", 0)
            return data
        except Exception:
            return {
                "updated_at": None,
                "totals": _empty_bucket(),
                "by_model": {},
                "by_purpose": {},
                "daily": {},
            }

    def aggregate_day(self, date_str: str) -> Dict[str, int]:
        """Recompute one day's totals from its JSONL detail file.

        Useful for verification and for rebuilding a lost totals.json.
        """
        bucket = _empty_bucket()
        path = self._daily_path(date_str)
        if not os.path.exists(path):
            return bucket
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        _add_bucket(bucket, json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            pass
        return bucket

    def format_summary(self, days: int = 7) -> str:
        """Render the summary table as human-readable text.

        Shows global totals, the per-model and per-purpose breakdown,
        and the last *days* daily rows. Intended for a future admin
        command (e.g. /tokens).
        """
        s = self.read_summary()
        t = s["totals"]

        def _fmt(b: Dict[str, int]) -> str:
            return (
                f"请求{b.get('requests', 0)} | "
                f"输入{b.get('input_tokens', 0)} "
                f"(命中{b.get('cached_input_tokens', 0)}/"
                f"未命中{b.get('uncached_input_tokens', 0)}) | "
                f"输出{b.get('output_tokens', 0)}"
            )

        lines = [
            "═══ Token 计量总表 ═══",
            f"总计: {_fmt(t)}",
        ]
        if t.get("input_tokens"):
            hit_rate = 100.0 * t.get("cached_input_tokens", 0) / t["input_tokens"]
            lines.append(f"缓存命中率: {hit_rate:.1f}% (按输入 token 计)")

        if s["by_model"]:
            lines.append("── 按模型 ──")
            for model, b in sorted(s["by_model"].items()):
                lines.append(f"  {model}: {_fmt(b)}")
        if s["by_purpose"]:
            lines.append("── 按用途 ──")
            for purpose, b in sorted(s["by_purpose"].items()):
                lines.append(f"  {purpose}: {_fmt(b)}")
        if s["daily"]:
            lines.append(f"── 最近{days}天 ──")
            for day in sorted(s["daily"].keys())[-days:]:
                lines.append(f"  {day}: {_fmt(s['daily'][day])}")
        return "\n".join(lines)


def _retention_cutoff_str() -> str:
    return (datetime.now() - timedelta(days=_RETENTION_DAYS)).strftime("%Y-%m-%d")


# Global singleton
try:
    token_ledger = TokenLedger()
except Exception:
    token_ledger = None

"""
Read-only access to bot data for the panel pages.

Sources (see Web-UI-Plan.md):
  token_usage/   TokenLedger (totals.json + daily JSONL)
  audit/         tool_calls_YYYY-MM-DD.jsonl
  feedback/      feedback_YYYY-MM.jsonl
  sessions/      {uid}.json            临时会话
  task_log/      {uid}.jsonl           任务日志
  USER_DATA_ROOT/{uid}/workspace/      用户工作区
  USER_DATA_ROOT/{uid}/sessions/       特殊会话（_index.json）

All functions are defensive: a missing file/directory yields an empty
result, never an exception.
"""

import json
import os
import re
import shutil
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from . import config

DATA = config.QQBOT_DIR / "data"
TOKEN_DIR = DATA / "token_usage"
AUDIT_DIR = DATA / "audit"
FEEDBACK_DIR = DATA / "feedback"
SESSION_DIR = DATA / "sessions"
TASKLOG_DIR = DATA / "task_log"
MEMORY_DIR = DATA / "memory"
WIKI_CACHE_DIR = DATA / "wiki_cache"
REDEEM_DIR = DATA / "redeem_code"

PAGE_SIZE_DEFAULT = 50
_SEEN_FILE = config.WEBUI_DATA / "feedback_seen.json"


# ── small helpers ─────────────────────────────────────────────────

def _read_jsonl(path: Path) -> list:
    out = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return out


def _paginate(items: list, page: int, size: int = PAGE_SIZE_DEFAULT) -> dict:
    page = max(1, int(page))
    size = max(1, min(int(size), 500))
    total = len(items)
    pages = max(1, (total + size - 1) // size)
    page = min(page, pages)
    start = (page - 1) * size
    return {"items": items[start:start + size], "page": page,
            "pages": pages, "total": total}


def _safe_uid(uid: str) -> str | None:
    """Reject path-traversal in user ids."""
    if re.fullmatch(r"[A-Za-z0-9_\-]{1,64}", uid or ""):
        return uid
    return None


def _ledger():
    from lib.token_ledger import TokenLedger  # QQBot/ on sys.path
    return TokenLedger(str(TOKEN_DIR))


def _hit_rate(bucket: dict) -> float | None:
    inp = bucket.get("input_tokens", 0)
    return bucket.get("cached_input_tokens", 0) / inp if inp else None


def _provider_of(model: str) -> str:
    """Infer the LLM provider from the model name.

    提供商缓存互相隔离（见 Cache-Hit-Rate-Plan.md Phase 1）：DeepSeek 与
    dashscope 的前缀缓存互不可见，命中率必须分模型呈现，不能混算。
    """
    m = (model or "").lower()
    if m.startswith("deepseek"):
        return "DeepSeek"
    if m.startswith("qwen") or "dashscope" in m:
        return "阿里云 dashscope"
    return "—"


# ── Tokens ────────────────────────────────────────────────────────

def tokens_summary(days: int = 7) -> dict:
    s = _ledger().read_summary()
    cutoff = (datetime.now() - timedelta(days=max(1, days) - 1)).strftime("%Y-%m-%d")
    daily = {d: b for d, b in s.get("daily", {}).items() if d >= cutoff}
    totals = s.get("totals", {})
    # 按模型注解视图：提供商 + 命中率，按总 token 降序（Phase 1.2）
    models = []
    for name, b in s.get("by_model", {}).items():
        models.append({
            "model": name,
            "provider": _provider_of(name),
            "hit_rate": _hit_rate(b),
            **{k: b.get(k, 0) for k in (
                "requests", "input_tokens", "output_tokens",
                "cached_input_tokens", "uncached_input_tokens")},
        })
    models.sort(key=lambda x: x["input_tokens"] + x["output_tokens"], reverse=True)
    return {
        "totals": totals,
        "hit_rate": _hit_rate(totals),
        "daily": dict(sorted(daily.items())),
        "by_model": s.get("by_model", {}),
        "models": models,
        "by_purpose": s.get("by_purpose", {}),
        "updated_at": s.get("updated_at"),
    }


def tokens_daily(date_str: str) -> dict:
    bucket = _ledger().aggregate_day(date_str)
    return {"date": date_str, "bucket": bucket, "hit_rate": _hit_rate(bucket)}


def tokens_daily_by_purpose(date_str: str) -> dict:
    """Aggregate one day's detail JSONL into per-purpose buckets.

    仪表盘卡片需要「今日 agent_loop 命中率」（全局命中率会被短小的 triage
    与低收益的多模态请求稀释，见 Cache-Hit-Rate-Plan.md Phase 1.2）。
    ledger 的 totals.json 只有累计 by_purpose，按日按用途要从明细文件聚合。
    """
    buckets: dict[str, dict] = {}
    path = TOKEN_DIR / f"usage_{date_str}.jsonl"
    for entry in _read_jsonl(path):
        purpose = entry.get("purpose") or "chat"
        b = buckets.setdefault(purpose, {
            "requests": 0, "input_tokens": 0, "output_tokens": 0,
            "cached_input_tokens": 0, "uncached_input_tokens": 0})
        b["requests"] += 1
        for k in ("input_tokens", "output_tokens",
                  "cached_input_tokens", "uncached_input_tokens"):
            try:
                b[k] += int(entry.get(k, 0) or 0)
            except (TypeError, ValueError):
                pass
    return buckets


def tokens_users(days: int = 7, limit: int = 20) -> list:
    """Aggregate per-user tokens from the last `days` daily detail files."""
    cutoff = datetime.now() - timedelta(days=max(1, days) - 1)
    per_user: dict[str, dict] = defaultdict(lambda: {
        "requests": 0, "input_tokens": 0, "output_tokens": 0,
        "cached_input_tokens": 0})
    try:
        # ledger 的明细文件名是 usage_YYYY-MM-DD.jsonl（token_ledger.py
        # _daily_path）——旧 glob "token_usage_*" 永远匹配不到，用户排行恒为空
        files = sorted(TOKEN_DIR.glob("usage_*.jsonl"))
    except OSError:
        files = []
    for fp in files:
        try:
            d = datetime.strptime(fp.stem.replace("usage_", ""), "%Y-%m-%d")
        except ValueError:
            continue
        if d < cutoff.replace(hour=0, minute=0, second=0, microsecond=0):
            continue
        for entry in _read_jsonl(fp):
            uid = entry.get("user_id") or "(未归属)"
            b = per_user[uid]
            b["requests"] += 1
            for k in ("input_tokens", "output_tokens", "cached_input_tokens"):
                b[k] += int(entry.get(k, 0))
    ranked = sorted(per_user.items(),
                    key=lambda kv: kv[1]["input_tokens"] + kv[1]["output_tokens"],
                    reverse=True)
    return [{"user_id": u, **b} for u, b in ranked[:max(1, limit)]]


# ── Tool audit ────────────────────────────────────────────────────

def _audit_files(days: int | None = None) -> list[Path]:
    try:
        files = sorted(AUDIT_DIR.glob("tool_calls_*.jsonl"))
    except OSError:
        return []
    if days:
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        files = [f for f in files if f.stem.replace("tool_calls_", "") >= cutoff]
    return files


def audit_calls(date: str = "", tool: str = "", user: str = "",
                page: int = 1, size: int = PAGE_SIZE_DEFAULT) -> dict:
    if not date:
        date = datetime.now().strftime("%Y-%m-%d")
    rows = _read_jsonl(AUDIT_DIR / f"tool_calls_{date}.jsonl")
    if tool:
        rows = [r for r in rows if r.get("tool") == tool]
    if user:
        rows = [r for r in rows if str(r.get("user_id")) == user]
    rows.reverse()  # newest first
    result = _paginate(rows, page, size)
    result["date"] = date
    return result


def audit_stats(days: int = 7) -> dict:
    per_tool: dict[str, dict] = defaultdict(lambda: {"calls": 0, "failed": 0})
    per_user: dict[str, int] = defaultdict(int)
    total = 0
    for fp in _audit_files(days):
        for r in _read_jsonl(fp):
            total += 1
            t = per_tool[r.get("tool", "?")]
            t["calls"] += 1
            if not r.get("success", True):
                t["failed"] += 1
            per_user[str(r.get("user_id", "?"))] += 1
    return {
        "total": total,
        "by_tool": dict(sorted(per_tool.items(), key=lambda kv: -kv[1]["calls"])),
        "by_user": dict(sorted(per_user.items(), key=lambda kv: -kv[1])[:20]),
        "dates": [f.stem.replace("tool_calls_", "") for f in _audit_files(days)],
    }


# ── Feedback ──────────────────────────────────────────────────────

def _feedback_files() -> list[Path]:
    try:
        return sorted(FEEDBACK_DIR.glob("feedback_*.jsonl"), reverse=True)
    except OSError:
        return []


def _parse_fb_ts(ts: str) -> float:
    try:
        return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").timestamp()
    except (ValueError, TypeError):
        return 0.0


def _feedback_seen_ts() -> float:
    try:
        return float(json.loads(_SEEN_FILE.read_text()).get("seen_ts", 0))
    except Exception:
        return 0.0


def feedback_total_count() -> int:
    n = 0
    for fp in _feedback_files():
        n += len(_read_jsonl(fp))
    return n


def feedback_unread_count() -> int:
    seen = _feedback_seen_ts()
    n = 0
    for fp in _feedback_files():
        for r in _read_jsonl(fp):
            if _parse_fb_ts(r.get("timestamp")) > seen:
                n += 1
    return n


def feedback_mark_read() -> None:
    _SEEN_FILE.write_text(json.dumps({"seen_ts": time.time()}))


def feedback_list(month: str = "", type_: str = "",
                  page: int = 1, size: int = PAGE_SIZE_DEFAULT) -> dict:
    rows = []
    for fp in _feedback_files():
        m = fp.stem.replace("feedback_", "")
        if month and m != month:
            continue
        for seq, r in enumerate(_read_jsonl(fp)):
            if type_ and r.get("type") != type_:
                continue
            r["_month"] = m
            r["_seq"] = seq
            rows.append(r)
    rows.sort(key=lambda r: _parse_fb_ts(r.get("timestamp")), reverse=True)
    result = _paginate(rows, page, size)
    result["months"] = [fp.stem.replace("feedback_", "") for fp in _feedback_files()]
    result["unread"] = feedback_unread_count()
    return result


def feedback_tag(month: str, seq: int, tag: str) -> dict:
    """Append a tag to one feedback record (rewrites the month file)."""
    if not re.fullmatch(r"\d{4}-\d{2}", month or ""):
        return {"error": "非法月份"}
    tag = (tag or "").strip()[:32]
    if not tag:
        return {"error": "标签为空"}
    fp = FEEDBACK_DIR / f"feedback_{month}.jsonl"
    rows = _read_jsonl(fp)
    if not (0 <= int(seq) < len(rows)):
        return {"error": "记录不存在"}
    # backup before rewrite
    try:
        config.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(fp, config.BACKUP_DIR /
                     f"feedback_{month}_{int(time.time())}.jsonl")
    except OSError:
        pass
    rows[int(seq)].setdefault("tags", [])
    if tag not in rows[int(seq)]["tags"]:
        rows[int(seq)]["tags"].append(tag)
    try:
        with open(fp, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    except OSError as e:
        return {"error": str(e)}
    return {"ok": True}


# ── Temp sessions ─────────────────────────────────────────────────

def sessions_list() -> list:
    out = []
    try:
        files = sorted(SESSION_DIR.glob("*.json"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return []
    for fp in files[:200]:
        try:
            d = json.loads(fp.read_text(encoding="utf-8"))
        except Exception:
            continue
        out.append({
            "user_id": d.get("user_id", fp.stem),
            "messages": len(d.get("context", [])),
            "created_at": d.get("created_at"),
            "last_active": d.get("last_active"),
            "tool_call_count": d.get("tool_call_count", 0),
            "size_bytes": fp.stat().st_size,
        })
    return out


def session_detail(uid: str) -> dict:
    if not _safe_uid(uid):
        return {"error": "非法用户 ID"}
    fp = SESSION_DIR / f"{uid}.json"
    try:
        return json.loads(fp.read_text(encoding="utf-8"))
    except OSError:
        return {"error": "会话不存在"}
    except json.JSONDecodeError:
        return {"error": "会话文件损坏"}


def session_clear_temp(uid: str) -> dict:
    """Delete a temp session file (backed up first)."""
    if not _safe_uid(uid):
        return {"error": "非法用户 ID"}
    fp = SESSION_DIR / f"{uid}.json"
    if not fp.exists():
        return {"error": "会话不存在"}
    try:
        bak_dir = config.BACKUP_DIR / "sessions"
        bak_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(fp, bak_dir / f"{uid}_{int(time.time())}.json")
        fp.unlink()
    except OSError as e:
        return {"error": str(e)}
    return {"ok": True}


# ── Special sessions ──────────────────────────────────────────────

def _special_manager():
    from agent.special_session import SpecialSessionManager
    return SpecialSessionManager(user_data_root=str(config.USER_DATA_ROOT))


def special_list(uid: str) -> dict:
    if not _safe_uid(uid):
        return {"error": "非法用户 ID"}
    try:
        mgr = _special_manager()
        active = mgr.get_active(uid)
        return {
            "sessions": mgr.list_sessions(uid),
            "active": active.name if active else None,
        }
    except Exception as e:
        return {"sessions": [], "active": None, "error": str(e)}


def special_delete(uid: str, name: str) -> dict:
    if not _safe_uid(uid):
        return {"error": "非法用户 ID"}
    try:
        return _special_manager().delete(uid, name)
    except Exception as e:
        return {"error": str(e)}


# ── Task log ──────────────────────────────────────────────────────

def tasks_list(user_id: str = "", page: int = 1,
               size: int = PAGE_SIZE_DEFAULT) -> dict:
    rows = []
    try:
        files = sorted(TASKLOG_DIR.glob("*.jsonl"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        files = []
    for fp in files[:100]:
        uid = fp.stem
        if user_id and uid != user_id:
            continue
        for r in _read_jsonl(fp):
            r["_user_id"] = uid
            rows.append(r)
    rows.sort(key=lambda r: r.get("timestamp", "") or "", reverse=True)
    result = _paginate(rows, page, size)
    result["users"] = sorted({fp.stem for fp in files})[:100]
    return result


def task_detail(uid: str, task_id: str) -> dict:
    if not _safe_uid(uid):
        return {"error": "非法用户 ID"}
    for r in _read_jsonl(TASKLOG_DIR / f"{uid}.jsonl"):
        if str(r.get("task_id")) == task_id:
            return r
    return {"error": "任务不存在"}


# ── Workspace ─────────────────────────────────────────────────────

def _role_of(uid: str) -> str:
    supers = {s.strip() for s in config.bot_env("SUPERUSERS", "").split(",") if s.strip()}
    vips = {s.strip() for s in config.bot_env("VIP_USERS", "").split(",") if s.strip()}
    if uid in supers:
        return "admin"
    if uid in vips:
        return "vip"
    return "regular"


_QUOTA_MB = {"admin": 2048, "vip": 500, "regular": 100}


def _dir_size(path: Path, cap_files: int = 20000) -> int:
    total, seen = 0, 0
    try:
        for root, _dirs, files in os.walk(path):
            for name in files:
                seen += 1
                if seen > cap_files:
                    return total
                try:
                    total += os.path.getsize(os.path.join(root, name))
                except OSError:
                    pass
    except OSError:
        pass
    return total


def workspace_stats() -> dict:
    root = config.USER_DATA_ROOT
    if not root.exists():
        return {"mounted": False, "root": str(root), "users": []}
    users = []
    total_used = 0
    for d in sorted(root.iterdir()):
        if not d.is_dir() or not _safe_uid(d.name):
            continue
        ws = d / "workspace"
        used = _dir_size(ws) if ws.exists() else 0
        role = _role_of(d.name)
        quota = _QUOTA_MB[role] * 1024 * 1024
        total_used += used
        users.append({
            "user_id": d.name, "role": role,
            "used": used, "quota": quota,
            "percent": round(100.0 * used / quota, 1) if quota else 0,
        })
    users.sort(key=lambda u: -u["used"])
    return {"mounted": True, "root": str(root), "users": users,
            "total_used": total_used}


def workspace_tree(uid: str, max_entries: int = 500) -> dict:
    if not _safe_uid(uid):
        return {"error": "非法用户 ID"}
    ws = config.USER_DATA_ROOT / uid / "workspace"
    if not ws.exists():
        return {"error": "工作区不存在", "entries": []}
    entries = []
    for root, dirs, files in os.walk(ws):
        rel = Path(root).relative_to(ws)
        for name in sorted(dirs):
            entries.append({"path": str(rel / name), "type": "dir"})
        for name in sorted(files):
            fp = Path(root) / name
            try:
                size = fp.stat().st_size
            except OSError:
                size = 0
            entries.append({"path": str(rel / name), "type": "file", "size": size})
        if len(entries) >= max_entries:
            break
    return {"root": str(ws), "entries": entries[:max_entries],
            "size": _dir_size(ws)}


# ── Memory ────────────────────────────────────────────────────────

def _memory_system():
    from agent.memory import MemorySystem
    return MemorySystem(str(MEMORY_DIR))


def memory_list(mem_type: str = "", user_id: str = "", q: str = "") -> dict:
    try:
        ms = _memory_system()
        entries = (ms.search(q, mem_type or None, user_id or None) if q
                   else ms.list_all(mem_type or None, user_id or None))
    except Exception as e:
        return {"entries": [], "error": str(e)}
    out = [{
        "name": e.name, "description": e.description, "type": e.type,
        "user_id": e.user_id, "content": e.content,
        "created_at": e.created_at, "updated_at": e.updated_at,
    } for e in entries]
    out.sort(key=lambda r: r.get("updated_at") or 0, reverse=True)
    return {"entries": out}


def memory_save(name: str, description: str, mem_type: str,
                content: str, user_id: str = "") -> dict:
    name = (name or "").strip()
    if not name:
        return {"error": "名称为空"}
    if mem_type not in ("user", "knowledge", "system"):
        return {"error": f"非法记忆类型: {mem_type}"}
    if mem_type == "user" and not user_id:
        return {"error": "user 类型记忆必须指定 user_id"}
    try:
        from agent.memory import MemoryEntry
        ms = _memory_system()
        existing = ms.recall(name, mem_type, user_id or None)
        entry = MemoryEntry(
            name=name, description=description or "", type=mem_type,
            content=content or "", user_id=user_id or None,
            created_at=existing.created_at if existing else time.time(),
        )
        path = ms.save(entry)
    except Exception as e:
        return {"error": str(e)}
    return {"ok": True, "path": path}


def memory_delete(name: str, mem_type: str = "", user_id: str = "") -> dict:
    try:
        ok = _memory_system().forget(name, mem_type or None, user_id or None)
    except Exception as e:
        return {"error": str(e)}
    return {"ok": True} if ok else {"error": "记忆不存在"}


# ── Profiles ──────────────────────────────────────────────────────

def _profile_manager():
    from agent.profile import ProfileManager
    return ProfileManager(base_dir=str(config.USER_DATA_ROOT))


def profile_get(uid: str) -> dict:
    if not _safe_uid(uid):
        return {"error": "非法用户 ID"}
    try:
        return _profile_manager().get(uid).to_dict()
    except Exception as e:
        return {"error": f"画像不可用: {e}"}


def profile_save(uid: str, data: dict) -> dict:
    if not _safe_uid(uid):
        return {"error": "非法用户 ID"}
    try:
        from agent.profile import UserProfile
        profile = UserProfile.from_dict({**data, "user_id": uid})
        _profile_manager().save(profile)
    except Exception as e:
        return {"error": f"保存失败: {e}"}
    return {"ok": True}


# ── Wiki cache status ─────────────────────────────────────────────

def _dir_stats(path: Path, json_depth: bool = True) -> dict:
    if not path.exists():
        return {"exists": False}
    try:
        regular = [f for f in path.rglob("*") if f.is_file()]
    except OSError:
        return {"exists": False}
    total = 0
    newest = 0.0
    json_entries = 0
    for f in regular:
        try:
            st = f.stat()
            total += st.st_size
            newest = max(newest, st.st_mtime)
        except OSError:
            pass
        if json_depth and f.suffix == ".json":
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
                json_entries += len(d) if isinstance(d, (dict, list)) else 1
            except Exception:
                pass
    return {
        "exists": True,
        "files": len(regular),
        "size": total,
        "newest_mtime": int(newest) if newest else None,
        "json_entries": json_entries,
    }


def wiki_cache_status() -> dict:
    return {
        "wiki_cache": _dir_stats(WIKI_CACHE_DIR),
        "redeem_code": _dir_stats(REDEEM_DIR),
        "root": str(DATA),
    }

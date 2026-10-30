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
import random
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
# 与 bot 侧 turn.py 同源：MEMORY_DIR env（生产指数据盘）经 QQBot/.env 透传，
# 缺省回落 data/memory。tiers/ 子目录随 base_dir 一起走。
MEMORY_DIR = Path(config.bot_env("MEMORY_DIR", str(DATA / "memory")))
WIKI_CACHE_DIR = DATA / "wiki_cache"
REDEEM_DIR = DATA / "redeem_code"
CHANGELOG_DIR = DATA / "changelog"

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


# 剧情/创作会话不计普通特殊会话配额（scene-session-archive-design.md §2.1）
_NON_QUOTA_KINDS = {"bond_scene", "char_creation"}


def special_list(uid: str) -> dict:
    if not _safe_uid(uid):
        return {"error": "非法用户 ID"}
    try:
        mgr = _special_manager()
        active = mgr.get_active(uid)
        raw = mgr.list_sessions(uid)
        # 展平 kind/completed 便于模板渲染徽标（scene-session-archive §5.5/T15）
        sessions = []
        for s in raw:
            meta = s.get("metadata") or {}
            kind = meta.get("kind") or ""
            item = dict(s)
            item["kind"] = kind
            item["completed"] = bool(meta.get("completed"))
            item["counts_quota"] = kind not in _NON_QUOTA_KINDS
            sessions.append(item)
        # 配额摘要：普通会话计配额；剧情/创作不计（与 turn.py 口径一致）
        try:
            quota_max = _permissions.get_max_special_sessions(
                _permissions.get_role(uid))
        except Exception:
            quota_max = None
        return {
            "sessions": sessions,
            "active": active.name if active else None,
            "quota_used": sum(1 for s in sessions if s["counts_quota"]),
            "quota_max": quota_max,
            "scene_archives": sum(1 for s in sessions if s["kind"] == "bond_scene"),
            "scene_done": sum(1 for s in sessions
                              if s["kind"] == "bond_scene" and s["completed"]),
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

# Reuse the bot's PermissionManager as the single source of truth for roles.
# QQBot/.env stores IDs in NoneBot JSON-array form (SUPERUSERS=["123", ...]);
# the previous naive comma-split parsed those into '["123"' / '"456"]' which
# never matched a bare uid, so every user showed as regular/100MB in the panel.
# Import is safe: webui.config puts QQBot/ on sys.path, and agent.permissions
# has no nonebot dependency. get_role() re-reads .env on every call, so panel
# edits via the config page take effect without a restart (same as the bot).
from agent.permissions import PermissionManager  # noqa: E402

_permissions = PermissionManager()


def _role_of(uid: str) -> str:
    return _permissions.get_role(uid).value


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


# ── Wiki alias management ─────────────────────────────────────────
# 权威别名字典（扁平 {alias: canonical_cn}），由 bot 的 name_resolver 使用。
# 面板以「分组视图」{canonical: [alias...]} 展示/编辑，保存时扁平化全量替换，
# 并调用 name_resolver.build_index() 重建拼音索引；bot 侧监听器随后热重载。

_ALIAS_DIR = config.QQBOT_DIR / "config" / "characters"
_ALIAS_FILES = {
    "char": _ALIAS_DIR / "character_dic.json",
    "bond": _ALIAS_DIR / "bonds_search_dic.json",
}


def wiki_aliases_read(kind: str) -> dict:
    """Read the alias dictionary for kind (char|bond), inverted to groups.

    Returns ``{canonical: [aliases...]}`` — aliases keep their original
    case, canonical order follows first appearance in the file.
    """
    path = _ALIAS_FILES.get(kind)
    if path is None:
        return {"error": f"非法别名类型: {kind}"}
    if not path.exists():
        return {"error": f"别名字典不存在: {path.name}"}
    try:
        flat = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        return {"error": f"读取失败: {e}"}
    groups: dict[str, list[str]] = {}
    for alias, canonical in flat.items():
        groups.setdefault(str(canonical), []).append(str(alias))
    return groups


def wiki_aliases_save(kind: str, grouped: dict) -> dict:
    """Validate + flatten + atomically replace the alias dictionary.

    ``grouped`` is the full snapshot ``{canonical: [aliases...]}`` for this
    kind. Alias uniqueness is enforced within the kind (an alias mapping to
    two canonicals would silently hijack lookups). After a successful write
    the pinyin index is rebuilt via name_resolver.build_index().
    """
    path = _ALIAS_FILES.get(kind)
    if path is None:
        return {"error": f"非法别名类型: {kind}"}

    # ── validate + flatten ──
    flat: dict[str, str] = {}
    owner: dict[str, str] = {}  # lowercase alias → canonical (for dup check)
    for canonical, aliases in grouped.items():
        canonical = str(canonical).strip()
        if not canonical:
            return {"error": "角色/羁绊名（canonical）不能为空"}
        if not isinstance(aliases, list):
            return {"error": f"{canonical}: 别名必须是列表"}
        for alias in aliases:
            alias = str(alias).strip()
            if not alias:
                return {"error": f"{canonical}: 存在空别名"}
            low = alias.lower()
            if low in owner:
                return {"error": f"别名「{alias}」已属于「{owner[low]}」，不能重复"}
            owner[low] = canonical
            flat[alias] = canonical

    # ── backup + atomic write ──
    try:
        if path.exists():
            shutil.copy2(path, str(path) + ".bak")
        tmp = str(path) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(flat, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        return {"error": f"写入失败: {e}"}

    # ── rebuild pinyin index (bot side only reads it from disk) ──
    warning = None
    try:
        from tools.name_resolver import build_index
        build_index()
    except Exception as e:
        warning = f"别名已保存，但拼音索引重建失败: {e}"

    result = {"ok": True, "groups": len(grouped), "aliases": len(flat)}
    if warning:
        result["warning"] = warning
    return result


# 详情缓存（wiki 爬取产物，envelope: {scraped_at, source_url, data: {英文标题: entry}}）。
# 用于发现「已爬取但字典中尚无分组」的角色/羁绊。
_ALIAS_DETAILS_FILES = {
    "char": WIKI_CACHE_DIR / "character_details.json",
    "bond": WIKI_CACHE_DIR / "bond_details.json",
}


def wiki_aliases_missing(kind: str) -> dict:
    """List scraped entries that have no group in the alias dictionary yet.

    Matching is by **English title** (the details-cache key) against the dic's
    alias set — deterministic, and immune to LLM Chinese-translation variance
    in ``name_cn`` (a character whose group already exists is never reported,
    even if the cached translation uses different hanzi than the canonical).
    Falls back to skipping entries whose ``name_cn`` already is a known
    canonical. A missing/corrupt details cache yields an empty list + note
    (bond_details.json is not present on every machine).

    Returns ``{"items": [{canonical, title_en, aliases_hint}], "note"?}``
    or ``{"error": ...}``.
    """
    if kind not in _ALIAS_FILES:
        return {"error": f"非法别名类型: {kind}"}
    details_path = _ALIAS_DETAILS_FILES[kind]
    if not details_path.exists():
        return {"items": [], "note": f"详情缓存不存在: {details_path.name}（尚未爬取）"}
    try:
        blob = json.loads(details_path.read_text(encoding="utf-8"))
        data = blob.get("data", {}) if isinstance(blob, dict) else {}
    except Exception as e:
        return {"items": [], "note": f"详情缓存读取失败: {e}"}

    # 已有别名 + 规范名集合（英文标题在字典中即以别名形式收录）
    known: set[str] = set()
    dic_path = _ALIAS_FILES[kind]
    if dic_path.exists():
        try:
            flat = json.loads(dic_path.read_text(encoding="utf-8"))
            known = {str(a) for a in flat} | {str(c) for c in flat.values()}
        except Exception:
            known = set()
    known_low = {k.lower() for k in known}

    items, seen = [], set()
    for title, entry in data.items():
        if not isinstance(entry, dict):
            continue
        title = str(title)
        if title in known or title.lower() in known_low:
            continue
        canon = str(entry.get("name_cn") or "").strip() or title
        if canon in known or canon in seen:
            continue
        seen.add(canon)
        hint = [canon] + ([title] if title != canon else [])
        items.append({"canonical": canon, "title_en": title, "aliases_hint": hint})
    items.sort(key=lambda x: x["canonical"])
    return {"items": items}


# ── Wiki 翻译术语表（glossary）─────────────────────────────────────
# 术语表的唯一 owner 是 QQBot/tools/wiki_glossary.py（校验/备份/原子写都在
# 那边，保证 bot 与面板走同一套规则）；面板侧只做转调。bot 全部消费点按
# mtime 热读，保存后无需重启即生效。

def wiki_glossary_read() -> dict:
    """当前生效术语表 + 来源徽标 + 表结构（供前端渲染行编辑器）。"""
    try:
        from tools.wiki_glossary import (TABLE_LABELS, TABLE_SHAPES,
                                         glossary_source, load_glossary)
        return {
            "glossary": load_glossary(),
            "source": glossary_source(),          # "file" | "builtin"
            "shapes": dict(TABLE_SHAPES),
            "labels": dict(TABLE_LABELS),
        }
    except Exception as e:
        return {"error": f"读取术语表失败: {e}"}


def wiki_glossary_save(tables: dict) -> dict:
    """校验 + 备份 + 原子写术语表文件（转调 bot 模块，规则唯一）。"""
    try:
        from tools.wiki_glossary import save_glossary
        return save_glossary(tables)
    except Exception as e:
        return {"error": f"保存术语表失败: {e}"}


def wiki_glossary_default() -> dict:
    """内置默认术语表（面板「恢复内置默认」预览/提交用）。"""
    try:
        from tools.wiki_glossary import default_glossary
        return {"glossary": default_glossary()}
    except Exception as e:
        return {"error": f"读取内置默认失败: {e}"}


# ── Wiki 详情缓存编辑（人工修正 / 重翻译写回）────────────────────
# 字段级人工修正：白名单路径（char: name_cn/desc/discs/skills.{i}.子字段；
# bond: name_cn/desc/effect/notes/obtain），被改字段记入 entry 的
# ``_manual_fields``（UI 徽标 + 重翻译 confirm 提示）。bot 侧刷新管线读同一
# 名单（wiki_scraper._MANUAL_TOP_FIELDS/_MANUAL_SKILL_FIELDS）：/刷新角色数据
# 或每周自动刷新重建条目后，人工字段从旧缓存回填，不被 LLM 翻译覆盖。
# 整条 LLM 重翻译由 main.py 的后台任务执行（force_retranslate），写回走
# wiki_details_replace_entry。

# 可人工修正的字段白名单（skills 子字段单独校验）
# ⚠ 与 QQBot/tools/wiki_scraper.py 的 _MANUAL_TOP_FIELDS/_MANUAL_SKILL_FIELDS
#   保持同步（test/test_character_lookup_and_heal.py 第 8 节做一致性校验）。
_DETAIL_TOP_FIELDS = {
    "char": ("name_cn", "desc", "discs"),
    "bond": ("name_cn", "desc", "effect", "notes", "obtain"),
}
_DETAIL_SKILL_FIELDS = ("name", "des", "des2", "burst")


def _details_load(kind: str) -> tuple[Path | None, dict | None, str | None]:
    """Return (path, envelope, error) for a kind's details cache."""
    path = _ALIAS_DETAILS_FILES.get(kind)
    if path is None:
        return None, None, f"非法详情类型: {kind}"
    if not path.exists():
        return path, None, f"详情缓存不存在: {path.name}（尚未爬取）"
    try:
        blob = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(blob, dict) or not isinstance(blob.get("data"), dict):
            return path, None, "详情缓存格式异常（缺少 data 对象）"
        return path, blob, None
    except Exception as e:
        return path, None, f"详情缓存读取失败: {e}"


def _details_write_atomic(path: Path, blob: dict) -> str | None:
    """Backup + atomic replace; returns error string or None."""
    try:
        if path.exists():
            shutil.copy2(path, str(path) + ".bak")
        tmp = str(path) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(blob, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return None
    except Exception as e:
        return f"写入失败: {e}"


def wiki_details_list(kind: str, q: str = "") -> dict:
    """Summary rows for the panel's entry picker: title/name_cn/id/stars/…

    ``q`` filters title/name_cn case-insensitively. A missing cache yields an
    empty list + note (bond_details.json is not present on every machine).
    """
    path, blob, err = _details_load(kind)
    if err:
        if path is not None and not path.exists():
            return {"items": [], "note": err}
        return {"error": err}
    q = (q or "").strip().lower()
    items = []
    for title, entry in blob["data"].items():
        if not isinstance(entry, dict):
            continue
        name_cn = str(entry.get("name_cn") or "")
        if q and q not in str(title).lower() and q not in name_cn.lower():
            continue
        items.append({
            "title": str(title),
            "name_cn": name_cn,
            "id": str(entry.get("id") or ""),
            "stars": int(entry.get("stars") or 0),
            "has_cn": bool(name_cn and name_cn != title),
            "manual": bool(entry.get("_manual_fields")),
        })
    items.sort(key=lambda x: x["title"])
    return {"items": items, "total": len(blob["data"])}


def wiki_details_entry(kind: str, title: str) -> dict:
    """Full entry for the field editor (or ``{"error": ...}``)."""
    _, blob, err = _details_load(kind)
    if err:
        return {"error": err}
    entry = blob["data"].get(title)
    if not isinstance(entry, dict):
        return {"error": f"条目不存在: {title}"}
    return {"kind": kind, "title": title, "entry": entry}


def wiki_details_save_fields(kind: str, title: str, fields: dict) -> dict:
    """Apply whitelisted manual edits to one entry and persist atomically.

    ``fields`` keys are dotted paths: ``name_cn`` / ``desc`` / ``discs``
    (list[str], char only) / ``skills.{i}.{name|des|des2|burst}`` (char) or
    ``name_cn/desc/effect/notes/obtain`` (bond). Every applied path is
    recorded in ``entry["_manual_fields"]`` so the UI can badge it and the
    retranslate confirm can warn that LLM re-translation overwrites it.
    Re-reads the freshest on-disk copy before applying (bot background
    refresh may have written in between).
    """
    if kind not in _DETAIL_TOP_FIELDS:
        return {"error": f"非法详情类型: {kind}"}
    if not fields:
        return {"error": "没有要保存的字段"}
    path, blob, err = _details_load(kind)
    if err:
        return {"error": err}
    entry = blob["data"].get(title)
    if not isinstance(entry, dict):
        return {"error": f"条目不存在: {title}"}

    top_ok = _DETAIL_TOP_FIELDS[kind]
    applied = []
    for key, value in fields.items():
        parts = str(key).split(".")
        if parts[0] == "skills":
            if kind != "char" or len(parts) != 3:
                return {"error": f"非法字段路径: {key}"}
            try:
                idx = int(parts[1])
            except ValueError:
                return {"error": f"非法技能序号: {key}"}
            skills = entry.get("skills") or []
            if not (0 <= idx < len(skills)) or not isinstance(skills[idx], dict):
                return {"error": f"技能序号越界: {key}"}
            if parts[2] not in _DETAIL_SKILL_FIELDS:
                return {"error": f"非法技能字段: {key}"}
            if not isinstance(value, str):
                return {"error": f"{key}: 值必须是字符串"}
            skills[idx][parts[2]] = value
        elif len(parts) == 1 and parts[0] in top_ok:
            if parts[0] == "discs":
                if (not isinstance(value, list)
                        or not all(isinstance(v, str) for v in value)):
                    return {"error": "discs: 值必须是字符串列表"}
                entry["discs"] = value
            else:
                if not isinstance(value, str):
                    return {"error": f"{key}: 值必须是字符串"}
                entry[parts[0]] = value
        else:
            return {"error": f"字段不在白名单内: {key}"}
        applied.append(str(key))

    manual = set(entry.get("_manual_fields") or []) | set(applied)
    entry["_manual_fields"] = sorted(manual)
    err = _details_write_atomic(path, blob)
    if err:
        return {"error": err}
    return {"ok": True, "applied": len(applied),
            "manual_fields": entry["_manual_fields"]}


def wiki_details_replace_entry(kind: str, title: str, entry: dict) -> dict:
    """Replace one entry wholesale (LLM re-translation write-back path).

    Re-reads the freshest on-disk envelope first so a concurrent bot refresh
    of *other* entries isn't clobbered; only ``data[title]`` is replaced.
    """
    path, blob, err = _details_load(kind)
    if err:
        return {"error": err}
    if title not in blob["data"]:
        return {"error": f"条目不存在（可能已被刷新移除）: {title}"}
    blob["data"][title] = entry
    err = _details_write_atomic(path, blob)
    if err:
        return {"error": err}
    return {"ok": True}


# ── Redeem code management (兑换码管理) ───────────────────────────
# CRUD over QQBot/data/redeem_code/redeem_code.json — the bot plugin
# (plugins/check_redeem_code.py) re-reads the file on every call, so
# panel saves take effect immediately. Envelope keys (scraped_at etc.)
# are preserved; only `codes` is replaced. Manual entries carry
# _source="panel" and are authoritative on the bot side (the scraper
# never overwrites them, cleanup never removes them). A panel entry's
# blank `valid` means long-term (the form promises 留空 = 长期) — the
# 30-day "fresh = time-limited" heuristic applies to scraped entries
# only, in redeem_list/_class, redeem.js classify() and the plugin's
# get_time_limited_codes/get_long_term_codes alike.

REDEEM_FILE = REDEEM_DIR / "redeem_code.json"
REDEEM_ALERT_FILE = REDEEM_DIR / "admin_alert.json"

_REDEEM_RECENT_DAYS = 30  # keep in sync with plugin _RECENT_DAYS


def redeem_list() -> dict:
    """Read the cache for the panel table, annotating 限时/长期 classes."""
    data = {}
    if REDEEM_FILE.exists():
        try:
            data = json.loads(REDEEM_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            return {"error": f"缓存文件损坏: {e}", "codes": []}
    codes = data.get("codes", [])
    recent_cutoff = (
        datetime.now() - timedelta(days=_REDEEM_RECENT_DAYS)
    ).strftime("%Y-%m-%d")
    for c in codes:
        if not isinstance(c, dict):
            continue
        expiry = c.get("valid", "")
        added = c.get("_added", "") or ""
        if expiry:
            c["_class"] = "限时"
        elif added and added >= recent_cutoff and c.get("_source") != "panel":
            # 30 天新码启发式只适用于爬取数据（来源常不给活动码标有效期）；
            # panel 条目是管理员的明确声明 —— 有效期留空即长期。
            c["_class"] = "限时"
        else:
            c["_class"] = "长期"
    return {
        "codes": codes,
        "scraped_at": data.get("scraped_at", 0),
        "scraped_at_iso": data.get("scraped_at_iso", ""),
        "source_url": data.get("source_url", ""),
        "alert": redeem_alert_status(),
    }


def redeem_save(codes) -> dict:
    """Validate + save the codes array, preserving envelope keys.

    `codes` is the full list snapshot from the panel. Clears the admin
    alert on success (the admin has acted on it).
    """
    from . import config_editor

    if not isinstance(codes, list):
        return {"error": "codes 必须是数组"}

    existing = {}
    if REDEEM_FILE.exists():
        try:
            existing = json.loads(REDEEM_FILE.read_text(encoding="utf-8"))
        except Exception:
            existing = {}
    existing_by_code = {
        e.get("code"): e
        for e in existing.get("codes", [])
        if isinstance(e, dict) and e.get("code")
    }

    seen = set()
    today = datetime.now().strftime("%Y-%m-%d")
    for i, entry in enumerate(codes):
        if not isinstance(entry, dict):
            return {"error": f"第 {i + 1} 条不是对象"}
        code = str(entry.get("code") or "").strip()
        if not code:
            return {"error": f"第 {i + 1} 条缺少兑换码"}
        if code in seen:
            return {"error": f"兑换码重复: {code}"}
        seen.add(code)
        entry["code"] = code
        valid = str(entry.get("valid") or "").strip()[:10]
        if valid:
            try:
                datetime.strptime(valid, "%Y-%m-%d")
            except ValueError:
                return {"error": f"{code}: 有效期格式应为 YYYY-MM-DD（{valid}）"}
        entry["valid"] = valid
        entry["content"] = str(entry.get("content") or "").strip()
        old = existing_by_code.get(code)
        if old is None:
            # Brand-new entry → panel defaults
            if not entry.get("_added"):
                entry["_added"] = today
            if not entry.get("_source"):
                entry["_source"] = "panel"
        else:
            # Cached entry: backfill from the file, never fabricate. A made-up
            # fresh _added would flip a long-term code into the time-limited
            # bucket for 30 days; a made-up _source="panel" would wrongly make
            # a scraped entry panel-authoritative.
            if not entry.get("_added"):
                entry["_added"] = old.get("_added") or ""
            if not entry.get("_source"):
                entry["_source"] = old.get("_source") or ""
        entry.pop("_class", None)  # transient annotation, never persisted

    envelope = {
        "scraped_at": existing.get("scraped_at", 0),
        "scraped_at_iso": existing.get("scraped_at_iso", ""),
        "source_url": existing.get("source_url", ""),
        "codes": codes,
    }
    raw = json.dumps(envelope, ensure_ascii=False, indent=2)
    result = config_editor.json_config_write(REDEEM_FILE, raw)
    if result.get("ok"):
        redeem_alert_clear()
    return result


def redeem_parse_tweet(text: str) -> dict:
    """Parse official announcement text via the bot plugin's parser.

    Lazy import is safe: plugins/__init__.py is empty and
    check_redeem_code has no nonebot imports (QQBot/ is on sys.path
    via webui.config).
    """
    try:
        from plugins.check_redeem_code import parse_tweet_text
    except Exception as e:
        return {"error": f"解析器不可用: {e}"}
    return parse_tweet_text(text or "")


def redeem_alert_status() -> dict:
    """Current admin alert state ({} when none)."""
    if not REDEEM_ALERT_FILE.exists():
        return {}
    try:
        return json.loads(REDEEM_ALERT_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def redeem_alert_clear() -> dict:
    """Dismiss the admin alert (no-op when absent)."""
    try:
        if REDEEM_ALERT_FILE.exists():
            REDEEM_ALERT_FILE.unlink()
    except OSError as e:
        return {"error": str(e)}
    return {"ok": True}


# ── 更新日志（changelog）────────────────────────────────────────────
# 数据源 QQBot/data/changelog/，bot 侧对应 QQBot/tools/changelog.py。
# 面板是唯一编辑入口；群发通过文件队列交给 bot 后台轮询器执行（面板与 bot
# 是独立进程，无法直接调用 bot.send_group_msg）。

CHANGELOG_FILE = CHANGELOG_DIR / "changelog.json"
ANNOUNCEMENTS_FILE = CHANGELOG_DIR / "announcements.json"
KNOWN_GROUPS_FILE = CHANGELOG_DIR / "known_groups.json"
PENDING_FILE = CHANGELOG_DIR / "pending_broadcast.json"
STATUS_FILE = CHANGELOG_DIR / "broadcast_status.json"

# 与 bot 侧 tools/changelog.py 的 CHANGE_TYPES 保持一致
CHANGELOG_CHANGE_TYPES = ["新增", "修复", "优化", "调整", "移除"]


def _atomic_write_json(path: Path, data) -> None:
    """tmp + os.replace 原子写（与 bot 侧一致）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def changelog_list() -> dict:
    """读取全部更新记录供面板编辑（新→旧）。缺失/损坏时返回空列表。"""
    data = {}
    if CHANGELOG_FILE.exists():
        try:
            data = json.loads(CHANGELOG_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            return {"error": f"更新日志文件损坏: {e}", "entries": []}
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        entries = []
    return {
        "entries": [e for e in entries if isinstance(e, dict)],
        "change_types": CHANGELOG_CHANGE_TYPES,
        "note": "保存后 bot 立即生效（每次查询直接读盘）。分类条目类型："
                + " / ".join(CHANGELOG_CHANGE_TYPES),
    }


def changelog_save(entries) -> dict:
    """校验并全量保存更新记录快照。"""
    from . import config_editor

    if not isinstance(entries, list):
        return {"error": "entries 必须是数组"}
    now = time.time()
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            return {"error": f"第 {i + 1} 条不是对象"}
        version = str(entry.get("version") or "").strip()
        date = str(entry.get("date") or "").strip()
        if not version and not date:
            return {"error": f"第 {i + 1} 条至少需要版本号或日期"}
        if date:
            try:
                datetime.strptime(date, "%Y-%m-%d")
            except ValueError:
                return {"error": f"第 {i + 1} 条日期格式应为 YYYY-MM-DD（{date}）"}
        changes = entry.get("changes")
        if changes is None:
            changes = []
        if not isinstance(changes, list):
            return {"error": f"第 {i + 1} 条的 changes 必须是数组"}
        clean_changes = []
        for ch in changes:
            if isinstance(ch, dict):
                ctype = str(ch.get("type") or "").strip()
                text = str(ch.get("text") or "").strip()
            else:
                ctype, text = "", str(ch).strip()
            if not text:
                continue
            if ctype and ctype not in CHANGELOG_CHANGE_TYPES:
                return {"error": f"第 {i + 1} 条含未知类型「{ctype}」"
                                 f"（可选：{'/'.join(CHANGELOG_CHANGE_TYPES)}）"}
            clean_changes.append({"type": ctype, "text": text})
        if not clean_changes:
            return {"error": f"第 {i + 1} 条至少要有一条变更内容"}
        entry["version"] = version
        entry["date"] = date
        entry["changes"] = clean_changes
        # created_at 作为稳定标识（群发回写 broadcast_at 时按此匹配）
        if not isinstance(entry.get("created_at"), (int, float)):
            entry["created_at"] = now - i  # 保证同批新增也有唯一且有序的标识
        entry.pop("_class", None)

    raw = json.dumps({"entries": entries}, ensure_ascii=False, indent=2)
    result = config_editor.json_config_write(CHANGELOG_FILE, raw)
    if result.get("ok"):
        result["note"] = f"已保存 {len(entries)} 条更新记录 — bot 每次查询直接读盘，立即生效"
        _export_public_changelog(entries)
    return result


def _export_public_changelog(entries) -> None:
    """保存成功后把更新记录脱敏导出为公开站点 changelog.json（保存即同步）。

    公开数据**只含** version/date/changes —— 剔除 created_at/broadcast_at/
    _class 等内部字段，绝不导出群列表/用户/任何敏感数据。写入
    ``config.PUBLIC_SITE_DIR/changelog.json``（nginx root 指向该目录即发布）。
    导出失败静默（本模块无 logger），绝不阻断保存主流程。
    """
    try:
        public_entries = []
        for e in entries:
            public_entries.append({
                "version": str(e.get("version") or "").strip(),
                "date": str(e.get("date") or "").strip(),
                "changes": [
                    {"type": str(ch.get("type") or "").strip(),
                     "text": str(ch.get("text") or "").strip()}
                    for ch in (e.get("changes") or []) if isinstance(ch, dict)
                ],
            })
        _atomic_write_json(config.PUBLIC_SITE_DIR / "changelog.json", {
            "generated_at": time.time(),
            "change_types": CHANGELOG_CHANGE_TYPES,
            "entries": public_entries,
        })
    except Exception:
        pass  # 公开站点导出是锦上添花，失败绝不影响保存


def changelog_groups() -> dict:
    """读取 bot 导出的群列表（OneBot 群 + 官方群），供群发勾选（§16.5.1）。

    统一结构：groups[] 每项带 channel；官方群附 status/active_enabled/
    sendable/reason（关闭通知或已退群的标灰由前端处理）。
    """
    data = {}
    if KNOWN_GROUPS_FILE.exists():
        try:
            data = json.loads(KNOWN_GROUPS_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    out = []
    groups = data.get("groups") if isinstance(data, dict) else None
    if isinstance(groups, dict):
        for gid, info in groups.items():
            info = info if isinstance(info, dict) else {}
            out.append({
                "channel": "onebot",
                "id": str(gid),
                "name": info.get("name") or "",
                "member_count": info.get("member_count", 0),
                "sendable": True,
                "reason": "",
            })
    official = data.get("official_groups") if isinstance(data, dict) else None
    if isinstance(official, dict):
        for g_openid, info in official.items():
            info = info if isinstance(info, dict) else {}
            status = str(info.get("status") or "unknown")
            enabled = bool(info.get("active_enabled", True))
            sendable = status == "active" and enabled
            out.append({
                "channel": "official",
                "id": str(g_openid),
                "name": f"官方群 {_mask_openid(g_openid)}",
                "status": status,
                "active_enabled": enabled,
                "sendable": sendable,
                "reason": "" if sendable else (
                    "已退群" if status == "left" else
                    "已关闭通知" if not enabled else "状态未知"),
                "last_seen_at": info.get("last_seen_at", 0),
            })
    out.sort(key=lambda g: (g["channel"] != "onebot", g.get("name") or g["id"]))
    return {
        "groups": out,
        "updated_at": data.get("updated_at") if isinstance(data, dict) else None,
        "note": "群列表由 bot 每 10 分钟自动导出（官方群来自群内 @ 消息登记表）；"
                "若为空，请确认 bot 已连接。",
    }


def _norm_broadcast_targets(targets):
    """群发目标归一化（§16.5.2）→ list 或 {"error": …}。

    接受："all"、{channel,id} 对象、旧格式裸字符串（视为 onebot 群号）。
    """
    if not isinstance(targets, list) or not targets:
        return {"error": "请至少选择一个目标群"}
    norm = []
    for t in targets:
        if t == "all":
            norm.append("all")
            continue
        if isinstance(t, dict):
            ch = str(t.get("channel") or "onebot")
            gid = str(t.get("id") or "").strip()
        else:
            ch, gid = "onebot", str(t).strip()
        if not gid:
            continue
        if ch not in ("onebot", "official"):
            return {"error": f"未知渠道: {ch}"}
        if ch == "onebot":
            if not gid.isdigit():
                return {"error": f"非法群号: {gid}"}
        elif not re.fullmatch(r"[A-Za-z0-9_\-]{1,128}", gid):
            return {"error": f"非法官方群 openid: {gid[:12]}…"}
        norm.append({"channel": ch, "id": gid})
    if not norm:
        return {"error": "请至少选择一个目标群"}
    return norm


def _enqueue_broadcast(kind: str, ref: dict, targets) -> dict:
    """写 pending_broadcast.json（新格式 kind/ref/targets，bot 兼容旧格式）。"""
    norm = _norm_broadcast_targets(targets)
    if isinstance(norm, dict):
        return norm
    # 避免覆盖尚未被认领的上一个请求
    if PENDING_FILE.exists():
        return {"error": "已有群发请求待处理，请稍后再试"}
    _atomic_write_json(PENDING_FILE, {
        "kind": kind,
        "ref": ref,
        "targets": norm,
        "queued_at": time.time(),
    })
    return {"ok": True, "queued": True,
            "note": "已入队 — bot 轮询器约 5 秒内开始发送，可在下方查看进度"}


def changelog_broadcast(entry_index, targets) -> dict:
    """入队一次更新记录群发（写 pending_broadcast.json，bot 轮询器认领执行）。"""
    if not isinstance(entry_index, int) or entry_index < 0:
        return {"error": "entry_index 非法"}
    entries = changelog_list().get("entries", [])
    if entry_index >= len(entries):
        return {"error": "entry_index 越界（请先保存后再群发）"}
    return _enqueue_broadcast("changelog", {"entry_index": entry_index}, targets)


def changelog_broadcast_status() -> dict:
    """读取最近一次群发的执行状态（供面板轮询显示进度/结果）。

    新状态含 kind/ref/skipped/by_channel（§16.5.3.5）；旧文件原样返回。
    """
    if not STATUS_FILE.exists():
        return {}
    try:
        return json.loads(STATUS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


# ── 公告（announcements，proactive-mail-design.md §16.3）──────────────
# 与 changelog.json 分离存储；面板全量快照保存，id 服务端生成并按 id
# 定位（不用数组下标）；sent_at/last_targets 由 bot 群发成功后回写。

ANNOUNCEMENT_TITLE_MAX = 60
ANNOUNCEMENT_BODY_MAX = 2000
ANNOUNCEMENT_FORMATS = ["text", "markdown"]


def _new_announcement_id() -> str:
    return "ann_%s_%06x" % (datetime.now().strftime("%Y%m%d"),
                            random.randrange(0x1000000))


def _load_announcement_entries() -> list:
    if not ANNOUNCEMENTS_FILE.exists():
        return []
    try:
        data = json.loads(ANNOUNCEMENTS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return []
    return [e for e in entries if isinstance(e, dict)]


def announcement_list() -> dict:
    """全部公告（新→旧），供面板编辑与发送。"""
    entries = _load_announcement_entries()
    entries.sort(key=lambda e: float(e.get("created_at") or 0), reverse=True)
    return {
        "entries": entries,
        "formats": ANNOUNCEMENT_FORMATS,
        "note": "公告按 id 定位（编辑/群发均需先保存）；发送状态由 bot 群发后回写。",
    }


def announcement_save(entries) -> dict:
    """校验并全量保存公告快照；id/created_at/updated_at 服务端维护。"""
    from . import config_editor

    if not isinstance(entries, list):
        return {"error": "entries 必须是数组"}
    current = {str(e.get("id")): e for e in _load_announcement_entries()}
    now = time.time()
    seen: set = set()
    clean = []
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            return {"error": f"第 {i + 1} 条公告不是对象"}
        title = str(entry.get("title") or "").strip()
        body = str(entry.get("body") or "")
        fmt = str(entry.get("format") or "text")
        if not title:
            return {"error": f"第 {i + 1} 条公告缺少标题"}
        if len(title) > ANNOUNCEMENT_TITLE_MAX:
            return {"error": f"第 {i + 1} 条公告标题超 {ANNOUNCEMENT_TITLE_MAX} 字"}
        if not body.strip():
            return {"error": f"第 {i + 1} 条公告缺少正文"}
        if len(body) > ANNOUNCEMENT_BODY_MAX:
            return {"error": f"第 {i + 1} 条公告正文超 {ANNOUNCEMENT_BODY_MAX} 字"}
        if fmt not in ANNOUNCEMENT_FORMATS:
            return {"error": f"第 {i + 1} 条公告 format 必须是 "
                             f"{'/'.join(ANNOUNCEMENT_FORMATS)}"}
        aid = str(entry.get("id") or "").strip()
        if aid and not re.fullmatch(r"ann_[0-9A-Za-z_\-]{1,64}", aid):
            aid = ""                       # 非法 id 视为新建，重新生成
        if not aid:
            aid = _new_announcement_id()
        if aid in seen:
            return {"error": f"公告 id 重复: {aid}"}
        seen.add(aid)
        old = current.get(aid)
        created = (old or {}).get("created_at")
        if not isinstance(created, (int, float)):
            created = entry.get("created_at") \
                if isinstance(entry.get("created_at"), (int, float)) else now - i
        content_changed = (old is None
                           or str(old.get("title") or "") != title
                           or str(old.get("body") or "") != body
                           or str(old.get("format") or "text") != fmt)
        updated = now if content_changed else (old or {}).get("updated_at", now)
        clean.append({
            "id": aid, "title": title, "body": body, "format": fmt,
            "created_at": created, "updated_at": updated,
            # bot 是 sent_at/last_targets 的唯一写者：保存时原样保留旧值
            "sent_at": (old or {}).get("sent_at", 0),
            "last_targets": (old or {}).get("last_targets", []),
        })
    raw = json.dumps({"entries": clean}, ensure_ascii=False, indent=2)
    result = config_editor.json_config_write(ANNOUNCEMENTS_FILE, raw)
    if result.get("ok"):
        result["note"] = f"已保存 {len(clean)} 条公告"
        result["entries"] = clean   # 含服务端生成的 id，前端直接刷新本地状态
    return result


def announcement_broadcast(announcement_id, targets) -> dict:
    """入队一次公告群发（按 id 取内容；保存后需重新发送才用新内容）。"""
    aid = str(announcement_id or "").strip()
    if not any(e.get("id") == aid for e in _load_announcement_entries()):
        return {"error": "公告不存在（请先保存后再群发）"}
    return _enqueue_broadcast("announcement", {"announcement_id": aid}, targets)


# ── 邮件管理（mail，proactive-mail-design.md §16.4）─────────────────────
# data/mailboxes/*.json 由 bot 单写、面板**只读**；删除信件/发送系统邮件
# 写 data/mail_admin/queue/ 文件队列，bot 的 _mail_admin_worker 认领执行并
# 回写 status/（按 id 幂等；status 保留 7 天，由 bot 清理）。

MAILBOX_DIR = Path(config.bot_env("MAILBOX_DIR", str(DATA / "mailboxes")))
MAIL_ADMIN_DIR = DATA / "mail_admin"
MAIL_QUEUE_DIR = MAIL_ADMIN_DIR / "queue"
MAIL_PROC_DIR = MAIL_ADMIN_DIR / "processing"
MAIL_STATUS_DIR = MAIL_ADMIN_DIR / "status"
GREET_TARGETS_FILE = DATA / "bond_greet_targets.json"

MAIL_SYS_TITLE_MAX = 60          # 与 bot 端 _SYS_TITLE_MAX 一致
MAIL_SYS_BODY_MAX = 2000
MAIL_SYS_FORMATS = ["text", "markdown"]


def _mask_openid(openid) -> str:
    """列表脱敏（W4）：abcd…wxyz；完整 openid 只在详情页展示。"""
    s = str(openid or "")
    if len(s) <= 8:
        return (s[:2] + "…") if s else ""
    return f"{s[:4]}…{s[-4:]}"


def _safe_openid_file(openid: str) -> str:
    """与 bot 侧 mailbox.py::_safe_id 同款文件名规则。"""
    return re.sub(r"[^0-9A-Za-z_.-]", "_", str(openid or "anonymous")) + ".json"


def _load_json_file(path: Path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _mail_boxes() -> list:
    """扫描信箱目录 → [(openid, box)]；openid 取文件内字段（bot 写入）。"""
    out = []
    try:
        files = sorted(MAILBOX_DIR.glob("*.json"))
    except OSError:
        return out
    for f in files:
        box = _load_json_file(f)
        if box is None:
            continue                     # 损坏文件跳过（bot 端会降级重建）
        out.append((str(box.get("openid") or f.stem), box))
    return out


def _greet_targets() -> dict:
    """``data/bond_greet_targets.json`` → {openid: entry}。

    这是 **c2c 联系人全量登记表**（bot 每个 c2c 入站回合都会
    ``note_c2c_contact`` 落库），而信箱文件只在首次写信/系统邮件时才建档。
    用户列表必须取两者并集，否则没收到过信的用户在面板上不可见、
    无法勾选发送系统邮件（尽管 ``_mail_openid_known`` 一直允许发给他们）。
    """
    data = _load_json_file(GREET_TARGETS_FILE)
    if not isinstance(data, dict):
        return {}
    return {str(k): v for k, v in data.items()
            if str(k or "").strip() and isinstance(v, dict)}


def _mail_openid_known(openid: str) -> bool:
    """§16.4.2 校验：openid 必须存在于 mailboxes/ 或 bond_greet_targets.json。"""
    openid = str(openid or "").strip()
    if not openid:
        return False
    if (MAILBOX_DIR / _safe_openid_file(openid)).exists():
        return True
    return openid in _greet_targets()


def mail_overview() -> dict:
    """统计卡：用户数 / 信件总数 / 未读 / 系统邮件 / 关闭主动消息 / 队列待处理。

    用户数 = 信箱建档用户 ∪ c2c 联系人登记表（与 ``mail_users`` 同口径）。
    """
    users = letters = unread = system_letters = disabled = 0
    boxed = set()
    for openid, box in _mail_boxes():
        boxed.add(openid)
        users += 1
        ls = [l for l in (box.get("letters") or []) if isinstance(l, dict)]
        letters += len(ls)
        unread += sum(1 for l in ls if not l.get("read"))
        system_letters += sum(1 for l in ls if l.get("system"))
        if not box.get("active_enabled", True):
            disabled += 1
    users += sum(1 for k in _greet_targets() if k not in boxed)
    pending = 0
    for d in (MAIL_QUEUE_DIR, MAIL_PROC_DIR):
        try:
            pending += len(list(d.glob("*.json")))
        except OSError:
            pass
    return {"users": users, "letters": letters, "unread": unread,
            "system_letters": system_letters, "disabled_users": disabled,
            "queue_pending": pending}


def mail_users(query: str = "", page: int = 1, page_size: int = 20) -> dict:
    """用户列表（分页 + 搜索：openid 片段/信件 id/人格名）。

    名单 = 信箱建档用户 ∪ ``bond_greet_targets.json``（c2c 联系人全量
    登记表）。仅登记、还没信箱文件的用户以空信箱行出现（``has_mailbox``
    =False，active_enabled 视为开启——拒收状态只存在信箱文件里），排序用
    ``max(last_digest_at, last_seen)`` 保证活跃联系人不被压底。

    行内 openid 供前端取详情用，**展示必须用 openid_masked**（W4 脱敏是
    展示纪律；接口本身在登录中间件之后，面板即管理员）。
    """
    q = str(query or "").strip().lower()
    rows = []
    boxed = set()
    for openid, box in _mail_boxes():
        boxed.add(openid)
        letters = [l for l in (box.get("letters") or []) if isinstance(l, dict)]
        personas = box.get("personas") if isinstance(box.get("personas"), dict) else {}
        known = box.get("known_personas") \
            if isinstance(box.get("known_personas"), dict) else {}
        if q:
            haystack = [openid.lower()]
            haystack += [str(l.get("id") or "").lower() for l in letters]
            haystack += [str(l.get("name") or "").lower() for l in letters]
            haystack += [str(p).lower() for p in list(personas) + list(known)]
            if not any(q in h for h in haystack if h):
                continue
        rows.append({
            "openid": openid,
            "openid_masked": _mask_openid(openid),
            "personas": len(known) or len(personas),
            "letters": len(letters),
            "unread": sum(1 for l in letters if not l.get("read")),
            "system": sum(1 for l in letters if l.get("system")),
            "last_digest_date": str(box.get("last_digest_date") or ""),
            "last_digest_at": box.get("last_digest_at", 0),
            "active_enabled": bool(box.get("active_enabled", True)),
            "has_mailbox": True,
            "last_seen": 0,
        })
    # c2c 登记表里还没建档的联系人（发系统邮件的合法目标，必须可勾选）
    for openid, entry in _greet_targets().items():
        if openid in boxed:
            continue
        if q and q not in openid.lower():
            continue
        try:
            last_seen = float(entry.get("last_seen") or 0)
        except (TypeError, ValueError):
            last_seen = 0
        rows.append({
            "openid": openid,
            "openid_masked": _mask_openid(openid),
            "personas": 0,
            "letters": 0,
            "unread": 0,
            "system": 0,
            "last_digest_date": "",
            "last_digest_at": 0,
            "active_enabled": True,
            "has_mailbox": False,
            "last_seen": last_seen,
        })
    rows.sort(key=lambda r: max(float(r.get("last_digest_at") or 0),
                                float(r.get("last_seen") or 0)), reverse=True)
    return _paginate(rows, page, page_size)


def mail_user(openid: str) -> dict:
    """单用户详情：完整 openid（W4）+ 人格状态 + 信件 + 信封历史。

    信件含全文（管理员本就有权读信箱文件；页面不提供批量导出，§16.6）。
    digest 的 letter_ids 中已不在 letters[] 的标 withdrawn=「已撤回」。
    """
    openid = str(openid or "").strip()
    if not _mail_openid_known(openid):
        return {"error": "用户不存在（无此信箱，也不在主动发送登记表）"}
    path = MAILBOX_DIR / _safe_openid_file(openid)
    box = _load_json_file(path) if path.exists() else None
    if box is None:
        # 仅登记过主动发送、还没有信箱文件：返回空信箱视图
        box = {"active_enabled": True, "known_personas": {}, "personas": {},
               "letters": [], "digests": [], "last_digest_at": 0,
               "last_digest_date": ""}
    letters = sorted([l for l in (box.get("letters") or []) if isinstance(l, dict)],
                     key=lambda l: float(l.get("created_at") or 0), reverse=True)
    letter_ids = {l.get("id") for l in letters}
    digests = sorted([d for d in (box.get("digests") or []) if isinstance(d, dict)],
                     key=lambda d: float(d.get("sent_at") or 0), reverse=True)
    for d in digests:
        d["letter_states"] = [
            {"id": lid, "withdrawn": lid not in letter_ids}
            for lid in (d.get("letter_ids") or [])]
    return {
        "openid": openid,
        "openid_masked": _mask_openid(openid),
        "active_enabled": bool(box.get("active_enabled", True)),
        "last_digest_at": box.get("last_digest_at", 0),
        "last_digest_date": str(box.get("last_digest_date") or ""),
        "personas": box.get("personas") if isinstance(box.get("personas"), dict) else {},
        "known_personas": box.get("known_personas")
                          if isinstance(box.get("known_personas"), dict) else {},
        "letters": letters,
        "digests": digests,
    }


def _new_mail_op_id() -> str:
    return "mailop_%s_%04x" % (datetime.now().strftime("%Y%m%d"),
                               random.randrange(0x10000))


def _mail_pending_ops() -> list:
    """队列中待处理 + 已认领未完成的项（幂等去重与队列状态共用）。"""
    out = []
    for d, state in ((MAIL_QUEUE_DIR, "pending"), (MAIL_PROC_DIR, "processing")):
        try:
            files = sorted(d.glob("*.json"))
        except OSError:
            continue
        for f in files:
            op = _load_json_file(f) or {"id": f.stem}
            op.setdefault("id", f.stem)
            op["_state"] = state
            out.append(op)
    return out


def _mail_enqueue(op: dict) -> dict:
    """写 queue/<id>.json（原子）；bot 每 3–5s 扫描认领。"""
    op_id = str(op.get("id") or _new_mail_op_id())
    op["id"] = op_id
    op.setdefault("created_at", time.time())
    try:
        MAIL_QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return {"error": f"无法创建队列目录: {e}"}
    _atomic_write_json(MAIL_QUEUE_DIR / f"{op_id}.json", op)
    return {"ok": True, "queued": True, "id": op_id}


def mail_enqueue_delete(openid, letter_id) -> dict:
    """删除信件入队（§16.4.3；v1 不提供编辑——删除即「撤回」）。"""
    openid = str(openid or "").strip()
    letter_id = str(letter_id or "").strip()
    if not openid or not letter_id:
        return {"error": "缺少 openid / letter_id"}
    path = MAILBOX_DIR / _safe_openid_file(openid)
    box = _load_json_file(path) if path.exists() else None
    if box is None:
        return {"error": "用户信箱不存在"}
    letters = [l for l in (box.get("letters") or []) if isinstance(l, dict)]
    if not any(l.get("id") == letter_id for l in letters):
        return {"error": "信件不存在或不属于该用户"}
    for op in _mail_pending_ops():          # 幂等：同操作已在队列则拒绝（§16.7）
        if (op.get("type") == "delete_letter" and op.get("openid") == openid
                and op.get("letter_id") == letter_id):
            return {"error": "相同的删除操作已在队列中"}
    return _mail_enqueue({"type": "delete_letter", "openid": openid,
                          "letter_id": letter_id})


def mail_enqueue_send(openids, title, body, format="text") -> dict:
    """系统邮件入队（§16.4.4）；批量多选 = 每用户一个队列文件。"""
    if isinstance(openids, str):
        openids = [openids]
    if not isinstance(openids, list) or not openids:
        return {"error": "请至少选择一个用户"}
    title = str(title or "").strip()
    body = str(body or "")
    fmt = str(format or "text")
    if not title or not body.strip():
        return {"error": "标题/正文不能为空"}
    if len(title) > MAIL_SYS_TITLE_MAX:
        return {"error": f"标题超 {MAIL_SYS_TITLE_MAX} 字"}
    if len(body) > MAIL_SYS_BODY_MAX:
        return {"error": f"正文超 {MAIL_SYS_BODY_MAX} 字"}
    if fmt not in MAIL_SYS_FORMATS:
        return {"error": f"format 必须是 {'/'.join(MAIL_SYS_FORMATS)}"}
    clean: list = []
    seen: set = set()
    for o in openids:
        o = str(o or "").strip()
        if not o or o in seen:
            continue
        seen.add(o)
        if not _mail_openid_known(o):
            return {"error": f"用户不存在: {_mask_openid(o)}"}
        clean.append(o)
    if not clean:
        return {"error": "请至少选择一个用户"}
    pending = _mail_pending_ops()
    for o in clean:
        for op in pending:                  # 幂等：同用户同标题系统邮件
            if (op.get("type") == "send_system" and op.get("openid") == o
                    and str(op.get("title") or "") == title):
                return {"error":
                        f"{_mask_openid(o)} 已有同标题系统邮件在队列中"}
    ids = []
    for o in clean:
        r = _mail_enqueue({"type": "send_system", "openid": o,
                           "title": title, "body": body, "format": fmt})
        if "error" in r:
            return r
        ids.append(r["id"])
    return {"ok": True, "queued": True, "ids": ids, "count": len(ids),
            "note": f"已入队 {len(ids)} 个用户的系统邮件 — "
                    f"bot 将逐条立即发送（独立主动消息，限速 1 条/秒）"}


def mail_queue_status(limit: int = 30) -> dict:
    """队列状态：pending/processing + 最近完成的 status（前后端轮询）。"""
    pending = []
    for op in _mail_pending_ops():
        pending.append({
            "id": str(op.get("id")), "type": op.get("type"),
            "openid_masked": _mask_openid(op.get("openid")),
            "letter_id": op.get("letter_id"), "title": op.get("title"),
            "state": op.get("_state"), "created_at": op.get("created_at", 0),
        })
    pending.sort(key=lambda o: float(o.get("created_at") or 0), reverse=True)
    recent = []
    try:
        files = sorted(MAIL_STATUS_DIR.glob("*.json"),
                       key=lambda f: f.stat().st_mtime, reverse=True)
    except OSError:
        files = []
    for f in files[:max(1, int(limit))]:
        st = _load_json_file(f)
        if st:
            recent.append(st)
    return {"pending": pending, "recent": recent}

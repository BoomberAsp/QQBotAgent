"""
角色管理业务层 — 浏览 / 编辑 / 校验 / 备份 / 修复用户自创角色（UGC）。

设计：``docs/character-admin-ui-design.md``（§6.3 写路径、§6.5 修复、§6.6 体检）。

职责边界
--------
- **读**：直接用 bot 的 ``CharacterRegistry`` 拿 display_name / voice_id / 立绘，
  面板看到的与 bot 用的是同一套语义，不重复实现解析。
- **写**：只经 ``CharacterRegistry.update_character()``，面板不自己拼文件格式
  （唯一例外是「修复损坏 meta」，那也是走 ``meta_replace``）。
- **面板独占**：备份 / 版本 / lint / issues / 乐观锁 / 删除前快照 —— 这些是
  「面板视角」，不属于 bot 的类。

三条容易踩的线（都有真实事故对应）
----------------------------------
1. ``CHARACTERS_DIR`` **必须显式传参**（面板进程不 load_dotenv，见 ``webui/config.py``）。
2. 恢复备份**不能** ``shutil.copy2``（会还原 mtime，bot 的 mtime 缓存会继续喂旧内容）；
   一律走 ``update_character``（tmp + ``os.replace``）。
3. 写盘前必须有备份：没有备份宁可报错也不改（生产有真实用户）。

返回值约定：成功 ``{"ok": True, ...}``；失败 ``{"error": str, "status": int}``
（``status`` 给 webui/main.py 决定 HTTP 码：400 参数、404 不存在、409 冲突、410 已删除）。
"""

import base64
import hashlib
import json
import logging
import os
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import config
from .card_parser import (
    KNOWN_SECTIONS, apply_section_edits, insert_section, parse_card,
    remove_section, replace_preamble, section_body, set_h1, set_meta,
    unified_diff,
)
from .config_editor import GROUP_PERSONALITY_FILE

logger = logging.getLogger(__name__)

# ── 常量 ──────────────────────────────────────────────────────────

#: card.md 体积阈值（L2 每轮注入成本；内置 rubi.md 10.5 KB 是量级参考）
WARN_CARD_BYTES = 12 * 1024
MAX_CARD_BYTES = 64 * 1024
#: 语料上限（与 creation_tools._MAX_NAME_LEN 一致）
MAX_NAME_LEN = 24
MAX_SECTION_CHARS = 4000
#: 立绘源图上限（与 agent/portrait.py:MAX_SOURCE_BYTES 对齐）
MAX_PORTRAIT_BYTES = 10 * 1024 * 1024
PORTRAIT_EXTS = ("png", "jpg", "jpeg", "webp")
VISIBILITIES = ("private", "gallery")

USER_SETTINGS_FILE = config.QQBOT_DIR / "data" / "personality_settings.json"

PERSONALITIES_DIR = config.QQBOT_DIR / "agent" / "config" / "personalities"

# char_id 白名单：make_char_id 会保留 CJK/字母/数字/下划线，后面跟 _xxxxxx
_CID_RE = re.compile(r"^[\w\u4e00-\u9fff\-]{1,48}$")
#: 与 agent/personality.py:115-117 的 _VOICE_ID_RE 同口径
_VOICE_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
#: 会让角色「静默跑偏」的疑似提示词注入片段（只告警，不硬拦，避免误伤创作自由）
_INJECTION_PATTERNS = (
    re.compile(r"忽略(以上|之前|前面)的?(所有)?(指令|设定|规则)"),
    re.compile(r"ignore\s+(all\s+)?(previous|above)\s+instructions", re.I),
    re.compile(r"system\s*prompt", re.I),
    re.compile(r"developer\s*mode", re.I),
    re.compile(r"你是(管理员|开发者|系统)"),
    re.compile(r"(输出|打印|复述)(你的)?(完整)?(系统)?(指令|提示词)"),
)

#: 并发写保护（FastAPI 的 sync 端点跑在线程池里，可能真并发）
_WRITE_LOCK = threading.Lock()


# ── 基础工具 ──────────────────────────────────────────────────────

def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_id(value: str) -> str:
    """与 agent/bond.py / asset_memory.py 同款的 filesystem-safe id。"""
    return re.sub(r"[^0-9A-Za-z_.-]", "_", value or "anonymous")


def _registry():
    """每次新建实例：构造极轻（只存路径），避免跨请求共享 mtime 缓存。"""
    from agent.character_registry import CharacterRegistry
    return CharacterRegistry(characters_dir=str(config.CHARACTERS_DIR))


def _root() -> Path:
    return Path(config.CHARACTERS_DIR)


def _char_dir(cid: str, *, must_exist: bool = True) -> Optional[Path]:
    """解析并校验 char_id → 角色目录（防路径穿越）。"""
    if not cid or not _CID_RE.match(cid) or cid in (".", ".."):
        return None
    root = _root()
    try:
        target = (root / cid).resolve()
        target.relative_to(root.resolve())
    except (OSError, ValueError):
        return None
    if must_exist and not target.is_dir():
        return None
    return target


def _read_bytes(path: Path) -> Optional[bytes]:
    try:
        return path.read_bytes()
    except OSError:
        return None


def _read_card(cdir: Path) -> dict:
    """读 card.md → {content, sha256, size, mtime, encoding_ok}。"""
    path = cdir / "card.md"
    data = _read_bytes(path)
    if data is None:
        return {"error": "card.md 不存在或不可读", "status": 404}
    info = {"sha256": _sha(data), "size": len(data),
            "mtime": int(path.stat().st_mtime), "encoding_ok": True,
            "content": ""}
    try:
        info["content"] = data.decode("utf-8")
    except UnicodeDecodeError:
        info["content"] = data.decode("utf-8", errors="replace")
        info["encoding_ok"] = False
    return info


def _read_meta(cdir: Path) -> dict:
    """读 meta.json → {state, meta, error, raw, sha256, bytes}。

    ``state`` ∈ ``ok`` / ``missing`` / ``corrupt`` / ``unreadable``。
    ``corrupt`` 是 bot 侧「角色永久隐身」的根因（get_meta 返回 None）。
    """
    path = cdir / "meta.json"
    if not path.is_file():
        return {"state": "missing", "meta": None, "error": None, "raw": "",
                "sha256": "", "bytes": 0}
    data = _read_bytes(path)
    if data is None:
        return {"state": "unreadable", "meta": None, "error": "文件不可读",
                "raw": "", "sha256": "", "bytes": 0}
    state = {"sha256": _sha(data), "bytes": len(data)}
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return {**state, "state": "corrupt", "meta": None,
                "error": "不是合法 UTF-8（常见于 Windows 中转产生的 GBK 编码）",
                "raw": data[:4000].decode("utf-8", errors="replace")}
    try:
        meta = json.loads(text)
    except json.JSONDecodeError as e:
        return {**state, "state": "corrupt", "meta": None,
                "error": f"JSON 语法错误: {e}", "raw": text[:4000]}
    if not isinstance(meta, dict):
        return {**state, "state": "corrupt", "meta": None,
                "error": "顶层必须是 JSON 对象", "raw": text[:4000]}
    return {**state, "state": "ok", "meta": meta, "error": None, "raw": text}


def _known_uids() -> set:
    """已知用户集合：数据盘用户目录 + .env 两张身份表的并集。"""
    uids = set()
    try:
        for p in Path(config.USER_DATA_ROOT).iterdir():
            if p.is_dir():
                uids.add(p.name)
    except OSError:
        pass
    env = config._BOT_ENV
    for key in ("SUPERUSERS", "VIP_USERS",
                "QQ_OFFICIAL_SUPERUSERS", "QQ_OFFICIAL_VIP_USERS"):
        raw = str(env.get(key) or "").strip().strip("[]").replace('"', "").replace("'", "")
        uids.update(x.strip() for x in raw.split(",") if x.strip())
    return uids


def _role_quota(uid: str) -> Tuple[str, int]:
    try:
        from agent.permissions import PermissionManager
        pm = PermissionManager()
        role = pm.get_role(uid)
        return role.value, pm.get_max_characters(role)
    except Exception:                      # noqa: BLE001 — 面板不为配额判定失败
        return "unknown", 0


def _voice_pool() -> Tuple[List[str], str]:
    try:
        from lib.media_gen import get_voice_pool
        pool, default = get_voice_pool()
        return list(pool or []), default or ""
    except Exception:                      # noqa: BLE001
        return [], ""


def _auto_voice(cid: str) -> str:
    """``assign_voice_id`` 同款确定性分配（面板只用来展示「自动」会是哪个）。"""
    try:
        from tools.creation_tools import assign_voice_id
        return assign_voice_id(cid) or ""
    except Exception:                      # noqa: BLE001
        return ""


def _bindings(cid: str) -> dict:
    """谁把它设成了个人人格 / 哪些群绑定了它。"""
    users: List[dict] = []
    groups: List[dict] = []
    try:
        settings = json.loads(USER_SETTINGS_FILE.read_text(encoding="utf-8"))
        if isinstance(settings, dict):
            users = [{"uid": uid, "created_at": 0}
                     for uid, val in settings.items() if val == cid]
    except (OSError, json.JSONDecodeError):
        pass
    try:
        settings = json.loads(GROUP_PERSONALITY_FILE.read_text(encoding="utf-8"))
        if isinstance(settings, dict):
            groups = [{"gid": gid} for gid, val in settings.items() if val == cid]
    except (OSError, json.JSONDecodeError):
        pass
    return {"users": users, "groups": groups}


def _bond_state(uid: str, cid: str) -> Optional[dict]:
    """读 {BONDS_DIR}/{uid}.json 里该角色的羁绊（只读展示）。"""
    if not uid:
        return None
    path = Path(config.BONDS_DIR) / f"{_safe_id(uid)}.json"
    data = _read_bytes(path)
    if data is None:
        return None
    try:
        blob = json.loads(data.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    state = blob.get(cid) if isinstance(blob, dict) else None
    if not isinstance(state, dict):
        return None
    stats = state.get("stats") or {}
    return {
        "level": state.get("level"),
        "exp": state.get("exp"),
        "mood": round(float(state.get("mood") or 0), 1),
        "msgs": stats.get("msgs"),
        "gifts": stats.get("gifts"),
        "first_met": stats.get("first_met"),
        "last_seen": stats.get("last_seen"),
    }


def _dir_size(path: Path, cap: int = 5000) -> Tuple[int, int]:
    total, count = 0, 0
    try:
        for fp in path.rglob("*"):
            if not fp.is_file():
                continue
            count += 1
            if count > cap:
                break
            try:
                total += fp.stat().st_size
            except OSError:
                pass
    except OSError:
        pass
    return total, count


def estimate_tokens(text: str) -> int:
    """粗估 token：CJK 约 1 字 1 token，其它约 4 字符 1 token（页面标注为估算）。"""
    text = text or ""
    cjk = sum(1 for ch in text if ord(ch) > 0x2E7F)
    return int(cjk + (len(text) - cjk) / 4 + 0.5)


# ── 体检（lint）────────────────────────────────────────────────────

def lint_card(content: str, meta: Optional[dict], size: int,
              *, cid: str = "") -> dict:
    """保存前的静态体检。``errors`` 默认阻断保存，``warnings``/``infos`` 只提示。

    每条的 ID 对应设计文档 §6.6 的表格，便于文档与实现互相追溯。
    """
    meta = meta or {}
    doc = parse_card(content)
    errors: List[dict] = []
    warnings: List[dict] = []
    infos: List[dict] = []

    def add(bucket, code, msg):
        bucket.append({"id": code, "msg": msg})

    # L1 编码
    if "\x00" in content:
        add(errors, "L1", "内容含 NUL 字符，bot 侧会读不出来")
    # L2 H1
    if not doc.h1.strip():
        add(errors, "L2", "缺少 `# 标题`（H1）—— bot 会把 char_id 当显示名")
    # L3 体积硬限
    if size > MAX_CARD_BYTES:
        add(errors, "L3", f"体积 {size / 1024:.1f} KB 超过上限 "
                          f"{MAX_CARD_BYTES // 1024} KB（每轮都会注入系统提示词）")
    # L4 元数据值域（与 personality.py 的正则同口径）
    mode = str(meta.get("mode") or doc.meta.get("mode") or "").strip().lower()
    if mode and mode not in ("unified", "split"):
        add(errors, "L4", f"mode 只能是 unified 或 split（当前 `{mode}`）")
    task_voice = str(meta.get("task_voice") or doc.meta.get("task_voice") or "").strip().lower()
    if task_voice and task_voice not in ("character", "template"):
        add(errors, "L4", f"task_voice 只能是 character 或 template（当前 `{task_voice}`）")
    for source, voice in (("meta.json", meta.get("voice_id")),
                          ("卡片注释", doc.meta.get("voice_id"))):
        if voice and not _VOICE_RE.match(str(voice)):
            add(errors, "L4", f"{source} 的 voice_id 含非法字符（只允许字母/数字/_ . -）")

    # L5 体积告警
    if WARN_CARD_BYTES < size <= MAX_CARD_BYTES:
        add(warnings, "L5", f"体积 {size / 1024:.1f} KB 偏大（内置人格约 10 KB），"
                            "会推高每轮 token 成本")
    # L6 必备段落
    headings = {s.heading.strip().lower() for s in doc.sections}
    for need in ("core personality", "communication style"):
        if need not in headings:
            add(warnings, "L6", f"缺少 `## {need.title()}` 段落")
    # L7 外观锚点
    if "appearance anchor" not in headings:
        add(warnings, "L7", "缺少 `## Appearance Anchor` —— 出图/出语音会失去视觉锚点")
    # L8 split 模式必须保留委托要求
    if (mode or "split") == "split" and "delegate_task" not in content:
        add(warnings, "L8", "split 模式但正文没有 delegate_task 委托要求 —— "
                            "角色会凭记忆编造实时信息")
    # L9 显示名与 H1 漂移
    display = str(meta.get("display_name") or "").strip()
    if display and doc.h1.strip() and display != doc.h1.strip():
        add(warnings, "L9", f"display_name（{display}）与卡片 H1（{doc.h1}）不一致")
    # L10 音色不在池里
    pool, _default = _voice_pool()
    pinned = str(meta.get("voice_id") or "").strip()
    if pinned and pool and pinned not in pool:
        add(warnings, "L10", f"voice_id `{pinned}` 不在当前音色池，TTS 可能失败")
    # L11 疑似注入
    for pattern in _INJECTION_PATTERNS:
        hit = pattern.search(content)
        if hit:
            add(warnings, "L11", f"疑似提示词注入片段：`{hit.group(0)}`（请确认是角色设定需要）")
            break
    # L12 CRLF
    if "\r" in content:
        add(infos, "L12", "存在 CRLF 行尾 —— 保存时会统一转成 LF")
    # L13 超长段落
    for sec in doc.sections:
        if sec.bytes > MAX_SECTION_CHARS:
            add(infos, "L13", f"段落 `{sec.heading}` 约 {sec.bytes} 字符，较长")
    # L14 UGC 音色冗余不一致（卡片注释 vs meta.json）
    note_voice = str(doc.meta.get("voice_id") or "").strip()
    if pinned and note_voice and pinned != note_voice:
        add(warnings, "L14", f"meta.json 的 voice_id（{pinned}）与卡片注释"
                             f"（{note_voice}）不一致，bot 以 meta.json 为准")

    return {"errors": errors, "warnings": warnings, "infos": infos,
            "bytes": size, "tokens": estimate_tokens(content)}


# ── 列表 / 统计 ───────────────────────────────────────────────────

def _entry_info(entry: Path) -> dict:
    """扫描一个角色目录 → 列表行所需的全部信息（不含羁绊等重查询）。"""
    cid = entry.name
    card_path = entry / "card.md"
    meta_state = _read_meta(entry)
    meta = meta_state["meta"] or {}
    info: Dict[str, object] = {
        "char_id": cid,
        "source": "ugc",
        "has_card": card_path.is_file(),
        "meta_state": meta_state["state"],
        "meta_error": meta_state["error"],
        "author": str(meta.get("author") or ""),
        "display_name": str(meta.get("display_name") or ""),
        "voice_id": meta.get("voice_id") or "",
        "initial_bond_level": meta.get("initial_bond_level"),
        "visibility": meta.get("visibility") or "private",
        "bytes": 0,
        "mtime": 0,
        "portrait": False,
        "h1": "",
        "issues": [],
    }

    if info["has_card"]:
        data = _read_bytes(card_path)
        if data is not None:
            info["bytes"] = len(data)
            try:
                info["mtime"] = int(card_path.stat().st_mtime)
            except OSError:
                info["mtime"] = 0
            text = data.decode("utf-8", errors="replace")
            info["h1"] = parse_card(text).h1
    if not info["display_name"]:
        info["display_name"] = info["h1"] or cid
    info["portrait"] = any((entry / "assets" / f"reference.{e}").is_file()
                           for e in PORTRAIT_EXTS)

    issues: List[dict] = []
    if not info["has_card"]:
        issues.append({"code": "orphan_dir", "level": "warn",
                       "msg": "目录里没有 card.md（僵尸目录，可清理）"})
    if meta_state["state"] == "missing":
        issues.append({"code": "meta_missing", "level": "error",
                       "msg": "缺少 meta.json —— 该角色对所有人隐身"})
    elif meta_state["state"] in ("corrupt", "unreadable"):
        issues.append({"code": "meta_corrupt", "level": "error",
                       "msg": f"meta.json 损坏（{meta_state['error']}）—— 该角色对所有人隐身"})
    if meta.get("char_id") and str(meta.get("char_id")) != cid:
        issues.append({"code": "char_id_mismatch", "level": "warn",
                       "msg": f"meta.char_id（{meta.get('char_id')}）与目录名不一致"})
    if info["h1"] and info["display_name"] and info["display_name"] != info["h1"] \
            and meta_state["state"] == "ok":
        issues.append({"code": "name_drift", "level": "warn",
                       "msg": f"显示名（{info['display_name']}）与 H1（{info['h1']}）不一致"})
    if not info["author"] and meta_state["state"] == "ok":
        issues.append({"code": "author_missing", "level": "error",
                       "msg": "meta.json 没有 author —— 该角色对所有人隐身"})
    if not info["portrait"]:
        issues.append({"code": "no_portrait", "level": "info", "msg": "未设置参考立绘"})
    info["issues"] = issues
    return info


def _scan() -> List[dict]:
    root = _root()
    if not root.is_dir():
        return []
    out: List[dict] = []
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return []
    for entry in entries:
        if not entry.is_dir() or entry.name.startswith((".", "_")):
            continue
        out.append(_entry_info(entry))
    return out


def _builtin_rows() -> List[dict]:
    """内置人格（代码盘，只读展示 + 形象覆盖层）。"""
    reg = _registry()
    out = []
    try:
        entries = reg.list_personalities()
    except Exception:                      # noqa: BLE001
        return []
    for item in entries:
        if item.get("source") != "builtin":
            continue
        key = item["key"]
        card_path = PERSONALITIES_DIR / f"{key}.md"
        try:
            size = card_path.stat().st_size
            mtime = int(card_path.stat().st_mtime)
        except OSError:
            size, mtime = 0, 0
        portrait = reg.reference_image_path(key) or ""
        layer = "none"
        if portrait:
            layer = "override" if str(portrait).startswith(str(_root())) else "factory"
        out.append({
            "char_id": key,
            "source": "builtin",
            "has_card": card_path.is_file(),
            "meta_state": "ok",
            "author": "",
            "display_name": item.get("display_name") or key,
            "voice_id": reg.get_voice_id(key) or "",
            "bytes": size,
            "mtime": mtime,
            "portrait": bool(portrait),
            "portrait_layer": layer,
            "mode": reg.get_mode(key),
            "h1": "",
            "issues": [],
            "readonly": True,
        })
    return out


def stats() -> dict:
    rows = _scan()
    root = _root()
    issue_counts: Dict[str, int] = {}
    for row in rows:
        for issue in row["issues"]:
            issue_counts[issue["code"]] = issue_counts.get(issue["code"], 0) + 1
    authors = {row["author"] for row in rows if row["author"]}
    builtin = _builtin_rows()
    return {
        "root": str(root),
        "mounted": root.is_dir(),
        "total": len(rows),
        "authors": len(authors),
        "total_bytes": sum(int(r["bytes"]) for r in rows),
        "issue_counts": issue_counts,
        "invisible": sum(1 for r in rows if any(
            i["code"] in ("meta_corrupt", "meta_missing", "author_missing")
            for i in r["issues"])),
        "builtin": builtin,
        "builtin_total": len(builtin),
    }


def authors() -> dict:
    rows = _scan()
    grouped: Dict[str, List[dict]] = {}
    for row in rows:
        grouped.setdefault(row["author"] or "", []).append(row)
    out = []
    for uid, items in grouped.items():
        role, quota = _role_quota(uid) if uid else ("unknown", 0)
        out.append({
            "uid": uid,
            "role": role,
            "quota": quota,
            "used": len(items),
            "bytes": sum(int(i["bytes"]) for i in items),
            "last_update": max(int(i["mtime"]) for i in items),
            "over_quota": bool(quota and len(items) > quota),
        })
    out.sort(key=lambda r: (-r["used"], r["uid"]))
    return {"authors": out, "total": len(rows)}


def voices() -> dict:
    pool, default = _voice_pool()
    return {"pool": pool, "default": default,
            "note": "留空 = 按 char_id 确定性分配（同一角色永远同一音色）"}


def list_characters(author: str = "", q: str = "", source: str = "ugc",
                    issue: str = "", page: int = 1, size: int = 50) -> dict:
    """列表。``source`` ∈ ugc / builtin / all。"""
    if source == "builtin":
        rows = _builtin_rows()
    elif source == "all":
        rows = _scan() + _builtin_rows()
    else:
        rows = _scan()

    if author:
        rows = [r for r in rows if r["author"] == author]
    if q:
        needle = q.strip().lower()
        rows = [r for r in rows if needle in (
            f"{r['display_name']} {r['char_id']} {r['author']}").lower()]
    if issue == "any":
        rows = [r for r in rows if r["issues"] and
                any(i["level"] in ("error", "warn") for i in r["issues"])]
    elif issue:
        rows = [r for r in rows if any(i["code"] == issue for i in r["issues"])]

    rows.sort(key=lambda r: (-int(r["mtime"]), r["char_id"]))
    total = len(rows)
    size = max(1, min(int(size or 50), 200))
    page = max(1, int(page or 1))
    pages = max(1, (total + size - 1) // size)
    page = min(page, pages)
    return {"items": rows[(page - 1) * size: page * size], "page": page,
            "pages": pages, "total": total, "root": str(_root())}


# ── 详情 ──────────────────────────────────────────────────────────

def _card_view(cid: str) -> dict:
    """详情用的卡片视图（不存在 → error/status）。"""
    if _registry().is_builtin(cid):
        path = PERSONALITIES_DIR / f"{cid}.md"
        data = _read_bytes(path)
        if data is None:
            return {"error": "内置人格卡不存在", "status": 404}
        reg = _registry()
        return {"readonly": True, "content": data.decode("utf-8", errors="replace"),
                "sha256": _sha(data), "size": len(data),
                "mtime": int(path.stat().st_mtime), "encoding_ok": True,
                "meta": {"char_id": cid, "display_name": reg.get_display_name(cid),
                         "voice_id": reg.get_voice_id(cid) or "",
                         "mode": reg.get_mode(cid), "source": "builtin"},
                "meta_state": {"state": "ok", "error": None, "sha256": "", "bytes": 0},
                "dir": str(path.parent)}
    cdir = _char_dir(cid)
    if cdir is None:
        return {"error": "角色不存在或 ID 非法", "status": 404}
    card = _read_card(cdir)
    if "error" in card:
        return card
    meta_state = _read_meta(cdir)
    return {"readonly": False, "content": card["content"], "sha256": card["sha256"],
            "size": card["size"], "mtime": card["mtime"],
            "encoding_ok": card["encoding_ok"],
            "meta": meta_state["meta"] or {}, "meta_state": meta_state,
            "dir": str(cdir)}


def detail(cid: str) -> dict:
    view = _card_view(cid)
    if "error" in view:
        return view

    content = view["content"]
    meta = view["meta"] or {}
    doc = parse_card(content)
    readonly = view["readonly"]

    if readonly:
        issues = []
        lint = {"errors": [], "warnings": [], "infos": [],
                "bytes": view["size"], "tokens": estimate_tokens(content)}
        files = {"card_bytes": view["size"], "assets_bytes": 0, "asset_count": 0,
                 "portrait": bool(_registry().reference_image_path(cid))}
        bond, bindings = None, {"users": [], "groups": []}
    else:
        row = _entry_info(Path(view["dir"]))
        issues = row["issues"]
        lint = lint_card(content, meta, view["size"], cid=cid)
        assets_bytes, asset_count = _dir_size(Path(view["dir"]) / "assets")
        files = {"card_bytes": view["size"], "assets_bytes": assets_bytes,
                 "asset_count": asset_count,
                 "portrait": any((Path(view["dir"]) / "assets" / f"reference.{e}").is_file()
                                 for e in PORTRAIT_EXTS)}
        author = str(meta.get("author") or "")
        bond = _bond_state(author, cid) if author else None
        bindings = _bindings(cid)
        if bond is not None:
            bond = {"uid": author, **bond}

    known = {name.lower(): (doc.find(name).index if doc.find(name) else None)
             for name in KNOWN_SECTIONS}
    sections = []
    for sec in doc.sections:
        body = section_body(doc, sec.index)
        sections.append({
            "index": sec.index, "heading": sec.heading, "level": sec.level,
            "bytes": sec.bytes, "chars": len(body), "body": body,
            "known": sec.heading.strip().lower() in known,
        })

    return {
        "char_id": cid,
        "readonly": readonly,
        "dir": view["dir"],
        "display_name": str(meta.get("display_name") or doc.h1 or cid),
        "author": str(meta.get("author") or ""),
        "source": "builtin" if readonly else "ugc",
        "card": {"content": content, "sha256": view["sha256"], "size": view["size"],
                 "mtime": view["mtime"], "encoding_ok": view["encoding_ok"]},
        "meta": meta,
        "meta_state": {"state": view["meta_state"]["state"],
                       "error": view["meta_state"]["error"],
                       "sha256": view["meta_state"]["sha256"],
                       "bytes": view["meta_state"]["bytes"]},
        "parsed": {
            "h1": doc.h1,
            "meta_comment": dict(doc.meta),
            "meta_comment_raw": doc.meta_raw,
            "preamble": doc.preamble,
            "sections": sections,
        },
        "issues": issues,
        "lint": lint,
        "bindings": bindings,
        "bond": bond,
        "files": files,
        "voices": voices(),
        "auto_voice": _auto_voice(cid) if not readonly else "",
    }


# ── 备份 / 版本 ───────────────────────────────────────────────────

def _backup(cid: str, action: str, *, actor_ip: str = "") -> Optional[str]:
    """写前全量快照（card.md + meta.json + manifest）。返回快照名，失败 None。"""
    cdir = _char_dir(cid)
    if cdir is None:
        return None
    try:
        ts = time.strftime("%Y%m%d-%H%M%S")
        dest = config.CHARACTER_BACKUP_DIR / cid / ts
        n = 1
        while dest.exists():
            dest = config.CHARACTER_BACKUP_DIR / cid / f"{ts}-{n}"
            n += 1
        dest.mkdir(parents=True, exist_ok=True)
        manifest = {"action": action, "actor_ip": actor_ip, "at": int(time.time()), "ts": dest.name}
        for name in ("card.md", "meta.json"):
            src = cdir / name
            data = _read_bytes(src)
            if data is None:
                continue
            # copyfile 而非 copy2：备份里的 mtime 无意义，恢复一律走原子写
            (dest / name).write_bytes(data)
            if name == "card.md":
                manifest["card_sha256"] = _sha(data)
                manifest["card_bytes"] = len(data)
            else:
                manifest["meta_bytes"] = len(data)
        (dest / "_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        _prune_backups(cid)
        return dest.name
    except OSError as e:
        logger.warning("character backup failed for %s: %s", cid, e)
        return None


def _prune_backups(cid: str, keep: int = 20) -> None:
    """保留最近 ``keep`` 版；24 小时内的一律不删（避免「刚改坏又滚掉」）。"""
    base = config.CHARACTER_BACKUP_DIR / cid
    try:
        dirs = sorted((p for p in base.iterdir() if p.is_dir()),
                      key=lambda p: p.name, reverse=True)
    except OSError:
        return
    now = time.time()
    for old in dirs[keep:]:
        try:
            if now - old.stat().st_mtime < 86400:
                continue
            shutil.rmtree(old, ignore_errors=True)
        except OSError:
            pass


def versions(cid: str) -> dict:
    base = config.CHARACTER_BACKUP_DIR / cid
    out = []
    try:
        dirs = sorted((p for p in base.iterdir() if p.is_dir()),
                      key=lambda p: p.name, reverse=True)
    except OSError:
        dirs = []
    for d in dirs[:60]:
        manifest = {}
        try:
            manifest = json.loads((d / "_manifest.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
        out.append({"ts": d.name, "action": manifest.get("action", ""),
                    "at": manifest.get("at"), "card_bytes": manifest.get("card_bytes"),
                    "card_sha256": manifest.get("card_sha256", "")[:12],
                    "has_meta": (d / "meta.json").is_file()})
    return {"versions": out, "root": str(base)}


def version_content(cid: str, ts: str) -> dict:
    """取某版本的两份原文 + 与当前版本的 diff。"""
    if not re.match(r"^[\w.\-]{1,40}$", ts or ""):
        return {"error": "非法版本号", "status": 400}
    base = config.CHARACTER_BACKUP_DIR / cid / ts
    if not base.is_dir():
        return {"error": "版本不存在", "status": 404}
    card_b = _read_bytes(base / "card.md")
    meta_b = _read_bytes(base / "meta.json")
    card = card_b.decode("utf-8", errors="replace") if card_b is not None else ""
    meta = meta_b.decode("utf-8", errors="replace") if meta_b is not None else ""
    view = _card_view(cid)
    current = view.get("content", "") if "error" not in view else ""
    return {"ts": ts, "card": card, "meta": meta,
            "card_sha256": _sha(card_b) if card_b else "",
            "diff": unified_diff(current, card,
                                 fromfile="当前", tofile=f"版本 {ts}")}


def restore(cid: str, ts: str, *, actor_ip: str = "") -> dict:
    """回滚到某版本（同样走完整写流程：备份 → 校验 → 原子写 → 回读）。"""
    with _WRITE_LOCK:
        snap = version_content(cid, ts)
        if "error" in snap:
            return snap
        if not snap["card"].strip():
            return {"error": "该版本没有 card.md，无法回滚", "status": 400}
        meta_blob = None
        if snap["meta"].strip():
            try:
                meta_blob = json.loads(snap["meta"])
            except json.JSONDecodeError as e:
                return {"error": f"该版本的 meta.json 已损坏: {e}", "status": 400}
        cdir = _char_dir(cid)
        if cdir is None:
            return {"error": "角色不存在或 ID 非法", "status": 404}
        current = _read_card(cdir)
        bak = _backup(cid, f"restore:{ts}", actor_ip=actor_ip)
        if bak is None:
            return {"error": "备份失败，已中止回滚（宁可不改）", "status": 500}
        reg = _registry()
        try:
            reg.update_character(cid, card_content=snap["card"],
                                 meta_replace=meta_blob if meta_blob is not None else None)
        except (OSError, ValueError, FileNotFoundError) as e:
            return {"error": f"回滚写入失败: {e}", "status": 500}
        check = _read_card(cdir)
        expected = _sha(snap["card"].encode("utf-8"))
        if check.get("sha256") != expected:
            # 回读不符：用刚做的备份恢复到回滚前状态
            if "content" in current:
                try:
                    reg.update_character(cid, card_content=current["content"])
                except (OSError, ValueError):
                    pass
            return {"error": "回滚后回读校验不一致，已恢复到操作前状态", "status": 500}
        return {"ok": True, "ts": ts, "sha256": expected, "backup": bak,
                "note": "已回滚，下一轮对话生效"}


# ── 写入：原文 / 分区 / 元数据 ─────────────────────────────────────

def _conflict(current_sha: str, expected_sha: str) -> Optional[dict]:
    if expected_sha and expected_sha != current_sha:
        return {"error": (f"文件已被改动（当前 sha256 {current_sha[:12]}…，"
                          f"你基于 {expected_sha[:12]}…），请重新加载后再保存"),
                "status": 409}
    return None


def _write_card(cid: str, content: str, *, current: dict, action: str,
                force: bool, actor_ip: str) -> dict:
    """原文/分区两条路径共用的落盘流程（备份 → 原子写 → 回读校验）。"""
    cdir = _char_dir(cid)
    if cdir is None:
        return {"error": "角色不存在或 ID 非法", "status": 404}
    if not content.strip():
        return {"error": "card.md 内容为空", "status": 400}
    size = len(content.encode("utf-8"))
    lint = lint_card(content, (_read_meta(cdir)["meta"] or {}), size, cid=cid)
    if lint["errors"] and not force:
        msg = "；".join(f"[{e['id']}] {e['msg']}" for e in lint["errors"])
        return {"error": f"体检未通过 —— {msg}", "status": 400, "lint": lint}

    bak = _backup(cid, action, actor_ip=actor_ip)
    if bak is None:
        return {"error": "备份失败，已中止写入（没有备份宁可不改）", "status": 500}

    reg = _registry()
    expected = _sha(content.encode("utf-8"))
    try:
        reg.update_character(cid, card_content=content)
    except FileNotFoundError:
        return {"error": "角色目录已不存在（可能刚在 QQ 侧被删除）", "status": 410}
    except (OSError, ValueError) as e:
        return {"error": f"写入失败: {e}", "status": 500}

    check = _read_card(cdir)
    if check.get("sha256") != expected:
        try:
            if "content" in current:
                reg.update_character(cid, card_content=current["content"])
        except (OSError, ValueError):
            pass
        return {"error": "写入后回读校验不一致，已恢复到操作前状态", "status": 500}

    return {"ok": True, "sha256": expected, "mtime": check["mtime"],
            "bytes": size, "backup": bak, "lint": lint,
            "tokens": estimate_tokens(content),
            "note": "已保存 —— 下一轮对话生效"}


def save_card(cid: str, content: str, *, expected_sha256: str = "",
              force: bool = False, actor_ip: str = "") -> dict:
    """原文编辑保存。"""
    with _WRITE_LOCK:
        cdir = _char_dir(cid)
        if cdir is None:
            return {"error": "角色不存在或 ID 非法", "status": 404}
        current = _read_card(cdir)
        if "error" in current:
            return current
        conflict = _conflict(current["sha256"], expected_sha256)
        if conflict:
            return conflict
        return _write_card(cid, content, current=current, action="card",
                           force=force, actor_ip=actor_ip)


def save_sections(cid: str, sections: Optional[List[dict]] = None, *, h1: Optional[str] = None,
                  preamble: Optional[str] = None, meta_values: Optional[dict] = None,
                  expected_sha256: str = "", force: bool = False,
                  actor_ip: str = "") -> dict:
    """分区编辑保存：只提交改动的段，其余字节不动。"""
    with _WRITE_LOCK:
        cdir = _char_dir(cid)
        if cdir is None:
            return {"error": "角色不存在或 ID 非法", "status": 404}
        current = _read_card(cdir)
        if "error" in current:
            return current
        conflict = _conflict(current["sha256"], expected_sha256)
        if conflict:
            return conflict

        doc = parse_card(current["content"])
        edits: Dict[int, str] = {}
        for item in sections or []:
            try:
                index = int(item.get("index"))
                body = item.get("body")
            except (TypeError, ValueError, AttributeError):
                return {"error": "分区数据格式错误", "status": 400}
            if not isinstance(body, str):
                return {"error": "分区正文必须是字符串", "status": 400}
            if not 0 <= index < len(doc.sections):
                return {"error": f"分区索引 {index} 超出范围（共 {len(doc.sections)} 段）",
                        "status": 400}
            edits[index] = body

        raw = apply_section_edits(doc, edits)
        doc = parse_card(raw)
        if preamble is not None:
            raw = replace_preamble(doc, preamble)
            doc = parse_card(raw)
        if h1 is not None and h1.strip() and h1.strip() != doc.h1:
            raw = set_h1(doc, h1)
            doc = parse_card(raw)
        if meta_values:
            clean = {k: v for k, v in meta_values.items()
                     if k in ("mode", "task_voice", "voice_id")}
            raw = set_meta(doc, **clean)

        if raw == current["content"]:
            return {"ok": True, "unchanged": True, "sha256": current["sha256"],
                    "note": "没有任何改动"}
        return _write_card(cid, raw, current=current, action="card", force=force,
                           actor_ip=actor_ip)


def _validate_meta_patch(patch: dict) -> Optional[str]:
    """返回错误文本；``None`` = 通过。未知键直接拒绝（防止拼错字段静默无效）。"""
    if not isinstance(patch, dict) or not patch:
        return "没有要保存的字段"
    allowed = {"display_name", "visibility", "voice_id", "initial_bond_level"}
    unknown = sorted(set(patch) - allowed)
    if unknown:
        return f"不支持的字段: {', '.join(unknown)}（作者改派请走「转移归属」）"
    if "display_name" in patch:
        name = str(patch["display_name"] or "").strip()
        if not name:
            return "显示名不能为空"
        if len(name) > MAX_NAME_LEN:
            return f"显示名最长 {MAX_NAME_LEN} 字符"
        patch["display_name"] = name
    if "visibility" in patch:
        if str(patch["visibility"]) not in VISIBILITIES:
            return f"visibility 只能是 {' / '.join(VISIBILITIES)}"
    if "voice_id" in patch:
        voice = str(patch["voice_id"] or "").strip()
        if voice and not _VOICE_RE.match(voice):
            return "voice_id 只允许字母/数字/下划线/点/连字符"
        patch["voice_id"] = voice or None
    if "initial_bond_level" in patch:
        try:
            level = int(patch["initial_bond_level"])
        except (TypeError, ValueError):
            return "initial_bond_level 必须是 1–5 的整数"
        if not 1 <= level <= 5:
            return "initial_bond_level 必须是 1–5 的整数"
        patch["initial_bond_level"] = level
    return None


def save_meta(cid: str, patch: dict, *, expected_sha256: str = "",
              sync_h1: bool = False, actor_ip: str = "") -> dict:
    """元数据保存；``sync_h1`` 时同一次请求内同步卡片 H1（失败则回滚）。"""
    with _WRITE_LOCK:
        cdir = _char_dir(cid)
        if cdir is None:
            return {"error": "角色不存在或 ID 非法", "status": 404}
        current = _read_card(cdir)
        if "error" in current:
            return current
        meta_state = _read_meta(cdir)
        if meta_state["state"] != "ok":
            return {"error": f"meta.json {meta_state['state']}（{meta_state['error'] or '不可读'}）"
                             "，请先在「体检」页修复", "status": 400}
        err = _validate_meta_patch(dict(patch or {}))
        if err:
            return {"error": err, "status": 400}
        conflict = _conflict(meta_state["sha256"], expected_sha256)
        if conflict:
            return conflict

        bak = _backup(cid, "meta", actor_ip=actor_ip)
        if bak is None:
            return {"error": "备份失败，已中止写入（没有备份宁可不改）", "status": 500}

        reg = _registry()
        new_card: Optional[str] = None
        name = (patch or {}).get("display_name")
        if sync_h1 and name:
            new_card = set_h1(parse_card(current["content"]), str(name))
            try:
                reg.update_character(cid, card_content=new_card)
            except (OSError, ValueError, FileNotFoundError) as e:
                return {"error": f"同步 H1 失败: {e}", "status": 500}
        try:
            reg.update_character(cid, meta_patch=patch)
        except (OSError, ValueError, FileNotFoundError) as e:
            if new_card is not None:       # 回滚已写的 H1，避免半成品
                try:
                    reg.update_character(cid, card_content=current["content"])
                except (OSError, ValueError):
                    pass
            return {"error": f"写入 meta.json 失败: {e}", "status": 500}

        after = _read_meta(cdir)
        if after["state"] != "ok":
            return {"error": "写入后回读 meta.json 失败，已保留备份 " + str(bak),
                    "status": 500}
        return {"ok": True, "meta": after["meta"], "meta_sha256": after["sha256"],
                "backup": bak, "synced_h1": new_card is not None,
                "note": "已保存 —— 显示名/音色等元数据立即生效"}


# ── 修复 ──────────────────────────────────────────────────────────

def repair(cid: str, action: str, payload: Optional[dict] = None, *,
           actor_ip: str = "") -> dict:
    """体检页的修复动作。每个动作都先备份。"""
    payload = payload or {}
    with _WRITE_LOCK:
        cdir = _char_dir(cid, must_exist=False)
        if cdir is None:
            return {"error": "角色 ID 非法", "status": 400}

        if action == "delete_orphan":
            if (cdir / "card.md").is_file():
                return {"error": "该目录有 card.md，不是僵尸目录", "status": 400}
            if not cdir.is_dir():
                return {"error": "目录不存在", "status": 404}
            try:
                ts = time.strftime("%Y%m%d-%H%M%S")
                dest = config.CHARACTER_BACKUP_DIR / cid / f"{ts}-delete-orphan"
                dest.mkdir(parents=True, exist_ok=True)
                shutil.copytree(cdir, dest / "dir", dirs_exist_ok=True)
                shutil.rmtree(cdir, ignore_errors=True)
            except OSError as e:
                return {"error": f"清理失败: {e}", "status": 500}
            return {"ok": True, "backup": dest.name, "note": "僵尸目录已备份并清理"}

        cdir = _char_dir(cid)
        if cdir is None:
            return {"error": "角色不存在或 ID 非法", "status": 404}
        current = _read_card(cdir)
        if "error" in current:
            return current
        meta_state = _read_meta(cdir)
        meta = meta_state["meta"] or {}
        reg = _registry()

        if action == "meta_rebuild":
            author = str(payload.get("author") or meta.get("author") or "").strip()
            if not author:
                return {"error": "重建 meta.json 必须指定 author（决定谁能看到这个角色）",
                        "status": 400}
            doc = parse_card(current["content"])
            name = str(payload.get("display_name") or meta.get("display_name")
                       or doc.h1 or cid).strip()[:MAX_NAME_LEN]
            rebuilt = {
                "char_id": cid,
                "display_name": name or cid,
                "author": author,
                "created_at": int(meta.get("created_at") or time.time()),
                "visibility": str(meta.get("visibility") or "private"),
                "voice_id": _auto_voice(cid) or None,
                "source": "ugc",
                "initial_bond_level": int(meta.get("initial_bond_level") or 1),
            }
            bak = _backup(cid, "repair:meta_rebuild", actor_ip=actor_ip)
            if bak is None:
                return {"error": "备份失败，已中止", "status": 500}
            try:
                reg.update_character(cid, meta_replace=rebuilt)
            except (OSError, ValueError) as e:
                return {"error": f"重建失败: {e}", "status": 500}
            after = _read_meta(cdir)
            if after["state"] != "ok":
                return {"error": "重建后仍不可读，请手工检查 meta.json", "status": 500}
            return {"ok": True, "meta": after["meta"], "backup": bak,
                    "note": "meta.json 已重建 —— 该角色重新对作者可见"}

        if action == "set_author":
            author = str(payload.get("author") or "").strip()
            if not author:
                return {"error": "新的 author 不能为空", "status": 400}
            if author not in _known_uids() and not payload.get("force"):
                return {"error": f"未在数据盘或身份表里找到用户 {author}，"
                                 "请确认 uid 正确（转移后原作者的绑定会失效；"
                                 "确认无误可勾选「强制」）",
                        "status": 400}
            bak = _backup(cid, "repair:set_author", actor_ip=actor_ip)
            if bak is None:
                return {"error": "备份失败，已中止", "status": 500}
            try:
                reg.update_character(cid, meta_patch={"author": author})
            except (OSError, ValueError) as e:
                return {"error": f"转移失败: {e}", "status": 500}
            return {"ok": True, "author": author, "backup": bak,
                    "note": f"归属已转移到 {author}"}

        if action == "sync_name":
            direction = str(payload.get("direction") or "meta_to_h1")
            if direction == "h1_to_meta":
                doc = parse_card(current["content"])
                name = (doc.h1 or "").strip()
                if not name:
                    return {"error": "卡片没有 H1，无法反向同步", "status": 400}
                patch = {"display_name": name[:MAX_NAME_LEN]}
                new_card = None
            else:
                name = str(meta.get("display_name") or "").strip()
                if not name:
                    return {"error": "meta.json 没有 display_name，无法同步", "status": 400}
                patch = None
                new_card = set_h1(parse_card(current["content"]), name)
            bak = _backup(cid, "repair:sync_name", actor_ip=actor_ip)
            if bak is None:
                return {"error": "备份失败，已中止", "status": 500}
            try:
                if new_card is not None:
                    reg.update_character(cid, card_content=new_card)
                    return {"ok": True, "backup": bak, "note": f"H1 已同步为「{name}」"}
                reg.update_character(cid, meta_patch=patch)
                return {"ok": True, "backup": bak, "note": f"display_name 已同步为「{name}」"}
            except (OSError, ValueError) as e:
                return {"error": f"同步失败: {e}", "status": 500}

        if action == "fix_char_id":
            bak = _backup(cid, "repair:fix_char_id", actor_ip=actor_ip)
            if bak is None:
                return {"error": "备份失败，已中止", "status": 500}
            try:
                reg.update_character(cid, meta_patch={"char_id": cid})
            except (OSError, ValueError) as e:
                return {"error": f"修正失败: {e}", "status": 500}
            return {"ok": True, "backup": bak, "note": "meta.char_id 已对齐目录名"}

        if action == "set_voice_auto":
            bak = _backup(cid, "repair:set_voice_auto", actor_ip=actor_ip)
            if bak is None:
                return {"error": "备份失败，已中止", "status": 500}
            try:
                reg.update_character(cid, meta_patch={"voice_id": None})
            except (OSError, ValueError) as e:
                return {"error": f"重置失败: {e}", "status": 500}
            return {"ok": True, "backup": bak, "voice_id": _auto_voice(cid),
                    "note": "已恢复自动分配（按 char_id 确定性选音色）"}

        if action == "add_section":
            heading = str(payload.get("heading") or "").strip().lstrip("#").strip()
            if not heading:
                return {"error": "段落标题不能为空", "status": 400}
            if len(heading) > 60:
                return {"error": "段落标题过长（≤60 字符）", "status": 400}
            doc = parse_card(current["content"])
            if doc.find(heading) is not None:
                return {"error": f"已存在段落「{heading}」", "status": 400}
            after = payload.get("after")
            try:
                after = None if after in (None, "") else int(after)
            except (TypeError, ValueError):
                after = None
            if after is not None and not 0 <= after < len(doc.sections):
                after = None
            raw = insert_section(doc, heading, str(payload.get("body") or ""), after=after)
            return _write_card(cid, raw, current=current,
                               action="repair:add_section", force=True,
                               actor_ip=actor_ip)

        if action == "remove_section":
            try:
                index = int(payload.get("index"))
            except (TypeError, ValueError):
                return {"error": "缺少段落索引", "status": 400}
            doc = parse_card(current["content"])
            if not 0 <= index < len(doc.sections):
                return {"error": "段落索引超出范围", "status": 400}
            raw = remove_section(doc, index)
            return _write_card(cid, raw, current=current,
                               action="repair:remove_section", force=True,
                               actor_ip=actor_ip)

        return {"error": f"未知的修复动作: {action}", "status": 400}


# ── 形象（参考立绘）───────────────────────────────────────────────

def _portrait_view(cid: str) -> dict:
    """→ {path, layer, mime} / error。内置人格会解析覆盖层与出厂层。"""
    reg = _registry()
    if not (reg.is_builtin(cid) or _char_dir(cid) is not None):
        return {"error": "角色不存在或 ID 非法", "status": 404}
    path = reg.reference_image_path(cid)
    if not path:
        return {"error": "未设置参考立绘", "status": 404}
    layer = "override" if str(path).startswith(str(_root())) else "factory"
    ext = Path(path).suffix.lower().lstrip(".")
    mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
            "webp": "image/webp"}.get(ext, "application/octet-stream")
    return {"path": str(path), "layer": layer, "mime": mime,
            "bytes": Path(path).stat().st_size, "builtin": reg.is_builtin(cid)}


def portrait_file(cid: str) -> dict:
    return _portrait_view(cid)


def save_portrait(cid: str, data_b64: str, *, actor_ip: str = "") -> dict:
    """上传立绘：base64 JSON 传输（面板没有 python-multipart，见设计 §2.4-坑 3）。"""
    reg = _registry()
    is_builtin = reg.is_builtin(cid)
    if not is_builtin and _char_dir(cid) is None:
        return {"error": "角色不存在或 ID 非法", "status": 404}
    try:
        raw = base64.b64decode((data_b64 or "").split(",")[-1], validate=True)
    except (ValueError, TypeError):
        return {"error": "图片数据不是合法 base64", "status": 400}
    if not raw:
        return {"error": "图片为空", "status": 400}
    if len(raw) > MAX_PORTRAIT_BYTES:
        return {"error": f"原图 {len(raw) / 1048576:.1f} MB 超过 "
                         f"{MAX_PORTRAIT_BYTES // 1048576} MB 上限", "status": 400}
    try:
        from agent.portrait import describe_size, normalize_reference_image
        png, width, height = normalize_reference_image(raw)
    except ImportError as e:
        return {"error": f"图片处理模块不可用: {e}", "status": 500}
    except ValueError as e:
        return {"error": str(e), "status": 400}
    except Exception as e:                 # noqa: BLE001 — Pillow 的奇怪异常
        return {"error": f"图片处理失败: {type(e).__name__}: {e}", "status": 400}

    if not is_builtin:
        bak = _backup(cid, "portrait", actor_ip=actor_ip)
        if bak is None:
            return {"error": "备份失败，已中止写入", "status": 500}
    try:
        path = reg.set_reference_image(cid, png, "png")
    except (OSError, FileNotFoundError) as e:
        return {"error": f"写入立绘失败: {e}", "status": 500}
    try:
        desc = describe_size(width, height, len(png))
    except Exception:                      # noqa: BLE001
        desc = f"{width}×{height}"
    return {"ok": True, "path": str(path), "width": width, "height": height,
            "bytes": len(png), "describe": desc,
            "note": "内置人格的形象是全局设置（影响所有使用该人格的用户）"
                    if is_builtin else "已保存 —— 下次出图立即使用新参考图"}


def clear_portrait(cid: str, *, actor_ip: str = "") -> dict:
    reg = _registry()
    is_builtin = reg.is_builtin(cid)
    if not is_builtin and _char_dir(cid) is None:
        return {"error": "角色不存在或 ID 非法", "status": 404}
    try:
        removed = reg.clear_reference_image(cid)
    except (OSError, FileNotFoundError) as e:
        return {"error": f"清除失败: {e}", "status": 500}
    if not removed:
        return {"error": "当前没有可清除的立绘", "status": 404}
    return {"ok": True,
            "note": "已删除覆盖层，回退到出厂立绘" if is_builtin else "立绘已清除"}


# ── 注入预览 ──────────────────────────────────────────────────────

def _sync_agent_registry() -> None:
    """让 agent 侧的 CharacterRegistry **单例**也指向面板配置的角色目录。

    ``agent._build_messages`` 的 L2 注入用的是模块级单例（agent.py:592），
    它的 ``characters_dir`` 只在首次构造时从 ``os.environ`` 读取。面板进程里
    那个 env 可能缺失（config.py 已补上 setdefault），而单例也可能早已被别处
    创建 —— 所以注入预览前再做一次显式对齐，否则会预览不到 UGC 卡。
    """
    os.environ["CHARACTERS_DIR"] = str(config.CHARACTERS_DIR)
    try:
        from agent import character_registry as registry_module
        instance = registry_module._character_registry      # 可能还没创建
        if instance is not None:
            instance._char_dir = str(config.CHARACTERS_DIR)
            instance._ugc_cache.clear()
    except Exception:                      # noqa: BLE001 — 对齐失败不该炸预览
        logger.debug("sync agent registry failed", exc_info=True)


def preview(cid: str, *, role: str = "admin") -> dict:
    """把该角色真实的 L1+L2 系统提示词拼出来（不联网）。"""
    from .playground import preview_system_prompt
    _sync_agent_registry()
    result = preview_system_prompt(personality=cid, role=role)
    if "error" in result:
        return result
    card_len = 0
    view = _card_view(cid)
    if "error" not in view:
        card_len = view["size"]
    return {"personality": cid, "role": result.get("role", role),
            "content": result.get("content", ""),
            "length": result.get("length", 0), "card_bytes": card_len,
            "card_tokens": estimate_tokens(view.get("content", "")) if "error" not in view else 0}


# ── 内容审查（可选，LLM）──────────────────────────────────────────

async def review_text(text: str) -> dict:
    """复用 QQ 创作通道的审查口径（creation_tools.review_card）。"""
    text = (text or "").strip()
    if not text:
        return {"error": "内容为空", "status": 400}
    try:
        from lib.deepseek_client import DeepSeekClient
        from tools.creation_tools import review_card
        from .playground import _load_model_block
    except Exception as e:                 # noqa: BLE001
        return {"error": f"导入失败: {e}", "status": 500}
    block = _load_model_block("flash")
    if "_error" in block or not block.get("api_key") or not block.get("api_base"):
        return {"error": "未配置 FLASH_MODEL，无法做内容审查", "status": 400}
    client = DeepSeekClient(api_key=block["api_key"], api_base=block["api_base"],
                            model=block.get("model", ""))
    reason = await review_card(text, client)
    return {"ok": True, "approved": reason is None, "reason": reason or "",
            "verdict": "APPROVE" if reason is None else "REJECT"}


# ── 新建（管理员代办）────────────────────────────────────────────

def prepare_create(author: str, fields: dict, *, allow_quota_bypass: bool = False) -> dict:
    """校验字段/配额/重名并组装卡片（不落盘，便于先做内容审查）。"""
    author = str(author or "").strip()
    if not author:
        return {"error": "必须指定作者（uid）", "status": 400}
    try:
        from tools.creation_tools import (assign_voice_id, build_card,
                                          validate_fields)
        from agent.character_registry import make_char_id
    except Exception as e:                 # noqa: BLE001
        return {"error": f"导入创作模块失败: {e}", "status": 500}

    if not isinstance(fields, dict):
        return {"error": "fields 必须是对象", "status": 400}
    err = validate_fields(fields)
    if err:
        return {"error": err.replace("[Error] ", ""), "status": 400}

    reg = _registry()
    if not allow_quota_bypass:
        role, quota = _role_quota(author)
        owned = [r for r in _scan() if r["author"] == author]
        if quota and len(owned) >= quota:
            return {"error": f"作者已拥有 {len(owned)}/{quota} 个角色（{role}），"
                             "如需强建请勾选「忽略配额」", "status": 400}
    name = str(fields.get("name") or "").strip()
    if reg.name_taken(name, author):
        return {"error": f"该作者已有名为「{name}」的角色", "status": 400}

    char_id = make_char_id(name)
    voice = assign_voice_id(char_id) or None
    card = build_card(fields, voice_id=voice or "")
    meta = {
        "char_id": char_id,
        "display_name": name,
        "author": author,
        "created_at": int(time.time()),
        "visibility": "private",
        "voice_id": voice,
        "source": "ugc",
        "initial_bond_level": max(1, min(int(fields.get("initial_bond_level") or 1), 5)),
    }
    return {"ok": True, "char_id": char_id, "card": card, "meta": meta,
            "warnings": [i for i in lint_card(card, meta, len(card.encode("utf-8")))["warnings"]]}


def commit_create(prepared: dict) -> dict:
    """落盘已准备好的卡（内容审查通过后调用）。"""
    if not isinstance(prepared, dict) or not prepared.get("char_id"):
        return {"error": "缺少已准备的卡片数据", "status": 400}
    char_id = prepared["char_id"]
    if not _CID_RE.match(char_id):
        return {"error": "非法 char_id", "status": 400}
    reg = _registry()
    if reg.is_ugc(char_id):
        return {"error": f"角色 {char_id} 已存在", "status": 409}
    try:
        reg.register_character(char_id, prepared["card"], prepared["meta"])
    except FileExistsError:
        return {"error": f"角色 {char_id} 已存在", "status": 409}
    except (OSError, ValueError) as e:
        return {"error": f"落盘失败: {e}", "status": 500}
    return {"ok": True, "char_id": char_id,
            "note": f"角色「{prepared['meta'].get('display_name')}」已创建"}


# ── 删除 ──────────────────────────────────────────────────────────

def delete_character(cid: str, confirm_name: str, *, actor_ip: str = "") -> dict:
    """删除角色：整树快照 → registry.delete → purge 绑定引用。"""
    with _WRITE_LOCK:
        cdir = _char_dir(cid)
        if cdir is None:
            return {"error": "角色不存在或 ID 非法", "status": 404}
        meta_state = _read_meta(cdir)
        meta = meta_state["meta"] or {}
        reg = _registry()
        display = str(meta.get("display_name") or reg.get_display_name(cid) or cid)
        if str(confirm_name or "").strip() not in (display, cid):
            return {"error": f"确认名称不匹配（请输入「{display}」）", "status": 400}

        try:
            ts = time.strftime("%Y%m%d-%H%M%S")
            dest = config.CHARACTER_BACKUP_DIR / cid / f"{ts}-delete"
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copytree(cdir, dest / "dir", dirs_exist_ok=True)
            (dest / "_manifest.json").write_text(json.dumps(
                {"action": "delete", "actor_ip": actor_ip, "at": int(time.time()),
                 "display_name": display, "author": meta.get("author", "")},
                ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as e:
            return {"error": f"删除前备份失败，已中止: {e}", "status": 500}

        purged = 0
        try:
            purged = reg.purge_references(cid)
            reg.delete(cid)
        except OSError as e:
            return {"error": f"删除失败（备份在 {dest.name}）: {e}", "status": 500}
        return {"ok": True, "backup": dest.name, "purged": purged, "display_name": display,
                "note": f"已删除「{display}」；清理了 {purged} 条绑定引用，"
                        "羁绊数据（bonds/）按设计保留"}

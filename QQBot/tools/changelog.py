"""更新记录（更新日志）存储与格式化。

面向用户的更新记录数据源，与开发者向的 ``docs/更新日志.md`` 分开维护。
WebUI（``webui/data_reader.py``）是唯一编辑入口，bot 侧只读并把内容
格式化为 **结构化纯文本** 发给 QQ（不使用 markdown —— 见 SOUL.md 规则 #8）。

本模块是纯 IO / 格式化，不依赖 nonebot，可被命令拦截器、LLM 工具与后台
群发轮询器共用。所有写入均为原子写（tmp + ``os.replace``）。

数据目录 ``QQBot/data/changelog/``：
  - ``changelog.json``          更新记录主数据（entries 数组，新→旧）
  - ``known_groups.json``       群列表（群号→{name, member_count}），bot 写、面板读
  - ``pending_broadcast.json``  群发请求队列，面板写、bot 轮询认领
  - ``broadcast_status.json``   群发结果回写，bot 写、面板读
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional

# ── 路径 ────────────────────────────────────────────────────────────
# tools/ 的上级即 QQBot/，data 目录与 check_redeem_code.py 保持一致的相对定位。
_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR = os.path.join(_TOOLS_DIR, "..", "data", "changelog")

CHANGELOG_FILE = os.path.join(_DATA_DIR, "changelog.json")
KNOWN_GROUPS_FILE = os.path.join(_DATA_DIR, "known_groups.json")
PENDING_FILE = os.path.join(_DATA_DIR, "pending_broadcast.json")
STATUS_FILE = os.path.join(_DATA_DIR, "broadcast_status.json")

# 变更条目的类型标签（WebUI 下拉；此处仅作展示/校验参考，不强制）
CHANGE_TYPES = ["新增", "修复", "优化", "调整", "移除"]

# 命令 /update record d 或 /更新日志 d 中 d 的取值范围
DEFAULT_COUNT = 3
MAX_COUNT = 20


# ── 原子 IO ─────────────────────────────────────────────────────────

def _atomic_write_json(path: str, data: Any) -> None:
    """tmp + os.replace 原子写，避免读到半截文件（与 memory.py 同模式）。"""
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _read_json(path: str, default: Any) -> Any:
    """读 JSON；文件缺失或损坏时返回 default（绝不抛异常）。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


# ── 更新记录读写 ────────────────────────────────────────────────────

def load_entries() -> List[Dict[str, Any]]:
    """返回全部更新记录（entries 数组）。缺失/损坏时返回空列表。"""
    data = _read_json(CHANGELOG_FILE, {})
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return []
    return [e for e in entries if isinstance(e, dict)]


def save_entries(entries: List[Dict[str, Any]]) -> None:
    """全量快照写入 entries（保留 envelope 结构）。"""
    _atomic_write_json(CHANGELOG_FILE, {"entries": entries})


def _sort_key(entry: Dict[str, Any]):
    """按 created_at 降序（新→旧）；无 created_at 时回退到 date 字符串。"""
    created = entry.get("created_at")
    if isinstance(created, (int, float)):
        return (1, float(created))
    date = str(entry.get("date") or "")
    return (0, date)


def get_recent(d: int = DEFAULT_COUNT) -> List[Dict[str, Any]]:
    """返回最近最多 d 条更新记录（新→旧）。d 被 clamp 到 1..MAX_COUNT。"""
    try:
        d = int(d)
    except (TypeError, ValueError):
        d = DEFAULT_COUNT
    d = max(1, min(d, MAX_COUNT))
    entries = sorted(load_entries(), key=_sort_key, reverse=True)
    return entries[:d]


# ── 格式化（结构化纯文本，绝不含 markdown 语法）──────────────────────

def _format_entry_lines(entry: Dict[str, Any]) -> List[str]:
    """把单条更新记录渲染为若干纯文本行。"""
    version = str(entry.get("version") or "").strip()
    date = str(entry.get("date") or "").strip()

    if version and date:
        head = f"【{version}】 {date}"
    elif version:
        head = f"【{version}】"
    elif date:
        head = f"【{date}】"
    else:
        head = "【更新】"

    lines = [head]
    changes = entry.get("changes")
    if isinstance(changes, list) and changes:
        for ch in changes:
            if isinstance(ch, dict):
                ctype = str(ch.get("type") or "").strip()
                text = str(ch.get("text") or "").strip()
            else:
                ctype, text = "", str(ch).strip()
            if not text:
                continue
            lines.append(f"  • {ctype}：{text}" if ctype else f"  • {text}")
    else:
        # 无结构化条目时，回退到 note/text 字段（若面板写了的话）
        note = str(entry.get("note") or entry.get("text") or "").strip()
        if note:
            for ln in note.splitlines():
                if ln.strip():
                    lines.append(f"  • {ln.strip()}")
    return lines


def format_for_qq(entries: List[Dict[str, Any]], requested: Optional[int] = None) -> List[str]:
    """把更新记录列表格式化为可直接 _send_text_chunks 的纯文本行。

    entries 为空时返回空态提示。requested 用于标题里说明"最近 N 条"。
    """
    if not entries:
        return ["暂无更新记录。"]

    if requested:
        head = f"更新记录（最近 {len(entries)} 条）："
    else:
        head = f"更新记录（共 {len(entries)} 条）："

    lines: List[str] = [head, ""]
    for i, entry in enumerate(entries):
        if i > 0:
            lines.append("")
        lines.extend(_format_entry_lines(entry))
    return lines


def format_announcement(entry: Dict[str, Any]) -> List[str]:
    """群发公告抬头 + 单条记录内容（结构化纯文本，供广播使用）。"""
    return ["📢 更新公告", ""] + _format_entry_lines(entry)


# ── 群发状态回写 ────────────────────────────────────────────────────

def mark_broadcast(created_at: Any, ts: Optional[float] = None) -> bool:
    """把某条记录标记为已群发（按 created_at 匹配）。成功返回 True。"""
    if ts is None:
        ts = time.time()
    entries = load_entries()
    hit = False
    for e in entries:
        if e.get("created_at") == created_at:
            e["broadcast_at"] = ts
            hit = True
    if hit:
        save_entries(entries)
    return hit


# ── 群列表（bot 写、面板读）─────────────────────────────────────────

def write_known_groups(groups: Dict[str, Dict[str, Any]]) -> None:
    """原子写入群列表快照。groups: {group_id(str): {name, member_count}}。"""
    _atomic_write_json(KNOWN_GROUPS_FILE, {
        "groups": groups,
        "updated_at": time.time(),
    })


def read_known_groups() -> Dict[str, Any]:
    return _read_json(KNOWN_GROUPS_FILE, {})


# ── 群发队列（面板写、bot 认领）─────────────────────────────────────

def claim_pending() -> Optional[Dict[str, Any]]:
    """若存在待处理群发请求则原子认领（改名为 .processing）并返回其内容。

    单进程单轮询器下用改名做认领，避免重复处理；认领后需调用
    ``finish_pending_claim()`` 清理。无请求或已被认领时返回 None。
    """
    if not os.path.exists(PENDING_FILE):
        return None
    processing = PENDING_FILE + ".processing"
    try:
        # 原子改名认领：若 .processing 已存在说明上一轮未清理，先覆盖
        os.replace(PENDING_FILE, processing)
    except OSError:
        return None
    payload = _read_json(processing, None)
    if not isinstance(payload, dict):
        # 内容损坏，直接丢弃认领文件
        _safe_remove(processing)
        return None
    return payload


def finish_pending_claim() -> None:
    """清理认领文件（群发流程结束后调用）。"""
    _safe_remove(PENDING_FILE + ".processing")


def recover_stale_claim() -> bool:
    """启动恢复：删除上次进程崩溃可能残留的 ``.processing`` 认领文件。

    正常流程下认领文件会在 ``finish_pending_claim()`` 被清理；若 bot 在群发
    途中退出，``.processing`` 会残留、且 ``broadcast_status.json`` 可能永远停在
    ``"sending"``。轮询器启动时调用本函数清理残留，返回是否清理了文件（调用方
    据此把卡住的 sending 状态改写为中断，避免面板无限轮询）。
    """
    processing = PENDING_FILE + ".processing"
    if os.path.exists(processing):
        _safe_remove(processing)
        return True
    return False


def write_status(status: Dict[str, Any]) -> None:
    """回写群发结果，供 WebUI 轮询显示。"""
    status = dict(status)
    status.setdefault("updated_at", time.time())
    _atomic_write_json(STATUS_FILE, status)


def read_status() -> Dict[str, Any]:
    return _read_json(STATUS_FILE, {})


def _safe_remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass

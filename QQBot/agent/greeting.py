"""
主动问候状态（Bond P3b）— 纯 JSON 簿记，无 nonebot 依赖，可独立单测。

QQ 官方途径的主动消息（不带 msg_id 的 ``send_to_c2c``）受平台**每日/每月
小额配额 + 内容审核**约束，且 c2c 主动推送只认 **user_openid**（仅 c2c 入站
可捕获；群 member_openid 不可用于 c2c）。本模块因此维护两份状态文件
（均为原子写 + 损坏降级 {}，与 bond.py / status_heartbeat 同款纪律）：

- **候选索引** ``data/bond_greet_targets.json``：
  ``{openid: {"last_seen": ts, "last_greeted": ts?, "greet_count": n?}}``。
  仅 c2c 入站回合调用 :func:`note_c2c_contact` 落库；HEARTBEAT 只问候
  索引内的用户（``last_greeted`` 供冷却门控）。
- **主动配额** ``data/official_active_quota.json``：
  ``{openid: {"month": "YYYY-MM", "month_count": n, "day": "YYYY-MM-DD",
  "day_count": n, "last_active": ts}}``，按自然日/自然月滚动重置，
  叠加平台配额与设计约束（Bond-System-Design.md §7.3 ≤1 次/天/用户）。

选靶门控 :func:`pick_greet_targets` 是纯函数（rng / now 均可注入）：
好感等级门 + 冷却门 + 概率门 + 批量上限。LLM 文案生成与真实发送留在
plugins/qq_official.py（需要 nonebot / bot 句柄，不进本模块）。
"""
from __future__ import annotations

import json
import os
import random
import time
from datetime import datetime
from typing import Callable, Dict, List, Optional, Tuple

# ── JSON 原子读写（损坏 → {}，绝不抛给调用方）──────────────────────


def _read_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, IOError, OSError, ValueError):
        return {}


def _atomic_write_json(path: str, data: dict) -> bool:
    d = os.path.dirname(path)
    try:
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
        return True
    except (IOError, OSError):
        return False


# ── 候选索引（c2c 入站落库）────────────────────────────────────────


def note_c2c_contact(path: str, openid: str, *, now: Optional[float] = None) -> None:
    """Record/refresh a c2c contact as a greetable candidate. Best-effort."""
    openid = str(openid or "").strip()
    if not openid:
        return
    now = time.time() if now is None else float(now)
    data = _read_json(path)
    entry = data.get(openid)
    if not isinstance(entry, dict):
        entry = {}
    entry["last_seen"] = now
    data[openid] = entry
    _atomic_write_json(path, data)


def load_targets(path: str) -> Dict[str, dict]:
    """{openid: entry} of candidates; non-dict junk values are filtered."""
    data = _read_json(path)
    return {k: v for k, v in data.items()
            if isinstance(k, str) and k.strip() and isinstance(v, dict)}


def mark_greeted(path: str, openid: str, *, now: Optional[float] = None) -> None:
    """Stamp last_greeted (+count) after a successful active greeting."""
    openid = str(openid or "").strip()
    if not openid:
        return
    now = time.time() if now is None else float(now)
    data = _read_json(path)
    entry = data.get(openid)
    if not isinstance(entry, dict):
        entry = {}
    entry["last_greeted"] = now
    try:
        entry["greet_count"] = int(entry.get("greet_count", 0)) + 1
    except (TypeError, ValueError):
        entry["greet_count"] = 1
    data[openid] = entry
    _atomic_write_json(path, data)


# ── 平台主动配额（自然日/自然月滚动重置）───────────────────────────


def _month_key(now: float) -> str:
    return datetime.fromtimestamp(now).strftime("%Y-%m")


def _day_key(now: float) -> str:
    return datetime.fromtimestamp(now).strftime("%Y-%m-%d")


def quota_allows(data: dict, openid: str, *, now: float,
                 daily_cap: int = 1, monthly_cap: int = 4) -> bool:
    """Pure check: is one more active message allowed for ``openid``?"""
    rec = data.get(openid)
    if not isinstance(rec, dict):
        return True
    try:
        if rec.get("month") == _month_key(now) and \
                int(rec.get("month_count", 0)) >= int(monthly_cap):
            return False
        if rec.get("day") == _day_key(now) and \
                int(rec.get("day_count", 0)) >= int(daily_cap):
            return False
    except (TypeError, ValueError):
        return True  # 脏数据 → 放行由平台侧兜底，不阻断
    return True


def quota_record(data: dict, openid: str, *, now: float) -> None:
    """Pure mutation: count one active send (rolls day/month windows)."""
    rec = data.get(openid)
    if not isinstance(rec, dict):
        rec = {}
        data[openid] = rec
    if rec.get("month") != _month_key(now):
        rec["month"] = _month_key(now)
        rec["month_count"] = 0
    if rec.get("day") != _day_key(now):
        rec["day"] = _day_key(now)
        rec["day_count"] = 0
    try:
        rec["month_count"] = int(rec.get("month_count", 0)) + 1
        rec["day_count"] = int(rec.get("day_count", 0)) + 1
    except (TypeError, ValueError):
        rec["month_count"] = 1
        rec["day_count"] = 1
    rec["last_active"] = now


def active_allowed(path: str, openid: str, *, now: Optional[float] = None,
                   daily_cap: int = 1, monthly_cap: int = 4) -> bool:
    """File-backed :func:`quota_allows`."""
    now = time.time() if now is None else float(now)
    return quota_allows(_read_json(path), openid, now=now,
                        daily_cap=daily_cap, monthly_cap=monthly_cap)


def record_active_send(path: str, openid: str, *,
                       now: Optional[float] = None) -> None:
    """File-backed :func:`quota_record` — call only AFTER a successful send."""
    openid = str(openid or "").strip()
    if not openid:
        return
    now = time.time() if now is None else float(now)
    data = _read_json(path)
    quota_record(data, openid, now=now)
    _atomic_write_json(path, data)


# ── 选靶门控（纯函数，rng/now 可注入）──────────────────────────────


def pick_greet_targets(
    entries: Dict[str, dict],
    *,
    state_for: Callable[[str], Optional[dict]],
    now: Optional[float] = None,
    min_level: int = 5,
    cooldown_days: float = 7,
    probability: float = 0.25,
    rng: Callable[[], float] = random.random,
    max_batch: int = 5,
) -> List[Tuple[str, dict]]:
    """Candidates passing every gate: [(openid, bond_state)].

    门控顺序（先廉价后昂贵）：
    1. 存在羁绊状态（``state_for`` 返回 None → 非养成人格/无记录，跳过）；
    2. 好感等级 ≥ ``min_level``（§7.2 提议 Lv5 密友起）；
    3. 距上次问候 ≥ ``cooldown_days`` 天；
    4. 概率门（``rng() < probability``，低频不打扰）；
    5. 批量上限 ``max_batch``（单次 HEARTBEAT  pass 最多问候几人）。

    平台每日/每月配额不在这里查——发送前由 :func:`active_allowed` 兜底，
    保持本函数纯内存、可单测。
    """
    now = time.time() if now is None else float(now)
    out: List[Tuple[str, dict]] = []
    for openid, entry in entries.items():
        if len(out) >= int(max_batch):
            break
        state = state_for(openid)
        if not isinstance(state, dict):
            continue
        try:
            if int(state.get("level", 1)) < int(min_level):
                continue
        except (TypeError, ValueError):
            continue
        try:
            last_greeted = float(entry.get("last_greeted") or 0)
        except (TypeError, ValueError):
            last_greeted = 0.0
        if last_greeted and (now - last_greeted) < float(cooldown_days) * 86400:
            continue
        if rng() >= float(probability):
            continue
        out.append((openid, state))
    return out

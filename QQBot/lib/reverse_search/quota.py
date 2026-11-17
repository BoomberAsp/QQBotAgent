"""找图配额与结果缓存（方案坑5）。

- 同用户 30s 冷却、20 次/天；全服 100 次/天（保护 SauceNAO 匿名小配额）
- 图片内容 sha256 缓存 30 天：同图重发直接回缓存，不耗当日次数
- 状态落 data/find_source/quota.json，原子写；evaluate/record 为纯函数可离线测
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

COOLDOWN_S = 30.0
USER_DAILY = 20
GLOBAL_DAILY = 100
CACHE_TTL_S = 30 * 86400
PARTIAL_CACHE_TTL_S = 3600
SEARCH_POLICY_VERSION = 2

_DEFAULT_PATH = (Path(__file__).resolve().parents[2]
                 / "data" / "find_source" / "quota.json")


def image_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def cache_key(data: bytes) -> str:
    return f"{SEARCH_POLICY_VERSION}:{image_hash(data)}"


def _fresh() -> dict:
    return {"users": {}, "global": {"day": "", "count": 0}, "cache": {}}


def evaluate(state: dict, uid: str, now: float, day: str) -> str:
    """配额校验，返回拒绝原因（空串=放行）。不修改 state。"""
    user = state.get("users", {}).get(uid)
    if user:
        wait = COOLDOWN_S - (now - user.get("last", 0))
        if wait > 0:
            return f"找图太频繁，请 {int(wait) + 1} 秒后再试"
        if user.get("day") == day and user.get("count", 0) >= USER_DAILY:
            return f"今日找图次数已用完（{USER_DAILY} 次/天），明天再来"
    g = state.get("global", {})
    if g.get("day") == day and g.get("count", 0) >= GLOBAL_DAILY:
        return "今日全服找图额度已用完，明天再来"
    return ""


def record_use(state: dict, uid: str, now: float, day: str) -> None:
    users = state.setdefault("users", {})
    user = users.get(uid)
    if not user or user.get("day") != day:
        user = {"day": day, "count": 0, "last": 0}
        users[uid] = user
    user["count"] += 1
    user["last"] = now
    g = state.setdefault("global", {"day": "", "count": 0})
    if g.get("day") != day:
        g["day"], g["count"] = day, 0
    g["count"] += 1


def cache_get(state: dict, digest: str, now: float) -> dict | None:
    entry = state.get("cache", {}).get(digest)
    if not entry:
        return None
    if now - entry.get("ts", 0) > entry.get("ttl", CACHE_TTL_S):
        return None
    payload = entry.get("result")
    return payload if isinstance(payload, dict) else None


def cache_put(state: dict, digest: str, payload: dict, now: float,
              *, ttl: float = CACHE_TTL_S) -> None:
    state.setdefault("cache", {})[digest] = {"ts": now, "ttl": ttl, "result": payload}


def prune(state: dict, now: float) -> None:
    cache = state.get("cache", {})
    for digest in [d for d, e in cache.items()
                   if now - e.get("ts", 0) > e.get("ttl", CACHE_TTL_S)]:
        del cache[digest]


class QuotaStore:
    """JSON 文件读写（原子写）。单进程 bot，调用方自行串行化。"""

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else _DEFAULT_PATH

    def load(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as f:
                state = json.load(f)
            if isinstance(state, dict):
                state.setdefault("users", {})
                state.setdefault("global", {"day": "", "count": 0})
                state.setdefault("cache", {})
                return state
        except (OSError, ValueError):
            pass
        return _fresh()

    def save(self, state: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
        os.replace(tmp, self.path)


def now_and_day() -> tuple[float, str]:
    now = time.time()
    return now, time.strftime("%Y-%m-%d", time.localtime(now))

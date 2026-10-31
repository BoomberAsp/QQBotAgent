"""
Group Registry — 官方群登记表（proactive-mail-design.md v0.5 §4.5 / §7.3）。

平台没有「机器人所在群列表」API，只能事件驱动 + 入站补偿：
- v1 只消费 ``GROUP_AT_MESSAGE_CREATE`` 做**纯记账** upsert（零发送风险，
  越早积累存量群越好）；ADD/DEL/REJECT/RECEIVE 事件与群发送归 P1。
- upsert **节流**：仅当群缺失或字段实际变化时才写盘，避免活跃群每条 @ 都
  造成文件 churn。

存储：单文件 ``data/group_registry.json``，原子写（.tmp + os.replace），
损坏降级视为空表。只存 group_openid（平台不提供群名；需要展示时走
``GET /v2/groups/{group_openid}/info`` 按需拉取）。

status: ``active`` / ``left`` / ``unknown``（unknown = 发送失败但不确定是否
退群的中间态，下次入站再修正）。发送前检查 ``status == active and
active_enabled``。

同步纯函数模块：不依赖 nonebot / asyncio，可独立单测。
"""

import json
import os
import time
from typing import Any, Dict, List, Optional


def _new_group(ts: float) -> Dict[str, Any]:
    return {
        "status": "active",
        "added_at": ts,
        "last_seen_at": ts,
        "active_enabled": True,
        "left_at": 0,
    }


class GroupRegistry:
    """Single-file registry of official group openids."""

    def __init__(self, path: str):
        self.path = path
        d = os.path.dirname(os.path.abspath(path))
        if d:
            os.makedirs(d, exist_ok=True)

    # ── 低层 I/O ─────────────────────────────────────────────────

    @staticmethod
    def _atomic_write_json(path: str, data: Any) -> bool:
        try:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
            return True
        except (IOError, OSError):
            return False

    def load(self) -> Dict[str, Any]:
        """读取整表；损坏/缺失 → 空表。"""
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict) and isinstance(raw.get("groups"), dict):
                return {"version": 1, "groups": raw["groups"]}
        except (json.JSONDecodeError, IOError, OSError):
            pass
        return {"version": 1, "groups": {}}

    def _save(self, data: Dict[str, Any]) -> bool:
        return self._atomic_write_json(self.path, data)

    # ── 记账 ─────────────────────────────────────────────────────

    def upsert_seen(self, group_openid: str, now: Optional[float] = None) -> bool:
        """``GROUP_AT_MESSAGE_CREATE`` 入站补偿建档（§4.5）。

        节流：仅当群缺失或 last_seen_at 实际变化（≥60s 粒度）才写盘。
        返回是否写盘。
        """
        if not group_openid:
            return False
        now = now if now is not None else time.time()
        data = self.load()
        groups = data["groups"]
        entry = groups.get(group_openid)
        if not isinstance(entry, dict):
            groups[group_openid] = _new_group(now)
            return self._save(data)
        changed = False
        # 60s 粒度节流：活跃群每条 @ 都写会造成文件 churn
        if now - float(entry.get("last_seen_at", 0) or 0) >= 60:
            entry["last_seen_at"] = now
            changed = True
        if entry.get("status") == "left":
            # 重新入站 = 之前 DEL 事件误报或重新拉入，修正为 active
            entry["status"] = "active"
            entry["left_at"] = 0
            changed = True
        if entry.get("status") == "unknown":
            entry["status"] = "active"
            changed = True
        if changed:
            return self._save(data)
        return False

    def mark_left(self, group_openid: str, now: Optional[float] = None) -> bool:
        """``GROUP_DEL_ROBOT`` / 发送 40034105 → status=left（保留记录，不删）。P1。"""
        now = now if now is not None else time.time()
        data = self.load()
        entry = data["groups"].get(group_openid)
        if isinstance(entry, dict):
            if entry.get("status") == "left":
                return False
            entry["status"] = "left"
            entry["left_at"] = now
        else:
            entry = _new_group(now)
            entry["status"] = "left"
            entry["left_at"] = now
            data["groups"][group_openid] = entry
        return self._save(data)

    def mark_unknown(self, group_openid: str) -> bool:
        """发送失败但不确定是否退群的中间态（§7.3）。P1。"""
        data = self.load()
        entry = data["groups"].get(group_openid)
        if not isinstance(entry, dict) or entry.get("status") == "unknown":
            return False
        entry["status"] = "unknown"
        return self._save(data)

    def set_active_enabled(self, group_openid: str, enabled: bool) -> bool:
        """``GROUP_MSG_REJECT`` / ``GROUP_MSG_RECEIVE`` 开关（§4.5）。P1。"""
        data = self.load()
        entry = data["groups"].get(group_openid)
        if not isinstance(entry, dict):
            entry = _new_group(time.time())
            entry["active_enabled"] = bool(enabled)
            data["groups"][group_openid] = entry
            return self._save(data)
        if bool(entry.get("active_enabled", True)) == bool(enabled):
            return False
        entry["active_enabled"] = bool(enabled)
        return self._save(data)

    # ── 查询 ─────────────────────────────────────────────────────

    def get(self, group_openid: str) -> Optional[Dict[str, Any]]:
        entry = self.load()["groups"].get(group_openid)
        return entry if isinstance(entry, dict) else None

    def list_active(self) -> List[str]:
        """发送前筛：``status == active and active_enabled``。"""
        return [
            gid for gid, e in self.load()["groups"].items()
            if isinstance(e, dict)
            and e.get("status") == "active"
            and bool(e.get("active_enabled", True))
        ]

    def count(self) -> int:
        return len(self.load()["groups"])

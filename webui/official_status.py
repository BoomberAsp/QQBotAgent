"""
QQ 官方路由状态 — 面板侧只读聚合器。

bot 进程（QQBot/plugins/qq_official.py）在 startup / bot connect /
bot disconnect 时写 QQBot/data/official_status.json，并在进程存活期间
每 60s 刷新 written_at 心跳。面板与 bot 是独立进程（同 changelog 文件
队列范式），这里只读该文件，按 **mtime** 判定新鲜度：

  age ≤ 150s          → 原样返回 bot 写的 state（connected/disconnected/starting）
  150s < age ≤ 600s   → "stale"（心跳滞后，进程可能卡死/刚被杀）
  age > 600s 或无文件 → "unknown"（官方路由未启用或从未运行）

150s = 心跳周期 60s 的 2.5 倍，容忍两次丢写；绝不写状态文件。
另附 appid_env（解析 .env 的 QQ_BOTS），bot 尚未连上时前端也能展示
配置的 appid，并据此区分「未启用」与「已配置但未连接」。
"""

import json
import time

from . import config

STATUS_FILE = config.QQBOT_DIR / "data" / "official_status.json"
_FRESH_SECONDS = 150
_STALE_SECONDS = 600


def _appid_from_env() -> str | None:
    """QQ_BOTS JSON 数组第一个元素的 id；缺失/损坏返回 None。"""
    try:
        bots = json.loads(config.bot_env("QQ_BOTS"))
        if isinstance(bots, list) and bots:
            return str(bots[0].get("id") or "") or None
    except (ValueError, AttributeError, TypeError):
        pass
    return None


async def status() -> dict:
    """聚合状态，供 GET /api/official/status 与 /api/dashboard 消费。"""
    appid_env = _appid_from_env()
    base = {
        "state": "unknown",
        "appid": None,
        "appid_env": appid_env,
        "connected_at": None,
        "last_message_at": None,
        "messages_total": 0,
        "heartbeat_age": None,
    }
    try:
        mtime = STATUS_FILE.stat().st_mtime
        with open(STATUS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("status file is not an object")
    except (OSError, ValueError):
        return base

    age = time.time() - mtime
    if age > _STALE_SECONDS:
        state = "unknown"
    elif age > _FRESH_SECONDS:
        state = "stale"
    else:
        state = data.get("state") or "unknown"
    base.update({
        "state": state,
        "appid": data.get("appid"),
        "connected_at": data.get("connected_at"),
        "last_message_at": data.get("last_message_at"),
        "messages_total": int(data.get("messages_total") or 0),
        "heartbeat_age": int(age),
    })
    return base

"""
offline_logger — 把 bot_offline / bot_* 通知事件的原始 payload 以 UTF-8 落盘。

背景：NapCat 在 QQ 服务端踢下线时会发 OneBot notice（notice_type=bot_offline），
踢下线原因（tag/message 字段）只存在于该事件里；控制台经 screen 抓取会乱码，
QQ 自身的 qqxlog 又是加密的。本插件把事件原文写入 QQBot/logs/bot_offline.log，
下次被踢即可直接读到原因。

只监听、不 block，不影响任何其它插件。
"""

import json
from datetime import datetime
from pathlib import Path

from nonebot import logger
from nonebot.adapters.onebot.v11 import Bot, Event
from nonebot.plugin import on_notice
from nonebot.rule import Rule

_LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
_LOG_FILE = _LOG_DIR / "bot_offline.log"


async def _is_bot_notice(event: Event) -> bool:
    # bot_offline（被踢/掉线）、bot_online（重新上线）等生命周期通知
    return str(getattr(event, "notice_type", "")).startswith("bot_")


_bot_notice = on_notice(rule=Rule(_is_bot_notice), priority=1, block=False)


@_bot_notice.handle()
async def _(bot: Bot, event: Event):
    try:
        raw = (
            event.model_dump_json()
            if hasattr(event, "model_dump_json")
            else event.json()
        )
        payload = json.loads(raw)
    except Exception:
        payload = {"raw": str(event)}

    record = {
        "logged_at": datetime.now().isoformat(timespec="seconds"),
        "self_id": str(getattr(bot, "self_id", "")),
        "event": payload,
    }

    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        with _LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        logger.warning(
            f"[offline_logger] {payload.get('notice_type')} 已记录: "
            f"tag={payload.get('tag')!r} message={payload.get('message')!r}"
        )
    except Exception as e:  # 落盘失败也不能影响事件流
        logger.error(f"[offline_logger] 写入 bot_offline.log 失败: {e}")

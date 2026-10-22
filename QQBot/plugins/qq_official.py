"""
QQ official bot entry — stage 4 of the QQ-official worktree plan.

Thin mirror of plugins/agent_router.py: one on_message matcher with a
platform-specific type guard, event → agent.inbound view conversion
(adapters/qq_official.py::build_view), per-turn sender binding through
the _turn_sender contextvar, and flush() of the buffering official
sender once the turn completes.

Official-route specifics vs the OneBot entry (plan §7.5/§7.6):
  - no continuous-session router: group messages only ever arrive
    @-gated (c2c_group_at_messages intent) and c2c has no @ concept,
    so "no-@ window" mode has nothing to act on;
  - no poke router and no changelog-broadcast / group-list-export
    startup tasks: no official API equivalents;
  - the handler is annotated with QQ event types — NoneBot dispatches by
    annotation, so this matcher never sees OneBot events and vice versa
    (both entries safely coexist at priority=1, block=False);
  - identity is opaque openids: each inbound message logs the author
    openid at INFO so admins can collect it into
    QQ_OFFICIAL_SUPERUSERS / QQ_OFFICIAL_VIP_USERS (plan §7.7).

WebUI observability: the panel is a separate process with no bot handle,
so this plugin mirrors the changelog file-queue IPC pattern — it writes
data/official_status.json (atomic tmp+replace) on startup / bot connect /
bot disconnect plus a 60s heartbeat while the process lives, and the
panel's webui/official_status.py reads it with mtime staleness rules.
The periodic heartbeat is essential: message-driven writes alone would
let a quiet evening degrade the panel card to "stale" falsely.
"""
import asyncio
import random
import time
from pathlib import Path

from nonebot import get_bots, get_driver, logger, on_message
from nonebot.adapters.qq import Bot as QQBot
from nonebot.adapters.qq.event import QQMessageEvent

from adapters.qq_official import (
    HANDLED_EVENTS,
    QQOfficialSender,
    build_view,
    send_active_c2c,
)
from agent import greeting
from agent.character_registry import get_character_registry
from agent.context import _turn_sender
from agent.delegation import _LLM_ERROR_PREFIXES
from agent.turn import _bond_manager, _model_router, run_agent_turn
from lib.status_heartbeat import read_status, write_status

official_router = on_message(priority=1, block=False)


# ── Status heartbeat file (panel reads, never writes) ─────────────

_STATUS_FILE = Path(__file__).resolve().parent.parent / "data" / "official_status.json"
_HEARTBEAT_INTERVAL = 60      # seconds between heartbeat refreshes
_MSG_WRITE_THROTTLE = 5.0     # min seconds between message-counter writes

_connected = False
_msg_total = 0
_last_msg_at: float | None = None
_last_msg_write = 0.0


def _read_status() -> dict:
    return read_status(_STATUS_FILE)


def _write_status(**patch) -> None:
    """Merge patch into the status file (atomic; lib/status_heartbeat).

    Best-effort: observability must never break message handling.
    """
    try:
        write_status(_STATUS_FILE, **patch)
    except OSError as e:
        logger.warning(f"[qq_official] status write failed: {e}")


def _note_message() -> None:
    """Bump in-memory message counters; flush to disk at most every 5s."""
    global _msg_total, _last_msg_at, _last_msg_write
    _msg_total += 1
    _last_msg_at = time.time()
    if _last_msg_at - _last_msg_write >= _MSG_WRITE_THROTTLE:
        _last_msg_write = _last_msg_at
        _write_status(last_message_at=_last_msg_at, messages_total=_msg_total)


@get_driver().on_startup
async def _official_status_startup():
    global _msg_total, _last_msg_at
    # Counters are lifetime-cumulative: seed from any pre-restart file.
    cur = _read_status()
    _msg_total = int(cur.get("messages_total") or 0)
    _last_msg_at = cur.get("last_message_at")
    _write_status(state="starting", appid=None, connected_at=None,
                  messages_total=_msg_total, last_message_at=_last_msg_at)

    async def _heartbeat():
        while True:
            await asyncio.sleep(_HEARTBEAT_INTERVAL)
            # Unconditional: proves process liveness even while the
            # official websocket is down (state stays "disconnected"
            # and fresh, instead of degrading to "stale").
            _write_status(messages_total=_msg_total, last_message_at=_last_msg_at)

    asyncio.create_task(_heartbeat())


@get_driver().on_bot_connect
async def _official_bot_connect(bot):
    global _connected
    if not isinstance(bot, QQBot):
        return  # OneBot (NapCat) bot connecting — not ours
    _connected = True
    _write_status(state="connected", appid=str(bot.self_id),
                  connected_at=time.time())
    logger.info(f"[qq_official] status: connected appid={bot.self_id}")


@get_driver().on_bot_disconnect
async def _official_bot_disconnect(bot):
    global _connected
    if not isinstance(bot, QQBot):
        return
    _connected = False
    _write_status(state="disconnected", connected_at=None)
    logger.warning(f"[qq_official] status: disconnected appid={bot.self_id}")


# ── P3b: HEARTBEAT 低频主动问候（官方 c2c 主动消息）────────────────
#
# 主动消息 = send_to_c2c 不带 msg_id，受平台每用户每日/每月小额配额 +
# 内容审核约束。簿记（候选索引 / 配额 / 冷却）全部在 agent/greeting.py
# 的纯函数里；本插件只做 bot 句柄获取、LLM 文案生成与发送编排。
# 全程 best-effort：任何失败只记日志，绝不影响入站消息处理。

_GREET_TARGETS_FILE = Path(__file__).resolve().parent.parent / "data" / "bond_greet_targets.json"
_GREET_QUOTA_FILE = Path(__file__).resolve().parent.parent / "data" / "official_active_quota.json"

# LLM 文案失败时的模板降级（{name} = 角色短名）
_GREET_FALLBACKS = (
    "{name}：「好久不见～最近过得怎么样呀？」",
    "{name}：「在忙吗？突然有点想找你聊聊天。」",
    "{name}：「今天也要好好吃饭、好好休息哦～」",
)


@get_driver().on_startup
async def _greeting_startup():
    asyncio.create_task(_greeting_loop())


async def _greeting_loop():
    """Periodic greeting pass: interval + jitter, config re-read each round."""
    while True:
        cfg = _bond_manager._config
        hours = float(cfg.get("greet_interval_hours", 6.0))
        jitter = float(cfg.get("greet_jitter_minutes", 45))
        await asyncio.sleep(hours * 3600 + random.uniform(0, max(0.0, jitter) * 60))
        try:
            await _run_greeting_pass()
        except Exception as e:
            logger.warning(f"[qq_official] greeting pass failed: {e!r}")


def _get_qq_bot() -> QQBot | None:
    for b in get_bots().values():
        if isinstance(b, QQBot):
            return b
    return None


async def _run_greeting_pass() -> None:
    cfg = _bond_manager._config
    if not bool(cfg.get("greet_enabled", True)):
        return
    bot = _get_qq_bot()
    if bot is None:
        return
    entries = greeting.load_targets(str(_GREET_TARGETS_FILE))
    if not entries:
        return
    pm = get_character_registry()

    def state_for(openid: str):
        # 仅 split（养成）人格 + 已有羁绊记录的用户可被问候
        try:
            persona = pm.resolve_effective_personality(openid, None)
            if pm.get_mode(persona) != "split":
                return None
            return _bond_manager.get_state(openid, persona, create=False)
        except Exception:
            return None

    picked = greeting.pick_greet_targets(
        entries,
        state_for=state_for,
        min_level=int(cfg.get("greet_min_level", 5)),
        cooldown_days=float(cfg.get("greet_cooldown_days", 7)),
        probability=float(cfg.get("greet_probability", 0.25)),
        max_batch=int(cfg.get("greet_batch_max", 5)),
    )
    greeted = 0
    for openid, state in picked:
        if not greeting.active_allowed(
                str(_GREET_QUOTA_FILE), openid,
                daily_cap=int(cfg.get("greet_daily_cap", 1)),
                monthly_cap=int(cfg.get("greet_monthly_cap", 4))):
            continue
        persona = pm.resolve_effective_personality(openid, None)
        text = await _compose_greeting(persona, state)
        if not text:
            continue
        if await send_active_c2c(bot, openid, text):
            greeting.record_active_send(str(_GREET_QUOTA_FILE), openid)
            greeting.mark_greeted(str(_GREET_TARGETS_FILE), openid)
            greeted += 1
            logger.info(
                f"[qq_official] active greeting → {openid[:8]}… as {persona}")
        await asyncio.sleep(random.uniform(2.0, 6.0))  # 错峰，避免齐发
    if greeted:
        logger.info(f"[qq_official] greeting pass: {greeted} sent")


async def _compose_greeting(persona_key: str, state: dict) -> str:
    """In-character low-frequency greeting (flash); template on failure."""
    pm = get_character_registry()
    short = pm.get_short_name(persona_key)
    try:
        card = pm.load(persona_key) or ""
        if card:
            gap = 0
            try:
                last_seen = float((state or {}).get("stats", {}).get("last_seen") or 0)
                gap = max(1, int((time.time() - last_seen) // 86400)) if last_seen else 0
            except (TypeError, ValueError):
                gap = 0
            level = int((state or {}).get("level", 1))
            system = (
                card
                + "\n\n---\n你正在扮演上述角色。你们已经有一段时间没聊天了，"
                  "你想主动发一条消息问候用户。保持角色口吻，自然、克制、不煽情，"
                  "像老朋友或亲近的人那样关心对方近况。"
                  "不要提及系统、平台、配额或本条指示本身。"
            )
            prompt = (
                f"距你们上次交谈约 {gap} 天（羁绊 Lv{level}）。"
                f"请写一句主动问候。要求：不超过50字；不使用markdown；"
                f"只输出这句问候本身。"
            )
            raw = await _model_router.flash_client.chat_completion(
                message=prompt,
                history=[{"role": "system", "content": system}],
                timeout_set=15.0,
                purpose="bond_greeting",
            )
            candidate = (raw or "").strip()
            if (
                candidate
                and not candidate.startswith(_LLM_ERROR_PREFIXES)
                and len(candidate) <= 120
                and "\n" not in candidate
            ):
                return candidate
    except Exception:
        pass
    return random.choice(_GREET_FALLBACKS).format(name=short)


@official_router.handle()
async def handle_official_message(bot: QQBot, event: QQMessageEvent):
    """One official c2c/group-@ message → one agent turn.

    The QQMessageEvent annotation keeps OneBot events out (dependency
    typing); the isinstance guard narrows to the two event types the
    intent config actually delivers.
    """
    if not isinstance(event, HANDLED_EVENTS):
        return
    _note_message()

    view = build_view(bot, event)
    scope = "group" if view.is_group else "c2c"
    if not view.is_group:
        # P3b: c2c user_openid is the only identity usable for active
        # c2c pushes → maintain the greeting candidate index from here
        # (group member_openid can NOT be messaged via send_to_c2c).
        greeting.note_c2c_contact(str(_GREET_TARGETS_FILE), view.user_id)
    logger.info(
        f"[qq_official] {scope} message: openid={view.user_id} "
        f"group={view.group_id or '-'} msg_id={view.message_id} "
        f"attachments={len(event.attachments or [])} "
        f"text={view.text[:60]!r}"
    )

    sender = QQOfficialSender(bot, event, view.is_group)
    if not sender.wants_reply(view):
        return

    # Bind this event's sender for the whole turn: _safe_send resolves
    # explicit arg → _turn_sender → default_sender, so every kernel path
    # (_send_response, cards, busy notice, tools via _ToolSender) lands
    # in the buffer without turn.py knowing the platform.
    token = _turn_sender.set(sender)
    try:
        await run_agent_turn(view)
    finally:
        _turn_sender.reset(token)
        # Best-effort: buffered output goes out even if the turn raised.
        await sender.flush()

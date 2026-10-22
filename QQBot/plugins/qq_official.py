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
import time
from pathlib import Path

from nonebot import get_driver, logger, on_message
from nonebot.adapters.qq import Bot as QQBot
from nonebot.adapters.qq.event import QQMessageEvent

from adapters.qq_official import HANDLED_EVENTS, QQOfficialSender, build_view
from agent.context import _turn_sender
from agent.turn import run_agent_turn
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

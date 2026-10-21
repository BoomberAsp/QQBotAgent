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
"""
from nonebot import logger, on_message
from nonebot.adapters.qq import Bot as QQBot
from nonebot.adapters.qq.event import QQMessageEvent

from adapters.qq_official import HANDLED_EVENTS, QQOfficialSender, build_view
from agent.context import _turn_sender
from agent.turn import run_agent_turn

official_router = on_message(priority=1, block=False)


@official_router.handle()
async def handle_official_message(bot: QQBot, event: QQMessageEvent):
    """One official c2c/group-@ message → one agent turn.

    The QQMessageEvent annotation keeps OneBot events out (dependency
    typing); the isinstance guard narrows to the two event types the
    intent config actually delivers.
    """
    if not isinstance(event, HANDLED_EVENTS):
        return

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

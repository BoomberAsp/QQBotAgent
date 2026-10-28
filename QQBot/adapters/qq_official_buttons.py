"""
QQ official keyboard wire format + interaction-reply sender (buttons P0+P1).

Leaf module on purpose: it imports only the nonebot QQ adapter models
and lib.button_spec, so adapters/qq_official.py can top-level import
``to_message_keyboard`` from here while this module reaches back into
qq_official (capability probes) only *inside* functions — one import
edge, no cycle.

Contents:
  - to_message_keyboard(): Rows (lib.button_spec DSL) → the adapter's
    MessageKeyboard model (docs/qqbot-api-v2-full-docs.md §50 field map);
  - encode_cb()/decode_cb(): the strict ``qb1:<verb>[:arg…]`` callback
    payload codec — the same regex validates on both ends, so a
    malformed payload can never reach a handler;
  - QQOfficialInteractionSender: the outbound.Sender used for the ONE
    receipt message after a button callback (bot.send on an
    InteractionCreateEvent resolves the event_id passive reply for us);
  - module-level click counters read by the status heartbeat.
"""
from __future__ import annotations

from nonebot import logger
from nonebot.adapters.qq import MessageSegment
from nonebot.adapters.qq.message import Message
from nonebot.adapters.qq.models.common import (
    Action,
    Button,
    InlineKeyboard,
    InlineKeyboardRow,
    MessageKeyboard,
    Modal,
    Permission,
    RenderData,
)

from lib.button_spec import CB_PAYLOAD_RE, Btn, Rows, to_text

import re

_ARG_RE = re.compile(r"^[A-Za-z0-9_.#-]{1,24}$")
_VERB_RE = re.compile(r"^[a-z_]+(?:\.[a-z_]+)?$")


# ── callback payload codec ──────────────────────────────────────────

def encode_cb(verb: str, *args: str) -> str:
    """Build a strict ``qb1:<verb>[:arg[:arg[:arg]]]`` payload.

    Raises ValueError on anything the decoder would reject (verb
    charset, arg charset/length/count) — encoding bugs must surface at
    render time, not as a silently dropped click.
    """
    if not _VERB_RE.match(verb or ""):
        raise ValueError(f"bad callback verb: {verb!r}")
    if len(args) > 3:
        raise ValueError(f"too many callback args for {verb}: {len(args)} > 3")
    str_args = [str(a) for a in args]
    for arg in str_args:
        if not _ARG_RE.match(arg):
            raise ValueError(f"bad callback arg for {verb}: {arg!r}")
    payload = ":".join(["qb1", verb, *str_args])
    if not CB_PAYLOAD_RE.match(payload):
        raise ValueError(f"encoded payload malformed: {payload!r}")
    return payload


def decode_cb(payload: str) -> tuple[str, list[str]]:
    """Parse a callback payload → (verb, args). ValueError if malformed.

    The arg charset excludes ':' so a plain split is unambiguous; the
    regex gate first means handlers never see injection-shaped data.
    """
    if not CB_PAYLOAD_RE.match(payload or ""):
        raise ValueError(f"malformed callback payload: {str(payload)[:64]!r}")
    parts = payload.split(":")
    return parts[1], parts[2:]


# ── Rows → MessageKeyboard wire model ───────────────────────────────

def to_message_keyboard(rows: Rows, *, scope: str = "",
                        openid: str = "") -> MessageKeyboard:
    """Translate validated Btn rows into the adapter's MessageKeyboard.

    Field map (docs §50):
      cmd → Action(type=2, data, enter, reply, anchor=1|None,
                   permission=type 2, unsupport_tips)
      cb  → Action(type=1, data, modal?  permission, unsupport_tips)
      url → Action(type=0, data, permission=type 2, unsupport_tips)

    Permissions: cb + admin_only → platform type=1 (仅管理者); cb in a
    GROUP with a known rendering-target openid → type=0 restricted to
    that user (a session-delete button rendered for user A must not be
    clickable by user B — design §9.2 privacy); everything else type=2
    (所有人). cmd/url buttons are always type=2 — worst case a click
    just types a command the permission layer will reject anyway.

    Callers must have run button_spec.validate() first; this function
    does no layout checks (style clamps to the platform's 0/1 enum).
    """
    wire_rows: list[InlineKeyboardRow] = []
    for i, row in enumerate(rows or []):
        buttons: list[Button] = []
        for j, btn in enumerate(row):
            buttons.append(Button(
                id=btn.id or f"b.{i}.{j}",
                group_id=(btn.group or None) if btn.action == "cb" else None,
                render_data=RenderData(
                    label=btn.label,
                    visited_label=btn.visited or btn.label,
                    style=btn.style if btn.style in (0, 1) else 1,
                ),
                action=_wire_action(btn, scope=scope, openid=openid),
            ))
        wire_rows.append(InlineKeyboardRow(buttons=buttons))
    return MessageKeyboard(content=InlineKeyboard(rows=wire_rows))


def _wire_action(btn: Btn, *, scope: str, openid: str) -> Action:
    if btn.action == "cmd":
        return Action(
            type=2,
            permission=Permission(type=2),
            data=btn.data,
            reply=bool(btn.reply),
            enter=bool(btn.enter),
            anchor=1 if btn.anchor else None,
            unsupport_tips=btn.tip,
        )
    if btn.action == "cb":
        if btn.admin_only:
            permission = Permission(type=1)
        elif scope == "group" and openid:
            permission = Permission(type=0, specify_user_ids=[openid])
        else:
            permission = Permission(type=2)
        return Action(
            type=1,
            permission=permission,
            data=btn.data,
            modal=(Modal(content=btn.modal, confirm_text=btn.confirm,
                         cancel_text=btn.cancel) if btn.modal else None),
            unsupport_tips=btn.tip,
        )
    return Action(
        type=0,
        permission=Permission(type=2),
        data=btn.data,
        unsupport_tips=btn.tip,
    )


# ── click telemetry (read by the status heartbeat) ─────────────────

_CLICK_STATS: dict = {"clicks_total": 0, "click_errors_total": 0,
                      "actions": {}}


def note_click(verb: str, *, ok: bool) -> None:
    _CLICK_STATS["clicks_total"] += 1
    if not ok:
        _CLICK_STATS["click_errors_total"] += 1
    if verb:
        per = _CLICK_STATS["actions"]
        per[verb] = int(per.get(verb, 0)) + 1


def click_snapshot() -> dict:
    """Shallow snapshot for data/official_status.json (heartbeat §10.1)."""
    return {"clicks_total": _CLICK_STATS["clicks_total"],
            "click_errors_total": _CLICK_STATS["click_errors_total"],
            "actions": dict(_CLICK_STATS["actions"])}


# ── interaction-reply sender ────────────────────────────────────────

class QQOfficialInteractionSender:
    """outbound.Sender for the ONE receipt after a button callback.

    bot.send(InteractionCreateEvent, …) uses event.event_id (the
    interaction id) as the passive-reply anchor, so the receipt rides
    the interaction's own quota and never eats message-reply quota.
    Exactly one message goes out (max_messages_per_turn=1): extra
    send_* calls are logged and dropped — degradation *retries* replace
    the pending message and are not counted twice. Images/audio/
    progress are no-ops — a callback receipt must stay text/markdown
    (+keyboard). ``scope``/``openid`` feed to_message_keyboard's
    privacy permissions (group receipts restrict cb buttons to the
    clicking user).
    """

    platform = "qq_official"
    max_messages_per_turn = 1

    def __init__(self, bot, event, *, scope: str = "", openid: str = ""):
        self._bot = bot
        self._event = event
        self._scope = scope
        self._openid = openid
        self._sent = 0

    @property
    def supports_markdown(self) -> bool:
        # Lazy import: adapters.qq_official top-level imports THIS
        # module, so a module-level back-import would be a cycle.
        from adapters.qq_official import _md_available
        return _md_available()

    @property
    def supports_keyboard(self) -> bool:
        from adapters.qq_official import _kb_available
        return _kb_available()

    def _budget_left(self) -> bool:
        if self._sent >= self.max_messages_per_turn:
            logger.warning(
                "[qq_official_kb] interaction receipt budget (1) "
                "exhausted; dropping extra send")
            return False
        return True

    async def send_text(self, text: str) -> None:
        if not (text and text.strip()) or not self._budget_left():
            return
        await self._post(MessageSegment.text(text))

    async def send_markdown(self, text: str) -> None:
        if not (text and text.strip()):
            return
        if self.supports_markdown:
            if self._budget_left():
                await self._post(MessageSegment.markdown(text))
        else:
            await self.send_text(text)

    async def send_keyboard(self, msg) -> None:
        """Receipt with a refreshed keyboard; immediate (no buffering).

        Degradation mirrors QQOfficialSender._send_payload but inline:
        invalid/unavailable → markdown-only up front; kb reject code →
        markdown-only retry; md reject code → plain text + command list.
        """
        from adapters.qq_official import (
            _KB_REJECT_CODES, _MD_REJECT_CODES, _kb_available,
            _kb_record_reject, _kb_record_success, _md_available,
            _md_record_reject, _md_record_success,
        )
        from lib.button_spec import validate
        from nonebot.adapters.qq.exception import ActionFailed

        if not self._budget_left():
            return
        body = (msg.markdown or "").strip()
        rows = msg.rows or []
        problems = validate(rows) if rows else ["empty rows"]
        if problems:
            logger.warning(
                f"[qq_official_kb] interaction receipt keyboard "
                f"invalid ({problems[:2]}); markdown-only")
        if problems or not _kb_available() or not _md_available():
            # keyboard requires msg_type=2 (markdown rider)
            if _md_available():
                await self._post(MessageSegment.markdown(body or " "))
            else:
                await self._post(MessageSegment.text(body or " "))
            return
        wire = to_message_keyboard(rows, scope=self._scope,
                                   openid=self._openid)
        attempts = [
            Message([MessageSegment.markdown(body or " "),
                     MessageSegment.keyboard(wire)]),
            Message([MessageSegment.markdown(body or " ")]),
        ]
        extra = to_text(rows)
        attempts.append(Message([MessageSegment.text(
            f"{body}\n\n{extra}" if extra else (body or " "))]))
        for stage, message in enumerate(attempts):
            try:
                await self._bot.send(self._event, message)
                self._sent += 1
                if stage == 0:
                    _kb_record_success()
                    _md_record_success()
                return
            except ActionFailed as e:
                code = getattr(e, "code", None)
                if stage == 0 and code in _KB_REJECT_CODES:
                    _kb_record_reject(code)
                    logger.warning(
                        f"[qq_official_kb] receipt keyboard rejected "
                        f"(code={code}); markdown-only")
                    continue
                if stage <= 1 and code in _MD_REJECT_CODES:
                    _md_record_reject()
                    logger.warning(
                        f"[qq_official_kb] receipt markdown rejected "
                        f"(code={code}); plain text")
                    continue
                logger.warning(
                    f"[qq_official_kb] receipt send rejected: {e!r}")
                return
            except Exception as e:  # noqa: BLE001 — best-effort receipt
                logger.warning(
                    f"[qq_official_kb] receipt send failed: "
                    f"{type(e).__name__}: {e}")
                return

    async def send_image(self, img) -> None:
        logger.debug("[qq_official_kb] image dropped on interaction receipt")

    async def send_audio(self, audio) -> None:
        logger.debug("[qq_official_kb] audio dropped on interaction receipt")

    async def send_progress(self, text: str, *, immediate: bool = False) -> None:
        return  # swallowed: the receipt IS the only output

    async def _post(self, message) -> None:
        """Best-effort single send (no degradation — text/md only)."""
        try:
            await self._bot.send(self._event, message)
            self._sent += 1
        except Exception as e:  # noqa: BLE001 — a receipt never raises
            logger.warning(
                f"[qq_official_kb] receipt send failed: "
                f"{type(e).__name__}: {e}")

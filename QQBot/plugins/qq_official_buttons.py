"""
QQ official INTERACTION_CREATE entry (buttons-design §6.5, P1).

One on_notice matcher annotated with InteractionCreateEvent — NoneBot
dispatches by annotation, so this never sees OneBot events. Callback
buttons (type=1) arrive here; command buttons (type=2) never do (they
make the client type text, which flows through the normal message
entries).

Pipeline (§6.5, all steps best-effort — this handler MUST NOT raise):
  1. type gate (11 = button callback);
  2. identity from the EVENT (c2c → user_openid, group →
     group_member_openid + group_openid; NEVER from the payload);
  3. contextvars for the permission layer (_current_platform selects
     the openid identity tables);
  4. decode_cb + verb lookup (malformed → code=1 + audit);
  5. idempotency: interaction ids are retried by the platform when the
     ack is slow — a duplicate gets code=3 and no second execution;
  6. rate limit: (user, verb) 3 clicks / 10s → code=2;
  7-8. dispatch (scene check → role re-check → 2.5s budget);
  9. bot.put_interaction ack (failure logged only — the receipt still
     goes out);
 10. exactly one receipt via QQOfficialInteractionSender (passive reply
     on the interaction id; may carry a refreshed keyboard).

Audit: JSONL at QQ_OFFICIAL_BUTTONS_AUDIT (default
data/official_buttons_audit_YYYY-MM-DD.jsonl, daily rotation),
single-line append, best-effort.
"""
from __future__ import annotations

import json
import os
import time
from collections import OrderedDict
from pathlib import Path

from nonebot import logger, on_notice
from nonebot.adapters.qq import Bot as QQBot
from nonebot.adapters.qq.event import InteractionCreateEvent

from adapters.qq_official_buttons import (
    QQOfficialInteractionSender,
    decode_cb,
    note_click,
)
from agent import context as agent_context
from agent.button_actions import ButtonContext, dispatch
from agent.outbound import OutKeyboard

interaction_router = on_notice(priority=3, block=False)


# ── idempotency (LRU + TTL) ────────────────────────────────────────

_SEEN_MAX = 2048
_SEEN_TTL = 600.0          # platform retries land within seconds; 10min is ample
_seen: "OrderedDict[str, float]" = OrderedDict()


def _seen_before(key: str) -> bool:
    """True if this interaction id was already processed (marks it if new)."""
    now = time.time()
    while _seen:
        oldest_key, oldest_ts = next(iter(_seen.items()))
        if now - oldest_ts > _SEEN_TTL:
            _seen.popitem(last=False)
        else:
            break
    if key in _seen:
        _seen.move_to_end(key)
        return True
    _seen[key] = now
    while len(_seen) > _SEEN_MAX:
        _seen.popitem(last=False)
    return False


# ── rate limit: (user, verb) ≤ 3 per 10s ───────────────────────────

_RATE_WINDOW = 10.0
_RATE_MAX = 3
_rate: dict = {}


def _rate_limited(user: str, verb: str) -> bool:
    key = (user, verb)
    now = time.time()
    hits = [t for t in _rate.get(key, []) if now - t < _RATE_WINDOW]
    hits.append(now)
    _rate[key] = hits
    return len(hits) > _RATE_MAX


# ── audit JSONL ────────────────────────────────────────────────────

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def _audit_path() -> Path:
    env = os.environ.get("QQ_OFFICIAL_BUTTONS_AUDIT", "").strip()
    if env:
        return Path(env)
    return _DATA_DIR / f"official_buttons_audit_{time.strftime('%Y-%m-%d')}.jsonl"


def _audit(record: dict) -> None:
    try:
        path = _audit_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as e:
        logger.debug(f"[qq_buttons] audit write failed: {e}")


# ── helpers ────────────────────────────────────────────────────────

async def _ack(bot, interaction_id: str, code: int) -> None:
    """put_interaction is mandatory within ~3s; failure is log-only."""
    try:
        await bot.put_interaction(interaction_id=interaction_id, code=code)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[qq_buttons] put_interaction(code={code}) failed: {e}")


async def _receipt(bot, event, scene: str, user: str, result) -> None:
    """Send the ONE receipt (never raises — sender is best-effort)."""
    has_body = bool((result.markdown or result.message or "").strip())
    if not has_body and not result.rows:
        return
    sender = QQOfficialInteractionSender(bot, event, scope=scene, openid=user)
    if result.rows:
        await sender.send_keyboard(OutKeyboard(
            markdown=result.markdown or result.message, rows=result.rows))
    elif result.markdown:
        body = result.markdown
        if result.message:
            body = f"{result.message}\n\n{body}"
        await sender.send_markdown(body)
    else:
        await sender.send_text(result.message)


# ── the handler ────────────────────────────────────────────────────

@interaction_router.handle()
async def _on_interaction(bot: QQBot, event: InteractionCreateEvent) -> None:
    t0 = time.time()
    verb = ""
    user = ""
    group = ""
    scene = ""
    code = 1
    kind = "click"
    button_id = ""
    button_data = ""
    try:
        # 1. type gate — 11 = button callback (12/13 are other interactions)
        if getattr(event, "type", None) != 11:
            return

        resolved = event.data.resolved
        button_data = str(getattr(resolved, "button_data", "") or "")
        button_id = str(getattr(resolved, "button_id", "") or "")

        # 2. identity — strictly from the event envelope
        chat_type = getattr(event, "chat_type", None)
        if chat_type == 2:
            scene = "c2c"
            user = str(getattr(event, "user_openid", "") or "")
        elif chat_type == 1:
            scene = "group"
            user = str(getattr(event, "group_member_openid", "") or "")
            group = str(getattr(event, "group_openid", "") or "")
        else:
            kind = "reject"
            await _ack(bot, event.id, 1)
            return
        if not user:
            kind = "reject"
            await _ack(bot, event.id, 1)
            return

        # 3. contextvars for the permission/role layer
        agent_context._current_platform.set("qq_official")
        agent_context._current_user_id.set(user)
        agent_context._current_group_id.set(group)
        from agent.turn import _perm_manager
        role = _perm_manager.get_role(user)
        agent_context._current_user_role.set(
            role.value if hasattr(role, "value") else str(role))

        # 4. decode the callback payload
        try:
            verb, args = decode_cb(button_data)
        except ValueError:
            kind = "reject"
            logger.warning(
                f"[qq_buttons] malformed payload from {user}: "
                f"{button_data[:80]!r}")
            await _ack(bot, event.id, 1)
            return

        # 5. idempotency (platform retries when our ack is slow)
        if _seen_before(event.id):
            code = 3
            await _ack(bot, event.id, 3)
            return

        # 6. rate limit
        if _rate_limited(user, verb):
            code = 2
            await _ack(bot, event.id, 2)
            sender = QQOfficialInteractionSender(
                bot, event, scope=scene, openid=user)
            await sender.send_text("操作太频繁了，请稍后再试。")
            return

        # 7-8. scene check, role re-check and execution live in dispatch
        ctx = ButtonContext(user_id=user, group_id=group, scene=scene,
                            verb=verb, args=args, interaction_id=event.id)
        result = await dispatch(ctx)
        code = result.code

        # 9. ack
        await _ack(bot, event.id, code)

        # 10. one receipt (+ telemetry)
        note_click(verb, ok=(code == 0))
        await _receipt(bot, event, scene, user, result)
    except Exception:  # noqa: BLE001 — the handler must never raise
        logger.exception(f"[qq_buttons] interaction crashed (verb={verb})")
        try:
            await _ack(bot, event.id, 1)
        except Exception:  # noqa: BLE001
            pass
    finally:
        if verb or kind == "reject":
            _audit({
                "ts": round(time.time(), 3),
                "kind": kind,
                "user": user,
                "scene": scene,
                "group": group,
                "button_id": button_id,
                "data": button_data[:80],
                "verb": verb,
                "code": code,
                "ms": round((time.time() - t0) * 1000, 1),
            })

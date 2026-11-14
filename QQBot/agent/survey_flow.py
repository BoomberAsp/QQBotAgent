"""Deterministic SurveyCake survey flow (P0 collection + P1 submission).

Routing rules
-------------
* ``/问卷 ...`` commands are handled here before the LLM sees the message.
* While a draft is active, the owner's plain text is parsed as an answer.
* Non-survey commands (e.g. ``/兑换码``) are never swallowed: anything that
  starts with ``/`` or ``#`` and is not a survey command passes through.
* In group chats one active flow per group is allowed; only the owner's
  messages advance it. This mirrors the existing follow-mode user lock
  (``scope -> owner/deadline``) and never mutes other members' normal chat.
* ``/确认提交`` is the only submit trigger. With P1 disabled it performs a
  local-validation dry run; with ``SURVEY_ENABLE_SUBMIT=1`` +
  ``SURVEY_BROWSER=ruyipage`` it runs the browser submitter and returns a
  receipt (screenshot included).
"""
from __future__ import annotations

import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from lib.button_spec import Btn, Rows, to_text

from lib.survey.draft import DraftStore, SurveyDraft
from lib.survey.model import (
    Answer,
    SurveyDef,
    SurveyQuestion,
    describe_answer,
    mask_member_id,
    validate_answers,
)
from lib.survey.surveycake import SurveyError, fetch_schema, parse_survey_url

_MAX_OPTION_BUTTONS = 12          # keep 跳过/取消 inside the 15-button cap
_DEFAULT_LOCK_TTL = 300.0
_DEFAULT_LOCK_TURNS = 60

_STORE: DraftStore | None = None
_locks: dict[str, "SurveyLock"] = {}
_submitting: set[str] = set()
_attempt_times: dict[str, list[float]] = {}
_pending_start: dict[str, float] = {}
_PENDING_START_TTL = 300.0


# ── configuration ─────────────────────────────────────────────────

def _env_float(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _env_int(name: str, default: int) -> int:
    return int(_env_float(name, float(default)))


def enabled() -> bool:
    raw = (os.environ.get("SURVEY_ENABLED") or "").strip().lower()
    return raw not in ("0", "false", "off", "no")


def _lock_ttl() -> float:
    return _env_float("SURVEY_LOCK_TTL_SEC", _DEFAULT_LOCK_TTL)


def _lock_max_turns() -> int:
    return max(1, _env_int("SURVEY_LOCK_MAX_TURNS", _DEFAULT_LOCK_TURNS))


# ── state (tests may replace the store / clear locks) ─────────────

def _store() -> DraftStore:
    global _STORE
    if _STORE is None:
        _STORE = DraftStore()
    return _STORE


def set_store(store: DraftStore | None) -> None:
    """Test hook: replace the module-wide draft store."""
    global _STORE
    _STORE = store


def reset_locks() -> None:
    _locks.clear()


@dataclass
class SurveyLock:
    scope: str
    owner: str
    draft_id: str
    expires_at: float
    turns: int = 0


def _active_lock(scope: str) -> SurveyLock | None:
    lock = _locks.get(scope)
    if lock is None:
        return None
    if lock.expires_at <= time.time() or lock.turns >= _lock_max_turns():
        _locks.pop(scope, None)
        return None
    return lock


def _prune_locks() -> None:
    now = time.time()
    cap = _lock_max_turns()
    for scope, lock in list(_locks.items()):
        if lock.expires_at <= now or lock.turns >= cap:
            _locks.pop(scope, None)


def _touch_or_acquire(scope: str, identity: str, draft_id: str) -> None:
    lock = _locks.get(scope)
    if lock is not None and lock.owner == identity:
        lock.draft_id = draft_id
        lock.expires_at = time.time() + _lock_ttl()
        lock.turns += 1
        return
    _locks[scope] = SurveyLock(
        scope=scope,
        owner=identity,
        draft_id=draft_id,
        expires_at=time.time() + _lock_ttl(),
        turns=1,
    )


def reset_scope(scope: str) -> None:
    """Drop a group's survey lock (group generation reset / admin release)."""
    _locks.pop(scope, None)


# ── submission helpers (P1) ───────────────────────────────────────

def _browser_ready() -> tuple[bool, str]:
    from lib import browser_action
    return browser_action.ready()


async def _submit_via_browser(survey, draft, screenshot_path: str) -> dict:
    from lib import browser_action
    return await browser_action.submit_survey(
        survey, draft, screenshot_path=screenshot_path
    )


def _receipt_days() -> int:
    return max(1, _env_int("SURVEY_SCREENSHOT_RETENTION_DAYS", 7))


def _member_id(survey: SurveyDef, draft: SurveyDraft) -> str:
    """Full member id for user-facing verification (not a secret in chat)."""
    for question in survey.questions:
        if _is_member_id_question(question):
            answer = draft.answers.get(question.subject_id)
            if answer is not None and (answer.text or "").strip():
                return answer.text.strip()
    return ""


def _receipt_text(survey: SurveyDef, draft: SurveyDraft, member_id: str) -> str:
    now = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    return "\n".join([
        "✅ 已提交成功",
        f"问卷：{survey.title}",
        f"时间：{now}（UTC+8）",
        f"草稿：#{draft.draft_id}",
        f"会员编号：{member_id or '（未填）'}",
        "回执截图见下一条消息。",
        "奖励将发送至该编号的游戏信箱，请勿重复提交。",
    ])


def _rate_limit_message(identity: str) -> str:
    now = time.time()
    times = [stamp for stamp in _attempt_times.get(identity, []) if now - stamp < 86400]
    _attempt_times[identity] = times
    per_hour = _env_int("SURVEY_MAX_PER_HOUR", 3)
    per_day = _env_int("SURVEY_MAX_PER_DAY", 5)
    hour_count = sum(1 for stamp in times if now - stamp < 3600)
    if hour_count >= per_hour or len(times) >= per_day:
        return ("⚠ 提交过于频繁，请稍后再试。\n"
                f"（每小时最多 {per_hour} 次、每天最多 {per_day} 次）")
    return ""


def _record_attempt_time(identity: str) -> None:
    _attempt_times.setdefault(identity, []).append(time.time())


def _reset_submit_state() -> None:
    """Test hook: clear in-flight, rate-limit and pending-link state."""
    _submitting.clear()
    _attempt_times.clear()
    _pending_start.clear()


# ── sending / view helpers ────────────────────────────────────────

async def _send(text: str, sender=None) -> None:
    from . import turn
    await turn._safe_send(text, sender=sender)


async def _send_with_buttons(text: str, rows: Rows, sender=None) -> None:
    if not rows:
        await _send(text, sender)
        return
    from . import turn
    try:
        supports = bool(turn._channel_supports_keyboard(sender))
    except Exception:
        supports = False
    if supports:
        sanitize = getattr(turn, "_sanitize_md_text", None)
        body = sanitize(text) if sanitize else text
        await turn._safe_send_kb(body, rows, sender=sender)
        return
    hint = to_text(rows)
    await _send(text + ("\n\n" + hint if hint else ""), sender)


def _identity(view) -> str:
    try:
        from .group_state import identity_for
        return identity_for(view)
    except Exception:
        return (f"{getattr(view, 'platform', '')}:"
                f"{getattr(view, 'self_id', '')}:"
                f"{getattr(view, 'user_id', '')}")


def _scope(view) -> str:
    if getattr(view, "is_group", False):
        from .group_state import scope_for
        return scope_for(view)
    return (f"private:{getattr(view, 'platform', '')}:"
            f"{getattr(view, 'self_id', '')}:"
            f"{getattr(view, 'user_id', '')}")


def _is_admin(view) -> bool:
    try:
        from . import turn
        role = turn._perm_manager.get_role(view.user_id)
        return getattr(role, "value", "") == "admin"
    except Exception:
        return getattr(view, "actor_role", "") in ("owner", "admin")


def _lock_hint(lock: SurveyLock) -> str:
    minutes = max(1, int((lock.expires_at - time.time()) / 60))
    return (f"群内已有成员正在填写问卷（剩余约 {minutes} 分钟）。\n"
            "请等 TA 完成，或让管理员发送 /问卷释放。")


def _group_task_hint(view) -> str:
    """Gacha-style hint: the task is alive for a few minutes, /取消 exits."""
    if not getattr(view, "is_group", False):
        return ""
    lock = _locks.get(_scope(view))
    if lock is None:
        return ""
    minutes = max(1, int((lock.expires_at - time.time()) / 60))
    return ("\n\n（问卷任务进行中，约剩 "
            f"{minutes} 分钟；由本人继续，发送 /取消 退出。"
            "未开启非 @ 接收时请 @机器人 或引用回复补充。）")


# ── command / URL parsing ───────────────────────────────────────

# Users paste links in many shapes: full-width punctuation from a Chinese
# IME, no space after the command, an explanatory prefix
# ("问卷链接：…"), angle brackets around the URL, or the link in a
# follow-up message. Normalise and extract the URL instead of taking the
# first whitespace token.
_ZERO_WIDTH = {"\u200b", "\u200c", "\u200d", "\u200e", "\u200f", "\ufeff"}
_URL_RE = re.compile(r"https?://[^\s<>\"'，。；、）】》]+", re.IGNORECASE)
_NAKED_URL_RE = re.compile(
    r"(?:www\.)?surveycake\.com/s/[A-Za-z0-9]{4,20}", re.IGNORECASE
)
_BARE_SVID_RE = re.compile(
    r"(?:svid\s*[=:]\s*)?([A-Za-z0-9]{4,20})", re.IGNORECASE
)


def _normalize_command_text(text: str) -> str:
    out = []
    for char in (text or ""):
        if char in _ZERO_WIDTH:
            continue
        code = ord(char)
        if 0xFF01 <= code <= 0xFF5E:          # full-width ASCII ／：＃ｈｔｔｐ
            char = chr(code - 0xFEE0)
        elif char == "\u3000":               # ideographic space
            char = " "
        out.append(char)
    return "".join(out).replace("\xa0", " ").strip()


def _extract_survey_url(text: str) -> str:
    """Return the first survey URL in the text, without wrappers.

    Tolerates a missing scheme: ``www.surveycake.com/s/ABb8y`` becomes
    ``https://www.surveycake.com/s/ABb8y``.
    """
    normalized = _normalize_command_text(text)
    match = _URL_RE.search(normalized)
    if match:
        return match.group(0).rstrip(".,;:!?)]}>）】》」』\"'")
    naked = _NAKED_URL_RE.search(normalized)
    if naked:
        return "https://" + naked.group(0).lstrip("/")
    return ""


def _url_from_bare_svid(text: str) -> str:
    """``ABb8y`` / ``svid=ABb8y`` -> full share URL (last-resort fallback)."""
    candidate = _normalize_command_text(text).strip().lstrip("#")
    for token in candidate.split():
        token = token.strip("，,。.;:：；")
        match = _BARE_SVID_RE.fullmatch(token.lstrip("#"))
        if match and any(char.isalpha() for char in match.group(1)):
            return f"https://www.surveycake.com/s/{match.group(1)}"
    return ""


def _view_raw_text(view) -> str:
    """Concatenate strings from the platform raw event.

    QQ clients may turn a pasted link into a ``json`` (link card) message
    segment; OneBot's ``get_plaintext()`` then omits it and ``view.text`` is
    only ``/问卷``. Scanning the raw segments recovers the URL.
    """
    raw = getattr(view, "raw", None)
    if raw is None:
        return ""
    chunks = []
    message = getattr(raw, "message", None)
    if message is not None:
        try:
            for segment in message:
                data = getattr(segment, "data", None) or {}
                if isinstance(data, dict):
                    chunks.extend(
                        str(value) for value in data.values()
                        if isinstance(value, (str, int))
                    )
        except TypeError:
            pass
    content = getattr(raw, "content", None)
    if isinstance(content, str):
        chunks.append(content)
    for attachment in getattr(raw, "attachments", None) or []:
        url = getattr(attachment, "url", None)
        if isinstance(url, str):
            chunks.append(url)
    return "\n".join(chunks)[:8000]


def _raw_segment_types(view) -> str:
    raw = getattr(view, "raw", None)
    message = getattr(raw, "message", None)
    if message is None:
        return ""
    try:
        return ",".join(str(getattr(seg, "type", "?")) for seg in message)[:120]
    except TypeError:
        return ""


def _extract_url_from_view(view) -> str:
    return (_extract_survey_url(getattr(view, "text", "") or "")
            or _extract_survey_url(getattr(view, "reply_context", "") or "")
            or _extract_survey_url(_view_raw_text(view)))


def _set_pending_start(identity: str) -> None:
    _pending_start[identity] = time.time() + _PENDING_START_TTL


def _pending_start_valid(identity: str) -> bool:
    expires = _pending_start.get(identity, 0.0)
    if expires <= time.time():
        _pending_start.pop(identity, None)
        return False
    return True


def _strip_mentions(text: str) -> str:
    text = re.sub(
        r"^(?:(?:<@!?[^>]+>|\[CQ:at,[^\]]+\])[\s\u200b\ufeff]*)+",
        "", text,
    )
    return re.sub(r"^(?:@\S+\s+)+", "", text)


_EXIT_COMMANDS = frozenset({
    "/取消", "/结束", "#取消", "#结束", "/退出问卷", "#退出问卷",
})
_EXIT_WORDS = frozenset({
    "取消", "退出", "结束", "算了", "不填了", "退出问卷",
})


def _is_exit_text(text: str) -> bool:
    normalized = _strip_mentions(_normalize_command_text(text))
    normalized = normalized.strip(" \t，,。.;；!！")
    return normalized in _EXIT_COMMANDS or normalized in _EXIT_WORDS


_VERBS = {
    "问卷": "start",
    "问卷诊断": "diag",
    "问卷答案": "show",
    "问卷详情": "detail",
    "确认提交": "confirm",
    "取消问卷": "cancel",
    "退出问卷": "cancel",
    "重试提交": "retry",
    "问卷释放": "release",
    "问卷选": "pick",
    "问卷跳过": "skip",
}


def is_survey_command(text: str) -> bool:
    """True when the text is any survey command (used by group dispatch)."""
    return _parse_command(text) is not None


SURVEY_PROMPT_STATIC = """## 问卷填写（Ark Re:Code 周年问卷）
用户可以用 /问卷 <链接> 开始填写（群聊需 @机器人）；系统先发概览，再逐题提问。
- 选择题点按钮，文本题直接回复；可选文本可发 /问卷跳过；/问卷答案 查看当前题，/问卷详情 查看审阅。
- 群聊任务约 5 分钟；/取消、/取消问卷 或 /退出问卷 退出；同一群同时只能有一个进行中的问卷。
- 只有用户本人发送 /确认提交 才会提交，且提交不可撤销；管理员可能未开启真实提交。
- 不要替用户编造答案、不要自动提交、不要声称已提交；用户问进度时先查看状态，再建议 /问卷答案。
"""


def prompt_static() -> str:
    """Static capability card injected into private and group prompts."""
    return SURVEY_PROMPT_STATIC


def status_payload(view) -> dict:
    """Read-only status for the survey_status tool (owner-scoped, no writes)."""
    try:
        if not enabled():
            return {"enabled": False, "active": False,
                    "message": "问卷功能当前未启用。"}
        identity = _identity(view)
        scope = _scope(view)
        is_group = bool(getattr(view, "is_group", False))
        lock = _locks.get(scope)
        if is_group and lock is not None and lock.owner != identity:
            return {"enabled": True, "active": False,
                    "message": "当前没有你名下的问卷任务。"}
        draft = _store().get(identity)
        if draft is None or draft.origin_scope != scope:
            ready, reason = (False, "unavailable")
            try:
                ready, reason = _browser_ready()
            except Exception:
                pass
            return {
                "enabled": True,
                "active": False,
                "submit_ready": bool(ready),
                "submit_reason": reason,
                "message": "当前没有进行中的问卷草稿。发送 /问卷 <链接> 开始。",
            }
        answered = sum(1 for answer in draft.answers.values()
                       if not answer.is_empty())
        skipped = len(draft.skipped)
        try:
            ready, reason = _browser_ready()
        except Exception:
            ready, reason = False, "unavailable"
        if draft.state == "submitting":
            message = "问卷正在提交，请稍候。"
        elif draft.state == "unknown":
            message = ("上次提交结果未知；如确认未提交，发送 /重试提交 确认。")
        elif draft.state == "failed":
            message = "上次提交失败；可发送 /问卷答案 检查答案，或 /重试提交。"
        else:
            message = ("可发送 /问卷答案 查看当前题，/问卷详情 查看审阅，"
                       "/确认提交 提交，/取消 退出。")
        payload = {
            "enabled": True,
            "active": True,
            "draft_id": draft.draft_id,
            "title": draft.title,
            "svid": draft.svid,
            "state": draft.state,
            "answered": answered,
            "skipped": skipped,
            "follow_up": bool(draft.follow_up_for),
            "submit_ready": bool(ready),
            "submit_reason": reason,
            "message": message,
        }
        if is_group and lock is not None:
            payload["group_remaining_min"] = max(
                0, int((lock.expires_at - time.time()) / 60))
        return payload
    except Exception:
        return {"enabled": bool(enabled()), "active": False,
                "message": "无法读取问卷状态。"}


def prompt_hint(view) -> str:
    """Per-turn survey state for the agent (owner-only, never raises)."""
    try:
        if not enabled():
            return ""
        identity = _identity(view)
        scope = _scope(view)
        is_group = bool(getattr(view, "is_group", False))
        lock = _locks.get(scope)
        if is_group and lock is not None and lock.owner != identity:
            return ""
        draft = _store().get(identity)
        if draft is None or draft.origin_scope != scope:
            return ""
        answered = sum(1 for answer in draft.answers.values()
                       if not answer.is_empty())
        skipped = len(draft.skipped)
        bits = [f"[问卷任务] 草稿 #{draft.draft_id}"]
        if draft.title:
            bits[0] += f"《{draft.title[:30]}》"
        bits.append(f"已填 {answered} 题" + (f"，跳过 {skipped} 题" if skipped else ""))
        if draft.follow_up_for:
            bits.append("当前在补充说明")
        if draft.state == "submitting":
            bits.append("正在提交")
        elif draft.state == "failed":
            bits.append("上次提交失败，可 /重试提交")
        elif draft.state == "unknown":
            bits.append("上次提交结果未知，需 /重试提交 确认")
        if is_group:
            if lock is not None:
                minutes = max(1, int((lock.expires_at - time.time()) / 60))
                bits.append(f"群聊窗口约剩 {minutes} 分钟")
            else:
                bits.append("群聊窗口已过期，可发 /问卷答案 继续")
        bits.append("用户可发 /问卷答案 查看当前题、/问卷详情 查看审阅、/取消 退出；"
                    "只有用户发 /确认提交 才会提交")
        return "；".join(bits)
    except Exception:
        return ""


def _parse_command(text: str) -> tuple[str, str] | None:
    """Parse a survey command, tolerating common chat-input shapes.

    Accepted: ``/问卷 https://…``, ``/问卷https://…``, ``/问卷：https://…``,
    ``／问卷 https://…`` (full-width slash) and a bare ``问卷 https://…``.
    """
    normalized = _strip_mentions(_normalize_command_text(text))
    if not normalized:
        return None
    if not normalized.startswith(("/", "#")):
        if normalized.startswith("问卷") and _extract_survey_url(normalized):
            return "start", normalized[len("问卷"):].lstrip(" :：,，、")
        return None
    body = normalized[1:]
    parts = body.split(maxsplit=1)
    first = parts[0]
    remainder = parts[1].strip() if len(parts) > 1 else ""
    action = _VERBS.get(first)
    if action is not None:
        return action, remainder
    # No space or extra punctuation between verb and argument.
    for verb in sorted(_VERBS, key=len, reverse=True):
        if body.startswith(verb):
            return _VERBS[verb], body[len(verb):].lstrip(" :：,，、")
    return None


# ── public entry ─────────────────────────────────────────────────

async def try_handle(view, sender=None) -> bool:
    """Handle one message for the survey flow; False = let the kernel decide."""
    text = (getattr(view, "text", "") or "").strip()
    command = _parse_command(text) if text else None
    # Diagnostics must work even when the feature is disabled.
    if command is not None and command[0] == "diag":
        return await _cmd_diag(view, _store(), sender)
    if not enabled():
        if "问卷" in text:
            print(f"[Survey] disabled; text={text[:120]!r}", file=sys.stderr)
        return False
    identity = _identity(view)
    scope = _scope(view)
    store = _store()
    _prune_locks()

    if not text:
        # A link-card-only message has no text but carries the URL in the raw
        # event; accept it while a link is pending.
        if _pending_start_valid(identity):
            url = _extract_url_from_view(view)
            if url:
                _pending_start.pop(identity, None)
                return await _dispatch("start", url, view, None, store, scope,
                                       sender)
        return False

    if command is None and (
        text.startswith(("/", "#", "问卷")) or "surveycake" in text.lower()
    ):
        print(f"[Survey] unparsed text={text[:140]!r}", file=sys.stderr)
    is_group = bool(getattr(view, "is_group", False))

    # Admin release must work even while another member holds the lock.
    if command is not None and command[0] == "release":
        return await _cmd_release(view, sender)

    draft = store.get(identity)

    if is_group:
        lock = _active_lock(scope)
        if lock is not None and lock.owner != identity:
            if command is not None:
                await _send(_lock_hint(lock), sender=sender)
                return True
            return False
    if draft is not None and _is_exit_text(text):
        await _cmd_cancel(view, draft, store, scope, sender)
        return True
    if command is not None:
        return await _dispatch(command[0], command[1], view, draft, store,
                               scope, sender)

    # Other slash/hash commands must never be eaten as survey answers.
    if text.startswith(("/", "#")):
        return False

    if draft is None:
        # A prior bare "/问卷" asked for the link; accept it as the next
        # message (the platform may deliver a link as its own message).
        if _pending_start_valid(identity):
            url = (_extract_url_from_view(view)
                   or _url_from_bare_svid(text)
                   or _url_from_bare_svid(_view_raw_text(view)))
            if url:
                _pending_start.pop(identity, None)
                return await _dispatch("start", url, view, None, store, scope,
                                       sender)
        return False
    # Plain text only auto-answers in the conversation that started the draft;
    # other chats must use explicit commands (/问卷答案, /问卷选, ...).
    if draft.origin_scope != scope:
        return False
    return await _handle_answer(view, draft, store, scope, sender)


async def _dispatch(action: str, rest: str, view, draft, store, scope, sender) -> bool:
    identity = _identity(view)
    if draft is not None:
        draft = store.get(identity) or draft
        if getattr(view, "is_group", False):
            _touch_or_acquire(scope, identity, draft.draft_id)

    if action == "start":
        return await _cmd_start(view, rest, draft, store, scope, sender)
    if action == "diag":
        return await _cmd_diag(view, store, sender)
    if action == "pick":
        return await _cmd_pick(view, rest, draft, store, scope, sender)
    if action == "skip":
        return await _cmd_skip(view, rest, draft, store, scope, sender)
    if action == "show":
        return await _cmd_show(view, draft, store, scope, sender)
    if action == "detail":
        return await _cmd_detail(view, draft, store, sender)
    if action == "confirm":
        return await _cmd_confirm(view, draft, store, scope, sender)
    if action == "cancel":
        return await _cmd_cancel(view, draft, store, scope, sender)
    if action == "retry":
        return await _cmd_retry(view, rest, draft, store, scope, sender)
    return False


# ── schema access ────────────────────────────────────────────────

async def _load_schema(svid: str) -> SurveyDef:
    """Indirection kept tiny so tests can stub the network call."""
    return await fetch_schema(svid)


async def _survey_for(draft: SurveyDraft) -> SurveyDef:
    survey = await _load_schema(draft.svid)
    if survey.schema_hash != draft.schema_hash:
        raise SurveyError("问卷题目已更新，请发送 /取消问卷 后重新开始")
    return survey


# ── command implementations ──────────────────────────────────────

async def _cmd_diag(view, store, sender) -> bool:
    """Echo what the survey flow actually received (remote debugging)."""
    identity = _identity(view)
    draft = store.get(identity)
    lines = [
        "问卷诊断：",
        f"enabled={enabled()}",
        f"platform={getattr(view, 'platform', '')} "
        f"group={bool(getattr(view, 'is_group', False))}",
        f"text={getattr(view, 'text', '')!r}",
        f"url={_extract_url_from_view(view)!r}",
        f"parts={_raw_segment_types(view)!r}",
        f"browser_ready={_browser_ready()}",
        f"env SURVEY_ENABLED={os.environ.get('SURVEY_ENABLED', '')!r} "
        f"SUBMIT={os.environ.get('SURVEY_ENABLE_SUBMIT', '')!r} "
        f"BROWSER={os.environ.get('SURVEY_BROWSER', '')!r}",
        f"draft={draft.draft_id if draft else 'none'}",
    ]
    await _send("\n".join(lines), sender)
    return True


async def _cmd_start(view, rest, draft, store, scope, sender) -> bool:
    identity = _identity(view)
    if draft is not None:
        await _send(
            f"已有进行中的问卷 #{draft.draft_id}（{draft.title}）。\n"
            "发送 /问卷答案 继续，或 /取消问卷 放弃后重新开始。",
            sender,
        )
        return True

    url = (_extract_survey_url(rest)
           or _extract_url_from_view(view)
           or _url_from_bare_svid(rest))
    if not url:
        _set_pending_start(identity)
        await _send(
            "请直接发送问卷链接（https://www.surveycake.com/s/...），\n"
            "也可以只发送问卷编号（如 ABb8y）。\n"
            "命令用法：/问卷 https://www.surveycake.com/s/ABb8y",
            sender,
        )
        return True

    print(f"[Survey] start raw={getattr(view, 'text', '')[:160]!r} url={url!r} "
          f"parts={_raw_segment_types(view)!r}", file=sys.stderr)
    try:
        svid = parse_survey_url(url)
        survey = await _load_schema(svid)
    except SurveyError as exc:
        await _send(f"⚠ {exc}", sender)
        return True
    except Exception as exc:                      # noqa: BLE001 - surfaced to user
        await _send(f"⚠ 读取问卷失败：{type(exc).__name__}", sender)
        return True

    _pending_start.pop(identity, None)
    draft = store.create(identity, scope, getattr(view, "group_id", "") or "", survey)
    if getattr(view, "is_group", False):
        _touch_or_acquire(scope, identity, draft.draft_id)
    await _send_with_buttons(
        _render_overview(survey, draft) + _group_task_hint(view),
        _overview_rows(draft),
        sender,
    )
    await _send_question(view, survey, draft, sender)
    return True


async def _cmd_pick(view, rest, draft, store, scope, sender) -> bool:
    if draft is None:
        await _send("当前没有进行中的问卷，请先发送 /问卷 <链接>。", sender)
        return True
    tokens = rest.split()
    if len(tokens) == 2:
        draft_id, question_token, option_token = "", tokens[0], tokens[1]
    elif len(tokens) == 3:
        draft_id, question_token, option_token = tokens
    else:
        await _send("用法：/问卷选 <草稿编号> <题号> <选项号>", sender)
        return True
    if draft_id and draft_id.lstrip("#").upper() != draft.draft_id:
        await _send("草稿编号不匹配，请使用最新消息里的按钮。", sender)
        return True
    try:
        question_number = int(question_token)
        option_number = int(option_token)
    except ValueError:
        await _send("题号和选项号都需要是数字，例如：/问卷选 A1B2C3 3 2", sender)
        return True

    try:
        survey = await _survey_for(draft)
    except SurveyError as exc:
        await _send(f"⚠ {exc}", sender)
        return True

    question = survey.question_by_number(question_number)
    if question is None or not question.is_choice:
        await _send("没有找到这道选择题，请发送 /问卷答案 查看当前草稿。", sender)
        return True
    if not 1 <= option_number <= len(question.options):
        await _send(f"选项号需在 1-{len(question.options)} 之间。", sender)
        return True

    option = question.options[option_number - 1]
    draft.answers[question.subject_id] = Answer(kind="choice", option_id=option.option_id)
    draft.skipped.discard(question.subject_id)
    if option.follow_up_input:
        draft.follow_up_for = question.subject_id
    elif draft.follow_up_for == question.subject_id:
        draft.follow_up_for = None
    store.save(draft)
    ack = [f"第 {survey.number_of(question.subject_id)} 题：{option_number}. {option.text}"]
    await _after_change(view, survey, draft, store, sender, ack)
    return True


async def _cmd_skip(view, rest, draft, store, scope, sender) -> bool:
    if draft is None:
        await _send("当前没有进行中的问卷，请先发送 /问卷 <链接>。", sender)
        return True
    tokens = rest.split()
    draft_id = ""
    question_number = None
    if len(tokens) == 1:
        token = tokens[0]
        if token.lstrip("#").upper() == draft.draft_id:
            question_number = None
        else:
            try:
                question_number = int(token)
            except ValueError:
                await _send("用法：/问卷跳过 <草稿编号> <题号>", sender)
                return True
    elif len(tokens) == 2:
        draft_id, question_token = tokens
        if draft_id.lstrip("#").upper() != draft.draft_id:
            await _send("草稿编号不匹配，请使用最新消息里的按钮。", sender)
            return True
        try:
            question_number = int(question_token)
        except ValueError:
            await _send("题号需要是数字。", sender)
            return True
    elif tokens:
        await _send("用法：/问卷跳过 <草稿编号> <题号>", sender)
        return True

    try:
        survey = await _survey_for(draft)
    except SurveyError as exc:
        await _send(f"⚠ {exc}", sender)
        return True

    # Skipping with no question number cancels a pending follow-up first.
    if question_number is None and draft.follow_up_for is not None:
        question = survey.question(draft.follow_up_for)
        if question is not None:
            draft.answers.pop(question.subject_id, None)
            draft.follow_up_for = None
            store.save(draft)
            await _after_change(
                view, survey, draft, store, sender,
                [f"第 {survey.number_of(question.subject_id)} 题：已取消该选项"],
            )
            return True

    question = (
        survey.question_by_number(question_number)
        if question_number is not None
        else _next_question(survey, draft)
    )
    if question is None:
        await _send("当前没有可以跳过的题目。", sender)
        return True
    if question.required:
        await _send(f"第 {survey.number_of(question.subject_id)} 题是必填题，不能跳过。", sender)
        return True

    draft.answers.pop(question.subject_id, None)
    draft.skipped.add(question.subject_id)
    if draft.follow_up_for == question.subject_id:
        draft.follow_up_for = None
    store.save(draft)
    await _after_change(
        view, survey, draft, store, sender,
        [f"第 {survey.number_of(question.subject_id)} 题：已跳过"],
    )
    return True


async def _cmd_show(view, draft, store, scope, sender) -> bool:
    if draft is None:
        await _send("当前没有进行中的问卷，请先发送 /问卷 <链接>。", sender)
        return True
    try:
        survey = await _survey_for(draft)
    except SurveyError as exc:
        await _send(f"⚠ {exc}", sender)
        return True
    await _send(_render_overview(survey, draft), sender)
    if draft.follow_up_for:
        await _send_question(view, survey, draft, sender,
                             question=survey.question(draft.follow_up_for))
    else:
        next_question = _next_question(survey, draft)
        if next_question is None:
            await _send_review(view, survey, draft, sender)
        else:
            await _send_question(view, survey, draft, sender, question=next_question)
    return True


async def _cmd_detail(view, draft, store, sender) -> bool:
    if draft is None:
        await _send("当前没有进行中的问卷，请先发送 /问卷 <链接>。", sender)
        return True
    try:
        survey = await _survey_for(draft)
    except SurveyError as exc:
        await _send(f"⚠ {exc}", sender)
        return True
    await _send_review(view, survey, draft, sender)
    return True


async def _cmd_confirm(view, draft, store, scope, sender) -> bool:
    if draft is None:
        await _send("当前没有进行中的问卷，请先发送 /问卷 <链接>。", sender)
        return True
    try:
        survey = await _survey_for(draft)
    except SurveyError as exc:
        await _send(f"⚠ {exc}", sender)
        return True

    record = store.submission_record(_identity(view), draft.svid)
    if record:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(record.get("submitted_at") or 0))
        await _send(
            f"该问卷已有提交记录（{when}）。\n为避免重复提交，同一问卷只能提交一次。",
            sender,
        )
        return True

    issues = validate_answers(survey, draft.answers)
    if issues:
        lines = ["⚠ 还不能提交："] + [f"- {item}" for item in issues]
        lines += ["", "发送 /问卷答案 查看当前草稿。"]
        await _send("\n".join(lines), sender)
        return True

    ready, reason = _browser_ready()
    if not ready:
        if reason == "browser_disabled":
            status = "提交功能已开启，但浏览器引擎未就绪（需 SURVEY_BROWSER=ruyipage）"
        else:
            status = "提交功能未启用"
        lines = [f"✅ 本地校验通过（{status}，本次不会真正提交）", ""]
        lines.extend(_review_lines(survey, draft))
        lines += [
            "",
            "管理员开启 SURVEY_ENABLE_SUBMIT=1 且 SURVEY_BROWSER=ruyipage 后，这里会真正提交。",
        ]
        await _send_with_buttons("\n".join(lines), _review_rows(draft), sender)
        return True
    return await _do_submit(view, survey, draft, store, scope, sender)


async def _cmd_cancel(view, draft, store, scope, sender) -> bool:
    _pending_start.pop(_identity(view), None)
    if draft is None:
        # A stale group lock must not outlive its draft.
        _locks.pop(scope, None)
        await _send("当前没有进行中的问卷。", sender)
        return True
    store.delete(_identity(view))
    _locks.pop(scope, None)
    _locks.pop(draft.origin_scope, None)
    await _send("问卷任务已取消。", sender)
    return True


async def _cmd_retry(view, rest, draft, store, scope, sender) -> bool:
    if draft is None:
        await _send("当前没有进行中的问卷，请先发送 /问卷 <链接>。", sender)
        return True
    if draft.state not in ("failed", "unknown", "submitting"):
        await _send("当前没有待重试的提交。", sender)
        return True
    if draft.state == "submitting":
        # The previous process died mid-submit; require an explicit confirm.
        draft.state = "unknown"
        store.save(draft)
    try:
        survey = await _survey_for(draft)
    except SurveyError as exc:
        await _send(f"⚠ {exc}", sender)
        return True
    if draft.state == "unknown" and "确认" not in rest:
        await _send(
            "上次提交结果未知，可能已经提交。\n"
            "如确认未提交，请发送 /重试提交 确认。",
            sender,
        )
        return True
    return await _do_submit(view, survey, draft, store, scope, sender)


async def _do_submit(view, survey, draft, store, scope, sender) -> bool:
    identity = _identity(view)
    issues = validate_answers(survey, draft.answers)
    if issues:
        lines = ["⚠ 还不能提交："] + [f"- {item}" for item in issues]
        lines += ["", "发送 /问卷答案 检查答案后重试。"]
        await _send("\n".join(lines), sender)
        return True
    record = store.submission_record(identity, draft.svid)
    if record:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(record.get("submitted_at") or 0))
        await _send(f"该问卷已有提交记录（{when}）。\n为避免重复提交，同一问卷只能提交一次。", sender)
        return True
    limit = _rate_limit_message(identity)
    if limit:
        await _send(limit, sender)
        return True
    if identity in _submitting:
        await _send("⏳ 上一次提交仍在进行，请稍候。", sender)
        return True
    if draft.state == "submitting":
        draft.state = "unknown"
        store.save(draft)
        await _send(
            "⚠ 上次提交没有正常结束，结果未知。\n"
            "如确认未提交，请发送 /重试提交 确认。",
            sender,
        )
        return True

    _submitting.add(identity)
    draft.state = "submitting"
    store.save(draft)
    try:
        store.prune_receipts(_receipt_days())
        screenshot = store.receipt_path(draft)
        await _send("⏳ 正在提交问卷（浏览器流程，约 10–60 秒）…", sender)
        started = time.time()
        try:
            result = await _submit_via_browser(survey, draft, screenshot)
        except Exception as exc:              # noqa: BLE001 - config/import errors
            result = {
                "ok": False, "stage": "error", "submitted": False,
                "error": f"{type(exc).__name__}: {exc}",
                "screenshot": "", "duration_ms": int((time.time() - started) * 1000),
            }
        finally:
            _submitting.discard(identity)
    except Exception as exc:                  # noqa: BLE001 - store/receipt failures
        _submitting.discard(identity)
        draft.state = "failed"
        store.save(draft)
        await _send(f"⚠ 提交前准备失败：{type(exc).__name__}", sender)
        return True

    duration = int(result.get("duration_ms") or 0)
    stage = str(result.get("stage") or "")
    outcome = "ok" if result.get("ok") else stage or "failed"
    try:
        store.append_attempt(draft, outcome, duration, str(result.get("error") or ""))
    except Exception:                         # noqa: BLE001 - never block the reply
        pass
    _record_attempt_time(identity)

    if result.get("ok"):
        member_id = _member_id(survey, draft)
        store.mark_submitted(draft, meta={
            "backend": "ruyipage",
            "stage": stage,
            "duration_ms": duration,
            "screenshot": os.path.basename(result.get("screenshot") or ""),
            # Persisted records stay PII-free; only the chat receipt is full.
            "member_masked": mask_member_id(member_id) if member_id else "",
        })
        _locks.pop(scope, None)
        _locks.pop(draft.origin_scope, None)
        await _send(_receipt_text(survey, draft, member_id), sender)
        shot = str(result.get("screenshot") or "")
        if shot and os.path.exists(shot):
            try:
                from .outbound import OutImage
                await _send(OutImage(path=shot, label="问卷提交回执"), sender)
            except Exception:                 # noqa: BLE001 - receipt is best-effort
                pass
        return True

    if result.get("submitted") or stage in ("submit_clicked", "unconfirmed"):
        draft.state = "unknown"
        store.save(draft)
        await _send(
            "⚠ 提交结果未知（" + (str(result.get("error") or "") or stage or "timeout") + "）。\n"
            "可能已经提交，请先到游戏信箱/感谢页确认；\n"
            "如确认未提交，发送 /重试提交 确认 再试一次。",
            sender,
        )
        return True

    draft.state = "failed"
    store.save(draft)
    await _send(
        "⚠ 提交失败：" + (str(result.get("error") or "") or stage or "未知原因") + "\n"
        "本次未提交成功。可发送 /问卷答案 检查答案，或 /重试提交。",
        sender,
    )
    return True


async def _cmd_release(view, sender) -> bool:
    if not getattr(view, "is_group", False):
        await _send("问卷释放仅在群聊中可用。", sender)
        return True
    if not _is_admin(view):
        await _send("只有群管理员可以释放问卷锁。", sender)
        return True
    scope = _scope(view)
    existed = _locks.pop(scope, None) is not None
    await _send("已释放本群问卷锁。" if existed else "本群当前没有问卷锁。", sender)
    return True


# ── free-text answers ───────────────────────────────────────────

async def _handle_answer(view, draft, store, scope, sender) -> bool:
    try:
        survey = await _survey_for(draft)
    except SurveyError as exc:
        await _send(f"⚠ {exc}", sender)
        return True

    text = (getattr(view, "text", "") or "").strip()
    applied: list[str] = []
    is_group = bool(getattr(view, "is_group", False))
    direct = bool(
        getattr(view, "mentioned", False)
        or getattr(view, "reply_to_bot", False)
        or not is_group
    )

    if draft.follow_up_for is not None:
        if not direct:
            return False
        question = survey.question(draft.follow_up_for)
        answer = draft.answers.get(draft.follow_up_for)
        if question is not None and answer is not None:
            answer.text = text[:1000]
            draft.follow_up_for = None
            applied = [
                f"第 {survey.number_of(question.subject_id)} 题补充说明：{text[:40]}"
            ]
        else:
            draft.follow_up_for = None
    else:
        applied, error = _parse_free_text(
            survey, draft, text, allow_fallback=direct
        )
        if error:
            if not direct:
                # Not a recognisable survey answer: let normal group chat
                # (or the agent) handle it instead of hijacking the message.
                return False
            current = _next_question(survey, draft)
            body = f"⚠ {error}"
            if current is not None:
                body += "\n\n" + _question_block(survey, draft, current)
                await _send_with_buttons(
                    body, _question_rows(survey, draft, current), sender
                )
            else:
                await _send(body, sender)
            return True
        if not applied:
            if not direct:
                return False
            current = _next_question(survey, draft)
            if current is None:
                await _send_review(view, survey, draft, sender)
            else:
                await _send_with_buttons(
                    "没有识别到答案。\n\n" + _question_block(survey, draft, current),
                    _question_rows(survey, draft, current),
                    sender,
                )
            return True

    store.save(draft)
    _touch_or_acquire(scope, _identity(view), draft.draft_id)
    await _after_change(view, survey, draft, store, sender, applied)
    return True


async def _after_change(view, survey, draft, store, sender, ack: list[str]) -> bool:
    store.save(draft)
    parts: list[str] = []
    if ack:
        parts.append("已记录：\n" + "\n".join(f"- {item}" for item in ack))

    if draft.follow_up_for is not None:
        question = survey.question(draft.follow_up_for)
        answer = draft.answers.get(draft.follow_up_for) if question else None
        option = question.option(answer.option_id) if (question and answer) else None
        if question is not None and option is not None:
            number = survey.number_of(question.subject_id)
            parts.append(
                f"请补充「{option.text}」的说明，直接回复文字；\n"
                f"或发送 /问卷跳过 {draft.draft_id} {number} 取消该选项。"
            )
            rows = [[Btn(
                label="取消该选项",
                data=f"/问卷跳过 {draft.draft_id} {number}",
                action="cmd",
            )]]
            await _send_with_buttons(
                "\n\n".join(parts) + _group_task_hint(view), rows, sender
            )
            return True
        draft.follow_up_for = None

    next_question = _next_question(survey, draft)
    if next_question is None:
        if parts:
            await _send("\n\n".join(parts), sender)
        await _send_review(view, survey, draft, sender)
        return True

    parts.append(_question_block(survey, draft, next_question))
    rows = _question_rows(survey, draft, next_question)
    await _send_with_buttons("\n\n".join(parts), rows, sender)
    return True


def _parse_free_text(survey: SurveyDef, draft: SurveyDraft,
                     text: str, *, allow_fallback: bool = True
                     ) -> tuple[list[str], str | None]:
    """Map one plain-text message to draft answers (deterministic rules).

    P0 deliberately avoids an LLM here: the stepwise prompts plus buttons
    already make the flow usable, and deterministic parsing keeps the P0
    acceptance tests offline.
    """
    applied: list[str] = []
    seen: set[int] = set()

    def record(question: SurveyQuestion, answer: Answer, label: str) -> None:
        if question.subject_id in seen:
            return
        draft.answers[question.subject_id] = answer
        draft.skipped.discard(question.subject_id)
        seen.add(question.subject_id)
        applied.append(label)

    stripped = text.strip()

    # 1) Skip words apply to the current question.
    if stripped in ("跳过", "不填", "略过", "skip", "SKIP"):
        current = _next_question(survey, draft)
        if current is None:
            return [], "当前没有可以跳过的题目。"
        if current.required:
            return [], f"第 {survey.number_of(current.subject_id)} 题是必填题，不能跳过。"
        draft.skipped.add(current.subject_id)
        draft.answers.pop(current.subject_id, None)
        draft.follow_up_for = None
        return [f"第 {survey.number_of(current.subject_id)} 题：已跳过"], ""

    # 2) Member / account id.
    id_question = _find_text_question(survey, ("会员编号", "會員編號", "会员码", "编号", "編號"))
    if id_question is not None:
        value = _extract_member_id(stripped)
        if value:
            record(
                id_question,
                Answer(kind="text", text=value),
                f"第 {survey.number_of(id_question.subject_id)} 题：{value}",
            )

    # 3) Choice questions by keyword + option number.
    for question in survey.questions:
        if not question.is_choice:
            continue
        keywords = _choice_keywords(question)
        if not keywords:
            continue
        pattern = "|".join(re.escape(word) for word in keywords)
        match = re.search(rf"(?:{pattern})[^\d]{{0,10}}(\d{{1,2}})", stripped)
        if not match:
            continue
        number = int(match.group(1))
        if not 1 <= number <= len(question.options):
            continue
        option = question.options[number - 1]
        record(
            question,
            Answer(kind="choice", option_id=option.option_id),
            f"第 {survey.number_of(question.subject_id)} 题：{number}. {option.text}",
        )
        if option.follow_up_input:
            draft.follow_up_for = question.subject_id
        elif draft.follow_up_for == question.subject_id:
            draft.follow_up_for = None

    # 4) Named long-text questions (favourite member / message).
    for question in survey.questions:
        if not question.is_text or question.type != "TXTLONG":
            continue
        value = _extract_named_text(question, stripped)
        if value:
            record(
                question,
                Answer(kind="text", text=value[:1000]),
                f"第 {survey.number_of(question.subject_id)} 题：{value[:40]}",
            )

    if applied:
        return applied, ""

    if not allow_fallback:
        return [], ""

    # 5) Fallback: the whole message answers the current question.
    current = _next_question(survey, draft)
    if current is None:
        return [], ""
    if current.is_text:
        record(
            current,
            Answer(kind="text", text=stripped[:1000]),
            f"第 {survey.number_of(current.subject_id)} 题：{stripped[:40]}",
        )
        return applied, ""
    if current.is_choice:
        match = re.fullmatch(r"\s*(?:选|第)?\s*(\d{1,2})\s*(?:个|项|号)?\s*", stripped)
        if match:
            number = int(match.group(1))
            if 1 <= number <= len(current.options):
                option = current.options[number - 1]
                record(
                    current,
                    Answer(kind="choice", option_id=option.option_id),
                    f"第 {survey.number_of(current.subject_id)} 题：{number}. {option.text}",
                )
                if option.follow_up_input:
                    draft.follow_up_for = current.subject_id
                return applied, ""
        return [], (
            f"第 {survey.number_of(current.subject_id)} 题是选择题，"
            f"请回复选项编号（1-{len(current.options)}）或点击按钮。"
        )
    return [], ""


def _find_text_question(survey: SurveyDef,
                        needles: tuple[str, ...]) -> SurveyQuestion | None:
    for question in survey.questions:
        if question.is_text and any(word in question.text for word in needles):
            return question
    return next((q for q in survey.questions if q.is_text and q.required), None)


def _choice_keywords(question: SurveyQuestion) -> tuple[str, ...]:
    if "服装" in question.text or "衣服" in question.text:
        return ("服装", "衣服", "服饰")
    if "体位" in question.text or "姿势" in question.text:
        return ("体位", "姿势")
    return ()


_FAVORITE_RE = re.compile(
    r"(?:最喜欢|最喜歡|喜欢|喜歡|团员|團員)[:：]?\s*([^，,。；;\n]+)"
)
_MESSAGE_RE = re.compile(r"(?:留言|想说的话|想說的話|建议|建議|其他)[:：]\s*([^\n]+)")
_MEMBER_ID_RE = re.compile(
    r"(?:会员编号|會員編號|会员码|编号|編號|member\s*id)\s*[:：]?\s*([A-Za-z0-9]{6,20})",
    re.IGNORECASE,
)


def _extract_member_id(text: str) -> str:
    match = _MEMBER_ID_RE.search(text)
    if match:
        return match.group(1)
    if re.fullmatch(r"[A-Za-z0-9]{9}", text.strip()):
        return text.strip()
    return ""


def _extract_named_text(question: SurveyQuestion, text: str) -> str:
    if any(word in question.text for word in ("团员", "團員", "最喜欢", "最喜歡")):
        match = _FAVORITE_RE.search(text)
        if match:
            value = match.group(1).strip(" 　的")
            if value and value not in ("谁", "誰", "什么", "什麼"):
                return value
    if any(word in question.text for word in ("留言", "想说", "想說", "建议", "建議", "其他")):
        match = _MESSAGE_RE.search(text)
        if match:
            return match.group(1).strip()
    return ""


# ── rendering ───────────────────────────────────────────────────

def _render_overview(survey: SurveyDef, draft: SurveyDraft) -> str:
    """First message: a short summary, not the full form."""
    meta = []
    if survey.deadline_hint:
        meta.append(f"截止 {survey.deadline_hint}")
    meta.append(f"{len(survey.questions)} 题")
    meta.append(f"必填 {survey.required_count} 题")
    lines = [f"📋 {survey.title}", "｜".join(meta), f"草稿 #{draft.draft_id}", "", "题目概览："]
    for question in survey.questions:
        number = survey.number_of(question.subject_id)
        suffix = "（必填）" if question.required else ""
        if question.is_choice:
            suffix += f"（{len(question.options)} 个选项）"
        text = question.text.strip()
        if len(text) > 30:
            text = text[:29] + "…"
        lines.append(f"{number}) {text}{suffix}")
    lines += [
        "",
        "接下来会一题一题提问：选择题点按钮，文本题直接回复。",
        "发送 /取消 可随时退出。",
    ]
    return "\n".join(lines)


def _question_block(survey: SurveyDef, draft: SurveyDraft,
                    question: SurveyQuestion) -> str:
    number = survey.number_of(question.subject_id)
    total = len(survey.questions)
    suffix = "（必填）" if question.required else "（可选）"
    lines = [f"第 {number}/{total} 题 · {question.text}{suffix}"]
    if question.desc:
        lines.append(question.desc)
    if question.is_choice:
        lines.append("")
        for index, option in enumerate(question.options, 1):
            extra = "（需补充说明）" if option.follow_up_input else ""
            lines.append(f"{index}. {option.text}{extra}")
        lines += ["", "点击下方按钮选择，或直接回复数字。"]
    elif question.is_text:
        if question.length_bounds:
            low, high = question.length_bounds
            if low == high:
                lines.append(f"请直接回复 {low} 位内容。")
            else:
                lines.append(f"请直接回复内容（{low}-{high} 个字符）。")
        else:
            lines.append("请直接回复内容。")
        if not question.required:
            lines.append("（也可以发送 /问卷跳过 跳过此题）")
    return "\n".join(lines)


def _add_button(rows: Rows, button: Btn) -> None:
    """Append a button within the platform limit (3/row, 5 rows, 15 total)."""
    if sum(len(row) for row in rows) >= 15:
        return
    if rows and len(rows[-1]) < 3:
        rows[-1].append(button)
        return
    if len(rows) < 5:
        rows.append([button])


def _question_rows(survey: SurveyDef, draft: SurveyDraft,
                   question: SurveyQuestion) -> Rows:
    number = survey.number_of(question.subject_id)
    rows: Rows = []
    if question.is_choice:
        for index, _option in enumerate(
                question.options[:_MAX_OPTION_BUTTONS], 1):
            _add_button(rows, Btn(
                label=f"选{index}",
                data=f"/问卷选 {draft.draft_id} {number} {index}",
                action="cmd",
            ))
        if not question.required:
            _add_button(rows, Btn(
                label="跳过",
                data=f"/问卷跳过 {draft.draft_id} {number}",
                action="cmd",
            ))
    elif not question.required:
        _add_button(rows, Btn(
            label="跳过",
            data=f"/问卷跳过 {draft.draft_id} {number}",
            action="cmd",
        ))

    # Every question (including the required first text question) gets an
    # explicit exit button; previously the required text question had none.
    _add_button(rows, Btn(
        label="取消问卷",
        data=f"/取消问卷 {draft.draft_id}",
        action="cmd",
    ))
    return rows


def _review_lines(survey: SurveyDef, draft: SurveyDraft) -> list[str]:
    lines = []
    for question in survey.questions:
        number = survey.number_of(question.subject_id)
        shown = describe_answer(question, draft.answers.get(question.subject_id))
        if not shown:
            shown = "（必填未填）" if question.required else "（未填）"
        lines.append(f"{number}) {question.text[:24]}：{shown}")
    return lines


def _overview_rows(draft: SurveyDraft) -> Rows:
    return [[
        Btn(label="取消问卷", data=f"/取消问卷 {draft.draft_id}", action="cmd"),
    ]]


def _review_rows(draft: SurveyDraft) -> Rows:
    return [[
        Btn(label="确认提交", data=f"/确认提交 {draft.draft_id}", action="cmd"),
        Btn(label="取消", data=f"/取消问卷 {draft.draft_id}", action="cmd"),
        Btn(label="查看详情", data=f"/问卷详情 {draft.draft_id}", action="cmd"),
    ]]


def _is_member_id_question(question: SurveyQuestion) -> bool:
    return any(word in question.text for word in ("会员编号", "會員編號", "会员码", "编号", "編號"))


async def _send_question(view, survey: SurveyDef, draft: SurveyDraft,
                         sender, question: SurveyQuestion | None = None) -> None:
    question = question or _next_question(survey, draft)
    if question is None:
        await _send_review(view, survey, draft, sender)
        return
    await _send_with_buttons(
        _question_block(survey, draft, question) + _group_task_hint(view),
        _question_rows(survey, draft, question),
        sender,
    )


async def _send_review(view, survey: SurveyDef, draft: SurveyDraft, sender) -> None:
    issues = validate_answers(survey, draft.answers)
    lines = [f"请确认（草稿 #{draft.draft_id}）", ""]
    lines.extend(_review_lines(survey, draft))
    if issues:
        lines += ["", "⚠ 还不能提交："] + [f"- {item}" for item in issues]
    else:
        lines += ["", "✅ 本地校验通过。"]
    lines += ["", "发送 /确认提交 才会提交；发送 /取消 放弃。"]
    await _send_with_buttons(
        "\n".join(lines) + _group_task_hint(view), _review_rows(draft), sender
    )


def _next_question(survey: SurveyDef, draft: SurveyDraft) -> SurveyQuestion | None:
    for question in survey.questions:
        if question.subject_id in draft.skipped:
            continue
        answer = draft.answers.get(question.subject_id)
        if answer is None or answer.is_empty():
            return question
        if question.is_choice:
            option = question.option(answer.option_id)
            if option is not None and option.follow_up_input and not (answer.text or "").strip():
                return question
    return None


# ── public-command table fallback ───────────────────────────────

async def handle_public_command(text: str, user_id: str) -> bool:
    """Fallback for the ``PUBLIC_COMMANDS`` table (no view context).

    The real flow runs earlier in the kernel; this only tells users how to
    start when the message reached the generic public-command dispatcher.
    Reaching here with a URL means the kernel hook is missing/disabled.
    """
    print(f"[Survey] public fallback reached; text={text[:120]!r}",
          file=sys.stderr)
    if not enabled():
        await _send("问卷功能当前未启用。")
        return True
    await _send(
        "发送 /问卷 <链接> 开始填写，例如：\n"
        "/问卷 https://www.surveycake.com/s/ABb8y"
    )
    return True

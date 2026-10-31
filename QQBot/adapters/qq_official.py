"""
QQ official bot adapter — stage 4 of the QQ-official worktree plan.

The official-API mirror of adapters/onebot.py: converts QQ official
events into agent.inbound.InboundMessage views and implements the
agent.outbound.Sender contract on top of the passive-reply API.

Official-route realities that shape this module (plan §7.5 / analysis):
  - identity is opaque openids (user_openid / group_openid), never QQ
    numbers — permission tables are selected by PermissionManager via
    the _current_platform contextvar (plan §7.7);
  - passive replies are quota-limited (group: 5 per 5min per message,
    c2c: 4 per 60min per message) → QQOfficialSender buffers text,
    merges fragments at flush(), swallows progress lines and drops
    quota overflow with a warning;
  - the API cannot read local files: OutImage.path → base64 upload via
    MessageSegment.file_image(); OutImage.url → platform-side fetch;
  - inbound attachments arrive as public https URLs and are downloaded
    with the same neutral URL→disk helper the OneBot resolver uses
    (uuid-prefixed naming, size cap, magic-byte extension detection,
    per-user workspace uploads dir), so the kernel sees identical
    Attachment semantics on both routes;
  - a *quoted* message (message_type=103) carries the referenced content
    and its attachments inside msg_elements[] (indexed by
    message_scene.ext ref_msg_idx); the adapter's _check_reply() also
    resolves that element into event.reply. Both are read here: the quoted
    text becomes reply_context and the quoted attachments are downloaded
    with quoted=True, so the kernel can tell "the user just sent this"
    from "the user is pointing at this".
"""
from __future__ import annotations

import asyncio
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from nonebot import logger
from nonebot.adapters.qq import Bot as QQBot, MessageSegment
from nonebot.adapters.qq.event import (
    C2CMessageCreateEvent,
    GroupAtMessageCreateEvent,
    GroupMessageCreateEvent,
    QQMessageEvent,
)
from nonebot.adapters.qq.exception import (
    ActionFailed,
    NetworkError,
    RateLimitException,
)
from nonebot.adapters.qq.message import Message

from agent.inbound import Attachment, InboundMessage
from agent.outbound import OutAudio, OutImage, OutKeyboard
from lib.button_spec import clip_rows, to_text
from lib.button_spec import validate as validate_buttons

# Reuse the neutral URL→disk download helper (stage 2 parked it in the
# OneBot adapter; its bot=None path is pure httpx and platform-neutral).
# Importing beats duplicating ~100 lines of naming/size/magic-byte logic
# that must not drift between the two routes.
from adapters.onebot import _download_and_save_file
# Rows → wire MessageKeyboard (leaf module; it reaches back into THIS
# module only inside functions, so the top-level import is acyclic).
from adapters.qq_official_buttons import to_message_keyboard
# Streaming transport (same acyclic discipline: qq_official_stream
# reaches back into this module only via function-level lazy imports).
from adapters.qq_official_stream import MessageStreamer, _stream_available

# Conservative per-message text cap for merged payloads. The kernel's
# _send_response already chunks at 300 chars; this only guards the
# merged result against the official content length limit.
_TEXT_CHUNK_LIMIT = 1500

# 官方富媒体「二进制直传」上限 5MB（超过须走分片上传；URL 上传软限制
# 20MB）。见 docs/qqbot-api-v2-full-docs.md 单聊富媒体上传节。
_BINARY_UPLOAD_LIMIT = 5 * 1024 * 1024


# ── Markdown (msg_type=2) 能力层 ───────────────────────────────────
# 2026/04/23 起单聊/群聊自定义 markdown 对所有机器人开放（无需模板），
# 见 docs/qqbot-api-v2-full-docs.md §36。单条长度上限官方未公布（通用
# 40054007 消息长度超限）。原设计稿 §6.10.10 保守取 1500 字，但 /帮助
# 等长文被迫拆条——调至 2000 字；若真机撞 40054007 会走既有纯文本降级
# 链（内容不丢）。切分只落在区块边界（空行 / 标题行），绝不切在行中。
_MD_CHUNK_LIMIT = 2000

# markdown 被拒的平台错误码 → 同内容纯文本降级重发一次（内容不丢）。
# 来源：docs/qqbot-api-v2-full-docs.md 错误码表（50037/50041/50054-57、
# 304036、40034006-40034011、40034124、40034127）与群聊 40054010
# （不允许发送 URL）。
_MD_REJECT_CODES = frozenset({
    50037, 50041, 50054, 50055, 50056, 50057,
    304036,
    40034006, 40034008, 40034009, 40034010, 40034011,
    40034124, 40034127,
    40054010,
})

# 熔断器：连续 _MD_BREAKER_THRESHOLD 次被拒 → 进程内禁用 markdown
# _MD_BREAKER_COOLDOWN 秒（期间 send_markdown 直接降级纯文本），防止
# 权限/政策意外时每条回复都白付一次失败请求。冷却期满自动半开重试。
_MD_BREAKER_THRESHOLD = 3
_MD_BREAKER_COOLDOWN = 600.0


@dataclass
class _MdBreaker:
    """Module-level markdown circuit breaker state (design §8.3 pattern)."""
    rejects: int = 0
    disabled_until: float = 0.0


_md_breaker = _MdBreaker()


def _md_env_enabled() -> bool:
    """QQ_OFFICIAL_MARKDOWN=off/0/false/no → 全渠道禁用（热读，逃生门）。"""
    return os.environ.get("QQ_OFFICIAL_MARKDOWN", "on").strip().lower() \
        not in ("off", "0", "false", "no")


def _md_available() -> bool:
    """markdown 当前是否可发：env 开关 && 熔断器未打开。"""
    if not _md_env_enabled():
        return False
    if _md_breaker.disabled_until:
        if time.monotonic() < _md_breaker.disabled_until:
            return False
        # 冷却期满：半开 —— 清零计数，下一条 md 即试探
        _md_breaker.disabled_until = 0.0
        _md_breaker.rejects = 0
        logger.info("[qq_official] markdown breaker half-open; retrying")
    return True


def _md_record_reject() -> None:
    _md_breaker.rejects += 1
    if (_md_breaker.rejects >= _MD_BREAKER_THRESHOLD
            and not _md_breaker.disabled_until):
        _md_breaker.disabled_until = time.monotonic() + _MD_BREAKER_COOLDOWN
        logger.warning(
            f"[qq_official] markdown rejected {_md_breaker.rejects}× "
            f"consecutively; degrading to plain text for "
            f"{_MD_BREAKER_COOLDOWN:.0f}s")


def _md_record_success() -> None:
    _md_breaker.rejects = 0


# ── Keyboard (消息按钮) 能力层 ─────────────────────────────────────
# 键盘只能挂在 msg_type=2（markdown）消息上，且自定义按钮属【内邀开通】
# 能力——未开通统一 304037。错误码语义（docs 错误码表 + 设计稿 §8）：
#   304037 没有发消息按钮组件的权限（未内邀）→ unsupported + 长熔断
#   304038/304039/304040 组件不存在/解析错/内容错 → 载荷 bug，ERROR 日志
#   305007 键盘样式参数错误 → 字段类型 bug，ERROR 日志
#   40034029 内联键盘行/列超限、304062 按钮数量上限 → 先裁剪重试一次
# 其余连续被拒走短熔断（3 次/600s），任何键盘失败都不丢正文：
# kb → md-only（L1）→ 纯文本+命令清单（L2）。
_KB_REJECT_CODES = frozenset({
    304037, 304038, 304039, 304040, 305007, 40034029, 304062,
})
_KB_CLIP_CODES = frozenset({40034029, 304062})
_KB_BUG_CODES = frozenset({304038, 304039, 304040, 305007})
_KB_BREAKER_THRESHOLD = 3
_KB_BREAKER_COOLDOWN = 600.0


@dataclass
class _KbBreaker:
    """Module-level keyboard circuit-breaker state (design §8.3).

    ``state``: unknown（尚未探测）| ok（平台已接受过键盘）|
    unsupported（吃过 304037，冷却期内直接 L1）。半开：冷却期满
    state 回 unknown，下一条键盘即试探。
    """
    state: str = "unknown"
    rejects: int = 0
    disabled_until: float = 0.0
    rejected_total: int = 0
    last_reject_code: int | None = None
    last_reject_at: float = 0.0


_kb_breaker = _KbBreaker()


def _kb_env() -> str:
    """QQ_OFFICIAL_BUTTONS：auto（默认，探测+熔断）| on | off（热读逃生门）。"""
    return (os.environ.get("QQ_OFFICIAL_BUTTONS", "auto").strip().lower()
            or "auto")


def _kb_unsupported_hours() -> float:
    try:
        return max(0.0, float(os.environ.get(
            "QQ_OFFICIAL_BUTTONS_CIRCUIT_HOURS", "24")))
    except ValueError:
        return 24.0


def _kb_available() -> bool:
    """键盘当前是否可发：env≠off && markdown 可发 && 熔断未开。

    键盘必须骑 markdown（msg_type=2），所以 _md_available() 是硬前置；
    熔断冷却期满自动半开（清计数、unsupported→unknown 重新探测）。
    """
    if _kb_env() == "off":
        return False
    if not _md_available():
        return False
    if _kb_breaker.disabled_until:
        if time.monotonic() < _kb_breaker.disabled_until:
            return False
        _kb_breaker.disabled_until = 0.0
        _kb_breaker.rejects = 0
        if _kb_breaker.state == "unsupported":
            _kb_breaker.state = "unknown"
        logger.info("[qq_official] keyboard breaker half-open; retrying")
    return True


def _kb_record_reject(code) -> None:
    _kb_breaker.rejected_total += 1
    _kb_breaker.last_reject_code = code
    _kb_breaker.last_reject_at = time.time()
    if code == 304037:
        # 权限未开通：长熔断，期间所有键盘直接 L1，不再逐条试错
        _kb_breaker.state = "unsupported"
        _kb_breaker.rejects = 0
        _kb_breaker.disabled_until = (time.monotonic()
                                      + _kb_unsupported_hours() * 3600.0)
        logger.warning(
            "[qq_official] keyboard permission not granted (304037); "
            f"buttons disabled for {_kb_unsupported_hours():.0f}h")
        return
    if code in _KB_BUG_CODES:
        logger.error(
            f"[qq_official] keyboard payload rejected (code={code}) — "
            "likely a render bug; check recent button payloads")
    _kb_breaker.rejects += 1
    if (_kb_breaker.rejects >= _KB_BREAKER_THRESHOLD
            and not _kb_breaker.disabled_until):
        _kb_breaker.disabled_until = time.monotonic() + _KB_BREAKER_COOLDOWN
        logger.warning(
            f"[qq_official] keyboard rejected {_kb_breaker.rejects}× "
            f"consecutively; degrading to markdown-only for "
            f"{_KB_BREAKER_COOLDOWN:.0f}s")


def _kb_record_success() -> None:
    _kb_breaker.rejects = 0
    if _kb_breaker.state != "ok":
        _kb_breaker.state = "ok"
        logger.info("[qq_official] keyboard accepted by platform (state=ok)")


def kb_snapshot() -> dict:
    """Keyboard capability snapshot for the status heartbeat (§10.1)."""
    left = 0.0
    if _kb_breaker.disabled_until:
        left = max(0.0, round(_kb_breaker.disabled_until - time.monotonic(), 1))
    return {
        "state": _kb_breaker.state,
        "env": _kb_env(),
        "available": _kb_available(),
        "rejects": _kb_breaker.rejects,
        "rejected_total": _kb_breaker.rejected_total,
        "last_reject_code": _kb_breaker.last_reject_code,
        "last_reject_at": _kb_breaker.last_reject_at,
        "disabled_seconds_left": left,
    }


def _event_user_openid(event) -> str:
    """The acting user's openid (group: member_openid; c2c: user_openid)."""
    author = getattr(event, "author", None)
    return str(getattr(author, "member_openid", "")
               or getattr(author, "user_openid", "") or "")


def _iter_segments(message):
    """Message | MessageSegment → iterable of segments."""
    if isinstance(message, Message):
        return list(message)
    return [message]


def _message_has_markdown(message) -> bool:
    return any(getattr(s, "type", "") == "markdown"
               for s in _iter_segments(message))


def _md_seg_content(message) -> str:
    """The markdown text inside a segment/Message ('' when absent)."""
    for seg in _iter_segments(message):
        if getattr(seg, "type", "") == "markdown":
            md = seg.data.get("markdown")
            content = getattr(md, "content", None) or (
                md.get("content") if isinstance(md, dict) else None) or ""
            return str(content)
    return ""


def _drop_by_priority(payloads: list[tuple[MessageSegment, str]],
                      n_drop: int) -> list[tuple[MessageSegment, str]]:
    """Choose which payloads to drop when the passive-reply quota is short.

    Order (§6.10.4): media (image/audio) first, then text — in both
    passes from the TAIL, because the disclaimer trails the body and a
    multi-chunk body's last chunk matters less than its first. Survivor
    order is preserved. 正文永不丢 unless nothing else can be sacrificed.
    """
    if n_drop <= 0:
        return payloads
    dropped: set[int] = set()
    for pass_kinds in (("media",), ("media", "markdown", "text")):
        for i in range(len(payloads) - 1, -1, -1):
            if n_drop <= 0:
                break
            if i not in dropped and payloads[i][1] in pass_kinds:
                dropped.add(i)
                n_drop -= 1
        if n_drop <= 0:
            break
    if dropped:
        logger.warning(
            f"[qq_official] reply quota short: dropped {len(dropped)} "
            f"payload(s) (media first, tail-first)")
    return [p for i, p in enumerate(payloads) if i not in dropped]


_MD_BLOCK_RE = re.compile(r"\n\n+|\n(?=#{1,6} )")


def _split_markdown(text: str, limit: int = _MD_CHUNK_LIMIT) -> list[str]:
    """Split over-long markdown at block boundaries (blank line / heading).

    Greedy packing keeps each chunk ≤ limit; a single oversized block is
    hard-cut (rare — the kernel already caps model output). Chunks rejoin
    paragraphs with a blank line so md paragraph semantics survive.
    """
    if len(text) <= limit:
        return [text]
    blocks = [b.strip("\n") for b in _MD_BLOCK_RE.split(text) if b.strip()]
    out: list[str] = []
    buf = ""
    for block in blocks:
        while len(block) > limit:
            if buf:
                out.append(buf)
                buf = ""
            out.append(block[:limit])
            block = block[limit:]
        if not block:
            continue
        if buf and len(buf) + 2 + len(block) > limit:
            out.append(buf)
            buf = block
        else:
            buf = f"{buf}\n\n{block}" if buf else block
    if buf:
        out.append(buf)
    return out or [text[:limit]]


def _markdown_text_fallback(seg: MessageSegment,
                            err: ActionFailed) -> MessageSegment | None:
    """markdown 段被平台拒收 → 同内容纯文本段（msg_type=0）降级。

    只对 _MD_REJECT_CODES 里的 markdown 专属错误码降级并计入熔断；
    其它拒绝（审核/配额）原样返回 None 走既有 log-and-drop 路径。
    """
    if getattr(seg, "type", "") != "markdown":
        return None
    code = getattr(err, "code", None)
    if code not in _MD_REJECT_CODES:
        return None
    _md_record_reject()
    md = seg.data.get("markdown")
    content = getattr(md, "content", None) or (
        md.get("content") if isinstance(md, dict) else None) or ""
    return MessageSegment.text(str(content))


def _audio_as_file_fallback(seg: MessageSegment) -> MessageSegment | None:
    """file_audio 被平台拒收 → 同字节降级为文件卡（file_type=4）。

    官方文档两处口径冲突：单聊富媒体上传页写「语音支持 silk」，富媒体
    概述页写「语音支持 silk/mp3/wav/ogg，发送后展示语音条」。策略：先按
    语音条发（file_type=3），若被拒（如 850019 不支持的文件格式）改用
    file_type=4（任意格式，展示为可下载文件卡）——用户仍拿到 mp3。
    """
    if seg is None or getattr(seg, "type", "") != "file_audio":
        return None
    data = seg.data.get("content")
    if not isinstance(data, (bytes, bytearray)):
        return None
    return MessageSegment.file_file(
        bytes(data), file_name=seg.data.get("file_name")
    )


# The official API delivers inline QQ faces inside event.content as raw
# pseudo-XML tags, e.g.:
#   <faceType=6,faceId="0",ext="eyJ0ZXh0IjoiIn0=">
# ext is base64 (alphabet A-Za-z0-9+/=) so it can never contain '>',
# making [^>]* a safe span. Left in, this noise goes verbatim into the
# LLM prompt; stripped, a face-only message degrades to empty text and
# hits the kernel's "nothing to process" guard — same net behavior as
# the OneBot route, where faces arrive as segments the text walk skips.
_FACE_TAG_RE = re.compile(r"<faceType=[^>]*>")


def _clean_content(raw: str | None) -> str:
    """Official event.content → kernel-ready text (face tags removed)."""
    return _FACE_TAG_RE.sub("", raw or "").strip()


# A quote can carry a whole merged chat history; truncate so one long quote
# cannot eat the prompt budget (the full text is still on the platform).
_QUOTE_TEXT_LIMIT = 400


@dataclass
class _Quoted:
    """The quoted message: its text plus every attachment it carried."""
    text: str = ""
    attachments: list = field(default_factory=list)
    author_name: str = ""


def _scene_ext(event: QQMessageEvent) -> dict[str, str]:
    """message_scene.ext (["msg_idx=…", "ref_msg_idx=…"]) → dict."""
    scene = getattr(event, "message_scene", None)
    out: dict[str, str] = {}
    for item in (getattr(scene, "ext", None) or []):
        if isinstance(item, str) and "=" in item:
            key, value = item.split("=", 1)
            out[key.strip()] = value.strip()
    return out


def _quoted_message(event: QQMessageEvent) -> _Quoted:
    """Extract the quoted message (text + attachments) from an inbound event.

    Official API: replying to a message delivers message_type=103 with the
    referenced content in msg_elements[] and its index in
    message_scene.ext.ref_msg_idx; nonebot-adapter-qq >= 1.7 also resolves
    the referenced element into event.reply. Prefer event.reply (that is the
    element the platform says was quoted) and fall back to msg_elements.

    Attachments are gathered from every element, not just the referenced
    one: nested/merged quotes (message_type 101/102) put their payload one
    level deeper, and deduping by URL below keeps that harmless.
    """
    elements = list(getattr(event, "msg_elements", None) or [])
    reply = getattr(event, "reply", None)
    if reply is not None and all(reply is not element for element in elements):
        elements.insert(0, reply)
    if not elements:
        if getattr(event, "message_type", None) == 103:
            # 103 means "this is a quote" but nothing came with it — log it so a
            # future payload shape does not silently degrade to an empty turn.
            logger.warning(
                "[qq_official] message_type=103 without msg_elements: "
                "quoted content unavailable")
        return _Quoted()

    ref_idx = _scene_ext(event).get("ref_msg_idx", "")
    target = next(
        (element for element in elements
         if getattr(element, "msg_idx", None) == ref_idx), None)
    if target is None:
        target = elements[0]

    attachments: list = []
    seen: set[str] = set()
    for element in elements:
        for att in (getattr(element, "attachments", None) or []):
            url = getattr(att, "url", "") or ""
            if url and url not in seen:
                seen.add(url)
                attachments.append(att)

    author = getattr(target, "author", None)
    return _Quoted(
        text=_clean_content(getattr(target, "content", "") or ""),
        attachments=attachments,
        author_name=(getattr(author, "username", "") or "").strip(),
    )


def _quote_context(quoted: _Quoted) -> str:
    """Quoted message → prompt line (OneBot parity: [用户引用了消息: "…"]).

    Only the text belongs here: attachment paths are produced by the
    resolver so a failed download degrades exactly like a failed upload.
    """
    text = quoted.text
    if not text:
        return ""
    if len(text) > _QUOTE_TEXT_LIMIT:
        text = text[:_QUOTE_TEXT_LIMIT].rstrip() + "…"
    return f'[用户引用了消息: "{text}"]'


def build_view(bot: QQBot, event: QQMessageEvent) -> InboundMessage:
    """QQ official event -> platform-agnostic view (attachments stay lazy).

    Group content excludes the @bot mention itself (it lives in
    event.mentions / the GROUP_AT_MESSAGE_CREATE dispatch), so
    event.content is already the clean user text on both scopes
    (modulo inline face tags — see _clean_content).
    """
    is_group = isinstance(event, GroupMessageCreateEvent)
    author = event.author
    user_id = author.member_openid if is_group else author.user_openid
    quoted = _quoted_message(event)
    return InboundMessage(
        user_id=str(user_id),
        is_group=is_group,
        group_id=str(event.group_openid) if is_group else "",
        text=_clean_content(event.content),
        message_id=str(event.id),
        self_id=str(bot.self_id),
        mentioned=True if not is_group else bool(getattr(event, "to_me", True)),
        reply_context=_quote_context(quoted),
        resolver=QQOfficialAttachmentResolver(event, quoted),
        platform="qq_official",
        raw=event,
    )


class QQOfficialAttachmentResolver:
    """Downloads this turn's attachments *and* the quoted message's ones.

    Kind mapping mirrors the OneBot resolver: image/* → image,
    audio/*|voice → voice ("语音消息"), everything else (video/file) → file.
    Quoted attachments carry quoted=True so the kernel words them as
    "[用户引用了…]" rather than "[用户上传了…]" (they were sent earlier).
    Failures become Attachment.error entries so the kernel records them
    in _recent_files and the prompt exactly like failed NapCat downloads.
    """

    def __init__(self, event: QQMessageEvent, quoted: _Quoted | None = None):
        self._event = event
        self._quoted = quoted if quoted is not None else _quoted_message(event)

    async def resolve(self, view: InboundMessage) -> list[Attachment]:
        out: list[Attachment] = []
        seen: set[str] = set()
        # This turn's uploads first (they are "now"), then the quoted ones;
        # dedupe by URL because some payloads repeat the attachment in both.
        pending = [(att, False) for att in (self._event.attachments or [])]
        pending += [(att, True) for att in self._quoted.attachments]
        for att, quoted in pending:
            url = att.url or ""
            if not url or url in seen:
                continue
            seen.add(url)
            kind, name = _classify_attachment(att, url)
            saved_path, error = await _download_and_save_file(url, name, bot=None)
            if error:
                logger.warning(
                    f"[qq_official] attachment download failed "
                    f"({'quoted ' if quoted else ''}{kind} {name!r}): {error}")
            out.append(Attachment(kind=kind, path=saved_path or "",
                                  name=name, error=error or "", quoted=quoted))
        return out


def _classify_attachment(att, url: str) -> tuple[str, str]:
    """MessageAttachment → (kernel kind, filename used on disk / in prompts).

    The official API reports 语音消息 with the bare content_type "voice"
    (not audio/*), and group files as "file" — mapping both here keeps voice
    turns on the voice path instead of silently degrading to a file card.
    """
    ct = (att.content_type or "").strip().lower()
    filename = (att.filename or "").strip()
    if ct.startswith("image"):
        return "image", filename or f"image-{abs(hash(url)) % 10 ** 8}"
    if ct.startswith("audio") or ct in ("voice", "silk", "amr"):
        return "voice", "语音消息"
    if ct.startswith("video"):
        return "file", filename or f"video-{abs(hash(url)) % 10 ** 8}"
    return "file", filename or f"file-{abs(hash(url)) % 10 ** 8}"


class QQOfficialSender:
    """agent.outbound.Sender for the QQ official bot API.

    Sends are *buffered*, not immediate: the passive-reply quota is too
    small for the kernel's chatty output (300-char chunks at 1s, card
    images, per-code redemption messages). The entry plugin calls
    flush() once the turn completes; consecutive text fragments are
    merged into as few messages as possible, images keep their relative
    order, progress lines are swallowed outright (plan v2 ③) except the
    immediate "正在思考…" indicator which goes out standalone right away,
    and quota overflow is dropped with a warning.

    bot.send(event, ...) resolves the passive-reply semantics for us:
    msg_id=event.id + an auto-incrementing msg_seq per event, and
    msg_type 7 media upload for file_image segments.

    Markdown (msg_type=2) is sent natively — custom md is open to all
    bots since 2026/04 (docs §36), the adapter derives msg_type from the
    MessageSegment.markdown() type. Guard rails: env kill-switch
    QQ_OFFICIAL_MARKDOWN=off, a reject-code-triggered plain-text resend,
    and a module-level circuit breaker (3 consecutive rejects → 10 min
    plain-text degradation). supports_markdown reflects all of it, so
    the kernel's per-turn format decision degrades automatically.
    """

    platform = "qq_official"

    def __init__(self, bot: QQBot, event: QQMessageEvent, is_group: bool):
        self._bot = bot
        self._event = event
        self._is_group = is_group
        # group: 5 replies / 5min; c2c: 4 replies / 60min (per msg_id)
        self.max_messages_per_turn = 5 if is_group else 4
        self._buffer: list[tuple[str, object]] = []
        self._sent = 0

    @property
    def supports_markdown(self) -> bool:
        # Dynamic: env kill-switch (hot-read) + circuit breaker. The
        # kernel queries this per turn to pick the render format.
        return _md_available()

    @property
    def supports_keyboard(self) -> bool:
        """Dynamic: keyboard layer available + this user passes the
        QQ_OFFICIAL_BUTTONS_USERS graylist (empty list = everyone)."""
        if not _kb_available():
            return False
        allow = os.environ.get("QQ_OFFICIAL_BUTTONS_USERS", "").strip()
        if not allow:
            return True
        allowed = {x.strip() for x in allow.replace("，", ",").split(",")
                   if x.strip()}
        return _event_user_openid(self._event) in allowed

    @property
    def supports_stream(self) -> bool:
        """Dynamic: c2c only (stream_messages is a c2c endpoint) + the
        streaming capability layer (env hot-read + breaker + md gate)."""
        return (not self._is_group) and _stream_available()

    async def open_stream(self, first_text: str = ""):
        """Open the progress stream (stream #1) for this turn.

        Returns a live MessageStreamer, or None when the first shard was
        rejected — the caller then falls back to the buffered path with
        zero user-visible difference (the breaker was already fed inside
        the streamer, so repeated failures stop wasting requests).
        """
        if not self.supports_stream:
            return None
        openid = _event_user_openid(self._event)
        if not openid:
            return None
        streamer = MessageStreamer(self._bot, self._event, openid)
        if await streamer.open_progress(first_text or "⏳ 正在思考…"):
            return streamer
        return None

    def wants_reply(self, view: InboundMessage) -> bool:
        # c2c: always answer. Group: only when @-mentioned — the
        # c2c_group_at_messages intent only delivers @ messages anyway,
        # this is belt-and-braces for future intent changes.
        return (not view.is_group) or view.mentioned

    # ── Sender contract (buffering, except immediate progress) ─────

    async def send_text(self, text: str) -> None:
        if text and text.strip():
            self._buffer.append(("text", text))

    async def send_progress(self, text: str, *, immediate: bool = False) -> None:
        if not immediate:
            # Swallowed on purpose: mid-turn tool-call progress lines
            # would each eat one passive reply and cannot be reclaimed
            # by end-of-turn merge.
            return
        # The turn-start "正在思考…" indicator is the one progress line
        # worth spending quota on: it only helps the user if it arrives
        # BEFORE the (possibly long) agent turn, so it goes out now as
        # a standalone message instead of into the merge buffer.
        if not (text and text.strip()):
            return
        if self._sent >= self.max_messages_per_turn:
            logger.warning(
                "[qq_official] reply quota exhausted; dropping "
                f"immediate progress line {text[:30]!r}")
            return
        await self._send_once(MessageSegment.text(text))

    async def send_markdown(self, text: str) -> None:
        """Buffer a markdown payload (msg_type=2, custom md — open to all
        bots since 2026/04, no template needed). Degrades to the plain
        text buffer when markdown is unavailable (env kill-switch or
        breaker open), so content is never lost."""
        if not (text and text.strip()):
            return
        if _md_available():
            self._buffer.append(("markdown", text))
        else:
            await self.send_text(text)

    async def send_keyboard(self, msg: OutKeyboard) -> None:
        """Buffer a markdown-body + buttons payload (design §6.4).

        Pre-wire degradation: an invalid layout or an unavailable
        keyboard layer (env off / breaker open / markdown dead / user
        not in graylist) drops ONLY the buttons — the body still goes
        out through send_markdown (which itself degrades to text), so
        content is never lost. Platform-side rejection is handled by
        the ladder in _send_payload (kb → md-only → text + commands).
        """
        rows = msg.rows or []
        problems = validate_buttons(rows)
        if problems:
            logger.warning(
                f"[qq_official] keyboard invalid ({problems[:2]}); "
                f"sending markdown-only (label={msg.label!r})")
            await self.send_markdown(msg.markdown)
            return
        if not self.supports_keyboard:
            await self.send_markdown(msg.markdown)
            return
        self._buffer.append(("kb", msg))

    async def send_image(self, img: OutImage) -> None:
        self._buffer.append(("image", img))

    async def send_audio(self, audio: OutAudio) -> None:
        self._buffer.append(("audio", audio))

    # ── Flush: merged payloads under the quota cap ─────────────────

    async def flush(self) -> None:
        """Send the buffered turn output. Best-effort: a failing payload
        is logged and skipped, never raised — mirrors _safe_send's
        "log but don't crash" policy (kernel retry does not reach here
        because buffering makes every send_* succeed).

        Quota overflow drops by priority (§6.10.4/§12.7 #9): media
        (image/audio) first, then tail-most text (the disclaimer sits
        after the body, so it goes before body chunks) — 正文永不丢
        unless literally nothing else fits."""
        if not self._buffer:
            return
        payloads = self._merged_payloads()
        self._buffer.clear()
        remaining = self.max_messages_per_turn - self._sent
        if remaining <= 0:
            logger.warning(
                f"[qq_official] reply quota exhausted "
                f"({self.max_messages_per_turn}/turn); dropping "
                f"{len(payloads)} buffered payload(s)")
            return
        if len(payloads) > remaining:
            payloads = _drop_by_priority(payloads, len(payloads) - remaining)
        for message, _kind, kb_meta in payloads:
            await self._send_payload(message, kb_meta)

    async def _send_once(self, seg: MessageSegment) -> None:
        """Legacy single-segment send (immediate progress line)."""
        await self._send_payload(seg, None)

    async def _send_payload(self, message, kb_meta=None) -> None:
        """Send one merged payload; drive its degradation ladder.

        ``kb_meta`` = (md_text, OutKeyboard) when a keyboard rides this
        payload. Ladder (design §8.1): kb reject code → clip-and-retry
        once for the over-limit codes (40034029/304062) → md-only (L1)
        → md reject → plain text + to_text command list (L2). Payloads
        without a keyboard keep the pre-button behavior verbatim: audio
        file-card fallback, _markdown_text_fallback, one network retry,
        log-and-drop.
        """
        kb = kb_meta[1] if kb_meta else None
        md_text = kb_meta[0] if kb_meta else ""
        net_retried = False
        clipped = False
        kb_dropped = kb is None
        md_dropped = False
        audio_dropped = False
        for _ in range(6):  # bounded: net(1)+clip(1)+kb(1)+md(1)+audio(1)
            try:
                await self._bot.send(self._event, message)
                self._sent += 1
                if _message_has_markdown(message):
                    _md_record_success()
                if kb is not None and not kb_dropped:
                    _kb_record_success()
                return
            except (NetworkError, RateLimitException) as e:
                if not net_retried:
                    net_retried = True
                    await asyncio.sleep(1.0)
                    continue
                logger.warning(f"[qq_official] send failed after retry: {e!r}")
                return
            except ActionFailed as e:
                code = getattr(e, "code", None)
                # 1) keyboard rejected → clip retry / strip to md-only
                if (kb is not None and not kb_dropped
                        and code in _KB_REJECT_CODES):
                    if code in _KB_CLIP_CODES and not clipped:
                        clipped = True
                        rows = clip_rows(kb.rows)
                        logger.warning(
                            f"[qq_official] keyboard over-limit "
                            f"(code={code}); clipped to "
                            f"{sum(len(r) for r in rows)} button(s), retrying")
                        message = self._kb_message(md_text, rows)
                        continue
                    kb_dropped = True
                    _kb_record_reject(code)
                    logger.warning(
                        f"[qq_official] keyboard rejected (code={code}); "
                        f"retrying markdown-only")
                    message = MessageSegment.markdown(md_text or " ")
                    continue
                # 2) markdown rejected on a kb payload → L2 text WITH the
                #    command list (buttons are unreachable from here on)
                fallback = None
                if (code in _MD_REJECT_CODES and kb is not None
                        and not md_dropped):
                    md_dropped = True
                    _md_record_reject()
                    if not kb_dropped:
                        # md dead ⇒ keyboard dead: converge both breakers
                        _kb_record_reject(code)
                        kb_dropped = True
                    extra = to_text(kb.rows)
                    body = md_text or _md_seg_content(message)
                    fallback = MessageSegment.text(
                        f"{body}\n\n{extra}" if extra else body)
                elif isinstance(message, MessageSegment):
                    # 语音条被拒 → 换文件卡再试一次（格式口径冲突的降级梯）
                    fallback = _audio_as_file_fallback(message)
                    if fallback is not None:
                        audio_dropped = True
                    elif not audio_dropped:
                        # markdown 被拒（模板/语法/URL 类错误码）→ 同内容
                        # 纯文本再试一次 + 熔断计数（§6.10.10 / §8.1 L2）
                        fallback = _markdown_text_fallback(message, e)
                if fallback is not None:
                    logger.warning(
                        f"[qq_official] {getattr(message, 'type', 'message')} "
                        f"rejected ({e!r}); retrying as {fallback.type}")
                    message = fallback
                    continue
                # API rejection (audit, quota, bad payload) — not retryable
                logger.warning(f"[qq_official] send rejected: {e!r}")
                return
            except Exception as e:
                logger.warning(
                    f"[qq_official] send failed: {type(e).__name__}: {e}")
                return

    def _kb_message(self, md_text: str, rows) -> Message:
        """Build the wire md+keyboard message for a body/rows pair."""
        wire = to_message_keyboard(
            rows,
            scope="group" if self._is_group else "c2c",
            openid=_event_user_openid(self._event))
        return Message([MessageSegment.markdown(md_text or " "),
                        MessageSegment.keyboard(wire)])

    def _merged_payloads(self) -> list[tuple]:
        """Merge the buffer into (message, priority-kind, kb-meta) payloads.

        Consecutive text fragments join into one message (the kernel's
        1s-apart 300-char chunks collapse); images keep their position
        relative to the surrounding text. Markdown runs merge *only with
        markdown* (joined by a blank line, then block-split at
        _MD_CHUNK_LIMIT): mixing plain text into a msg_type=2 payload
        would render its markers verbatim, and splitting mid-line would
        break md syntax.

        A "kb" entry merges with the *immediately preceding* md run into
        one md+keyboard message (§6.4) — after a text/image payload the
        keyboard stands alone on its own markdown body (plain text is
        never force-converted to md: escaping artifacts). The keyboard
        rides the LAST chunk of a block-split body. kb-meta is
        (body_text, OutKeyboard) — exactly what _send_payload's
        degradation ladder needs to rebuild lower rungs. Kind is
        "text"/"markdown"/"media" for _drop_by_priority (a kb payload
        counts as "markdown" — body-class, dropped last)."""
        out: list[tuple] = []
        text_run: list[str] = []
        md_run: list[str] = []
        kb_count = 0

        def flush_text() -> None:
            if not text_run:
                return
            merged = "\n".join(text_run)
            text_run.clear()
            for i in range(0, len(merged), _TEXT_CHUNK_LIMIT):
                out.append((MessageSegment.text(merged[i:i + _TEXT_CHUNK_LIMIT]),
                            "text", None))

        def flush_md() -> None:
            if not md_run:
                return
            merged = "\n\n".join(md_run)
            md_run.clear()
            for chunk in _split_markdown(merged):
                out.append((MessageSegment.markdown(chunk), "markdown", None))

        for kind, item in self._buffer:
            if kind == "text":
                flush_md()
                text_run.append(item)  # type: ignore[arg-type]
            elif kind == "markdown":
                flush_text()
                md_run.append(item)    # type: ignore[arg-type]
            elif kind == "kb":
                flush_text()
                kb_count += 1
                if kb_count > 1:
                    logger.warning(
                        "[qq_official] multiple keyboards in one turn; "
                        "each becomes a standalone message")
                kb: OutKeyboard = item                    # type: ignore[assignment]
                body_parts = [p for p in md_run if p.strip()]
                md_run.clear()
                if (kb.markdown or "").strip():
                    body_parts.append(kb.markdown.strip())
                body = "\n\n".join(body_parts) or to_text(kb.rows) or " "
                chunks = _split_markdown(body)
                for chunk in chunks[:-1]:
                    out.append((MessageSegment.markdown(chunk),
                                "markdown", None))
                # kb-meta carries the LAST chunk (the one this payload
                # sends): the degradation ladder must rebuild exactly
                # this message, not re-send earlier standalone chunks.
                out.append((self._kb_message(chunks[-1], kb.rows),
                            "markdown", (chunks[-1], kb)))
            else:
                flush_text()
                flush_md()
                seg = (self._image_segment(item) if kind == "image"  # type: ignore[arg-type]
                       else self._audio_segment(item))               # type: ignore[arg-type]
                if seg is not None:
                    out.append((seg, "media", None))
        flush_text()
        flush_md()
        return out

    def _image_segment(self, img: OutImage) -> MessageSegment | None:
        if img.path and os.path.isfile(img.path):
            # file_image(): bytes upload — the official API cannot read
            # local paths, and image(url) only accepts public URLs.
            # 二进制直传上限 5MB：超限且有公网 URL 时改走平台侧下载
            # （URL 上传软限制 20MB）。
            if os.path.getsize(img.path) > _BINARY_UPLOAD_LIMIT and img.url:
                return MessageSegment.image(img.url)
            return MessageSegment.file_image(Path(img.path))
        if img.url:
            return MessageSegment.image(img.url)
        logger.warning(
            f"[qq_official] image dropped (label={img.label!r}): "
            f"{'local path missing: ' + img.path if img.path else 'no path or url'}")
        return None

    def _audio_segment(self, audio: OutAudio) -> MessageSegment | None:
        if audio.path and os.path.isfile(audio.path):
            # file_audio(): bytes upload as msg_type=3 rich media — the
            # official API cannot read local paths (same as file_image).
            return MessageSegment.file_audio(Path(audio.path))
        if audio.url:
            return MessageSegment.audio(audio.url)
        logger.warning(
            f"[qq_official] audio dropped (label={audio.label!r}): "
            f"{'local path missing: ' + audio.path if audio.path else 'no path or url'}")
        return None


# ── Active (non-passive) c2c messaging ────────────────────────────
# bot.send_to_c2c WITHOUT msg_id/msg_seq is an *active* message: it does
# not consume the passive-reply quota but is limited by the platform's
# small per-user daily/monthly active quota + content audit. Callers
# (agent/greeting.py bookkeeping in the entry plugin) own the quota
# gating; this helper only does the send with the route's error policy:
#   ActionFailed (audit/quota/bad payload) → log, False, no retry;
#   NetworkError/RateLimitException        → one retry, then False.


async def send_active_c2c(bot: QQBot, openid: str, text: str) -> bool:
    """Send one active c2c message. Returns True only on confirmed send."""
    openid = str(openid or "").strip()
    if not openid or not (text and text.strip()):
        return False
    for attempt in range(2):
        try:
            await bot.send_to_c2c(openid=openid,
                                  message=MessageSegment.text(text))
            return True
        except (NetworkError, RateLimitException) as e:
            if attempt == 0:
                await asyncio.sleep(1.0)
                continue
            logger.warning(
                f"[qq_official] active c2c to {openid[:8]}… failed "
                f"after retry: {e!r}")
            return False
        except ActionFailed as e:
            # platform rejection (active quota exhausted / audit) — the
            # quota file may lag reality; log for observability, no retry
            logger.warning(
                f"[qq_official] active c2c to {openid[:8]}… rejected: {e!r}")
            return False
        except Exception as e:
            logger.warning(
                f"[qq_official] active c2c to {openid[:8]}… failed: "
                f"{type(e).__name__}: {e}")
            return False
    return False


async def send_active_c2c_media(bot: QQBot, openid: str,
                                seg: MessageSegment) -> bool:
    """Send one ACTIVE c2c rich-media message (P4 proactive image/voice).

    Same active-message semantics as send_active_c2c (no msg_id) but with
    a media segment: the adapter uploads the bytes (msg_type=7 payload /
    file_type 1|3) and posts in one call. Platform audit + the small
    active quota apply — error policy mirrors send_active_c2c exactly:
    ActionFailed → False, no retry; Network/RateLimit → one retry.
    """
    openid = str(openid or "").strip()
    if not openid or seg is None:
        return False
    for attempt in range(2):
        try:
            await bot.send_to_c2c(openid=openid, message=seg)
            return True
        except (NetworkError, RateLimitException) as e:
            if attempt == 0:
                await asyncio.sleep(1.0)
                continue
            logger.warning(
                f"[qq_official] active c2c media to {openid[:8]}… failed "
                f"after retry: {e!r}")
            return False
        except ActionFailed as e:
            # 语音条被拒 → 文件卡降级再试一次（同 _send_once 策略）
            fallback = _audio_as_file_fallback(seg)
            if fallback is not None and attempt == 0:
                logger.warning(
                    f"[qq_official] active c2c audio to {openid[:8]}… "
                    f"rejected ({e!r}); retrying as file card")
                seg = fallback
                continue
            logger.warning(
                f"[qq_official] active c2c media to {openid[:8]}… "
                f"rejected: {e!r}")
            return False
        except Exception as e:
            logger.warning(
                f"[qq_official] active c2c media to {openid[:8]}… failed: "
                f"{type(e).__name__}: {e}")
            return False
    return False


async def send_active_c2c_markdown(bot: QQBot, openid: str, md_text: str,
                                   rows=None,
                                   plain_text: str | None = None) -> bool:
    """Send one ACTIVE c2c markdown envelope (mail digest, design §6.4).

    Degradation ladder, sharing the route's md/keyboard breakers (v0.5 —
    never a private copy):
      1. markdown + keyboard (only when ``_kb_available()`` and rows given);
         kb over-limit codes clip-and-retry once, other kb rejects strip to
      2. markdown-only (only when ``_md_available()``);
         md rejects record on BOTH breakers (md dead ⇒ kb dead) and fall to
      3. plain text (``plain_text`` when given, else the raw md source).
    When both breakers are open / ``QQ_OFFICIAL_MARKDOWN=off`` the send
    starts directly at tier 3 — no per-user wall-banging during cooldown.

    Error policy mirrors send_active_c2c: ActionFailed → ladder step or
    False, no network retry of a rejection; NetworkError/RateLimitException
    → one retry per tier. Returns True only on confirmed send.
    """
    openid = str(openid or "").strip()
    if not openid or not (md_text and md_text.strip()):
        return False
    plain = plain_text if (plain_text and plain_text.strip()) else md_text

    kb_on = bool(rows) and _kb_available()
    md_on = _md_available()
    if not kb_on and not md_on:
        return await send_active_c2c(bot, openid, plain)

    wire_rows = rows
    clipped = False
    tier = "kb" if kb_on else ("md" if md_on else "plain")
    for _ in range(6):  # bounded: net(1)+clip(1)+kb(1)+md(1) per tier
        try:
            if tier == "kb":
                wire = to_message_keyboard(wire_rows, scope="c2c",
                                           openid=openid)
                message = Message([MessageSegment.markdown(md_text or " "),
                                   MessageSegment.keyboard(wire)])
            elif tier == "md":
                message = MessageSegment.markdown(md_text or " ")
            else:
                message = MessageSegment.text(plain)
            for attempt in range(2):
                try:
                    await bot.send_to_c2c(openid=openid, message=message)
                    _md_record_success()
                    if tier == "kb":
                        _kb_record_success()
                    return True
                except (NetworkError, RateLimitException):
                    if attempt == 0:
                        await asyncio.sleep(1.0)
                        continue
                    raise
        except (NetworkError, RateLimitException) as e:
            logger.warning(
                f"[qq_official] active c2c envelope ({tier}) to "
                f"{openid[:8]}… failed after retry: {e!r}")
            return False
        except ActionFailed as e:
            code = getattr(e, "code", None)
            if tier == "kb":
                if code in _KB_CLIP_CODES and not clipped:
                    clipped = True
                    wire_rows = clip_rows(wire_rows)
                    logger.warning(
                        f"[qq_official] envelope keyboard over-limit "
                        f"(code={code}); clipped, retrying")
                    continue
                if code in _KB_REJECT_CODES:
                    _kb_record_reject(code)
                    logger.warning(
                        f"[qq_official] envelope keyboard rejected "
                        f"(code={code}); retrying markdown-only")
                    tier = "md" if md_on else "plain"
                    continue
                if code in _MD_REJECT_CODES:
                    _md_record_reject()
                    _kb_record_reject(code)  # md dead ⇒ kb dead: converge
                    logger.warning(
                        f"[qq_official] envelope markdown rejected "
                        f"({e!r}); retrying plain text")
                    tier = "plain"
                    continue
            elif tier == "md":
                if code in _MD_REJECT_CODES:
                    _md_record_reject()
                    logger.warning(
                        f"[qq_official] envelope markdown rejected "
                        f"({e!r}); retrying plain text")
                    tier = "plain"
                    continue
            # unrecoverable rejection (quota/audit/bad payload) — no retry
            logger.warning(
                f"[qq_official] active c2c envelope to {openid[:8]}… "
                f"rejected: {e!r}")
            return False
        except Exception as e:
            logger.warning(
                f"[qq_official] active c2c envelope to {openid[:8]}… "
                f"failed: {type(e).__name__}: {e}")
            return False
    return False


# ── Active group messaging (announcement broadcast, mail-design §16.5) ──
# bot.send_to_group WITHOUT msg_id is an *active* group message. Error
# taxonomy for the caller (_do_broadcast), returned as a string:
#   "ok"     — confirmed send;
#   "left"   — ActionFailed 40034101 (机器人非群成员) / 40034105 (无权限):
#              GROUP_DEL_ROBOT may be lost, so treat the group as gone and
#              let the caller mark it left in group_registry (§4.5);
#   "failed" — anything else (network after retry, audit, quota, payload…).

_GROUP_LEFT_CODES = (40034101, 40034105)


async def send_active_group(bot: QQBot, group_openid: str, text: str,
                            md_text: str | None = None) -> str:
    """Send one ACTIVE group message → "ok" | "left" | "failed".

    ``md_text`` (announcement format=markdown) rides msg_type=2 when the
    route's shared markdown breaker allows; md rejects record on the
    breaker and degrade to ``text`` once — the c2c envelope ladder minus
    keyboard (v1 announcements carry no buttons). Error policy mirrors
    send_active_c2c: ActionFailed → no retry; NetworkError /
    RateLimitException → one retry per tier.
    """
    group_openid = str(group_openid or "").strip()
    if not group_openid:
        return "failed"
    plain = text if (text and text.strip()) else (md_text or "")
    if not plain.strip():
        return "failed"
    tier = ("md" if (md_text and md_text.strip() and _md_available())
            else "plain")
    for _ in range(3):  # bounded: net-retry inline; md→plain degrade once
        message = (MessageSegment.markdown(md_text or " ") if tier == "md"
                   else MessageSegment.text(plain))
        try:
            for attempt in range(2):
                try:
                    await bot.send_to_group(group_openid=group_openid,
                                            message=message)
                    if tier == "md":
                        _md_record_success()
                    return "ok"
                except (NetworkError, RateLimitException):
                    if attempt == 0:
                        await asyncio.sleep(1.0)
                        continue
                    raise
        except (NetworkError, RateLimitException) as e:
            logger.warning(
                f"[qq_official] active group ({tier}) to "
                f"{group_openid[:8]}… failed after retry: {e!r}")
            return "failed"
        except ActionFailed as e:
            code = getattr(e, "code", None)
            if tier == "md" and code in _MD_REJECT_CODES:
                _md_record_reject()
                logger.warning(
                    f"[qq_official] group announcement markdown rejected "
                    f"({e!r}); retrying plain text")
                tier = "plain"
                continue
            if code in _GROUP_LEFT_CODES:
                logger.warning(
                    f"[qq_official] active group to {group_openid[:8]}… "
                    f"rejected (code={code}); treating group as left")
                return "left"
            logger.warning(
                f"[qq_official] active group to {group_openid[:8]}… "
                f"rejected: {e!r}")
            return "failed"
        except Exception as e:
            logger.warning(
                f"[qq_official] active group to {group_openid[:8]}… "
                f"failed: {type(e).__name__}: {e}")
            return "failed"
    return "failed"


# Event types this route handles — the entry plugin's isinstance guard
# and the intent config (c2c_group_at_messages) must agree on this set.
HANDLED_EVENTS = (C2CMessageCreateEvent, GroupAtMessageCreateEvent)

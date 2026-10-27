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

from agent.inbound import Attachment, InboundMessage
from agent.outbound import OutAudio, OutImage

# Reuse the neutral URL→disk download helper (stage 2 parked it in the
# OneBot adapter; its bot=None path is pure httpx and platform-neutral).
# Importing beats duplicating ~100 lines of naming/size/magic-byte logic
# that must not drift between the two routes.
from adapters.onebot import _download_and_save_file

# Conservative per-message text cap for merged payloads. The kernel's
# _send_response already chunks at 300 chars; this only guards the
# merged result against the official content length limit.
_TEXT_CHUNK_LIMIT = 1500

# 官方富媒体「二进制直传」上限 5MB（超过须走分片上传；URL 上传软限制
# 20MB）。见 docs/qqbot-api-v2-full-docs.md 单聊富媒体上传节。
_BINARY_UPLOAD_LIMIT = 5 * 1024 * 1024


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
    """

    platform = "qq_official"

    def __init__(self, bot: QQBot, event: QQMessageEvent, is_group: bool):
        self._bot = bot
        self._event = event
        # group: 5 replies / 5min; c2c: 4 replies / 60min (per msg_id)
        self.max_messages_per_turn = 5 if is_group else 4
        self._buffer: list[tuple[str, object]] = []
        self._sent = 0

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
        # msg_type=2 markdown requires a platform-approved template;
        # unapproved md is rejected, so degrade to plain text like OneBot.
        await self.send_text(text)

    async def send_image(self, img: OutImage) -> None:
        self._buffer.append(("image", img))

    async def send_audio(self, audio: OutAudio) -> None:
        self._buffer.append(("audio", audio))

    # ── Flush: merged payloads under the quota cap ─────────────────

    async def flush(self) -> None:
        """Send the buffered turn output. Best-effort: a failing payload
        is logged and skipped, never raised — mirrors _safe_send's
        "log but don't crash" policy (kernel retry does not reach here
        because buffering makes every send_* succeed)."""
        if not self._buffer:
            return
        payloads = self._merged_payloads()
        self._buffer.clear()
        for i, seg in enumerate(payloads):
            if self._sent >= self.max_messages_per_turn:
                logger.warning(
                    f"[qq_official] reply quota exhausted "
                    f"({self.max_messages_per_turn}/turn); dropping "
                    f"{len(payloads) - i} remaining payload(s)")
                break
            await self._send_once(seg)

    async def _send_once(self, seg: MessageSegment) -> None:
        for attempt in range(2):
            try:
                await self._bot.send(self._event, seg)
                self._sent += 1
                return
            except (NetworkError, RateLimitException) as e:
                if attempt == 0:
                    await asyncio.sleep(1.0)
                    continue
                logger.warning(f"[qq_official] send failed after retry: {e!r}")
            except ActionFailed as e:
                # 语音条被拒 → 换文件卡再试一次（格式口径冲突的降级梯）
                fallback = _audio_as_file_fallback(seg)
                if fallback is not None and attempt == 0:
                    logger.warning(
                        f"[qq_official] audio rejected ({e!r}); "
                        f"retrying as file card")
                    seg = fallback
                    continue
                # API rejection (audit, quota, bad payload) — not retryable
                logger.warning(f"[qq_official] send rejected: {e!r}")
                return
            except Exception as e:
                logger.warning(
                    f"[qq_official] send failed: {type(e).__name__}: {e}")
                return

    def _merged_payloads(self) -> list[MessageSegment]:
        """Merge the buffer: consecutive text fragments join into one
        message (the kernel's 1s-apart 300-char chunks collapse), images
        keep their position relative to the surrounding text."""
        out: list[MessageSegment] = []
        text_run: list[str] = []

        def flush_text() -> None:
            if not text_run:
                return
            merged = "\n".join(text_run)
            text_run.clear()
            for i in range(0, len(merged), _TEXT_CHUNK_LIMIT):
                out.append(MessageSegment.text(merged[i:i + _TEXT_CHUNK_LIMIT]))

        for kind, item in self._buffer:
            if kind == "text":
                text_run.append(item)  # type: ignore[arg-type]
            else:
                flush_text()
                seg = (self._image_segment(item) if kind == "image"  # type: ignore[arg-type]
                       else self._audio_segment(item))               # type: ignore[arg-type]
                if seg is not None:
                    out.append(seg)
        flush_text()
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


# Event types this route handles — the entry plugin's isinstance guard
# and the intent config (c2c_group_at_messages) must agree on this set.
HANDLED_EVENTS = (C2CMessageCreateEvent, GroupAtMessageCreateEvent)

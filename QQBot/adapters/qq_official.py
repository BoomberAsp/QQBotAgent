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
  - inbound attachments arrive as public https URLs (event.attachments)
    and are downloaded with the same neutral URL→disk helper the OneBot
    resolver uses (uuid-prefixed naming, size cap, magic-byte extension
    detection, per-user workspace uploads dir), so the kernel sees
    identical Attachment semantics on both routes.
"""
from __future__ import annotations

import asyncio
import os
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
from agent.outbound import OutImage

# Reuse the neutral URL→disk download helper (stage 2 parked it in the
# OneBot adapter; its bot=None path is pure httpx and platform-neutral).
# Importing beats duplicating ~100 lines of naming/size/magic-byte logic
# that must not drift between the two routes.
from adapters.onebot import _download_and_save_file

# Conservative per-message text cap for merged payloads. The kernel's
# _send_response already chunks at 300 chars; this only guards the
# merged result against the official content length limit.
_TEXT_CHUNK_LIMIT = 1500


def build_view(bot: QQBot, event: QQMessageEvent) -> InboundMessage:
    """QQ official event -> platform-agnostic view (attachments stay lazy).

    Group content excludes the @bot mention itself (it lives in
    event.mentions / the GROUP_AT_MESSAGE_CREATE dispatch), so
    event.content is already the clean user text on both scopes.
    """
    is_group = isinstance(event, GroupMessageCreateEvent)
    author = event.author
    user_id = author.member_openid if is_group else author.user_openid
    return InboundMessage(
        user_id=str(user_id),
        is_group=is_group,
        group_id=str(event.group_openid) if is_group else "",
        text=(event.content or "").strip(),
        message_id=str(event.id),
        self_id=str(bot.self_id),
        mentioned=True if not is_group else bool(getattr(event, "to_me", True)),
        reply_context="",  # official API has no inbound quote passthrough
        resolver=QQOfficialAttachmentResolver(event),
        platform="qq_official",
        raw=event,
    )


class QQOfficialAttachmentResolver:
    """Downloads event.attachments (public https URLs) to the uploads dir.

    Kind mapping mirrors the OneBot resolver: image/* → image,
    audio/* → voice ("语音消息"), everything else (video/file) → file.
    Failures become Attachment.error entries so the kernel records them
    in _recent_files and the prompt exactly like failed NapCat downloads.
    """

    def __init__(self, event: QQMessageEvent):
        self._event = event

    async def resolve(self, view: InboundMessage) -> list[Attachment]:
        out: list[Attachment] = []
        for att in (self._event.attachments or []):
            url = att.url or ""
            if not url:
                continue
            ct = (att.content_type or "").split("/", 1)[0]
            if ct == "image":
                kind, name = "image", att.filename or f"image-{abs(hash(url)) % 10**8}"
            elif ct == "audio":
                kind, name = "voice", "语音消息"
            else:
                kind, name = "file", att.filename or f"file-{abs(hash(url)) % 10**8}"
            saved_path, error = await _download_and_save_file(url, name, bot=None)
            out.append(Attachment(kind=kind, path=saved_path or "",
                                  name=name, error=error or ""))
        return out


class QQOfficialSender:
    """agent.outbound.Sender for the QQ official bot API.

    Sends are *buffered*, not immediate: the passive-reply quota is too
    small for the kernel's chatty output (300-char chunks at 1s, card
    images, per-code redemption messages). The entry plugin calls
    flush() once the turn completes; consecutive text fragments are
    merged into as few messages as possible, images keep their relative
    order, progress lines are swallowed outright (plan v2 ③) and quota
    overflow is dropped with a warning.

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

    # ── Sender contract (all buffering; nothing hits the API here) ──

    async def send_text(self, text: str) -> None:
        if text and text.strip():
            self._buffer.append(("text", text))

    async def send_progress(self, text: str) -> None:
        # Swallowed on purpose: each mid-turn "thinking…" line would eat
        # one passive reply and cannot be reclaimed by end-of-turn merge.
        pass

    async def send_markdown(self, text: str) -> None:
        # msg_type=2 markdown requires a platform-approved template;
        # unapproved md is rejected, so degrade to plain text like OneBot.
        await self.send_text(text)

    async def send_image(self, img: OutImage) -> None:
        self._buffer.append(("image", img))

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
                seg = self._image_segment(item)  # type: ignore[arg-type]
                if seg is not None:
                    out.append(seg)
        flush_text()
        return out

    def _image_segment(self, img: OutImage) -> MessageSegment | None:
        if img.path and os.path.isfile(img.path):
            # file_image(): bytes upload — the official API cannot read
            # local paths, and image(url) only accepts public URLs.
            return MessageSegment.file_image(Path(img.path))
        if img.url:
            return MessageSegment.image(img.url)
        logger.warning(
            f"[qq_official] image dropped (label={img.label!r}): "
            f"{'local path missing: ' + img.path if img.path else 'no path or url'}")
        return None


# Event types this route handles — the entry plugin's isinstance guard
# and the intent config (c2c_group_at_messages) must agree on this set.
HANDLED_EVENTS = (C2CMessageCreateEvent, GroupAtMessageCreateEvent)

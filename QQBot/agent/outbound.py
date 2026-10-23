"""
Platform-agnostic outbound contract — stage 3 of the QQ-official plan.

The turn kernel (agent/turn.py) and the tool layer never construct
platform message objects anymore: they hold a ``Sender`` (via argument
or the ``agent.context._current_sender`` contextvar) and express sends
semantically — text, progress line, local image, markdown. Each entry
adapter provides
the implementation (adapters/onebot.py::OneBotSender now; stage 4 adds
QQOfficialSender, which turns OutImage.path into base64 + file_image()
because the official API cannot read local files).

Retry policy stays in the kernel (_safe_send): SendFailed is the only
retryable failure, mirroring the pre-stage-3 behavior where OneBot
ActionFailed was retried with backoff and everything else was logged
and dropped.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass
class OutImage:
    """An image to send, described platform-agnostically.

    OneBot: ``path`` → MessageSegment.image(Path(path)); ``url`` is used
    as-is when there is no local path. Official (stage 4): ``path`` →
    base64 upload via file_image(); ``url`` can be passed through.
    """
    path: str | None = None      # local absolute path
    url: str | None = None       # public URL (or file:// URI)
    label: str = ""              # for logs / fallback text


@dataclass
class OutAudio:
    """An audio/voice message to send, described platform-agnostically (P4).

    OneBot: ``path`` → MessageSegment.record(Path(path)); ``url`` used
    as-is when there is no local path. Official: ``path`` → bytes upload
    via MessageSegment.file_audio() (msg_type=3 rich media); ``url`` →
    MessageSegment.audio(url). Generated TTS always carries a local path
    (media_gen downloads the 24h-expiring provider URL immediately).
    """
    path: str | None = None      # local absolute path (mp3/wav/silk…)
    url: str | None = None       # public URL
    label: str = ""              # for logs / fallback text


@dataclass
class OutProgress:
    """An intermediate progress line ("正在思考…" and friends).

    OneBot: sent verbatim, like any text (pre-stage-4 behavior).
    QQ official: swallowed by QQOfficialSender — every passive reply
    eats the limited quota (group 5/5min, c2c 4/60min) and progress
    lines land mid-turn, where end-of-turn merging cannot reclaim them
    (plan v2 supplement ③).

    ``immediate=True`` marks the one line worth spending quota on even
    on the official route (the turn-start "正在思考…" indicator):
    QQOfficialSender sends it right away as a standalone message
    instead of buffering it into the end-of-turn merge, so the user
    gets prompt feedback rather than seeing it glued onto the final
    reply. Tool-call progress lines keep the default (swallowed on
    official, verbatim on OneBot). OneBot ignores the flag — every
    send there is already immediate.
    """
    text: str
    immediate: bool = False


class SendFailed(Exception):
    """Platform-agnostic retryable send failure (replaces ActionFailed).

    ``info`` carries the platform-specific failure payload for logging
    (OneBot: the ActionFailed.info dict), so kernel log messages keep
    their pre-stage-3 shape.
    """

    def __init__(self, message: str = "", info: Any = None):
        super().__init__(message)
        self.info = info if info is not None else message


class Sender(Protocol):
    """Platform-agnostic sender.

    Implementations translate semantics into platform API calls and
    must raise SendFailed for retryable failures.
    """

    platform: str
    # Optional: the official route's passive-reply quota is limited, so
    # an implementation may declare how many messages one turn may emit.
    max_messages_per_turn: int

    async def send_text(self, text: str) -> None: ...
    async def send_image(self, img: OutImage) -> None: ...
    async def send_audio(self, audio: OutAudio) -> None: ...
    async def send_markdown(self, text: str) -> None: ...
    async def send_progress(self, text: str, *, immediate: bool = False) -> None: ...

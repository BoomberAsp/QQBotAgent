"""
Platform-agnostic inbound view — stage 2 of the QQ-official worktree plan.

Entry adapters (adapters/onebot.py now, adapters/qq_official.py in stage 4)
convert platform events into ``InboundMessage`` before calling into
agent/turn.py. The turn kernel only reads view fields; platform-specific
work happens on the adapter side:

  - ``reply_context``  — pre-computed quote/reply prompt (OneBot: reply
    segment / [CQ:reply] parsing; official: no equivalent, stays "")
  - ``resolver``       — lazy attachment fetching, triggered by
    ``await view.load_attachments()`` at the exact point where the old
    code walked ``event.message`` segments

Deviation from the plan doc (§5.1), deliberate for G3 behavior parity:
``Attachment`` carries an ``error`` field because the turn kernel records
failed downloads too (``_recent_files`` + context parts, so quoting a
message whose attachment failed still tells the LLM about it); and
``load_attachments()`` caches via an explicit flag instead of list
truthiness (an all-failed... actually an *empty* resolve result would
otherwise re-run on every call).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class Attachment:
    """An inbound attachment fetched to local disk (or the failure to)."""
    kind: str                     # "image" | "file" | "voice"
    path: str = ""                # local absolute path ("" when failed)
    name: str = ""                # semantic name used in prompts/_recent_files
    content_type: str = ""
    error: str = ""               # download failure reason ("" on success)
    quoted: bool = False          # came from the *quoted* message rather than
                                  # this turn's upload (official: msg_elements)

    def __post_init__(self) -> None:
        if not self.name and self.path:
            self.name = self.path.rsplit("/", 1)[-1]


class AttachmentResolver(Protocol):
    """Platform-specific attachment fetching.

    OneBot: ``bot.call_api("get_file"/"get_record", ...)`` (NapCat) with
    HTTP/base64 fallbacks; QQ official: download ``attachments[].url``.
    """

    async def resolve(self, view: "InboundMessage") -> list[Attachment]: ...


@dataclass
class InboundMessage:
    """Platform-agnostic view of one inbound message."""
    user_id: str
    is_group: bool = False
    group_id: str = ""
    text: str = ""                    # stripped plaintext
    message_id: str = ""
    self_id: str = ""
    mentioned: bool = False           # @-bot / always True in private (entry layer)
    reply_context: str = ""           # quote-context prompt (entry layer)
    attachments: list[Attachment] = field(default_factory=list)
    attachments_loaded: bool = field(default=False, repr=False)
    resolver: AttachmentResolver | None = None
    platform: str = "unknown"         # "onebot" | "qq_official"
    raw: Any = None                   # original event (escape hatch)

    async def load_attachments(self) -> list[Attachment]:
        if self.attachments_loaded or self.resolver is None:
            return self.attachments
        self.attachments = await self.resolver.resolve(self)
        self.attachments_loaded = True
        return self.attachments

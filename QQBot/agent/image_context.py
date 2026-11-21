"""Short-lived image references scoped to a conversation and its speaker."""
import asyncio
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from pathlib import Path
import re
import time


_TTL = 300
_MAX_SCOPES = 512
_recent = OrderedDict()
_private_batches = {}
_CAPTION_WINDOW = 0.7
_MAX_BATCH_MESSAGES = 4
_ORDINAL = re.compile(r"第\s*([1-9１-９][0-9０-９]?|[一二两三四五六七八九十])\s*张")
_REF = re.compile(
    r"(?:刚才|上面|之前|上一).{0,8}(?:图|照片|截图)"
    r"|(?:这|那)(?:一|几)?(?:张|幅)(?:图|照片|截图)?"
    r"|(?:这|那)(?:图|照片|截图)"
    r"|(?:图里|图上|截图里|图中)"
    r"|^(?:那|这)?(?:左边|右边|上边|下边|中间|左上|右下).{0,4}(?:呢|那个|这个|[？?])"
)
_CN_NUMBERS = dict(zip("一二三四五六七八九十", range(1, 11)), 两=2)


def reference(text):
    text = (text or "").strip()
    ordinal = _ORDINAL.search(text)
    if ordinal:
        number = ordinal[1]
        return True, _CN_NUMBERS[number] if number in _CN_NUMBERS else int(number)
    return bool(_REF.search(text)), None


def images_only(attachments):
    return [attachment for attachment in attachments if attachment.kind == "image"]


def local_images(attachments, workspace):
    if not workspace:
        return []
    root = Path(workspace).resolve()
    paths = []
    for attachment in images_only(attachments):
        if not attachment.path:
            continue
        try:
            path = Path(attachment.path).resolve(strict=True)
        except (OSError, ValueError, RuntimeError):
            continue
        if path.is_relative_to(root) and path.is_file():
            paths.append(attachment)
    return paths


def remember(key, message_id, attachments, workspace):
    images = images_only(attachments)
    if not local_images(images, workspace):
        return
    previous = [entry for entry in _recent.get(key, ()) if entry[1] != message_id]
    _recent[key] = [(time.monotonic() + _TTL, message_id, tuple(images)), *previous[:3]]
    _recent.move_to_end(key)
    if len(_recent) > _MAX_SCOPES:
        _recent.popitem(last=False)


def recall(key, reply_id=""):
    now = time.monotonic()
    for expires, message_id, attachments in _recent.get(key, ()):
        if expires > now and (not reply_id or reply_id == message_id):
            return [replace(attachment, quoted=True) for attachment in attachments]
    return []


def select(attachments, ordinal):
    if ordinal is None:
        return attachments
    images = images_only(attachments)
    return images[ordinal - 1:ordinal]


@dataclass
class _PrivateImageBatch:
    views: list
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    caption: str = ""

    async def resolve(self, _view):
        attachments = []
        for source in self.views:
            attachments.extend(await source.load_attachments())
        return attachments


async def coalesce_private_images(view, scope):
    """Use one private reply when a caption immediately follows an upload."""
    key = (view.platform, view.self_id, view.user_id, scope)
    text = view.text.strip()
    kinds = view.attachment_kinds or tuple(attachment.kind for attachment in view.attachments)
    quote = view.reply_message_id or view.reply_context
    bare = not text and bool(kinds) and all(kind == "image" for kind in kinds) and not quote
    current = _private_batches.get(key)
    if current is not None and not current.ready.is_set():
        if bare and len(current.views) < _MAX_BATCH_MESSAGES:
            current.views.append(view)
            return None
        if text and not kinds and not quote and not text.startswith(("/", "#", "／")):
            current.caption = text
            current.ready.set()
            return None
    if not bare or current is not None:
        return view
    batch = _PrivateImageBatch([view])
    _private_batches[key] = batch
    try:
        await asyncio.wait_for(batch.ready.wait(), _CAPTION_WINDOW)
    except asyncio.TimeoutError:
        pass
    finally:
        _private_batches.pop(key, None)
    if len(batch.views) == 1 and not batch.caption:
        return view
    return replace(view, text=batch.caption, attachments=[], attachments_loaded=False,
                   resolver=batch, attachment_kinds=("image",), image_batch_started_bare=True)

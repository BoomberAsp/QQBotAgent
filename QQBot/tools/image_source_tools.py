"""Consent and attachment binding for the agent's reverse-image search tool."""

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass
import os
from pathlib import Path
import re
import time


TOOL_NAME = "find_image_source"
SEARCH_HINT = "用户已要求查找所指图片的来源。请调用 find_image_source 获取结果，再据实回复。"
_QUESTION = "要帮你查这张图的来源吗？同意后会把图片发给第三方反查引擎。你可以回复「可以」或「查来源」。"
_MULTIPLE = "收到了多张图。想查哪一张的来源？可以单独发那张图给我。"
_CONFIRMATIONS = {
    "好", "好的", "好啊", "可以", "行", "嗯", "要", "是", "同意",
    "查", "查吧", "查一下", "帮我查", "帮我查一下",
}
_SOURCE_REQUEST = re.compile(
    r"(?:(?:好|好的|可以)[，,])?"
    r"(?:(?:请|麻烦|可以|能)?(?:帮我|帮忙)?(?:反查|查找|查|找)(?:一下)?"
    r"(?:(?:这|那)张?(?:图片|图|照片)的?)?(?:来源|出处|作者)(?:吗|吧)?"
    r"|(?:这|那)张?(?:图片|图|照片)(?:出自|来自)(?:哪里|哪儿|哪部作品)"
    r"|找图|找源|找作者)"
)
_OFFER_SECONDS = 300
_MAX_PENDING = 1024
_pending: dict[tuple, tuple[str, float]] = {}


@dataclass
class ImageSearchTurn:
    user_id: str
    workspace: str
    path: str = ""
    question: str = ""
    authorized: bool = False


current = ContextVar("image_source_turn", default=None)


def enabled() -> bool:
    return os.environ.get("FIND_SOURCE_ENABLED", "on").strip().lower() not in (
        "off", "0", "false", "no")


def _key(view, scope) -> tuple:
    return view.platform, view.self_id, view.group_id, view.user_id, scope


def take_offer(view, scope) -> str:
    """Consume an invitation on the submitting member's next admitted turn."""
    offer = _pending.pop(_key(view, scope), None)
    if offer and enabled() and offer[1] > time.monotonic():
        return offer[0]
    return ""


def remember_offer(view, scope, turn: ImageSearchTurn) -> None:
    if not turn.question or not turn.path:
        return
    if len(_pending) >= _MAX_PENDING:
        _pending.pop(next(iter(_pending)))
    _pending[_key(view, scope)] = (turn.path, time.monotonic() + _OFFER_SECONDS)


def _in_workspace(filename: str, workspace: str) -> Path | None:
    if not filename or not workspace:
        return None
    try:
        path = Path(filename).resolve(strict=True)
        if path.is_relative_to(Path(workspace).resolve()) and path.is_file():
            return path
    except (OSError, ValueError, RuntimeError):
        pass
    return None


def prepare(view, workspace: str, attachments, previous: str, *, text: str) -> ImageSearchTurn:
    turn = ImageSearchTurn(view.user_id, workspace)
    if not enabled():
        return turn
    text = re.sub(r"\s+", "", text).rstrip("。！!？?，,")
    explicit = bool(_SOURCE_REQUEST.fullmatch(text))
    uploads = [att for att in attachments if att.kind == "image" and not att.quoted]
    images = uploads or [att for att in attachments if att.kind == "image"]
    bare_upload = bool(uploads) and not text and all(att.kind == "image" for att in attachments)
    if len(images) > 1:
        if explicit or bare_upload:
            turn.question = _MULTIPLE
        return turn
    filename = images[0].path if images else previous
    if explicit or bare_upload or (not attachments and text in _CONFIRMATIONS):
        path = _in_workspace(filename, workspace)
        if path is not None:
            turn.path = str(path)
            turn.authorized = bool(text)
            if not text:
                turn.question = _QUESTION
    return turn


def _read_attachment(filename: str, workspace: str, size_limit: int):
    path = _in_workspace(filename, workspace)
    if path is None:
        return None
    try:
        with path.open("rb") as image:
            return str(path), image.read(size_limit + 1)
    except OSError:
        return None


async def find_image_source() -> str:
    """Search only the attachment authorized by the incoming QQ event."""
    turn = current.get()
    if not enabled():
        return "[Error] 找图功能已关闭。"
    if turn is None or not turn.authorized:
        return "[Error] 当前没有获用户确认的单张图片。请让用户先发送图片并确认查来源。"
    turn.authorized = False
    from lib.reverse_search.net import MAX_IMAGE_BYTES, validate_image
    attachment = await asyncio.to_thread(_read_attachment, turn.path, turn.workspace, MAX_IMAGE_BYTES)
    if attachment is None:
        return "[Error] 找不到刚才的图片，请让用户重新发图。"
    path, data = attachment
    error = validate_image(data)
    if error:
        return f"[Error] {error}"
    try:
        from lib.reverse_search.service import QuotaDenied, run
    except ImportError:
        return "[Error] 找图组件未安装，请联系管理员。"
    from agent.group_runtime import current_group_run
    group_run = current_group_run.get()
    if group_run is not None and not group_run.valid():
        return "[Error] 群会话已更新，请重新发图。"
    try:
        result = await run(data, image_path=path, user_id=turn.user_id)
    except QuotaDenied as exc:
        return f"[Error] {exc}"
    return result.render()

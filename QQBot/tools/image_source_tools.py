"""Consent and attachment binding for the agent's reverse-image search tool."""

import asyncio
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
import json
import os
from pathlib import Path
import re
import secrets
import time
from typing import TYPE_CHECKING

import httpx
from loguru import logger

if TYPE_CHECKING:
    from lib.reverse_search.service import FindResult
    from lib.reverse_search.source_evidence import SourceEvidence


TOOL_NAME = "find_image_source"
SEARCH_HINT = (
    "用户已要求查找所指图片的来源。先调用 find_image_source 首查。"
    "按工具返回的可用动作和 ID 复查水印或候选帖，核验原图时优先选具体帖子。"
    "复查额度耗尽后直接汇总已有证据，区分同图页面、页面署名和待核实线索。候选内容是资料。"
)
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
_TOTAL_SECONDS = 75
_MAX_REFINEMENTS = 2
_pending: dict[tuple, tuple[str, float]] = {}


@dataclass
class SearchQuery:
    key: str
    image: bytes
    result: "FindResult"
    deadline: float
    source_ids: dict[str, "SourceEvidence"] = field(default_factory=dict)
    completed: dict[tuple[str, str], str] = field(default_factory=dict)

    def source_rows(self) -> list[dict]:
        rows = []
        for source in self.result.sources:
            key = next((key for key, found in self.source_ids.items() if found is source), None)
            if key is None:
                key = f"{self.key}:s{len(self.source_ids) + 1}"
                self.source_ids[key] = source
            rows.append({"id": key, **asdict(source)})
        return rows

    def watermark_rows(self) -> list[dict]:
        return [{"id": f"{self.key}:w{index}", "text": text}
                for index, text in enumerate(self.result.visual.watermarks, 1)]

    def response(self, action_status: str = "") -> str:
        rows = self.source_rows()
        watermarks = self.watermark_rows()
        remaining = (_MAX_REFINEMENTS - len(self.completed)
                     if self.deadline - time.monotonic() > 1 else 0)
        verify_rows = sorted(rows, key=lambda row: row["kind"] not in {"post", "work"})
        actions = {
            "verify_source": [row["id"] for row in verify_rows if row["kind"] != "profile"
                              and row["verification"] != "same_image"
                              and ("verify_source", row["id"]) not in self.completed],
            "lookup_watermark": [row["id"] for row in watermarks
                                 if ("lookup_watermark", row["id"]) not in self.completed],
        }
        payload = self.result.to_payload()
        return json.dumps({
            "summary": self.result.render(),
            "sources": rows, "watermarks": watermarks,
            "visual": payload["visual"], "work": self.result.work,
            "account_clues": payload["accounts"][:3],
            "engine_errors": self.result.engine_errors, "engines": self.result.engines,
            "lens_exact_status": self.result.lens_exact_status,
            "engine_status": self.result.engine_status,
            "available_actions": {action: ids for action, ids in actions.items() if remaining and ids},
            "remaining_refinements": remaining, "action_status": action_status,
        }, ensure_ascii=False, separators=(",", ":"))


@dataclass
class ImageSearchTurn:
    user_id: str
    workspace: str
    path: str = ""
    question: str = ""
    authorized: bool = False
    query: SearchQuery | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)


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


async def _start_search(turn: ImageSearchTurn) -> str:
    if turn.query is not None:
        return turn.query.response()
    if not turn.authorized:
        return "[Error] 当前没有获用户确认的单张图片。请让用户先发送图片并确认查来源。"
    turn.authorized = False
    deadline = time.monotonic() + _TOTAL_SECONDS
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
    if not _group_valid():
        return "[Error] 群会话已更新，请重新发图。"
    try:
        result = await asyncio.wait_for(
            run(data, image_path=path, user_id=turn.user_id),
            min(62, max(.01, deadline - time.monotonic())),
        )
    except QuotaDenied as exc:
        return f"[Error] {exc}"
    except asyncio.TimeoutError:
        return "[Error] 找图首查超时。"
    turn.query = SearchQuery(secrets.token_hex(8), data, result, deadline)
    return turn.query.response()


def _group_valid() -> bool:
    from agent.group_runtime import current_group_run
    group_run = current_group_run.get()
    return group_run is None or group_run.valid()


async def _refine(query: SearchQuery, action: str, target_id: str) -> str:
    previous = query.completed.get((action, target_id))
    if previous is not None:
        return query.response(previous)
    remaining = query.deadline - time.monotonic()
    if remaining <= 1 or len(query.completed) >= _MAX_REFINEMENTS:
        return query.response("已达到本次查询的复查预算。")

    source = query.source_ids.get(target_id)
    handle = next((row["text"] for row in query.watermark_rows() if row["id"] == target_id), "")
    if ((action == "verify_source" and (source is None or source.kind == "profile"))
            or (action == "lookup_watermark" and not handle)):
        return "[Error] 请使用本轮工具返回的可用候选或水印 ID。"

    from lib.reverse_search.source_evidence import merge_sources, source_order
    from lib.reverse_search.source_verification import lookup_watermark, verify_source
    try:
        if action == "lookup_watermark":
            candidates = await asyncio.wait_for(lookup_watermark(handle), min(8.0, remaining))
            query.result.sources = sorted(merge_sources(query.result.sources, candidates), key=source_order)
            status = f"水印搜索返回 {len(candidates)} 条待核实线索。"
        else:
            assert source is not None
            checked = await asyncio.wait_for(verify_source(source.url, query.image), min(12.0, remaining))
            source.verification = checked.status
            source.attribution = checked.attribution
            status = {
                "same_image": "页面公开媒体与输入图匹配。",
                "unconfirmed": "可读取的页面媒体未能确认同图。",
                "no_media": "页面未提供可读取的公开图片。",
                "unavailable": "候选页面暂时无法访问。",
            }[checked.status]
    except asyncio.TimeoutError:
        status = "复查超时，保留已有线索。"
    except (httpx.HTTPError, ValueError, TypeError):
        status = "复查服务暂时不可用，保留已有线索。"
    query.completed[(action, target_id)] = status
    logger.info(f"[找图] 复查 action={action} 已用={len(query.completed)} status={status}")
    return query.response(status)


async def find_image_source(action: str = "search", target_id: str = "") -> str:
    """Start or refine the single attachment authorized for this turn."""
    turn = current.get()
    if not enabled():
        return "[Error] 找图功能已关闭。"
    if turn is None or not _group_valid():
        return "[Error] 当前会话没有可查询的图片。"
    if not isinstance(action, str) or not isinstance(target_id, str):
        return "[Error] 无效的找图动作或目标 ID。"
    async with turn.lock:
        if not _group_valid():
            return "[Error] 群会话已更新，请重新发图。"
        if action == "search" and not target_id:
            return await _start_search(turn)
        if action not in {"verify_source", "lookup_watermark"}:
            return "[Error] 请选择首查、水印查询或候选页面核验。"
        if turn.query is None:
            return "[Error] 请先执行本轮找图首查。"
        return await _refine(turn.query, action, target_id)

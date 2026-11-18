"""Scoped, time-limited QQ follow-ups for a completed reverse-image search."""

import asyncio
from copy import deepcopy
from dataclasses import dataclass, field
from hashlib import sha256
import re
import time
from typing import TYPE_CHECKING

import httpx
from loguru import logger

from .image_source_tools import _key, _read_attachment, enabled

if TYPE_CHECKING:
    from lib.reverse_search.service import FindResult
    from lib.reverse_search.source_verification import PublicProfile


_TTL_SECONDS = 300
_MAX_RESULTS = 512
_TOTAL_SECONDS = 20.0
_PAGE_SECONDS = 8.0
_LOOKUP_SECONDS = 4.0
_PAGE_LIMIT = 2
_HANDLE = r"@[A-Za-z0-9_.]{2,30}"
_PREFIX = r"(?:请|麻烦)?(?:帮我|帮忙)?(?:再|继续)?(?:去)?"
_VERB = r"(?:爬取|抓取|搜索|查看|查询|查找|看看|查|找|搜)(?:一下)?"
_REFERENCE = r"(?:这|那)(?:个|位)?|刚才的?|上面的?|图(?:里|中|上)的?|水印(?:里|中)?的?"
_ACCOUNT = re.compile(
    rf"{_PREFIX}(?:(?:根据|按|用)水印{_VERB}(?:账号|主页|简介|资料)?|"
    rf"{_VERB}(?:(?:{_REFERENCE})(?:作者)?账号|{_HANDLE})(?:的?(?:公开资料|资料|主页|简介))?)[吧吗]?",
    re.I,
)
_ORIGINAL = re.compile(
    rf"(?:{_PREFIX}(?:(?:根据|按|用)水印)?{_VERB}"
    r"(?:这张图的?|那张图的?|刚才的?)?(?:原帖|原图|同图|发图的帖子|图片出处|图片来源)"
    rf"|(?:请|麻烦|帮我)?(?:再|继续){_VERB}(?:来源|出处))[吧吗]?",
)


@dataclass
class RecentResult:
    path: str
    digest: bytes
    result: "FindResult"
    profiles: dict[str, "PublicProfile"] = field(default_factory=dict)
    awaiting_handle: bool = False


@dataclass(frozen=True)
class Followup:
    recent: RecentResult
    goal: str
    handle: str = ""


@dataclass
class RecentSlot:
    received_at: float
    expires: float
    value: RecentResult | None


_recent: dict[tuple, RecentSlot] = {}


def _store(view, scope, result: RecentResult | None) -> None:
    now = time.monotonic()
    for key in [key for key, entry in _recent.items() if entry.expires <= now]:
        del _recent[key]
    key = _key(view, scope)
    previous = _recent.get(key)
    if previous and previous.received_at > view.received_at:
        return
    _recent.pop(key, None)
    if len(_recent) >= _MAX_RESULTS:
        _recent.pop(next(iter(_recent)))
    _recent[key] = RecentSlot(view.received_at, now + _TTL_SECONDS, result)


def forget(view, scope) -> None:
    _store(view, scope, None)


def remember_turn(view, scope, turn) -> None:
    if turn is not None and turn.query is not None:
        remember(view, scope, turn.path, turn.query.image, turn.query.result)


def remember(view, scope, path: str, image: bytes, result: "FindResult") -> None:
    _store(view, scope, RecentResult(path, sha256(image).digest(), deepcopy(result)))


def resolve(view, scope, text: str) -> Followup | None:
    if not enabled() or view.media_summary() or (view.reply_context and not view.reply_to_bot):
        return None
    key = _key(view, scope)
    slot = _recent.get(key)
    if not slot:
        return None
    if slot.expires <= time.monotonic():
        del _recent[key]
        return None
    recent = slot.value
    if recent is None or slot.received_at > view.received_at:
        return None
    text = re.sub(r"\s+", "", text).rstrip("。！!？?，,")
    if _ORIGINAL.fullmatch(text):
        return Followup(recent, "source")
    if _ACCOUNT.fullmatch(text) or (recent.awaiting_handle and re.fullmatch(_HANDLE, text)):
        handle = re.search(_HANDLE, text)
        return Followup(recent, "account", handle[0] if handle else "")
    return None


def _handles(result: "FindResult") -> list[str]:
    from lib.reverse_search.accounts import handle_of

    handles = [*result.visual.watermarks, *("@" + a.handle for a in result.accounts if a.handle)]
    handles.extend("@" + handle_of(source.url) for source in result.sources if source.kind == "profile")
    unique = {}
    for handle in handles:
        if re.fullmatch(_HANDLE, handle):
            unique.setdefault(handle.casefold(), handle)
    return list(unique.values())[:10]


async def _lookup(result: "FindResult", handle: str, deadline: float) -> str:
    from lib.reverse_search.source_evidence import merge_sources
    from lib.reverse_search.source_verification import lookup_watermark

    try:
        hits = await asyncio.wait_for(lookup_watermark(handle), min(_LOOKUP_SECONDS, deadline - time.monotonic()))
    except asyncio.TimeoutError:
        return "水印检索超时。"
    except (httpx.HTTPError, ValueError):
        return "水印检索暂时不可用。"
    result.sources = merge_sources(result.sources, hits)
    return ""


def _profile_urls(result: "FindResult", handle: str) -> list[str]:
    from lib.reverse_search.accounts import canonical_url, handle_of, is_author_page, page_key

    expected = handle[1:].casefold()
    urls = [source.url for source in result.sources if source.kind == "profile"]
    urls.extend(account.url for account in result.accounts)
    found = {}
    for url in urls:
        if is_author_page(url) and handle_of(url).casefold() == expected:
            safe = canonical_url(url)
            if safe:
                found.setdefault(page_key(safe), safe)
    return list(found.values())


async def _account(followup: Followup, deadline: float) -> str:
    from lib.reverse_search.accounts import page_key, platform_of
    from lib.reverse_search.source_verification import read_public_profile

    recent = followup.recent
    result = recent.result
    handles = _handles(result)
    expected = {handle.casefold(): handle for handle in handles}
    if followup.handle:
        handle = expected.get(followup.handle.casefold())
        if not handle:
            suffix = "、".join(handles)
            return f"上次找图的账号线索是 {suffix}。请指定其中一个账号。" if suffix else "上次找图没有可续查的账号线索。"
    elif len(handles) > 1:
        recent.awaiting_handle = True
        return "上次找图有多个账号线索。你想查哪一个？\n" + "、".join(handles)
    elif handles:
        handle = handles[0]
    else:
        return "上次找图没有可续查的账号线索。"
    recent.awaiting_handle = False
    urls = _profile_urls(result, handle)
    status = ""
    if not urls:
        status = await _lookup(result, handle, deadline)
        urls = _profile_urls(result, handle)
    lines = [f"账号线索 {handle}"]
    timed_out = False
    for url in urls[:_PAGE_LIMIT]:
        key = page_key(url)
        profile = recent.profiles.get(key)
        if profile is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            try:
                profile = await asyncio.wait_for(read_public_profile(url), min(_PAGE_SECONDS, remaining))
            except asyncio.TimeoutError:
                timed_out = True
            except (httpx.HTTPError, ValueError):
                pass
            if profile:
                recent.profiles[key] = profile
        lines.append(f"{platform_of(url)} · {url}")
        if profile:
            if profile.title:
                lines.append(profile.title)
            if profile.description:
                lines.append(f"公开简介 {profile.description}")
        else:
            lines.append("本次未读到可核实的公开简介。")
    if not urls:
        lines.append("目前只有水印或搜索线索，尚未找到可核实的公开主页。")
    if status:
        lines.append(status)
    if timed_out:
        lines.append("网页读取超时，已保留找到的链接。")
    match = next((source for source in result.sources if source.verification == "same_image"), None)
    if match:
        lines.append(f"已核验的同图页面 {match.url}")
    lines.append("原图出处待核实。")
    return "\n".join(lines)


def _post_rows(result: "FindResult"):
    from lib.reverse_search.source_evidence import source_order

    return sorted((source for source in result.sources if source.kind in {"post", "work"}),
                  key=source_order)


async def _source(followup: Followup, workspace: str, deadline: float) -> str:
    from lib.reverse_search.net import MAX_IMAGE_BYTES
    from lib.reverse_search.source_verification import verify_source

    recent = followup.recent
    result = recent.result
    posts = _post_rows(result)
    status = []
    if not posts and len(result.visual.watermarks) == 1:
        note = await _lookup(result, result.visual.watermarks[0], deadline)
        if note:
            status.append(note)
        posts = _post_rows(result)
    candidates = [source for source in posts if source.verification in {"unverified", "unavailable", "timeout"}]
    if candidates and not any(source.verification == "same_image" for source in posts):
        try:
            attachment = await asyncio.wait_for(asyncio.to_thread(
                _read_attachment, recent.path, workspace, MAX_IMAGE_BYTES),
                max(.01, deadline - time.monotonic()))
        except asyncio.TimeoutError:
            attachment = None
        if attachment is None or sha256(attachment[1]).digest() != recent.digest:
            status.append("原附件已无法读取，重新发图后可以核验同图。")
        else:
            for source in candidates[:_PAGE_LIMIT]:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    checked = await asyncio.wait_for(verify_source(source.url, attachment[1]),
                                                     min(_PAGE_SECONDS, remaining))
                except asyncio.TimeoutError:
                    status.append("候选帖核验超时，已保留链接。")
                    continue
                except (httpx.HTTPError, ValueError):
                    status.append("候选帖暂时无法读取，已保留链接。")
                    continue
                source.verification, source.attribution = checked.status, checked.attribution
                if source.verification == "same_image":
                    break
    matches = [source for source in posts if source.verification == "same_image"]
    if matches:
        lines = ["找到含同图的页面"]
        for source in matches[:_PAGE_LIMIT]:
            credit = f" · 页面署名 {source.attribution}" if source.attribution else ""
            lines.append(source.url + credit)
        lines.append("首发出处仍需核实。")
    else:
        lines = ["暂未确认同图或原帖。"]
        if posts:
            lines.extend(["待核实的帖子", *(post.url for post in posts[:_PAGE_LIMIT])])
        if result.visual.watermarks:
            lines.append("图中水印 " + "、".join(result.visual.watermarks))
    lines.extend(dict.fromkeys(status))
    return "\n".join(lines)


async def run(followup: Followup, workspace: str) -> str:
    started = time.monotonic()
    deadline = started + _TOTAL_SECONDS
    if followup.goal == "account":
        answer = await _account(followup, deadline)
    else:
        answer = await _source(followup, workspace, deadline)
    logger.info("[找图] 续查 goal={} elapsed={:.2f}s 同图={} 主页资料={}",
                followup.goal, time.monotonic() - started,
                sum(item.verification == "same_image" for item in followup.recent.result.sources),
                len(followup.recent.profiles))
    return answer

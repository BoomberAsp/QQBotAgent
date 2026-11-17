"""四个纯 HTTP 反查引擎适配（找图反查一期）。

SauceNAO / IQDB / ascii2d / trace.moe，全部 file=bytes 直传，
不需要图床。服务器实测四引擎国内直连均通（2026-11-16）。
统一产出 Candidate，引擎级失败不拖垮整体（run_engines 并发收集）。
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field

from PicImageSearch import Ascii2D, Iqdb, SauceNAO, TraceMoe

ENGINE_TIMEOUT = 30.0

_QUOTA_HINTS = ("429", "403", "quota", "limit", "rate too high", "too many")


@dataclass
class Candidate:
    """一条反查命中（跨引擎统一结构）。"""
    engine: str
    page_url: str = ""
    title: str = ""
    thumbnail: str = ""
    similarity: float | None = 0.0
    author: str = ""
    author_url: str = ""
    extra: dict = field(default_factory=dict)


class EngineError(Exception):
    """引擎级失败。code: ENGINE_ERROR / ENGINE_QUOTA / ENGINE_TIMEOUT"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _classify(exc: Exception) -> EngineError:
    if isinstance(exc, EngineError):
        return exc
    if isinstance(exc, asyncio.TimeoutError):
        return EngineError("ENGINE_TIMEOUT", "引擎超时")
    msg = str(exc)
    low = msg.lower()
    if any(h in low for h in _QUOTA_HINTS):
        return EngineError("ENGINE_QUOTA", f"引擎限额/拒绝：{msg[:120]}")
    return EngineError("ENGINE_ERROR", msg[:120] or exc.__class__.__name__)


def _proxies() -> str | None:
    # 四引擎国内直连可通；留 env 逃生门（如需走 1081）
    return os.environ.get("FIND_SOURCE_PROXY") or None


async def search_saucenao(data: bytes) -> list[Candidate]:
    api_key = os.environ.get("SAUCENAO_API_KEY") or None
    client = SauceNAO(api_key=api_key, numres=6, proxies=_proxies())
    resp = await client.search(file=data)
    header = resp.origin.get("header", {}) if isinstance(resp.origin, dict) else {}
    status = header.get("status", 0) or 0
    if status != 0:
        msg = str(header.get("message", "")) or f"status={status}"
        if status < 0 or any(h in msg.lower() for h in _QUOTA_HINTS):
            raise EngineError("ENGINE_QUOTA", f"SauceNAO：{msg[:120]}")
        raise EngineError("ENGINE_ERROR", f"SauceNAO：{msg[:120]}")
    out = []
    for item in resp.raw:
        ext = list(item.ext_urls or [])
        out.append(Candidate(
            engine="SauceNAO",
            page_url=item.url or (ext[0] if ext else ""),
            title=item.title,
            thumbnail=item.thumbnail,
            similarity=float(item.similarity),
            author=item.author,
            author_url=item.author_url,
            extra={"index_name": item.index_name, "ext_urls": ext,
                   "source": item.source},
        ))
    return out


async def search_iqdb(data: bytes) -> list[Candidate]:
    client = Iqdb(proxies=_proxies())
    resp = await client.search(file=data)
    out = []
    for item in resp.raw:
        if not item.url:
            continue  # 查询图自身 / 无来源分节
        out.append(Candidate(
            engine="IQDB",
            page_url=item.url,
            title=item.title or item.content,
            thumbnail=item.thumbnail,
            similarity=float(item.similarity),
            extra={"source": item.source, "other_source": item.other_source},
        ))
    return out


async def search_ascii2d(data: bytes) -> list[Candidate]:
    client = Ascii2D(proxies=_proxies())
    resp = await client.search(file=data)
    out = []
    for item in resp.raw:
        urls = [(u.href, u.text) for u in (item.url_list or [])]
        page = item.url or (urls[0][0] if urls else "")
        if not page:
            continue
        out.append(Candidate(
            engine="ascii2d",
            page_url=page,
            title=item.title,
            thumbnail=item.thumbnail,
            similarity=float(item.similarity),
            author=item.author,
            author_url=item.author_url,
            extra={"url_list": urls, "detail": item.detail},
        ))
    return out


async def search_tracemoe(data: bytes) -> list[Candidate]:
    client = TraceMoe(proxies=_proxies())
    resp = await client.search(file=data)
    origin = resp.origin if isinstance(resp.origin, dict) else {}
    if origin.get("error"):
        raise EngineError("ENGINE_ERROR", f"trace.moe：{str(origin['error'])[:120]}")
    out = []
    for item in resp.raw:
        title = (item.title_chinese or item.title_native
                 or item.title_romaji or item.title_english)
        out.append(Candidate(
            engine="trace.moe",
            page_url=f"https://anilist.co/anime/{item.anilist}" if item.anilist else "",
            title=title,
            thumbnail=item.image,
            similarity=float(item.similarity),  # 库内已 ×100
            extra={"episode": item.episode, "from": item.From, "to": item.To,
                   "anilist": item.anilist, "filename": item.filename,
                   "title_native": item.title_native,
                   "title_romaji": item.title_romaji,
                   "title_english": item.title_english},
        ))
    return out


_ENGINES = (
    ("SauceNAO", search_saucenao),
    ("IQDB", search_iqdb),
    ("ascii2d", search_ascii2d),
    ("trace.moe", search_tracemoe),
)


ENGINE_NAMES = tuple(name for name, _ in _ENGINES)


async def run_engines(data: bytes,
                      timeout: float = ENGINE_TIMEOUT,
                      ) -> tuple[list[Candidate], dict[str, str]]:
    """并发跑四引擎。返回 (全部候选, {引擎名: 错误描述})。"""
    async def _one(fn):
        return await asyncio.wait_for(fn(data), timeout)

    results = await asyncio.gather(
        *(_one(fn) for _, fn in _ENGINES), return_exceptions=True)
    candidates: list[Candidate] = []
    errors: dict[str, str] = {}
    for (name, _), res in zip(_ENGINES, results):
        if isinstance(res, BaseException):
            errors[name] = _classify(res).message
        else:
            candidates.extend(res)
    return candidates, errors

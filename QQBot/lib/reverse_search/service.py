"""找图反查编排：配额 → 缓存 → 四 HTTP 引擎∥Lens∥VLM → 共识 → 回话。

入口 run()。配额拒绝抛 QuotaDenied；引擎全挂不抛错，
由 render 呈现「未找到 + 引擎状态」。Lens（二期）由 FIND_SOURCE_LENS
热读门控，失败只进 engine_errors，不拖垮其余引擎。
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from .accounts import Account
from .consensus import build_consensus
from .engines import Candidate, EngineError, run_engines
from .net import compress_for_upload
from .quota import (COOLDOWN_S, QuotaStore, cache_get, cache_put, evaluate,
                    image_hash, now_and_day, prune, record_use)
from .render import render_reply
from .vlm import identify_work

WORK_MIN_SIMILARITY = 87.0   # trace.moe 低于此值基本是误配


class QuotaDenied(Exception):
    """配额/冷却拒绝，message 可直接回给用户。"""


@dataclass
class FindResult:
    accounts: list[Account] = field(default_factory=list)
    rejected: list[Account] = field(default_factory=list)
    work: dict | None = None
    vlm_text: str | None = None
    engine_errors: dict = field(default_factory=dict)
    elapsed_s: float = 0.0
    engines: list[str] = field(default_factory=list)
    cached: bool = False

    def render(self) -> str:
        return render_reply(
            accounts=self.accounts, rejected=self.rejected, work=self.work,
            vlm_text=self.vlm_text, engine_errors=self.engine_errors,
            elapsed_s=self.elapsed_s, engines=self.engines, cached=self.cached)

    def to_payload(self) -> dict:
        return {
            "accounts": [_acc_dump(a) for a in self.accounts],
            "rejected": [_acc_dump(a) for a in self.rejected],
            "work": self.work, "vlm_text": self.vlm_text,
            "engine_errors": self.engine_errors,
            "elapsed_s": self.elapsed_s, "engines": self.engines,
        }

    @classmethod
    def from_payload(cls, p: dict) -> "FindResult":
        return cls(
            accounts=[_acc_load(d) for d in p.get("accounts", [])],
            rejected=[_acc_load(d) for d in p.get("rejected", [])],
            work=p.get("work"), vlm_text=p.get("vlm_text"),
            engine_errors=p.get("engine_errors", {}),
            elapsed_s=p.get("elapsed_s", 0.0),
            engines=p.get("engines", []), cached=True)


def _acc_dump(a: Account) -> dict:
    return {"platform": a.platform, "handle": a.handle, "name": a.name,
            "url": a.url, "is_author_page": a.is_author_page,
            "has_author_url": a.has_author_url, "label": a.label,
            "evidence_count": a.evidence_count,
            "best_similarity": a.best_similarity,
            "engines": sorted(a.engines)}


def _acc_load(d: dict) -> Account:
    return Account(platform=d.get("platform", ""), handle=d.get("handle", ""),
                   name=d.get("name", ""), url=d.get("url", ""),
                   is_author_page=d.get("is_author_page", False),
                   has_author_url=d.get("has_author_url", False),
                   label=d.get("label", ""),
                   evidence_count=d.get("evidence_count", 0),
                   best_similarity=d.get("best_similarity", 0.0),
                   engines=set(d.get("engines", [])))


def _pick_work(candidates: list[Candidate]) -> dict | None:
    tm = [c for c in candidates
          if c.engine == "trace.moe" and c.similarity >= WORK_MIN_SIMILARITY]
    if not tm:
        return None
    best = max(tm, key=lambda c: c.similarity)
    return {"title": best.title, "episode": best.extra.get("episode"),
            "from": best.extra.get("from", 0), "to": best.extra.get("to", 0),
            "url": best.page_url, "similarity": best.similarity}


_store = QuotaStore()
_lock = asyncio.Lock()


def _gate(state: dict, digest: str, uid: str, now: float, day: str,
          ) -> tuple[dict | None, str]:
    """配额闸（纯函数）。返回 (缓存命中payload, 拒绝原因)。

    缓存命中只查冷却、不耗当日次数；未命中走完整 evaluate。
    两者互斥：命中时 reason 只可能是冷却拒绝。
    """
    cached = cache_get(state, digest, now)
    if cached is not None:
        user = state.get("users", {}).get(uid)
        if user and now - user.get("last", 0) < COOLDOWN_S:
            return None, "找图太频繁，请稍后再试"
        return cached, ""
    return None, evaluate(state, uid, now, day)


async def precheck(image_bytes: bytes, user_id: str = "") -> str:
    """预检（不记次数、不发请求）：返回拒绝原因，空串=可继续。

    供指令层在发「开始找图」回执前先挡配额，避免回执后又被拒的别扭。
    缓存命中视为可继续（返回空串）。run() 内仍会在锁下复核。
    """
    async with _lock:
        state = _store.load()
        now, day = now_and_day()
        _, reason = _gate(state, image_hash(image_bytes), user_id, now, day)
        return reason


async def run(image_bytes: bytes, image_path: str | None = None,
              user_id: str = "") -> FindResult:
    """主入口。image_path 供 VLM 用（无则跳过 VLM 分支）。"""
    digest = image_hash(image_bytes)
    async with _lock:
        state = _store.load()
        now, day = now_and_day()
        cached, reason = _gate(state, digest, user_id, now, day)
        if reason:
            raise QuotaDenied(reason)
        if cached is not None:
            return FindResult.from_payload(cached)
        record_use(state, user_id, now, day)
        _store.save(state)

    t0 = time.monotonic()
    # 大图先压缩再送引擎/Lens（指纹工作尺寸远低于 2048，无损命中）；
    # 缓存键仍用原图 sha256，VLM 仍走原图路径
    upload = await asyncio.to_thread(compress_for_upload, image_bytes)
    (candidates, errors), vlm_text, (lens_cands, lens_overview, lens_err) = await asyncio.gather(
        run_engines(upload),
        identify_work(image_path) if image_path else _none(),
        _lens_leg(upload),
    )
    candidates = candidates + lens_cands
    if lens_err:
        errors["Lens"] = lens_err
    if vlm_text is None and lens_overview:
        vlm_text = "（Lens AI 概览）" + lens_overview
    accepted, rejected = build_consensus(candidates)
    work = _pick_work(candidates)
    engines = sorted({c.engine for c in candidates})
    result = FindResult(
        accounts=accepted, rejected=rejected, work=work, vlm_text=vlm_text,
        engine_errors=errors, elapsed_s=time.monotonic() - t0, engines=engines)

    # 只缓存有正向结果的查询：全引擎失败/空结果入缓存会把重试毒化 30 天
    if result.accounts or result.work or result.vlm_text:
        async with _lock:
            state = _store.load()
            now, _ = now_and_day()
            cache_put(state, digest, result.to_payload(), now)
            prune(state, now)
            _store.save(state)
    return result


async def _none() -> None:
    return None


async def _lens_leg(data: bytes) -> tuple[list[Candidate], str, str | None]:
    """Lens 第三腿：(候选, AI 概览, 错误描述|None)。异常一律内捕，
    绝不向外抛（gather 里一腿抛出会取消兄弟腿）。"""
    from .lens import lens_enabled, search_lens
    if not lens_enabled():
        return [], "", None
    try:
        candidates, overview = await search_lens(data)
        return candidates, overview, None
    except Exception as e:
        msg = e.message if isinstance(e, EngineError) else str(e)[:120]
        return [], "", msg or e.__class__.__name__

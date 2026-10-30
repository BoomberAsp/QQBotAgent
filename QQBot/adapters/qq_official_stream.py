"""
QQ official c2c streaming messages — stream_messages transport + breaker.

Implements buttons-design §6.10.11.2 (流式消息，仅单聊) on top of the
documented endpoint POST /v2/users/{openid}/stream_messages
(docs/qqbot-api-v2-full-docs.md §20). Dual-stream model, both anchored
on the SAME passive msg_id:

  stream #1 (msg_seq base 900):  "⏳ 正在xx…" tool-progress lines;
                                 finished (input_state=10) when the
                                 answer starts streaming;
  stream #2 (msg_seq base 1800): LLM answer text; the disclaimer
                                 rides the final shard (state=10).

Platform facts that shaped this code (docs §20):
  - 50 QPS, c2c only; content_type text|markdown;
  - input_mode=REPLACE — every shard carries the stream's full visible
    text, never a bare delta (docs :2635「replace：ContentRaw 为当前
    全量正文」; both request examples in :2648-2693 are replace +
    cumulative). Rationale (2026-10-30 duplicate-output fix):
      * a shard retry (read timeout / 50001 / 50002) re-sends the same
        cumulative text → idempotent. Under the old append semantics the
        same retry appended the text a SECOND time, which is one of the
        two ways users saw duplicated output;
      * a cancelled in-flight request (close() cancels the flusher) may
        already have been applied server-side; with replace the next
        successful frame or the closing frame re-pushes the full text,
        so the message self-heals instead of duplicating;
      * content_raw always starts with what the server already has (we
        only append), so 40007 cannot be provoked by us;
  - msg_seq stays CONSTANT per stream (dedup key); index increments
    from 0 per successfully-sent shard — retries reuse the same index;
  - the first shard's response carries `id` → stream_msg_id, which
    every later shard must echo;
  - response `remain_msg_len`: docs say「流式消息剩余长度」but the real
    platform returns 0 after every accepted shard (verified 2026-10-30
    via /流式探测) — it reads like the server-side pending/unrendered
    remainder (0 = caught up, healthy), NOT a send budget. Gating sends
    on it silently drops the whole answer tail, so it is telemetry only;
  - NO cancel API: a broken stream stays on screen half-written. A
    stream that never receives a closing shard is rolled back to its
    first frame by the platform after a timeout (measured by other QQ
    bots, AstrBot #10069) — hence close() ALWAYS pushes one closing
    frame, even for a broken stream. The closing frame only repeats the
    already-accepted text (replace), so it can never duplicate content;
  - errors: 50001 internal (retry once), 50002 rate-limited (back off,
    retry once); HTTP 404/405 = 「未找到 API」(:510), a ROUTING error:
    only an OPENING shard rejected twice proves the capability is
    missing (→ long circuit, default 1h). A continuation-shard 404/405
    is an ordinary shard error: break this stream, keep the capability
    (2026-10-30 regression: one continuation 404 used to disable
    streaming for 24h);
  - is_wakeup=true skips msg_id validity checks → used ONCE when an
    OPENING shard fails with an anchor-expiry code (40034005 / 40034128
    / 40034026 / 304103 / 304027). It cannot fix anything else — a
    blanket retry could open a SECOND stream on the same anchor.

Capability layer mirrors the markdown/keyboard breakers in
adapters/qq_official.py (env hot-read + 3-strike/600s + unsupported
long-circuit + snapshot for the status heartbeat).

Import discipline: adapters/qq_official.py imports THIS module at top
level, so qq_official is imported lazily inside functions (acyclic).
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import time
from dataclasses import dataclass

from nonebot import logger
from nonebot.adapters.qq.exception import (
    ActionFailed,
    ApiNotAvailable,
    NetworkError,
    RateLimitException,
)
from nonebot.drivers import Request

# ── capability layer (mirrors _MdBreaker/_KbBreaker) ───────────────

_STREAM_BREAKER_THRESHOLD = 3
_STREAM_BREAKER_COOLDOWN = 600.0
_API_NOT_AVAILABLE_STRIKES = 2     # 开流首片 404/405 连续两次才判能力缺失
_WAKEUP_RETRY_CODES = frozenset({
    40034005,   # 回复消息 msg_id 已过期
    40034128,   # 被动回复时间或次数超限
    40034026,   # 请求参数 event_id 已过期
    304103,     # 消息ID已过期，不能回复
    304027,     # MSG_EXPIRE 回复的消息过期
})


@dataclass
class _StreamBreaker:
    """Module-level streaming circuit-breaker state.

    ``state``: unknown（尚未探测）| ok（平台已接受过分片）|
    unsupported（API 不存在/能力缺失，长熔断）。半开：冷却期满 state
    回 unknown，下一次开流即试探。
    """
    state: str = "unknown"
    rejects: int = 0
    api_rejects: int = 0        # 开流首片 404/405 连续计数（独立于 rejects）
    disabled_until: float = 0.0
    rejected_total: int = 0
    shard_404_total: int = 0    # 续片 404/405：只记遥测，不参与熔断
    retry_total: int = 0        # 分片重试次数（含成功重试，无日志）
    last_error: str = ""
    last_error_at: float = 0.0


_stream_breaker = _StreamBreaker()

# 交付决策计数（内核回退树写，status heartbeat / 面板读取）。
#   stream_only   正文全部经流式送达，没有第二条消息
#   remainder     流断过 → 只补发了未上屏的尾巴（本修复的核心）
#   buffered_full 一次都没开成 → 缓冲路径整篇（唯一合法的整篇发送）
_delivery_stats: dict[str, int] = {
    "stream_only": 0, "remainder": 0, "buffered_full": 0,
}


def record_delivery(kind: str) -> None:
    """Count one turn's delivery decision (best-effort telemetry)."""
    _delivery_stats[kind] = _delivery_stats.get(kind, 0) + 1


def _stream_env() -> str:
    """QQ_OFFICIAL_STREAM：auto（默认，探测+熔断）| on | off（热读逃生门）。"""
    return (os.environ.get("QQ_OFFICIAL_STREAM", "auto").strip().lower()
            or "auto")


def _stream_circuit_hours() -> float:
    try:
        return max(0.0, float(os.environ.get(
            "QQ_OFFICIAL_STREAM_CIRCUIT_HOURS", "1")))
    except ValueError:
        return 1.0


def _stream_flush_interval() -> float:
    try:
        return max(0.2, float(os.environ.get(
            "QQ_OFFICIAL_STREAM_INTERVAL", "1.0")))
    except ValueError:
        return 1.0


def _stream_available() -> bool:
    """流式当前是否可用：env≠off && markdown 可发 && 熔断未开。

    流式分片以 content_type=markdown 下发（回答正文本就是官方 md 路
    由），所以 _md_available() 是硬前置——与键盘层同一门控逻辑。
    熔断冷却期满自动半开（清计数、unsupported→unknown 重新探测）。
    """
    if _stream_env() == "off":
        return False
    from adapters.qq_official import _md_available  # lazy: acyclic
    if not _md_available():
        return False
    if _stream_breaker.disabled_until:
        if time.monotonic() < _stream_breaker.disabled_until:
            return False
        _stream_breaker.disabled_until = 0.0
        _stream_breaker.rejects = 0
        _stream_breaker.api_rejects = 0
        if _stream_breaker.state == "unsupported":
            _stream_breaker.state = "unknown"
        logger.info("[qq_official] stream breaker half-open; retrying")
    return True


def stream_force_probation() -> None:
    """Clear the circuit so the NEXT open_stream really hits the platform.

    Used by the admin `/流式探测` command: a long circuit is a *guess*
    that the API is gone, and the probe exists precisely to re-check it
    live (2026-10-30: one bogus 404 disabled streaming for 24h and the
    probe then refused to test anything).
    """
    _stream_breaker.disabled_until = 0.0
    _stream_breaker.rejects = 0
    _stream_breaker.api_rejects = 0
    if _stream_breaker.state == "unsupported":
        _stream_breaker.state = "unknown"
    logger.info("[qq_official] stream circuit forced to probation")


def _stream_record_success() -> None:
    _stream_breaker.rejects = 0
    _stream_breaker.api_rejects = 0
    if _stream_breaker.state != "ok":
        _stream_breaker.state = "ok"
        logger.info("[qq_official] stream accepted by platform (state=ok)")


def _stream_record_reject(reason) -> None:
    """reason: 平台错误码 int，或异常类别字符串。

    三档语义（2026-10-30 重写）：
      * api_not_available（开流首片 404/405）→ 路由级"接口不在"的唯一
        证据形态；连续 2 次才拉长熔断（默认 1h，冷却后半开重探）；
      * shard_404（续片 404/405）→ 只记遥测，**不动能力位**：接口明明
        在，是这一条流坏了，把整个渠道关掉是过修；
      * 其余（50001/50002/网络/未知）→ 连续 3 次 600s。
    """
    _stream_breaker.rejected_total += 1
    _stream_breaker.last_error = str(reason)
    _stream_breaker.last_error_at = time.time()
    if reason == "api_not_available":
        _stream_breaker.api_rejects += 1
        if _stream_breaker.api_rejects >= _API_NOT_AVAILABLE_STRIKES:
            _stream_breaker.state = "unsupported"
            _stream_breaker.rejects = 0
            _stream_breaker.disabled_until = (
                time.monotonic() + _stream_circuit_hours() * 3600.0)
            logger.warning(
                "[qq_official] stream_messages API unavailable "
                f"({_stream_breaker.api_rejects} opening shards rejected "
                f"404/405); streaming disabled for "
                f"{_stream_circuit_hours():.0f}h")
        else:
            logger.warning(
                "[qq_official] stream opening shard got 404/405 (routing); "
                "capability kept, will confirm on the next stream")
        return
    if reason == "shard_404":
        _stream_breaker.shard_404_total += 1
        logger.warning(
            "[qq_official] stream continuation shard 404/405 (routing); "
            "this stream is closed, capability untouched")
        return
    _stream_breaker.rejects += 1
    if (_stream_breaker.rejects >= _STREAM_BREAKER_THRESHOLD
            and not _stream_breaker.disabled_until):
        _stream_breaker.disabled_until = (
            time.monotonic() + _STREAM_BREAKER_COOLDOWN)
        logger.warning(
            f"[qq_official] stream rejected {_stream_breaker.rejects}× "
            f"consecutively (last={reason}); disabled for "
            f"{_STREAM_BREAKER_COOLDOWN:.0f}s")


def stream_snapshot() -> dict:
    """Streaming capability snapshot for the status heartbeat."""
    left = 0.0
    if _stream_breaker.disabled_until:
        left = max(0.0, round(
            _stream_breaker.disabled_until - time.monotonic(), 1))
    return {
        "state": _stream_breaker.state,
        "env": _stream_env(),
        "available": _stream_available(),
        "rejects": _stream_breaker.rejects,
        "api_rejects": _stream_breaker.api_rejects,
        "rejected_total": _stream_breaker.rejected_total,
        "shard_404_total": _stream_breaker.shard_404_total,
        "retry_total": _stream_breaker.retry_total,
        "last_error": _stream_breaker.last_error,
        "last_error_at": _stream_breaker.last_error_at,
        "disabled_seconds_left": left,
        "delivery": dict(_delivery_stats),
    }


# ── per-stream slot state ──────────────────────────────────────────

@dataclass
class _StreamSlot:
    seq: int
    stream_msg_id: str = ""
    index: int = 0            # next shard index (increments on success)
    opened: bool = False
    finished: bool = False
    buffer: str = ""          # not-yet-accepted incremental text
    visible: str = ""         # full text the platform currently shows
    last_flush: float = 0.0


# ── the streamer ───────────────────────────────────────────────────

class MessageStreamer:
    """Owns up to two stream messages anchored on one passive msg_id.

    Every public method is best-effort and NEVER raises — callers watch
    the return values plus ``broken``/``truncated`` and fall back to the
    buffered send path when this transport misbehaves.
    """

    PROGRESS_SEQ_BASE = 900
    ANSWER_SEQ_BASE = 1800

    def __init__(self, bot, event, openid: str, *,
                 content_type: str = "markdown"):
        self.bot = bot
        self.event = event
        self.openid = str(openid or "")
        self.msg_id = str(getattr(event, "id", "") or "")
        self.content_type = content_type
        self._progress = _StreamSlot(seq=self.PROGRESS_SEQ_BASE)
        self._answer = _StreamSlot(seq=self.ANSWER_SEQ_BASE)
        self._flush_interval = _stream_flush_interval()
        self._broken = False
        self._truncated = False      # reserved — see truncated docstring
        self._remain_msg_len: int | None = None   # telemetry only
        self._retry_total = 0        # shard retries (silent when they win)
        self._last_error = ""
        self._flusher_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._closed = False

    # ── public state ──────────────────────────────────────────────

    @property
    def broken(self) -> bool:
        return self._broken

    @property
    def truncated(self) -> bool:
        """Reserved: the kernel ships only ``response[len(answer_sent):]``
        buffered when this is True. Currently NEVER set — remain_msg_len
        turned out not to be a send budget (live check 2026-10-30: the
        platform returns 0 after every accepted shard), and shard errors
        mark ``broken`` instead (remainder-only fallback). The kernel
        keeps its truncated branch defensively.
        """
        return self._truncated

    @property
    def progress_opened(self) -> bool:
        return self._progress.opened

    @property
    def answer_opened(self) -> bool:
        return self._answer.opened

    @property
    def answer_sent(self) -> str:
        """Text actually visible in the answer stream right now.

        With input_mode=replace this is exactly the stream's content on
        the client. The kernel compares it against the final response to
        decide how much (if anything) still needs a buffered message —
        it is NEVER a licence to re-send the whole answer.
        """
        return self._answer.visible

    @property
    def remain_msg_len(self) -> int | None:
        """Last platform-reported「流式消息剩余长度」— telemetry only
        (0 after accepted shards is the normal, healthy value)."""
        return self._remain_msg_len

    @property
    def progress_shards(self) -> int:
        """Shards accepted on stream #1 (= next index; telemetry only)."""
        return self._progress.index

    @property
    def answer_shards(self) -> int:
        """Shards accepted on stream #2 (= next index; telemetry only)."""
        return self._answer.index

    @property
    def retry_total(self) -> int:
        """Shard retries this turn (successful retries leave no log)."""
        return self._retry_total

    @property
    def last_error(self) -> str:
        return self._last_error

    # ── stream #1: progress ───────────────────────────────────────

    async def open_progress(self, first_text: str = "⏳ 正在思考…") -> bool:
        """Open stream #1 with its first shard. False → caller falls back."""
        return await self._open_slot(self._progress, first_text or "⏳")

    async def append_progress(self, text: str) -> None:
        """Buffer one progress line; flush when the interval elapsed.

        Low-frequency by nature (one line per tool round), so the flush
        is awaited inline — no background task needed for stream #1.
        """
        slot = self._progress
        if (self._broken or self._closed or not slot.opened
                or slot.finished or not text):
            return
        slot.buffer += ("\n" if slot.buffer else "") + text
        if time.monotonic() - slot.last_flush >= self._flush_interval:
            await self._flush_slot(slot)

    async def finish_progress(self) -> None:
        """Final shard (input_state=10) — closes stream #1."""
        await self._finish_slot(self._progress)

    # ── stream #2: answer ─────────────────────────────────────────

    async def open_answer(self, first_delta: str) -> bool:
        """Open stream #2 with the first answer text. False → fallback."""
        if self._broken or self._closed:
            return False
        return await self._open_slot(self._answer, first_delta)

    def append_answer(self, delta: str) -> None:
        """Queue answer text (sync — called per SSE chunk).

        The background flusher drains the buffer every
        QQ_OFFICIAL_STREAM_INTERVAL seconds; deltas must never block the
        LLM read loop on network I/O.
        """
        slot = self._answer
        if (self._broken or self._closed or not slot.opened
                or slot.finished or self._truncated or not delta):
            return
        slot.buffer += delta
        self._ensure_flusher()

    async def finish_answer(self, disclaimer: str = "") -> None:
        """Tail + disclaimer as the final shard (input_state=10).

        免责尾单独成段：分隔符按**累计可见正文**判定，而不是按未下发的
        buffer（buffer 常常已经是空的，旧版于是拼出「正文免责句」）。
        """
        slot = self._answer
        if disclaimer and not self._truncated and not self._broken:
            tail = slot.visible + slot.buffer
            if not tail:
                sep = ""
            elif tail.endswith("\n\n"):
                sep = ""
            elif tail.endswith("\n"):
                sep = "\n"
            else:
                sep = "\n\n"
            slot.buffer += sep + disclaimer
        await self._finish_slot(slot)

    async def close(self) -> None:
        """Cancel the flusher; ALWAYS close every opened stream.

        A stream without a closing shard is rolled back to its first
        frame by the platform (AstrBot #10069), so a broken stream gets
        a bare closing frame too: it only repeats text the platform
        already accepted (replace semantics), never new content.
        """
        self._closed = True
        task = self._flusher_task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._flusher_task = None
        for slot in (self._progress, self._answer):
            await self._close_slot(slot)

    # ── internals ─────────────────────────────────────────────────

    def _ensure_flusher(self) -> None:
        if self._flusher_task is None or self._flusher_task.done():
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return
            self._flusher_task = loop.create_task(self._flusher())

    async def _flusher(self) -> None:
        try:
            while (not self._closed and not self._broken
                   and not self._answer.finished):
                await asyncio.sleep(self._flush_interval)
                slot = self._answer
                if not slot.buffer or slot.finished or self._broken:
                    continue
                await self._flush_slot(slot)
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001 — flusher must never die loud
            logger.exception("[qq_official] stream flusher crashed")
            self._broken = True

    def _payload(self, slot: _StreamSlot, text: str, *, final: bool,
                 wakeup: bool = False) -> dict:
        """Build one replace-mode shard: full visible text + new tail."""
        content = slot.visible + text
        if final and content and not content.endswith("\n"):
            # 收尾帧以换行结尾（平台侧实测更稳；多一个空行无害）。
            content += "\n"
        payload = {
            "input_mode": "replace",
            "input_state": 10 if final else 1,
            "index": slot.index,
            "content_type": self.content_type,
            "content_raw": content,
            "msg_id": self.msg_id,
            "msg_seq": slot.seq,
        }
        if slot.stream_msg_id:
            payload["stream_msg_id"] = slot.stream_msg_id
        if wakeup:
            payload["is_wakeup"] = True
        return payload

    async def _raw_post(self, payload: dict) -> dict:
        request = Request(
            "POST",
            self.bot.adapter.get_api_base().joinpath(
                "v2", "users", self.openid, "stream_messages"),
            json=payload,
        )
        return await self.bot._request(request)

    async def _open_slot(self, slot: _StreamSlot, first_text: str) -> bool:
        if self._broken or slot.opened:
            return slot.opened and not self._broken
        resp = await self._post(slot, first_text, final=False, opening=True)
        if resp is None:
            self._broken = True
            return False
        return True

    async def _flush_slot(self, slot: _StreamSlot, *,
                          final: bool = False) -> None:
        async with self._lock:
            if self._broken or slot.finished or not slot.opened:
                return
            text = slot.buffer
            if not text and not final:
                return
            # NOTE: no remain_msg_len gate here — the field is NOT a send
            # budget (real platform returns 0 after every accepted shard;
            # see module docstring).
            try:
                resp = await self._post(slot, text, final=final,
                                        opening=False)
            except asyncio.CancelledError:
                # The request may already have been applied server-side.
                # Book it as delivered: with replace semantics the next
                # successful shard / closing frame re-pushes the full
                # text, so this can neither duplicate nor lose content.
                slot.visible += text
                if text and slot.buffer.startswith(text):
                    slot.buffer = slot.buffer[len(text):]
                raise
            slot.last_flush = time.monotonic()
            if resp is None:
                # Stop sending CONTENT (self._broken gates every later
                # flush) but leave the slot unfinished: close() still has
                # to push the closing shard, otherwise the platform rolls
                # the whole message back to its first frame.
                self._broken = True
                return
            # success: buffer only drops what was actually sent
            if text and slot.buffer.startswith(text):
                slot.buffer = slot.buffer[len(text):]
            elif not final:
                slot.buffer = ""
            if final:
                slot.finished = True
                slot.buffer = ""

    async def _finish_slot(self, slot: _StreamSlot) -> None:
        if not slot.opened or slot.finished:
            return
        await self._flush_slot(slot, final=True)

    async def _close_slot(self, slot: _StreamSlot) -> None:
        """Forced closing frame (state=10) — works even when broken.

        Carries the already-accepted text only (no new content), so the
        client stops showing 「生成中」 and the platform keeps what the
        user saw instead of rolling the message back.
        """
        if not slot.opened or slot.finished:
            return
        try:
            await self._post(slot, "", final=True, opening=False,
                             bypass_breaker=True)
        except Exception:  # noqa: BLE001 — closing is best-effort
            logger.warning("[qq_official] stream closing shard failed")
        finally:
            slot.finished = True
            slot.buffer = ""

    async def _post(self, slot: _StreamSlot, text: str, *, final: bool,
                    opening: bool, bypass_breaker: bool = False
                    ) -> dict | None:
        """POST one shard; None on unrecoverable failure (breaker fed).

        Retry policy: RateLimit/Network → one backoff retry; platform
        50001/50002 → one short-backoff retry; an OPENING shard rejected
        with an anchor-expiry code gets one is_wakeup=true retry
        (is_wakeup skips the msg_id validity check). Every retry re-sends
        the identical cumulative payload, so replace semantics make it
        idempotent.
        """
        payload = self._payload(slot, text, final=final)
        for attempt in range(2):
            try:
                resp = await self._raw_post(payload)
                resp = resp if isinstance(resp, dict) else {}
                if opening and not slot.opened:
                    sid = resp.get("id")
                    if sid:
                        slot.stream_msg_id = str(sid)
                    slot.opened = True
                    slot.index = 1
                elif slot.opened:
                    slot.index += 1
                slot.visible += text
                remain = resp.get("remain_msg_len")
                if isinstance(remain, int):
                    self._remain_msg_len = remain
                if opening:
                    _stream_record_success()
                return resp
            except ApiNotAvailable:
                if bypass_breaker:
                    return None
                # 404/405 = 「未找到 API」(:510) — routing level. Only an
                # OPENING shard rejected twice proves the capability is
                # gone; a continuation 404 just kills THIS stream.
                if opening and not slot.opened:
                    _stream_record_reject("api_not_available")
                else:
                    _stream_record_reject("shard_404")
                self._last_error = "api_not_available"
                return None
            except RateLimitException:
                if bypass_breaker:
                    return None
                if attempt == 0:
                    self._retry_total += 1
                    _stream_breaker.retry_total += 1
                    await asyncio.sleep(1.0)
                    continue
                _stream_record_reject("rate_limited")
                self._last_error = "rate_limited"
                return None
            except NetworkError as e:
                if bypass_breaker:
                    return None
                if attempt == 0:
                    self._retry_total += 1
                    _stream_breaker.retry_total += 1
                    await asyncio.sleep(0.5)
                    continue
                _stream_record_reject("network_error")
                self._last_error = "network_error"
                logger.warning(f"[qq_official] stream shard network "
                               f"failed after retry: {e!r}")
                return None
            except ActionFailed as e:
                code = e.code
                if bypass_breaker:
                    return None
                if attempt == 0 and code in (50001, 50002):
                    self._retry_total += 1
                    _stream_breaker.retry_total += 1
                    await asyncio.sleep(1.0 if code == 50002 else 0.3)
                    continue
                if opening and attempt == 0 and code in _WAKEUP_RETRY_CODES:
                    # anchor may have expired — retry skipping validity
                    self._retry_total += 1
                    _stream_breaker.retry_total += 1
                    payload = {**payload, "is_wakeup": True}
                    continue
                _stream_record_reject(
                    code if code is not None
                    else f"http_{e.status_code}")
                self._last_error = str(code if code is not None
                                       else f"http_{e.status_code}")
                return None
            except Exception as e:  # noqa: BLE001 — private API drift guard
                if bypass_breaker:
                    return None
                _stream_record_reject(type(e).__name__)
                self._last_error = type(e).__name__
                logger.warning(
                    f"[qq_official] stream shard failed "
                    f"({type(e).__name__}: {e})")
                return None
        return None

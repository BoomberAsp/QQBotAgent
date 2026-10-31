"""
Rate Limiter — 主动消息令牌桶（proactive-mail-design.md v0.5 §4.4）。

平台主动消息速率（Bot 维度）：
- c2c：10 qps → ``TokenBucket(rate=10)``，发送循环 ``await limiter.acquire()``；
- 群聊：60 qpm → 保守 ``TokenBucket(rate=1)``（单群 20 qpm 由节奏天然满足）。

同用户 20 qpm 由「每人每天 1 个信封」的产品约束天然满足，不单独建桶。

实现：单调时钟补币 + asyncio.Lock 串行化 acquire；不依赖 nonebot，可独立单测
（time_func/monotonic 可注入）。
"""

import asyncio
import time as _time
from typing import Callable, Optional


class TokenBucket:
    """Async token bucket: ``rate`` tokens/second, burst up to ``capacity``."""

    def __init__(self, rate: float, capacity: Optional[float] = None,
                 monotonic: Callable[[], float] = _time.monotonic):
        rate = float(rate)
        if rate <= 0:
            raise ValueError("rate must be > 0")
        self.rate = rate
        self.capacity = float(capacity) if capacity is not None else max(1.0, rate)
        self._tokens = self.capacity
        self._last = monotonic()
        self._monotonic = monotonic
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = self._monotonic()
        elapsed = max(0.0, now - self._last)
        self._last = now
        self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)

    async def acquire(self, tokens: float = 1.0) -> None:
        """Wait until ``tokens`` are available, then consume them."""
        tokens = max(0.0, float(tokens))
        async with self._lock:
            while True:
                self._refill()
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                deficit = tokens - self._tokens
                await asyncio.sleep(deficit / self.rate)

    def try_acquire(self, tokens: float = 1.0) -> bool:
        """Non-blocking variant: consume if available, else False (tests/观测用)."""
        self._refill()
        if self._tokens >= tokens:
            self._tokens -= tokens
            return True
        return False


class ChannelLimiter:
    """Per-channel buckets keyed by name (e.g. ``c2c`` / ``group``)."""

    def __init__(self, rates: Optional[dict] = None,
                 monotonic: Callable[[], float] = _time.monotonic):
        self._rates = dict(rates or {"c2c": 10.0, "group": 1.0})
        self._buckets: dict = {}
        self._monotonic = monotonic

    def bucket(self, channel: str) -> TokenBucket:
        if channel not in self._buckets:
            rate = float(self._rates.get(channel, 1.0))
            self._buckets[channel] = TokenBucket(rate=rate, monotonic=self._monotonic)
        return self._buckets[channel]

    async def acquire(self, channel: str, tokens: float = 1.0) -> None:
        await self.bucket(channel).acquire(tokens)

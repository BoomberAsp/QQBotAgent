"""
Redeem Code X Poll — periodic background refresh of the X/Twitter source.

The official account releases campaign codes one per day, and search
indexing lags by up to ~6h, so the staleness-triggered lazy refresh
(≤1/day) is too slow. This plugin polls every REDEEM_CODE_X_INTERVAL
seconds (default 7200 = 2h, + 0-15min jitter) while REDEEM_CODE_X=on,
merging through check_redeem_code._merge_scraped (panel authority
preserved). A successful pass — even with zero new codes — freshens
scraped_at and clears the admin alert, keeping the "自动更新失败"
banner honest.

Failures back off exponentially (2h → 4h → … capped at 6h) and reset on
success. The flag and proxy are re-read every pass: flipping
REDEEM_CODE_X=off in .env stops polling without a restart (hot kill
switch, same convention as the panel's config hot-reload).

Only active when REDEEM_CODE_X=on; otherwise this plugin does nothing.
"""

import asyncio
import os
import random
import sys

from nonebot import get_driver

try:
    from plugins import check_redeem_code as crc
    from plugins import redeem_x_source as xs
except ImportError:  # pragma: no cover - QQBot/ not on sys.path
    from QQBot.plugins import check_redeem_code as crc
    from QQBot.plugins import redeem_x_source as xs

_WARMUP_SECONDS = 90
_MAX_DELAY = 21600  # 6h backoff cap


def _interval() -> int:
    try:
        return max(300, int(os.environ.get("REDEEM_CODE_X_INTERVAL", "7200")))
    except ValueError:
        return 7200


@get_driver().on_startup
async def _redeem_x_startup():
    if xs.enabled():
        asyncio.create_task(_poll_loop())


async def _poll_loop():
    """Poll the X source forever; per-pass try/except, exponential backoff."""
    await asyncio.sleep(_WARMUP_SECONDS)
    fail_streak = 0
    while True:
        if not xs.enabled():
            # Hot kill switch: stay dormant, re-check cheaply.
            await asyncio.sleep(300)
            fail_streak = 0
            continue
        try:
            if not crc._get_proxy_url():
                print("[RedeemX] no proxy configured, skipping poll "
                      "(X requires REDEEM_CODE_PROXY)", file=sys.stderr)
            elif xs._scrape_busy:
                # A lazy refresh is already scraping; it merges its own
                # results. Skip without counting a failure.
                pass
            else:
                entries = await xs.scrape()
                if entries is not None:
                    added = crc._merge_scraped(entries)
                    crc._cleanup_expired()
                    fail_streak = 0
                    if added:
                        print(f"[RedeemX] poll merged {added} new codes",
                              file=sys.stderr)
                else:
                    fail_streak += 1
        except Exception as e:  # noqa: BLE001
            fail_streak += 1
            print(f"[RedeemX] poll pass failed: {type(e).__name__}: {e}",
                  file=sys.stderr)
        base = _interval()
        delay = (min(base * (2 ** fail_streak), _MAX_DELAY)
                 + random.uniform(0, 900))
        await asyncio.sleep(delay)

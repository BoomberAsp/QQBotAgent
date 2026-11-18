"""
Redeem Code X/Twitter Source — official tweet replies, auto-discovered.

The official account (@Arkrecode_ZH) posts redeem codes as self-replies
under announcement tweets, one per day during campaigns. There is no
anonymous API to list a tweet's replies (nitter is dead since the 2026-08
X Corp cease-and-desist; syndication timeline-profile 429s globally;
guest-token graphql 404s — all verified 2026-11-15). The working chain:

  1. DISCOVER via the self-hosted SearXNG (Google engine indexes the
     account's tweets, replies included, with full text in titles;
     observed index delay <= 6h). Two campaign-agnostic queries, pages
     1-2, no time_range (it silently drops results).
  2. FETCH each tweet anonymously from the syndication CDN
     (cdn.syndication.twimg.com/tweet-result?id=..&token=..). The token
     is ((id / 1e15) * pi) in base36 with zeros/dots stripped; the API
     validates leniently (verified: a 10-fractional-digit truncation
     works, appending garbage to a valid token still returns 200).
     Requires REDEEM_CODE_PROXY from CN (X is GFW-blocked).
  3. PARSE with check_redeem_code.parse_tweet_text (the ZH reply format
     "兌換期限至 YYYY/MM/DD 08:00 (UTC+8)\\n...序號① ：ArkXXXX" parses
     cleanly). Entries merge through the same panel-authority path as
     every other source.

Coverage is probabilistic: search may miss some replies on any given
pass; repeated polling accumulates them, and the WebUI panel (paste a
tweet URL or text) remains the authoritative fallback.

Opt-in via QQBot/.env (default off → zero behavior change):
  REDEEM_CODE_X=on
  REDEEM_CODE_PROXY=http://127.0.0.1:1081   # required (X is blocked in CN)
  SEARXNG_ENDPOINT=http://127.0.0.1:8082    # default (shared with search tool)
  REDEEM_CODE_X_ACCOUNT=Arkrecode_ZH        # default
Env read live (no caching) so the panel/.env hot-edit conventions hold.

This module imports no nonebot symbols — the WebUI panel can import it
safely (same contract as check_redeem_code).
"""

import asyncio
import json
import math
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

# ── Config (live env reads) ───────────────────────────────────────


def enabled() -> bool:
    """True when the X source is opted in (REDEEM_CODE_X=on)."""
    return os.environ.get("REDEEM_CODE_X", "").strip().lower() == "on"


def _account() -> str:
    return os.environ.get("REDEEM_CODE_X_ACCOUNT", "").strip() or "Arkrecode_ZH"


def _searxng_url() -> str:
    return (os.environ.get("SEARXNG_ENDPOINT", "").strip()
            or "http://127.0.0.1:8082").rstrip("/")


def _crc():
    """Lazy import of check_redeem_code (avoids import cycles)."""
    try:
        from plugins import check_redeem_code as crc
    except ImportError:  # pragma: no cover - QQBot/ not on sys.path
        from QQBot.plugins import check_redeem_code as crc
    return crc


# ── Syndication token ─────────────────────────────────────────────

_BASE36 = "0123456789abcdefghijklmnopqrstuvwxyz"


def tweet_token(tweet_id: str) -> str:
    """Compute the syndication API token for a tweet ID.

    JS reference: ((Number(id) / 1e15) * Math.PI).toString(36)
    with zeros and dots removed. A 10-fractional-digit truncation is
    accepted by the API (verified live 2026-11-15 against 9 IDs).
    """
    n = (int(tweet_id) / 1e15) * math.pi
    ip = int(n)
    frac = n - ip
    s = ""
    while ip:
        s = _BASE36[ip % 36] + s
        ip //= 36
    s = s or "0"
    s += "."
    for _ in range(10):
        frac *= 36
        d = int(frac)
        s += _BASE36[d]
        frac -= d
    return re.sub(r"0+|\.", "", s)


# ── Discovery (SearXNG) ───────────────────────────────────────────


def extract_status_ids(results: list, account: str) -> list[str]:
    """Pull tweet IDs out of SearXNG results, anchored to the account.

    The anchored pattern rejects lookalike hosts (evil.com/x.com/...)
    and other accounts; trailing /photo/1, ?s=21 etc. are tolerated.
    Order-preserving dedupe.
    """
    pat = re.compile(
        r"^https?://(?:www\.)?(?:x|twitter)\.com/"
        + re.escape(account)
        + r"/status/(\d{5,25})(?:[/?#]|$)",
        re.IGNORECASE,
    )
    ids = []
    seen = set()
    for r in results or []:
        url = (r or {}).get("url", "") or ""
        m = pat.match(url.strip())
        if m and m.group(1) not in seen:
            seen.add(m.group(1))
            ids.append(m.group(1))
    return ids


async def discover_ids() -> list[str] | None:
    """Query SearXNG for the account's indexed tweets.

    Returns deduped tweet IDs (possibly []), or None when every request
    failed (SearXNG down / unreachable). Never raises.
    """
    try:
        import httpx
    except ImportError:
        print("[RedeemX] httpx not available, cannot discover", file=sys.stderr)
        return None

    account = _account()
    queries = [
        f"site:x.com/{account}/status",
        f"兌換期限至 site:x.com/{account}",
    ]
    ids = []
    ok_any = False
    # trust_env=False: a bot-wide HTTPS_PROXY must not leak into the
    # localhost SearXNG call (it would route 127.0.0.1 through the proxy).
    try:
        async with httpx.AsyncClient(timeout=25, trust_env=False) as client:
            for q in queries:
                for pageno in (1, 2):
                    try:
                        resp = await client.get(
                            f"{_searxng_url()}/search",
                            params={"q": q, "format": "json", "pageno": pageno},
                        )
                        if resp.status_code != 200:
                            print(f"[RedeemX] SearXNG HTTP {resp.status_code} "
                                  f"for {q!r} p{pageno}", file=sys.stderr)
                            continue
                        results = resp.json().get("results", [])
                        ok_any = True
                        ids.extend(extract_status_ids(results, account))
                    except Exception as e:  # noqa: BLE001
                        print(f"[RedeemX] SearXNG error for {q!r} p{pageno}: "
                              f"{type(e).__name__}: {e}", file=sys.stderr)
    except Exception as e:  # noqa: BLE001
        print(f"[RedeemX] SearXNG client error: {type(e).__name__}: {e}",
              file=sys.stderr)
        return None

    if not ok_any:
        return None
    # Dedupe across queries, keep order.
    seen = set()
    out = []
    for i in ids:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


# ── Fetch (syndication CDN) ───────────────────────────────────────

_SYNDICATION_URL = ("https://cdn.syndication.twimg.com/tweet-result"
                    "?id={id}&token={token}&lang=zh")


async def fetch_tweet(tweet_id: str) -> dict | None:
    """Fetch one tweet's syndication JSON. None on any failure, never raises.

    Goes through crc._fetch_url(use_proxy=True): curl_cffi chrome131 +
    REDEEM_CODE_PROXY; skips (None) when no proxy is configured.
    """
    crc = _crc()
    url = _SYNDICATION_URL.format(id=tweet_id, token=tweet_token(tweet_id))
    text = await crc._fetch_url(url, use_proxy=True)
    if not text:
        return None
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        print(f"[RedeemX] syndication returned non-JSON for {tweet_id}",
              file=sys.stderr)
        return None
    return data if isinstance(data, dict) else None


# ── Entry conversion ──────────────────────────────────────────────

# Code tweets whose deadline is older than this are historical noise
# (the 期限至 query surfaces last year's anniversary replies).
_MAX_VALID_AGE_DAYS = 30


def tweet_to_entry(data: dict, account: str) -> dict | None:
    """Convert syndication JSON to a cache entry, or None to skip.

    Guards: live Tweet (not tombstone), authored by the configured
    account, parseable code AND deadline (real code tweets always state
    one; this also rejects EN-format misparses and announcement roots),
    deadline not older than 30 days. _added is the tweet's publish date
    in Beijing time (preserves the 30-day time-limited heuristic).
    """
    if not isinstance(data, dict) or data.get("__typename") != "Tweet":
        return None
    user = data.get("user") or {}
    if (user.get("screen_name") or "").lower() != account.lower():
        return None
    crc = _crc()
    parsed = crc.parse_tweet_text(data.get("text") or "")
    code = parsed.get("code") or ""
    valid = parsed.get("valid") or ""
    if not code or not valid:
        return None
    expiry = crc._parse_date(valid)
    if expiry and expiry < datetime.now(timezone.utc) - timedelta(days=_MAX_VALID_AGE_DAYS):
        return None

    added = ""
    created = data.get("created_at") or ""
    try:
        added = (datetime.fromisoformat(created.replace("Z", "+00:00"))
                 .astimezone(crc.BEIJING).strftime("%Y-%m-%d"))
    except (ValueError, AttributeError):
        added = datetime.now(crc.BEIJING).strftime("%Y-%m-%d")

    content = parsed.get("content") or ""
    # parse_tweet_text strips the leading 《 as punctuation, leaving an
    # orphan 》 ("星隕計畫》3周年紀念序號①"). Drop unmatched closers.
    while content.count("》") > content.count("《"):
        content = content.replace("》", "", 1)

    return {
        "code": code,
        "content": content,
        "valid": valid,
        "_added": added,
        "_source": "x",
    }


# ── Seen-tweet state ──────────────────────────────────────────────

_SEEN_CAP = 2000


def _seen_file() -> str:
    return os.path.join(_crc()._DATA_DIR, "x_seen.json")


def _load_seen() -> dict:
    """Load seen-state; fresh default on missing/corrupt file."""
    default = {"version": 1, "ids": {}, "last_poll": 0, "fail_streak": 0}
    try:
        with open(_seen_file(), "r", encoding="utf-8") as f:
            state = json.load(f)
        if isinstance(state, dict) and isinstance(state.get("ids"), dict):
            return state
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    return default


def _save_seen(state: dict):
    """Atomic write with a bounded ID set (evict oldest first_seen)."""
    ids = state.get("ids", {})
    if len(ids) > _SEEN_CAP:
        ordered = sorted(ids.items(),
                         key=lambda kv: kv[1].get("first_seen", ""))
        state["ids"] = dict(ordered[-_SEEN_CAP:])
    try:
        os.makedirs(os.path.dirname(_seen_file()), exist_ok=True)
        tmp = _seen_file() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
        os.replace(tmp, _seen_file())
    except OSError:
        pass


# ── Orchestration ─────────────────────────────────────────────────

# Single-flight flag: check-and-set before the first await (safe on the
# bot's single event loop; avoids import-time asyncio.Lock loop binding).
_scrape_busy = False

_FETCH_BUDGET = 15      # max new tweets fetched per pass
_FETCH_SPACING = 0.5    # seconds between syndication fetches


async def scrape() -> list[dict] | None:
    """Discover → fetch unseen tweets → entries.

    Returns entries (possibly [] when nothing new), or None when
    discovery failed or another scrape is in flight. Never raises.
    Tweets are marked seen on successful FETCH regardless of parse
    outcome (announcement roots must not be re-fetched every pass);
    fetch failures stay unseen and retry next pass.
    """
    global _scrape_busy
    if _scrape_busy:
        return None
    _scrape_busy = True
    try:
        account = _account()
        ids = await discover_ids()
        state = _load_seen()
        if ids is None:
            state["fail_streak"] = state.get("fail_streak", 0) + 1
            state["last_poll"] = time.time()
            _save_seen(state)
            return None
        state["fail_streak"] = 0
        state["last_poll"] = time.time()

        new_ids = [i for i in ids if i not in state.get("ids", {})][:_FETCH_BUDGET]
        entries = []
        for idx, tid in enumerate(new_ids):
            if idx:
                await asyncio.sleep(_FETCH_SPACING)
            data = await fetch_tweet(tid)
            if data is None:
                continue  # not seen → retried next pass
            entry = tweet_to_entry(data, account)
            state.setdefault("ids", {})[tid] = {
                "first_seen": datetime.now(timezone.utc).isoformat(),
                "code": entry["code"] if entry else "",
            }
            if entry:
                entries.append(entry)
        _save_seen(state)
        print(f"[RedeemX] discovered {len(ids)} tweets, "
              f"{len(new_ids)} new fetched, {len(entries)} codes",
              file=sys.stderr)
        return entries
    except Exception as e:  # noqa: BLE001
        print(f"[RedeemX] scrape failed: {type(e).__name__}: {e}",
              file=sys.stderr)
        return None
    finally:
        _scrape_busy = False


# ── Panel helper (paste a tweet URL instead of its text) ──────────

_STATUS_URL_RE = re.compile(
    r"(?:x|twitter)\.com/[^/\s]+/status/(\d{5,25})", re.IGNORECASE)


async def parse_tweet_url(url: str) -> dict:
    """Fetch a tweet by URL and parse it. For the WebUI paste helper.

    Returns {"code","valid","content"} on success, {"error": "..."} on
    failure. No account guard here — the admin chose the URL explicitly.
    """
    m = _STATUS_URL_RE.search(url or "")
    if not m:
        return {"error": "无法识别推文链接（需 x.com/…/status/… 形式）"}
    data = await fetch_tweet(m.group(1))
    if data is None:
        return {"error": "抓取失败：请检查代理（REDEEM_CODE_PROXY）与网络"}
    if data.get("__typename") != "Tweet":
        return {"error": "推文不存在或已删除"}
    parsed = _crc().parse_tweet_text(data.get("text") or "")
    if not parsed.get("code"):
        return {"error": "未从推文中解析出兑换码，请改贴推文文本"}
    return parsed

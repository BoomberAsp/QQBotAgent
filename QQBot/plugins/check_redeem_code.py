"""
Redeem Code Plugin — Query and auto-update game redeem codes.

Supports:
  - /兑换码, /redeem-code (direct NoneBot commands, skip agent)
  - Natural language → agent → redeem_code tool
  - Scraper with Beijing-time yesterday 18:00 staleness check
  - Auto-cleanup: codes expired 7+ days are removed (panel entries exempt)
  - Manual maintenance via the WebUI 兑换码管理 page — panel-authored
    entries (_source="panel") are authoritative: the scraper never
    overwrites them and cleanup never removes them.

Data source:
  - https://ucngame.com/codes/ark-recode-redeem-codes/ — the only
    high-timeliness aggregator, but behind a Cloudflare JS challenge
    since ~2026-09; kept as a dormant auto-fallback in case it recovers.
    (gamevoyant / cofregamers were removed 2026-10: incomplete data.)

Sources unreachable directly from the server are fetched through the proxy
configured via REDEEM_CODE_PROXY (or HTTPS_PROXY) in QQBot/.env, e.g.:
  REDEEM_CODE_PROXY=http://127.0.0.1:1081

Expiry semantics: a `valid` date means UTC midnight of that day, i.e.
08:00 (UTC+8) — official announcements read "兌換期限至 2026-10-14
08:00(UTC+8)". Expiry dates in English prose ("Valid until September
30th, 2026") are extracted so filtering and cleanup work.
"""

import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

BEIJING = timezone(timedelta(hours=8))

# ── Paths ────────────────────────────────────────────────────────

_DATA_DIR = os.path.join(
    os.path.dirname(__file__), "..", "data", "redeem_code"
)
_CACHE_FILE = os.path.join(_DATA_DIR, "redeem_code.json")
# Alert state file: written by the bot when no valid time-limited codes
# remain and scraping failed; read/cleared by the WebUI 兑换码管理 page.
_ALERT_FILE = os.path.join(_DATA_DIR, "admin_alert.json")

# ── Scraper Config ────────────────────────────────────────────────

_REDEEM_CODE_URL = "https://ucngame.com/codes/ark-recode-redeem-codes/"
_CLEANUP_DAYS = 7  # Remove codes expired this many days ago


# ── Public API ────────────────────────────────────────────────────

def get_redeem_codes() -> list[dict]:
    """Return list of currently valid (non-expired) redeem codes.

    Each entry: {"code": str, "content": str, "valid": str}
    """
    entries = _load_cache()
    now = datetime.now(BEIJING)
    valid = []
    for entry in entries:
        expiry = entry.get("valid", "")
        if expiry and _parse_date(expiry) and _parse_date(expiry) < now:
            continue  # Expired
        valid.append(entry)
    # Newest first (by publish date, falling back to expiry date) so display
    # caps show recent codes rather than ancient leftovers.
    valid.sort(key=lambda e: e.get("_added", "") or e.get("valid", ""),
               reverse=True)
    return valid


# Codes first seen within this many days count as time-limited event codes
# even when the source provides no expiry date (event codes typically live
# 2–4 weeks; permanent codes eventually age out of this window).
_RECENT_DAYS = 30


def get_time_limited_codes(grace_days: int = _CLEANUP_DAYS) -> list[dict]:
    """Return time-limited codes.

    Time-limited = has a known expiry date (`valid`), OR was first published
    within the last _RECENT_DAYS days (fresh event codes whose expiry the
    source doesn't state). Includes codes expired within `grace_days`.

    Each returned entry gets a transient "recently_expired" bool key
    (not persisted to the cache). Sorted: non-expired first, newest first.
    """
    entries = _load_cache()
    now = datetime.now(BEIJING)
    grace_cutoff = now - timedelta(days=grace_days)
    recent_cutoff = (now - timedelta(days=_RECENT_DAYS)).strftime("%Y-%m-%d")
    out = []
    for entry in entries:
        expiry = entry.get("valid", "")
        parsed = _parse_date(expiry) if expiry else None
        if parsed:
            if parsed < grace_cutoff:
                continue  # expired beyond the grace window
            recently_expired = parsed < now
        else:
            # No expiry info → only count as time-limited if recently added
            added = entry.get("_added", "") or ""
            if not added or added < recent_cutoff:
                continue  # old / undated → long-term bucket
            recently_expired = False
        item = dict(entry)
        item["recently_expired"] = recently_expired
        out.append(item)
    out.sort(key=lambda e: e.get("valid", "") or e.get("_added", "") or "",
             reverse=True)
    out.sort(key=lambda e: e["recently_expired"])  # stable: active first
    return out


def get_long_term_codes() -> list[dict]:
    """Return long-term codes: no known expiry AND not recently published.

    This bucket mixes genuinely permanent codes with old unverified ones —
    sources stop tracking expiry for codes they no longer check.
    Sorted by publish date (_added), newest first.
    """
    entries = _load_cache()
    recent_cutoff = (
        datetime.now(BEIJING) - timedelta(days=_RECENT_DAYS)
    ).strftime("%Y-%m-%d")
    out = []
    for entry in entries:
        expiry = entry.get("valid", "")
        if expiry and _parse_date(expiry):
            continue  # has expiry info → time-limited bucket
        added = entry.get("_added", "") or ""
        if added and added >= recent_cutoff:
            continue  # fresh event code → time-limited bucket
        out.append(entry)
    out.sort(key=lambda e: e.get("_added", "") or "", reverse=True)
    return out


def get_cache_info() -> dict:
    """Return cache freshness metadata for user-facing staleness warnings.

    Keys: scraped_at (epoch seconds, 0 if unknown), scraped_at_iso, stale (bool).
    """
    scraped_at = 0
    iso = ""
    if os.path.exists(_CACHE_FILE):
        try:
            with open(_CACHE_FILE, "r", encoding="utf-8") as f:
                cache = json.load(f)
            scraped_at = cache.get("scraped_at", 0) or 0
            iso = cache.get("scraped_at_iso", "")
        except Exception:
            pass
    if scraped_at and not iso:
        iso = datetime.fromtimestamp(scraped_at, tz=BEIJING).isoformat()
    return {"scraped_at": scraped_at, "scraped_at_iso": iso, "stale": _is_stale()}


async def check_and_refresh() -> bool:
    """Check cache staleness and trigger background scrape if needed.

    Returns True if a refresh was triggered (not whether it succeeded).
    Call get_redeem_codes() afterwards to get the best available data.
    """
    if _is_stale():
        await _refresh()
        _cleanup_expired()
        return True
    return False


# ── Cache I/O ─────────────────────────────────────────────────────

def _load_cache() -> list[dict]:
    """Load the raw list of all redeem code entries from cache.

    On read, entries are normalized: missing `valid` dates are backfilled
    from English prose in `content` (e.g. "(Valid until September 30th,
    2026)"), and generic filler content is stripped. Normalization is
    idempotent and persisted on the next _save_cache.
    """
    if not os.path.exists(_CACHE_FILE):
        # Seed with empty cache
        return []
    try:
        with open(_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        entries = data.get("codes", [])
    except (json.JSONDecodeError, IOError):
        return []
    for entry in entries:
        content = entry.get("content", "") or ""
        if not entry.get("valid"):
            parsed = _parse_prose_date(content)
            if parsed:
                entry["valid"] = parsed
        if not entry.get("_added"):
            added = _parse_added_date(content)
            if added:
                entry["_added"] = added
        entry["content"] = _normalize_content(content)
    return entries


def _save_cache(codes: list[dict], scraped_at: float = None):
    """Persist redeem codes to the cache file."""
    os.makedirs(_DATA_DIR, exist_ok=True)
    if scraped_at is None:
        # Preserve existing scraped_at if not provided
        existing = {}
        if os.path.exists(_CACHE_FILE):
            try:
                with open(_CACHE_FILE, "r", encoding="utf-8") as f:
                    existing = json.load(f)
            except Exception:
                pass
        scraped_at = existing.get("scraped_at", time.time())

    cache = {
        "scraped_at": scraped_at,
        "scraped_at_iso": datetime.fromtimestamp(scraped_at, tz=BEIJING).isoformat(),
        "source_url": _REDEEM_CODE_URL,
        "codes": codes,
    }
    # Atomic write (tmp + os.replace) — the WebUI panel writes the same
    # file atomically; never leave a half-written cache behind.
    tmp = _CACHE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _CACHE_FILE)


# ── Staleness ─────────────────────────────────────────────────────

def _is_stale() -> bool:
    """Return True if cache is older than Beijing time yesterday 18:00."""
    if not os.path.exists(_CACHE_FILE):
        return True
    try:
        with open(_CACHE_FILE, "r", encoding="utf-8") as f:
            cache = json.load(f)
        scraped_at = cache.get("scraped_at", 0)
    except Exception:
        return True

    now = datetime.now(BEIJING)
    yesterday_18 = datetime.combine(
        now.date() - timedelta(days=1),
        datetime.min.time().replace(hour=18),
        tzinfo=BEIJING,
    )
    cache_time = datetime.fromtimestamp(scraped_at, tz=BEIJING)
    return cache_time < yesterday_18


# ── Scraper ───────────────────────────────────────────────────────

async def _refresh():
    """Scrape latest redeem codes and merge into cache."""
    print("[RedeemCode] Refreshing redeem codes...", file=sys.stderr)

    scraped = await _scrape()
    if not scraped:
        print("[RedeemCode] Scrape returned no results, keeping existing cache",
              file=sys.stderr)
        return

    # Merge: update existing entries, add new ones, preserve unmatched
    existing = _load_cache()
    existing_by_code = {e["code"]: e for e in existing}

    for entry in scraped:
        code = entry["code"]
        if code in existing_by_code:
            # Update from scraper, but never blank out known data
            # (sources differ in what fields they provide).
            ex = existing_by_code[code]
            # Panel-authored entries are authoritative (manually curated
            # from official announcements) — the scraper may refresh
            # _added (proof the code is still listed) but must not
            # overwrite content/valid/_source.
            is_panel = ex.get("_source") == "panel"
            if not is_panel:
                if entry.get("content"):
                    ex["content"] = entry["content"]
                if entry.get("valid"):
                    ex["valid"] = entry["valid"]
                ex["_source"] = entry.get("_source", "scraped")
            if (entry.get("_added") or "") > (ex.get("_added") or ""):
                ex["_added"] = entry["_added"]
        else:
            existing_by_code[code] = entry

    merged = list(existing_by_code.values())
    _save_cache(merged, scraped_at=time.time())

    print(f"[RedeemCode] Refresh complete: {len(scraped)} scraped, "
          f"{len(merged)} total in cache", file=sys.stderr)


def _get_proxy_url() -> str:
    """Proxy for sources that are unreachable directly from the server.

    Configured via REDEEM_CODE_PROXY (preferred) or HTTPS_PROXY in .env,
    e.g. REDEEM_CODE_PROXY=http://127.0.0.1:1081
    """
    return (os.environ.get("REDEEM_CODE_PROXY", "")
            or os.environ.get("HTTPS_PROXY", "")
            or os.environ.get("https_proxy", "")).strip()


async def _fetch_url(url: str, use_proxy: bool = False) -> str | None:
    """Fetch a URL, optionally through the configured proxy.

    Returns HTML text, or None on failure/skip.
    """
    proxy_url = _get_proxy_url() if use_proxy else ""
    if use_proxy and not proxy_url:
        print(f"[RedeemCode] No proxy configured (REDEEM_CODE_PROXY), skipping {url}",
              file=sys.stderr)
        return None

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
                      " AppleWebKit/537.36 (KHTML, like Gecko)"
                      " Chrome/131.0.0.0 Safari/537.36",
    }
    try:
        from curl_cffi import requests as cffi_requests
    except ImportError:
        cffi_requests = None

    if cffi_requests is not None:
        loop = asyncio.get_running_loop()

        def _run():
            try:
                kw = {}
                if proxy_url:
                    kw["proxies"] = {"http": proxy_url, "https": proxy_url}
                resp = cffi_requests.get(
                    url, impersonate="chrome131", timeout=30, headers=headers, **kw,
                )
                if resp.status_code != 200:
                    print(f"[RedeemCode] HTTP {resp.status_code} for {url}",
                          file=sys.stderr)
                    return None
                return resp.text
            except Exception as e:
                print(f"[RedeemCode] HTTP error for {url}: {type(e).__name__}: {e}",
                      file=sys.stderr)
                return None

        return await loop.run_in_executor(None, _run)

    # httpx fallback
    print("[RedeemCode] curl_cffi not available, trying httpx", file=sys.stderr)
    try:
        import httpx
        async with httpx.AsyncClient(
            timeout=30, proxy=proxy_url or None,
        ) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code != 200:
                print(f"[RedeemCode] HTTP {resp.status_code} for {url}",
                      file=sys.stderr)
                return None
            return resp.text
    except Exception as e:
        print(f"[RedeemCode] HTTP error for {url}: {type(e).__name__}: {e}",
              file=sys.stderr)
        return None


# ── Source registry ───────────────────────────────────────────────
# ucngame.com is behind a Cloudflare JS challenge since ~2026-09 and is
# kept only as a dormant auto-fallback; the primary maintenance channel
# is the WebUI 兑换码管理 page (panel entries). gamevoyant/cofregamers
# were removed 2026-10 (incomplete, low-quality data).

_SOURCES = [
    {
        "name": "ucngame",
        "url": _REDEEM_CODE_URL,
        "proxy": False,
    },
]


async def _scrape() -> list[dict] | None:
    """Scrape redeem codes from all reachable sources and merge.

    Returns list of {"code", "content", "valid", "_source"} dicts,
    or None if every source failed.
    """
    all_entries = []
    ok_any = False

    for src in _SOURCES:
        html = await _fetch_url(src["url"], use_proxy=src["proxy"])
        if html is None:
            continue
        ok_any = True
        entries = _parse_html(html)
        for e in entries:
            e["_source"] = src["name"]
        print(f"[RedeemCode] {src['name']}: parsed {len(entries)} codes",
              file=sys.stderr)
        all_entries.extend(entries)

    if not ok_any:
        return None
    return _dedupe_entries(all_entries)


def _dedupe_entries(entries: list[dict]) -> list[dict]:
    """Dedupe scraped entries by code, merging the best info per field.

    - valid/content: first non-empty wins
    - _added: newest wins (semantics: most recent date any source listed
      or confirmed the code, so an active listing on one source outweighs
      an old publish date on another)
    """
    by_code = {}
    for entry in entries:
        code = entry["code"]
        if code not in by_code:
            by_code[code] = entry
            continue
        ex = by_code[code]
        if not ex.get("valid") and entry.get("valid"):
            ex["valid"] = entry["valid"]
        if not ex.get("content") and entry.get("content"):
            ex["content"] = entry["content"]
        if (entry.get("_added") or "") > (ex.get("_added") or ""):
            ex["_added"] = entry["_added"]
    return list(by_code.values())


def _fullwidth_to_ascii(text: str) -> str:
    """Normalize fullwidth ASCII characters (Ｍ → M) used as anti-scrape noise."""
    return "".join(
        chr(ord(c) - 0xFEE0) if 0xFF01 <= ord(c) <= 0xFF5E else c
        for c in text
    )


def _parse_html(html: str) -> list[dict]:
    """Extract redeem codes from HTML table.

    Expected table structure: CODE | REWARDS (with optional expiry dates).
    Uses regex-based extraction to be tolerant of HTML variations.
    """
    codes = []

    # Strategy: find table rows containing code-like patterns
    # Code format: typically alphanumeric, 8-20 chars
    code_pattern = re.compile(r'\b([A-Za-z0-9]{8,20})\b')

    # Find all table rows
    rows = re.findall(r'<tr[^>]*>(.*?)</tr>', html, re.DOTALL | re.IGNORECASE)

    for row in rows:
        cells = re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', row, re.DOTALL | re.IGNORECASE)
        if len(cells) < 2:
            continue

        # Clean HTML tags from cells
        clean_cells = [re.sub(r'<[^>]+>', '', c).strip() for c in cells]

        # Find the code cell and content cell
        code = None
        content = ""
        valid = ""

        for cell in clean_cells:
            match = code_pattern.fullmatch(cell)
            if match and not cell.startswith("http"):
                code = match.group(1)
            elif re.search(r'\d{4}[-/]\d{2}[-/]\d{2}', cell):
                # Date-like pattern — treat as expiry
                date_match = re.search(r'(\d{4}[-/]\d{2}[-/]\d{2})', cell)
                if date_match:
                    valid = date_match.group(1).replace("/", "-")
            elif len(cell) > 3 and code:
                content = cell

        # If we didn't find a code in the row, try extracting from first cell
        if not code and clean_cells:
            first = clean_cells[0]
            match = code_pattern.search(first)
            if match and not first.startswith("http"):
                code = match.group(1)
                # Rest of first cell is content
                content = first[match.end():].strip()
                if len(clean_cells) > 1:
                    content = content or clean_cells[1]

        if code and len(code) >= 8:
            # Expiry often lives only in prose inside content — extract it
            if not valid:
                valid = _parse_prose_date(content)
            codes.append({
                "code": code,
                "content": _normalize_content(content),
                "valid": valid,
            })

    if not codes:
        # Try alternative: find code-like strings near "CODE" or "code" headers
        text = re.sub(r'<[^>]+>', ' ', html)
        text = re.sub(r'\s+', ' ', text)
        # Look for patterns like "CODE: ABC123 - Rewards description"
        for match in re.finditer(
            r'(?:CODE|Code|code)\s*[:：]\s*([A-Za-z0-9]{8,20})\s*[-–—]\s*([^<\n]{3,100})',
            text
        ):
            raw_content = match.group(2).strip()
            codes.append({
                "code": match.group(1),
                "content": _normalize_content(raw_content),
                "valid": _parse_prose_date(raw_content),
            })

    print(f"[RedeemCode] Parsed {len(codes)} codes from HTML", file=sys.stderr)
    return codes


# ── Cleanup ───────────────────────────────────────────────────────

def _cleanup_expired():
    """Remove codes that expired more than _CLEANUP_DAYS days ago.

    Panel-authored entries (_source="panel") are exempt — the admin
    decides when to delete them via the WebUI.
    """
    entries = _load_cache()
    cutoff = datetime.now(BEIJING) - timedelta(days=_CLEANUP_DAYS)
    kept = []
    removed = 0

    for entry in entries:
        expiry = entry.get("valid", "")
        if expiry and entry.get("_source") != "panel":
            parsed = _parse_date(expiry)
            if parsed and parsed < cutoff:
                removed += 1
                continue
        kept.append(entry)

    if removed > 0:
        _save_cache(kept)
        print(f"[RedeemCode] Cleaned up {removed} expired codes (>{_CLEANUP_DAYS} days)",
              file=sys.stderr)


def _parse_date(date_str: str) -> datetime | None:
    """Parse a `valid` date (YYYY-MM-DD) into an expiry datetime.

    Official semantics: codes expire at UTC midnight of the given date,
    i.e. 08:00 (UTC+8) that morning — announcements read
    "兌換期限至 2026-10-14 08:00(UTC+8)". Returns None on failure.
    """
    if not date_str or not date_str.strip():
        return None
    try:
        return datetime.strptime(
            date_str.strip()[:10], "%Y-%m-%d"
        ).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def format_expiry_display(date_str: str) -> str:
    """Format a `valid` date for user display: '2026-10-14 08:00 (UTC+8)'.

    Expiry is UTC midnight = 08:00 Beijing time on that date.
    Returns '' for empty/unparseable input.
    """
    if not date_str or not date_str.strip():
        return ""
    if _parse_date(date_str) is None:
        return date_str.strip()[:10]
    return f"{date_str.strip()[:10]} 08:00 (UTC+8)"


# ── English prose date extraction ────────────────────────────────
# The scraper source writes expiry as prose inside the content cell,
# e.g. "(Valid until September 30th, 2026)". Extract it so expiry
# filtering and cleanup actually work.

_MONTHS = {
    "january": 1, "jan": 1,
    "february": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}

_PROSE_EXPIRY_RE = re.compile(
    r'(?:valid\s+(?:until|through|till)|expires?|expiration|until)\s*:?\s*'
    r'([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s*(\d{4})',
    re.IGNORECASE,
)

_ADDED_ON_RE = re.compile(
    r'added\s+on\s*([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s*(\d{4})',
    re.IGNORECASE,
)

# Filler content that carries no reward information (source boilerplate).
_GENERIC_CONTENT_RE = re.compile(
    r'^(?:redeem this gift code for exclusive rewards'
    r'|activate this code to access free rewards instantly)[!.]*$',
    re.IGNORECASE,
)

# "Redeem this gift code for <real reward>" — prefix to unwrap
_GIFT_PREFIX_RE = re.compile(
    r'^redeem this gift code for\s+(.+?)[!.]*$', re.IGNORECASE,
)
_EXCLUSIVE_REWARDS_RE = re.compile(r'^exclusive rewards$', re.IGNORECASE)
_ACTIVATE_BOILER_RE = re.compile(
    r'^activate this code to access free rewards instantly$', re.IGNORECASE,
)

# Parentheticals to strip from content: (Valid until ...), (Added on ...), (New)
_CONTENT_PAREN_RE = re.compile(
    r'\(\s*(?:valid\s+until|expires?|added\s+on|new)[^)]*\)',
    re.IGNORECASE,
)


def _parse_prose_date(text: str) -> str:
    """Extract an expiry date from English prose. Returns 'YYYY-MM-DD' or ''.

    Only matches expiry phrasing ("valid until", "expires", "until");
    "Added on <date>" is a publish date, NOT an expiry, and is ignored.
    """
    if not text:
        return ""
    match = _PROSE_EXPIRY_RE.search(text)
    if not match:
        return ""
    return _month_day_year_to_iso(match.group(1), match.group(2), match.group(3))


def _parse_added_date(text: str) -> str:
    """Extract the 'Added on <date>' publish date. Returns 'YYYY-MM-DD' or ''."""
    if not text:
        return ""
    match = _ADDED_ON_RE.search(text)
    if not match:
        return ""
    return _month_day_year_to_iso(match.group(1), match.group(2), match.group(3))


def _month_day_year_to_iso(month_name: str, day: str, year: str) -> str:
    month = _MONTHS.get(month_name.lower())
    if not month:
        return ""
    try:
        return datetime(int(year), month, int(day),
                        tzinfo=BEIJING).strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _normalize_content(content: str) -> str:
    """Strip date parentheticals and source boilerplate from content.

    Unwraps "Redeem this gift code for Recruit Contract x10" → "Recruit
    Contract x10". Returns '' when nothing informative is left.
    """
    if not content:
        return ""
    cleaned = _CONTENT_PAREN_RE.sub("", content).strip()
    if not cleaned:
        return ""
    if _GENERIC_CONTENT_RE.match(cleaned):
        return ""
    match = _GIFT_PREFIX_RE.match(cleaned)
    if match:
        reward = match.group(1).strip(" .!")
        if not reward or _EXCLUSIVE_REWARDS_RE.match(reward):
            return ""
        return reward
    if _ACTIVATE_BOILER_RE.match(cleaned):
        return ""
    return cleaned


# ── Official announcement (tweet) parsing ────────────────────────
# Used by the WebUI 兑换码管理 page's paste-to-parse helper. Official
# posts on X (@Arkrecode_ZH) look like:
#   兌換期限至 2026-10-14 08:00(UTC+8)
#   🏁 極速的西爾維納登場紀念序號：Ark99u5XgYsp
# Parsing is line-based so the deadline line never leaks into content.

_TWEET_DEADLINE_RE = re.compile(
    r'(?:期限至|期限到|有效期[至到]?)\s*[:：]?\s*'
    r'(\d{4})[-/年.](\d{1,2})[-/月.](\d{1,2})'
)
_TWEET_CODE_LABEL_RE = re.compile(
    r'(?:序號|序号|兌換碼|兑换码|禮品碼|礼品码|禮包碼|礼包码)\s*[：:]\s*'
    r'([A-Za-z0-9]{6,30})'
)
# Fallback when no label: first token that isn't pure digits
_TWEET_CODE_FALLBACK_RE = re.compile(r'\b(?!\d+\b)[A-Za-z0-9]{8,20}\b')
_TWEET_TRAIL_PUNCT_RE = re.compile(r'[\s，,。.、:：!！?？~～·…]+$')


def parse_tweet_text(text: str) -> dict:
    """Parse official announcement text into {"code", "valid", "content"}.

    Tolerant of fullwidth obfuscation, traditional/simplified variants and
    missing fields — unmatched fields come back as empty strings. Never
    raises.
    """
    result = {"code": "", "valid": "", "content": ""}
    if not text or not text.strip():
        return result
    try:
        text = _fullwidth_to_ascii(text)

        m = _TWEET_DEADLINE_RE.search(text)
        if m:
            try:
                result["valid"] = datetime(
                    int(m.group(1)), int(m.group(2)), int(m.group(3)),
                ).strftime("%Y-%m-%d")
            except ValueError:
                pass

        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        code_prefix = ""
        for ln in lines:
            m = _TWEET_CODE_LABEL_RE.search(ln)
            if m:
                result["code"] = m.group(1)
                code_prefix = ln[:m.start()]
                break
        if not result["code"]:
            stripped = re.sub(r'https?://\S+', ' ', text)
            for ln in (l.strip() for l in stripped.splitlines()):
                m = _TWEET_CODE_FALLBACK_RE.search(ln)
                if m:
                    result["code"] = m.group(0)
                    code_prefix = ln[:m.start()]
                    break

        # Content = text preceding the code on its own line, minus
        # leading emoji/punctuation, trailing punctuation and any
        # deadline fragment.
        raw = _TWEET_DEADLINE_RE.sub("", code_prefix)
        raw = re.sub(r'^[^\w]+', '', raw.strip())
        raw = _TWEET_TRAIL_PUNCT_RE.sub('', raw)
        result["content"] = raw
    except Exception:
        pass
    return result


# ── Admin alert (bot → WebUI panel) ──────────────────────────────

def _write_admin_alert(reason: str):
    """Record an alert for the WebUI 兑换码管理 page banner.

    Written when /兑换码 finds no valid time-limited codes AND the
    scraper failed — the admin should add codes manually. The panel
    clears the file when codes are saved (or via the dismiss button).
    """
    os.makedirs(_DATA_DIR, exist_ok=True)
    payload = {
        "reason": reason,
        "triggered_at": datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M:%S"),
    }
    tmp = _ALERT_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, _ALERT_FILE)
    except OSError:
        pass


def _clear_admin_alert():
    """Remove the alert state file (no-op if absent)."""
    try:
        if os.path.exists(_ALERT_FILE):
            os.remove(_ALERT_FILE)
    except OSError:
        pass

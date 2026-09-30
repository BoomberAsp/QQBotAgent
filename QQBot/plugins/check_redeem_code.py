"""
Redeem Code Plugin — Query and auto-update game redeem codes.

Supports:
  - /兑换码, /redeem-code (direct NoneBot commands, skip agent)
  - Natural language → agent → redeem_code tool
  - Scraper with Beijing-time yesterday 18:00 staleness check
  - Auto-cleanup: codes expired 7+ days are removed
  - Manual maintenance: add codes directly to the JSON file

Data sources (tried in order, results merged):
  - https://gamevoyant.com/codes/ark-recode-redeem-codes   (direct, JS data)
  - https://cofregamers.com/en/ark-recode-redeem-code-list/ (needs proxy)
  - https://ucngame.com/codes/ark-recode-redeem-codes/     (Cloudflare-blocked
    since ~2026-09, kept as fallback)

Sources unreachable directly from the server are fetched through the proxy
configured via REDEEM_CODE_PROXY (or HTTPS_PROXY) in QQBot/.env, e.g.:
  REDEEM_CODE_PROXY=http://127.0.0.1:1081

Expiry dates are extracted from English prose in reward text
("Valid until September 30th, 2026") so filtering and cleanup work.
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
    with open(_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


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
            # (sources differ in what fields they provide)
            ex = existing_by_code[code]
            if entry.get("content"):
                ex["content"] = entry["content"]
            if entry.get("valid"):
                ex["valid"] = entry["valid"]
            if entry.get("_added"):
                ex["_added"] = entry["_added"]
            ex["_source"] = entry.get("_source", "scraped")
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
# kept only as a fallback. gamevoyant works directly from the server;
# cofregamers requires the proxy.

_SOURCES = [
    {
        "name": "gamevoyant",
        "url": "https://gamevoyant.com/codes/ark-recode-redeem-codes",
        "proxy": False,
    },
    {
        "name": "cofregamers",
        "url": "https://cofregamers.com/en/ark-recode-redeem-code-list/",
        "proxy": True,
    },
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
        if src["name"] == "gamevoyant":
            entries = _parse_gamevoyant(html)
        elif src["name"] == "cofregamers":
            entries = _parse_cofregamers(html)
        else:
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
    """Dedupe scraped entries by code, preferring ones with expiry info."""
    by_code = {}
    for entry in entries:
        code = entry["code"]
        if code not in by_code:
            by_code[code] = entry
        elif not by_code[code].get("valid") and entry.get("valid"):
            by_code[code] = entry
    return list(by_code.values())


def _fullwidth_to_ascii(text: str) -> str:
    """Normalize fullwidth ASCII characters (Ｍ → M) used as anti-scrape noise."""
    return "".join(
        chr(ord(c) - 0xFEE0) if 0xFF01 <= ord(c) <= 0xFF5E else c
        for c in text
    )


_GV_ENTRY_RE = re.compile(
    r'\{code:"([^"]+)",reward:"([^"]*)",status:"([^"]*)"\}'
)

_BARE_DATE_RE = re.compile(
    r'([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s*(\d{4})'
)


def _parse_bare_date(text: str) -> str:
    """Parse a bare 'Month Day, Year' date. Returns 'YYYY-MM-DD' or ''."""
    if not text:
        return ""
    match = _BARE_DATE_RE.search(text)
    if not match:
        return ""
    month = _MONTHS.get(match.group(1).lower())
    if not month:
        return ""
    try:
        return datetime(int(match.group(3)), month, int(match.group(2)),
                        tzinfo=BEIJING).strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _parse_gamevoyant(html: str) -> list[dict]:
    """Parse gamevoyant.com's embedded JS data.

    Format: $R[n]={code:"...",reward:"...(Valid until September 30th, 2026)",
                   status:"expired" | "expires:september 30, 2026" | ...}
    """
    codes = []
    yesterday = (datetime.now(BEIJING) - timedelta(days=1)).strftime("%Y-%m-%d")

    for match in _GV_ENTRY_RE.finditer(html):
        code = _fullwidth_to_ascii(match.group(1)).strip()
        reward = _fullwidth_to_ascii(match.group(2))
        status = match.group(3).strip().lower()

        if not code or len(code) < 6:
            continue

        # Expiry: from status ("expires:<date>") or from prose in reward text
        valid = ""
        if status.startswith("expires:"):
            valid = _parse_bare_date(status[len("expires:"):])
        if not valid:
            valid = _parse_prose_date(reward)
        if status == "expired" and not valid:
            # Source says expired but gave no date — mark with yesterday so
            # display filtering and 7-day cleanup both work.
            valid = yesterday

        codes.append({
            "code": code,
            "content": _normalize_content(reward),
            "valid": valid,
            "_added": _parse_added_date(reward),
        })

    return codes


_CG_ROW_RE = re.compile(r'<tr[^>]*>(.*?)</tr>', re.DOTALL | re.IGNORECASE)
_CG_CODE_RE = re.compile(r'data-codigo="([^"]+)"')
_CG_REWARD_RE = re.compile(
    r'<span class="recompensa-texto">(.*?)</span>', re.DOTALL | re.IGNORECASE,
)


def _parse_cofregamers(html: str) -> list[dict]:
    """Parse cofregamers.com's active-code HTML table.

    Rows: <td class="codigo-cell">...data-codigo="CODE"... +
          <span class="recompensa-texto">reward</span>
    The page lists active codes only; no per-code expiry is given.
    """
    # Page-level "Updated: <date>" applies to all listed codes
    updated = ""
    updated_match = re.search(
        r'<span class="fecha-valor">\s*([^<]+?)\s*</span>', html, re.IGNORECASE,
    )
    if updated_match:
        updated = _parse_bare_date(updated_match.group(1))

    codes = []
    seen = set()
    for row in _CG_ROW_RE.findall(html):
        code_match = _CG_CODE_RE.search(row)
        if not code_match:
            continue
        code = _fullwidth_to_ascii(code_match.group(1)).strip()
        # Keep only plausible ASCII codes
        if not re.fullmatch(r'[A-Za-z0-9]{6,24}', code) or code in seen:
            continue
        seen.add(code)
        content = ""
        reward_match = _CG_REWARD_RE.search(row)
        if reward_match:
            reward = re.sub(r'<[^>]+>', '', reward_match.group(1)).strip()
            content = _normalize_content(_fullwidth_to_ascii(reward))
        codes.append({
            "code": code,
            "content": content,
            "valid": "",  # unknown — page only lists currently-active codes
            "_added": updated,
        })
    return codes


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
    """Remove codes that expired more than _CLEANUP_DAYS days ago."""
    entries = _load_cache()
    cutoff = datetime.now(BEIJING) - timedelta(days=_CLEANUP_DAYS)
    kept = []
    removed = 0

    for entry in entries:
        expiry = entry.get("valid", "")
        if expiry:
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
    """Parse a date string (YYYY-MM-DD) into a datetime. Returns None on failure."""
    if not date_str or not date_str.strip():
        return None
    try:
        return datetime.strptime(
            date_str.strip()[:10], "%Y-%m-%d"
        ).replace(tzinfo=BEIJING)
    except ValueError:
        return None


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
    """Strip date parentheticals and ucngame boilerplate from content.

    Returns '' when nothing informative is left.
    """
    if not content:
        return ""
    cleaned = _CONTENT_PAREN_RE.sub("", content).strip()
    if not cleaned or _GENERIC_CONTENT_RE.match(cleaned):
        return ""
    return cleaned

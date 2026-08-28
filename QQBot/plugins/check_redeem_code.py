"""
Redeem Code Plugin — Query and auto-update game redeem codes.

Supports:
  - /兑换码, /redeem-code (direct NoneBot commands, skip agent)
  - Natural language → agent → redeem_code tool
  - Scraper with Beijing-time yesterday 18:00 staleness check
  - Auto-cleanup: codes expired 7+ days are removed
  - Manual maintenance: add codes directly to the JSON file

Data source: https://ucngame.com/codes/ark-recode-redeem-codes/
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
    return valid


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
    """Load the raw list of all redeem code entries from cache."""
    if not os.path.exists(_CACHE_FILE):
        # Seed with empty cache
        return []
    try:
        with open(_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("codes", [])
    except (json.JSONDecodeError, IOError):
        return []


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
            # Update content/valid from scraper (more authoritative)
            existing_by_code[code]["content"] = entry.get("content", "")
            existing_by_code[code]["valid"] = entry.get("valid", "")
            existing_by_code[code]["_source"] = "scraped"
        else:
            entry["_source"] = "scraped"
            existing_by_code[code] = entry

    merged = list(existing_by_code.values())
    _save_cache(merged, scraped_at=time.time())

    print(f"[RedeemCode] Refresh complete: {len(scraped)} scraped, "
          f"{len(merged)} total in cache", file=sys.stderr)


async def _scrape() -> list[dict] | None:
    """Scrape redeem codes from ucngame.com.

    Returns list of {"code", "content", "valid"} dicts, or None on failure.
    """
    try:
        from curl_cffi import requests as cffi_requests
    except ImportError:
        print("[RedeemCode] curl_cffi not available, trying httpx", file=sys.stderr)
        try:
            import httpx
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(_REDEEM_CODE_URL, headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
                                  " AppleWebKit/537.36 (KHTML, like Gecko)"
                                  " Chrome/116.0.0.0 Safari/537.36",
                })
                html = resp.text
        except Exception as e:
            print(f"[RedeemCode] HTTP error: {type(e).__name__}: {e}", file=sys.stderr)
            return None
    else:
        loop = asyncio.get_running_loop()

        def _run():
            try:
                resp = cffi_requests.get(
                    _REDEEM_CODE_URL,
                    impersonate="chrome116",
                    timeout=30,
                    headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
                                      " AppleWebKit/537.36 (KHTML, like Gecko)"
                                      " Chrome/116.0.0.0 Safari/537.36",
                    },
                )
                if resp.status_code != 200:
                    print(f"[RedeemCode] HTTP {resp.status_code}", file=sys.stderr)
                    return None
                return resp.text
            except Exception as e:
                print(f"[RedeemCode] HTTP error: {type(e).__name__}: {e}",
                      file=sys.stderr)
                return None

        html = await loop.run_in_executor(None, _run)
        if html is None:
            return None

    return _parse_html(html)


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
            codes.append({
                "code": code,
                "content": content,
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
            codes.append({
                "code": match.group(1),
                "content": match.group(2).strip(),
                "valid": "",
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

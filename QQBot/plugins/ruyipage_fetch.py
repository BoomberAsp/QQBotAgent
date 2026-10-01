#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Standalone ruyiPage fetcher — pierce Cloudflare JS challenges.

Invoked as a subprocess by ``check_redeem_code._fetch_url_browser`` (and
runnable by hand) under ``xvfb-run``. ruyiPage drives a fingerprinted
Firefox via WebDriver BiDi; it auto-solves ucngame's Cloudflare JS
challenge with zero interaction (~8s from both residential and Tencent
Cloud datacenter IPs, verified 2026-10).

Why a subprocess: the bot runs under ``nb run`` with no X display, and a
browser must not share the bot's process/venv. Isolating it here means a
Firefox crash can never take down the bot, and ruyiPage's heavy deps stay
out of the production venv (point REDEEM_CODE_BROWSER_PYTHON at a
dedicated venv — see scripts/install_redeem_browser.sh).

Usage:
    xvfb-run -a <python> ruyipage_fetch.py <url> <out_file> [max_wait_s]

Writes the final rendered HTML to ``<out_file>`` (a file, not stdout, so
browser noise can't contaminate it). Diagnostics go to stderr.

Exit codes:
    0  success — Cloudflare cleared and real content captured
    2  bad usage / missing argument
    3  fetched but still on a Cloudflare challenge / no real content
    4  ruyiPage import or Firefox launch failure
    5  unrecoverable error during fetch
"""
import re
import sys
import time

# Cloudflare interstitial markers. The managed challenge page title is
# "Just a moment..."; these strings appear in the challenge HTML.
_CF_MARKERS = (
    "cf-mitigated", "challenge-platform", "just a moment",
    "checking your browser", "cf_chl", "turnstile",
    "cf-browser-verification", "attention required",
    "cf-challenge", "verify you are human", "verify you are a human",
    "cloudflare to restrict access", "please turn javascript on",
    "enable javascript and cookies",
)

# A page this small after CF clears is almost certainly a not-yet-rendered
# shell (ucngame's real page is ~210KB); keep waiting for lazy content.
_MIN_CONTENT = 20000


def _cf_present(html: str) -> bool:
    low = (html or "").lower()
    return any(m in low for m in _CF_MARKERS)


def _log(msg: str) -> None:
    print(f"[ruyipage_fetch] {msg}", file=sys.stderr, flush=True)


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        _log("usage: ruyipage_fetch.py <url> <out_file> [max_wait_s]")
        return 2
    url = argv[1]
    out_file = argv[2]
    try:
        max_wait = int(argv[3]) if len(argv) > 3 else 60
    except ValueError:
        max_wait = 60

    try:
        import ruyipage
        from ruyipage import FirefoxPage, FirefoxOptions, resolve_firefox_path
    except Exception as e:  # noqa: BLE001
        _log(f"ruyiPage import failed: {type(e).__name__}: {e}")
        return 4

    browser = None
    try:
        # Resolution order: RUYIPAGE_FIREFOX_EXECUTABLE_PATH env →
        # managed runtime (python -m ruyipage install) → system firefox.
        browser = resolve_firefox_path()
    except Exception as e:  # noqa: BLE001
        _log(f"resolve_firefox_path raised: {type(e).__name__}: {e}")
    if not browser:
        _log("no Firefox runtime found — run scripts/install_redeem_browser.sh "
             "or set RUYIPAGE_FIREFOX_EXECUTABLE_PATH")
        return 4
    _log(f"ruyiPage {getattr(ruyipage, '__version__', '?')} | firefox={browser}")
    _log(f"target={url}")

    opts = FirefoxOptions()
    try:
        opts.set_browser_path(browser)
        opts.enable_marionette(False)          # BiDi, not marionette
        opts.set_window_size(1920, 1080)
        opts.set_timeouts(page_load=60000, script=30000)
        # Disable HTTP/3 (QUIC). On some networks Firefox's HTTP/3 negotiation
        # fails with NS_ERROR_NET_HTTP3_PROTOCOL_ERROR and the page never loads
        # (stuck on "Problem loading page", ~1.3KB). Cloudflare does not require
        # HTTP/3, so forcing HTTP/2 fallback makes the fetch reliable.
        opts.set_pref("network.http.http3.enabled", False)
        # NOTE: smart_fingerprint() intentionally skipped — it needs the
        # `requests` package and a matching geo (fails on CN servers with
        # CountryMismatchError), and CF is bypassed fine without it.
        # headless left OFF; xvfb-run provides the display.
    except Exception as e:  # noqa: BLE001
        _log(f"option setup warning: {type(e).__name__}: {e}")

    page = None
    html = ""
    try:
        page = FirefoxPage(opts)
    except Exception as e:  # noqa: BLE001
        _log(f"Firefox launch failed: {type(e).__name__}: {e}")
        return 4

    try:
        t0 = time.time()
        try:
            page.get(url, timeout=max_wait)
        except Exception as e:  # noqa: BLE001
            _log(f"page.get raised (continuing with partial load): "
                 f"{type(e).__name__}: {e}")

        # Poll: first wait for CF to clear, then for lazily-rendered content
        # to stabilize (ucngame populates its code table via WordPress
        # shortcodes a few seconds AFTER the challenge clears; grabbing too
        # early yields only the ~35KB shell with zero codes).
        stable = 0
        last_len = -1
        deadline = t0 + max_wait + 30
        i = 0
        while time.time() < deadline:
            try:
                html = page.html or ""
            except Exception as e:  # noqa: BLE001
                # Transient during navigation: document.documentElement null.
                html = ""
                _log(f"[{i}] page.html raised: {type(e).__name__}: {e}")
            cur = len(html)
            cf = _cf_present(html)
            has_table = ("<table" in html.lower()) or ("data-code" in html.lower())
            try:
                title = page.title
            except Exception:  # noqa: BLE001
                title = "?"
            _log(f"[{i:2d}] len={cur:6d} cf={cf!s:5} table={has_table!s:5} "
                 f"title={str(title)[:50]!r}")
            if (not cf) and cur > _MIN_CONTENT:
                if cur == last_len:
                    stable += 1
                else:
                    stable = 0
                # Stop once content stopped growing and the table is present,
                # or after enough stable reads even without a table marker.
                if (stable >= 3 and has_table) or stable >= 6:
                    _log(f"content stabilized at {cur} bytes "
                         f"(table={has_table})")
                    break
            else:
                stable = 0
            last_len = cur
            i += 1
            time.sleep(2)

        elapsed = time.time() - t0
        try:
            with open(out_file, "w", encoding="utf-8") as f:
                f.write(html)
        except OSError as e:
            _log(f"failed to write {out_file}: {e}")
            return 5

        cf_final = _cf_present(html)
        ok = (not cf_final) and len(html) > _MIN_CONTENT
        codes = len(re.findall(r'\bArk[A-Za-z0-9]{6,14}\b', html))
        _log(f"RESULT elapsed={elapsed:.1f}s len={len(html)} "
             f"cf={cf_final} ark_tokens={codes} ok={ok} -> {out_file}")
        return 0 if ok else 3
    except Exception as e:  # noqa: BLE001
        _log(f"unrecoverable error: {type(e).__name__}: {e}")
        return 5
    finally:
        if page is not None:
            try:
                page.quit(timeout=5, force=True)
            except Exception:  # noqa: BLE001
                pass


if __name__ == "__main__":
    sys.exit(main(sys.argv))

"""Shared headless-browser fetch runner (ruyiPage under xvfb-run).

Both the redeem-code scraper and the ``web_fetch`` tool need to pierce
Cloudflare JS challenges that plain HTTP clients (curl_cffi/httpx) cannot.
This module centralises the pieces they share:

  * engine gating     — is the opt-in env flag set to ``ruyipage``?
  * interpreter resolution — the dedicated ruyiPage venv
  * a single process-wide concurrency cap — so redeem + web_fetch can never
    together spawn an unbounded number of Firefox instances
  * the subprocess runner itself (``ruyipage_fetch.py`` under ``xvfb-run``)

Isolation: ruyiPage lives in a DEDICATED venv and runs as a subprocess under
``xvfb-run -a``, so a browser crash can never take down the bot and ruyiPage's
heavy deps stay out of the production venv.

Callers pass their own ``enable_flag_env`` (``REDEEM_CODE_BROWSER`` for the
scraper, ``WEB_FETCH_BROWSER`` for web_fetch) so each feature is gated
independently, but they share ``resolve_python()`` and the semaphore.
"""
import asyncio
import os
import shutil
import sys
import tempfile

# The fetcher script lives beside the plugins; this module lives in QQBot/lib.
_BROWSER_FETCHER = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "plugins", "ruyipage_fetch.py")
)

# Lazily-created process-wide semaphore (see _sem()).
_SEM: asyncio.Semaphore | None = None


def engine_enabled(flag_env: str) -> bool:
    """True when the given env flag is opted in to the ruyiPage engine."""
    return os.environ.get(flag_env, "").strip().lower() == "ruyipage"


def resolve_python() -> str:
    """Interpreter that has ruyiPage installed.

    Resolution order (first hit wins):
      1. ``BROWSER_FETCH_PYTHON``   — shared override for both callers
      2. ``REDEEM_CODE_BROWSER_PYTHON`` — legacy redeem-code override
      3. ``~/.virtualenvs/ruyipage/bin/python`` — the dedicated venv
      4. ``sys.executable`` — bot venv (works if ruyiPage was installed there)
    """
    for env in ("BROWSER_FETCH_PYTHON", "REDEEM_CODE_BROWSER_PYTHON"):
        val = os.environ.get(env, "").strip()
        if val:
            return os.path.expanduser(val)
    dedicated = os.path.expanduser("~/.virtualenvs/ruyipage/bin/python")
    if os.path.exists(dedicated):
        return dedicated
    return sys.executable


def _max_concurrency() -> int:
    try:
        return max(1, int(os.environ.get("BROWSER_FETCH_MAX_CONCURRENCY", "2")))
    except ValueError:
        return 2


def _sem() -> asyncio.Semaphore:
    """Process-wide cap on concurrent Firefox subprocesses (redeem + web_fetch
    share it). Created lazily so it binds to the running event loop."""
    global _SEM
    if _SEM is None:
        _SEM = asyncio.Semaphore(_max_concurrency())
    return _SEM


async def fetch_via_browser(
    url: str,
    *,
    enable_flag_env: str,
    timeout: int,
    min_content: int = 20000,
    require_table: bool = True,
    log_prefix: str = "[Browser]",
    ssrf_recheck=None,
) -> str | None:
    """Fetch a Cloudflare-protected URL via the ruyiPage subprocess.

    Runs ``xvfb-run -a <python> ruyipage_fetch.py <url> <out> <timeout>
    [--min-content N] [--no-table-wait] [--final-url-file F]`` with a hard
    timeout, reads the rendered HTML back from the temp file.

    Returns the HTML, or ``None`` on ANY failure (engine disabled, xvfb/
    browser missing, timeout, non-zero exit, SSRF recheck hit, empty/tiny
    output) so the caller can fall back — never raises.

    ``ssrf_recheck``: optional ``callable(final_url) -> str | None``. When the
    fetcher reports a post-redirect final URL and the recheck returns a
    non-empty reason (i.e. it points at an internal/metadata address), the
    rendered output is discarded and ``None`` returned.
    """
    if not engine_enabled(enable_flag_env):
        return None
    if not os.path.exists(_BROWSER_FETCHER):
        print(f"{log_prefix} browser fetcher missing: {_BROWSER_FETCHER}",
              file=sys.stderr)
        return None
    xvfb = shutil.which("xvfb-run")
    if not xvfb:
        print(f"{log_prefix} xvfb-run not found; cannot run browser engine "
              f"(install xvfb or unset {enable_flag_env})", file=sys.stderr)
        return None

    py = resolve_python()
    fd, out_path = tempfile.mkstemp(prefix="browser_fetch_", suffix=".html")
    os.close(fd)
    url_path = None
    if ssrf_recheck is not None:
        fd2, url_path = tempfile.mkstemp(prefix="browser_url_", suffix=".txt")
        os.close(fd2)

    cmd = [xvfb, "-a", py, _BROWSER_FETCHER, url, out_path, str(timeout),
           "--min-content", str(min_content)]
    if not require_table:
        cmd.append("--no-table-wait")
    if url_path:
        cmd += ["--final-url-file", url_path]

    async with _sem():
        print(f"{log_prefix} browser engine fetch: {' '.join(cmd[:2])} ... {url}",
              file=sys.stderr)
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout + 30,
                )
            except asyncio.TimeoutError:
                print(f"{log_prefix} browser fetch timed out after "
                      f"{timeout + 30}s; killing", file=sys.stderr)
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                try:
                    await proc.communicate()
                except Exception:  # noqa: BLE001
                    pass
                return None
            if stderr:
                # Surface the fetcher's own diagnostics (CF progress, result).
                tail = stderr.decode("utf-8", "replace").strip().splitlines()[-6:]
                for ln in tail:
                    print(f"{log_prefix}   {ln}", file=sys.stderr)
            if proc.returncode != 0:
                print(f"{log_prefix} browser fetch exit={proc.returncode} "
                      f"for {url}", file=sys.stderr)
                return None
            # SSRF recheck on the post-redirect final URL before trusting it.
            if ssrf_recheck is not None and url_path:
                try:
                    with open(url_path, encoding="utf-8") as f:
                        final_url = f.read().strip()
                except OSError:
                    final_url = ""
                if final_url:
                    reason = ssrf_recheck(final_url)
                    if reason:
                        print(f"{log_prefix} final URL failed SSRF recheck: "
                              f"{reason}", file=sys.stderr)
                        return None
            try:
                with open(out_path, encoding="utf-8") as f:
                    html = f.read()
            except OSError as e:
                print(f"{log_prefix} cannot read browser output: {e}",
                      file=sys.stderr)
                return None
            if not html or len(html) < 1000:
                print(f"{log_prefix} browser returned empty/tiny HTML "
                      f"({len(html)} bytes)", file=sys.stderr)
                return None
            print(f"{log_prefix} browser engine OK: {len(html)} bytes from {url}",
                  file=sys.stderr)
            return html
        except Exception as e:  # noqa: BLE001
            print(f"{log_prefix} browser fetch error for {url}: "
                  f"{type(e).__name__}: {e}", file=sys.stderr)
            if proc is not None:
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass
            return None
        finally:
            for p in (out_path, url_path):
                if p:
                    try:
                        os.remove(p)
                    except OSError:
                        pass

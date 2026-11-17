"""Public HTTPS through a proxy pinned to a previously validated address."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from http.client import HTTPException, parse_headers
import io
import os
import shutil

import httpx


_MAX_HEADER = 64 * 1024
_ATTEMPT_SECONDS = 3.2
_RETRYABLE = {7, 28, 35}  # connect, timeout, TLS handshake


class ProfileProxyError(Exception):
    def __init__(self, curl_code: int | None = None):
        self.curl_code = curl_code
        self.retryable = curl_code in _RETRYABLE


@dataclass(frozen=True)
class ProfileResponse:
    status: int
    headers: httpx.Headers
    body: bytes = b""


async def _headers(pipe: asyncio.StreamReader) -> tuple[int, httpx.Headers]:
    for _ in range(3):
        block = await pipe.readuntil(b"\r\n\r\n")
        line, raw = block.split(b"\r\n", 1)
        fields = line.split(None, 2)
        if len(fields) < 2 or not fields[0].startswith(b"HTTP/"):
            raise ValueError("Invalid HTTP status line")
        status = int(fields[1])
        headers = httpx.Headers(parse_headers(io.BytesIO(raw)).raw_items())
        if not 100 <= status < 200:
            return status, headers
    raise ValueError("Too many provisional HTTP responses")


async def fetch_pinned(url: httpx.URL, address: str, proxy: str, limit: int) -> ProfileResponse:
    """Keep the original URL for SNI, Host and certificate verification."""
    curl = shutil.which("curl")
    if not curl:
        raise ProfileProxyError()
    host = url.raw_host.decode("ascii")
    peer = f"[{address}]" if ":" in address else address
    command = (
        curl, "-q", "--silent", "--globoff", "--http1.1", "--proto", "=https",
        "--noproxy", "", "--connect-to", f"{host}:443:{peer}:443",
        "--connect-timeout", "2.5", "--max-time", str(_ATTEMPT_SECONDS),
        "--max-filesize", str(limit), "--include", "--suppress-connect-headers", "--compressed",
        "--user-agent", "QQBot-Agent/2.0", "--url", str(url),
    )
    # Keep configured proxy credentials out of the process arguments.
    env = {**os.environ, "https_proxy": proxy, "HTTPS_PROXY": proxy}
    try:
        process = await asyncio.create_subprocess_exec(
            *command, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, env=env, limit=_MAX_HEADER)
    except OSError as exc:
        raise ProfileProxyError() from exc

    try:
        assert process.stdout is not None
        async with asyncio.timeout(_ATTEMPT_SECONDS + .3):
            status, headers = await _headers(process.stdout)
            if status != 200:
                return ProfileResponse(status, headers)
            try:
                await process.stdout.readexactly(limit + 1)
            except asyncio.IncompleteReadError as exc:
                code = await process.wait()
                if code:
                    # Headers already arrived. Keep this a body failure.
                    raise ProfileProxyError() from exc
                return ProfileResponse(status, headers, exc.partial)
            raise ProfileProxyError()  # decoded body exceeded the limit
    except asyncio.IncompleteReadError as exc:
        raise ProfileProxyError(await process.wait()) from exc
    except asyncio.TimeoutError as exc:
        raise ProfileProxyError(28) from exc
    except (asyncio.LimitOverrunError, HTTPException, ValueError) as exc:
        raise ProfileProxyError() from exc
    finally:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.communicate()

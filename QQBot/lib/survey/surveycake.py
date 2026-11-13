"""SurveyCake adapter (P0): link parsing and read-only schema fetch.

Only the public survey definition is read. No answer is ever sent in P0;
submission will be added behind an explicit confirmation in a later phase.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
import socket
import time
import urllib.parse
import urllib.request

from .model import SurveyDef, parse_survey_json

DEFAULT_HOSTS = ("www.surveycake.com", "surveycake.com")
_SCHEMA_HOST = "www.surveycake.com"
_SVID_RE = re.compile(r"^[A-Za-z0-9]{4,20}$")
_CACHE_TTL_SEC = 600.0
_cache: dict[str, tuple[float, SurveyDef]] = {}


class SurveyError(Exception):
    """A user-facing survey error; its message is safe to show in chat."""


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or "").strip() or default


def allowed_hosts() -> tuple[str, ...]:
    raw = _env("SURVEY_ALLOWED_HOSTS")
    if not raw:
        return DEFAULT_HOSTS
    hosts = tuple(part.strip().lower() for part in raw.split(",") if part.strip())
    return hosts or DEFAULT_HOSTS


def proxy_url() -> str:
    """Proxy for schema fetch + browser submit.

    SURVEY_PROXY wins; otherwise the standard HTTPS_PROXY, then the
    redeem-code proxy (the deployment already configures one for the same
    outbound path, so survey does not need a second setting).
    """
    return (_env("SURVEY_PROXY") or _env("HTTPS_PROXY")
            or _env("https_proxy") or _env("REDEEM_CODE_PROXY"))


def parse_survey_url(url: str) -> str:
    """Return the svid from a SurveyCake share link."""
    raw = (url or "").strip()
    if not raw:
        raise SurveyError("请提供问卷链接，例如：/问卷 https://www.surveycake.com/s/ABb8y")
    if not re.match(r"(?i)^https://", raw):
        raise SurveyError("仅支持 https:// 开头的问卷链接")
    parts = urllib.parse.urlsplit(raw)
    host = (parts.hostname or "").lower()
    if host not in allowed_hosts():
        raise SurveyError(f"暂不支持该问卷站点：{host or raw}")
    segments = [segment for segment in parts.path.split("/") if segment]
    svid = ""
    if len(segments) >= 2 and segments[0].lower() == "s":
        svid = segments[1]
    if not svid or not _SVID_RE.match(svid):
        raise SurveyError("无法从链接中识别问卷编号（应为 /s/<编号>）")
    return svid


def schema_url(svid: str) -> str:
    return f"https://{_SCHEMA_HOST}/s3/json/{urllib.parse.quote(svid)}.json"


def _ensure_public_host(host: str) -> None:
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise SurveyError(f"无法解析问卷地址：{host}") from exc
    for info in infos:
        try:
            address = ipaddress.ip_address(info[4][0])
        except (TypeError, ValueError):
            continue
        if not address.is_global:
            raise SurveyError("问卷地址不是公网地址，已拒绝访问")


def _http_get_json(url: str, timeout: float, proxy: str) -> dict:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
                      " AppleWebKit/537.36 (KHTML, like Gecko)"
                      " Chrome/131.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.surveycake.com/",
    }
    try:
        from curl_cffi import requests as cffi_requests
    except ImportError:
        cffi_requests = None

    status = 0
    body = ""
    if cffi_requests is not None:
        kwargs = {}
        if proxy:
            kwargs["proxies"] = {"http": proxy, "https": proxy}
        try:
            response = cffi_requests.get(
                url, impersonate="chrome131", timeout=timeout,
                headers=headers, **kwargs,
            )
        except Exception as exc:
            raise SurveyError(f"读取问卷失败：{type(exc).__name__}") from exc
        status = response.status_code
        body = response.text
    else:
        try:
            import httpx
        except ImportError:
            return _urllib_get_json(url, timeout, proxy, headers)
        client_kwargs = {"timeout": timeout, "trust_env": False}
        if proxy:
            client_kwargs["proxy"] = proxy
        try:
            try:
                with httpx.Client(**client_kwargs) as client:
                    response = client.get(url, headers=headers)
            except TypeError:
                if proxy:
                    client_kwargs.pop("proxy", None)
                    client_kwargs["proxies"] = proxy
                with httpx.Client(**client_kwargs) as client:
                    response = client.get(url, headers=headers)
        except Exception as exc:
            raise SurveyError(f"读取问卷失败：{type(exc).__name__}") from exc
        status = response.status_code
        body = response.text

    if status != 200:
        raise SurveyError(f"读取问卷失败（HTTP {status}）")
    try:
        return json.loads(body)
    except ValueError as exc:
        raise SurveyError("问卷定义不是有效的 JSON") from exc


def _urllib_get_json(url: str, timeout: float, proxy: str, headers: dict) -> dict:
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    opener = urllib.request.build_opener(*handlers)
    request = urllib.request.Request(url, headers=headers)
    try:
        with opener.open(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
    except Exception as exc:
        raise SurveyError(f"读取问卷失败：{type(exc).__name__}") from exc
    try:
        return json.loads(body)
    except ValueError as exc:
        raise SurveyError("问卷定义不是有效的 JSON") from exc


def ensure_public_host(host: str) -> None:
    """Public alias used by the P1 browser submitter (SSRF pre-check)."""
    _ensure_public_host(host)


async def fetch_schema(svid: str, *, timeout: float | None = None,
                       force: bool = False) -> SurveyDef:
    """Fetch (and cache) the public survey definition."""
    if not _SVID_RE.match(svid or ""):
        raise SurveyError("问卷编号无效")
    now = time.time()
    cached = _cache.get(svid)
    if cached and not force and cached[0] > now:
        return cached[1]

    _ensure_public_host(_SCHEMA_HOST)
    if timeout is None:
        try:
            timeout = float(_env("SURVEY_TIMEOUT", "15") or 15)
        except ValueError:
            timeout = 15.0
    data = await asyncio.to_thread(_http_get_json, schema_url(svid), timeout, proxy_url())
    survey = parse_survey_json(data, svid=svid)
    if survey.status and survey.status.upper() != "PUBLIC":
        raise SurveyError(f"问卷当前不可填写（状态：{survey.status}）")
    _cache[svid] = (now + _CACHE_TTL_SEC, survey)
    return survey


def clear_cache() -> None:
    _cache.clear()
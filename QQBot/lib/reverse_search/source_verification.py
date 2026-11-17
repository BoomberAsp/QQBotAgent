"""Bounded public-page lookup and same-image checks for an authorized search."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from html.parser import HTMLParser
import io
import ipaddress
import json
import os
import re
import socket
from urllib.parse import urljoin, urlsplit

import httpx
from loguru import logger
from PIL import Image, ImageChops, ImageOps, ImageStat

from .accounts import (
    canonical_url, handle_of, handles_in_text, is_author_page, is_post_page, page_key, platform_of,
)
from .engines import Candidate
from .net import configured_proxy
from .profile_transport import ProfileProxyError, fetch_pinned

_MAX_HTML = 1024 * 1024
_MAX_MEDIA = 4 * 1024 * 1024
_MAX_PIXELS = 20_000_000
_REDIRECTS = {301, 302, 303, 307, 308}
_HANDLE = re.compile(r"@[A-Za-z0-9_.]{2,30}")
_X_HOSTS = {"x.com", "www.x.com", "m.x.com", "twitter.com", "www.twitter.com",
            "m.twitter.com", "mobile.twitter.com"}
_X_PHOTO_PATH = re.compile(r"^(.+/status/\d+)/(?:photo|video)/\d+/?$")


@dataclass(frozen=True)
class Verification:
    status: str
    attribution: str = ""


@dataclass(frozen=True)
class PublicProfile:
    title: str
    description: str


class PublicPageUnavailable(Exception):
    pass


async def _public_addresses(host: str) -> list[str]:
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            host, 443, type=socket.SOCK_STREAM)
        addresses = list(dict.fromkeys(row[4][0] for row in infos))
        ips = [ipaddress.ip_address(address) for address in addresses]
    except (OSError, ValueError) as exc:
        raise PublicPageUnavailable from exc
    normalized = [getattr(ip, "ipv4_mapped", None) or ip for ip in ips]
    if not normalized or any(not ip.is_global or ip.is_multicast or ip.is_reserved for ip in normalized):
        raise PublicPageUnavailable
    return sorted(addresses, key=lambda address: ":" in address)


async def _public_get(url: str, *, limit: int, content: str) -> tuple[bytes, str]:
    """Validate every redirect and pin the connection to a checked IP."""
    for _ in range(4):
        try:
            target = httpx.URL(url)
            if (target.scheme != "https" or not target.host or target.userinfo
                    or target.port not in (None, 443)):
                raise PublicPageUnavailable
            host = target.raw_host.decode("ascii")
            addresses = await _public_addresses(host)
            for address in addresses[:2]:
                try:
                    async with httpx.AsyncClient(trust_env=False, timeout=3) as client:
                        async with client.stream(
                            "GET", target.copy_with(host=address), follow_redirects=False,
                            headers={"Host": host, "User-Agent": "QQBot-Agent/2.0"},
                            extensions={"sni_hostname": host},
                        ) as response:
                            if response.status_code in _REDIRECTS and response.headers.get("location"):
                                url = urljoin(url, response.headers["location"])
                                break
                            mime = response.headers.get("content-type", "").split(";", 1)[0].lower()
                            if response.status_code != 200 or not mime.startswith(content):
                                raise PublicPageUnavailable
                            data = bytearray()
                            async for chunk in response.aiter_bytes():
                                if len(data) + len(chunk) > limit:
                                    raise PublicPageUnavailable
                                data.extend(chunk)
                            return bytes(data), url
                except httpx.TransportError:
                    continue
            else:
                raise PublicPageUnavailable
        except (httpx.HTTPError, httpx.InvalidURL, ValueError) as exc:
            raise PublicPageUnavailable from exc
    raise PublicPageUnavailable


def _x_host(url: str) -> bool:
    return urlsplit(url).hostname in _X_HOSTS


def _x_post_media(url: str) -> bool:
    try:
        target = httpx.URL(url)
    except (httpx.InvalidURL, ValueError):
        return False
    return target.host == "pbs.twimg.com" and target.path.startswith("/media/")


async def _proxied_x_get(url: str, proxy: str, *, limit: int, content: str,
                         allowed_url: Callable[[str], bool]) -> tuple[bytes, str]:
    retry_available = True
    try:
        # Preserve the caller's per-page budget, including DNS and redirects.
        async with asyncio.timeout(7.5):
            for _ in range(4):
                target = httpx.URL(url)
                if (target.scheme != "https" or target.userinfo or target.port not in (None, 443)
                        or not allowed_url(url)):
                    raise PublicPageUnavailable
                host = target.raw_host.decode("ascii")
                addresses = await _public_addresses(host)
                while True:
                    try:
                        response = await fetch_pinned(target, addresses[0], proxy, limit)
                        break
                    except ProfileProxyError as exc:
                        logger.info("[找图] X 代理核验 host={} curl_code={} retry={}",
                                    host, exc.curl_code, exc.retryable and retry_available)
                        if not exc.retryable or not retry_available:
                            raise PublicPageUnavailable from exc
                        retry_available = False
                location = response.headers.get("location")
                if response.status in _REDIRECTS and location:
                    url = urljoin(url, location)
                    continue
                mime = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if response.status == 200 and mime.startswith(content):
                    return response.body, url
                logger.info("[找图] X 代理核验 host={} status={}", host, response.status)
                break
    except (httpx.InvalidURL, ValueError, asyncio.TimeoutError) as exc:
        raise PublicPageUnavailable from exc
    raise PublicPageUnavailable


class _PageMetadata(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.images: list[str] = []
        self.summary: dict[str, str] = {}
        self.attribution = ""
        self._json_ld: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        pairs = dict(attrs)
        key = str(pairs.get("property") or pairs.get("name") or "").lower()
        content = pairs.get("content") or ""
        if tag == "meta" and key in {"og:title", "og:description", "og:url", "twitter:title",
                                     "twitter:description", "description"} and content:
            self.summary.setdefault(key, " ".join(content.split())[:350])
        if tag == "meta" and key in {"og:image", "og:image:secure_url", "twitter:image"}:
            if content and content not in self.images and len(self.images) < 3:
                self.images.append(content)
        if tag == "link" and "author" in str(pairs.get("rel") or "").lower().split():
            self.attribution = self.attribution or (pairs.get("href") or "")[:250]
        if tag == "meta" and key in {"article:author", "author"}:
            self.attribution = self.attribution or content[:250]
        if tag == "script" and pairs.get("type") == "application/ld+json":
            self._json_ld = []

    def handle_data(self, data):
        if self._json_ld is not None:
            self._json_ld.append(data)

    def handle_endtag(self, tag):
        if tag != "script" or self._json_ld is None:
            return
        raw = "".join(self._json_ld)
        self._json_ld = None
        try:
            record = json.loads(raw)
        except (ValueError, RecursionError):
            return
        entries = record if isinstance(record, list) else [record]
        for item in entries:
            if not isinstance(item, dict):
                continue
            author = item.get("author")
            if isinstance(author, list):
                author = author[0] if author else None
            if isinstance(author, dict):
                author = author.get("name") or author.get("url")
            if isinstance(author, str) and author:
                self.attribution = self.attribution or author[:250]


def _normalized(data: bytes) -> tuple[Image.Image, float]:
    with Image.open(io.BytesIO(data)) as image:
        if image.width * image.height > _MAX_PIXELS:
            raise ValueError("Image too large")
        image = ImageOps.exif_transpose(image)
        ratio = image.width / image.height
        rgb = image.convert("RGBA")
        background = Image.new("RGBA", rgb.size, "white")
        background.alpha_composite(rgb)
        return background.convert("RGB").resize((48, 48), Image.Resampling.LANCZOS), ratio


def _same_image(left: bytes, right: bytes) -> bool:
    if left == right:
        return True
    try:
        a, ratio_a = _normalized(left)
        b, ratio_b = _normalized(right)
    except (OSError, ValueError, Image.DecompressionBombError):
        return False
    if abs(ratio_a - ratio_b) > .035 * max(ratio_a, ratio_b):
        return False
    # Compare the full image. Re-encoding and resizing pass; substantial
    # cropping or different photos should remain for manual review.
    diff = ImageStat.Stat(ImageChops.difference(a, b))
    texture = ImageStat.Stat(a)
    return max(texture.stddev) >= 8 and sum(diff.mean) / 3 <= 4 and max(diff.rms) <= 10


async def _get_metadata(url: str, *, proxy: str | None = None,
                        allowed_url: Callable[[str], bool] | None = None) -> tuple[_PageMetadata, str]:
    if proxy and allowed_url:
        data, final_url = await _proxied_x_get(
            url, proxy, limit=_MAX_HTML, content="text/html", allowed_url=allowed_url)
    else:
        data, final_url = await _public_get(url, limit=_MAX_HTML, content="text/html")
    metadata = _PageMetadata()
    try:
        metadata.feed(data.decode("utf-8", errors="replace"))
    except (ValueError, AssertionError) as exc:
        raise PublicPageUnavailable from exc
    return metadata, final_url


async def read_public_profile(url: str) -> PublicProfile | None:
    if not is_author_page(url):
        return None
    proxy = (configured_proxy("FIND_SOURCE_X_PROFILE_PROXY", "FIND_SOURCE_LENS_PROXY")
             if platform_of(url) == "Twitter/X" else None)
    expected = page_key(url)
    try:
        metadata, final_url = await _get_metadata(
            url, proxy=proxy, allowed_url=lambda target: _x_host(target) and page_key(target) == expected)
    except PublicPageUnavailable:
        return None
    if page_key(final_url) != page_key(url):
        return None
    fields = metadata.summary
    title = fields.get("og:title") or fields.get("twitter:title", "")
    about = fields.get("og:description") or fields.get("twitter:description") or fields.get("description", "")
    handles = {name.casefold() for name in handles_in_text(f"{title} {about}")}
    og_url = fields.get("og:url", "")
    identified = handle_of(url).casefold() in handles or (og_url and page_key(og_url) == page_key(url))
    if not identified or not (title or about):
        return None
    return PublicProfile(title[:120], about[:280])


async def verify_source(url: str, image: bytes) -> Verification:
    post = is_post_page(url)
    x_post = post and _x_host(url)
    expected = page_key(url)
    proxy = None
    if x_post:
        proxy = configured_proxy("FIND_SOURCE_X_POST_PROXY", "FIND_SOURCE_X_PROFILE_PROXY", "FIND_SOURCE_LENS_PROXY")
        target = httpx.URL(url)
        # The /photo/1 viewer may load differently from the post. Both
        # identify the same post, which is rechecked after redirects.
        url = str(target.copy_with(path=_X_PHOTO_PATH.sub(r"\1", target.path), query=None, fragment=None))
    try:
        metadata, final_url = await _get_metadata(
            url, proxy=proxy, allowed_url=lambda target: _x_host(target) and page_key(target) == expected)
    except PublicPageUnavailable:
        return Verification("unavailable")
    if post and page_key(final_url) != expected:
        return Verification("unavailable")
    checked = False
    for media in metadata.images:
        media_url = urljoin(final_url, media)
        if x_post and not _x_post_media(media_url):
            continue
        try:
            if x_post and proxy:
                data, _ = await _proxied_x_get(
                    media_url, proxy, limit=_MAX_MEDIA, content="image/", allowed_url=_x_post_media)
            else:
                data, landed = await _public_get(media_url, limit=_MAX_MEDIA, content="image/")
                if x_post and not _x_post_media(landed):
                    continue
        except PublicPageUnavailable:
            continue
        checked = True
        if await asyncio.to_thread(_same_image, image, data):
            author = metadata.attribution.strip()
            if author.startswith(("http://", "https://", "/")):
                author = canonical_url(urljoin(final_url, author))
            return Verification("same_image", author)
    return Verification("unconfirmed" if checked else "no_media")


async def lookup_watermark(handle: str) -> list[Candidate]:
    """Search an exact image watermark using the existing SearXNG endpoint."""
    if not _HANDLE.fullmatch(handle):
        return []
    endpoint = os.environ.get("SEARXNG_ENDPOINT", "http://localhost:8082").rstrip("/")
    params = {"q": f'"{handle}"', "format": "json", "language": "zh-CN",
              "safesearch": "1", "categories": "general"}
    async with httpx.AsyncClient(trust_env=False, timeout=5) as client:
        response = await client.get(f"{endpoint}/search", params=params)
        response.raise_for_status()
        payload = response.json()
    matches = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(matches, list):
        return []
    out = []
    seen = set()
    expected = handle[1:].lower()
    for row in matches[:30]:
        if not isinstance(row, dict):
            continue
        url = canonical_url(row.get("url", ""))
        title = str(row.get("title") or "")[:200]
        text = title + " " + str(row.get("content") or "")[:500]
        identity = page_key(url)
        if identity and identity not in seen and (handle_of(url).lower() == expected or expected in {
                name.lower() for name in handles_in_text(text)}):
            seen.add(identity)
            out.append(Candidate("SearXNG", url, title, similarity=None, extra={"match": "watermark"}))
        if len(out) == 5:
            break
    return out

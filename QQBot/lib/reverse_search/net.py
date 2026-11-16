"""图片校验与安全下载（找图反查一期）。

一期引擎全部 file=bytes 直传，不需要第三方图床；URL 入口只用于
「命令里直接给图片地址」，必须挡内网/环回（SSRF）。
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

import httpx

MAX_IMAGE_BYTES = 10 * 1024 * 1024   # 10MB，QQ 图远低于此
MIN_IMAGE_BYTES = 512

_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpg"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
    (b"RIFF", "webp"),          # RIFF....WEBP，下面再验 WEBP 标记
    (b"BM", "bmp"),
)


def sniff_format(data: bytes) -> str:
    """按魔数判格式，返回 png/jpg/gif/webp/bmp 或空串。"""
    for magic, fmt in _MAGIC:
        if data.startswith(magic):
            if fmt == "webp" and data[8:12] != b"WEBP":
                continue
            return fmt
    return ""


def validate_image(data: bytes) -> str:
    """校验图片字节，返回错误原因（空串=通过）。"""
    if not data:
        return "图片为空"
    if len(data) < MIN_IMAGE_BYTES:
        return "图片太小，可能不是有效图片"
    if len(data) > MAX_IMAGE_BYTES:
        return "图片超过 10MB 上限"
    if not sniff_format(data):
        return "无法识别的图片格式（支持 PNG/JPG/GIF/WEBP/BMP）"
    return ""


def check_url_allowed(url: str) -> str:
    """SSRF 校验：只放行 http(s) 且解析到公网地址。返回错误原因（空串=通过）。

    纯校验，不发请求；safe_download 在 redirects 后对末 URL 再调一次。
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return "URL 不合法"
    if parts.scheme not in ("http", "https"):
        return "只支持 http/https 图片地址"
    host = parts.hostname or ""
    if not host:
        return "URL 缺少主机名"
    if host in ("localhost",) or host.endswith(".local") or host.endswith(".internal"):
        return "不允许访问内网地址"
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return "域名无法解析"
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return "无法解析的地址"
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return "不允许访问内网/环回地址"
    return ""


async def safe_download(url: str, timeout: float = 20.0) -> bytes:
    """下载公网图片字节，带 SSRF 校验与大小上限。失败抛 ValueError。"""
    err = check_url_allowed(url)
    if err:
        raise ValueError(err)
    async with httpx.AsyncClient(follow_redirects=True, timeout=timeout) as client:
        resp = await client.get(url)
        if resp.status_code != 200:
            raise ValueError(f"图片下载失败 (HTTP {resp.status_code})")
        final = str(resp.url)
        if final != url:
            err = check_url_allowed(final)
            if err:
                raise ValueError(f"重定向目标被拦截：{err}")
        data = resp.content
        if len(data) > MAX_IMAGE_BYTES:
            raise ValueError("图片超过 10MB 上限")
        return data

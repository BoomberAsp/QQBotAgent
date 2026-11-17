"""图片校验与安全下载（找图反查一期）。

一期引擎全部 file=bytes 直传，不需要第三方图床；URL 入口只用于
「命令里直接给图片地址」，必须挡内网/环回（SSRF）。
"""
from __future__ import annotations

import io
import ipaddress
import os
import socket
from urllib.parse import urlsplit

import httpx
from PIL import Image

MAX_IMAGE_BYTES = 20 * 1024 * 1024   # 20MB 输入上限；送引擎前大图另经压缩
MIN_IMAGE_BYTES = 512

UPLOAD_MAX_BYTES = 2 * 1024 * 1024   # 压缩触发阈值：字节或长边超限才压
UPLOAD_MAX_SIDE = 2048               # 引擎指纹工作尺寸远低于此，降采样无损
UPLOAD_JPEG_QUALITY = 88

_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpg"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
    (b"RIFF", "webp"),          # RIFF....WEBP，下面再验 WEBP 标记
    (b"BM", "bmp"),
)


def configured_proxy(*names: str) -> str | None:
    """Use the first configured route. An explicit direct route ends fallback."""
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return None if value.lower() in {"direct", "off", "0", "no", "false"} else value
    return None


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
        return "图片超过 20MB 上限"
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
            raise ValueError("图片超过 20MB 上限")
        return data


def compress_for_upload(data: bytes) -> bytes:
    """送引擎/Lens 前压缩大图：白底合成 + 长边 2048 / JPEG q88。

    仅当 >2MB 或长边 >2048 触发；压缩后不小于原图、或解码失败时原样
    返回（小图/已压图不强制重编码）。实测（2026-11-17 SauceNAO 真匹配
    A/B）：降采样+重压缩下命中集合与排序稳定、sim 波动 ±1.5；白底合成
    消除 alpha 底色造成的指纹漂移（透明底变黑会整体拉低相似度）。
    """
    try:
        with Image.open(io.BytesIO(data)) as im:
            if len(data) <= UPLOAD_MAX_BYTES and max(im.size) <= UPLOAD_MAX_SIDE:
                return data
            if im.mode in ("RGBA", "LA", "P"):
                rgba = im.convert("RGBA")
                bg = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
                rgb = Image.alpha_composite(bg, rgba).convert("RGB")
            else:
                rgb = im.convert("RGB")
        rgb.thumbnail((UPLOAD_MAX_SIDE, UPLOAD_MAX_SIDE))
        buf = io.BytesIO()
        rgb.save(buf, "JPEG", quality=UPLOAD_JPEG_QUALITY)
        out = buf.getvalue()
        return out if len(out) < len(data) else data
    except Exception:  # noqa: BLE001 — 解码失败回退原图，格式由引擎侧自判
        return data

"""
Image → OpenAI content-part encoder (Direct Vision).

Builds ``image_url`` content parts (base64 data URIs) from local image
files so the main chat models — when their ``models_settings.json``
section carries ``supports_vision: true`` — can SEE user uploads
directly in the current-turn user message, instead of round-tripping
through a separate vision model via tools.

Design notes:
- Parts are ephemeral: they live only in the in-memory ``messages`` list
  of one ``Agent.run()`` (across its tool iterations). Session history
  persists the TEXT placeholder lines only, so base64 never hits disk
  and the provider prefix cache is untouched (parts sit in the final
  user message, after the volatile L4 tail).
- All failures are silent (return None / skip): vision is an
  enhancement — the text placeholder + read_file lazy analysis remain
  as the fallback path.
"""

from __future__ import annotations

import base64
import io
import os
from typing import Dict, List, Optional

# Longest side after downscaling. 1568 keeps typical vision-tokenizer
# tiling (~1500 tokens/image) bounded while preserving screenshot text.
MAX_SIDE = 1568
# Reject raw files larger than this before even opening them.
RAW_SIZE_CAP = 20 * 1024 * 1024
# Reject images whose base64 encoding exceeds this (provider body limits).
ENCODED_CAP = 5 * 1024 * 1024
# Per-turn attachment cap (drop the tail beyond this).
MAX_PER_TURN = 4

# Extension → MIME (mirrors lib/multimodal_client.py analyze_image).
_MIME_BY_EXT = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".webp": "image/webp",
}

# Header magic → MIME, for QQ downloads that arrive extensionless.
_MAGIC_SNIFFERS = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"BM", "image/bmp"),
    (b"RIFF", "image/webp"),  # RIFF....WEBP — second check below
)

_MIME_BY_FORMAT = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "GIF": "image/gif",
    "BMP": "image/bmp",
    "WEBP": "image/webp",
}


def _sniff_mime(head: bytes) -> Optional[str]:
    """Guess MIME from the first bytes; None when unrecognised."""
    for magic, mime in _MAGIC_SNIFFERS:
        if head.startswith(magic):
            if magic == b"RIFF" and head[8:12] != b"WEBP":
                continue
            return mime
    return None


def _maybe_downscale(data: bytes, path: str) -> Optional[tuple]:
    """Normalise image bytes → (bytes, mime), or None when undecodable.

    - GIF: first frame only, converted to JPEG (multi-frame base64 blows
      the cap and most vision APIs handle GIF poorly).
    - Any side > MAX_SIDE: aspect-preserving LANCZOS resize.
    - Re-encode to JPEG q=85 when the source has no alpha or was
      resized; alpha sources stay PNG; small plain PNG/JPEG pass through.
    - PIL cannot open the bytes → None: if we can't decode it, the
      provider's vision API can't either — skip instead of sending
      garbage base64.
    """
    ext = os.path.splitext(path)[1].lower()
    fallback_mime = _MIME_BY_EXT.get(ext) or _sniff_mime(data[:16]) or "image/png"
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as im:
            im.load()  # force decode — corrupt bodies raise here
            fmt = (im.format or "").upper()
            mime = _MIME_BY_FORMAT.get(fmt, fallback_mime)
            if fmt == "GIF":
                im.seek(0)
                frame = im.convert("RGB")
                if max(frame.size) > MAX_SIDE:
                    frame = _resize(frame)
                buf = io.BytesIO()
                frame.save(buf, format="JPEG", quality=85)
                return buf.getvalue(), "image/jpeg"

            has_alpha = im.mode in ("RGBA", "LA", "PA") or (
                im.mode == "P" and "transparency" in im.info
            )
            needs_resize = max(im.size) > MAX_SIDE
            if not needs_resize and not has_alpha and fmt in ("PNG", "BMP"):
                # Small alpha-capable/bitmap source: keep bytes, but BMP
                # is poorly supported inline → still re-encode those.
                if fmt != "BMP":
                    return data, mime
            rgb = im.convert("RGBA") if has_alpha else im.convert("RGB")
            if needs_resize:
                rgb = _resize(rgb)
            buf = io.BytesIO()
            if has_alpha:
                rgb.save(buf, format="PNG", optimize=True)
                out_mime = "image/png"
            else:
                rgb.save(buf, format="JPEG", quality=85)
                out_mime = "image/jpeg"
            out = buf.getvalue()
            # Re-encoding a small image can occasionally inflate it;
            # prefer the original when it was already fine.
            if not needs_resize and len(out) > len(data) and fmt in _MIME_BY_FORMAT:
                return data, mime
            return out, out_mime
    except Exception:
        return None  # undecodable — skip rather than send garbage base64


def _resize(im):
    """Aspect-preserving LANCZOS resize so the longest side ≤ MAX_SIDE."""
    from PIL import Image

    w, h = im.size
    scale = MAX_SIDE / float(max(w, h))
    return im.resize(
        (max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS
    )


def build_image_part(path: str) -> Optional[Dict]:
    """Encode one local image file into an OpenAI ``image_url`` part.

    Returns None (never raises) for missing/unreadable/oversized/corrupt
    files or unsupported formats — callers just skip those images and the
    text placeholder + read_file lazy-analysis fallback still applies.
    """
    try:
        if not path or not os.path.isfile(path):
            return None
        if os.path.getsize(path) > RAW_SIZE_CAP:
            return None
        with open(path, "rb") as f:
            data = f.read()
        if not data:
            return None

        ext = os.path.splitext(path)[1].lower()
        mime = _MIME_BY_EXT.get(ext) or _sniff_mime(data[:16])
        if not mime:
            return None  # unrecognised format — don't guess

        normalised = _maybe_downscale(data, path)
        if normalised is None:
            return None  # corrupt/undecodable
        data, mime = normalised
        encoded = base64.b64encode(data).decode("utf-8")
        if len(encoded) > ENCODED_CAP:
            return None
        return {
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{encoded}"},
        }
    except Exception:
        return None


def build_image_parts(paths: List[str], max_count: int = MAX_PER_TURN) -> List[Dict]:
    """Encode a list of image paths into content parts (caps + skips).

    Order is preserved; unreadable/oversized entries are dropped without
    consuming a slot; stops as soon as ``max_count`` valid parts exist.
    """
    parts: List[Dict] = []
    for p in paths or []:
        if len(parts) >= max_count:
            break
        part = build_image_part(p)
        if part is not None:
            parts.append(part)
    return parts

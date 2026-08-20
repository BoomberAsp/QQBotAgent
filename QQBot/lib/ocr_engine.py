"""
OCR Engine — Lightweight abstraction over EasyOCR for battle screenshot parsing.

Supports Chinese + English text extraction with bounding-boxes, plus PIL-based
pixel colour sampling for side (ally/enemy) determination. The EasyOCR model
is loaded lazily and kept resident for the lifetime of the process (battle
speed-check is a high-frequency feature; reloading every call wastes CPU).

Frame detection is heuristic: the central dark semi-transparent panel in the
game's battle UI is located by scanning inward from the image edges for the
first dark-pixel region. All subsequent OCR and colour work is confined to
this frame.
"""

import os
import sys
from typing import Any

from PIL import Image


# ── Lazy EasyOCR handle ────────────────────────────────────────────

_reader: Any = None  # easyocr.Reader | None


def _get_reader(gpu: bool = False) -> Any:
    """Return the process-wide EasyOCR Reader (lazy-init)."""
    global _reader
    if _reader is not None:
        return _reader
    try:
        import easyocr
    except ImportError:
        raise RuntimeError(
            "EasyOCR is not installed. Install it with: pip install easyocr"
        )
    _reader = easyocr.Reader(["ch_sim", "en"], gpu=gpu)
    return _reader


def unload_ocr():
    """Release the in-memory OCR model (for manual memory management)."""
    global _reader
    _reader = None


# ── Frame detection ─────────────────────────────────────────────────


def _darkness(pixel: tuple) -> int:
    """Return a single-channel darkness value for a pixel (0=black, 255=white)."""
    if len(pixel) >= 3:
        return int(0.299 * pixel[0] + 0.587 * pixel[1] + 0.114 * pixel[2])
    return pixel[0] if isinstance(pixel, int) else int(pixel[0])


def detect_frame(
    image: Image.Image,
    dark_threshold: int = 60,
    min_frame_ratio: float = 0.35,
) -> tuple[int, int, int, int] | None:
    """Locate the dark central battle-info panel in *image*.

    Returns ``(left, top, right, bottom)`` in pixel coordinates, or *None* if
    no dark panel is found. The algorithm scans horizontally from the left
    edge and vertically from the top edge, looking for the first contiguous
    dark region wide enough to be the panel.

    Args:
        image: PIL Image (RGB or RGBA).
        dark_threshold: Max per-channel value (0-255) considered "dark".
        min_frame_ratio: Minimum fraction of image width/height the panel
            must occupy to be accepted.
    """
    w, h = image.size
    pixels = image.load()
    if pixels is None:
        return None

    min_w = int(w * min_frame_ratio)
    min_h = int(h * min_frame_ratio)

    # ── Left edge ──
    left = 0
    for x in range(w // 3):
        dark_count = 0
        for y in range(0, h, 2):
            if _darkness(pixels[x, y]) < dark_threshold:
                dark_count += 1
        if dark_count > h * 0.3:
            left = x
            break

    # ── Right edge ──
    right = w
    for x in range(w - 1, 2 * w // 3, -1):
        dark_count = 0
        for y in range(0, h, 2):
            if _darkness(pixels[x, y]) < dark_threshold:
                dark_count += 1
        if dark_count > h * 0.3:
            right = x
            break

    # ── Top edge ──
    top = 0
    for y in range(h // 3):
        dark_count = 0
        for x in range(left, right, 2):
            if _darkness(pixels[x, y]) < dark_threshold:
                dark_count += 1
        if dark_count > (right - left) * 0.3:
            top = y
            break

    # ── Bottom edge ──
    bottom = h
    for y in range(h - 1, 2 * h // 3, -1):
        dark_count = 0
        for x in range(left, right, 2):
            if _darkness(pixels[x, y]) < dark_threshold:
                dark_count += 1
        if dark_count > (right - left) * 0.3:
            bottom = y
            break

    frame_w = right - left
    frame_h = bottom - top
    if frame_w < min_w or frame_h < min_h:
        return None

    return (left, top, right, bottom)


# ── OCR (EasyOCR) ──────────────────────────────────────────────────


def extract_text_blocks(
    image: Image.Image,
    frame: tuple[int, int, int, int] | None = None,
) -> list[dict]:
    """Run EasyOCR on *image* and return a list of text-block dicts.

    Each dict: ``{"text": str, "bbox": [[x1,y1],[x2,y2],[x3,y3],[x4,y4]],
    "confidence": float}``.  If *frame* is given, OCR is confined to that
    region (the image is cropped first).

    Returns an empty list when EasyOCR is unavailable or finds nothing.
    """
    try:
        reader = _get_reader()
    except RuntimeError:
        print("[ocr_engine] EasyOCR not available", file=sys.stderr)
        return []

    if frame is not None:
        left, top, right, bottom = frame
        crop = image.crop((left, top, right, bottom))
    else:
        crop = image

    # easyocr.readtext wants a numpy array or a file path. PIL → numpy.
    try:
        import numpy as np
        arr = np.array(crop)
    except ImportError:
        # Fallback: save to temp file
        import tempfile
        tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        crop.save(tmp.name)
        results = reader.readtext(tmp.name)
        os.unlink(tmp.name)
        # Adjust bbox back to original coordinates
        adjusted = []
        for bbox, text, conf in results:
            if frame is not None:
                left, top, _, _ = frame
                bbox = [[p[0] + left, p[1] + top] for p in bbox]
            adjusted.append({"text": text, "bbox": bbox, "confidence": conf})
        return adjusted

    results = reader.readtext(arr)

    # Adjust bbox coordinates back to the original image if cropped
    out = []
    for bbox, text, conf in results:
        if frame is not None:
            left, top, _, _ = frame
            bbox = [[p[0] + left, p[1] + top] for p in bbox]
        out.append({"text": text, "bbox": bbox, "confidence": conf})
    return out


# ── Colour sampling for side determination ──────────────────────────


def _rgb_to_hue(r: int, g: int, b: int) -> float:
    """Return the HSV hue angle (0-360) for an RGB pixel."""
    import colorsys
    return colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)[0] * 360.0


def sample_side_colour(
    image: Image.Image,
    bbox: list,
    sample_width: int = 12,
) -> str:
    """Determine whether a text block belongs to an *ally* or *enemy*.

    Samples the horizontal strip immediately to the left and right of the
    bounding box (the character-name background in the battle UI is coloured
    red for enemies, blue for allies).  The median hue of the sampled pixels
    is compared against fixed thresholds.

    Returns one of ``"ally"``, ``"enemy"``, or ``"unknown"``.
    """
    w, h = image.size
    pixels = image.load()
    if pixels is None:
        return "unknown"

    # Bbox is EasyOCR format: [[x1,y1],[x2,y2],[x3,y3],[x4,y4]]
    # Top-left corner and bottom-right corner define the bounding rectangle.
    xs = [p[0] for p in bbox]
    ys = [p[1] for p in bbox]
    x1, x2 = int(min(xs)), int(max(xs))
    y1, y2 = int(min(ys)), int(max(ys))

    hues: list[float] = []

    # Sample left strip
    lx1 = max(0, x1 - sample_width)
    lx2 = max(0, x1)
    for x in range(lx1, lx2):
        for y in range(y1, min(y2, h)):
            try:
                p = pixels[x, y]
                if len(p) >= 3:
                    hues.append(_rgb_to_hue(p[0], p[1], p[2]))
            except (IndexError, TypeError):
                continue

    # Sample right strip
    rx1 = min(w, x2)
    rx2 = min(w, x2 + sample_width)
    for x in range(rx1, rx2):
        for y in range(y1, min(y2, h)):
            try:
                p = pixels[x, y]
                if len(p) >= 3:
                    hues.append(_rgb_to_hue(p[0], p[1], p[2]))
            except (IndexError, TypeError):
                continue

    if not hues:
        return "unknown"

    # Median hue
    hues.sort()
    median = hues[len(hues) // 2]

    # Red: 0-25° or 335-360°  |  Blue: 190-260°
    if median <= 25 or median >= 335:
        return "enemy"
    if 190 <= median <= 260:
        return "ally"
    return "unknown"
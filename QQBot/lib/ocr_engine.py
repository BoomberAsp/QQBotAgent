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

from lib.status_icons import STATUS_ICON_CN, STATUS_ICON_DIR


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


# Characteristic background colour of the central battle-info panel.
# Measured with a colour picker across different devices and brightness
# levels; per-channel variance is within ±10.
_PANEL_REF_RGB: tuple[int, int, int] = (161, 169, 182)  # #A1A9B6
_PANEL_COLOR_TOLERANCE: int = 12


def _panel_bg_match(pixel: tuple, ref: tuple, tol: int) -> bool:
    """Return True if *pixel* is within *tol* of *ref* on every channel."""
    if len(pixel) < 3:
        return False
    return all(abs(int(pixel[i]) - ref[i]) <= tol for i in range(3))


def detect_frame(
    image: Image.Image,
    dark_threshold: int = 60,
    min_frame_ratio: float = 0.35,
) -> tuple[int, int, int, int] | None:
    """Locate the central battle-info panel in *image*.

    Tries colour-based detection first (matching the panel's characteristic
    background colour #A1A9B6), then falls back to the old dark-pixel
    heuristic.  Returns ``(left, top, right, bottom)`` or *None*.
    """
    frame = _detect_frame_by_color(image, min_frame_ratio)
    if frame is not None:
        return frame
    return _detect_frame_by_darkness(image, dark_threshold, min_frame_ratio)


def _detect_frame_by_color(
    image: Image.Image,
    min_frame_ratio: float = 0.35,
) -> tuple[int, int, int, int] | None:
    """Detect the panel by matching its background colour ``#A1A9B6``.

    Scans inward from each edge; the panel boundary is the first column/row
    where at least 25% of sampled pixels match the reference colour.
    """
    w, h = image.size
    pixels = image.load()
    if pixels is None:
        return None

    ref = _PANEL_REF_RGB
    tol = _PANEL_COLOR_TOLERANCE
    min_w = int(w * min_frame_ratio)
    min_h = int(h * min_frame_ratio)

    # ── Left edge ──
    left = 0
    for x in range(w // 3):
        match = 0
        total = 0
        for y in range(0, h, 2):
            total += 1
            if _panel_bg_match(pixels[x, y], ref, tol):
                match += 1
        if total > 0 and match / total >= 0.25:
            left = x
            break

    if left == 0:
        return None  # panel not found

    # ── Right edge ──
    right = w
    for x in range(w - 1, 2 * w // 3, -1):
        match = 0
        total = 0
        for y in range(0, h, 2):
            total += 1
            if _panel_bg_match(pixels[x, y], ref, tol):
                match += 1
        if total > 0 and match / total >= 0.25:
            right = x
            break

    # ── Top edge ──
    top = 0
    for y in range(h // 3):
        match = 0
        total = 0
        for x in range(left, right, 2):
            total += 1
            if _panel_bg_match(pixels[x, y], ref, tol):
                match += 1
        if total > 0 and match / total >= 0.25:
            top = y
            break

    # ── Bottom edge ──
    bottom = h
    for y in range(h - 1, 2 * h // 3, -1):
        match = 0
        total = 0
        for x in range(left, right, 2):
            total += 1
            if _panel_bg_match(pixels[x, y], ref, tol):
                match += 1
        if total > 0 and match / total >= 0.25:
            bottom = y
            break

    frame_w = right - left
    frame_h = bottom - top
    if frame_w < min_w or frame_h < min_h:
        return None

    return (left, top, right, bottom)


def _detect_frame_by_darkness(
    image: Image.Image,
    dark_threshold: int = 60,
    min_frame_ratio: float = 0.35,
) -> tuple[int, int, int, int] | None:
    """Fallback: detect the panel by scanning for dark-pixel columns/rows.

    Kept for screenshots where the background colour heuristic fails
    (e.g. extremely bright or tinted backgrounds).
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

    # If the "frame" is essentially the whole image the heuristic didn't
    # find any edges — the panel is not darker than the background.
    if frame_w >= w * 0.95 and frame_h >= h * 0.95:
        return None

    return (left, top, right, bottom)


# ── OCR (EasyOCR) ──────────────────────────────────────────────────


def extract_text_blocks(
    image: Image.Image,
    frame: tuple[int, int, int, int] | None = None,
    allowlist: str | None = None,
) -> list[dict]:
    """Run EasyOCR on *image* and return a list of text-block dicts.

    Each dict: ``{"text": str, "bbox": [[x1,y1],[x2,y2],[x3,y3],[x4,y4]],
    "confidence": float}``.  If *frame* is given, OCR is confined to that
    region (the image is cropped first).

    If *allowlist* is given, only characters in the allowlist are recognised
    (e.g. ``"0123456789%"`` for action-value regions).

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
        kwargs = {}
        if allowlist:
            kwargs["allowlist"] = allowlist
        results = reader.readtext(tmp.name, **kwargs)
        os.unlink(tmp.name)
        # Adjust bbox back to original coordinates
        adjusted = []
        for bbox, text, conf in results:
            if frame is not None:
                left, top, _, _ = frame
                bbox = [[p[0] + left, p[1] + top] for p in bbox]
            adjusted.append({"text": text, "bbox": bbox, "confidence": conf})
        return adjusted

    kwargs = {}
    if allowlist:
        kwargs["allowlist"] = allowlist
    results = reader.readtext(arr, **kwargs)

    # Adjust bbox coordinates back to the original image if cropped
    out = []
    for bbox, text, conf in results:
        if frame is not None:
            left, top, _, _ = frame
            bbox = [[p[0] + left, p[1] + top] for p in bbox]
        out.append({"text": text, "bbox": bbox, "confidence": conf})
    return out


# ── Glyph rendering & visual similarity ────────────────────────────

_FONT_PATH = os.path.join(
    os.path.dirname(__file__), "..", "config", "font", "NotoSansSC-SemiBold.ttf"
)


def render_glyph_mask(char: str, height: int = 40):
    """Render a single character as a binary ink mask, normalised to *height*.

    Returns a uint8 0/1 numpy array cropped tight to the glyph ink.
    Used for visual (stroke-level) similarity matching of OCR-misread
    characters — pinyin is deliberately NOT used, since OCR errors are
    visual, not phonetic.
    """
    import numpy as np
    from PIL import ImageDraw, ImageFont

    font = ImageFont.truetype(_FONT_PATH, 60)
    img = Image.new("L", (80, 90), 0)
    ImageDraw.Draw(img).text((5, 5), char, fill=255, font=font)
    a = np.array(img)
    ys, xs = np.where(a > 100)
    if len(ys) == 0:
        return np.zeros((height, 1), dtype=np.uint8)
    a = a[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    mask = (a > 100).astype(np.uint8)
    return normalize_mask(mask, height)


def normalize_mask(mask, height: int = 40):
    """Resize a binary mask to *height* preserving aspect ratio."""
    import cv2
    import numpy as np

    if mask.shape[0] == 0 or mask.shape[1] == 0:
        return np.zeros((height, 1), dtype=np.uint8)
    w = max(1, int(mask.shape[1] * height / mask.shape[0]))
    return cv2.resize(mask, (w, height), interpolation=cv2.INTER_NEAREST)


def stroke_iou(a, b) -> float:
    """Dilation-tolerant IoU between two binary glyph masks.

    Both masks are dilated by a 3x3 kernel before intersection/union so
    that 1-pixel stroke offsets don't destroy the score. Widths are
    centre-padded to match.
    """
    import cv2
    import numpy as np

    w = max(a.shape[1], b.shape[1])
    if a.shape[1] < w:
        pad = (w - a.shape[1]) // 2
        a = np.pad(a, ((0, 0), (pad, w - a.shape[1] - pad)))
    if b.shape[1] < w:
        pad = (w - b.shape[1]) // 2
        b = np.pad(b, ((0, 0), (pad, w - b.shape[1] - pad)))
    k = np.ones((3, 3), np.uint8)
    da, db = cv2.dilate(a, k), cv2.dilate(b, k)
    inter = np.logical_and(da, db).sum()
    union = np.logical_or(da, db).sum()
    return inter / union if union else 0.0


# ── Colour sampling for side determination ──────────────────────────


def _rgb_to_hue(r: int, g: int, b: int) -> float:
    """Return the HSV hue angle (0-360) for an RGB pixel."""
    import colorsys
    return colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)[0] * 360.0


# ── Buff / Debuff Icon Detection ────────────────────────────────────


class BuffDetector:
    """Detect buff/debuff icons in battle screenshots via template matching.

    Uses multi-scale cv2.matchTemplate (TM_CCOEFF_NORMED) against reference
    icons stored in ``images/cal-speed-data/``.  Reference icons are loaded
    lazily on first use and cached for the lifetime of the process.

    Typical usage::

        detector = BuffDetector()
        buffs = detector.detect_in_rows(image, rows, frame)
        # buffs: list[dict] — one per row, keys: {icon_name: confidence}
    """

    # Icons that indicate the battle has already started (entry buffs, morale,
    # immunity gear).  Other icons (debuffs, etc.) are also detected but these
    # are the ones the validity check cares about.
    _ICON_DIR = os.path.join(
        os.path.dirname(__file__), "..", "images", "cal-speed-data"
    )

    # Minimum correlation coefficient for a positive match (0.0–1.0).
    MATCH_THRESHOLD = 0.70

    # Scale range for multi-scale matching (relative to the template's native
    # size).  Game screenshots vary in resolution; buff icons are typically
    # 20–48 px in the central panel.
    SCALES = [0.5, 0.6, 0.7, 0.8, 0.9, 1.0, 1.1, 1.2, 1.3, 1.4, 1.5]

    def __init__(self, icon_dir: str | None = None):
        self._icon_dir = icon_dir or self._ICON_DIR
        self._templates: dict[str, Any] = {}   # name → BGR ndarray
        self._loaded = False

    # ── Template loading ──────────────────────────────────────────

    def _load_templates(self):
        """Load all reference icon PNGs into memory (idempotent)."""
        if self._loaded:
            return
        try:
            import cv2
        except ImportError:
            print("[BuffDetector] OpenCV not available", file=sys.stderr)
            self._loaded = True
            return

        if not os.path.isdir(self._icon_dir):
            print(f"[BuffDetector] icon dir not found: {self._icon_dir}", file=sys.stderr)
            self._loaded = True
            return

        for fname in sorted(os.listdir(self._icon_dir)):
            if not fname.endswith(".png"):
                continue
            # Only load reference icons (named "*图标.png"), not screenshots
            # that happen to be in the same directory.
            if "图标" not in fname:
                continue
            path = os.path.join(self._icon_dir, fname)
            img = cv2.imread(path, cv2.IMREAD_COLOR)
            if img is not None:
                # Strip the "图标" suffix for shorter keys
                name = fname.replace("图标.png", "").replace(".png", "")
                self._templates[name] = img

        # Second icon source: wiki status icons (downloaded by wiki_scraper).
        # Keyed by their Chinese label via STATUS_ICON_CN; loaded after the
        # manual icons so a wiki icon covers the same-named manual icon.
        if os.path.isdir(STATUS_ICON_DIR):
            for fname in sorted(os.listdir(STATUS_ICON_DIR)):
                if not fname.endswith(".png"):
                    continue
                cn = STATUS_ICON_CN.get(fname)
                if not cn:
                    continue
                path = os.path.join(STATUS_ICON_DIR, fname)
                img = BuffDetector._read_icon(path)
                if img is not None:
                    self._templates[cn] = img

        self._loaded = True
        if self._templates:
            print(f"[BuffDetector] Loaded {len(self._templates)} reference icons",
                  file=sys.stderr)

    @staticmethod
    def _read_icon(path: str):
        """Read an icon PNG as a BGR ndarray, handling an alpha channel.

        ``cv2.IMREAD_COLOR`` drops alpha and turns transparent pixels black,
        which skews ``TM_CCOEFF_NORMED`` against the in-game rendering. A 4-
        channel icon is therefore alpha-composited onto a neutral mid-gray
        background first (constant offsets are subtracted out by the
        coefficient normalisation, but the anti-aliased edges stay correct).
        """
        try:
            import cv2
        except ImportError:
            return None
        img = cv2.imread(path, cv2.IMREAD_UNCHANGED)
        if img is None:
            return None
        if img.ndim == 3 and img.shape[2] == 4:
            import numpy as np
            bgr = img[:, :, :3].astype(np.float32)
            alpha = img[:, :, 3:4].astype(np.float32) / 255.0
            background = np.full_like(bgr, 128.0)
            img = (bgr * alpha + background * (1.0 - alpha)).astype(np.uint8)
        elif img.ndim == 2:
            img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
        return img

    @property
    def template_names(self) -> list[str]:
        """Return the list of loaded icon names (e.g. ``['免疫', '气魄', ...]``)."""
        self._load_templates()
        return sorted(self._templates.keys())

    # ── Multi-scale matching ──────────────────────────────────────

    @staticmethod
    def _match_template_multiscale(
        roi: Any,
        template: Any,
        scales: list[float] | None = None,
        threshold: float | None = None,
    ) -> float:
        """Return the best TM_CCOEFF_NORMED correlation across scales (0.0–1.0)."""
        try:
            import cv2
        except ImportError:
            return 0.0

        if scales is None:
            scales = BuffDetector.SCALES
        if threshold is None:
            threshold = BuffDetector.MATCH_THRESHOLD

        roi_h, roi_w = roi.shape[:2]
        t_h, t_w = template.shape[:2]

        best = 0.0
        for scale in scales:
            new_w, new_h = int(t_w * scale), int(t_h * scale)
            if new_w < 5 or new_h < 5:
                continue
            if new_w > roi_w or new_h > roi_h:
                continue
            if new_w > roi_w * 0.8 and new_h > roi_h * 0.8:
                # The template is nearly as large as the ROI — unlikely to be
                # a buff icon, skip to avoid false positives.
                continue

            try:
                scaled = cv2.resize(template, (new_w, new_h))
            except cv2.error:
                continue

            result = cv2.matchTemplate(roi, scaled, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, _ = cv2.minMaxLoc(result)

            if max_val > best:
                best = max_val
                if best >= 0.95:  # early exit on near-perfect match
                    break

        return float(best)

    # ── Region-based detection ────────────────────────────────────

    def detect_in_region(
        self,
        image: Any,
        region: tuple[int, int, int, int],
    ) -> dict[str, float]:
        """Detect buff icons in a rectangular *region* of *image*.

        Args:
            image: BGR numpy array (full screenshot).
            region: ``(x1, y1, x2, y2)`` in pixel coordinates.

        Returns:
            ``{icon_name: confidence}`` for icons whose best correlation
            exceeds ``MATCH_THRESHOLD``.
        """
        self._load_templates()
        if not self._templates:
            return {}

        try:
            import cv2
        except ImportError:
            return {}

        x1, y1, x2, y2 = region
        # Clamp to image bounds
        h, w = image.shape[:2]
        x1 = max(0, min(x1, w))
        y1 = max(0, min(y1, h))
        x2 = max(0, min(x2, w))
        y2 = max(0, min(y2, h))

        if x2 <= x1 or y2 <= y1:
            return {}

        roi = image[y1:y2, x1:x2]

        results: dict[str, float] = {}
        for name, template in self._templates.items():
            conf = self._match_template_multiscale(roi, template)
            if conf >= self.MATCH_THRESHOLD:
                results[name] = conf

        return results

    # ── Row-based detection ───────────────────────────────────────

    def detect_in_rows(
        self,
        image: Any,
        rows: list[list[dict]],
        frame: tuple[int, int, int, int] | None = None,
    ) -> list[dict[str, float]]:
        """Detect buff icons in each character row.

        Each row is a list of block dicts carrying a ``bbox`` (from the
        battle parser's banner-band regions).  The row region spans the full
        horizontal width of *frame* (or image) and, vertically, the row's
        Voronoi cell: from the midpoint between this row's band and the
        previous row's band down to the midpoint with the next row's band
        (frame edges for the first/last row).

        The cell (not band±padding) is essential: buff icons are taller than
        the OCR text bands (a 47px icon on a 30px band, protruding ~27px
        below it), and ``TM_CCOEFF_NORMED`` collapses (0.92→0.65) when even a
        single icon row is clipped.  Cells tile the panel, so every icon is
        fully inside exactly one row's region.

        Args:
            image: BGR numpy array (full screenshot).
            rows: Per-row block lists (each block has a ``bbox`` field).
            frame: Optional ``(l, t, r, b)`` of the central panel.

        Returns:
            A list parallel to *rows*; each element is ``{icon_name: confidence}``
            for the buffs detected in that row.
        """
        h, w = image.shape[:2]
        left, right = 0, w
        top_edge, bot_edge = 0, h
        if frame is not None:
            left, top_edge, right, bot_edge = frame

        bands: list[tuple[int, int] | None] = []
        for row_blocks in rows:
            if not row_blocks:
                bands.append(None)
                continue
            ys = [p[1] for b in row_blocks for p in b["bbox"]]
            bands.append((min(ys), max(ys)))

        order = sorted(
            (i for i, b in enumerate(bands) if b is not None),
            key=lambda i: (bands[i][0] + bands[i][1]) / 2,
        )
        cells: dict[int, tuple[int, int]] = {}
        for pos, i in enumerate(order):
            y1 = top_edge if pos == 0 else (
                bands[order[pos - 1]][1] + bands[i][0]) / 2
            y2 = bot_edge if pos == len(order) - 1 else (
                bands[i][1] + bands[order[pos + 1]][0]) / 2
            cells[i] = (int(max(top_edge, y1)), int(min(bot_edge, y2)))

        results: list[dict[str, float]] = []
        for i, row_blocks in enumerate(rows):
            if not row_blocks or i not in cells:
                results.append({})
                continue
            y1, y2 = cells[i]
            region = (left, y1, right, y2)
            results.append(self.detect_in_region(image, region))

        return results


# ── Skill Cooldown (grayed icon) Detection ──────────────────────────


class SkillCooldownDetector:
    """Detect whether skill icons are on cooldown (grayed out).

    Ark Re:Code renders an on-cooldown skill as a desaturated (gray) version
    of its icon, optionally with an ``N回合`` overlay; an available skill keeps
    its full colour.  The core signal is therefore **saturation**: a grayed
    icon has the three RGB channels nearly equal, a coloured icon has a large
    max−min channel spread.

    The detector is colour- and layout-agnostic: it takes an explicit list of
    icon *cells* (rectangles ``(x1, y1, x2, y2)``) and returns a per-cell
    ``{saturation, grayed}`` verdict.  Cell positions are resolution / UI
    specific and must be calibrated on a real screenshot before relying on the
    verdicts — use :meth:`sample_cells` to tune ``GRAY_SAT_THRESHOLD``.

    This is the observation half of L2 (Phase B2 of
    ``docs/speedcheck-trigger-correction.md``).  When the cells are not yet
    calibrated the caller falls back to the L2 *prediction* (AI rules).

    Caveats (game knowledge, 2026-08-22): a darkened icon is NOT proof of a
    cooldown — 沉默类控制 (skill-lock CC) and 战意/集中力不足 (unpayable cost)
    dim skills the same way, and the basic skill (S1) never has a cooldown.
    Callers must therefore discard slot-0 verdicts and, when a pre-battle
    screenshot is available, treat only slots that *turned* dark between pre
    and post as fresh cooldowns (cost/CC dimming is already present pre).
    """

    # Mean max−min channel spread (0–255) below which a cell is judged grayed.
    # The cooldown overlay ("N回合") renders as a dark brown/red octagon with
    # saturation up to ~52 (measured 15–52 on real casts); colourful icons sit
    # at ~41+ *but* stay bright, so saturation alone is only half the signal.
    # Tune with sample_cells() against a real post-cast screenshot.
    GRAY_SAT_THRESHOLD = 60.0

    # Mean per-pixel max-channel brightness below which a cell is judged
    # darkened.  This is the primary discriminator: cooldown overlays measure
    # val ≤ ~119 while colourful icons (even dark-purple art) measure ≥ ~129.
    # Combined with GRAY_SAT_THRESHOLD a bright low-sat cell is never grayed.
    GRAY_VAL_THRESHOLD = 120.0

    # Horizontal position of the three skill slots (S1/S2/S3) as fractions of
    # the detected frame width.  The skill strip sits between the action value
    # and the buff icons; icons span ~0.42-0.49 / 0.51-0.585 / 0.60-0.67 of the
    # frame.  Validated as frame-relative on 1610/2532/2688-px-wide captures.
    SKILL_SLOT_X = [(0.415, 0.478), (0.505, 0.568), (0.595, 0.658)]

    @classmethod
    def cell_stats(
        cls,
        image: Any,
        cell: tuple[int, int, int, int],
    ) -> tuple[float, float] | None:
        """Return ``(saturation, value)`` over *cell*, or None if empty/bounds."""
        import numpy as np

        h, w = image.shape[:2]
        x1, y1, x2, y2 = cell
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(w, x2), min(h, y2)
        if x2 <= x1 or y2 <= y1:
            return None
        roi = image[y1:y2, x1:x2].astype(np.int32)
        if roi.size == 0:
            return None
        mx = roi.max(axis=2)
        mn = roi.min(axis=2)
        return float((mx - mn).mean()), float(mx.mean())

    # Backwards-compatible alias.
    cell_saturation = cell_stats

    @classmethod
    def is_grayed(cls, saturation: float | None, value: float | None = None) -> bool:
        """True when a cell is desaturated (and, if *value* given, darkened)."""
        if saturation is None:
            return False
        if value is None:
            return saturation < cls.GRAY_SAT_THRESHOLD
        return (saturation < cls.GRAY_SAT_THRESHOLD
                and value < cls.GRAY_VAL_THRESHOLD)

    @classmethod
    def detect(
        cls,
        image: Any,
        cells: list[tuple[int, int, int, int]],
    ) -> list[dict]:
        """Return per-cell ``{saturation, value, grayed}`` (parallel to *cells*)."""
        out: list[dict] = []
        for cell in cells:
            stats = cls.cell_stats(image, cell)
            sat, val = stats if stats else (None, None)
            out.append({"saturation": sat, "value": val,
                        "grayed": cls.is_grayed(sat, val)})
        return out

    @classmethod
    def detect_row_skills(
        cls,
        image: Any,
        frame: tuple[int, int, int, int],
        y0: int,
        y1: int,
        slots: list[tuple[float, float]] | None = None,
    ) -> list[dict]:
        """Detect the S1/S2/S3 cooldown state for one character row.

        *frame* is the detected panel; *y0*/*y1* the row's vertical band.  The
        three skill cells are placed at :data:`SKILL_SLOT_X` fractions of the
        frame width.  Returns a length-3 list of ``{saturation, value,
        grayed}`` (index 0/1/2 = S1/S2/S3).
        """
        left, _, right, _ = frame
        fw = right - left
        slots = slots or cls.SKILL_SLOT_X
        out: list[dict] = []
        for x0f, x1f in slots:
            cell = (left + int(fw * x0f), y0 + 2, left + int(fw * x1f), y1 - 2)
            stats = cls.cell_stats(image, cell)
            sat, val = stats if stats else (None, None)
            out.append({"saturation": sat, "value": val,
                        "grayed": cls.is_grayed(sat, val)})
        return out

    @classmethod
    def cooldown_slot(cls, row_skills: list[dict]) -> int | None:
        """Return the single on-cooldown slot index (0/1/2), or None.

        Only a *unique* grayed slot is trusted; zero or multiple grayed cells
        mean the observation is ambiguous and the caller should fall back to
        the L2 prediction.
        """
        grayed = [i for i, s in enumerate(row_skills) if s["grayed"]]
        return grayed[0] if len(grayed) == 1 else None

    @classmethod
    def sample_cells(
        cls,
        image: Any,
        cells: dict[str, tuple[int, int, int, int]],
    ) -> list[dict]:
        """Calibration helper: dump per-cell saturation/value for tuning."""
        out: list[dict] = []
        for name, cell in cells.items():
            stats = cls.cell_stats(image, cell)
            sat, val = stats if stats else (None, None)
            out.append({
                "name": name,
                "cell": cell,
                "saturation": sat,
                "value": val,
                "grayed": cls.is_grayed(sat, val),
            })
        return out


# ── Morale / Focus Bar Detection ────────────────────────────────────


class MoraleFocusDetector:
    """Read the 战意/集中力 resource bar rendered under each row's HP bar.

    Verified pixel layout (see ``test/test_morale_focus.py`` fixtures;
    labelled visually 2026-08-22):

    * every row card ends with a faction stripe directly below the green
      HP bar — blue-ish for allies, red-ish for enemies (bottom edge of
      the side-coloured banner plate);
    * characters with a resource mechanic render a 5-segment bar a couple
      of pixels below that stripe (≈7–9 px tall at 1080p, slightly
      narrower than the HP bar), filled strictly left-to-right:

      - 战意 (morale): filled cells bright azure ``RGB ~(16-54,96-148,
        240-255)`` on a dark navy empty background ``RGB ~(16,0,64-96)``;
        5 cells = 100 morale, half-cell = 10;
      - 集中力 (focus): filled cells pink ``RGB ~(192-255,64-96,144-224)``
        on a near-black maroon background ``RGB ~(32,0,16-32)``;
        5 cells = 5 focus.

    The empty background colour identifies the resource kind even at zero
    fill (navy → morale, maroon → focus).  Rows of characters without the
    mechanic render no bar below the stripe (``kind=None``).

    Note: the input image is an OpenCV **BGR** array.
    """

    # mask parameters tuned on the checked-in cal-speed-data screenshots
    _MIN_BAR_COLS = 100     # real bars span 140–370 px (1x/2x renders)
    _MIN_BAR_ROWS = 4       # real bars span 7–18 rows
    _MIN_ROW_PIX = 40       # per-row bar-pixel count to qualify

    @classmethod
    def detect_in_rows(
        cls,
        image: Any,
        rows: list[list[dict]],
        frame: tuple[int, int, int, int],
    ) -> list[dict]:
        """Return per-row ``{"kind": "morale"|"focus"|None, "cells": float|None}``.

        *rows* mirrors ``BuffDetector.detect_in_rows`` (per-row block lists
        with ``bbox`` fields).  The HP-bar search is confined to each row's
        Voronoi cell (midpoint-to-midpoint between neighbouring bands,
        frame edges for first/last): OCR bands can overlap the neighbour
        card, and an unconfined search would latch onto the neighbour's HP
        bar and attribute its resource value to the wrong character.
        """
        h, w = image.shape[:2]
        left, top_edge, right, bot_edge = frame

        bands: list[tuple[int, int] | None] = []
        for row_blocks in rows:
            if not row_blocks:
                bands.append(None)
                continue
            ys = [p[1] for b in row_blocks for p in b["bbox"]]
            bands.append((min(ys), max(ys)))

        order = sorted(
            (i for i, b in enumerate(bands) if b is not None),
            key=lambda i: (bands[i][0] + bands[i][1]) / 2,
        )
        cells: dict[int, tuple[int, int]] = {}
        for pos, i in enumerate(order):
            y1 = top_edge if pos == 0 else (
                bands[order[pos - 1]][1] + bands[i][0]) / 2
            y2 = bot_edge if pos == len(order) - 1 else (
                bands[i][1] + bands[order[pos + 1]][0]) / 2
            cells[i] = (int(max(top_edge, y1)), int(min(bot_edge, y2)))

        x2 = left + int((right - left) * 0.45)
        results: list[dict] = []
        for i, row_blocks in enumerate(rows):
            if not row_blocks or i not in cells:
                results.append({"kind": None, "cells": None})
                continue
            cy1, cy2 = cells[i]
            results.append(cls._detect_cell(image, cy1, cy2, left, x2))
        return results

    @classmethod
    def _detect_cell(
        cls,
        image: Any,
        y1: int,
        y2: int,
        x1: int,
        x2: int,
    ) -> dict:
        """Classify the resource bar below the HP run inside one row cell.

        The HP anchor is confined to the cell, but the strip below it may
        bleed ~20 px past the cell bottom: with tight row spacing the bar
        (which protrudes ~12 px below the OCR band) can lie below the
        Voronoi midpoint while still belonging to this row.
        """
        import numpy as np

        h = image.shape[0]
        cell_h = y2 - y1
        y2e = min(h, y2 + 20)
        roi = image[y1:y2e, x1:x2].astype(np.int32)
        if roi.size == 0:
            return {"kind": None, "cells": None}
        # cv2 loads BGR — keep semantic names
        B, G, R = roi[:, :, 0], roi[:, :, 1], roi[:, :, 2]
        green = (G > 150) & (R < 120) & (B < 160)
        counts = green[:cell_h].sum(axis=1)
        if counts.size == 0 or counts.max() < 20:
            return {"kind": None, "cells": None}

        thr = max(20, counts.max() * 0.5)
        ok = counts >= thr
        runs = []
        i = 0
        while i < len(ok):
            if ok[i]:
                j = i
                while j + 1 < len(ok) and ok[j + 1]:
                    j += 1
                runs.append([i, j])
                i = j + 1
            else:
                i += 1
        # merge runs split by ≤4 px gaps (HP bars show 2 px seams)
        merged = [runs[0]]
        for s_, e_ in runs[1:]:
            if s_ - merged[-1][1] <= 4:
                merged[-1][1] = e_
            else:
                merged.append([s_, e_])
        merged.sort(key=lambda t: -(t[1] - t[0]))
        ms, me = merged[0]
        if me - ms + 1 < 4:
            return {"kind": None, "cells": None}

        xs = np.where(green[ms:me + 1].any(axis=0))[0]
        gx0, gx1 = int(xs.min()), int(xs.max())
        gw = gx1 - gx0 + 1

        fill = (B >= 190) & (G >= 55) & (R <= 90)                  # azure
        navy = (R <= 45) & (G <= 45) & (B >= 40) & (B <= 135)      # morale bg
        pink = (R >= 140) & (G <= 125) & (B >= 110) & (R >= B - 60)
        marn = (R >= 12) & (R <= 85) & (G <= 35) & (B <= 50)       # focus bg
        rimb = (R <= 80) & (G >= 55) & (G <= 115) & (B >= 95) & (B <= 195)
        rimr = (R >= 60) & (R <= 160) & (G <= 55) & (B <= 85) & (R >= B)

        reg = slice(me + 1, min(roi.shape[0], me + 19))
        sub = {k: v[reg, gx0:gx1 + 1] for k, v in (
            ("fill", fill), ("navy", navy), ("pink", pink),
            ("marn", marn), ("rimb", rimb), ("rimr", rimr))}
        bar_rows: list[int] = []
        for rr in range(sub["fill"].shape[0]):
            c = {k: int(sub[k][rr].sum()) for k in sub}
            if max(c["rimb"], c["rimr"]) >= 0.5 * gw:
                continue  # faction stripe under the HP bar, not the resource
            bar_like = c["fill"] + c["navy"] + c["pink"] + c["marn"]
            if bar_like >= cls._MIN_ROW_PIX:
                bar_rows.append(rr)
        if len(bar_rows) < cls._MIN_BAR_ROWS:
            return {"kind": None, "cells": None}

        fb = sub["fill"][bar_rows].any(axis=0)
        pk = sub["pink"][bar_rows].any(axis=0)
        nv = sub["navy"][bar_rows].any(axis=0)
        mr = sub["marn"][bar_rows].any(axis=0)
        # Dark stray pixels (card shadows, banner edges) match the bg masks
        # far from the bar; keep only the largest contiguous column run
        # (gap ≤4 bridges the fill/empty border seam).
        idx = np.where(fb | pk | nv | mr)[0]
        run_s = run_e = best_s = best_e = int(idx[0])
        for a, b in zip(idx, idx[1:]):
            if b - a <= 4:
                run_e = int(b)
            else:
                if run_e - run_s > best_e - best_s:
                    best_s, best_e = run_s, run_e
                run_s = run_e = int(b)
        if run_e - run_s > best_e - best_s:
            best_s, best_e = run_s, run_e
        if best_e - best_s + 1 < cls._MIN_BAR_COLS:
            return {"kind": None, "cells": None}
        seg = slice(best_s, best_e + 1)
        fill_mask = fb | pk
        empty_mask = (nv | mr) & ~fill_mask
        n_fill, n_empty = (int(fill_mask[seg].sum()),
                           int(empty_mask[seg].sum()))
        if n_fill + n_empty < cls._MIN_BAR_COLS:
            return {"kind": None, "cells": None}

        if fb.any() and not pk.any():
            kind = "morale"
        elif pk.any() and not fb.any():
            kind = "focus"
        else:  # empty bar: background colour decides (vote over the middle
            # rows — the bar's top/bottom border rows tint towards navy)
            mid = bar_rows[1:-1] if len(bar_rows) >= 3 else bar_rows
            c_navy = int(sub["navy"][mid].sum())
            c_marn = int(sub["marn"][mid].sum())
            kind = "morale" if c_navy >= c_marn else "focus"
        frac = n_fill / (n_fill + n_empty)
        cells = int(frac * 10 + 0.5) / 2
        return {"kind": kind, "cells": cells}


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


# ── Calibration CLI (skill cooldown grayed detection) ───────────────

if __name__ == "__main__":
    import argparse

    import numpy as np

    ap = argparse.ArgumentParser(
        description="Calibrate SkillCooldownDetector (grayed skill icons)."
    )
    ap.add_argument("image", help="battle screenshot path")
    ap.add_argument(
        "--cells",
        action="append",
        default=[],
        help="cell spec 'x1,y1,x2,y2' (repeatable); default = 3x3 grid over "
             "the right half of the detected frame",
    )
    args = ap.parse_args()

    im = Image.open(args.image).convert("RGB")
    arr = np.array(im)
    frame = detect_frame(im)
    print("frame:", frame)

    if args.cells:
        cells = {}
        for i, spec in enumerate(args.cells):
            parts = [int(v) for v in spec.split(",")]
            if len(parts) != 4:
                raise SystemExit(f"bad cell spec: {spec}")
            cells[f"cell{i}"] = tuple(parts)  # type: ignore[arg-type]
    else:
        if frame is None:
            raise SystemExit("no frame detected")
        left, top, right, bottom = frame
        cells = {}
        for gy in range(3):
            for gx in range(3):
                cx0 = left + (right - left) // 2 + gx * (right - left) // 2 // 3
                cx1 = cx0 + (right - left) // 2 // 3 - 2
                cy0 = top + gy * (bottom - top) // 3
                cy1 = cy0 + (bottom - top) // 3 - 2
                cells[f"g{gy}{gx}"] = (cx0, cy0, cx1, cy1)

    for row in SkillCooldownDetector.sample_cells(arr, cells):
        sat = row["saturation"]
        print(
            f"  {row['name']:10s} {row['cell']}  "
            f"sat={sat and round(sat, 1)}  grayed={row['grayed']}"
        )
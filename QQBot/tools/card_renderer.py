"""
Card Renderer — Render Ark Re:Code character detail cards as PNG.

Uses PIL to lay out a character detail entry (portrait/icon/skill icons + text)
for direct image display in QQ chat via ``/角色详情`` and the ``character_detail``
tool.

Layout (1080px wide, height adaptive, three stacked bands):

    ┌───────────────────────────────────────────────┐
    │  [portrait]   [icon] 名称 ★★★★                 │
    │               属性 · 职业 · 星座  ID            │
    │               身高/体重/生日/胸围                │
    │               简介 ...                          │
    ├───────────────────────────────────────────────┤
    │  面板 · 天赋 · 潜能                              │
    ├───────────────────────────────────────────────┤
    │  [s1] 技能1  [s2] 技能2  [s3] 技能3             │
    └───────────────────────────────────────────────┘

Missing images degrade to element-coloured placeholder tiles, so rendering
never blocks on network state.
"""

import hashlib
import json
import os
import threading

from PIL import Image, ImageDraw, ImageFont


# ── Paths ────────────────────────────────────────────────────────

_FONT_DIR = os.path.join(os.path.dirname(__file__), "..", "config", "font")
_FONT_MAIN = os.path.join(_FONT_DIR, "NotoSansSC-SemiBold.ttf")

_CACHE_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "wiki_cache")
_PORTRAIT_DIR = os.path.join(_CACHE_DIR, "portraits")
_ICON_DIR = os.path.join(_CACHE_DIR, "icons")
_SKILL_DIR = os.path.join(_CACHE_DIR, "skill_icons")
_CARD_DIR = os.path.join(_CACHE_DIR, "cards")
_BOND_ICON_DIR = os.path.join(_CACHE_DIR, "bond_icons")
_BOND_CARD_DIR = os.path.join(_CACHE_DIR, "bond_cards")

# Per-directory sidecar mapping card filename → content hash of the entry it
# was rendered from. Lets renderers skip re-drawing cards whose data is
# unchanged (the wiki_scraper stamps each entry with a ``_card_hash``).
_CARD_HASH_FILE = os.path.join(_CARD_DIR, "_hashes.json")
_BOND_CARD_HASH_FILE = os.path.join(_BOND_CARD_DIR, "_hashes.json")


# ── Palette ──────────────────────────────────────────────────────

ELEMENT_COLOR = {
    "火": (232, 80, 58),
    "水": (58, 140, 232),
    "木": (76, 175, 80),
    "光": (232, 181, 58),
    "暗": (122, 92, 232),
}
_DEFAULT_ELEMENT_COLOR = (110, 116, 132)

# Bond class accent colors (bonds have Class, not Element).
CLASS_COLOR = {
    "Warrior": (232, 80, 58),
    "Caster": (122, 92, 232),
    "Defender": (58, 140, 232),
    "Medic": (76, 175, 80),
    "Sniper": (232, 181, 58),
    "Vanguard": (110, 116, 132),
}

_BG = (27, 30, 39)              # card background
_PANEL = (36, 40, 52)           # raised panel
_PANEL_2 = (31, 34, 44)         # inner panel
_LINE = (52, 58, 72)            # divider
_TEXT = (242, 244, 248)         # primary text
_TEXT_DIM = (180, 186, 200)     # body text
_TEXT_FAINT = (128, 136, 152)   # meta text
_STAR = (255, 213, 74)          # gold star
_BURST = (255, 158, 74)         # burst orange

# ── Layout ───────────────────────────────────────────────────────

W = 1080
PAD = 40
GAP = 26
PORTRAIT_W = 400
PORTRAIT_H = int(PORTRAIT_W * 1503 / 1733)  # wiki portrait is near-square
ICON_SIZE = 64
SKILL_ICON = 48
BOND_ICON_SIZE = 96


_fonts: dict = {}


def _font(size: int) -> ImageFont.FreeTypeFont:
    if size not in _fonts:
        _fonts[size] = ImageFont.truetype(_FONT_MAIN, size)
    return _fonts[size]


def _tw(draw: ImageDraw.ImageDraw, text: str, size: int) -> float:
    return draw.textlength(text, font=_font(size))


def _lh(size: int) -> int:
    ascent, descent = _font(size).getmetrics()
    return ascent + descent


def _wrap(draw: ImageDraw.ImageDraw, text: str, size: int, max_width: float) -> list:
    """Greedy pixel-width line wrap, newline-aware."""
    if not text:
        return []
    font = _font(size)
    lines = []
    for raw in str(text).split("\n"):
        cur = ""
        for ch in raw:
            if draw.textlength(cur + ch, font=font) <= max_width:
                cur += ch
            else:
                if cur:
                    lines.append(cur)
                cur = ch
        if cur:
            lines.append(cur)
    return lines


# ── Image helpers ────────────────────────────────────────────────

def _load_scaled(path: str, w: int, h: int | None = None,
                 keep_aspect: bool = True) -> Image.Image | None:
    """Load an image and resize it to *w* × *h* (or auto-height if *h* is None).

    When *keep_aspect* is True and both dimensions are given, the image is
    scaled to **fit within** the box while preserving the original aspect
    ratio (the resulting image may be smaller than *w* or *h* in one axis).
    Set *keep_aspect* to False to force an exact stretch (used for icons
    that are later masked to circles).
    """
    if not os.path.exists(path):
        return None
    try:
        im = Image.open(path).convert("RGBA")
        if h is None:
            h = int(im.height * w / im.width)
        elif keep_aspect:
            scale = min(w / im.width, h / im.height)
            w = int(im.width * scale)
            h = int(im.height * scale)
        return im.resize((w, h), Image.LANCZOS)
    except Exception:
        return None


def _placeholder(w: int, h: int, color: tuple, label: str = "") -> Image.Image:
    im = Image.new("RGBA", (w, h), color + (255,))
    if label:
        d = ImageDraw.Draw(im)
        size = max(16, min(h // 2, 40))
        f = _font(size)
        bbox = d.textbbox((0, 0), label, font=f)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        d.text(((w - tw) / 2 - bbox[0], (h - th) / 2 - bbox[1]),
               label, font=f, fill=(255, 255, 255, 210))
    return im


def _circle(im: Image.Image, size: int) -> Image.Image:
    im = im.resize((size, size), Image.LANCZOS)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size, size), fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(im, (0, 0), mask)
    return out


def _rounded(im: Image.Image, radius: int) -> Image.Image:
    im = im.convert("RGBA")
    w, h = im.size
    mask = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(mask)
    d.rounded_rectangle((0, 0, w - 1, h - 1), radius=radius, fill=255)
    out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    out.paste(im, (0, 0), mask)
    return out


# ── Entry field access ───────────────────────────────────────────

def _element_color(entry: dict) -> tuple:
    return ELEMENT_COLOR.get(entry.get("element", ""), _DEFAULT_ELEMENT_COLOR)


def _portrait_path(entry: dict) -> str:
    return os.path.join(_PORTRAIT_DIR, f"{entry.get('id', '')}.png")


def _icon_path(entry: dict) -> str:
    return os.path.join(_ICON_DIR, f"{entry.get('title', '')}.png")


def _skill_icon_path(entry: dict, idx: int) -> str:
    return os.path.join(_SKILL_DIR, f"{entry.get('id', '')}_{idx + 1}.png")


def _skill_meta(sk: dict) -> str:
    parts = []
    if sk.get("type"):
        parts.append(str(sk["type"]))
    if sk.get("soul"):
        parts.append(f"星尘 {sk['soul']}")
    if sk.get("cd"):
        parts.append(f"冷却 {sk['cd']}")
    return " · ".join(parts)


# ── Card path / freshness ────────────────────────────────────────

# Bump this when the card layout / rendering code changes to invalidate all
# cached PNGs. The version is appended to the content hash so that cards are
# re-rendered even when only the renderer (not the data) has changed.
_RENDERER_VERSION = "2"

_hash_lock = threading.Lock()


def _entry_hash(entry: dict) -> str:
    """Return the content hash stamped on an entry (or compute one).

    The wiki_scraper stamps each cache entry with ``_card_hash``; when it's
    absent (e.g. an old cache built before this feature) we fall back to
    hashing the whole entry, which yields a stable value for dedup.
    """
    h = entry.get("_card_hash")
    if h:
        return str(h)
    payload = {k: v for k, v in entry.items() if k != "_card_hash"}
    s = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _load_hashes(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_hashes(path: str, data: dict):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def character_card_path(entry: dict) -> str:
    name = entry.get("name_cn") or entry.get("title", "character")
    return os.path.join(_CARD_DIR, f"{name}.png")


def bond_card_path(entry: dict) -> str:
    name = entry.get("name_cn") or entry.get("title", "bond")
    return os.path.join(_BOND_CARD_DIR, f"{name}.png")


def _render_if_stale(entry: dict, hash_file: str, path_fn, render_fn) -> str:
    """Render a card only if its PNG is missing or stale; return its path."""
    path = path_fn(entry)
    key = os.path.basename(path)
    h = _entry_hash(entry) + ":" + _RENDERER_VERSION

    with _hash_lock:
        if os.path.exists(path) and _load_hashes(hash_file).get(key) == h:
            return path

    # Render outside the lock (PIL work is slow); the hash is deterministic so
    # a benign race only means a redundant draw + identical last-writer write.
    path = render_fn(entry, path)

    with _hash_lock:
        hashes = _load_hashes(hash_file)
        hashes[key] = h
        _save_hashes(hash_file, hashes)
    return path


def render_character_card_if_stale(entry: dict, out_path: str | None = None) -> str:
    """Render a character card if missing/stale, else return the cached path."""
    if out_path is not None:
        return _render_if_stale(entry, _CARD_HASH_FILE,
                                lambda e: out_path, render_character_card)
    return _render_if_stale(entry, _CARD_HASH_FILE,
                            character_card_path, render_character_card)


def render_bond_card_if_stale(entry: dict, out_path: str | None = None) -> str:
    """Render a bond card if missing/stale, else return the cached path."""
    if out_path is not None:
        return _render_if_stale(entry, _BOND_CARD_HASH_FILE,
                                lambda e: out_path, render_bond_card)
    return _render_if_stale(entry, _BOND_CARD_HASH_FILE,
                            bond_card_path, render_bond_card)


# ── Main render ──────────────────────────────────────────────────

def render_character_card(entry: dict, out_path: str | None = None) -> str:
    """Render a character detail entry to PNG. Returns the output path."""
    color = _element_color(entry)

    # --- prepare images ------------------------------------------
    portrait = _load_scaled(_portrait_path(entry), PORTRAIT_W)
    if portrait is None:
        portrait = _placeholder(PORTRAIT_W, PORTRAIT_H, color, entry.get("name_cn", "")[:2])

    icon = _load_scaled(_icon_path(entry), ICON_SIZE, ICON_SIZE)
    if icon is None:
        icon = _placeholder(ICON_SIZE, ICON_SIZE, color, entry.get("name_cn", "")[:1])
    icon = _circle(icon, ICON_SIZE)

    skills = entry.get("skills") or []
    skill_icons = []
    for i in range(3):
        si = _load_scaled(_skill_icon_path(entry, i), SKILL_ICON, SKILL_ICON)
        if si is None:
            si = _placeholder(SKILL_ICON, SKILL_ICON, color, str(i + 1))
        skill_icons.append(_rounded(si, 10))

    # --- geometry & text pre-layout ------------------------------
    probe = ImageDraw.Draw(Image.new("RGBA", (W, 10)))
    right_x = PAD + PORTRAIT_W + GAP
    right_w = W - right_x - PAD

    name_text = entry.get("name_cn") or entry.get("title", "")
    stars = "★" * int(entry.get("stars", 0) or 0)

    meta_bits = [x for x in (entry.get("element"), entry.get("class_cn")) if x]
    if entry.get("constellation"):
        meta_bits.append(entry["constellation"])
    if entry.get("id"):
        meta_bits.append(f"ID {entry['id']}")
    meta_line = " · ".join(meta_bits)

    personal = []
    for label, key in (("身高", "height"), ("体重", "weight"),
                       ("生日", "birthday"), ("胸围", "breast"), ("上线", "release")):
        if entry.get(key):
            personal.append(f"{label} {entry[key]}")
    personal_line = " | ".join(personal)
    personal_lines = _wrap(probe, personal_line, 20, right_w) if personal_line else []

    desc_lines = _wrap(probe, entry.get("desc", ""), 22, right_w)

    stats = entry.get("stats") or {}
    stats_max = entry.get("stats_max") or {}
    stat_order = [("ATK", "攻击"), ("DEF", "防御"), ("HP", "生命"), ("SPD", "速度")]

    # discs: keep true talent level (level 3 is universally empty on seeds)
    discs_raw = entry.get("discs") or []
    discs = [(i + 1, d) for i, d in enumerate(discs_raw) if d]

    team_pot = entry.get("team_pot", "")
    self_pot = entry.get("self_pot", "")

    # skill columns — body is a list of (size, text, color) wrapped lines
    col_gap = GAP
    col_w = (W - 2 * PAD - 2 * col_gap) // 3
    col_text_w = col_w - 40
    skill_cols = []
    for i, sk in enumerate(skills[:3]):
        body = []
        for key, size in (("des", 20), ("des2", 20)):
            for ln in _wrap(probe, sk.get(key, ""), size, col_text_w):
                body.append((size, ln, _TEXT_DIM))
        if sk.get("multi"):
            for raw in str(sk["multi"]).splitlines():
                if raw.strip():
                    for ln in _wrap(probe, f"倍率 {raw}", 18, col_text_w):
                        body.append((18, ln, _TEXT_DIM))
        if sk.get("burst"):
            for ln in _wrap(probe, f"Burst: {sk['burst']}", 18, col_text_w):
                body.append((18, ln, _BURST))
        skill_cols.append({
            "name": sk.get("name") or sk.get("name_en", ""),
            "meta": _skill_meta(sk),
            "body": body,
        })

    # --- heights --------------------------------------------------
    # Top band
    header_h = ICON_SIZE
    header_h += _lh(20) + 4                       # meta line
    if personal_lines:
        header_h += len(personal_lines) * _lh(20) + 4
    header_h += max(1, len(desc_lines)) * _lh(22)
    top_h = max(portrait.height, header_h)

    # Middle band (stats + discs + potential)
    band2_inner = 0
    band2_inner += _lh(24) + 14                   # title
    band2_inner += _lh(18) + _lh(26) + 4          # stats label + Lv.60 value
    if stats_max:
        band2_inner += _lh(18) + 4               # growth-ratio row
    band2_inner += 12
    band2_inner += ((len(discs) + 2) // 3) * (_lh(20) + 8)  # discs grid
    band2_inner += 6
    if team_pot or self_pot:
        band2_inner += _lh(20)
    band2_h = band2_inner + 2 * PAD

    # Skill band
    skill_name_h = _lh(24)
    skill_meta_h = _lh(18) if any(c["meta"] for c in skill_cols) else 0
    skill_body_h = max(
        (sum(_lh(s) for s, _, _ in c["body"]) for c in skill_cols), default=0
    )
    skill_inner = SKILL_ICON + 12 + skill_name_h + skill_meta_h + 8 + skill_body_h
    skill_h = skill_inner + 2 * 20  # 20px vertical padding inside panel

    total_h = PAD + top_h + GAP + band2_h + GAP + skill_h + PAD

    # --- draw -----------------------------------------------------
    canvas = Image.new("RGBA", (W, total_h), _BG + (255,))
    draw = ImageDraw.Draw(canvas)

    # Top band
    y = PAD
    canvas.paste(portrait, (PAD, y), portrait)
    rx, ry = right_x, y
    canvas.paste(icon, (rx, ry), icon)
    name_x = rx + ICON_SIZE + 14
    draw.text((name_x, ry + 2), name_text, font=_font(40), fill=_TEXT)
    if stars:
        nw = _tw(draw, name_text, 40)
        draw.text((name_x + nw + 10, ry + 10), stars, font=_font(28), fill=_STAR)
    ry += ICON_SIZE + 4

    draw.text((rx, ry), meta_line, font=_font(20), fill=_TEXT_FAINT)
    ry += _lh(20) + 4

    for ln in personal_lines:
        draw.text((rx, ry), ln, font=_font(20), fill=_TEXT_DIM)
        ry += _lh(20)
    if personal_lines:
        ry += 4

    for ln in desc_lines:
        draw.text((rx, ry), ln, font=_font(22), fill=_TEXT_DIM)
        ry += _lh(22)

    y += top_h + GAP

    # Middle band
    draw.rounded_rectangle((PAD, y, PAD + (W - 2 * PAD), y + band2_h),
                           radius=16, fill=_PANEL + (255,))
    px, py = PAD + PAD, y + PAD
    draw.text((px, py), "面板(Lv.60) · 天赋 · 潜能", font=_font(24), fill=_TEXT)
    py += _lh(24) + 14

    stat_x = px
    stat_col_w = (W - 2 * PAD) // 4
    if stats_max:
        # Row 1: Lv.60 max values (large, coloured)
        for key, label in stat_order:
            draw.text((stat_x, py), label, font=_font(18), fill=_TEXT_FAINT)
            draw.text((stat_x, py + _lh(18)), str(stats_max.get(key, "")),
                      font=_font(26), fill=color)
            stat_x += stat_col_w
        py += _lh(18) + _lh(26) + 4
        # Row 2: growth ratios (small, dim)
        stat_x = px
        for key, label in stat_order:
            draw.text((stat_x, py), f"成长 {stats.get(key, '')}",
                      font=_font(18), fill=_TEXT_FAINT)
            stat_x += stat_col_w
        py += _lh(18) + 4
    else:
        for key, label in stat_order:
            draw.text((stat_x, py), label, font=_font(18), fill=_TEXT_FAINT)
            draw.text((stat_x, py + _lh(18)), str(stats.get(key, "")),
                      font=_font(26), fill=color)
            stat_x += stat_col_w
        py += _lh(18) + _lh(26) + 16

    disc_col_w = (W - 2 * PAD) // 3
    disc_rows = (len(discs) + 2) // 3
    for n, (lv, d) in enumerate(discs):
        row, col = divmod(n, 3)
        draw.text((px + col * disc_col_w, py + row * (_lh(20) + 8)),
                  f"Lv{lv} {d}", font=_font(20), fill=_TEXT_DIM)
    py += disc_rows * (_lh(20) + 8)
    py += 6

    if team_pot or self_pot:
        pot_parts = []
        if team_pot:
            pot_parts.append(f"团队 {team_pot}")
        if self_pot:
            pot_parts.append(f"自身 {self_pot}")
        draw.text((px, py), " / ".join(pot_parts), font=_font(20), fill=_TEXT_DIM)

    y += band2_h + GAP

    # Skill band
    for i, col in enumerate(skill_cols):
        cx = PAD + i * (col_w + col_gap)
        draw.rounded_rectangle((cx, y, cx + col_w, y + skill_h),
                               radius=16, fill=_PANEL_2 + (255,))
        ix, iy = cx + 20, y + 20
        canvas.paste(skill_icons[i], (ix, iy), skill_icons[i])
        draw.text((ix + SKILL_ICON + 12, iy - 2), f"[{i + 1}] {col['name']}",
                  font=_font(22), fill=_TEXT)
        iy += SKILL_ICON + 8
        if col["meta"]:
            draw.text((ix, iy), col["meta"], font=_font(18), fill=_TEXT_FAINT)
            iy += _lh(18)
        iy += 4
        for size, ln, fill in col["body"]:
            draw.text((ix, iy), ln, font=_font(size), fill=fill)
            iy += _lh(size)

    # footer
    draw.text((PAD, total_h - PAD + 8), "数据来源: Ark Re:Code Wiki",
              font=_font(16), fill=_TEXT_FAINT)

    if out_path is None:
        out_path = character_card_path(entry)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    canvas.convert("RGB").save(out_path, "PNG")
    return out_path


# ── Bond card ─────────────────────────────────────────────────────

def _bond_rarity_cn(stars: int) -> str:
    return {5: "传说", 4: "史诗", 3: "稀有"}.get(int(stars), "")


def _bond_class_color(entry: dict) -> tuple:
    return CLASS_COLOR.get(entry.get("class_en", ""), _DEFAULT_ELEMENT_COLOR)


def _bond_icon_path(entry: dict) -> str:
    return os.path.join(_BOND_ICON_DIR, f"{entry.get('id', '')}.png")


def render_bond_card(entry: dict, out_path: str | None = None) -> str:
    """Render a bond detail entry to PNG. Returns the output path.

    Layout (1080px wide, dark theme, adaptive height):

        ┌───────────────────────────────────────────────┐
        │  [icon]  名称 ★★★★  · 传说羁绊                  │
        │          描述 ...                              │
        ├───────────────────────────────────────────────┤
        │  类别 重装 · 星级 ★★★★★ · 攻击 9/117 · 生命 76/988│
        ├───────────────────────────────────────────────┤
        │  羁绊技能 <effect>                             │
        │  备注 <notes>                                  │
        ├───────────────────────────────────────────────┤
        │  获取方式 / 出售价格 / 经验值 / 上线时间          │
        └───────────────────────────────────────────────┘
    """
    color = _bond_class_color(entry)

    icon = _load_scaled(_bond_icon_path(entry), BOND_ICON_SIZE, BOND_ICON_SIZE)
    if icon is None:
        icon = _placeholder(BOND_ICON_SIZE, BOND_ICON_SIZE, color, entry.get("name_cn", "")[:1])
    icon = _rounded(icon, 18)

    probe = ImageDraw.Draw(Image.new("RGBA", (W, 10)))
    name_text = entry.get("name_cn") or entry.get("title", "")
    stars = int(entry.get("stars", 0) or 0)
    star_text = "★" * stars
    rarity = _bond_rarity_cn(stars)

    text_x = PAD + BOND_ICON_SIZE + 24
    text_w = W - text_x - PAD
    desc_lines = _wrap(probe, entry.get("desc", ""), 22, text_w)

    panel_inner_w = W - 2 * PAD - 2 * 28
    effect_lines = _wrap(probe, entry.get("effect", ""), 22, panel_inner_w)
    notes_lines = _wrap(probe, entry.get("notes", ""), 20, panel_inner_w)

    label_w = 130
    value_w = panel_inner_w - label_w
    obtain_lines = _wrap(probe, entry.get("obtain", ""), 20, value_w)

    # --- heights --------------------------------------------------
    header_text_h = _lh(40) + 4 + _lh(20) + 8 + max(1, len(desc_lines)) * _lh(22)
    top_h = max(BOND_ICON_SIZE, header_text_h)

    stat_h = _lh(24) + 14 + _lh(18) + _lh(26) + 8 + 2 * 28

    skill_inner = _lh(24) + 14 + max(1, len(effect_lines)) * _lh(22)
    if notes_lines:
        skill_inner += _lh(20) + 8 + len(notes_lines) * _lh(20)
    skill_h = skill_inner + 2 * 28

    bottom_rows = [("获取方式", obtain_lines)]
    if entry.get("unsellable"):
        bottom_rows.append(("出售价格", ["不可出售"]))
    else:
        sell = f"金币 +{entry.get('sell_gold', '')} · 记忆碎片 +{entry.get('sell_fragment', '')}"
        bottom_rows.append(("出售价格", [sell]))
        bottom_rows.append(("经验值", [str(entry.get('xp_value', ''))]))
    if entry.get("release"):
        bottom_rows.append(("上线时间", [entry["release"]]))

    bottom_inner = sum(max(1, len(lns)) * _lh(20) for _, lns in bottom_rows)
    bottom_inner += (len(bottom_rows) - 1) * 10
    bottom_h = bottom_inner + 2 * 28

    total_h = PAD + top_h + GAP + stat_h + GAP + skill_h + GAP + bottom_h + PAD

    # --- draw -----------------------------------------------------
    canvas = Image.new("RGBA", (W, total_h), _BG + (255,))
    draw = ImageDraw.Draw(canvas)

    # Header band
    y = PAD
    canvas.paste(icon, (PAD, y), icon)
    ry = y
    draw.text((text_x, ry), name_text, font=_font(40), fill=_TEXT)
    if star_text:
        nw = _tw(draw, name_text, 40)
        draw.text((text_x + nw + 10, ry + 10), star_text, font=_font(28), fill=_STAR)
    ry += _lh(40) + 4
    if rarity:
        draw.text((text_x, ry), f"{rarity}羁绊", font=_font(20), fill=color)
    ry += _lh(20) + 8
    for ln in desc_lines:
        draw.text((text_x, ry), ln, font=_font(22), fill=_TEXT_DIM)
        ry += _lh(22)

    y += top_h + GAP

    # Stats panel
    draw.rounded_rectangle((PAD, y, PAD + (W - 2 * PAD), y + stat_h),
                           radius=16, fill=_PANEL + (255,))
    px, py = PAD + 28, y + 28
    draw.text((px, py), "基础属性", font=_font(24), fill=_TEXT)
    py += _lh(24) + 14

    stats = [
        ("类别", entry.get("class_cn", ""), color),
        ("星级", star_text, _STAR),
        ("攻击", f"{entry.get('atk_base', '')} / {entry.get('atk_max', '')}", color),
        ("生命", f"{entry.get('hp_base', '')} / {entry.get('hp_max', '')}", color),
    ]
    stat_col_w = (W - 2 * PAD - 2 * 28) // 4
    sx = px
    for label, value, vcolor in stats:
        draw.text((sx, py), label, font=_font(18), fill=_TEXT_FAINT)
        draw.text((sx, py + _lh(18)), value, font=_font(26), fill=vcolor)
        sx += stat_col_w

    y += stat_h + GAP

    # Skill panel
    draw.rounded_rectangle((PAD, y, PAD + (W - 2 * PAD), y + skill_h),
                           radius=16, fill=_PANEL + (255,))
    px, py = PAD + 28, y + 28
    draw.text((px, py), "羁绊技能", font=_font(24), fill=_TEXT)
    py += _lh(24) + 14
    for ln in effect_lines:
        draw.text((px, py), ln, font=_font(22), fill=_TEXT_DIM)
        py += _lh(22)
    if notes_lines:
        py += _lh(20) + 8
        draw.text((px, py), "备注", font=_font(20), fill=_TEXT_FAINT)
        py += _lh(20)
        for ln in notes_lines:
            draw.text((px, py), ln, font=_font(20), fill=_TEXT_DIM)
            py += _lh(20)

    y += skill_h + GAP

    # Bottom panel
    draw.rounded_rectangle((PAD, y, PAD + (W - 2 * PAD), y + bottom_h),
                           radius=16, fill=_PANEL_2 + (255,))
    px, py = PAD + 28, y + 28
    for ri, (label, lns) in enumerate(bottom_rows):
        if not lns:
            continue
        if ri:
            py += 10
        draw.text((px, py), label, font=_font(20), fill=_TEXT_FAINT)
        vx = px + label_w
        for ln in lns:
            draw.text((vx, py), ln, font=_font(20), fill=_TEXT_DIM)
            py += _lh(20)

    draw.text((PAD, total_h - PAD + 8), "数据来源: Ark Re:Code Wiki",
              font=_font(16), fill=_TEXT_FAINT)

    if out_path is None:
        out_path = bond_card_path(entry)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    canvas.convert("RGB").save(out_path, "PNG")
    return out_path

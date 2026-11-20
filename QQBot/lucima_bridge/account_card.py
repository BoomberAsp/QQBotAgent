"""Build an account snapshot card from an allow-listed, session-scoped profile."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import io
from pathlib import Path
import re
import unicodedata

from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageOps

from .receipt import FONT, PALETTES

RESOURCES = (
    ("gold", "金币", "1"), ("recruit", "招募契约", "5"),
    ("diamond", "钻石", "2"), ("mystery", "神秘契约", "6"),
    ("battery", "能源电池", "37"), ("galaxy", "银河契约", "7"),
)
AVATAR = re.compile(r"[A-Za-z0-9_-]{1,72}\.(?:png|webp|jpe?g)", re.IGNORECASE)
UID = re.compile(r"[A-Za-z0-9_-]{1,32}")
BEIJING = timezone(timedelta(hours=8))


def _text(value, limit):
    if not isinstance(value, str):
        return ""
    clean = (" " if unicodedata.category(ch).startswith("C") else ch for ch in value[:limit * 2])
    return " ".join("".join(clean).split())[:limit]


def _count(value):
    return value if type(value) is int and 0 <= value < 10**15 else None


def _avatar(value):
    return value if isinstance(value, str) and AVATAR.fullmatch(value) else ""


def normalize_profile(data, *, full):
    if not isinstance(data, dict) or type(data.get("sampled_at")) is not int:
        return None
    if not 0 < data["sampled_at"] < 4_102_444_800:
        return None
    card = {
        "name": _text(data.get("name"), 40) or "游戏账号",
        "level": _count(data.get("level")), "avatar": _avatar(data.get("avatar")),
        "sampled_at": data["sampled_at"], "full": full,
        "heroes_total": _count(data.get("heroes_total")), "items_total": _count(data.get("items_total")),
    }
    for key in ("stamina", "flag"):
        gauge = data.get(key)
        card[key] = {field: _count(gauge.get(field)) if isinstance(gauge, dict) else None
                     for field in ("current", "max")}
    if full:
        uid = data.get("uid")
        if isinstance(uid, str) and UID.fullmatch(uid):
            card["uid"] = uid
        currencies = data.get("currencies")
        card["currencies"] = {key: _count(currencies.get(key)) if isinstance(currencies, dict) else None
                              for key, _, _ in RESOURCES}
        heroes = data.get("heroes")
        card["heroes"] = [{
            "name": _text(hero.get("name"), 28) or "团员",
            "level": _count(hero.get("level")), "avatar": _avatar(hero.get("avatar")),
            "grade": hero.get("grade") if hero.get("grade") in ("D", "C", "B", "A", "S", "SS", "SSS") else "",
        } for hero in (heroes[:5] if isinstance(heroes, list) else []) if isinstance(hero, dict)]
    return card


def _number(value):
    return "—" if value is None else f"{value:,}"


def _gauge_text(gauge):
    current = _number(gauge["current"])
    maximum = gauge["max"]
    return f"{current}/{_number(maximum)}" if maximum else current


def profile_summary(card):
    stamp = datetime.fromtimestamp(card["sampled_at"], BEIJING)
    lines = [
        f"角色等级 {_number(card['level'])} · 快照 {stamp:%m-%d %H:%M}（北京时间）",
        f"体力 {_gauge_text(card['stamina'])} · 旗帜 {_gauge_text(card['flag'])}",
        f"团员 {_number(card['heroes_total'])} · 物品 {_number(card['items_total'])}",
    ]
    if card["full"]:
        if uid := card.get("uid"):
            lines.append(f"UID {uid}")
        for index in (0, 3):
            lines.append(" · ".join(f"{label} {_number(card['currencies'][key])}"
                                    for key, label, _ in RESOURCES[index:index + 3]))
    return lines


def _asset(root, category, filename):
    if not filename:
        return None
    try:
        folder = (root / "assets" / category).resolve()
        path = (folder / filename).resolve()
        if not path.is_relative_to(folder):
            return None
        with Image.open(path) as item:
            return item.convert("RGBA")
    except (OSError, ValueError):
        return None


def _paste_art(canvas, art, xy, size, radius=0):
    if art is None:
        return
    if radius:
        art = ImageOps.fit(art, (size, size), method=Image.Resampling.LANCZOS)
        mask = Image.new("L", art.size)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=255)
        canvas.paste(art, xy, ImageChops.multiply(mask, art.getchannel("A")))
    else:
        art = ImageOps.contain(art, (size, size), method=Image.Resampling.LANCZOS)
        canvas.paste(art, (xy[0] + (size - art.width) // 2, xy[1] + (size - art.height) // 2), art)


def _fit(draw, value, font, width):
    if draw.textlength(value, font=font) <= width:
        return value
    while value and draw.textlength(value + "…", font=font) > width:
        value = value[:-1]
    return value + "…"


def render_account_card(card, root: Path, theme=None) -> bytes:
    background, panel, text, accent, border = PALETTES.get(theme, PALETTES["dark"])
    dark = theme != "light"
    muted = "#95a3bb" if dark else "#52617a"
    green = "#43ddaa" if dark else "#147958"
    grid = "#141c2b" if dark else "#e1e8f2"
    track = "#2b263c" if dark else "#e7dce9"
    full = card["full"]
    width, height = 760, 896 if full else 561
    canvas = Image.new("RGB", (width, height), background)
    draw = ImageDraw.Draw(canvas)
    fonts = {size: ImageFont.truetype(str(FONT), size) for size in (15, 17, 19, 21, 25, 30, 41)}
    for x in range(0, width, 48):
        draw.line((x, 0, x, height), fill=grid)
    for y in range(0, height, 48):
        draw.line((0, y, width, y), fill=grid)

    draw.text((33, 21), "LucimaTools", fill=accent, font=fonts[17])
    draw.text((31, 43), "基本信息", fill=text, font=fonts[41])
    draw.rounded_rectangle((573, 43, 728, 82), 19, fill=panel, outline=border, width=2)
    draw.ellipse((591, 58, 601, 68), fill=green)
    draw.text((612, 49), "账号快照", fill=text, font=fonts[19])

    main_end = 601 if full else 438
    draw.rounded_rectangle((20, 105, 739, main_end), 27, fill=panel, outline=border, width=2)
    draw.ellipse((47, 126, 158, 237), fill=track, outline=accent, width=2)
    draw.text((103, 181), "团", fill=muted, font=fonts[30], anchor="mm")
    _paste_art(canvas, _asset(root, "avatars", card["avatar"]), (53, 132), 100, 50)
    draw.text((180, 133), _fit(draw, card["name"], fonts[41], 520), fill=text, font=fonts[41])
    draw.text((182, 193), f"LV {_number(card['level'])}", fill=accent, font=fonts[25])
    if full and card.get("uid"):
        draw.text((315, 196), "UID " + card["uid"], fill=muted, font=fonts[19])

    for cx, key, label, sid, color in (
        (221, "stamina", "体力", "3", green), (539, "flag", "旗帜", "4", accent)
    ):
        gauge = card[key]
        box = (cx - 72, 246, cx + 72, 390)
        draw.ellipse(box, outline=track, width=9)
        current, maximum = gauge["current"], gauge["max"]
        if current and maximum:
            draw.arc(box, -90, -90 + min(current / maximum, 1) * 360, fill=color, width=9)
        draw.ellipse((cx - 58, 260, cx + 58, 376), outline=border, width=1)
        _paste_art(canvas, _asset(root, "items", sid + ".png"), (cx - 20, 268), 40)
        value = _fit(draw, _gauge_text(gauge), fonts[25], 106)
        draw.text((cx, 344), value, fill=text, font=fonts[25], anchor="mm")
        draw.text((cx, 410), label, fill=muted, font=fonts[19], anchor="mm")

    if full:
        draw.line((47, 437, 712, 437), fill=border)
        for index, (key, label, sid) in enumerate(RESOURCES):
            x, y = 53 + (index % 2) * 348, 448 + (index // 2) * 49
            _paste_art(canvas, _asset(root, "items", sid + ".png"), (x, y + 5), 32)
            draw.text((x + 42, y), label, fill=muted, font=fonts[15])
            draw.text((x + 42, y + 17), _number(card["currencies"][key]), fill=text, font=fonts[21])
    top = main_end + 17
    draw.rounded_rectangle((20, top, 739, height - 46), 24, fill=panel, outline=border, width=2)
    draw.text((44, top + 13), "团员与物品", fill=text, font=fonts[25])
    draw.text((711, top + 16),
              f"团员 {_number(card['heroes_total'])}   ·   物品 {_number(card['items_total'])}",
              fill=muted, font=fonts[19], anchor="ra")
    if full:
        y = top + 68
        if not card["heroes"]:
            draw.text((44, y + 14), "暂无可显示的团员", fill=muted, font=fonts[19])
        for index, hero in enumerate(card["heroes"]):
            x = 49 + index * 139
            draw.rounded_rectangle((x, y, x + 108, y + 108), 12, fill=track, outline=border)
            _paste_art(canvas, _asset(root, "avatars", hero["avatar"]), (x + 1, y + 1), 107, 12)
            if hero["level"] is not None:
                draw.rounded_rectangle((x + 5, y + 5, x + 40, y + 28), 7, fill=background)
                draw.text((x + 22, y + 16), str(hero["level"]), fill=text, font=fonts[15], anchor="mm")
            if hero["grade"]:
                draw.rounded_rectangle((x + 63, y + 80, x + 104, y + 103), 7, fill=background)
                draw.text((x + 83, y + 92), hero["grade"], fill=accent, font=fonts[15], anchor="mm")
            label = _fit(draw, hero["name"], fonts[17], 121)
            draw.text((x + 54, y + 129), label, fill=text, font=fonts[17], anchor="mm")

    stamp = datetime.fromtimestamp(card["sampled_at"], BEIJING)
    draw.text((30, height - 32), f"快照 {stamp:%Y-%m-%d %H:%M:%S}  ·  北京时间", fill=muted, font=fonts[17])
    with io.BytesIO() as output:
        canvas.save(output, format="PNG")
        return output.getvalue()

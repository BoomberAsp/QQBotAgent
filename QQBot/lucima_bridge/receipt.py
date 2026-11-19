"""Render allow-listed Lucima task and login receipts as PNGs."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import io
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


FONT = Path(__file__).resolve().parents[1] / "config/font/NotoSansSC-SemiBold.ttf"
PALETTES = {
    "light": ("#eef2f8", "#ffffff", "#1b2740", "#803259", "#e9ddec"),
    "dark": ("#070b14", "#101828", "#dce6f5", "#ff9abf", "#38283f"),
}


def render_receipt(receipt: dict) -> bytes:
    is_login = receipt.get("kind") == "login"
    finished = datetime.fromtimestamp(receipt["completed_at"], timezone(timedelta(hours=8)))
    heading = "Lucima 登录回执" if is_login else "Lucima 任务回执"
    subject = "QQ 游戏账号授权" if is_login else receipt["task"]
    footer = f"{finished:%Y-%m-%d %H:%M:%S}  UTC+8   ·   {receipt['id']}"
    return render_card(heading, subject, [receipt["message"], *receipt["details"]], footer, receipt.get("theme"))


def render_panel(panel: dict) -> bytes:
    title, _, message = panel["message"].partition("\n")
    return render_card(title, "", message.splitlines(), "", panel.get("theme"))


def render_card(heading, subject, paragraphs, footer, theme) -> bytes:
    background, paper, foreground, accent, border = PALETTES.get(theme, PALETTES["dark"])
    title = ImageFont.truetype(str(FONT), 36)
    body = ImageFont.truetype(str(FONT), 24)
    small = ImageFont.truetype(str(FONT), 20)
    width, padding = 760, 36
    if title.getlength(heading) > width - 2 * padding:
        heading = heading.split(" / ")[-1].removeprefix("LucimaTools · ")
    lines = []
    for text in paragraphs:
        line = ""
        for char in text:
            if body.getlength(line + char) > width - padding * 2 and line:
                lines.append(line)
                line = ""
            line += char
        if line:
            lines.append(line)
    top = 138 if subject else 92
    img = Image.new("RGB", (width, top + 37 * len(lines) + (49 if footer else 26)), background)
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((12, 12, width - 13, img.height - 12), radius=20,
                           fill=paper, outline=border, width=2)
    draw.text((padding, 26), heading, fill=accent, font=title)
    if subject:
        draw.text((padding, 85), subject, fill=foreground, font=body)
    draw.line((padding, top - 12, width - padding, top - 12), fill=border, width=2)
    for index, line in enumerate(lines):
        draw.text((padding, top + index * 37), line, fill=foreground, font=body)
    if footer:
        draw.text((padding, img.height - 42), footer, fill=accent, font=small)
    with io.BytesIO() as buffer:
        img.save(buffer, format="PNG")
        return buffer.getvalue()

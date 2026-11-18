"""Render the bridge's private, allow-listed task receipt as a PNG."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import io
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


FONT = Path(__file__).resolve().parents[1] / "config/font/NotoSansSC-SemiBold.ttf"


def render_receipt(receipt: dict) -> bytes:
    title = ImageFont.truetype(str(FONT), 36)
    body = ImageFont.truetype(str(FONT), 24)
    small = ImageFont.truetype(str(FONT), 20)
    width, padding = 760, 36
    lines = []
    for text in (receipt["message"], *receipt["details"]):
        line = ""
        for char in text:
            if body.getlength(line + char) > width - padding * 2 and line:
                lines.append(line)
                line = ""
            line += char
        if line:
            lines.append(line)
    img = Image.new("RGB", (width, 180 + 37 * len(lines)), "#f0f5f2")
    draw = ImageDraw.Draw(img)
    draw.text((padding, 26), "Lucima 任务回执", fill="#17382c", font=title)
    draw.text((padding, 85), receipt["task"], fill="#30584a", font=body)
    draw.line((padding, 125, width-padding, 125), fill="#b0c9bf", width=2)
    for index, line in enumerate(lines):
        draw.text((padding, 139 + index*37), line, fill="#192b24", font=body)
    finished = datetime.fromtimestamp(receipt["completed_at"], timezone(timedelta(hours=8)))
    footer = f"{finished:%Y-%m-%d %H:%M:%S}  UTC+8   ·   {receipt['id']}"
    draw.text((padding, img.height-34), footer, fill="#30584a", font=small)
    with io.BytesIO() as buffer:
        img.save(buffer, format="PNG")
        return buffer.getvalue()

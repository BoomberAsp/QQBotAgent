"""VLM 识别分支（方案 §2.4）。

电影/电视剧/动漫/真人图走多模态识别：作品、角色/演员、水印@账号。
multimodal_client 不可用或出错时返回 None，不阻塞引擎反查主路。
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
import re

from .accounts import handles_in_text

logger = logging.getLogger("find_source.vlm")

_PROMPT = (
    "这是一张需要找来源的图片。请识别：\n"
    "1. 若来自电影/电视剧/动漫/游戏：给出作品名与角色名（不确定就说「不确定」）。\n"
    "2. 若是真人照片：描述可能的身份类型（coser/演员/网红），不要猜测具体姓名。\n"
    "3. 提取图中任何水印、@账号、签名文字，原样列出。\n"
    '用中文返回 JSON，字段为 image_type、work、character、description 和 watermarks。'
    'watermarks 是从图中原样抄录的字符串数组。未知字段用空字符串或空数组，'
    '不要编造账号、链接、名称或水印。description 不超过120字。'
)

@dataclass
class VisualClues:
    text: str = ""
    image_type: str = ""
    work: str = ""
    character: str = ""
    watermark_text: list[str] = field(default_factory=list)
    watermarks: list[str] = field(default_factory=list)


def watermarked_handles(texts: list[str]) -> list[str]:
    handles = {}
    for text in texts:
        for handle in handles_in_text(text):
            handles.setdefault(handle.lower(), "@" + handle)
    return list(handles.values())[:5]


def parse_clues(raw: str | None) -> VisualClues:
    if not raw:
        return VisualClues()
    fence = chr(96) * 3
    cleaned = raw.strip()
    if cleaned.startswith(fence):
        cleaned = re.sub(rf"^{fence}(?:json)?\s*|\s*{fence}$", "", cleaned)
    try:
        result = json.loads(cleaned)
    except (ValueError, RecursionError):
        result = None
    if not isinstance(result, dict):
        lines = [line for line in raw.splitlines() if re.search(r"水印|签名|watermark", line, re.I)]
        return VisualClues(text=raw[:700], watermarks=watermarked_handles(lines))
    values = result.get("watermarks")
    texts = []
    if isinstance(values, list):
        texts = [text.strip()[:120] for text in values if isinstance(text, str) and text.strip()][:5]
    names = [str(result.get(key) or "").strip()[:120] for key in ("image_type", "work", "character")]
    summary = [f"{label}：{value}" for label, value in zip(("类型", "疑似作品", "角色"), names) if value]
    description = result.get("description")
    if isinstance(description, str) and description.strip():
        summary.append(description.strip()[:250])
    return VisualClues("\n".join(summary), *names, texts, watermarked_handles(texts))



async def identify_work(image_path: str, timeout: float = 45.0) -> str | None:
    """VLM 识别图片出处。不可用/失败返回 None。"""
    try:
        from lib.multimodal_client import multimodal_client
    except ImportError:
        return None
    if not multimodal_client.is_available():
        return None
    try:
        text = await multimodal_client.analyze_image(
            image_path, prompt=_PROMPT, timeout=timeout)
    except Exception as e:  # noqa: BLE001 识别分支失败不影响主路
        logger.warning("VLM 识别失败: %s", e)
        return None
    if not text or text.startswith("[多模态]"):
        return None
    return text.strip()

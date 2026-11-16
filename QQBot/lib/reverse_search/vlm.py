"""VLM 识别分支（方案 §2.4）。

电影/电视剧/动漫/真人图走多模态识别：作品、角色/演员、水印@账号。
multimodal_client 不可用或出错时返回 None，不阻塞引擎反查主路。
"""
from __future__ import annotations

import logging

logger = logging.getLogger("find_source.vlm")

_PROMPT = (
    "这是一张需要找来源的图片。请识别：\n"
    "1. 若来自电影/电视剧/动漫/游戏：给出作品名与角色名（不确定就说「不确定」）。\n"
    "2. 若是真人照片：描述可能的身份类型（coser/演员/网红），不要猜测具体姓名。\n"
    "3. 提取图中任何水印、@账号、签名文字，原样列出。\n"
    "用中文回答，不超过150字，只给结论不展开。"
)


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

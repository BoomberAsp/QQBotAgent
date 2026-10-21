"""
角色面社交互动工具（Bond-System-Design.md §4.4 / P2）。

这些工具只在**角色面**（split 人格 + social 平面）经 allowed_tools 下发，
与任务面物理隔离。工具本身不扮演角色——它把「发生了什么」以中立事实
observation 的形式回给角色面 LLM，由角色以自己的口吻自然回应。

当前实现：
- give_gift: 送礼。按角色卡 Gift Preferences 匹配偏好档位，结算好感/心情，
  写入共同经历日志（bond_events）。

所有协作对象经参数注入，可独立单测（与 delegation.py / creation_tools.py 同模式）。
"""

from typing import Any, Dict, Optional

from agent import bond_content

_TIER_LABEL = {"loved": "非常喜爱", "normal": "觉得还不错", "disliked": "不太喜欢"}


async def give_gift(
    *,
    user_id: str,
    gift_name: str,
    char_id: str,
    card_content: Optional[str],
    bond_manager,
) -> str:
    """Settle a gift and return a neutral observation for the character LLM.

    Returns a string the character-plane LLM reads as a tool observation; it
    then reacts in character. Daily-cap and level-ups are folded into the text.
    """
    gift_name = (gift_name or "").strip()
    if not user_id or not char_id:
        return "[Error] 缺少用户或角色上下文，无法送礼。"
    if not gift_name:
        return "[Error] 礼物名为空，请在 gift_name 参数里填用户送的礼物。"

    prefs = bond_content.parse_gift_preferences(card_content or "")
    tier = bond_content.match_gift(gift_name, prefs)
    result = bond_manager.give_gift(user_id, char_id, tier)

    if not result.get("accepted"):
        return (
            f"用户想送你「{gift_name}」，但今天收礼次数已达上限"
            f"（{result.get('gifts_today')}/{result.get('daily_gift_limit')}）。"
            "请以角色口吻温柔地告诉用户明天再来，并谢谢这份心意。"
        )

    label = _TIER_LABEL.get(tier, "觉得还不错")
    bond_manager.append_event(
        user_id, char_id, f"用户送了「{gift_name}」给你，你{label}"
    )
    parts = [
        f"用户送给你「{gift_name}」，你{label}这份礼物"
        f"（好感 +{result.get('exp_gained', 0)}）。"
    ]
    for up in result.get("level_ups", []):
        bond_manager.append_event(
            user_id, char_id, f"羁绊提升到了 Lv{up['level']}（{up['title']}）"
        )
        parts.append(f"你们的好感提升到了 Lv{up['level']}（{up['title']}）！")
    parts.append("请以角色口吻自然地回应这份礼物和此刻的心情（不要报数值）。")
    return " ".join(parts)


# give_gift 工具的 JSON Schema（注册进 ToolRegistry 用）
GIVE_GIFT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "gift_name": {
            "type": "string",
            "description": "用户送出的礼物名称（从用户消息里提炼，如「一朵花」「草莓蛋糕」）",
        },
    },
    "required": ["gift_name"],
}

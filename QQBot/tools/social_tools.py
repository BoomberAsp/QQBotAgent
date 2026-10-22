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


# ── P3: 剧情场景收尾 ──────────────────────────────────────────────

async def complete_scene(
    *,
    user_id: str,
    char_id: str,
    scene_title: str,
    bond_manager,
    memento: str = "",
) -> Dict[str, Any]:
    """Settle a finished bond scene; return {"ok", "result", "observation"}.

    Settlement is one-shot and guarded by ``bond_manager.complete_scene``
    (+scene_exp, mood bump, mark scenes_done — a repeat completion yields
    accepted=False and no EXP). The character-plane LLM receives
    ``observation`` and speaks a closing line in the same turn. The turn.py
    closure flags ``_pending_bond_scene_done`` and returns ``observation`` as
    the tool string; the post-run hook consumes ``result`` (level_ups / exp /
    memento) to send the level-up notice + commemorative card and to close the
    special session. Mirrors give_gift (settlement + event log live here).
    """
    scene_title = (scene_title or "").strip()
    if not user_id or not char_id:
        return {"ok": False, "result": {},
                "observation": "[Error] 缺少用户或角色上下文，无法收尾场景。"}
    title = scene_title or "这段剧情"
    memento = (memento or "").strip()

    bond_result = bond_manager.complete_scene(user_id, char_id, title)
    result = dict(bond_result)
    result["scene_title"] = title
    result["char_id"] = char_id
    result["memento"] = memento

    if not bond_result.get("accepted"):
        return {
            "ok": True, "result": result,
            "observation": (
                f"剧情「{title}」其实早已完成过、回忆也已珍藏。"
                f"请以角色口吻温柔地告诉用户这一点，并邀请 TA 去体验别的剧情。"
            ),
        }

    bond_manager.append_event(user_id, char_id, f"完成了剧情场景「{title}」")
    parts = [f"剧情「{title}」已走向结局，你们的羁绊因此更深了。"]
    for up in bond_result.get("level_ups", []):
        bond_manager.append_event(
            user_id, char_id, f"羁绊提升到了 Lv{up['level']}（{up['title']}）"
        )
        parts.append(f"你们的好感提升到了 Lv{up['level']}（{up['title']}）！")
    parts.append(
        "请以角色口吻说一段收尾的话，为这段共同经历画上温柔的句点"
        "（可以回望场景中最打动人的一个瞬间，或轻声约定下一次）。"
        "不要报数值；本轮结束后系统会送出一张纪念卡片。"
    )
    return {"ok": True, "result": result, "observation": " ".join(parts)}


# complete_scene 工具的 JSON Schema。LLM 无需传场景名（turn.py 闭包从当前
# bond_scene 会话 metadata 读取）；memento 为可选的卡面纪念语。
COMPLETE_SCENE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "memento": {
            "type": "string",
            "description": "以角色口吻写的一句纪念语（≤40 字），将镌刻在纪念卡片上，概括本场景最难忘的瞬间",
        },
    },
    "required": [],
}

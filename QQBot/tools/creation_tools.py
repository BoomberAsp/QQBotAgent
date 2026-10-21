"""
UGC 角色创作工具 — create_character（Bond-System-Design.md §5 / P1）。

访谈式创作 special_session 的落盘环节：访谈由 L4 注入的指令驱动
（agent.py），LLM 收集齐字段后调用本工具生成角色卡并注册到
CharacterRegistry（data/characters/{char_id}/）。

流程：字段校验 → 配额复查 → 重名检查 → 组装 card.md + meta.json →
敏感审查（flash 小调用；显式 REJECT = fail-closed，基础设施错误 =
fail-open + log，避免 API 抖动把正常创作卡死）→ 落盘注册。

本模块不依赖 nonebot；所有协作对象经参数注入，可独立单测
（与 agent/delegation.py 同一模式）。
"""

import logging
import time
from typing import Any, Dict, List, Optional

from agent.delegation import _LLM_ERROR_PREFIXES
from agent.character_registry import make_char_id

logger = logging.getLogger(__name__)

# 单字段长度上限（防 prompt 注入式超长卡片撑爆 L2）
_MAX_FIELD_LEN = 2000
_MAX_NAME_LEN = 24
_MAX_LIST_ITEMS = 10
_MAX_ITEM_LEN = 60

_REQUIRED_FIELDS = ("name", "personality", "speech_style", "appearance")

_REVIEW_SYSTEM = (
    "你是内容安全审查员。给你一份用户自创的虚拟角色设定卡，请判断其是否包含"
    "违规内容：真实人物影射诋毁、未成年人色情或性化描写、极端血腥暴力美化、"
    "仇恨言论（针对性别/种族/宗教/地域等的煽动歧视）、教唆现实违法犯罪、"
    "政治敏感煽动。虚构角色的一般性黑暗设定（如反派、战斗、情感纠葛）不算违规。"
    "只输出一行：APPROVE 或 REJECT: <简要理由>。"
)


def _clean_str(value: Any, max_len: int = _MAX_FIELD_LEN) -> str:
    if value is None:
        return ""
    return str(value).strip()[:max_len]


def _clean_list(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    items = []
    for v in value[:_MAX_LIST_ITEMS]:
        s = str(v).strip()[:_MAX_ITEM_LEN]
        if s:
            items.append(s)
    return items


def build_card(fields: Dict[str, Any]) -> str:
    """Assemble card.md content from interview fields (§5.3 structure)."""
    name = _clean_str(fields.get("name"), _MAX_NAME_LEN)
    gender = _clean_str(fields.get("gender"), 40)
    personality = _clean_str(fields.get("personality"))
    worldview = _clean_str(fields.get("worldview")) or "现代日常"
    relationship = _clean_str(fields.get("relationship"))
    greeting = _clean_str(fields.get("greeting"), 200)
    speech_style = _clean_str(fields.get("speech_style"))
    appearance = _clean_str(fields.get("appearance"))
    loved = _clean_list(fields.get("gift_loved"))
    normal = _clean_list(fields.get("gift_normal"))
    disliked = _clean_list(fields.get("gift_disliked"))
    bond_levels = _clean_str(fields.get("bond_levels"))

    lines = [
        f"# {name}",
        "<!-- mode: split | task_voice: character -->",
        "",
        f"你是「{name}」，一个由用户创作的虚拟角色。始终以第一人称扮演该角色，"
        "不要提及自己是 AI、语言模型或角色卡。",
        "",
        "## Core Personality",
        personality,
    ]
    if gender:
        lines.append(f"- 性别: {gender}")
    lines += [
        "",
        "## Worldview",
        worldview,
        "",
        "## Relationship with User",
        relationship or "初次相识的伙伴，随着羁绊加深关系逐渐升温。",
    ]
    if greeting:
        lines += ["", f"- 对用户的称呼/开场: {greeting}"]
    lines += [
        "",
        "## Communication Style",
        speech_style,
        "- 当用户请求涉及实时信息、计算、代码、文件操作等需要工具能力的任务时，"
        "必须调用 delegate_task 工具委托任务智能体执行，再以自己的口吻转达结果；"
        "绝不能凭记忆编造此类内容。",
        "",
        "## Appearance Anchor",
        appearance,
        "",
        "## Gift Preferences",
        f"- 喜爱: {'、'.join(loved) if loved else '（未设定）'}",
        f"- 普通: {'、'.join(normal) if normal else '（未设定）'}",
        f"- 讨厌: {'、'.join(disliked) if disliked else '（未设定）'}",
    ]
    if bond_levels:
        lines += ["", "## Bond Levels", bond_levels]
    lines.append("")
    return "\n".join(lines)


def validate_fields(fields: Dict[str, Any]) -> Optional[str]:
    """Return an error string if fields are invalid, else None."""
    if not isinstance(fields, dict):
        return "[Error] 字段格式错误。"
    for key in _REQUIRED_FIELDS:
        if not _clean_str(fields.get(key), _MAX_NAME_LEN if key == "name" else _MAX_FIELD_LEN):
            return f"[Error] 缺少必填字段「{key}」，请向用户补齐后再调用。"
    name = _clean_str(fields.get("name"), _MAX_NAME_LEN)
    if len(str(fields.get("name") or "").strip()) > _MAX_NAME_LEN:
        return f"[Error] 角色名过长（最多 {_MAX_NAME_LEN} 字符），请让用户缩短。"
    if not name:
        return "[Error] 角色名无效。"
    return None


async def review_card(card_content: str, review_client, timeout: float = 25.0) -> Optional[str]:
    """Sensitive-content review of the assembled card.

    Returns None if approved (or fail-open on infra error), else a rejection
    reason string. Explicit REJECT = fail-closed; API errors must not brick
    character creation, so they fail open with a warning log.
    """
    if review_client is None:
        return None
    try:
        raw = await review_client.chat_completion(
            message=f"待审查的角色设定卡：\n\n{card_content[:6000]}",
            history=[{"role": "system", "content": _REVIEW_SYSTEM}],
            timeout_set=timeout,
            purpose="char_review",
        )
    except Exception as e:
        logger.warning("create_character review call failed (fail-open): %s", e)
        return None
    verdict = (raw or "").strip()
    if not verdict or verdict.startswith(_LLM_ERROR_PREFIXES):
        logger.warning("create_character review got infra error (fail-open): %s", verdict[:80])
        return None
    first = verdict.split("\n", 1)[0].strip()
    if first.upper().startswith("REJECT"):
        reason = first.split(":", 1)[1].strip() if ":" in first else first
        return reason or "内容未通过安全审查"
    return None


async def create_character(
    *,
    user_id: str,
    fields: Dict[str, Any],
    registry,
    perm_manager,
    review_client=None,
) -> str:
    """Validate → quota → collision → assemble → review → register.

    Returns a user-facing summary string, or "[Error] ..." on failure.
    """
    if not user_id:
        return "[Error] 缺少用户上下文，无法创建角色。"

    err = validate_fields(fields)
    if err:
        return err

    # 配额复查（路由层已挡一道，这里防 LLM 绕过命令直接调工具）
    role = perm_manager.get_role(user_id)
    max_chars = perm_manager.get_max_characters(role)
    owned = registry.list_ugc(user_id)
    if len(owned) >= max_chars:
        return (
            f"[Error] 你的自创角色数量已达上限（{len(owned)}/{max_chars}）。"
            "请告诉用户先用 /删除角色 清理旧角色。"
        )

    name = _clean_str(fields.get("name"), _MAX_NAME_LEN)
    if registry.name_taken(name, user_id):
        return f"[Error] 你已拥有名为「{name}」的角色，请让用户换个名字或先删除旧角色。"

    card = build_card(fields)

    rejection = await review_card(card, review_client)
    if rejection is not None:
        return f"[Error] 角色设定未通过安全审查：{rejection}。请让用户调整设定后重试。"

    char_id = make_char_id(name)
    meta = {
        "char_id": char_id,
        "display_name": name,
        "author": user_id,
        "created_at": int(time.time()),
        "visibility": "private",
        "voice_id": None,
        "source": "ugc",
    }
    try:
        registry.register_character(char_id, card, meta)
    except Exception as e:
        logger.exception("create_character write failed")
        return f"[Error] 角色落盘失败: {type(e).__name__}: {e}"

    return (
        f"✅ 角色「{name}」创建成功（ID: {char_id}）。\n"
        f"请告知用户：现在可以用 /人格切换 {name} 切换到该角色开始相处，"
        "日常聊天会累积好感度、提升羁绊等级；用 /好感度 随时查看羁绊状态，"
        "用 /删除角色 " + name + " 可以删除该角色。"
    )


# create_character 工具的 JSON Schema（注册进 ToolRegistry 用）
CREATE_CHARACTER_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "角色名（≤24字符）"},
        "gender": {"type": "string", "description": "性别（可选）"},
        "personality": {"type": "string", "description": "性格关键词与描述（必填）"},
        "worldview": {"type": "string", "description": "世界观背景（可选，默认现代日常）"},
        "relationship": {"type": "string", "description": "角色与用户的关系设定（可选）"},
        "greeting": {"type": "string", "description": "对用户的初始称呼/开场白（可选）"},
        "speech_style": {"type": "string", "description": "说话风格描述与示例台词（必填）"},
        "appearance": {"type": "string", "description": "外观锚点：发型/发色/瞳色/服饰等固定特征（必填）"},
        "gift_loved": {"type": "array", "items": {"type": "string"}, "description": "喜爱的礼物（≤10项，可选）"},
        "gift_normal": {"type": "array", "items": {"type": "string"}, "description": "普通的礼物（可选）"},
        "gift_disliked": {"type": "array", "items": {"type": "string"}, "description": "讨厌的礼物（可选）"},
        "bond_levels": {"type": "string", "description": "各羁绊等级的态度变化/解锁档案描述（可选）"},
    },
    "required": ["name", "personality", "speech_style", "appearance"],
}

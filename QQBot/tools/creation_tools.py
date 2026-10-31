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

import hashlib
import logging
import time
from typing import Any, Dict, List, Optional

from agent.delegation import _LLM_ERROR_PREFIXES
from agent.character_registry import make_char_id
from lib.media_gen import get_voice_pool

logger = logging.getLogger(__name__)

# 单字段长度上限（防 prompt 注入式超长卡片撑爆 L2）
_MAX_FIELD_LEN = 2000
_MAX_NAME_LEN = 24
_MAX_LIST_ITEMS = 10
_MAX_ITEM_LEN = 60

# 初始羁绊封顶 Lv5（密友）：Lv4/Lv5 的画图/语音门槛可以作为创作预设送出，
# Lv6+（知己/暧昧/恋人…）必须相处挣得，保住养成感。
_MAX_INITIAL_BOND = 5

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


def _coerce_initial_bond(value: Any) -> int:
    """Clamp the interview's initial_bond_level into [1, _MAX_INITIAL_BOND]."""
    try:
        level = int(value)
    except (TypeError, ValueError):
        return 1
    return max(1, min(level, _MAX_INITIAL_BOND))


def _clean_list(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    items = []
    for v in value[:_MAX_LIST_ITEMS]:
        s = str(v).strip()[:_MAX_ITEM_LEN]
        if s:
            items.append(s)
    return items


def assign_voice_id(char_id: str) -> Optional[str]:
    """从 voice_pool 按 char_id 确定性分配固定音色（P4 声纹一致性锚点）。

    与 character_registry.get_voice_id 同款 blake2b 取模（不用内建
    hash()——PYTHONHASHSEED 随机化会跨进程漂移声纹）。注册时写进
    meta.json.voice_id 把音色**钉死**：后续 voice_pool 增删不影响已有
    角色。池空 → default_voice → None（registry 解析链继续兜底）。
    """
    pool, default = get_voice_pool()
    if not pool:
        return default or None
    digest = hashlib.blake2b(
        str(char_id).encode("utf-8"), digest_size=8
    ).hexdigest()
    return pool[int(digest, 16) % len(pool)]


def build_card(fields: Dict[str, Any], voice_id: str = "") -> str:
    """Assemble card.md content from interview fields (§5.3 structure).

    ``voice_id`` 非空时写进元数据注释（与内置卡同款 ``voice_id: X`` 槽位；
    UGC 主通路是 meta.json.voice_id，卡片注释是第二优先级冗余）。
    """
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

    meta_comment = "<!-- mode: split | task_voice: character"
    if voice_id:
        meta_comment += f" | voice_id: {voice_id}"
    meta_comment += " -->"
    lines = [
        f"# {name}",
        meta_comment,
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

    # char_id 先行：voice_id 按其确定性分配并钉进卡注释 + meta（P4 声纹一致性）
    char_id = make_char_id(name)
    voice_id = assign_voice_id(char_id)
    card = build_card(fields, voice_id=voice_id or "")

    rejection = await review_card(card, review_client)
    if rejection is not None:
        return f"[Error] 角色设定未通过安全审查：{rejection}。请让用户调整设定后重试。"

    meta = {
        "char_id": char_id,
        "display_name": name,
        "author": user_id,
        "created_at": int(time.time()),
        "visibility": "private",
        "voice_id": voice_id,
        "source": "ugc",
        # 初始羁绊（1-5）：bond 状态首次创建时对作者本人生效（bond._seed_initial_bond）
        "initial_bond_level": _coerce_initial_bond(fields.get("initial_bond_level")),
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
        "initial_bond_level": {
            "type": "integer",
            "description": (
                "初始羁绊等级 1-5（可选，默认1=陌生；5=密友）。"
                "用户与角色初次建立羁绊时按此等级起步，之后照常相处升级。"
            ),
        },
    },
    "required": ["name", "personality", "speech_style", "appearance"],
}

"""AG LLM Resolver — L4 narrow LLM fallback (Phase D).

Classifies the trigger of ``conditional`` / ``needs_context`` action-gauge
effects (the nested-subskill cases L1 cannot resolve) into the 15-class
taxonomy of ``docs/speedcheck-trigger-correction.md`` §4, using an
independent flash-model instance with a game-specialised prompt.  Called
**on demand only** (when such items exist) and degrades gracefully to
"keep for human confirmation" when the client/network is unavailable.

Window-feasibility is NOT decided by the model: the trigger class is mapped
deterministically onto ``window_feasible`` via the L1 tables.
"""

from __future__ import annotations

import json
import os
import re

import httpx

_L4_TIMEOUT = 60.0

# The side-aware taxonomy the model may answer with (must mirror
# ``ag_skill_index._TRIGGER_RULES``).
_TRIGGER_CLASSES = (
    "battle_start", "on_skill_use",
    "on_ally_attack", "on_ally_aoe", "on_ally_nonattack", "on_ally_skill",
    "on_ally_hit", "on_ally_extra_turn", "on_ally_turn_end", "on_ally_crit",
    "on_self_crit", "on_hit", "on_follow_up", "on_evade",
    "on_enemy_aoe", "on_enemy_nonattack_skill", "on_enemy_extra_turn",
    "on_enemy_turn_end", "on_kill", "on_death",
    "on_morale_full", "on_focus_full", "conditional",
)

_L4_SYSTEM = """\
你是《Ark Re:Code》技能文案分析引擎。任务：把给定技能文案中「行动值提升/下降」\
效果的触发条件归入下列 trigger 类之一。

测速窗口事实（战斗开始→首动行动完毕）：全员满血、无减益、无死亡、无闪避、\
敌方未行动；首动角色可能攻击/施展技能/暴击/触发追击。

trigger 类（「我方/敌方」按触发者阵营严格区分）：
- battle_start 战斗开始时生效
- on_skill_use 主动技能自身（攻击后/施展后）
- on_ally_attack 我方成员攻击命中后
- on_ally_aoe 我方成员施展全体攻击后
- on_ally_nonattack 我方成员施展非攻击技能（Buff/Heal）后
- on_ally_skill 我方成员施展技能后（无全体/非攻击限定）
- on_ally_hit 我方成员受到攻击后
- on_ally_extra_turn 我方成员产生追加回合时
- on_ally_turn_end 我方成员回合结束后
- on_ally_crit 我方成员暴击后
- on_self_crit 自身暴击时
- on_hit 自身受到攻击时
- on_follow_up 受到追加技能/反击/追击时
- on_evade 自身闪避时
- on_enemy_aoe 敌人施展全体攻击时
- on_enemy_nonattack_skill 敌人施展非攻击技能时
- on_enemy_extra_turn 敌人产生追加回合时
- on_enemy_turn_end 敌方成员回合结束时
- on_kill 消灭敌人时 / on_death 任一成员死亡时
- on_morale_full 战意达到最大值时 / on_focus_full 集中力达到最大值时
- conditional 纯条件（HP/状态/冷却，无明确事件触发）

规则：
1. 行动值效果嵌套在子技能里时穿透子技能：把嵌套效果归到触发该子技能的条件上。
2. 「敌方/我方」前缀必须严格区分；全体攻击与非攻击技能要细分到 \
on_ally_aoe / on_ally_nonattack，不要笼统归 on_ally_skill。
3. 只输出 JSON 数组，元素为 {"i": 编号, "trigger": 类名, "window_note": \
窗口内能否触发的一句话判定}，不要任何其他内容。

示例：
输入：
1. 璀璨誓约的露比「焦点新娘」：我方成员受到攻击且生命力低于50%时，\
施展「传递幸福」，使自身行动值提升20%。
2. 乌尔德「有限干涉」：我方成员受到全体攻击后，有50%概率施展「干涉」，\
使自身行动值提升15%。
输出：
[{"i": 1, "trigger": "conditional", "window_note": \
窗口内满血，HP<50% 不成立，不触发"},
 {"i": 2, "trigger": "on_ally_aoe", "window_note": \
若首动为全体攻击则触发（概率性，需确认）"}]
"""


def _flash_cfg() -> dict | None:
    """FLASH_MODEL section (or .env DeepSeek fallback); None when unconfigured."""
    try:
        from lib.multimodal_client import MultimodalClient
        section = MultimodalClient().get_section("FLASH_MODEL")
    except Exception:
        section = None
    if section and section.get("api_key") and section.get("api_base"):
        return {
            "api_key": section["api_key"],
            "api_base": section["api_base"],
            "model": section.get("model") or "deepseek-chat",
        }
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        return None
    return {
        "api_key": api_key,
        "api_base": os.getenv("DEEPSEEK_API_BASE", "https://api.deepseek.com/v1"),
        "model": "deepseek-chat",
    }


def _skill_text(index: dict, char: str, skill_name: str) -> str:
    ch = index.get(char) or {}
    for sk in ch.get("skills", []):
        # index values are Skill objects (dicts when loaded from JSON cache)
        name = getattr(sk, "name", None) or (sk.get("name")
                                              if isinstance(sk, dict) else "")
        if name == skill_name:
            return getattr(sk, "text", None) or (sk.get("text")
                                                 if isinstance(sk, dict)
                                                 else "") or ""
    return ""


async def resolve_uncertain(items: list[dict], index: dict) -> list[dict] | None:
    """L4-pass over *items* (uncertain hypothesis entries, trigger=conditional).

    Returns a list parallel to *items* with added ``l4_trigger`` /
    ``window_feasible`` / ``window_note``, or None when the LLM is
    unavailable / the answer is unparseable (callers keep the items as
    "human confirmation").
    """
    cfg = _flash_cfg()
    if not cfg or not items:
        return None

    lines = []
    for i, e in enumerate(items, 1):
        text = _skill_text(index, e.get("char", ""), e.get("skill_name", ""))
        lines.append(
            f"{i}. {e.get('char', '')}「{e.get('skill_name', '')}」"
            f"（{e.get('skill', '')}）：{text or e.get('note', '')}"
        )
    user = "输入：\n" + "\n".join(lines) + "\n输出："

    body = {
        "model": cfg["model"],
        "temperature": 0.0,
        "messages": [
            {"role": "system", "content": _L4_SYSTEM},
            {"role": "user", "content": user},
        ],
    }
    headers = {
        "Authorization": f"Bearer {cfg['api_key']}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient() as http:
            resp = await http.post(
                f"{cfg['api_base']}/chat/completions",
                headers=headers, json=body, timeout=_L4_TIMEOUT,
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"] or ""
    except Exception:
        return None

    m = re.search(r"\[.*\]", content, re.S)
    try:
        answers = json.loads(m.group(0) if m else content)
    except (json.JSONDecodeError, TypeError, AttributeError):
        return None
    if not isinstance(answers, list):
        return None
    by_i = {
        a.get("i"): a for a in answers
        if isinstance(a, dict) and isinstance(a.get("i"), int)
    }

    from tools.ag_skill_index import _WINDOW_FEASIBLE

    out: list[dict] = []
    for i, e in enumerate(items, 1):
        a = by_i.get(i)
        trigger = (a or {}).get("trigger")
        if trigger not in _TRIGGER_CLASSES:
            return None  # model went off-taxonomy → treat whole pass as failed
        out.append({
            "char": e.get("char"),
            "skill": e.get("skill"),
            "skill_name": e.get("skill_name"),
            "l4_trigger": trigger,
            "window_feasible": trigger in _WINDOW_FEASIBLE,
            "window_note": str((a or {}).get("window_note", "")).strip(),
        })
    return out

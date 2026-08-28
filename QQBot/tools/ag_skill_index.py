"""AG Skill Index — structured action-gauge (拉条/推条) skill extraction.

Builds a per-character index of :class:`Skill` objects from
``data/wiki_cache/character_details.json``.  Every skill carries the
five classification axes mandated by the speed-check redesign (attack
behaviour / scope / active-passive nature / follow-up / counter-able),
its *trigger modes* (which events it reacts to, side-aware) and one or
more ``ag_effects`` — each recording *direction* (pull/push),
*magnitude*, *target*, *trigger* (side-aware event taxonomy) and an
optional *condition gate* (hp / state).

Trigger attribution is **per-sentence**: an AG effect only sees trigger
keywords inside its own sentence (earlier clauses first, longest
keyword wins) or inherited from the parent sentence of a sub-skill
block (``大肆庆贺：…``).  The old full-text fallback that bled
``battle_start`` across sentences is gone.

This is the **L1 offline extraction** layer — deterministic
(regex + keyword).  Output cache ``data/wiki_cache/ag_skill_index.json``
keeps the ``scraped_at + data`` envelope; skills serialize via
``Skill.to_dict``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

_PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DEFAULT_CHAR_CACHE = os.path.join(
    _PROJECT_DIR, "data", "wiki_cache", "character_details.json"
)
_DEFAULT_OUT = os.path.join(
    _PROJECT_DIR, "data", "wiki_cache", "ag_skill_index.json"
)

_PULL_VERBS = ("提升", "增加", "提高", "上升")
_PUSH_VERBS = ("下降", "降低", "减少", "削减")
_DIRECTION_VERB_RE = re.compile(r"提升|增加|提高|上升|下降|降低|减少|削减")


def _has_ag(text: str) -> bool:
    return "行动值" in text


# ── Negation guard ───────────────────────────────────────────────────
# Phrases that mention 行动值 but do NOT manipulate it: resistance /
# amplification of an existing AG effect, or immunity.
_NEGATE_RE = re.compile(
    r"行动值(?:提升|下降|降低|增加)(?:效果|量)(?:减少|降低|下降|增加|提升)"
    r"|不受行动值[^。；\n]{0,12}影响"
    r"|行动值(?:下降|提升)(?:效果|量)"
)


def _is_negated(clause: str, ag_pos: int) -> bool:
    """True if the 行动值 occurrence at *ag_pos* is a resistance/immunity phrase."""
    window = clause[max(0, ag_pos - 6): ag_pos + 12]
    return bool(_NEGATE_RE.search(window))


# ── Magnitude ────────────────────────────────────────────────────────

# A master matcher for the known magnitude expression formats (§4 关键事实 3).
_MAG_RE = re.compile(
    r"\[\s*(?P<r1>\d+(?:\.\d+)?)\s*%?\s*[-–]\s*(?P<r2>\d+(?:\.\d+)?)\s*%?\s*\]"  # [a-b%]
    r"|(?P<t1>\d+(?:\.\d+)?)\s*[%~]\s*(?P<t2>\d+(?:\.\d+)?)\s*%"                    # a%~b%
    r"|(?:目标数量|目标数|每一个目标|每个目标|敌军目标数)\s*[×xX]\s*(?P<pt>\d+(?:\.\d+)?)\s*%"  # 目标数量x6%
    r"|(?:数量|成员数量|负向状态数量)\s*[×xX]\s*(?P<pc>\d+(?:\.\d+)?)\s*%"            # 数量x8%
    r"|(?P<f>\d+(?:\.\d+)?)\s*%"                                                      # 固定 NN%
)


def _match_magnitude(text: str) -> dict | None:
    """Return the first magnitude expression in *text*, or None."""
    m = _MAG_RE.search(text)
    if not m:
        return None
    raw = m.group(0).strip()
    if m.group("r1") is not None:
        return {"kind": "range", "raw": raw,
                "min": float(m.group("r1")), "max": float(m.group("r2"))}
    if m.group("t1") is not None:
        return {"kind": "range", "raw": raw,
                "min": float(m.group("t1")), "max": float(m.group("t2"))}
    if m.group("pt") is not None:
        return {"kind": "per_target", "raw": raw, "per": float(m.group("pt"))}
    if m.group("pc") is not None:
        return {"kind": "per_count", "raw": raw, "per": float(m.group("pc"))}
    return {"kind": "fixed", "raw": raw, "value": float(m.group("f"))}


# ── Target ───────────────────────────────────────────────────────────

# Ordered: more specific targets first.
_TARGET_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("allies_except_self", ("自身除外的全体我方", "自身除外的我方", "自身以外的我方",
                            "自身除外")),
    ("all_allies", ("全体我方", "我方全体", "我方全体的", "全体我方成员")),
    ("highest_ag_enemy", ("行动值最高的敌人", "行动值最大的敌人")),
    ("all_enemies", ("全体敌人", "所有敌人", "剩余所有敌人")),
    ("self", ("自身", "自己")),
    ("single_ally", ("我方目标", "我方成员", "随机我方成员")),
    ("single_enemy", ("敌人", "目标")),
]


def _match_target(text: str) -> str | None:
    for label, kws in _TARGET_RULES:
        for kw in kws:
            if kw in text:
                return label
    return None


# ── Trigger taxonomy (side-aware event classes) ─────────────────────
# Ordered roughly specific→generic; the matcher prefers the LONGEST
# keyword inside the effect's sentence so e.g. 「我方成员施展全体攻击」
# beats 「我方成员施展」 regardless of list order.
_TRIGGER_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("battle_start", ("首次战斗开始", "战斗开始", "进入战斗")),
    # 敌方非攻击性技能触发（原 on_enemy_skill）
    ("on_enemy_nonattack_skill", ("敌人施展非攻击技能", "敌方使用非攻击技能",
                                  "敌人使用非攻击技能", "敌方施展非攻击技能")),
    # 敌方全体攻击触发
    ("on_enemy_aoe", ("敌人施展全体攻击", "敌方全体攻击", "敌人使用全体攻击",
                      "敌人发动全体攻击")),
    # 敌方追加回合触发 / 我方追加回合触发
    ("on_enemy_extra_turn", ("敌人产生追加回合", "敌方产生追加回合",
                             "敌人获得追加回合")),
    ("on_ally_extra_turn", ("自身除外的我方成员产生追加回合",
                            "我方成员产生追加回合", "我方产生追加回合")),
    ("on_enemy_turn_end", ("敌方成员回合结束", "敌方回合结束", "敌人回合结束",
                           "任一敌人回合结束")),
    ("on_ally_turn_end", ("我方成员回合结束", "我方回合结束",
                          "自身除外的我方成员回合结束")),
    ("on_follow_up", ("受到追加技能", "受到追加", "反击", "追击", "发动追击")),
    ("on_evade", ("闪避",)),
    ("on_kill", ("消灭敌人", "敌人死亡", "消灭目标", "击杀")),
    ("on_death", ("任一成员死亡", "我方成员死亡", "成员死亡")),
    ("on_ally_crit", ("我方成员发生暴击", "我方成员暴击")),
    ("on_self_crit", ("发生暴击时", "暴击时", "若发生暴击")),
    # 我方全体攻击触发（必须先于泛化的 on_ally_skill 匹配——长词优先保证）
    ("on_ally_aoe", ("我方成员施展全体攻击", "我方施展全体攻击",
                     "我方成员使用全体攻击", "我方成员发动全体攻击")),
    # 我方非攻击性技能触发
    ("on_ally_nonattack", ("我方成员施展非攻击技能", "我方施展非攻击技能")),
    # 我方角色被攻击触发（长于 on_hit 的「受到攻击」，避免吞掉我方前缀）
    ("on_ally_hit", ("我方成员受到攻击", "我方成员被攻击", "我方受到攻击")),
    # 我方角色攻击触发
    ("on_ally_attack", ("我方成员攻击", "我方攻击", "攻击命中时")),
    ("on_ally_skill", ("我方成员施展", "我方施展", "我方成员使用")),
    # 自身被攻击触发
    ("on_hit", ("受到攻击", "受击")),
    # 战意条件触发 / 集中力条件触发
    ("on_morale_full", ("战意达到最大值", "战意达到最大")),
    ("on_focus_full", ("集中力达到最大值", "集中力达到最大")),
    ("conditional", ("若", "以下时", "以上时", "战意达到", "达到最大值",
                     "处于", "状态时", "生命力为")),
]


def _classify_in_scope(scope: str, ag_pos: int | None = None) -> str | None:
    """Classify the trigger for an AG effect from *scope* text only.

    Longest keyword wins; ties prefer the occurrence nearest before the
    effect position (triggers precede their effect).  Never searches
    outside *scope* — sentence boundaries are hard stops.
    """
    cands: list[tuple[int, int, str]] = []
    for trigger, kws in _TRIGGER_RULES:
        # morale/focus gates are the true firing condition when present
        # (奥柏丝蒂恩: 任一敌人回合结束…若战意达到最大值…行动值+40%).
        bonus = 2 if trigger in ("on_morale_full", "on_focus_full") else 0
        for kw in kws:
            for m in re.finditer(re.escape(kw), scope):
                cands.append((m.start(), len(kw) + bonus, trigger))
    if not cands:
        return None
    limit = ag_pos if ag_pos is not None else len(scope)
    before = [c for c in cands if c[0] <= limit]
    pool = before or cands
    return max(pool, key=lambda c: (c[1], c[0]))[2]


# ── Condition gates (血量 / buff-debuff 状态) ────────────────────────
# Gates are NOT event triggers: they parse into the effect's
# ``condition`` field (kind/side/pct) instead of the trigger key.

_HP_RE = re.compile(r"(目标敌人|敌人)?的?生命?力为\s*(\d+(?:\.\d+)?)\s*%?\s*以下")
_STATE_RE = re.compile(r"处于「([^」]+)」状态")
_DEBUFF_RE = re.compile(r"(自身除外的我方成员|我方成员|自身|目标|敌人)带(?:减益|负向状态)")


def _extract_condition(scope: str) -> dict | None:
    m = _HP_RE.search(scope)
    if m:
        if m.group(1):
            side = "enemy"
        elif "我方" in scope:
            side = "ally"
        else:
            side = "self"
        return {"kind": "hp", "side": side, "op": "<=",
                "pct": float(m.group(2))}
    m = _STATE_RE.search(scope)
    if m:
        side = "enemy" if "敌人" in scope[:m.start()] else \
            ("ally" if "我方" in scope[:m.start()] else "self")
        return {"kind": "state", "side": side, "name": m.group(1)}
    m = _DEBUFF_RE.search(scope)
    if m:
        who = m.group(1)
        side = "enemy" if who == "敌人" else \
            ("self" if who == "自身" else "ally")
        return {"kind": "state", "side": side, "name": "减益"}
    return None


# ── Observability / window-feasibility (§4.1) ───────────────────────

# Pure-random prerequisites that the screenshot can never confirm (§4.1).
_UNOBSERVABLE = {"on_evade"}

# Triggers that can fire within the measurement window (battle start →
# first actor finishes) — the ✅ column of the §4 table.
_WINDOW_FEASIBLE = {
    "battle_start", "on_skill_use", "on_ally_attack", "on_ally_skill",
    "on_ally_turn_end", "on_ally_crit", "on_hit", "on_self_crit",
    "on_follow_up", "on_ally_aoe", "on_ally_nonattack",
    "on_enemy_nonattack_skill", "on_enemy_aoe",
    "on_ally_extra_turn", "on_enemy_extra_turn", "on_ally_hit",
}


# ── Category / attack-axis inference ────────────────────────────────

_HEAL_KW = ("恢复", "治疗", "治愈", "回复")
# Compound damage verbs — a bare 「攻击」 must NOT count (攻击力提升 is a
# buff, 非攻击技能 is a negation): this was the 爱莉卡 S2 misclass root.
_ATTACK_VERB_RE = re.compile(
    r"攻击(?:敌人|目标|全体敌人|敌方)|斩击|斩裂|射击|打击|射击敌人"
    r"|造成[^，。；\n]{0,10}伤害|骂倒攻击|连续斩击|贯穿[^，。；\n]{0,6}防御"
)
_AOE_RE = re.compile(r"全体敌人|所有敌人|全体敌方|所有敌方|全体目标|所有目标")
_DUAL_RE = re.compile(r"2名敌人|两名敌人|2体|双体")


def _masked(text: str) -> str:
    """Mask negation contexts so 「非攻击」 never reads as an attack."""
    return text.replace("非攻击", "???")


def _infer_category(des: str) -> str:
    clean = _masked(des)
    if any(kw in clean for kw in _HEAL_KW) \
            and not _ATTACK_VERB_RE.search(clean):
        return "Heal"
    if _ATTACK_VERB_RE.search(clean):
        return "Attack"
    return "Buff"


# ── Axis regexes ─────────────────────────────────────────────────────

_NO_COUNTER_RE = re.compile(r"不会被反击|无法反击|不可反击|不能反击|"
                            r"此攻击不会发动追击|不会发动追击")
_FOLLOW_UP_RE = re.compile(r"追加攻击|进行反击|反击敌人|发动追击|受到追加")
# 敌人产生追加回合 is REACTING to an enemy extra turn, not producing one
# (猫祭「疯猫的混沌」) → negative lookbehind on the producer.
_EXTRA_TURN_RE = re.compile(r"(?<!敌人)(?<!敌方)(?:产生|生成|获得)追加回合")
_SUBSKILL_CAST_RE = re.compile(r"施展「([^」]+)」")
_SUBSKILL_HEAD_RE = re.compile(r"^「?([^：「」，,、\n]{1,12})」?：(.+)$")


# ── Clause splitting ─────────────────────────────────────────────────

_SENT_SPLIT = re.compile(r"[。；;\n]")
_CLAUSE_SPLIT = re.compile(r"[，,、]")


# ── Per-occurrence AG effect extraction ──────────────────────────────

def _extract_ag_effects(clause: str) -> list[dict]:
    """Extract every 行动值 effect in a single clause.

    Handles both phrasings — 行动值-first (``行动值提升50%``) and verb-first
    (``提升自身行动值50%`` / ``提高自身25%行动值``) — and multiple 行动值
    occurrences within one clause (e.g. 爱莉卡 S1).
    """
    effects: list[dict] = []

    for m in re.finditer("行动值", clause):
        ag_pos = m.start()
        if _is_negated(clause, ag_pos):
            continue

        # 1. Find the direction verb — first look right of 行动值, else left.
        right = clause[ag_pos + 3: ag_pos + 12]
        rm = _DIRECTION_VERB_RE.search(right)
        if rm and rm.start() <= 5:
            verb = rm.group(0)
            verb_pos = ag_pos + 3 + rm.start()
        else:
            left = clause[max(0, ag_pos - 8): ag_pos]
            lms = list(_DIRECTION_VERB_RE.finditer(left))
            if not lms:
                continue
            lm = lms[-1]
            verb = lm.group(0)
            verb_pos = max(0, ag_pos - 8) + lm.start()

        direction = "pull" if verb in _PULL_VERBS else "push"

        # 2. Magnitude — search right of 行动值 first, then between verb/行动值.
        #    Window is wide enough for "…行动值提升相当于…成员数量x5%" phrasing.
        magnitude = _match_magnitude(clause[ag_pos + 3: ag_pos + 30])
        if magnitude is None and verb_pos < ag_pos:
            magnitude = _match_magnitude(clause[verb_pos + 1: ag_pos + 30])

        # 3. Target — read from the ~16 chars before 行动值 (or between verb
        #    and 行动值 for the verb-first form).
        pre = clause[max(0, ag_pos - 16): ag_pos]
        target = _match_target(pre)
        if target is None and verb_pos < ag_pos:
            target = _match_target(clause[verb_pos + 1: ag_pos])

        effects.append({
            "direction": direction,
            "magnitude": magnitude,
            "target": target,
            "clause": clause[:120],
            "_ag_pos": ag_pos,
        })

    return effects


@dataclass
class Skill:
    """One skill, fully classified from its wiki description text.

    Axes (user-mandated):
      * ``has_attack``     是否有攻击行为
      * ``attack_scope``   全体攻击 aoe / 单体攻击 single / 双体攻击 dual /
                           非攻击技能 non_attack
      * ``nature``         active_basic 主动基础技能(无冷却) /
                           active_non_basic 主动非基础技能(有冷却) /
                           passive_trigger 被动触发技能(有冷却) /
                           pure_passive 被动(无冷却,不视为技能)
      * ``is_follow_up_skill`` 是否为追加技能
      * ``can_be_counter``     是否能被反击

    ``trigger_modes`` lists every event class the skill reacts to
    (敌方全体攻击触发 / 我方追加回合触发 / 战意条件触发 …); each
    ``ag_effects`` entry carries its own ``trigger`` plus an optional
    ``condition`` gate ({kind: hp|state, side, …}).
    """

    char_name: str = ""
    slot: int = 0
    name: str = ""
    name_en: str = ""
    type: str = ""
    cd: str = ""
    category: str = "Buff"
    text: str = ""

    has_attack: bool = False
    attack_scope: str = "non_attack"
    nature: str = "pure_passive"
    is_follow_up_skill: bool = False
    can_be_counter: bool = False
    generates_extra_turn: bool = False
    trigger_modes: list[str] = field(default_factory=list)
    ag_effects: list[dict] = field(default_factory=list)

    # ── (de)serialization ──
    def to_dict(self) -> dict:
        return {
            "char_name": self.char_name,
            "name": self.name, "name_en": self.name_en, "type": self.type,
            "cd": self.cd, "category": self.category, "slot": self.slot,
            "has_attack": self.has_attack, "attack_scope": self.attack_scope,
            "nature": self.nature,
            "is_follow_up_skill": self.is_follow_up_skill,
            "can_be_counter": self.can_be_counter,
            "generates_extra_turn": self.generates_extra_turn,
            "trigger_modes": list(self.trigger_modes),
            "ag_effects": self.ag_effects,
            "text": self.text,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Skill":
        return cls(
            char_name=d.get("char_name", ""), slot=d.get("slot", 0),
            name=d.get("name", ""), name_en=d.get("name_en", ""),
            type=d.get("type", ""), cd=d.get("cd", ""),
            category=d.get("category", "Buff"), text=d.get("text", ""),
            has_attack=bool(d.get("has_attack")),
            attack_scope=d.get("attack_scope", "non_attack"),
            nature=d.get("nature", "pure_passive"),
            is_follow_up_skill=bool(d.get("is_follow_up_skill")),
            can_be_counter=bool(d.get("can_be_counter")),
            generates_extra_turn=bool(d.get("generates_extra_turn")),
            trigger_modes=list(d.get("trigger_modes") or []),
            ag_effects=list(d.get("ag_effects") or []),
        )


def _parse_skill(skill: dict, char_name: str, slot: int) -> Skill:
    """Parse one raw wiki skill dict into a classified :class:`Skill`."""
    skill_type = str(skill.get("type", "")).strip()
    cd = str(skill.get("cd", "")).strip()

    text_cn = "\n".join(
        str(skill.get(k, "")) for k in ("name", "des", "des2", "burst")
    )
    full_text = "\n".join(
        str(skill.get(k, ""))
        for k in ("name", "des", "des2", "burst", "des_en", "des2_en",
                  "burst_en")
    )

    masked = _masked(text_cn)
    has_attack = bool(_ATTACK_VERB_RE.search(masked))
    if not has_attack:
        attack_scope = "non_attack"
    elif _AOE_RE.search(masked):
        attack_scope = "aoe"
    elif _DUAL_RE.search(masked):
        attack_scope = "dual"
    else:
        attack_scope = "single"

    if slot == 0 and skill_type == "Active":
        nature = "active_basic"
    elif skill_type == "Active":
        nature = "active_non_basic"
    elif cd:
        nature = "passive_trigger"
    else:
        nature = "pure_passive"

    sk = Skill(
        char_name=char_name, slot=slot,
        name=skill.get("name") or skill.get("name_en", ""),
        name_en=skill.get("name_en", ""),
        type=skill_type, cd=cd,
        category=_infer_category(text_cn),
        text=full_text,
        has_attack=has_attack,
        attack_scope=attack_scope,
        nature=nature,
        is_follow_up_skill=bool(_FOLLOW_UP_RE.search(text_cn)),
        can_be_counter=has_attack and not _NO_COUNTER_RE.search(text_cn)
        and not bool(_FOLLOW_UP_RE.search(text_cn)),
        generates_extra_turn=bool(_EXTRA_TURN_RE.search(text_cn)),
    )

    # ── Per-sentence trigger attribution ──
    sentences = [s.strip() for s in _SENT_SPLIT.split(text_cn) if s.strip()]

    # Pass 1: register sub-skill trigger contexts — 「…施展「X」」 records
    # the parent sentence's trigger so the later 「X：…」 block inherits it
    # (新春的蜜娜 S2 大肆庆贺 case).
    sub_ctx: dict[str, tuple[str, dict | None]] = {}
    for sent in sentences:
        for m in _SUBSKILL_CAST_RE.finditer(sent):
            trig = _classify_in_scope(sent[:m.end()])
            # gate-only parents (若…状态时) carry no event information —
            # let the sub-skill fall back to its own scope instead.
            if trig is not None and trig != "conditional":
                sub_ctx[m.group(1)] = (trig, _extract_condition(sent[:m.end()]))

    effects: list[dict] = []
    modes: set[str] = set()
    for sent in sentences:
        head = _SUBSKILL_HEAD_RE.match(sent)
        inherited = sub_ctx.get(head.group(1)) if head else None
        body = head.group(2) if head else sent
        clauses = [c.strip() for c in _CLAUSE_SPLIT.split(body) if c.strip()]

        # scope grows clause by clause; a trigger may sit in any earlier
        # clause of the SAME sentence (never across sentences).
        for i, clause in enumerate(clauses):
            if not _has_ag(clause):
                continue
            scope = "，".join(clauses[: i + 1])
            for eff in _extract_ag_effects(clause):
                ag_pos = eff.pop("_ag_pos")
                # position within the joined scope (offset by earlier clauses)
                pos_in_scope = len(scope) - len(clause) + ag_pos
                # finer clause splitting can strip the target context that
                # lived in an earlier sub-clause — backfill from the scope.
                if eff["target"] is None:
                    eff["target"] = _match_target(
                        scope[max(0, pos_in_scope - 20): pos_in_scope])
                # Implicit target: a bare "行动值下降/降低" pushes the
                # attacked enemy.
                if eff["target"] is None and eff["direction"] == "push":
                    eff["target"] = "single_enemy"
                trigger = _classify_in_scope(scope, pos_in_scope)
                condition = _extract_condition(scope)
                needs_context = False
                if trigger is None and inherited is not None:
                    trigger, pcond = inherited
                    condition = condition or pcond
                if trigger is None and skill_type == "Active":
                    trigger = "on_skill_use"
                if trigger is None:
                    trigger = "conditional"
                if trigger == "conditional":
                    needs_context = True

                # battle_start AG is almost always a self-pull (e.g. 蕾蜜莉);
                # reactive passives pulling without an explicit target pull
                # themselves (ally-targeted pulls always name 我方成员).
                if eff["target"] is None and eff["direction"] == "pull" and (
                        trigger == "battle_start"
                        or nature in ("passive_trigger", "pure_passive")):
                    eff["target"] = "self"

                eff.update({
                    "trigger": trigger,
                    "condition": condition,
                    "observable": trigger not in _UNOBSERVABLE,
                    "window_feasible": trigger in _WINDOW_FEASIBLE,
                    "needs_context": needs_context,
                })
                effects.append(eff)
                modes.add(trigger)

    for trig, _cond in sub_ctx.values():
        modes.add(trig)
    sk.ag_effects = effects
    sk.trigger_modes = sorted(modes)
    return sk


# ── Index builder ────────────────────────────────────────────────────

def build_index(cache_path: str | None = None) -> dict:
    path = cache_path or _DEFAULT_CHAR_CACHE
    if not os.path.exists(path):
        return {}

    with open(path, "r", encoding="utf-8") as f:
        cache = json.load(f)

    data = cache.get("data", {})
    index: dict[str, dict] = {}

    for _title, entry in data.items():
        if not isinstance(entry, dict):
            continue
        name = entry.get("name_cn") or entry.get("title", "")
        if not name:
            continue

        skills = entry.get("skills") or []
        parsed_skills = []
        for i, s in enumerate(skills):
            if not isinstance(s, dict):
                continue
            parsed_skills.append(_parse_skill(s, name, i))

        index[name] = {
            "element": entry.get("element", ""),
            "class_cn": entry.get("class_cn", ""),
            "stars": entry.get("stars", ""),
            "skills": parsed_skills,
        }

    return index


# ── Report ───────────────────────────────────────────────────────────

def _report(index: dict) -> str:
    lines: list[str] = []
    trigger_counts: dict[str, int] = {}
    dir_counts = {"pull": 0, "push": 0}
    mag_kinds: dict[str, int] = {}
    effect_count = 0
    needs_context_chars: list[str] = []
    no_mag: list[str] = []
    no_target: list[str] = []
    nature_counts: dict[str, int] = {}
    scope_counts: dict[str, int] = {}

    for name, ch in index.items():
        for s in ch.get("skills", []):
            nature_counts[s.nature] = nature_counts.get(s.nature, 0) + 1
            scope_counts[s.attack_scope] = \
                scope_counts.get(s.attack_scope, 0) + 1
            for e in s.ag_effects:
                effect_count += 1
                t = e["trigger"]
                trigger_counts[t] = trigger_counts.get(t, 0) + 1
                dir_counts[e["direction"]] = \
                    dir_counts.get(e["direction"], 0) + 1
                mag = e.get("magnitude")
                mag_kinds[mag["kind"] if mag else "none"] = \
                    mag_kinds.get(mag["kind"] if mag else "none", 0) + 1
                if e.get("needs_context"):
                    needs_context_chars.append(f"{name}/{s.name} → {t}")
                if mag is None:
                    no_mag.append(f"{name}/{s.name}: {e['clause']}")
                if e.get("target") is None:
                    no_target.append(f"{name}/{s.name}: {e['clause']}")

    lines.append(f"characters indexed: {len(index)}")
    lines.append(f"total ag_effects extracted: {effect_count}")
    lines.append(f"direction: {dir_counts}")
    lines.append(f"nature distribution: {nature_counts}")
    lines.append(f"attack_scope distribution: {scope_counts}")
    lines.append(f"magnitude kinds: {mag_kinds}")
    lines.append("trigger distribution:")
    for t in sorted(trigger_counts, key=trigger_counts.get, reverse=True):
        lines.append(f"  {t:24s} {trigger_counts[t]}")
    lines.append(f"needs_context count: {len(needs_context_chars)}")
    for c in needs_context_chars[:40]:
        lines.append(f"  ⚠ {c}")
    lines.append(f"no-magnitude count: {len(no_mag)}")
    for c in no_mag[:20]:
        lines.append(f"  ? {c}")
    lines.append(f"no-target count: {len(no_target)}")
    for c in no_target[:30]:
        lines.append(f"  · {c}")
    return "\n".join(lines)


# ── CLI ──────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="Build the AG skill index (L1).")
    ap.add_argument("--cache", default=None, help="character_details.json path")
    ap.add_argument("--out", default=None, help="output json path")
    ap.add_argument("--char", action="append", default=[],
                    help="also print per-character detail for this name (repeatable)")
    args = ap.parse_args()

    index = build_index(args.cache)
    print(_report(index))

    out_path = args.out or _DEFAULT_OUT
    envelope = {
        "scraped_at": None,
        "source": "character_details.json",
        "data": {
            name: {
                "element": ch.get("element", ""),
                "class_cn": ch.get("class_cn", ""),
                "stars": ch.get("stars", ""),
                "skills": [s.to_dict() for s in ch.get("skills", [])],
            }
            for name, ch in index.items()
        },
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(envelope, f, ensure_ascii=False, indent=2)
    print(f"\nwrote {out_path}")

    for name in args.char:
        if name in index:
            ch = index[name]
            print(f"\n=== {name} ===")
            print(json.dumps(
                {**ch, "skills": [s.to_dict() for s in ch["skills"]]},
                ensure_ascii=False, indent=2))
        else:
            print(f"\n=== {name} NOT FOUND ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

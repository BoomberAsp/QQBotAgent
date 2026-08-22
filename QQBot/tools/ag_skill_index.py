"""AG Skill Index — structured action-gauge (拉条/推条) skill extraction.

Builds a per-character index of action-gauge skills from
``data/wiki_cache/character_details.json``.  For every skill that mentions
行动值 manipulation, extracts one or more ``ag_effects`` — each recording the
effect's *direction* (pull/push), *magnitude*, *target*, and *trigger*
(classified into the 15-class taxonomy of
``docs/speedcheck-trigger-correction.md`` §4).

This is the **L1 offline extraction** layer.  It is deliberately deterministic
(regex + keyword); ``conditional`` / nested-subskill cases that the parser
cannot resolve are marked ``needs_context`` for the Phase-D LLM pass.

Output is a cache file ``data/wiki_cache/ag_skill_index.json`` with the same
``scraped_at + data`` envelope as ``character_details.json``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
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


# ── Trigger classification (15 classes, §4) ─────────────────────────

_TRIGGER_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("battle_start", ("首次战斗开始", "战斗开始", "进入战斗")),
    ("on_enemy_skill", ("敌人施展非攻击技能", "敌方使用非攻击技能",
                        "敌人使用非攻击技能", "敌方施展非攻击技能")),
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
    # 区分「我方施展全体攻击」与「我方施展非攻击技能」——必须先于泛化的
    # on_ally_skill（「我方成员施展」）匹配，且带「我方」前缀，避免把
    # 「敌人施展非攻击技能」（on_enemy_skill）误判成我方触发。
    ("on_ally_aoe", ("我方成员施展全体攻击", "我方施展全体攻击",
                     "我方成员使用全体攻击")),
    ("on_ally_nonattack", ("我方成员施展非攻击技能", "我方施展非攻击技能")),
    ("on_ally_skill", ("我方成员施展", "我方施展", "我方成员使用")),
    ("on_ally_attack", ("我方成员攻击", "我方攻击", "攻击命中时")),
    ("on_hit", ("受到攻击", "受击")),
    ("conditional", ("若", "以下时", "以上时", "战意达到", "达到最大值",
                     "处于", "状态时", "生命力为")),
]


def _classify_trigger(text: str) -> str | None:
    for trigger, kws in _TRIGGER_RULES:
        for kw in kws:
            if kw in text:
                return trigger
    return None


def _classify_trigger_near(clause: str, ag_pos: int) -> str | None:
    """Classify by the trigger keyword *closest* to the 行动值 occurrence.

    Prefers the nearest preceding keyword (triggers usually precede the
    effect), which avoids a distant ``battle_start`` keyword overriding a
    nearer ``conditional`` (e.g. 奥柏丝蒂恩 战意满自拉条, where 首次战斗开始
    grants 战意 but the AG effect fires on 战意达到最大值).
    """
    best: str | None = None
    best_pos = -1
    for trigger, kws in _TRIGGER_RULES:
        for kw in kws:
            idx = clause.rfind(kw, 0, ag_pos)  # last occurrence before ag_pos
            if idx > best_pos:
                best_pos = idx
                best = trigger
    if best is not None:
        return best

    best_pos = len(clause) + 1
    for trigger, kws in _TRIGGER_RULES:
        for kw in kws:
            idx = clause.find(kw, ag_pos)
            if idx != -1 and idx < best_pos:
                best_pos = idx
                best = trigger
    return best


# ── Observability / window-feasibility (§4.1) ───────────────────────

# Pure-random prerequisites that the screenshot can never confirm (§4.1).
_UNOBSERVABLE = {"on_evade"}

# Triggers that can fire within the measurement window (battle start →
# first actor finishes) — the ✅ column of the §4 table.
_WINDOW_FEASIBLE = {
    "battle_start", "on_skill_use", "on_ally_attack", "on_ally_skill",
    "on_ally_turn_end", "on_ally_crit", "on_hit", "on_self_crit",
    "on_follow_up", "on_ally_aoe", "on_ally_nonattack",
}


# ── Category inference (Attack / Buff / Heal) ───────────────────────

_HEAL_KW = ("恢复", "治疗", "治愈", "回复")
_ATTACK_KW = ("攻击", "打击", "斩击", "射击", "刺", "造成", "消灭")


def _infer_category(skill: dict) -> str:
    des = str(skill.get("des", "")) + str(skill.get("des2", ""))
    if any(kw in des for kw in _HEAL_KW) and not any(kw in des for kw in _ATTACK_KW):
        return "Heal"
    if any(kw in des for kw in _ATTACK_KW):
        return "Attack"
    return "Buff"


# ── Clause splitting ─────────────────────────────────────────────────

_SENT_SPLIT = re.compile(r"[。；;\n]")


def _split_clauses(text: str) -> list[str]:
    return [c.strip() for c in _SENT_SPLIT.split(text) if c.strip()]


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
        # Implicit target: a bare "行动值下降/降低" pushes the attacked enemy.
        if target is None and direction == "push":
            target = "single_enemy"

        effects.append({
            "direction": direction,
            "magnitude": magnitude,
            "target": target,
            "clause": clause[:120],
            "_ag_pos": ag_pos,
        })

    return effects


# ── Per-skill extraction ─────────────────────────────────────────────

def _parse_skill(skill: dict) -> dict:
    """Parse one skill dict into ``{slot_type, category, ag_effects[]}``."""
    skill_type = str(skill.get("type", "")).strip()
    cd = str(skill.get("cd", "")).strip()
    category = _infer_category(skill)

    full_text = "\n".join(
        str(skill.get(k, ""))
        for k in ("name", "des", "des2", "burst", "des_en", "des2_en", "burst_en")
    )

    clauses = _split_clauses(full_text)
    effects: list[dict] = []

    for c in clauses:
        if not _has_ag(c):
            continue
        for eff in _extract_ag_effects(c):
            ag_pos = eff.pop("_ag_pos")
            trigger = _classify_trigger_near(c, ag_pos)
            needs_context = False
            if trigger is None and skill_type == "Active":
                trigger = "on_skill_use"
            elif trigger is None:
                trigger = _classify_trigger(full_text)
            if trigger is None:
                trigger = "conditional"
                needs_context = True

            # Active skill's own "attack ends → AG" effect = on_skill_use only
            # when no reaction trigger was found in the same clause.
            observable = trigger not in _UNOBSERVABLE
            window_feasible = trigger in _WINDOW_FEASIBLE

            # battle_start AG is almost always a self-pull (e.g. 蕾蜜莉).
            if eff["target"] is None and eff["direction"] == "pull" \
                    and trigger == "battle_start":
                eff["target"] = "self"

            eff.update({
                "trigger": trigger,
                "observable": observable,
                "window_feasible": window_feasible,
                "needs_context": needs_context,
            })
            effects.append(eff)

    return {
        "name": skill.get("name") or skill.get("name_en", ""),
        "type": skill_type,
        "cd": cd,
        "category": category,
        "ag_effects": effects,
        "text": full_text,
    }


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
            p = _parse_skill(s)
            p["slot"] = i
            parsed_skills.append(p)

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

    for name, ch in index.items():
        for s in ch.get("skills", []):
            for e in s.get("ag_effects", []):
                effect_count += 1
                t = e["trigger"]
                trigger_counts[t] = trigger_counts.get(t, 0) + 1
                dir_counts[e["direction"]] = dir_counts.get(e["direction"], 0) + 1
                mag = e.get("magnitude")
                mag_kinds[mag["kind"] if mag else "none"] = \
                    mag_kinds.get(mag["kind"] if mag else "none", 0) + 1
                if e.get("needs_context"):
                    needs_context_chars.append(f"{name}/{s.get('name')} → {t}")
                if mag is None:
                    no_mag.append(f"{name}/{s.get('name')}: {e['clause']}")
                if e.get("target") is None:
                    no_target.append(f"{name}/{s.get('name')}: {e['clause']}")

    lines.append(f"characters indexed: {len(index)}")
    lines.append(f"total ag_effects extracted: {effect_count}")
    lines.append(f"direction: {dir_counts}")
    lines.append(f"magnitude kinds: {mag_kinds}")
    lines.append("trigger distribution:")
    for t in sorted(trigger_counts, key=trigger_counts.get, reverse=True):
        lines.append(f"  {t:20s} {trigger_counts[t]}")
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
        "data": index,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(envelope, f, ensure_ascii=False, indent=2)
    print(f"\nwrote {out_path}")

    for name in args.char:
        if name in index:
            print(f"\n=== {name} ===")
            print(json.dumps(index[name], ensure_ascii=False, indent=2))
        else:
            print(f"\n=== {name} NOT FOUND ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

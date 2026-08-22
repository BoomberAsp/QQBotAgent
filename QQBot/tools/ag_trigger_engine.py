"""AG Trigger Engine — L2 first-skill prediction + L3 trigger-chain resolution.

Phase B/C of ``docs/speedcheck-trigger-correction.md``.  Deterministic and
offline-testable on top of the L1 ``ag_skill_index`` (tools/ag_skill_index.py).

Two public entry points:

* :func:`predict_first_skill` (L2) — which skill the first actor casts in
  window 1 (full HP / no debuffs / no cooldowns), from the wiki AI rules
  (``S3→S2→S1``, category gates, hard-coded Exceptions).

* :func:`resolve_trigger_chain` (L3) — an **event-driven BFS** that expands
  the first actor's cast into every reactive action-gauge effect that fires
  in the measurement window.  A reaction may itself emit further events
  (follow-up / counter / extra turn), so the chain is resolved to *arbitrary
  length* — ``A→B→C→D…`` — until no reaction fires again.  Two guards bound
  it: an **once-per-turn** guard (each effect fires at most once) and a
  **max-step** guard (hard cap on total fired effects).

The two together build the ``ag_trigger_hypothesis`` structure (§5.1) via
:func:`build_hypothesis`.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from collections import deque
from typing import Any

from tools.ag_skill_index import build_index as _build_ag_index

_DEFAULT_CACHE = os.path.join(
    os.path.dirname(__file__), "..", "data", "wiki_cache", "character_details.json"
)

_SLOT_LABEL = {0: "S1", 1: "S2", 2: "S3"}


# ── L2: AI skill-release rules (wiki Battle_Mechanics → AI Interaction) ─

# Characters whose skill order priority is S2 → S3 → S1 (instead of the
# default S3 → S2 → S1).  English wiki titles mapped to ``name_cn``.
_AI_ORDER_S2S3S1: set[str] = {
    "茱莉亚",          # Julia
    "迪尔德丽",        # Dildri
    "玛夏",            # Marsha
    "希诺莉",          # Hinorie
    "卢妮艾塔",        # Runiata
    "莱莎曼德",        # Laiisma
    "亚毕丝",          # Abyss
    "巴尔托丝",        # Bartoz
    "赫罗薇克",        # Heroic
    "妮诺凯西",        # Nenookaasi
    "兔女郎爱莉卡",    # Bunny Girl Erica
    "可伊",            # Chloe
    "归还者璐茜娅",    # Redeemer Lucia
    "啦啦队蓓蕾卡",    # Cheerleader Berrica
    "喧闹之星哈蒂",    # Chaotic Star Heidi
    "望月",            # Mitsuki
    "Shizuna",         # Shizuna (name_cn not localised)
}

# Hard-coded S2/S3 *trigger conditions* that are NOT met in window 1
# (full HP / no debuffs / no cooldowns / no Focus).  (name, slot) → reason.
# slot is 0-indexed (S1=0, S2=1, S3=2).
_AI_TRIGGER_GATE: dict[tuple[str, int], str] = {
    ("艾瑞儿", 2): "自身HP<50%才触发",              # Ariel S3
    ("多恩", 2): "我方成员带减益才触发",            # Dawn S3
    ("普利蔓", 1): "我方成员技能冷却中才触发",      # Preema S2
    ("复制体卡洛琳", 2): "敌人HP<30%才触发",        # Clone Carolyn S3
    ("瓦妮莎", 2): "我方成员技能冷却中才触发",      # Vanessa S3
    ("尤菲米亚", 2): "我方成员HP<80%才触发",        # Euphemia S3
    ("茵罗洛", 2): "我方成员带减益才触发",          # Ingloroe S3
    ("辛狄", 1): "我方成员带减益才触发",            # Cindy S2
    ("洁莉摩尔", 2): "目标带减益才触发",            # Gerrimore S3
    ("妮诺凯西", 2): "自身集中力达5才触发",         # Nenookaasi S3
    ("彩伽", 2): "我方成员带减益或HP<80%才触发",    # Saika S3
    ("律法使者维里特", 2): "自身HP<50%才触发",      # Lawbringer Veritte S3
    ("希诺莉", 1): "自身HP<70%才触发",              # Hinorie S2
}

# Characters whose Heal-category S3 fires unconditionally (no HP gate).
_AI_S3_NO_CONDITION: set[str] = {
    "茉伊拉",  # Moira: "S3 Trigger Condition: No conditions."
}


def _skill_by_slot(skills: list[dict], slot: int) -> dict | None:
    for s in skills:
        if s.get("slot") == slot:
            return s
    return None


def _skip_reason(name: str, slot: int, sk: dict) -> str | None:
    """Why *sk* will not be cast in window 1, or None if it can be cast."""
    if str(sk.get("type", "")).strip() != "Active":
        return "被动技能，回合内不主动释放"
    gate = _AI_TRIGGER_GATE.get((name, slot))
    if gate:
        return f"第1回合条件未满足（{gate}）"
    if sk.get("category") == "Heal":
        if name in _AI_S3_NO_CONDITION and slot == 2:
            return None  # Moira S3 Heal fires regardless of HP
        return "治疗技能，第1回合满血无目标（需队友<80%HP）"
    return None


def predict_first_skill(name: str, index: dict) -> dict:
    """L2 — predict the first actor's cast skill.

    Returns ``{char, predicted_skill, skill_name, category, skipped[]}``.
    ``predicted_skill`` is ``"S1"|"S2"|"S3"`` or None when the character is
    unknown / has no castable skill.
    """
    ch = index.get(name)
    if not ch:
        return {"char": name, "predicted_skill": None,
                "reason": "角色不在行动值技能索引中"}

    skills = ch.get("skills", [])
    order = [1, 2, 0] if name in _AI_ORDER_S2S3S1 else [2, 1, 0]
    skipped: list[dict] = []

    for slot in order:
        sk = _skill_by_slot(skills, slot)
        if sk is None:
            continue
        reason = _skip_reason(name, slot, sk)
        if reason:
            skipped.append({
                "skill": _SLOT_LABEL[slot],
                "name": sk.get("name", ""),
                "category": sk.get("category"),
                "reason": reason,
            })
            continue
        return {
            "char": name,
            "predicted_skill": _SLOT_LABEL[slot],
            "skill_slot": slot,
            "skill_name": sk.get("name", ""),
            "category": sk.get("category"),
            "order": "S2→S3→S1" if name in _AI_ORDER_S2S3S1 else "S3→S2→S1",
            "skipped": skipped,
        }

    # No castable skill (should not happen — S1 is Attack by default).
    return {
        "char": name,
        "predicted_skill": None,
        "reason": "无可在第1回合释放的技能",
        "skipped": skipped,
    }


# ── L3: trigger-chain resolution ─────────────────────────────────────

# Triggers that can never fire within the measurement window (enemy has not
# acted, no death, no evade).  Matches the ❌ column of §4.
_NON_WINDOW = {"on_enemy_skill", "on_enemy_turn_end", "on_kill", "on_death",
               "on_evade"}

# Triggers that are probabilistic — never auto-fired, but surfaced in the
# ``uncertain`` bucket for the user to confirm.
_PROBABILISTIC = {"on_self_crit", "on_ally_crit"}

# The skill *produces* an extra action (chain-extending).  Deliberately
# excludes reacting to one (``…时``), buffing a rate (``…率``), or negating
# (``不会…``) — e.g. 翠西「少女的兴趣」“追击率提升…发动追击时…” must NOT
# count as producing a follow-up.
_EXTRA_ACTION_RE = re.compile(
    r"产生追加回合|追加回合|追加攻击|进行反击|反击敌人"
    r"|发动追击(?!时)|使.{0,6}发动追击"
)
_EXTRA_ACTION_NEGATE_RE = re.compile(
    r"不会发动追击|不会被反击|无法反击|不可反击|不会触发追击|不发动追击"
)
# The extra action is itself an attack (counter / follow-up / extra attack),
# as opposed to a bare extra turn (追加回合) whose re-act is unknowable.
_EXTRA_ATTACK_RE = re.compile(
    r"追加攻击|进行反击|反击敌人|发动追击(?!时)|使.{0,6}发动追击"
)
_AOE_RE = re.compile(r"全体敌人|所有敌人|全体敌方|所有敌方|全体目标|所有目标")


def _skill_text(sk: dict) -> str:
    if sk.get("text"):
        return str(sk["text"])
    return " ".join(str(sk.get(k, "")) for k in ("name", "des", "des2", "burst"))


def _is_aoe(sk: dict) -> bool:
    return bool(_AOE_RE.search(_skill_text(sk)))


def _infer_output_events(sk: dict, char: str, side: str) -> list[dict]:
    """Infer the events a fired skill emits beyond its AG manipulation.

    A pure AG reactive passive (e.g. 珍「豪爽性格」) emits nothing — its only
    effect is the gauge change.  A skill that also follows-up / counters /
    gains an extra turn emits a ``follow_up`` (and, when that extra action is
    itself an attack, an ``attack`` + ``hit``) event, which continues the
    chain to arbitrary length.
    """
    text = _skill_text(sk)
    if _EXTRA_ACTION_NEGATE_RE.search(text):
        return []
    if not _EXTRA_ACTION_RE.search(text):
        return []
    aoe = bool(_AOE_RE.search(text))
    events = [{"kind": "follow_up", "actor": char, "actor_side": side}]
    if _EXTRA_ATTACK_RE.search(text):
        events.append({"kind": "attack", "actor": char, "actor_side": side,
                       "aoe": aoe})
        events.append({"kind": "hit", "victim_side": _opposite(side), "aoe": aoe})
    return events


def _trigger_matches(trigger: str, ev: dict, char: str, side: str) -> bool:
    """Does reactive trigger *trigger* fire for *char* on event *ev*?"""
    kind = ev.get("kind")
    actor = ev.get("actor")
    actor_side = ev.get("actor_side")
    if trigger == "on_ally_attack":
        return kind == "attack" and actor_side == side and actor != char
    if trigger == "on_ally_aoe":
        return (kind == "attack" and ev.get("aoe") and actor_side == side
                and actor != char)
    if trigger == "on_ally_nonattack":
        return (kind == "skill_cast" and not ev.get("is_attack")
                and actor_side == side and actor != char)
    if trigger == "on_ally_skill":
        return kind == "skill_cast" and actor_side == side and actor != char
    if trigger == "on_ally_turn_end":
        return kind == "turn_end" and actor_side == side and actor != char
    if trigger == "on_ally_crit":
        return kind == "crit" and actor_side == side and actor != char
    if trigger == "on_self_crit":
        return kind == "crit" and actor == char
    if trigger == "on_hit":
        return kind == "hit" and ev.get("victim_side") == side
    if trigger == "on_follow_up":
        return kind == "follow_up" and actor_side == side
    return False  # on_skill_use / battle_start / conditional / non-window


def _opposite(side: str) -> str:
    return "enemy" if side == "ally" else "ally"


def _seed_events(first_actor: str, side: str, sk: dict) -> list[dict]:
    """The deterministic events the first actor's cast produces."""
    events: list[dict] = []
    is_attack = (sk or {}).get("category") == "Attack"
    aoe = _is_aoe(sk or {})
    events.append({"kind": "skill_cast", "actor": first_actor, "actor_side": side,
                   "is_attack": is_attack, "aoe": aoe})
    if is_attack:
        events.append({"kind": "attack", "actor": first_actor, "actor_side": side,
                       "aoe": aoe})
        events.append({"kind": "hit", "victim_side": _opposite(side), "aoe": aoe})
    events.append({"kind": "turn_end", "actor": first_actor, "actor_side": side})
    # The first actor's own skill may also follow-up / gain an extra turn.
    events.extend(_infer_output_events(sk or {}, first_actor, side))
    return events


def _mag_str(mag: dict | None) -> str:
    if not mag:
        return "?"
    kind = mag.get("kind")
    if kind == "range":
        return f"{mag['min']:g}%~{mag['max']:g}%"
    if kind == "per_target":
        return f"每目标{mag['per']:g}%"
    if kind == "per_count":
        return f"每计数{mag['per']:g}%"
    return f"{mag['value']:g}%"


def resolve_trigger_chain(
    team: list[dict],
    first_actor: str,
    first_skill_slot: int,
    index: dict,
) -> dict:
    """L3 — resolve the window-1 trigger chain to arbitrary length.

    Args:
        team: list of ``{"name", "side"}`` (side ∈ "ally"/"enemy").
        first_actor: name of the character who acts first.
        first_skill_slot: 0/1/2 for the first actor's cast skill.
        index: the L1 ``ag_skill_index`` (name → skills).

    Returns a dict with ``chain`` (deterministically fired effects, ordered),
    ``uncertain`` (probabilistic / conditional / unobservable), ``battle_start``
    (already applied at start), and ``terminated``.
    """
    side_of = {c["name"]: c["side"] for c in team}

    first_sk = None
    ch0 = index.get(first_actor)
    if ch0:
        first_sk = _skill_by_slot(ch0.get("skills", []), first_skill_slot)

    # ── Seed: first actor's own on_skill_use effect(s) ──
    chain: list[dict] = []
    fired_effects: set[tuple] = set()
    if first_sk:
        for ei, eff in enumerate(first_sk.get("ag_effects", [])):
            if eff.get("trigger") == "on_skill_use":
                fired_effects.add((first_actor, first_skill_slot, ei))
                chain.append({
                    "step": 0,
                    "char": first_actor,
                    "skill": _SLOT_LABEL[first_skill_slot],
                    "skill_name": first_sk.get("name", ""),
                    "trigger": "on_skill_use",
                    "direction": eff.get("direction"),
                    "magnitude": _mag_str(eff.get("magnitude")),
                    "target": eff.get("target"),
                    "observable": eff.get("observable", True),
                    "note": "首动技能自身的行动值效果",
                })

    queue: deque = deque(_seed_events(first_actor, side_of.get(first_actor, "ally"),
                                      first_sk))
    emitted_skills: set[tuple] = set()

    max_steps = 64
    while queue and len(chain) < max_steps:
        ev = queue.popleft()
        for c in team:
            name = c["name"]
            side = c["side"]
            ch = index.get(name)
            if not ch:
                continue
            for sk in ch.get("skills", []):
                slot = sk.get("slot")
                key_skill = (name, slot)
                for ei, eff in enumerate(sk.get("ag_effects", [])):
                    trigger = eff.get("trigger")
                    if trigger in _NON_WINDOW or trigger in ("on_skill_use",
                                                             "battle_start",
                                                             "conditional"):
                        continue
                    key = (name, slot, ei)
                    if key in fired_effects:
                        continue
                    if trigger in _PROBABILISTIC:
                        continue  # handled in the uncertain bucket below
                    if not _trigger_matches(trigger, ev, name, side):
                        continue
                    fired_effects.add(key)
                    note = f"响应 {ev['kind']}"
                    if trigger == "on_hit" and not ev.get("aoe", False):
                        note += "（单体攻击，实际被击中的敌人才触发）"
                    chain.append({
                        "step": len(chain),
                        "char": name,
                        "skill": _SLOT_LABEL[slot],
                        "skill_name": sk.get("name", ""),
                        "trigger": trigger,
                        "direction": eff.get("direction"),
                        "magnitude": _mag_str(eff.get("magnitude")),
                        "target": eff.get("target"),
                        "observable": eff.get("observable", True),
                        "note": note,
                    })
                    if key_skill not in emitted_skills:
                        emitted_skills.add(key_skill)
                        for out_ev in _infer_output_events(sk, name, side):
                            queue.append(out_ev)

    terminated = "max_depth" if len(chain) >= max_steps else "queue_empty"

    # ── Uncertain / conditional / battle_start buckets ──
    uncertain: list[dict] = []
    battle_start: list[dict] = []
    for c in team:
        name = c["name"]
        ch = index.get(name)
        if not ch:
            continue
        for sk in ch.get("skills", []):
            for ei, eff in enumerate(sk.get("ag_effects", [])):
                trigger = eff.get("trigger")
                base = {
                    "char": name,
                    "skill": _SLOT_LABEL[sk.get("slot")],
                    "skill_name": sk.get("name", ""),
                    "trigger": trigger,
                    "direction": eff.get("direction"),
                    "magnitude": _mag_str(eff.get("magnitude")),
                    "target": eff.get("target"),
                    "observable": eff.get("observable", True),
                }
                if trigger == "battle_start":
                    base["note"] = "战斗开始即生效，已打入初始行动值"
                    battle_start.append(base)
                elif trigger == "conditional" or eff.get("needs_context"):
                    base["note"] = "条件被动/嵌套子技能，需人工确认（L4）"
                    uncertain.append(base)
                elif trigger in _PROBABILISTIC:
                    base["note"] = "暴击触发，概率性，需人工确认"
                    uncertain.append(base)
                elif trigger in _NON_WINDOW:
                    base["note"] = "窗口内不会发生（敌方未行动/无死亡/无闪避）"
                    uncertain.append(base)

    return {
        "chain": chain,
        "uncertain": uncertain,
        "battle_start": battle_start,
        "terminated": terminated,
    }


# ── Combined hypothesis builder ──────────────────────────────────────

def build_hypothesis(
    team: list[dict],
    first_actor: str,
    index: dict,
    first_skill_slot: int | None = None,
) -> dict:
    """Build the §5.1 ``ag_trigger_hypothesis`` for a team + first actor.

    ``first_skill_slot`` may be passed explicitly (e.g. observed from the
    screenshot's cooldown state); when None it is predicted via L2.
    """
    pred = predict_first_skill(first_actor, index)
    slot = first_skill_slot if first_skill_slot is not None else pred.get("skill_slot")
    observed = first_skill_slot is not None
    if slot is None:
        return {
            "first_actor": first_actor,
            "first_actor_skill": None,
            "first_skill_observed": False,
            "prediction": pred,
            "chain": [],
            "uncertain": [],
            "battle_start": [],
            "bond_reminder": True,
            "confidence": "low",
            "note": "无法判定首动技能",
        }

    # Skill name for the *actual* cast slot (which may be an explicit
    # override of the L2 prediction).
    actual_sk = _skill_by_slot((index.get(first_actor) or {}).get("skills", []), slot)
    skill_name = (actual_sk or {}).get("name", pred.get("skill_name", ""))

    chain = resolve_trigger_chain(team, first_actor, slot, index)
    has_chain = bool(chain["chain"]) or bool(chain["battle_start"])
    return {
        "first_actor": first_actor,
        "first_actor_skill": f"{_SLOT_LABEL[slot]} {skill_name}".strip(),
        "first_skill_observed": observed,
        "prediction": pred,
        "chain": chain["chain"],
        "uncertain": chain["uncertain"],
        "battle_start": chain["battle_start"],
        "bond_reminder": True,
        "confidence": "medium" if has_chain else "low",
    }


# ── CLI ──────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="AG trigger engine (L2+L3) offline test.")
    ap.add_argument("--cache", default=None, help="character_details.json path")
    ap.add_argument("--char", action="append", default=[],
                    help="first-actor name (repeatable: one team per --char)")
    ap.add_argument("--team", default=None,
                    help="comma-separated 'name:side' list; defaults to a "
                         "synthetic 6-character team")
    args = ap.parse_args()

    index = _build_ag_index(args.cache)
    if not index:
        print("no index (missing character_details.json)")
        return 1

    names = args.char or ["安熙恩"]
    for name in names:
        if args.team:
            team = [{"name": p.split(":")[0],
                     "side": p.split(":")[1] if ":" in p else "ally"}
                    for p in args.team.split(",")]
        else:
            team = [{"name": name, "side": "ally"},
                    {"name": "珍", "side": "ally"},
                    {"name": "莎莉丝特", "side": "ally"},
                    {"name": "望月", "side": "enemy"},
                    {"name": "露娜", "side": "enemy"},
                    {"name": "辛狄", "side": "enemy"}]
        hyp = build_hypothesis(team, name, index)
        print(f"\n=== {name} ===")
        print(json.dumps(hyp, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

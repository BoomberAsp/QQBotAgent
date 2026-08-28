"""AG Trigger Engine — L2 first-skill prediction + L3 trigger-chain resolution.

Deterministic and offline-testable on top of the L1 ``ag_skill_index``
(:mod:`tools.ag_skill_index`, :class:`Skill` objects).

Two public entry points:

* :func:`predict_first_skill` (L2) — which skill the first actor casts in
  window 1 (full HP / no debuffs / no cooldowns), from the wiki AI rules
  (``S3→S2→S1``, category gates, hard-coded Exceptions).

* :func:`resolve_trigger_chain` (L3) — an **event-driven BFS** that expands
  the first actor's cast into every reactive action-gauge effect that fires
  in the measurement window, then **reconciles the prediction against the
  cooldown evidence** observed on the screenshots (``observed`` argument):

  * a seen skill (slot S2/S3, has cooldown) that turned freshly dark was
    cast → ``confirmed=True, evidence="observed_cooldown"`` (a fact, never
    a question);
  * a predicted-but-not-dark skill did NOT fire → moved to
    ``not_triggered`` (silently dropped, never a question);
  * only genuinely unobservable items survive into ``uncertain``:
    probabilistic crits, condition gates whose base event happened, and
    the counter-gear roll (反击套装, 30%).
"""

from __future__ import annotations

import argparse
import json
import re
from collections import deque

from tools.ag_skill_index import Skill, build_index as _build_ag_index

_SLOT_LABEL = {0: "S1", 1: "S2", 2: "S3"}
_LABEL_SLOT = {v: k for k, v in _SLOT_LABEL.items()}


# ── L2: AI skill-release rules (wiki Battle_Mechanics → AI Interaction) ─

# Characters whose skill order priority is S2 → S3 → S1 (instead of the
# default S3 → S2 → S1).  Chinese names.
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


def _as_skill(sk) -> Skill:
    """Normalise an index entry to a :class:`Skill` (dicts from JSON caches
    are transparently rehydrated)."""
    return sk if isinstance(sk, Skill) else Skill.from_dict(sk)


def _skill_by_slot(skills: list, slot: int) -> Skill | None:
    for s in skills:
        s = _as_skill(s)
        if s.slot == slot:
            return s
    return None


def _skip_reason(name: str, slot: int, sk: Skill) -> str | None:
    """Why *sk* will not be cast in window 1, or None if it can be cast."""
    if sk.type.strip() != "Active":
        return "被动技能，回合内不主动释放"
    gate = _AI_TRIGGER_GATE.get((name, slot))
    if gate:
        return f"第1回合条件未满足（{gate}）"
    if sk.category == "Heal":
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
                "name": sk.name,
                "category": sk.category,
                "reason": reason,
            })
            continue
        return {
            "char": name,
            "predicted_skill": _SLOT_LABEL[slot],
            "skill_slot": slot,
            "skill_name": sk.name,
            "category": sk.category,
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

# Triggers that can never fire within the measurement window (no turn ends
# for the non-acting side, no death, no evade).  NOTE: enemy NON-attack
# skills / extra turns / AoE CAN happen — the first actor may be an enemy,
# and allies' casts are enemy-side events from the foes' perspective.
_NON_WINDOW = {"on_enemy_turn_end", "on_kill", "on_death", "on_evade"}

# Triggers that are probabilistic — never auto-fired, but surfaced in the
# ``uncertain`` bucket for the user to confirm.
_PROBABILISTIC = {"on_self_crit", "on_ally_crit"}

# Resource gates (战意/集中力满) are invisible on the screenshot → never
# auto-fired; evidence reconciliation catches them when they have a cooldown.
_RESOURCE_GATE = {"on_morale_full", "on_focus_full"}

# Negated follow-up production (盖儿 S1 「此攻击不会发动追击」) — the L1
# follow-up axis is negation-blind, so re-guard here before emitting events.
_EXTRA_ACTION_NEGATE_RE = re.compile(
    r"不会发动追击|不会被反击|无法反击|不可反击|不会触发追击|不发动追击"
)

# Counter-gear (反击套装): wearer counters with S1 at this probability when
# hit by an enemy ATTACK skill.  Gear loadout is invisible → always ask.
_COUNTER_GEAR_PROBABILITY = 0.30


def _opposite(side: str) -> str:
    return "enemy" if side == "ally" else "ally"


def _infer_output_events(sk: Skill | None, char: str, side: str) -> list[dict]:
    """Events a fired skill emits beyond its AG manipulation.

    A pure AG reactive passive emits nothing.  A skill that grants an extra
    turn emits ``extra_turn`` (reacted to by 敌方/我方追加回合触发) plus a
    ``follow_up``; a skill that is itself a follow-up/counter attack also
    emits ``attack`` + ``hit`` so counter/被攻击 reactions chain onward.
    """
    if sk is None:
        return []
    events: list[dict] = []
    negated = bool(_EXTRA_ACTION_NEGATE_RE.search(sk.text))
    if sk.generates_extra_turn:
        events.append({"kind": "extra_turn", "actor": char, "actor_side": side})
        events.append({"kind": "follow_up", "actor": char, "actor_side": side})
    if sk.is_follow_up_skill and not negated:
        events.append({"kind": "follow_up", "actor": char, "actor_side": side})
        if sk.has_attack:
            aoe = sk.attack_scope == "aoe"
            events.append({"kind": "attack", "actor": char,
                           "actor_side": side, "aoe": aoe})
            events.append({"kind": "hit", "victim_side": _opposite(side),
                           "aoe": aoe})
    # de-dup identical events (both branches can emit follow_up)
    seen: set[tuple] = set()
    out: list[dict] = []
    for ev in events:
        key = (ev["kind"], ev.get("actor_side"), ev.get("victim_side"),
               ev.get("aoe"))
        if key not in seen:
            seen.add(key)
            out.append(ev)
    return out


def _trigger_matches(trigger: str, ev: dict, char: str, side: str) -> bool:
    """Does reactive trigger *trigger* fire for *char* on event *ev*?

    *side* is the REACTOR's side; 「敌方」 events come from the opposite
    side, 「我方」 from the reactor's own side.
    """
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
    if trigger == "on_ally_extra_turn":
        return kind == "extra_turn" and actor_side == side and actor != char
    if trigger == "on_enemy_extra_turn":
        return kind == "extra_turn" and actor_side != side
    if trigger == "on_enemy_nonattack_skill":
        return (kind == "skill_cast" and not ev.get("is_attack")
                and actor_side != side)
    if trigger == "on_enemy_aoe":
        return kind == "attack" and ev.get("aoe") and actor_side != side
    if trigger == "on_ally_turn_end":
        return kind == "turn_end" and actor_side == side and actor != char
    if trigger == "on_ally_crit":
        return kind == "crit" and actor_side == side and actor != char
    if trigger == "on_self_crit":
        return kind == "crit" and actor == char
    if trigger == "on_hit":
        return kind == "hit" and ev.get("victim_side") == side
    if trigger == "on_ally_hit":
        return kind == "hit" and ev.get("victim_side") == side
    if trigger == "on_follow_up":
        return kind == "follow_up" and actor_side == side
    return False  # on_skill_use / battle_start / conditional / non-window


def _seed_events(first_actor: str, side: str, sk: Skill | None) -> list[dict]:
    """The deterministic events the first actor's cast produces."""
    events: list[dict] = []
    is_attack = bool(sk.has_attack) if sk else False
    aoe = (sk.attack_scope == "aoe") if sk else False
    events.append({"kind": "skill_cast", "actor": first_actor,
                   "actor_side": side, "is_attack": is_attack, "aoe": aoe})
    if is_attack:
        events.append({"kind": "attack", "actor": first_actor,
                       "actor_side": side, "aoe": aoe})
        events.append({"kind": "hit", "victim_side": _opposite(side),
                       "aoe": aoe})
    events.append({"kind": "turn_end", "actor": first_actor,
                   "actor_side": side})
    # The first actor's own skill may also follow-up / gain an extra turn.
    events.extend(_infer_output_events(sk, first_actor, side))
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


def _cond_str(cond: dict | None) -> str:
    if not cond:
        return ""
    kind = cond.get("kind")
    if kind == "hp":
        who = {"ally": "我方", "enemy": "敌方", "self": "自身"}.get(
            cond.get("side"), cond.get("side") or "")
        op = {"<=": "≤", ">=": "≥"}.get(cond.get("op"), cond.get("op"))
        return f"{who}生命力{op}{cond.get('pct'):g}%"
    if kind == "state":
        return f"处于「{cond.get('name', '?')}」状态"
    return str(cond)


def resolve_trigger_chain(
    team: list[dict],
    first_actor: str,
    first_skill_slot: int,
    index: dict,
    observed: dict | None = None,
) -> dict:
    """L3 — resolve the window-1 trigger chain, then reconcile with evidence.

    Args:
        team: list of ``{"name", "side"}`` (side ∈ "ally"/"enemy").
        first_actor: name of the character who acts first.
        first_skill_slot: 0/1/2 for the first actor's cast skill.
        index: the L1 ``ag_skill_index`` (name → {"skills": [Skill, …]}).
        observed: optional cooldown evidence from
            :func:`battle_parser._observe_all_cooldowns` —
            ``{"fresh": {(name, side): [slot]}, "seen": {(name, side): True}}``.
            When given, the cooldown state is AUTHORITATIVE: fresh-dark skills
            were cast (confirmed), seen-but-colourful skills were not
            (silently moved to ``not_triggered``).

    Returns ``{chain, not_triggered, uncertain, battle_start, terminated}``.
    """
    side_of: dict[str, str] = {}
    for c in team:  # first occurrence wins (mirror-row safety)
        side_of.setdefault(c["name"], c["side"])

    first_sk = None
    ch0 = index.get(first_actor)
    if ch0:
        first_sk = _skill_by_slot(ch0.get("skills", []), first_skill_slot)

    # ── Seed: first actor's own on_skill_use effect(s) ──
    chain: list[dict] = []
    fired_effects: set[tuple] = set()
    if first_sk:
        for ei, eff in enumerate(first_sk.ag_effects):
            if eff.get("trigger") == "on_skill_use":
                fired_effects.add((first_actor, first_skill_slot, ei))
                chain.append({
                    "step": 0,
                    "char": first_actor,
                    "side": side_of.get(first_actor, "ally"),
                    "skill": _SLOT_LABEL[first_skill_slot],
                    "skill_name": first_sk.name,
                    "trigger": "on_skill_use",
                    "direction": eff.get("direction"),
                    "magnitude": _mag_str(eff.get("magnitude")),
                    "target": eff.get("target"),
                    "observable": eff.get("observable", True),
                    "note": "首动技能自身的行动值效果",
                })

    queue: deque = deque(_seed_events(
        first_actor, side_of.get(first_actor, "ally"), first_sk))
    event_log: list[dict] = []
    emitted_skills: set[tuple] = set()

    max_steps = 64
    while queue and len(chain) < max_steps:
        ev = queue.popleft()
        event_log.append(ev)
        for c in team:
            name = c["name"]
            side = c["side"]
            ch = index.get(name)
            if not ch:
                continue
            for sk in (_as_skill(s) for s in ch.get("skills", [])):
                slot = sk.slot
                key_skill = (name, side, slot)
                for ei, eff in enumerate(sk.ag_effects):
                    trigger = eff.get("trigger")
                    if trigger in _NON_WINDOW or trigger in _RESOURCE_GATE \
                            or trigger in ("on_skill_use", "battle_start",
                                           "conditional"):
                        continue
                    if eff.get("condition"):
                        continue  # gated → uncertain bucket if event matched
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
                        "side": side,
                        "skill": _SLOT_LABEL[slot],
                        "skill_name": sk.name,
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

    # ── Evidence reconciliation (authoritative over the BFS prediction) ──
    not_triggered: list[dict] = []
    reconciled: set[tuple] = set()   # (name, side, slot) settled by evidence
    if observed:
        chain = _reconcile(chain, not_triggered, reconciled, observed,
                           index, team, first_actor,
                           side_of.get(first_actor, "ally"),
                           first_skill_slot)

    # ── Uncertain / battle_start buckets ──
    uncertain: list[dict] = []
    battle_start: list[dict] = []
    for c in team:
        name = c["name"]
        side = c["side"]
        ch = index.get(name)
        if not ch:
            continue
        for sk in (_as_skill(s) for s in ch.get("skills", [])):
            for eff in sk.ag_effects:
                trigger = eff.get("trigger")
                base = {
                    "char": name,
                    "side": side,
                    "skill": _SLOT_LABEL[sk.slot],
                    "skill_name": sk.name,
                    "trigger": trigger,
                    "direction": eff.get("direction"),
                    "magnitude": _mag_str(eff.get("magnitude")),
                    "target": eff.get("target"),
                    "observable": eff.get("observable", False),
                }
                if trigger == "battle_start":
                    base["note"] = "战斗开始即生效，已打入初始行动值"
                    battle_start.append(base)
                elif trigger in _PROBABILISTIC:
                    base["note"] = "暴击触发，概率性，需人工确认"
                    uncertain.append(base)
                elif eff.get("condition") and trigger not in _NON_WINDOW:
                    # gated effect whose base event DID happen in the window
                    # — the gate itself is invisible; ask.  Evidence-settled
                    # skills (cooldown observed) already have their verdict.
                    if (name, side, sk.slot) in reconciled:
                        continue
                    if any(_trigger_matches(trigger, ev, name, side)
                           for ev in event_log):
                        base["note"] = (
                            f"触发事件已发生，但条件门无法从截图确认："
                            f"{_cond_str(eff.get('condition'))}（需确认）")
                        uncertain.append(base)

    uncertain.extend(_check_counter_gear(team, index, event_log))

    return {
        "chain": chain,
        "not_triggered": not_triggered,
        "uncertain": uncertain,
        "battle_start": battle_start,
        "terminated": terminated,
    }


def _reconcile(
    chain: list[dict],
    not_triggered: list[dict],
    reconciled: set[tuple],
    observed: dict,
    index: dict,
    team: list[dict],
    first_actor: str,
    first_side: str,
    first_slot: int,
) -> list[dict]:
    """Apply cooldown evidence to the BFS prediction (returns new chain).

    * seen row, slot freshly dark → the skill WAS cast: chain entries for it
      become ``confirmed=True, evidence="observed_cooldown"``; when the BFS
      never predicted it, a confirmed entry is added anyway.
    * seen row, slot colourful → the skill was NOT cast: predicted entries
      are demoted into *not_triggered*; candidate effects the BFS never
      fired are recorded there too (silent — never asked).
    * unseen rows keep their prediction untouched (no evidence either way).
    """
    fresh: dict = observed.get("fresh", {})
    seen: set = set(observed.get("seen", {}))

    def _entry(name: str, side: str, sk: Skill, slot: int, eff: dict | None,
               confirmed: bool, note: str) -> dict:
        return {
            "step": len(chain),
            "char": name,
            "side": side,
            "skill": _SLOT_LABEL[slot],
            "skill_name": sk.name,
            "trigger": eff.get("trigger") if eff else "observed_cooldown",
            "direction": eff.get("direction") if eff else None,
            "magnitude": _mag_str(eff.get("magnitude")) if eff else "",
            "target": eff.get("target") if eff else None,
            "observable": True,
            "confirmed": confirmed,
            "evidence": "observed_cooldown",
            "note": note,
        }

    # index chain entries by (char, side, slot)
    chain_by: dict[tuple, list[dict]] = {}
    for entry in chain:
        key = (entry["char"], entry.get("side"),
               _LABEL_SLOT.get(entry["skill"]))
        chain_by.setdefault(key, []).append(entry)

    dropped: set[int] = set()
    for c in team:
        name, side = c["name"], c["side"]
        key = (name, side)
        if key not in seen:
            continue
        ch = index.get(name)
        if not ch:
            continue
        for slot in (1, 2):  # S1 has no cooldown → never observable
            sk = _skill_by_slot(ch.get("skills", []), slot)
            if sk is None or not sk.cd:
                continue
            reconciled.add((name, side, slot))
            entries = chain_by.get((name, side, slot), [])
            entry_ids = {id(e) for e in entries}
            if slot in fresh.get(key, []):
                # freshly dark → cast (fact)
                if entries:
                    for e in entries:
                        e["confirmed"] = True
                        e["evidence"] = "observed_cooldown"
                        e["note"] += "；跑条后截图该技能新冷却（已发动）"
                else:
                    is_own_cast = (name == first_actor and side == first_side
                                   and slot == first_slot)
                    if sk.ag_effects:
                        for eff in sk.ag_effects:
                            chain.append(_entry(
                                name, side, sk, slot, eff, True,
                                "跑条后截图该技能新冷却，本窗口已发动"))
                    elif not is_own_cast:
                        # fired but no AG effect extracted — still a fact
                        chain.append(_entry(
                            name, side, sk, slot, None, True,
                            "跑条后截图该技能新冷却，本窗口已发动"))
            else:
                # colourful → not cast
                for e in entries:
                    e["confirmed"] = False
                    e["note"] += "；跑条后截图未见新冷却（未发动）"
                    not_triggered.append(e)
                    dropped.add(id(e))
                if not entries and sk.ag_effects:
                    for eff in sk.ag_effects:
                        not_triggered.append(_entry(
                            name, side, sk, slot, eff, False,
                            "冷却态未见触发（本窗口未发动）"))
    return [e for e in chain if id(e) not in dropped]


def _check_counter_gear(team: list[dict], index: dict,
                        event_log: list[dict]) -> list[dict]:
    """Counter-gear (反击套装) candidates — the one genuinely unobservable
    item: when an enemy ATTACK skill lands, the wearer counters with S1 at
    30% probability; gear loadout is invisible from the screenshot.

    Triple gate: (1) an attack event happened in the window, (2) character
    is on the attacked side, (3) their S1 has an AG effect worth tracking.
    """
    attack_sides = {ev.get("actor_side") for ev in event_log
                    if ev.get("kind") == "attack"}
    out: list[dict] = []
    if not attack_sides:
        return out
    for c in team:
        name, side = c["name"], c["side"]
        if _opposite(side) not in attack_sides:
            continue
        ch = index.get(name)
        if not ch:
            continue
        sk = _skill_by_slot(ch.get("skills", []), 0)
        if sk is None or not sk.ag_effects:
            continue
        out.append({
            "char": name,
            "side": side,
            "skill": "S1",
            "skill_name": sk.name,
            "trigger": "counter_gear",
            "observable": False,
            "note": (f"反击套装：敌方攻击技能指向该角色时"
                     f"{_COUNTER_GEAR_PROBABILITY:.0%}概率以S1反击"
                     f"（装备不可见，需确认）"),
        })
    return out


# ── Combined hypothesis builder ──────────────────────────────────────

def build_hypothesis(
    team: list[dict],
    first_actor: str,
    index: dict,
    first_skill_slot: int | None = None,
    observed: dict | None = None,
) -> dict:
    """Build the §5.1 ``ag_trigger_hypothesis`` for a team + first actor.

    ``first_skill_slot`` may be passed explicitly (e.g. observed from the
    screenshot's cooldown state); when None it is predicted via L2.
    ``observed`` (per-row cooldown evidence) makes the verdicts factual:
    ``chain`` entries carry ``confirmed``/``evidence``, silent drops land in
    ``not_triggered``.
    """
    pred = predict_first_skill(first_actor, index)
    slot = first_skill_slot if first_skill_slot is not None else pred.get("skill_slot")
    was_observed = first_skill_slot is not None
    if slot is None:
        return {
            "first_actor": first_actor,
            "first_actor_skill": None,
            "first_skill_observed": False,
            "prediction": pred,
            "chain": [],
            "not_triggered": [],
            "uncertain": [],
            "battle_start": [],
            "bond_reminder": True,
            "confidence": "low",
            "note": "无法判定首动技能",
        }

    # Skill name for the *actual* cast slot (which may be an explicit
    # override of the L2 prediction).
    actual_sk = _skill_by_slot((index.get(first_actor) or {}).get("skills", []), slot)
    skill_name = actual_sk.name if actual_sk else pred.get("skill_name", "")

    chain = resolve_trigger_chain(team, first_actor, slot, index,
                                  observed=observed)
    has_chain = bool(chain["chain"]) or bool(chain["battle_start"])
    return {
        "first_actor": first_actor,
        "first_actor_skill": f"{_SLOT_LABEL[slot]} {skill_name}".strip(),
        "first_skill_observed": was_observed,
        "prediction": pred,
        "chain": chain["chain"],
        "not_triggered": chain["not_triggered"],
        "uncertain": chain["uncertain"],
        "battle_start": chain["battle_start"],
        "observed_evidence": bool(observed),
        "bond_reminder": True,
        "confidence": "high" if observed else ("medium" if has_chain else "low"),
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

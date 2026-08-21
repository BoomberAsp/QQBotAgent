"""
Status icon mapping — wiki buff/debuff icon filenames → Chinese labels.

Shared by ``tools/wiki_scraper.py`` (which downloads the icons from the wiki
into ``data/wiki_cache/status_icons/``) and ``lib/ocr_engine.py`` (whose
``BuffDetector`` loads those icons as a second template source, keyed by the
Chinese label so the downstream ``_validate_pre_screenshot`` keyword checks in
``tools/battle_parser.py`` keep working unchanged).

The mapping is keyed by the *wiki file name including extension* (e.g.
``"Buff_Immune.png"``), matching the names ``wiki_scraper`` extracts from the
``[[File:...|30px]]`` markup and saves to disk.

The 28 entries under ``# Existing manual icons`` must produce Chinese labels
that exactly match the basenames of ``images/cal-speed-data/*图标.png``
(e.g. ``Buff_Immune.png → 免疫`` for ``免疫图标.png``), so a wiki icon can
transparently cover the corresponding manually-captured icon.  Everything
else is an *additional* status the wiki documents but that has no manual icon.
"""

import os

# Wiki status icons are downloaded here (regenerable; git-ignored).
STATUS_ICON_DIR = os.path.join(
    os.path.dirname(__file__), "..", "data", "wiki_cache", "status_icons"
)

# wiki file name (with .png) → Chinese label.
STATUS_ICON_CN: dict[str, str] = {
    # ── Existing manual icons (labels match cal-speed-data/*图标.png) ──
    "Buff_Immune.png": "免疫",
    "Buff_Counterattack.png": "反击",
    "Buff_Lifesteal.png": "吸血",
    "Debuff_DecreaseHitChance.png": "命中率降低",
    "Buff_SkillNullifier.png": "技能免疫",
    "Buff_Barrier.png": "护盾",
    "Buff_AddAttackBig.png": "攻击力大幅提升",
    "Buff_IncreaseAttack.png": "攻击力提升",
    "Debuff_DecreaseAttack.png": "攻击力降低",
    "Buff_Invincible.png": "无敌",
    "Debuff_CannotBuff.png": "无法强化",
    "Buff_IncreaseCriticalHitResistance.png": "暴击抵抗",
    "Buff_IncreaseCriticalHitChance.png": "暴击率提升",
    "Buff_Vigor.png": "气魄",
    "Debuff_Sleep.png": "沉睡",
    "Debuff_Silence.png": "沉默",
    "Debuff_Bleed.png": "流血",
    "Buff_Stealth.png": "潜伏",
    "Buff_Perception.png": "看破",
    "Debuff_Stun.png": "眩晕",
    "H118S2.png": "自视甚高的欺侮",
    "Buff_IncreaseSpeed.png": "速度提升",
    "Debuff_DecreaseSpeed.png": "速度降低",
    "Buff_IncreaseEvasion.png": "闪避",
    "Debuff_DecreaseDefense.png": "防御力下降",
    "Buff_IncreaseDefense.png": "防御力提升",
    "Immortal_Resident.png": "不屈（无法解除）",
    "Buff_H167S2.png": "FXXK YXU",

    # ── Additional buffs (no manual icon) ──
    "Buff_IncreaseCriticalHitDamage.png": "暴击伤害提升",
    "Buff_IncreaseEffectiveness.png": "效果命中提升",
    "Buff_IncreaseEffectResistance.png": "效果抵抗提升",
    "Buff_IncreasePenetrateResistance.png": "贯穿抵抗提升",
    "Buff_IncreaseHitChance.png": "（基础）命中率提升",
    "Buff_ContinuousHeal.png": "持续恢复",
    "Buff_Immortal.png": "不屈",
    "Buff_Revive.png": "复苏",
    "Buff_Reflect.png": "反射",
    "Buff_H025S2_2.png": "深层幻影",
    "Buff_Rage.png": "激怒",
    "Buff_Loveliness.png": "可爱",
    "Buff_MindsEye.png": "洞察",
    "Buff_Idol.png": "公演模式",
    "Buff_Rage2.png": "愤怒",
    "Buff_H162S2.png": "遇强则强",
    "Buff_H172S2.png": "鬼跳南瓜",
    "Buff_H613S3.png": "扶桑晓露",
    "Buff_PinchEnhance.png": "追击强化",
    "Buff_Guard.png": "守护",
    "Buff_MaxSkillDamage.png": "受伤上限",
    "Buff_Flash.png": "瞬动",

    # ── Additional debuffs (no manual icon) ──
    "Debuff_Unhealable.png": "禁疗",
    "Debuff_Provoke.png": "嘲讽",
    "Debuff_Poison.png": "中毒",
    "Debuff_Burn.png": "灼烧",
    "Debuff_Bomb.png": "炸弹",
    "Debuff_Target.png": "锁定",
    "Debuff_Restrict.png": "拘禁",
    "Debuff_Brand.png": "妨碍",
    "Debuff_Curse.png": "诅咒",
    "Debuff_Hypertoxic.png": "猛毒",
    "Debuff_MagicNail.png": "咒印",
    "Debuff_Seal.png": "被动无效",
    "Debuff_RedirectedProvoke.png": "指定嘲讽",
    "Debuff_VamoiricTouch.png": "回生",
    "Debuff_Fascination.png": "迷乱",
    "Debuff_Frostburn.png": "冰灼",
    "H168S2_1.png": "星链标记",
}

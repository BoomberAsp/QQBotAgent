"""
Translation glossary store — single owner of the English→Chinese game-term
tables used for wiki translation.

Consumers:
  tools/wiki_scraper.py    deterministic maps (_clean_markup, element/class/…)
                           + LLM prompt glossaries (build_prompt_glossary)
  lib/buff_alias.py        battle-text term matching (status_terms)
  tools/buff_vocab_dump.py vocabulary dump
  webui panel              GET/PUT /api/wiki/glossary (edit + restore defaults)

Storage: ``QQBot/config/translation_glossary.json`` (git-tracked; edited from
the panel). Missing/corrupt file → built-in DEFAULT_GLOSSARY, so fresh clones
work without the file. Per-table fallback: a file missing one table gets that
table from the defaults. ``load_glossary()`` caches by mtime — panel edits take
effect on the next call in the bot process, no restart and no watcher needed.

Table shapes (historical, preserved exactly):
  pairs     ordered ``[[en, zh], ...]`` — order matters: status_terms applies
            regex substitution in sequence (longer phrases first; duplicate
            keys are legal — the first occurrence wins), prompt_terms are
            joined into the LLM prompt in order.
  map       ``{key: zh}``
  map_flag  ``{key: [zh, is_percentage]}``
  map_int   ``{key: int}``

The returned glossary dict is a shared cache — consumers must NOT mutate it.
"""

import copy
import json
import os
import shutil
import threading

GLOSSARY_PATH = os.path.join(
    os.path.dirname(__file__), "..", "config", "translation_glossary.json"
)

# table name → shape (also acts as the whitelist for panel saves)
TABLE_SHAPES = {
    "status_terms": "pairs",
    "prompt_terms_char": "pairs",
    "prompt_terms_bond": "pairs",
    "element": "map",
    "class": "map",
    "constellation": "map",
    "bond_sell_gold": "map",
    "bond_sell_fragment": "map",
    "bond_xp_value": "map",
    "discipline_stat": "map_flag",
    "stat_multipliers": "map_int",
}

# Chinese labels for the panel UI
TABLE_LABELS = {
    "status_terms": "状态/游戏术语（有序，正则按序替换）",
    "prompt_terms_char": "角色翻译提示词术语（有序）",
    "prompt_terms_bond": "羁绊翻译提示词术语（有序）",
    "element": "属性",
    "class": "职业",
    "constellation": "星座",
    "discipline_stat": "Discipline 属性（[中文, 是否百分比]）",
    "bond_sell_gold": "羁绊出售价（金币，按星级）",
    "bond_sell_fragment": "羁绊出售价（碎片，按星级）",
    "bond_xp_value": "羁绊经验值（按星级）",
    "stat_multipliers": "60 级属性成长系数（整数）",
}

# ── Built-in defaults (migrated verbatim from wiki_scraper.py constants and
#    the two inline LLM prompt glossary strings; generated, do not hand-edit) ──
DEFAULT_GLOSSARY = { 'status_terms': [ ['Effect Resistance', '效果抵抗'],
                        ['Effect Hit Rate', '效果命中'],
                        ['SPD Down', '速度下降'],
                        ['ATK Down', '攻击力下降'],
                        ['DEF Down', '防御力下降'],
                        ['SPD Up', '速度提升'],
                        ['ATK Up', '攻击力提升'],
                        ['DEF Up', '防御力提升'],
                        ['Provoke', '嘲讽'],
                        ['Immunity', '免疫'],
                        ['Shield', '护盾'],
                        ['Stun', '眩晕'],
                        ['Silence', '沉默'],
                        ['Poison', '中毒'],
                        ['Burn', '灼烧'],
                        ['Bleed', '流血'],
                        ['Recovery', '持续恢复'],
                        ['Speed', '速度'],
                        ['ATK', '攻击力'],
                        ['DEF', '防御力'],
                        ['HP', '生命值'],
                        ['Additional damage', '追加伤害'],
                        ['Upon hit', '技能命中时'],
                        ['ACC', '（基础）命中率'],
                        ['Lock On', '锁定'],
                        ['Foresight', '看破'],
                        ['Ignore Effect RES', '无视效果抵抗'],
                        ['Astrogen', '星源力'],
                        ['Flanking', '追加攻击'],
                        ['Extra Turn', '额外回合'],
                        ['Stealth', '潜伏'],
                        ['Penetrate', '贯穿（一定防御力）'],
                        ['Extinction', '灭绝'],
                        ['Increase the Action Gauge', '行动值提升'],
                        ['damage distribution effects', '伤害分配（分摊）效果'],
                        ['ACC Up', '（基础）命中率提升'],
                        ['Morale', '战意值'],
                        ['Injury', '创伤'],
                        ['restore HP', '回复生命值'],
                        ['Vigor', '气魄'],
                        ['Hits', '命中的攻击'],
                        ['Crit', '暴击'],
                        ['Unbuffable', '无法强化'],
                        ['Immortal', '不屈'],
                        ['Revived', '复活'],
                        ['fatal blow', '致命伤害'],
                        ['removing all buffs', '驱散所有正向状态'],
                        ['Seal/Passiveless', '被动无效'],
                        ['ACC Down', '（基础）命中率下降'],
                        ['damage taken is reduced', '伤害量下降'],
                        ['Counter', '反击'],
                        ['Evasion Up', '闪避率提升'],
                        ['reduce the Cooldown', '冷却减少'],
                        ['At the start of the battle', '进入战斗时'],
                        ['Invincible', '无敌'],
                        ['stealing (one/two/etc.) buff(s)', '窃取（一个/两个/等）正向状态'],
                        ['Blink', '瞬动'],
                        ['Bomb', '炸弹'],
                        ['lower their Action Gauge', '造成行动值降低'],
                        ['Ignite (the burn and bomb)', '激发'],
                        ['DMG RED effect', '伤害量下降效果'],
                        ['Curse', '诅咒'],
                        ['Unhealable', '禁疗'],
                        ['Resurgence', '回生'],
                        ['Unremovable', '不可解除'],
                        ['Lifesteal', '吸血'],
                        ['increase their Skill Cooldowns', '技能冷却时间延长'],
                        ['Sleep', '沉睡'],
                        ['Confusion', '迷乱'],
                        ['Focus', '集中力'],
                        ['Crit RES Up', '暴击抵抗'],
                        ['Defiant', '遇强则强'],
                        ['Hinder', '妨碍'],
                        ['Skill Nullifier', '技能免疫'],
                        ['Restrict', '拘禁'],
                        ['Frostburn', '冰灼'],
                        ['Stellar Sigil', '星链标记'],
                        ['Flanking Boost', '追击强化'],
                        ['Flanking', '追击'],
                        ['decreasing the duration of their buffs', '减少正向状态时间'],
                        ['Guard', '守护'],
                        ['fixed damage', '固定伤害'],
                        ['copy buffs', '复制正向状态'],
                        ['Random Buff', '随机正向状态'],
                        ['extending the duration of all debuffs', '负向状态延长'],
                        ['Immobilizing Debuffs', '无法行动类型负向状态'],
                        ['Pain Threshold', '受伤上限'],
                        ['Hibiscus Morning Dew', '扶桑晓露'],
                        ['Crit DMG Up', '暴击伤害提升'],
                        ['Performance Mode', '公演模式'],
                        ['Arrogant Bullying', '自视甚高的欺侮'],
                        ['Flow State', '心流状态'],
                        ['Active', '主动技'],
                        ['Passive', '被动技'],
                        ['Resuscitate', '复苏'],
                        ['ADD DMG RED', '追加伤害下降'],
                        ['Shield Conversion', '护盾转换'],
                        ['Cluster', '凝聚'],
                        ['reducing the debuffs', '负向状态时间减少'],
                        ['Jumpy Pumpkins', '鬼跳南瓜'],
                        ['FXXK YXU', 'FXXK YXU'],
                        ['Targeted Taunt', '指定嘲讽'],
                        ['Transfer', '转移'],
                        ['rebound', '反弹'],
                        ['favorable attribute', '有利属性'],
                        ['attribute counter', '不利属性'],
                        ['Member', '团员']],
      'prompt_terms_char': [ ['ATK', '攻击力'],
                             ['DEF', '防御力'],
                             ['HP', '生命值'],
                             ['Speed', '速度'],
                             ['ATK Down', '攻击力下降'],
                             ['DEF Down', '防御力下降'],
                             ['SPD Down', '速度下降'],
                             ['ATK Up', '攻击力提升'],
                             ['DEF Up', '防御力提升'],
                             ['SPD Up', '速度提升'],
                             ['Provoke', '嘲讽'],
                             ['Immunity', '免疫'],
                             ['Shield', '护盾'],
                             ['Stun', '眩晕'],
                             ['Silence', '沉默'],
                             ['Poison', '中毒'],
                             ['Burn', '灼烧'],
                             ['Bleed', '流血'],
                             ['Barrier', '屏障'],
                             ['Recovery', '恢复'],
                             ['Effect Hit Rate', '效果命中'],
                             ['Effect Resistance', '效果抵抗']],
      'prompt_terms_bond': [ ['ATK', '攻击力'],
                             ['DEF', '防御力'],
                             ['HP', '生命值'],
                             ['Shield', '护盾'],
                             ['Immune', '免疫'],
                             ['Provoke', '嘲讽'],
                             ['Stun', '眩晕'],
                             ['Bleed', '流血'],
                             ['Burn', '灼烧'],
                             ['Poison', '中毒'],
                             ['Revive', '复活'],
                             ['Barrier', '屏障'],
                             ['Recovery', '恢复']],
      'element': {'Flame': '火', 'Water': '水', 'Nature': '木', 'Light': '光', 'Dark': '暗'},
      'class': { 'Warrior': '战士',
                 'Caster': '术士',
                 'Defender': '重装',
                 'Medic': '医疗',
                 'Sniper': '狙击',
                 'Vanguard': '先锋'},
      'constellation': { 'Aries': '白羊座',
                         'Taurus': '金牛座',
                         'Gemini': '双子座',
                         'Cancer': '巨蟹座',
                         'Leo': '狮子座',
                         'Virgo': '处女座',
                         'Libra': '天秤座',
                         'Scorpio': '天蝎座',
                         'Scorpius': '天蝎座',
                         'Sagittarius': '射手座',
                         'Capricorn': '摩羯座',
                         'Capricornus': '摩羯座',
                         'Aquarius': '水瓶座',
                         'Pisces': '双鱼座'},
      'discipline_stat': { 'ATK%': ['攻击力', True],
                           'HP%': ['生命值', True],
                           'DEF%': ['防御力', True],
                           'Speed': ['速度', False],
                           'Effect_Hit_Rate': ['效果命中', True],
                           'Effect_RES': ['效果抵抗', True],
                           'Crit_Rate': ['暴击率', True],
                           'Crit_DMG': ['暴击伤害', True]},
      'bond_sell_gold': {'5': '12500', '4': '4200', '3': '2100'},
      'bond_sell_fragment': {'5': '30', '4': '8', '3': '1'},
      'bond_xp_value': {'5': '1030', '4': '850', '3': '680'},
      'stat_multipliers': {'ATK': 608, 'HP': 4960, 'DEF': 617, 'SPD': 100}}


# ── Loading (mtime-cached) ────────────────────────────────────────

_lock = threading.Lock()
_cache: dict | None = None
_cache_mtime: float | None = None


def _read_file() -> tuple[dict | None, float | None]:
    """Return (file_data, mtime); (None, mtime) if missing/unreadable."""
    try:
        mtime = os.path.getmtime(GLOSSARY_PATH)
    except OSError:
        return None, None
    try:
        with open(GLOSSARY_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            print("[wiki_glossary] 术语表文件不是 JSON 对象，回退内置默认", flush=True)
            return None, mtime
        return data, mtime
    except Exception as e:
        print(f"[wiki_glossary] 术语表文件读取失败（{e}），回退内置默认", flush=True)
        return None, mtime


def load_glossary() -> dict:
    """Full glossary: file tables override built-in defaults per-table.

    Result is cached until the file's mtime changes. Do NOT mutate the
    returned dict (shared cache).
    """
    global _cache, _cache_mtime
    data, mtime = _read_file()
    with _lock:
        if _cache is not None and mtime == _cache_mtime:
            return _cache
        merged = copy.deepcopy(DEFAULT_GLOSSARY)
        if data:
            for table in TABLE_SHAPES:
                if table in data:
                    merged[table] = data[table]
        _cache = merged
        _cache_mtime = mtime
        return _cache


def glossary_source() -> str:
    """'file' when a readable glossary file exists, else 'builtin'."""
    _, mtime = _read_file()
    return "file" if mtime is not None else "builtin"


def default_glossary() -> dict:
    """Deep copy of the built-in defaults (panel「恢复内置默认」)."""
    return copy.deepcopy(DEFAULT_GLOSSARY)


def build_prompt_glossary(kind: str) -> str:
    """Render the LLM prompt glossary line for kind ('char'|'bond').

    Format is byte-identical to the historical inline strings:
    ``术语参考：ATK=攻击力，…。``
    """
    table = load_glossary().get(
        "prompt_terms_char" if kind == "char" else "prompt_terms_bond", [])
    if not table:
        return ""
    return "术语参考：" + "，".join(f"{en}={cn}" for en, cn in table) + "。"


# ── Validation + saving (panel path) ──────────────────────────────

def validate_glossary(data: dict) -> list[str]:
    """Return a list of human-readable errors ([] = valid)."""
    errors: list[str] = []
    if not isinstance(data, dict) or not data:
        return ["术语表必须是非空 JSON 对象"]
    for table, value in data.items():
        shape = TABLE_SHAPES.get(table)
        if shape is None:
            errors.append(f"未知表「{table}」")
            continue
        if shape == "pairs":
            if not isinstance(value, list) or not value:
                errors.append(f"{table}: 必须是非空的有序 [[en, zh], ...] 列表")
                continue
            for i, pair in enumerate(value):
                if (not isinstance(pair, list) or len(pair) != 2
                        or not all(isinstance(s, str) and s.strip() for s in pair)):
                    errors.append(f"{table}[{i}]: 必须是 [非空英文, 非空中文]")
        elif shape == "map":
            if not isinstance(value, dict) or not value:
                errors.append(f"{table}: 必须是非空对象 {{key: 中文字符串}}")
                continue
            for k, v in value.items():
                if not isinstance(v, str) or not v.strip():
                    errors.append(f"{table}[{k}]: 值必须是非空字符串")
        elif shape == "map_flag":
            if not isinstance(value, dict) or not value:
                errors.append(f"{table}: 必须是非空对象 {{key: [中文, 布尔]}}")
                continue
            for k, v in value.items():
                if (not isinstance(v, list) or len(v) != 2
                        or not isinstance(v[0], str) or not v[0].strip()
                        or not isinstance(v[1], bool)):
                    errors.append(f"{table}[{k}]: 值必须是 [非空中文, true/false]")
        elif shape == "map_int":
            if not isinstance(value, dict) or not value:
                errors.append(f"{table}: 必须是非空对象 {{key: 整数}}")
                continue
            for k, v in value.items():
                if not isinstance(v, int) or isinstance(v, bool):
                    errors.append(f"{table}[{k}]: 值必须是整数")
    return errors


def save_glossary(data: dict) -> dict:
    """Validate + backup + atomically replace the glossary file.

    Only whitelisted tables are written (unknown keys are rejected, not
    silently dropped). Returns ``{"ok": True, "tables": n}`` or ``{"error": …}``.
    """
    errors = validate_glossary(data)
    if errors:
        return {"error": "；".join(errors[:5])}
    # normalize: strip whitespace in string cells, keep order/duplicates
    out: dict = {}
    for table, value in data.items():
        shape = TABLE_SHAPES[table]
        if shape == "pairs":
            out[table] = [[en.strip(), cn.strip()] for en, cn in value]
        elif shape in ("map", "map_flag"):
            out[table] = value
        else:
            out[table] = value
    try:
        os.makedirs(os.path.dirname(GLOSSARY_PATH), exist_ok=True)
        if os.path.exists(GLOSSARY_PATH):
            shutil.copy2(GLOSSARY_PATH, GLOSSARY_PATH + ".bak")
        tmp = GLOSSARY_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        os.replace(tmp, GLOSSARY_PATH)
    except Exception as e:
        return {"error": f"写入失败: {e}"}
    global _cache
    with _lock:
        _cache = None  # force reload (mtime granularity can be coarse)
    return {"ok": True, "tables": len(out)}

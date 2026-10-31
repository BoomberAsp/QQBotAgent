"""GameData source — 解包产物（gamedata）的只读查询层。

产物由独立项目 ``E:\\ArkReCodeMiner`` 生成（设计文档见仓库外的 ``gamedata-source-design.md``），
经 zip 同步到数据盘后由本模块读取；**运行时不依赖 UnityPy，也不做任何解包**。

设计要点：
  * **状态 ID 是唯一规范键**；显示名取 ENG（``names.en``），其它语言只做别名。
  * ``role_grants`` 给的是「角色级进战授予」，带 ``phase``：
      - ``pre``        —— 早于乱速即可见，可用于「跑条前截图必须看到」的 blocking 判定
      - ``post_start`` —— 晚于乱速（实测夏妮觉醒护盾），只能 advisory
      - ``runtime``    —— 战斗中触发
    且 ``advisory = (source == "awaken") or (phase != "pre")``：觉醒等级在截图上不可知。
  * 产物缺失/过期时 ``available`` 为 False，调用方回落到 wiki 逻辑。

环境变量：
  ``GAMEDATA_DIR``   —— 显式指定产物目录（优先）
  ``USER_DATA_ROOT`` —— 未指定时取 ``$USER_DATA_ROOT/gamedata``
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import unicodedata

log = logging.getLogger(__name__)

# 旧标签（wiki / 手工图标 PNG）→ 状态 ID。gamedata 规范名与旧名不一致的主要是这几项。
LEGACY_LABEL_TO_ID = {
    "技能免疫": "SkillNullifier",
    "技能伤害无效": "SkillNullifier",
    "无法恢复": "Unhealable",
    "禁疗": "Unhealable",
    "晕眩": "Stun",
    "眩晕": "Stun",
    "睡眠": "Sleep",
    "沉睡": "Sleep",
    "速度下降": "ReduceSpeed",
    "速度降低": "ReduceSpeed",
    "攻击力下降": "ReduceAttack",
    "攻击力降低": "ReduceAttack",
    "防御力下降": "ReduceDef",
    "命中率下降": "DecreaseHitChance",
    "妨害": "Brand",
    "妨碍": "Brand",
    "闪避": "IncreaseEvasion",
    "闪避提升": "IncreaseEvasion",
    "看破": "Knowledge",
    "气魄": "Vigor",
    "护盾": "Shield",
    "免疫": "Immune",
    "无敌": "Invincible",
    "反击": "Counterattack",
    "吸血": "Lifesteal",
    "潜伏": "Stealth",
    "沉默": "Silence",
    "流血": "Bleed",
    "中毒": "Poison",
    "灼烧": "Burn",
    "炸弹": "Bomb",
    "诅咒": "Curse",
    "拘禁": "Restrict",
    "锁定": "Target",
    "嘲讽": "Taunt",
    "指定嘲讽": "TargetTaunt",
    "无法强化": "CannotBuff",
    "激怒": "Enrage",
    "狂暴": "Rage",
    "复苏": "Revive",
    "不屈": "Immortal",
    "不屈（无法解除）": "Immortal_Resident",
    "持续恢复": "ContinuousHeal",
    "暴击率提升": "IncreaseCriticalHitChance",
    "暴击伤害提升": "IncreaseCriticalHitDamage",
    "暴击抵抗": "IncreaseCriticalHitResistance",
    "攻击力提升": "AddAttack",
    "攻击力大幅提升": "AddAttackBig",
    "防御力提升": "AddDefence",
    "速度提升": "IncreaseSpeed",
    "反弹": "Reflection",
    "回生": "VamoiricTouch",
    "被动无效": "Seal",
    "咒印": "MagicNail",
    "星链标记": "H168S2_1",
}

_PUNCT = re.compile(r"[\s\u3000·・\-_/\\()\[\]{}（）【】「」『』,，.。:：;；!！?？'\"`~+|]+")


def _norm(s: str) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s)
    return _PUNCT.sub("", s).strip().lower()


class GameData:
    """一次加载、进程内共享（产物是静态文件；重新同步后需 ``get_source(refresh=True)``）。"""

    def __init__(self, base_dir: str):
        self.base_dir = base_dir
        self.status: dict[str, dict] = {}
        self.skills: dict[str, dict] = {}
        self.skill_grants: dict[str, dict] = {}
        self.role_grants: dict[str, dict] = {}
        self.roles: dict[str, dict] = {}
        self.bonds: dict[str, dict] = {}
        self.locale: dict[str, dict[str, str]] = {}
        self.manifest: dict = {}
        self._alias: dict[str, str] = {}
        self._role_alias: dict[str, str] = {}
        self._load()

    # ── 加载 ────────────────────────────────────────────────────
    def _read(self, name: str):
        path = os.path.join(self.base_dir, name)
        if not os.path.exists(path):
            return None
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:                       # 损坏的产物不应拖垮 bot
            log.warning("[gamedata] 读取 %s 失败: %s", name, e)
            return None

    def _load(self) -> None:
        cat = self._read("status_catalog.json")
        if cat:
            self.status = cat.get("statuses") or {}
        self.manifest = self._read("manifest.json") or {}
        self.skills = (self._read("skills.json") or {}).get("skills") or {}
        self.skill_grants = self._read("skill_grants.json") or {}
        self.role_grants = self._read("role_grants.json") or {}
        self.roles = (self._read("roles.json") or {}).get("roles") or {}
        self.bonds = (self._read("bonds.json") or {}).get("bonds") or {}

        for code in ("en", "zh_hans", "zh_hant"):
            table = self._read(os.path.join("locale", "slim", f"{code}.json"))
            if table:
                self.locale[code] = table

        for sid, st in self.status.items():
            for a in [sid, *(st.get("aliases") or []), *((st.get("names") or {}).values())]:
                key = _norm(a)
                if key and key not in self._alias:
                    self._alias[key] = sid
        for label, sid in LEGACY_LABEL_TO_ID.items():
            if sid in self.status:
                self._alias.setdefault(_norm(label), sid)

        for rid, role in self.roles.items():
            for a in [rid, *(role.get("aliases") or []), *((role.get("names") or {}).values())]:
                key = _norm(a)
                if key and key not in self._role_alias:
                    self._role_alias[key] = rid

        if self.status:
            log.info("[gamedata] 载入 %s：状态 %d / 技能 %d / 角色 %d / 羁绊 %d（build=%s）",
                     self.base_dir, len(self.status), len(self.skills), len(self.roles),
                     len(self.bonds), (self.manifest or {}).get("game_build"))

    # ── 可用性 ──────────────────────────────────────────────────
    @property
    def available(self) -> bool:
        return bool(self.status)

    def template_dir(self) -> str:
        return os.path.join(self.base_dir, "icons", "status")

    # ── 状态 ────────────────────────────────────────────────────
    def status_info(self, sid: str) -> dict | None:
        return self.status.get(sid)

    def resolve(self, text: str) -> str | None:
        """任意写法（ID / ENG / 简繁中文 / 旧标签）→ 状态 ID。"""
        if not text:
            return None
        if text in self.status:
            return text
        return self._alias.get(_norm(text))

    def display_name(self, sid: str, lang: str = "en") -> str:
        names = (self.status.get(sid) or {}).get("names") or {}
        return names.get(lang) or names.get("en") or sid

    def has_row_icon(self, sid: str) -> bool:
        return bool((self.status.get(sid) or {}).get("has_row_icon"))

    def is_resident(self, sid: str) -> bool:
        return bool((self.status.get(sid) or {}).get("resident"))

    def round_count(self, sid: str) -> int | None:
        return (self.status.get(sid) or {}).get("round_count")

    def exclusive_owner(self, sid: str) -> str | None:
        return (self.status.get(sid) or {}).get("exclusive_to_role")

    # ── 技能 ────────────────────────────────────────────────────
    def skill_info(self, skill_id: str) -> dict | None:
        return self.skills.get(skill_id)

    def grants_for_skill(self, skill_id: str) -> list[dict]:
        return (self.skill_grants.get(skill_id) or {}).get("grants") or []

    def entry_grants_for_skill(self, skill_id: str) -> list[str]:
        return (self.skill_grants.get(skill_id) or {}).get("entry_grants") or []

    def ag_ops_for_skill(self, skill_id: str) -> list[dict]:
        """行动值操纵（拉条/推条）：数值来自 DSL 的 ``AddTime.Value``，不是从文案猜。"""
        return (self.skill_grants.get(skill_id) or {}).get("ag_ops") or []

    # ── 角色级进战授予 ───────────────────────────────────────────
    def role_grants_of(self, role_code: str) -> dict:
        return self.role_grants.get(role_code) or {}

    def all_grant_ids(self, role_code: str) -> list[str]:
        """该角色所有技能+觉醒可能授予的状态 ID（行级模板白名单用）。"""
        return sorted({g["id"] for g in (self.role_grants_of(role_code).get("grants") or [])})

    def pre_grants(self, role_code: str) -> list[str]:
        """早于乱速即应可见的状态（blocking 候选）。"""
        return self.role_grants_of(role_code).get("pre_grants") or []

    def advisory_grants(self, role_code: str) -> list[str]:
        """时序/养成相关的进战状态（不能用于判 pre 无效）。"""
        return self.role_grants_of(role_code).get("advisory_grants") or []

    def entry_grants(self, role_code: str) -> list[str]:
        return self.role_grants_of(role_code).get("entry_grants") or []

    # ── 角色 ────────────────────────────────────────────────────
    def role(self, role_code: str) -> dict | None:
        return self.roles.get(role_code)

    def resolve_role(self, text: str) -> str | None:
        """角色名 → 角色 ID。

        优先级：角色 ID → **面板维护的 character_dic.json**（管理员覆盖层）→
        产物内置别名（ENG/简繁/旧标签）。character_dic 改动按 mtime 自动生效。
        """
        if not text:
            return None
        t = text.strip()
        if t in self.roles:
            return t
        canon = _dic_lookup(t)
        if canon:
            hit = self._role_alias.get(_norm(canon))
            if hit:
                return hit
        return self._role_alias.get(_norm(t))

    def role_display_name(self, role_code: str, lang: str = "zh_hans") -> str:
        names = (self.roles.get(role_code) or {}).get("names") or {}
        return names.get(lang) or names.get("en") or role_code

    def role_skills(self, role_code: str) -> list[dict]:
        return (self.roles.get(role_code) or {}).get("skills") or []

    # ── 羁绊 / 本地化 ───────────────────────────────────────────
    def bonds_of_role(self, role_code: str) -> list[dict]:
        return [b for b in self.bonds.values() if b.get("role") == role_code]

    def bond(self, bond_id: str) -> dict | None:
        return self.bonds.get(bond_id)

    def locale_text(self, key: str, lang: str = "zh_hans") -> str | None:
        if not key:
            return None
        val = (self.locale.get(lang) or {}).get(key)
        if val:
            return val
        # 剧情台词正文不在 slim locale 里，走 story/text_<lang>.json 切片
        return (self._story_text(lang) or {}).get(key)

    def _story_text(self, lang: str) -> dict:
        if not hasattr(self, "_story_text_cache"):
            self._story_text_cache: dict[str, dict] = {}
        if lang not in self._story_text_cache:
            data = self._read(os.path.join("story", f"text_{lang}.json"))
            self._story_text_cache[lang] = data if isinstance(data, dict) else {}
        return self._story_text_cache[lang]

    # ── 剧情（P3，按需懒加载；dialogs.json 较大不预载）───────────
    def _story(self, name: str) -> dict:
        if not hasattr(self, "_story_cache"):
            self._story_cache: dict[str, dict] = {}
        if name not in self._story_cache:
            data = self._read(os.path.join("story", f"{name}.json"))
            self._story_cache[name] = data if isinstance(data, (dict, list)) else {}
        return self._story_cache[name]

    def story_episodes(self) -> dict:
        return self._story("episodes")

    def story_chapters(self) -> dict:
        return self._story("chapters")

    def story_scenes(self) -> dict:
        return self._story("scenes")

    def story_line_dialogs(self) -> list:
        return self._story("line_dialogs") or []

    def story_dialogs(self) -> list:
        return self._story("dialogs") or []

    def chapter_scenes(self, chapter_id: str) -> list[dict]:
        scenes = self.story_scenes()
        return [s for s in scenes.values() if s.get("chapter") == chapter_id]

    def scene_text(self, scene_id: str, lang: str = "zh_hans") -> str | None:
        scene = self.story_scenes().get(scene_id) or {}
        names = scene.get("name") or {}
        return names.get(lang) or names.get("en")

    def bond_drama_ids(self, role_code: str) -> list[str]:
        """角色的羁绊剧情（LineDrama ID）列表，按出现顺序去重。"""
        out: list[str] = []
        seen: set[str] = set()
        for b in self.bonds_of_role(role_code):
            for did in b.get("drama_ids") or []:
                if did and did not in seen:
                    seen.add(did)
                    out.append(did)
        return out

    @staticmethod
    def _drama_paths(rows: list[dict], max_paths: int = 24) -> tuple[list[list[str]], dict]:
        """把一段 drama 的台词图展开成**多条完整路径**（不再把分支压平成一条）。

        353 个 drama 里有 251 个含 ``NextDialogIDs`` 分叉（339 行），把分支顺序
        拼起来会让玩家看到的剧情顺序错乱，因此这里按路径返回：
        每个分叉点产出若干条"从头到叶子"的可读路径（共享前缀会重复出现在每条里）。
        """
        index = {r.get("id"): r for r in rows}
        succ = {r.get("id"): [n for n in str(r.get("next") or "").split("~") if n in index]
                for r in rows}
        pred: set[str] = set()
        for v in succ.values():
            pred.update(v)
        roots = [r.get("id") for r in rows if r.get("id") not in pred] or [rows[0].get("id")]
        paths: list[list[str]] = []
        branch_nodes: dict[str, int] = {}

        def walk(node_id: str, acc: list[str]) -> None:
            if len(paths) >= max_paths:
                return
            row = index.get(node_id)
            if row is None:
                return
            acc = acc + [node_id]
            nxt = succ.get(node_id) or []
            if len(nxt) > 1:
                branch_nodes[node_id] = len(nxt)
                for n in nxt:
                    walk(n, acc)
                return
            if not nxt:
                paths.append(acc)
                return
            if nxt[0] in acc:          # 环保护
                paths.append(acc)
                return
            walk(nxt[0], acc)

        for root in roots:
            walk(root, [])
        return paths, branch_nodes

    def bond_drama_lines(self, role_code: str, lang: str = "zh_hans",
                         max_dramas: int = 0, max_lines: int = 0,
                         with_paths: bool = False) -> list[dict]:
        """羁绊剧情台词：按**路径**展开（分支不压平）。

        每个 drama 返回 ``{drama, lines, path_count, branch_points, all_paths?}``；
        ``lines`` 是第一条路径（玩家未做选择时最可能看到的主线），``all_paths``
        需要显式 ``with_paths=True`` 才返回（分支可能很多，别默认带上）。
        """
        drama_ids = self.bond_drama_ids(role_code)
        if max_dramas:
            drama_ids = drama_ids[:max_dramas]
        by_drama: dict[str, list[dict]] = {}
        for row in self.story_line_dialogs():
            d = row.get("drama")
            if d in drama_ids:
                by_drama.setdefault(d, []).append(row)

        def to_objs(ids: list[str], index: dict) -> list[dict]:
            out = []
            for i in ids:
                row = index.get(i) or {}
                text = (self.locale_text(row.get("text_key") or "", lang)
                        if row.get("text_key") else None)
                out.append({"id": i, "speaker": row.get("speaker"),
                            "type": row.get("type"), "text": text})
            return out

        result: list[dict] = []
        for did in drama_ids:
            rows = by_drama.get(did) or []
            if not rows:
                continue
            paths, branches = self._drama_paths(rows)
            index = {r.get("id"): r for r in rows}
            main = paths[0] if paths else []
            lines = to_objs(main, index)
            if max_lines:
                lines = lines[:max_lines]
            item = {"drama": did, "lines": lines,
                    "path_count": len(paths),
                    "branch_points": len(branches),
                    "row_count": len(rows)}
            if with_paths:
                item["all_paths"] = [to_objs(p, index) for p in paths]
            result.append(item)
        return result

    # ── 诊断 ────────────────────────────────────────────────────
    def stats(self) -> dict:
        return {
            "base_dir": self.base_dir,
            "build": (self.manifest or {}).get("game_build"),
            "statuses": len(self.status),
            "skills": len(self.skills),
            "roles": len(self.roles),
            "bonds": len(self.bonds),
            "templates": self._count_templates(),
            "aliases": len(self._alias),
        }

    def _count_templates(self) -> int:
        d = self.template_dir()
        if not os.path.isdir(d):
            return 0
        return sum(1 for f in os.listdir(d) if f.endswith(".png"))


# ── 单例 ────────────────────────────────────────────────────────

_lock = threading.Lock()
_source: GameData | None = None


def default_dir() -> str:
    explicit = os.environ.get("GAMEDATA_DIR")
    if explicit:
        return explicit
    root = os.environ.get("USER_DATA_ROOT")
    if root:
        return os.path.join(root, "gamedata")
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(here, "..", "data", "gamedata")


_SOURCE_STAMP: dict = {"stamp": None}


def _source_stamp() -> tuple:
    """产物版本指纹（manifest.json 的 mtime + size）。"""
    try:
        st = os.stat(os.path.join(os.path.abspath(default_dir()), "manifest.json"))
        return (st.st_mtime, st.st_size)
    except OSError:
        return (None, None)


def get_source(refresh: bool = False) -> GameData:
    """进程级单例；``manifest.json`` 变了就自动重建（上传后无需重启）。

    产物缺失时返回 ``available=False`` 的空源（不抛异常）。
    """
    global _source
    with _lock:
        stamp = _source_stamp()
        stale = _SOURCE_STAMP["stamp"] not in (None, stamp)
        if _source is None or refresh or stale:
            _source = GameData(os.path.abspath(default_dir()))
            _SOURCE_STAMP["stamp"] = stamp
        return _source


def status_id(label_or_id: str) -> str | None:
    return get_source().resolve(label_or_id)


def is_available() -> bool:
    return get_source().available


# ── 面板维护的别名映射（character_dic.json，管理员覆盖层）──────────
_CHAR_DIC: dict = {"mtime": None, "map": {}}


def _char_dic_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "..", "config", "characters", "character_dic.json")


def _char_dic() -> dict:
    """别名 → 规范中文名；文件变了自动重载（面板保存后无需重启）。"""
    path = _char_dic_path()
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {}
    if _CHAR_DIC["mtime"] == mtime:
        return _CHAR_DIC["map"]
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f) or {}
        mapping = {str(k).strip(): str(v).strip() for k, v in raw.items() if k and v}
    except Exception:
        mapping = {}
    _CHAR_DIC.update({"mtime": mtime, "map": mapping})
    return mapping


def _dic_lookup(text: str) -> str | None:
    """在面板别名表里查规范名（先原样、再归一化）。"""
    m = _char_dic()
    if not m:
        return None
    return m.get(text) or m.get(_norm(text))

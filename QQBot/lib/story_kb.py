"""
Story Knowledge Base — Ark Re:Code plot retrieval ("了解原作"的记忆层).

Build offline with ``scripts/build_story_kb.py`` into
``data/story_kb/story.db``; this module is the read-only runtime side.

Why a custom SQLite/FTS5 store instead of embeddings
---------------------------------------------------
The production venv has no sentence-transformers / faiss / jieba, and the
story corpus is ~50k dialogue lines of *named-speaker dialogue*, which is
exactly what BM25 is good at. So retrieval is:

    Chinese query ──► unit/scene Chinese index (bigram tokenised)  ─┐
                                                                     ├─► RRF ─► rerank ─► hits
    English query ──► dialogue index (unicode61, BM25 weighted)   ─┘

Design notes
------------
* SQLite ships FTS5 ``unicode61`` but no CJK segmenter, so Chinese text is
  stored as overlapping **bigrams** (``露比``/``比的``/…). A query is split
  the same way, so 2-character terms still match (FTS5 ``trigram`` would
  need >= 3 chars). Function words are dropped from the bigram query.
* Two contentless FTS5 tables keyed by ``rowid == chunk.id`` keep the
  index compact and let ``bm25()`` do the ranking.
* Final ranking blends the two ranked lists (RRF) plus explicit bonuses:
  the active persona's character being *in* the scene, main story over
  urgent mission, and a scene that has a Chinese gist (better to inject:
  it gives the model a Chinese summary next to raw English lines).
* The module never imports NoneBot so build scripts and tests can use it.

Public API
----------
    story_kb.available                     -> bool
    story_kb.search(query, ...)            -> list[StoryHit]
    story_kb.format_injection(query, ...)  -> str   (L4 volatile block)
    story_kb.format_tool_result(query, ...)-> str   (story_search tool text)
    story_kb.stats()                       -> dict
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

# ── Constants ──────────────────────────────────────────────────────

# ── Paths / data root ──────────────────────────────────────────────
#
# The database is a *build artifact*, not source: on a real deployment it lives
# on the data disk next to the per-user memories and workspaces
# (USER_DATA_ROOT=/mnt/datadisk0/QQBotUserData → …/story_kb/story.db), so it
# survives `git pull` / redeploys and is picked up by whatever backs up
# USER_DATA_ROOT.
#
# Resolution order (first hit wins):
#   1. $STORY_KB_DIR                 explicit override, build + read
#   2. $USER_DATA_ROOT/story_kb      the deployment data disk
#   3. <QQBot>/data/story_kb         a plain dev checkout
_QQBOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ── Persona allowlist ──────────────────────────────────────────────
#
# Story memory is opt-in per persona: only the personas listed in
# config/story_personas.json get the per-turn injection and may call the
# `story_search` tool. Everyone else (UGC characters, 角色 Roxy …) sees neither,
# so a user-created character never starts reciting Ark Re:Code canon.
_STORY_PERSONAS_FILE = os.path.join(_QQBOT_DIR, "config", "story_personas.json")
_DEFAULT_STORY_PERSONAS = frozenset({"rubi", "assistant"})
_personas_cache: Dict[str, object] = {"mtime": None, "path": "", "value": _DEFAULT_STORY_PERSONAS}


def story_personas(path: Optional[str] = None) -> set:
    """Persona keys allowed to use story memory (mtime-cached, default rubi+assistant).

    A malformed/missing config falls back to the default instead of disabling the
    feature silently.
    """
    target = path or _STORY_PERSONAS_FILE
    try:
        mtime = os.path.getmtime(target)
    except OSError:
        return set(_DEFAULT_STORY_PERSONAS)
    if (_personas_cache["mtime"] == mtime
            and _personas_cache["path"] == target):
        return set(_personas_cache["value"])
    try:
        with open(target, "r", encoding="utf-8") as f:
            raw = json.load(f)
        names = raw.get("personas") if isinstance(raw, dict) else raw
        value = frozenset(
            str(n).strip() for n in (names or [])
            if isinstance(n, (str, int)) and str(n).strip()
        )
    except Exception:  # noqa: BLE001
        return set(_DEFAULT_STORY_PERSONAS)
    if not value:
        value = _DEFAULT_STORY_PERSONAS
    _personas_cache.update({"mtime": mtime, "path": target, "value": value})
    return set(value)


_NEUTRAL_CACHE: Dict[str, object] = {"mtime": None, "path": "", "value": frozenset({"assistant"})}


def story_neutral_personas(path: Optional[str] = None) -> set:
    """Personas allowed to inject story memory *without* a character mapping.

    These get the neutral (third-person reference) framing instead of the
    in-character "your own memory" framing. Defaults to ``{"assistant"}``.
    """
    target = path or _STORY_PERSONAS_FILE
    try:
        mtime = os.path.getmtime(target)
    except OSError:
        return set(_NEUTRAL_CACHE["value"])
    if _NEUTRAL_CACHE["mtime"] == mtime and _NEUTRAL_CACHE["path"] == target:
        return set(_NEUTRAL_CACHE["value"])
    value = frozenset({"assistant"})
    try:
        with open(target, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict) and raw.get("neutral") is not None:
            value = frozenset(
                str(n).strip() for n in (raw.get("neutral") or [])
                if isinstance(n, (str, int)) and str(n).strip()
            )
    except Exception:  # noqa: BLE001
        pass
    _NEUTRAL_CACHE.update({"mtime": mtime, "path": target, "value": value})
    return set(value)


def _clean(path: str) -> str:
    return (path or "").strip().rstrip("/\\")


_DOTENV_LOADED = False


def _load_dotenv_once() -> None:
    """Populate os.environ from ``QQBot/.env`` the first time a path resolves.

    The bot loads .env when agent/turn.py is imported, but scripts, the webui
    process and tests can reach this module first. Without this, USER_DATA_ROOT
    would be absent and the KB would silently resolve to the git checkout
    instead of the data disk. Existing environment variables always win, which
    matches python-dotenv's default behaviour.
    """
    global _DOTENV_LOADED
    if _DOTENV_LOADED:
        return
    _DOTENV_LOADED = True
    try:
        with open(os.path.join(_QQBOT_DIR, ".env"), "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                if not key or key in os.environ:
                    continue
                os.environ[key] = value.split(" #")[0].strip().strip('"').strip("'")
    except OSError:
        pass


def data_root() -> str:
    """The configured data root (``USER_DATA_ROOT``), or "" when unset."""
    _load_dotenv_once()
    return _clean(os.environ.get("USER_DATA_ROOT", ""))


def story_kb_candidates() -> List[str]:
    """Every directory the KB may live in, most authoritative first."""
    _load_dotenv_once()
    out: List[str] = []
    explicit = _clean(os.environ.get("STORY_KB_DIR", ""))
    if explicit:
        out.append(explicit)
    root = data_root()
    if root:
        out.append(os.path.join(root, "story_kb"))
    out.append(os.path.join(_QQBOT_DIR, "data", "story_kb"))
    return out


def preferred_story_kb_dir() -> str:
    """Where the KB *should* live on this host (created on demand by the builder)."""
    _load_dotenv_once()
    explicit = _clean(os.environ.get("STORY_KB_DIR", ""))
    if explicit:
        return explicit
    root = data_root()
    if root and os.path.isdir(root):
        return os.path.join(root, "story_kb")
    return os.path.join(_QQBOT_DIR, "data", "story_kb")


def default_db_path() -> str:
    """Absolute path of the database to read.

    Deliberately *not* a cross-candidate search: in a configured deployment the
    KB location must be deterministic, otherwise the bot could silently serve a
    stale copy left inside the git checkout while the data disk holds the real
    one. When USER_DATA_ROOT is unset (or points at a path that does not exist
    yet, e.g. a dev box), ``preferred_story_kb_dir()`` already resolves to
    ``<QQBot>/data/story_kb`` — so a bare checkout keeps working.
    """
    return os.path.join(preferred_story_kb_dir(), "story.db")

# Schema version written by the builder; the reader refuses newer DBs
# (forward-compatibility guard) and tolerates older ones.
#   1 — initial (unit / scene / chunk + two FTS indexes)
#   2 — scene.zh_kw (per-scene Chinese keywords, used by ranking + entity gate)
#   3 — scene.cast_line (English speaker → official Chinese name)
SCHEMA_VERSION = 3

# RRF constant (standard value from the TREC RRF literature).
_RRF_K = 60

# Column weights for bm25(): FTS5 allows one weight per indexed column.
_EN_WEIGHTS = (3.0, 1.0, 2.0)  # title, text_en, chars
_ZH_WEIGHTS = (1.0,)

# Auto-injection ("does this turn deserve story memory?") thresholds.
# RRF scores live in a narrow band: 1/(60+rank) per list, so two rank-1 hits
# score 0.0328 and a single rank-1 hit 0.0164. Anything at or above
# _STRONG_SCORE is injected even without a recognised entity in the query;
# below it, a named entity (character/event/keyword) is required, which is
# what keeps 闲聊 and unrelated task messages out.
_STRONG_SCORE = 0.033

# Scene-level keywords reused by more scenes than this are treated as generic
# topical words, not entities (they would make the gate fire on any sentence).
_SCENE_TERM_MAX_DF = 3

# Everyday nouns that leak out of the scene keywords and would otherwise make
# the auto-injection gate fire on unrelated chitchat ("推荐一部电影"). These are
# vocabulary the story world also happens to mention; the *specific* story
# entities (露比 / 方舟 / 使徒 / 星源 / 真理会 …) are deliberately absent.
_GENERIC_TERMS = set("""
电影 时间 事情 东西 地方 声音 味道 感觉 感情 朋友 大家 自己 现在 之前 之后
今天 明天 昨天 样子 心情 计划 工作 任务 目标 结果 原因 问题 答案 方法 办法
机会 能力 力量 记忆 回忆 故事 世界 城市 国家 生活 食物 饮料 衣服 房间 街道
价格 数量 名字 称呼 关系 队伍 小队 组织 会议 命令 报告 消息 情报 资料 内容
推荐 建议 喜欢 讨厌 需要 希望 准备 发现 告诉 选择 决定 开始 结束 觉得 认为
知道 明白 担心 放心 努力 坚持 等待 寻找 追击 逃走 出发 到达 离开 回来 见面
相遇 交谈 讨论 商量 拜托 答应 拒绝 道歉 感谢 解释 说明 承认 隐瞒 说谎 相信
怀疑 提醒 警告 照顾 保护 治疗 受伤 死亡 梦想 理想 愿望 承诺 约定
""".split())

# Chinese function characters — bigrams made ONLY of these are dropped from
# the query (they match everything and add noise, e.g. 比的 / 了吗 / 就是).
_ZH_STOP_CHARS = set(
    "的了着过是在有和与及或就都也很不没吗呢啊吧呀嘛哦嗯这那个们"
    "我你他她它上下中里说要点么什怎为以对把被给让而且地得之其"
    "一二三又再还只更最太多少些样时后前边面"
)

# English stopwords for query tokenisation (BM25 already down-weights them,
# but dropping them keeps the MATCH expression small).
_EN_STOP = set(
    "a an the and or but if then than that this these those is are was were "
    "be been being am do does did doing have has had having i you he she it we "
    "they me him her us them my your his its our their of to in on at for with "
    "from by about into over under as not no so such can could will would shall "
    "should may might must s t just very too also there here what which who whom "
    "when where why how all any both each few more most other some only own same"
    .split()
)

# Upper bound on query terms — guards very long inputs.
_MAX_QUERY_TERMS = 24

_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_CJK_RUN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")

_ROMAN = ["I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X"]


# ── Data types ─────────────────────────────────────────────────────

@dataclass
class StoryHit:
    """One retrieved story chunk, already joined with unit/scene context."""

    chunk_id: int
    scene_id: int
    unit_id: int
    score: float
    # unit context
    work: str = ""                  # main | hard | prologue | event | urgent
    episode: str = ""
    chapter_no: Optional[int] = None
    chapter_title_en: str = ""
    chapter_title_zh: str = ""
    event_name: str = ""
    event_name_zh: str = ""
    page_title: str = ""
    url: str = ""
    zh_summary: str = ""
    # scene context
    scene_ord: int = 0
    scene_title: str = ""
    scene_title_zh: str = ""
    zh_gist: str = ""
    zh_kw: str = ""
    cast: str = ""
    char_ids: str = ""
    speakers: str = ""
    # chunk body
    ord: int = 0
    text_en: str = ""
    n_lines: int = 0

    # ── Convenience ───────────────────────────────────────────────

    @property
    def char_id_set(self) -> set:
        return {c for c in (self.char_ids or "").split(",") if c}

    def label(self, persona_char_id: str = "") -> str:
        """Human-readable source label, e.g. 主线 · Part I · 第1章《共鸣觉醒》."""
        bits: List[str] = []
        if self.work == "event":
            name = self.event_name_zh or self.event_name or self.page_title
            bits.append(f"活动剧情《{name}》")
        elif self.work == "urgent":
            bits.append("主线 · 紧急任务")
        elif self.work == "prologue":
            bits.append("主线 · 序章")
        else:
            bits.append("主线" if self.work == "main" else "主线（困难）")
            if self.episode:
                bits.append(_roman_episode(self.episode))
            if self.chapter_no is not None:
                title = self.chapter_title_zh or self.chapter_title_en
                bits.append(f"第{self.chapter_no}章《{title}》" if title
                            else f"第{self.chapter_no}章")
        scene = self.scene_title_zh or self.scene_title
        if scene:
            bits.append(scene)
        label = " · ".join(b for b in bits if b)
        # The "you were there" marker only makes sense for a mapped character;
        # for a neutral persona (assistant) the label carries no suffix.
        if persona_char_id:
            label += (" ｜ 你（本角色）在场" if persona_char_id in self.char_id_set
                      else " ｜ 你不在场")
        return label


# ── Text helpers ───────────────────────────────────────────────────

def _source_label(hit) -> str:
    """命中片段的来源标注 —— 让模型分清「游戏原文」与「wiki 英文原文/中文摘要」。

    混合数据源后这点很关键：主线/活动/羁绊（客户端）是**中文原文**，可以直接按
    措辞转述；wiki 来源是英文原文 + LLM 中文摘要，只能转述、不能当原话引用。
    """
    title = getattr(hit, "page_title", "") or ""
    work = getattr(hit, "work", "") or ""
    if work == "bond":
        return "〔来源：羁绊剧情·游戏原文〕"
    if "（活动·客户端原文）" in title:
        return "〔来源：活动剧情·游戏原文〕"
    if "（剧情）" in title:
        return "〔来源：主线剧情·游戏原文〕"
    if "（场景目录）" in title:
        return "〔来源：章节场景索引〕"
    if work == "event":
        return "〔来源：活动剧情·wiki〕"
    if work in ("main", "hard", "prologue"):
        return "〔来源：主线剧情·wiki〕"
    return "〔来源：wiki〕"


def has_cjk(text: str) -> bool:
    """True if ``text`` contains at least one CJK (Han) character."""
    return bool(_CJK_RE.search(text or ""))


def cjk_bigrams(text: str) -> List[str]:
    """Overlapping character bigrams for every CJK run in ``text``.

    Non-CJK characters act as separators. Single-character runs are emitted
    as-is so a 1-char query still produces a term.
    """
    out: List[str] = []
    for run in _CJK_RUN_RE.findall(text or ""):
        if len(run) == 1:
            out.append(run)
            continue
        for i in range(len(run) - 1):
            out.append(run[i:i + 2])
    return out


def zh_query_bigrams(query: str) -> List[str]:
    """Meaningful bigrams for a Chinese query (function-word bigrams removed)."""
    grams: List[str] = []
    seen = set()
    for g in cjk_bigrams(query):
        if len(g) == 2 and (g[0] in _ZH_STOP_CHARS and g[1] in _ZH_STOP_CHARS):
            continue
        if g not in seen:
            seen.add(g)
            grams.append(g)
    return grams[:_MAX_QUERY_TERMS]


def en_query_terms(query: str) -> List[str]:
    """Lower-cased ASCII query terms, stopwords and 1-char noise removed."""
    terms: List[str] = []
    seen = set()
    for tok in re.findall(r"[A-Za-z][A-Za-z0-9'’\-]{1,}", query or ""):
        low = tok.lower().strip("'’-")
        if len(low) < 2 or low in _EN_STOP:
            continue
        if low not in seen:
            seen.add(low)
            terms.append(low)
    return terms[:_MAX_QUERY_TERMS]


def _roman_episode(episode: str) -> str:
    """'Episode 2' → 'Part II'; anything else passes through."""
    m = re.match(r"Episode\s+(\d+)", episode or "")
    if m:
        num = int(m.group(1))
        if 0 < num <= len(_ROMAN):
            return "Part " + _ROMAN[num - 1]
        return f"第{num}部"
    return episode


def _fts_quote(term: str) -> str:
    """Quote a term for an FTS5 MATCH expression (doubling embedded quotes)."""
    return '"' + term.replace('"', '""') + '"'


# ── StoryKB ────────────────────────────────────────────────────────

class StoryKB:
    """Read-only reader over the story knowledge base."""

    def __init__(self, db_path: Optional[str] = None):
        # Resolved per instance (not at import time): USER_DATA_ROOT is read
        # from .env which the bot loads after this module may first be imported.
        self.db_path = os.path.abspath(db_path or default_db_path())
        self._lock = threading.Lock()
        self._ok: Optional[bool] = None
        self._stats: Dict[str, object] = {}
        self._entities: Optional[List[str]] = None
        # Filled by available(): schema version (informational) and whether this
        # DB carries the optional scene.cast_line column.
        self._schema = 0
        self._has_cast = False

    # ── Availability ─────────────────────────────────────────────

    # Columns this reader depends on. Checked instead of the schema version so
    # that an *additive* schema bump never disables the feature: the builder can
    # rewrite the DB before the process is restarted without any downtime, and a
    # reader newer than the DB still works as long as the required columns exist.
    _REQUIRED_COLUMNS = {
        "unit": {"id", "page_title", "work", "zh_summary", "zh_keywords"},
        "scene": {"id", "unit_id", "ord", "zh_gist", "zh_kw", "char_ids", "speakers"},
        "chunk": {"id", "scene_id", "text_en"},
    }

    @property
    def available(self) -> bool:
        """True when a readable, schema-compatible database exists.

        A negative result is re-checked whenever the DB file appears, so building
        the knowledge base while the bot is running becomes visible without a
        restart (the deploy order is: ship code → build/migrate DB → chat).
        """
        if self._ok:
            return True
        if self._ok is False and os.path.isfile(self.db_path):
            self._ok = None
            self._stats = {}
        if self._ok is not None:
            return self._ok
        self._ok = False
        if not os.path.isfile(self.db_path):
            return False
        try:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT value FROM meta WHERE key='schema_version'"
                ).fetchone()
                self._schema = int(row[0]) if row else 0
                if self._schema <= 0:
                    return False
                tables = {}
                for table, required in self._REQUIRED_COLUMNS.items():
                    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
                    tables[table] = cols
                    if not required <= cols:
                        return False
                self._has_cast = "cast_line" in tables.get("scene", set())
                self._ok = True
            finally:
                conn.close()
        except Exception:  # noqa: BLE001 — never break a chat turn
            self._ok = False
        return self._ok

    def _connect(self) -> sqlite3.Connection:
        """Open a short-lived read-only connection (SQLite handles are cheap)."""
        uri = "file:" + self.db_path.replace("\\", "/") + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn

    def stats(self) -> dict:
        """Row counts + build metadata (diagnostics only)."""
        if self._stats:
            return dict(self._stats)
        if not self.available:
            return {"available": False}
        try:
            conn = self._connect()
            try:
                def one(sql: str, default=0):
                    row = conn.execute(sql).fetchone()
                    return row[0] if row and row[0] is not None else default

                meta = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM meta")}
                self._stats = {
                    "available": True,
                    "db_path": self.db_path,
                    "schema_version": self._schema,
                    "has_cast_line": self._has_cast,
                    "units": one("SELECT COUNT(*) FROM unit"),
                    "scenes": one("SELECT COUNT(*) FROM scene"),
                    "chunks": one("SELECT COUNT(*) FROM chunk"),
                    "zh_units": one("SELECT COUNT(*) FROM unit WHERE zh_summary != ''"),
                    "dialog_lines": one("SELECT SUM(n_lines) FROM scene"),
                    "built_at": meta.get("built_at", ""),
                    "source": meta.get("source", ""),
                }
            finally:
                conn.close()
        except Exception:  # noqa: BLE001
            self._stats = {"available": False}
        return dict(self._stats)

    # ── Search ───────────────────────────────────────────────────

    def search(
        self,
        query: str,
        limit: int = 6,
        persona_char_id: str = "",
        pool: int = 60,
        min_score: float = 0.0,
    ) -> List[StoryHit]:
        """Retrieve the best matching story chunks for ``query``.

        Args:
            query: user text, Chinese or English.
            limit: number of hits returned.
            persona_char_id: character id of the active persona (e.g. H076);
                hits featuring that character are boosted and labelled.
            pool: candidate pool size per index before fusion.
            min_score: drop hits below this fused score.
        """
        query = (query or "").strip()
        if not query or not self.available:
            return []

        zh_grams = zh_query_bigrams(query)
        en_terms = en_query_terms(query)
        if not zh_grams and not en_terms:
            return []

        zh_ranked: List[int] = []
        en_ranked: List[int] = []

        try:
            with self._lock:
                conn = self._connect()
                try:
                    if zh_grams:
                        expr = " OR ".join(_fts_quote(g) for g in zh_grams)
                        rows = conn.execute(
                            "SELECT rowid AS cid, bm25(chunk_zh_fts, ?) AS s FROM chunk_zh_fts "
                            "WHERE chunk_zh_fts MATCH ? ORDER BY s LIMIT ?",
                            (*_ZH_WEIGHTS, expr, pool),
                        ).fetchall()
                        # bm25() returns a negative number; smaller = better match.
                        zh_ranked = [int(r["cid"]) for r in rows]

                    if en_terms:
                        expr = " OR ".join(_fts_quote(t) for t in en_terms)
                        rows = conn.execute(
                            "SELECT rowid AS cid, bm25(chunk_fts, ?, ?, ?) AS s FROM chunk_fts "
                            "WHERE chunk_fts MATCH ? ORDER BY s LIMIT ?",
                            (*_EN_WEIGHTS, expr, pool),
                        ).fetchall()
                        en_ranked = [int(r["cid"]) for r in rows]

                    if not zh_ranked and not en_ranked:
                        return []

                    fused = self._fuse(zh_ranked, en_ranked)
                    cand_ids = [cid for cid, _ in fused[: max(limit * 6, 24)]]
                    hits = self._load_hits(conn, cand_ids)
                finally:
                    conn.close()
        except Exception:  # noqa: BLE001 — retrieval must never break a turn
            return []

        by_id = {h.chunk_id: h for h in hits}
        q_zh = set(zh_grams)
        ranked: List[StoryHit] = []
        for cid, base in fused:
            hit = by_id.get(cid)
            if hit is None:
                continue
            score = base + self._bonus(hit, persona_char_id, q_zh)
            if score < min_score:
                continue
            hit.score = score
            ranked.append(hit)

        ranked.sort(key=lambda h: h.score, reverse=True)

        # Prefer diversity: one chunk per scene first, so a single long scene
        # cannot eat the whole budget; fill up from the remainder if needed.
        picked: List[StoryHit] = []
        seen_scenes: set = set()
        for hit in ranked:
            if hit.scene_id in seen_scenes:
                continue
            seen_scenes.add(hit.scene_id)
            picked.append(hit)
            if len(picked) >= limit:
                break
        if len(picked) < limit:
            picked_ids = {h.chunk_id for h in picked}
            for hit in ranked:
                if hit.chunk_id not in picked_ids:
                    picked.append(hit)
                if len(picked) >= limit:
                    break
        return picked

    @staticmethod
    def _fuse(zh_ranked: Sequence[int], en_ranked: Sequence[int]) -> List[Tuple[int, float]]:
        """Reciprocal Rank Fusion over the two ranked chunk-id lists."""
        fused: Dict[int, float] = {}
        for rank, cid in enumerate(zh_ranked):
            fused[cid] = fused.get(cid, 0.0) + 1.0 / (_RRF_K + rank + 1)
        for rank, cid in enumerate(en_ranked):
            fused[cid] = fused.get(cid, 0.0) + 1.0 / (_RRF_K + rank + 1)
        return sorted(fused.items(), key=lambda kv: kv[1], reverse=True)

    @staticmethod
    def _bonus(hit: "StoryHit", persona_char_id: str, q_zh: set) -> float:
        """Small deterministic boosts applied on top of the fused rank score.

        Weights are intentionally an order of magnitude below the RRF span
        (0.0164 per list at rank 1) so they only reorder near-ties or break
        ties between equally-matching scenes — except the title overlap,
        which is a genuine relevance signal ("第一次方舟战争" must surface
        the chapter called 第一次方舟战争).
        """
        bonus = 0.0
        # The persona actually being in the scene — the "she was there" prior.
        if persona_char_id and persona_char_id in hit.char_id_set:
            bonus += 0.0035
        if hit.work in ("main", "prologue"):
            bonus += 0.0025
        elif hit.work == "hard":
            bonus += 0.0005
        elif hit.work == "urgent":
            bonus -= 0.003
        if hit.zh_gist:
            bonus += 0.001
        if q_zh:
            # Overlap with the scene keywords: the enrichment LLM's own
            # vocabulary for this beat, so it beats gist text matching.
            if hit.zh_kw:
                kw_grams = set(cjk_bigrams(hit.zh_kw))
                kw_overlap = len(q_zh & kw_grams)
                if kw_overlap:
                    bonus += min(0.014, 0.005 * kw_overlap)
            # Overlap with the scene gist: a direct topical match.
            if hit.zh_gist:
                overlap = len(q_zh & set(cjk_bigrams(hit.zh_gist)))
                if overlap:
                    bonus += min(0.012, 0.004 * overlap)
            # Overlap with the chapter / event title: naming the chapter in the
            # query is the strongest possible signal short of an exact quote.
            title = (hit.chapter_title_zh or "") + " " + (hit.event_name_zh or "")
            if title.strip():
                t_overlap = len(q_zh & set(cjk_bigrams(title)))
                if t_overlap:
                    bonus += min(0.018, 0.007 * t_overlap)
        if hit.scene_ord == 0:
            bonus += 0.0005
        return bonus

    def _load_hits(self, conn: sqlite3.Connection, ids: Sequence[int]) -> List[StoryHit]:
        """Join chunk + scene + unit rows for the given chunk ids."""
        if not ids:
            return []
        marks = ",".join("?" for _ in ids)
        cast_expr = "s.cast_line" if self._has_cast else "''"
        sql = f"""
            SELECT c.id AS chunk_id, c.scene_id, c.ord AS chunk_ord, c.text_en,
                   c.n_lines,
                   s.ord AS scene_ord, s.scene_title, s.scene_title_zh, s.zh_gist,
                   s.zh_kw,
                   {cast_expr} AS cast_line,
                   s.char_ids, s.speakers, s.unit_id,
                   u.work, u.episode, u.chapter_no, u.chapter_title_en,
                   u.chapter_title_zh, u.event_name, u.event_name_zh,
                   u.page_title, u.url, u.zh_summary
            FROM chunk c
            JOIN scene s ON s.id = c.scene_id
            JOIN unit  u ON u.id = s.unit_id
            WHERE c.id IN ({marks})
        """
        rows = conn.execute(sql, tuple(ids)).fetchall()
        hits: List[StoryHit] = []
        for r in rows:
            hits.append(StoryHit(
                chunk_id=int(r["chunk_id"]),
                scene_id=int(r["scene_id"]),
                unit_id=int(r["unit_id"]),
                score=0.0,
                work=r["work"] or "",
                episode=r["episode"] or "",
                chapter_no=r["chapter_no"],
                chapter_title_en=r["chapter_title_en"] or "",
                chapter_title_zh=r["chapter_title_zh"] or "",
                event_name=r["event_name"] or "",
                event_name_zh=r["event_name_zh"] or "",
                page_title=r["page_title"] or "",
                url=r["url"] or "",
                zh_summary=r["zh_summary"] or "",
                scene_ord=int(r["scene_ord"] or 0),
                scene_title=r["scene_title"] or "",
                scene_title_zh=r["scene_title_zh"] or "",
                zh_gist=r["zh_gist"] or "",
                zh_kw=r["zh_kw"] or "",
                cast=r["cast_line"] or "",
                char_ids=r["char_ids"] or "",
                speakers=r["speakers"] or "",
                ord=int(r["chunk_ord"] or 0),
                text_en=r["text_en"] or "",
                n_lines=int(r["n_lines"] or 0),
            ))
        return hits

    # ── Formatting ───────────────────────────────────────────────

    def format_injection(
        self,
        query: str,
        persona_char_id: str = "",
        persona_name: str = "",
        limit: int = 2,
        max_chars: int = 2200,
        min_score: float = 0.0,
        dialogue_chars: int = 700,
    ) -> str:
        """Build the L4 volatile 剧情记忆 block, or "" when nothing is relevant.

        Auto-injection runs on *every* character-plane turn, so a false positive
        costs tokens and can derail small talk. Two gates:
          1. the query must name something the corpus knows (character, event or
             keyword), or
          2. the lexical match must be exceptionally strong.
        Ordinary chitchat ("今天天气怎么样") matches stray bigrams like 今天/天气
        and must be filtered out — for that, gate 1 is what does the work.
        """
        hits = self.search(
            query, limit=limit, persona_char_id=persona_char_id, min_score=min_score,
        )
        if not hits:
            return ""
        if hits[0].score < _STRONG_SCORE and not self.entity_hit(query):
            return ""
        return self.render_block(
            hits, persona_char_id=persona_char_id, persona_name=persona_name,
            max_chars=max_chars, dialogue_chars=dialogue_chars, with_preamble=True,
        )

    # ── Entity gate ──────────────────────────────────────────────

    def entity_terms(self) -> List[str]:
        """Every Chinese story entity the corpus knows (characters, events, keywords).

        Built once from the DB and memoised. Used only to decide *whether* to
        inject automatically — never to filter explicit tool searches.
        """
        if self._entities is not None:
            return self._entities
        terms: set = set()
        scene_terms: Dict[str, int] = {}
        try:
            conn = self._connect()
            try:
                for row in conn.execute(
                    "SELECT zh_keywords, chapter_title_zh, event_name_zh FROM unit"
                ):
                    for field in row:
                        if not field:
                            continue
                        for part in str(field).replace("，", "、").split("、"):
                            part = part.strip()
                            if (2 <= len(part) <= 12 and not part.isdigit()
                                    and part not in _GENERIC_TERMS):
                                terms.add(part)
                for row in conn.execute("SELECT zh_kw FROM scene"):
                    field = row[0]
                    if not field:
                        continue
                    for part in str(field).replace("，", "、").split("、"):
                        part = part.strip()
                        if 2 <= len(part) <= 12 and not part.isdigit():
                            scene_terms[part] = scene_terms.get(part, 0) + 1
                # Scene keywords are per-beat vocabulary, so a handful are
                # generic nouns (电影/时间/…). A term reused across many scenes
                # is not an entity — keep only the specific ones.
                terms.update(
                    t for t, n in scene_terms.items()
                    if n <= _SCENE_TERM_MAX_DF and t not in _GENERIC_TERMS
                )
            finally:
                conn.close()
        except Exception:  # noqa: BLE001
            terms = set()
        self._entities = sorted(terms, key=len, reverse=True)
        return self._entities

    def entity_hit(self, query: str) -> bool:
        """True when ``query`` mentions a known story entity."""
        q = (query or "").strip()
        if not q:
            return False
        for term in self.entity_terms():
            if term in q:
                return True
        return False

    def source_label(hit: "StoryHit") -> str:
        """兼容旧引用：模块级 source_label 的转发（勿在新代码里用）。"""
        return _source_label(hit)

    def render_block(
        self,
        hits: Sequence[StoryHit],
        persona_char_id: str = "",
        persona_name: str = "",
        max_chars: int = 2200,
        dialogue_chars: int = 700,
        with_preamble: bool = True,
    ) -> str:
        """Render retrieved hits into a token-budgeted context block."""
        if not hits:
            return ""
        who = persona_name or "本角色"
        parts: List[str] = []
        if with_preamble:
            parts.append("## 剧情记忆（原作剧情检索命中）")
            if persona_char_id:
                # In-character framing: these scenes are "her own" memories.
                parts.append(
                    "以下片段检索自《星陨计划》原作剧情，请当作**你自己的亲身记忆**使用：\n"
                    "- 只在与当前话题相关时自然提及，不要朗读原文，"
                    "不要提「知识库/检索/资料」之类的系统用词。\n"
                    "- 标注〔来源：…·游戏原文〕的片段是**中文原文**，可以按其措辞转述；"
                    "标注〔来源：…·wiki〕的是英文原文+中文摘要，只转述不引用原文。\n"
                    f"- 转述时用中文口语，并保持{who}的语气。\n"
                    "- 人名的中文写法**一律**按每段的「本段人物」一行（官方译名），"
                    "不要自己音译。\n"
                    "- 标注「你（本角色）在场」的段落你是当事人，用第一人称；"
                    "标注「你不在场」的按「后来听说」处理。\n"
                    "- 检索可能不完整：把握不大时就用模糊的说法，不要编造细节。"
                )
            else:
                # Neutral framing (assistant): reference material, not memories.
                parts.append(
                    "以下片段检索自《星陨计划》原作剧情库，是这款游戏的设定与剧情原文，"
                    "供你回答玩家问题时引用：\n"
                    "- 只在与当前话题相关时引用，不要大段朗读原文，"
                    "不要提「知识库/检索/资料」之类的系统用词。\n"
                    "- 标注〔来源：…·游戏原文〕的片段是中文原文，可以按其措辞转述；"
                    "标注〔来源：…·wiki〕的是英文原文+中文摘要，只转述不引用原文。\n"
                    "- 向玩家讲述时用中文。\n"
                    "- 人名的中文写法**一律**按每段的「本段人物」一行（官方译名）。\n"
                    "- 你是助手而不是剧中角色：描述这些内容时用第三人称/说书口吻，"
                    "不要用「我亲身经历」的语气。\n"
                    "- 检索可能不完整：把握不大时就说「这段剧情我不太确定」，不要编造细节。"
                )

        budget = max(400, int(max_chars))
        used = 0
        emitted = 0
        for i, hit in enumerate(hits, 1):
            body: List[str] = [f"【{i}】{hit.label(persona_char_id)} {_source_label(hit)}"]
            if hit.zh_gist:
                body.append(f"梗概：{hit.zh_gist}")
            elif hit.zh_summary:
                body.append(f"单元梗概：{hit.zh_summary}")
            if hit.cast:
                body.append(f"本段人物：{hit.cast}")
            dialogue = self._trim_dialogue(hit.text_en, dialogue_chars)
            if dialogue:
                body.append("片段：\n" + dialogue)
            block = "\n".join(body)
            if used + len(block) > budget and emitted > 0:
                break
            parts.append(block)
            used += len(block)
            emitted += 1
        return "\n\n".join(parts) if emitted else ""

    def format_tool_result(
        self,
        query: str,
        limit: int = 4,
        persona_char_id: str = "",
        detail: int = 1400,
    ) -> str:
        """Build the ``story_search`` tool response (deeper than auto injection)."""
        hits = self.search(query, limit=limit, persona_char_id=persona_char_id)
        if not hits:
            return (
                "[story_search] 原作剧情库中没有找到相关内容。\n"
                "可以换用关键角色名、事件名（如「第一次方舟战争」）或章节号重试。"
            )
        blocks: List[str] = [
            f"[story_search] 命中 {len(hits)} 段原作剧情（台词为英文原文，转述时译成中文）："
        ]
        for i, hit in enumerate(hits, 1):
            lines = [f"【{i}】{hit.label(persona_char_id)}"]
            if hit.zh_summary and i == 1:
                lines.append(f"单元梗概：{hit.zh_summary}")
            if hit.zh_gist:
                lines.append(f"本段梗概：{hit.zh_gist}")
            if hit.cast:
                lines.append(f"本段人物（官方译名）：{hit.cast}")
            lines.append("对白：\n" + self._trim_dialogue(hit.text_en, detail))
            if hit.url:
                lines.append(f"来源：{hit.url}")
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)

    # ── Internals ────────────────────────────────────────────────

    @staticmethod
    def _trim_dialogue(text: str, limit: int) -> str:
        """Trim a dialogue block to ``limit`` chars on a line boundary."""
        text = (text or "").strip()
        if len(text) <= limit:
            return text
        cut = text[:limit]
        nl = cut.rfind("\n")
        if nl > limit // 3:
            cut = cut[:nl]
        return cut.rstrip() + "\n…（后略）"


# ── Module-level singleton ─────────────────────────────────────────

_story_kb: Optional[StoryKB] = None
_singleton_lock = threading.Lock()


def get_story_kb(db_path: Optional[str] = None) -> StoryKB:
    """Get (or lazily create) the process-wide StoryKB instance."""
    global _story_kb
    if db_path is not None:
        return StoryKB(db_path)
    if _story_kb is None:
        with _singleton_lock:
            if _story_kb is None:
                _story_kb = StoryKB()
    return _story_kb


# ── Persona → character id resolution ─────────────────────────────

_PERSONA_CHAR_CACHE: Dict[str, str] = {}
_PERSONA_CHAR_TS = 0.0
_PERSONA_CHAR_TTL = 300.0
_members_lock = threading.Lock()


def _load_member_aliases() -> Dict[str, str]:
    """Return {alias_lower: character_id} from the vendored members_info.json."""
    path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..", "config", "characters", "members_info.json",
    )
    aliases: Dict[str, str] = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            members = json.load(f)
    except Exception:  # noqa: BLE001
        return aliases
    for m in members or []:
        cid = m.get("id") or ""
        if not cid:
            continue
        for name in [m.get("name"), m.get("e7_name")] + list(m.get("other_names") or []):
            if isinstance(name, str) and name.strip():
                aliases.setdefault(name.strip().lower(), cid)
    return aliases


def persona_char_id(persona_key: str, display_name: str = "") -> str:
    """Resolve a persona key/display name to an Ark Re:Code character id.

    ``"rubi"`` → ``"H076"`` (via 露比 / Rubi). Returns "" when unknown.
    """
    global _PERSONA_CHAR_TS
    key = (persona_key or "").strip()
    disp = (display_name or "").strip()
    if not key and not disp:
        return ""

    now = time.time()
    with _members_lock:
        if not _PERSONA_CHAR_CACHE or (now - _PERSONA_CHAR_TS) > _PERSONA_CHAR_TTL:
            _PERSONA_CHAR_CACHE.clear()
            _PERSONA_CHAR_CACHE.update(_load_member_aliases())
            _PERSONA_CHAR_TS = now

    for cand in (disp, key):
        if not cand:
            continue
        low = cand.lower()
        if low in _PERSONA_CHAR_CACHE:
            return _PERSONA_CHAR_CACHE[low]
        # Strip a trailing parenthetical: "露比 (Rubi)" → "露比"
        stripped = re.sub(r"\s*[（(][^)）]*[)）]\s*$", "", cand).strip()
        if stripped and stripped.lower() in _PERSONA_CHAR_CACHE:
            return _PERSONA_CHAR_CACHE[stripped.lower()]
    return ""

"""多剧情库融合层（wiki 库 + gamedata 库 → 一条排序结果）。

为什么不合并成一个 SQLite：

* **重建频率不同** —— 游戏侧每 14 天更新（活动归档），wiki 侧要爬取 + LLM 摘要，
  合并会把高频重建绑到低频重建上；
* **schema 语义不同** —— gamedata 的 bond/event 行没有 episode/cast_line/chapter_no，
  而 `StoryKB._bonus()` 的排序加成正是看这些字段，混表会让权重算不准；
* **爆炸半径** —— 一次坏的 gamedata 构建不该能弄坏 wiki 检索。

这里只做薄融合，不复制任何检索逻辑：

* 每个库各取 top-K → **RRF（Reciprocal Rank Fusion）** 融合 → 按内容哈希去重
  （同内容优先保留高优先级库的版本，即 gamedata 原文）→ 交给现成的
  `StoryKB.render_block()` 做预算裁剪与渲染。
* `entity_terms()` 取并集，供 `format_injection()` 的自动注入闸门使用。
"""

from __future__ import annotations

import hashlib
import os
import re
from typing import Iterable, List, Sequence

from .story_kb import StoryKB, data_root, default_db_path

RRF_K = 60            # RRF 常量：越大越平滑，60 是常用默认
PER_DB_FACTOR = 4     # 每个库取 limit*factor 条参与融合
MIN_PER_DB = 6


def _gd_candidates() -> List[str]:
    """gamedata 库的候选路径（优先数据盘，其次仓库内）。"""
    out: List[str] = []
    env = (os.environ.get("STORY_GDATA_DB") or "").strip()
    if env:
        out.append(env)
    gd = (os.environ.get("GAMEDATA_DIR") or "").strip()
    if gd:
        out.append(os.path.join(gd, "story", "story_gd.db"))
    root = data_root()
    out.append(os.path.join(root, "story", "story_gd.db"))
    out.append(os.path.join(os.path.dirname(default_db_path()), "story_gd.db"))
    return out


def discover_dbs() -> List[tuple[str, str]]:
    """[(source_name, db_path)]，按优先级排序；只返回真实存在的文件。"""
    found: List[tuple[str, str]] = []
    gd = next((p for p in _gd_candidates() if os.path.isfile(p)), "")
    if gd:
        found.append(("gamedata", gd))
    wiki = default_db_path()
    if os.path.isfile(wiki) and os.path.abspath(wiki) != os.path.abspath(gd):
        found.append(("wiki", wiki))
    return found


def _content_hash(hit) -> str:
    """内容指纹：用于跨库去重（同一段剧情两边各有一份）。"""
    text = re.sub(r"\s+", "", getattr(hit, "text_en", "") or "")
    return hashlib.md5(text[:800].encode("utf-8")).hexdigest()


class StorySources:
    """多个 :class:`StoryKB` 的门面，接口与单个 KB 保持一致。"""

    def __init__(self, dbs: Sequence[tuple[str, str]] | None = None):
        self.sources: List[tuple[str, StoryKB]] = []
        for name, path in (dbs if dbs is not None else discover_dbs()):
            kb = StoryKB(path)
            if kb.available:
                self.sources.append((name, kb))

    # ── 基本状态 ──
    def __bool__(self) -> bool:
        return bool(self.sources)

    @property
    def available(self) -> bool:
        return bool(self.sources)

    def stats(self) -> dict:
        return {name: kb.stats() for name, kb in self.sources}

    # ── 检索：每库 top-K → RRF → 去重 ──
    def search(self, query: str, limit: int = 3,
               persona_char_id: str = "", min_score: float = 0.0):
        per_db = max(MIN_PER_DB, limit * PER_DB_FACTOR)
        lists: List[list] = []
        for _name, kb in self.sources:
            try:
                lists.append(kb.search(query, limit=per_db,
                                       persona_char_id=persona_char_id,
                                       min_score=min_score))
            except Exception:  # noqa: BLE001
                lists.append([])

        fused: dict[str, dict] = {}
        for name, hits in zip([n for n, _ in self.sources], lists):
            for rank, hit in enumerate(hits):
                key = _content_hash(hit)
                slot = fused.setdefault(key, {"rrf": 0.0, "hit": None,
                                              "source": name, "best_rank": rank})
                slot["rrf"] += 1.0 / (RRF_K + rank + 1)
                # 同内容优先保留高优先级库（sources 已按 gamedata 在前排序）
                if slot["hit"] is None:
                    slot["hit"] = hit
                    slot["source"] = name
                if rank < slot["best_rank"]:
                    slot["best_rank"] = rank

        ordered = sorted(fused.values(), key=lambda s: (-s["rrf"], s["best_rank"]))
        out = []
        for slot in ordered[:limit]:
            hit = slot["hit"]
            try:
                object.__setattr__(hit, "source_db", slot["source"])
            except Exception:  # noqa: BLE001
                pass
            out.append(hit)
        return out

    # ── 实体闸门：并集 ──
    def entity_terms(self) -> List[str]:
        terms: List[str] = []
        seen: set = set()
        for _name, kb in self.sources:
            try:
                for t in kb.entity_terms():
                    if t and t not in seen:
                        seen.add(t)
                        terms.append(t)
            except Exception:  # noqa: BLE001
                continue
        return terms

    def entity_hit(self, query: str) -> bool:
        return any(kb.entity_hit(query) for _n, kb in self.sources)

    # ── 渲染 / 注入：复用首个库的实现（各库同 schema 同代码）──
    def render_block(self, hits, **kwargs) -> str:
        if not self.sources:
            return ""
        return self.sources[0][1].render_block(hits, **kwargs)

    def format_injection(self, query: str, **kwargs) -> str:
        if not self.sources:
            return ""
        hits = self.search(query, limit=kwargs.get("limit", 2),
                           persona_char_id=kwargs.get("persona_char_id", ""),
                           min_score=kwargs.get("min_score", 0.0))
        if not hits:
            return ""
        return self.render_block(hits, **kwargs)

    def format_tool_result(self, query: str, **kwargs) -> str:
        if not self.sources:
            return ""
        hits = self.search(query, limit=kwargs.get("limit", 5),
                           persona_char_id=kwargs.get("persona_char_id", ""),
                           min_score=kwargs.get("min_score", 0.0))
        kb = self.sources[0][1]
        return kb.format_tool_result(query, hits, **kwargs)


def get_story_sources(dbs: Sequence[tuple[str, str]] | None = None) -> StorySources:
    """门面单例（缓存）。"""
    global _CACHE
    key = tuple(dbs) if dbs is not None else None
    if _CACHE.get("key") == key and _CACHE.get("obj") is not None:
        return _CACHE["obj"]
    obj = StorySources(dbs)
    _CACHE.update({"key": key, "obj": obj})
    return obj


_CACHE: dict = {}

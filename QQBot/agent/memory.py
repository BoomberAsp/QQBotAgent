"""
Memory System — Long-term persistent memory for the agent.

Memory types:
- user: Per-user facts, preferences, interaction summaries (scoped to user_id)
- knowledge: Agent-learned information (shared across users)
- system: Agent self-reflection and configuration history (shared across users)

Memory is stored as markdown files with frontmatter, with an index in MEMORY.md.
User-type memories are isolated: stored in per-user subdirectories and only
returned when the matching user_id is provided.
"""

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class MemoryEntry:
    """A single memory entry."""

    name: str
    description: str
    type: str  # user, knowledge, system
    content: str
    user_id: Optional[str] = None  # Owner user_id (required for user-type memories)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)


class MemorySystem:
    """File-based long-term memory system with per-user isolation.

    User-type memories are stored in {base_dir}/user/{user_id}/ subdirectories
    and are only returned when queried with the matching user_id. Knowledge
    and system memories are stored globally and shared across users.
    """

    def __init__(self, base_dir: str):
        self.base_dir = base_dir
        self.index_path = os.path.join(base_dir, "MEMORY.md")
        os.makedirs(base_dir, exist_ok=True)
        self._ensure_index()

    def _ensure_index(self):
        if not os.path.exists(self.index_path):
            with open(self.index_path, "w", encoding="utf-8") as f:
                f.write(
                    "# Memory Index\n\n"
                    "## User Memories\n\n"
                    "## Knowledge Memories\n\n"
                    "## System Memories\n\n"
                )

    # ── Path resolution ───────────────────────────────────────────

    def _get_storage_dir(self, mem_type: str, user_id: str = None) -> str:
        """Get the storage directory for a memory type.

        User memories go to {base_dir}/user/{user_id}/ for isolation.
        Knowledge and system memories go to {base_dir}/{type}/ (flat, shared).
        """
        if mem_type == "user" and user_id:
            dir_path = os.path.join(self.base_dir, "user", self._safe_id(user_id))
        else:
            dir_path = os.path.join(self.base_dir, mem_type)
        os.makedirs(dir_path, exist_ok=True)
        return dir_path

    @staticmethod
    def _safe_id(id_str: str) -> str:
        """Sanitize an ID for use as a directory name."""
        return "".join(c if c.isalnum() or c in "-_" else "_" for c in id_str)

    # ── CRUD ──────────────────────────────────────────────────────

    def save(self, entry: MemoryEntry) -> str:
        """Save a memory entry. Returns the file path.

        User-type memories are stored in a per-user subdirectory.
        """
        user_id = entry.user_id if entry.type == "user" else None
        type_dir = self._get_storage_dir(entry.type, user_id)

        filename = self._sanitize_filename(entry.name) + ".md"
        filepath = os.path.join(type_dir, filename)

        # Include user_id in frontmatter for user-type memories
        user_id_line = f"user_id: {entry.user_id}\n" if entry.user_id else ""

        content = (
            f"---\n"
            f"name: {entry.name}\n"
            f"description: {entry.description}\n"
            f"type: {entry.type}\n"
            f"{user_id_line}"
            f"created_at: {entry.created_at}\n"
            f"updated_at: {entry.updated_at}\n"
            f"---\n\n"
            f"{entry.content}\n"
        )

        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)

        self._update_index(entry, filepath)
        return filepath

    def recall(self, name: str, mem_type: str = None, user_id: str = None) -> Optional[MemoryEntry]:
        """Recall a memory by name, optionally filtered by type and user_id.

        For user-type memories, user_id is required to find the memory.
        """
        search_dirs = self._get_search_dirs(mem_type, user_id)
        for search_dir in search_dirs:
            filename = self._sanitize_filename(name) + ".md"
            filepath = os.path.join(search_dir, filename)
            if os.path.exists(filepath):
                return self._load_file(filepath)
        return None

    def forget(self, name: str, mem_type: str = None, user_id: str = None) -> bool:
        """Delete a memory by name. For user memories, user_id scopes the deletion."""
        search_dirs = self._get_search_dirs(mem_type, user_id)
        for search_dir in search_dirs:
            filename = self._sanitize_filename(name) + ".md"
            filepath = os.path.join(search_dir, filename)
            if os.path.exists(filepath):
                os.remove(filepath)
                self._remove_from_index(name)
                return True
        return False

    def search(self, query: str, mem_type: str = None, user_id: str = None) -> List[MemoryEntry]:
        """Search memories by content keyword. Simple, not semantic.

        User-type memories are only returned when user_id matches.
        Knowledge and system memories are always included (they're shared).
        """
        results = []
        search_dirs = self._get_search_dirs(mem_type, user_id)
        for search_dir in search_dirs:
            if not os.path.exists(search_dir):
                continue
            for filename in os.listdir(search_dir):
                if filename.endswith(".md") and filename != "MEMORY.md":
                    filepath = os.path.join(search_dir, filename)
                    entry = self._load_file(filepath)
                    if entry and query.lower() in entry.content.lower():
                        results.append(entry)
        return results

    def list_all(self, mem_type: str = None, user_id: str = None) -> List[MemoryEntry]:
        """List all memories, optionally filtered by type and user_id."""
        results = []
        search_dirs = self._get_search_dirs(mem_type, user_id)
        for search_dir in search_dirs:
            if not os.path.exists(search_dir):
                continue
            for filename in os.listdir(search_dir):
                if filename.endswith(".md") and filename != "MEMORY.md":
                    filepath = os.path.join(search_dir, filename)
                    entry = self._load_file(filepath)
                    if entry:
                        results.append(entry)
        return results

    # ── Search directory resolution ───────────────────────────────

    def _get_search_dirs(self, mem_type: str, user_id: str) -> List[str]:
        """Get the list of directories to search based on type and user_id.

        - Specific type provided: return directories for that type only.
          For user type, scoped to the given user_id.
        - No type: return all applicable directories:
          - knowledge/ and system/ (shared, always included)
          - user/{user_id}/ if user_id is given (scoped isolation)
        """
        if mem_type:
            if mem_type == "user":
                if user_id:
                    return [self._get_storage_dir("user", user_id)]
                else:
                    # No user_id — search ALL user subdirectories (admin/debug use)
                    user_base = os.path.join(self.base_dir, "user")
                    if os.path.exists(user_base):
                        return [
                            os.path.join(user_base, d)
                            for d in os.listdir(user_base)
                            if os.path.isdir(os.path.join(user_base, d))
                        ]
                    return []
            else:
                return [self._get_storage_dir(mem_type)]
        else:
            # All types
            dirs = [
                self._get_storage_dir("knowledge"),
                self._get_storage_dir("system"),
            ]
            if user_id:
                dirs.append(self._get_storage_dir("user", user_id))
            else:
                # No user_id — include all user subdirectories
                user_base = os.path.join(self.base_dir, "user")
                if os.path.exists(user_base):
                    for d in os.listdir(user_base):
                        d_path = os.path.join(user_base, d)
                        if os.path.isdir(d_path):
                            dirs.append(d_path)
            return dirs

    # ── Helpers ───────────────────────────────────────────────────

    def _load_file(self, filepath: str) -> Optional[MemoryEntry]:
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                content = f.read()
            # Parse frontmatter
            if content.startswith("---"):
                parts = content.split("---", 2)
                if len(parts) >= 3:
                    meta = {}
                    for line in parts[1].strip().split("\n"):
                        if ":" in line:
                            k, v = line.split(":", 1)
                            meta[k.strip()] = v.strip()
                    return MemoryEntry(
                        name=meta.get("name", ""),
                        description=meta.get("description", ""),
                        type=meta.get("type", "knowledge"),
                        user_id=meta.get("user_id"),
                        content=parts[2].strip(),
                        created_at=float(meta.get("created_at", time.time())),
                        updated_at=float(meta.get("updated_at", time.time())),
                    )
        except Exception:
            pass
        return None

    def _update_index(self, entry: MemoryEntry, filepath: str):
        """Add entry pointer to MEMORY.md index."""
        try:
            with open(self.index_path, "r", encoding="utf-8") as f:
                index_content = f.read()

            rel_path = os.path.relpath(filepath, self.base_dir)
            entry_line = f"- [{entry.name}]({rel_path}) — {entry.description}\n"

            section_marker = {
                "user": "## User Memories",
                "knowledge": "## Knowledge Memories",
                "system": "## System Memories",
            }.get(entry.type, "## Knowledge Memories")

            if entry_line.strip() not in index_content:
                # Insert after section marker
                marker_pos = index_content.find(section_marker)
                if marker_pos != -1:
                    next_section = index_content.find("##", marker_pos + len(section_marker))
                    if next_section == -1:
                        next_section = len(index_content)
                    # Find end of section marker line
                    line_end = index_content.find("\n", marker_pos) + 1
                    new_content = (
                        index_content[:line_end]
                        + entry_line
                        + index_content[line_end:]
                    )
                    with open(self.index_path, "w", encoding="utf-8") as f:
                        f.write(new_content)
        except Exception:
            pass

    def _remove_from_index(self, name: str):
        """Remove entry from MEMORY.md index."""
        try:
            with open(self.index_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            lines = [l for l in lines if not l.startswith(f"- [{name}]")]
            with open(self.index_path, "w", encoding="utf-8") as f:
                f.writelines(lines)
        except Exception:
            pass

    @staticmethod
    def _sanitize_filename(name: str) -> str:
        return "".join(c if c.isalnum() or c in "-_" else "_" for c in name)


# ═══════════════════════════════════════════════════════════════════════════
# P1 — Three-Tier Memory Engine (Profile-Fact-Extraction-Plan.md §7)
# ═══════════════════════════════════════════════════════════════════════════
#
# Replaces agent._maybe_remember's raw-dump "long-term memory" with a
# confidence-graded three-tier store:
#
#   SHORT  — bounded list (cap SHORT_MAX). New facts enter at count=1, are NOT
#            injected into the prompt, and promote to MEDIUM at count>=3.
#   MEDIUM — count>=3 refined facts. ACTIVELY injected into the system prompt.
#            age = extraction_count - entry_extraction_index; demote back to
#            SHORT at age>MEDIUM_DOWNGRADE_AGE; promote to LONG at
#            count>MEDIUM_LONG_THRESHOLD.
#   LONG   — snapshot + provenance. P1 was create + persist only; P2-lite
#            (Memory-Optimization-Plan ①) added one-line index injection
#            (build_long_index) + the recall_memory query tool (recall_long),
#            fixing the dead-end where a demoted MEDIUM twin made the LONG
#            copy unreachable. Cap demotion remains P2 (§11).
#
# A per-user `extraction_count` logical clock drives age. It is incremented once
# per SUCCESSFUL extract_batch (ProfileManager calls touch_extraction_count).
#
# Concurrency (§12.1): per-user single-flight is enforced by ProfileManager; all
# LLM awaits complete BEFORE the synchronous atomic apply. Every TieredMemory
# method here is synchronous and never awaits, so calls cannot interleave under
# asyncio — no locks needed.
#
# Coexists with MemorySystem above (which still serves knowledge/system types);
# MemorySystem is retired in P3 when config/MEMORY.md is rewritten (Decision K).

# ── Three-tier engine constants (Profile-Fact-Extraction-Plan §7.7) ──
SHORT_MAX = 50
SHORT_PROMOTE_AT_COUNT = 3
SHORT_PRIORITY_STEP = 10
MEDIUM_LONG_THRESHOLD = 10
MEDIUM_DOWNGRADE_AGE = 30
MEDIUM_DOWNGRADE_COUNT_RESET = 2
MEDIUM_MAX = 60
MEDIUM_INJECT_TOP_N = 15            # 12 top-by-count + 3 recent-promotions
MEDIUM_INJECT_RECENT_SLOTS = 3
MEDIUM_INJECT_TOKEN_CAP = 600       # chars
LOW_CONFIDENCE_THRESHOLD = 5        # count < 5 → "(低置信)" label
JUDGE_MEDIUM_LIST_CAP = 40

# ── LONG-tier index + recall constants (Memory-Optimization-Plan ①) ──
LONG_INJECT_MAX_LINES = 20          # index lines injected into the L4 tail
LONG_RECALL_MAX_MATCHES = 3         # entries returned per recall_memory call
LONG_RECALL_SNAPSHOT_CAP = 1200     # chars of conversation snapshot per entry
LONG_RECALL_QUERY_LOG_CAP = 20      # query_log entries kept per LONG object

# ── Ingest-level deterministic guards (Memory-Optimization-Plan ⑨) ──
# Both fire ONLY on the judge="new" path inside _add_short, after the exact-dup
# guard — a deterministic backstop for the two failure classes L2 exposed as
# stable across runs:
#   M2: explicit corrections the LLM judged "new" → the stale old value survived
#       alongside the corrected one (all 6 persona ct3 facts failed, both versions)
#   M1: repeated junk re-phrasings piling up as parallel SHORT items that each
#       independently promote to MEDIUM (p2's 南京-work triple)
# Deliberately narrow: 已经/现在/其实 are NOT correction markers — compatible
# restatements must not clobber existing facts; routing those stays the LLM
# judge's job (update/keep). False-positive guards live in test_agent.py
# (花生过敏 vs 海鲜过敏 must coexist; 已经在玩原神五年 must not replace 每天肝原神).
CORRECTION_MARKERS = ("不是", "而非", "不再", "说错", "纠正", "记错", "收回",
                      "改成", "改为", "换成", "搬到", "搬回", "转行", "戒",
                      "停用", "改口")
NEARDUP_MERGE_DICE = 0.6            # char-bigram Dice ≥ this → same fact, re-phrased
NEARDUP_MERGE_OVERLAP = 0.6         # OR containment ≥ this (a short variant of a
                                    # longer embellished entry — Dice alone dilutes:
                                    # 今年三十五岁 vs 今年三十岁,刚过完生日 = 0.43
                                    # Dice but 0.60 overlap). Measured guard pairs
                                    # stay ≤ 0.33: 花生/海鲜过敏 0.22, 杭州/南京
                                    # 0.25→(0.33 for the bare pair), 原神五年/
                                    # 每天肝日常 0.33, 咖啡/茶 0.17, 女儿幼儿园/
                                    # 升小学 0.13.
STALE_VARIANT_OVERLAP = 0.30        # ⑨ M2/M2b eligibility bar (Dice OR containment,
                                    # PLUS ≥1 strong shared bigram — see
                                    # _is_stale_variant). The first L2 batch-2 run
                                    # used "any distinctive shared bigram" and
                                    # over-fired badly: p4's 游戏-topic correction
                                    # swept 第一款游戏亏损三万块 (shares only 游戏,
                                    # 0.09) and every salient episode; p2's 升小学
                                    # correction replaced the FUSED episode entry
                                    # 有一个上幼儿园的女儿,曾因女儿演出感动落泪
                                    # (0.18), erasing the canary. Measured: true
                                    # variants score ≥ 0.30 (住在杭州/家住杭州 0.33,
                                    # p4 ct1 外包 correction 0.31, 独生女 chain
                                    # 0.5-1.0, 会计专业 0.7, 教数学 containment 0.5,
                                    # 氪金 containment 0.63); harmful pairs stay
                                    # ≤ 0.25 (fused-episode 0.18, 会计证考过了 0.25,
                                    # 花生/海鲜 0.22, topic-word-only 0.09-0.15,
                                    # 住在杭州/住在南京 rejected by the strong-bigram
                                    # gate — their only share 住在 is stop-mixed).
# Chars so generic that a bigram made of them carries no topic identity
# (用户/每天/喜欢/的是…). A bigram is STRONG when BOTH chars are outside this set
# (会计 strong even though 会 is generic; 住在/喜欢 stop-mixed → weak): weak
# shares mark sentence-pattern overlap, not same-fact identity (see
# _is_stale_variant — 住在杭州 vs 住在南京 share only 住在 and must not match).
_BIGRAM_STOP_CHARS = frozenset("用户是我他她它的在了和与有都很非常就还也就都这那个一每天会喜欢说讲")

TIER_SCHEMA_VERSION = 1


def _new_id() -> str:
    """Stable-enough short id for tier items (uuid4 hex[:12])."""
    return uuid.uuid4().hex[:12]


# ── ⑨ bigram helpers (pure stdlib, deterministic — safe on the sync ingest path) ──

def _norm_bigram_text(text: str) -> str:
    """Normalize for bigram comparison: drop a leading 用户, lowercase, keep alnum."""
    t = (text or "").strip().lower()
    if t.startswith("用户"):
        t = t[2:]
    return "".join(ch for ch in t if ch.isalnum())


def _char_bigrams(text: str) -> set:
    t = _norm_bigram_text(text)
    if len(t) < 2:
        return {t} if t else set()
    return {t[i:i + 2] for i in range(len(t) - 1)}


def _bigram_dice(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return 2.0 * len(a & b) / (len(a) + len(b))


def _digit_signature(text: str) -> str:
    """ASCII digit string of a fact, or "" when it carries none."""
    return "".join(ch for ch in (text or "") if ch.isdigit())


def _is_near_dup(a: set, b: set) -> bool:
    """⑨ M1 criterion: Dice ≥ NEARDUP_MERGE_DICE OR containment ≥ NEARDUP_MERGE_OVERLAP.

    Containment rescues the short-variant-vs-embellished-entry case where Dice
    is diluted by the longer tail (measured on L2 baseline p3-ct3 data:
    0.46 Dice / 0.75 overlap). Guard pairs stay far below both bars
    (花生/海鲜过敏 0.25 Dice / 0.25 overlap; 杭州/南京 0.25/0.25;
    原神五年/每天肝日常 0.14/0.40).
    """
    if not a or not b:
        return False
    inter = len(a & b)
    if inter == 0:
        return False
    return (2.0 * inter / (len(a) + len(b)) >= NEARDUP_MERGE_DICE
            or inter / min(len(a), len(b)) >= NEARDUP_MERGE_OVERLAP)


def _is_stale_variant(a: set, b: set) -> bool:
    """⑨ M2/M2b eligibility: a and b look like two wordings of the SAME fact.

    Two gates, BOTH required:
      - ≥1 STRONG shared bigram (neither char in _BIGRAM_STOP_CHARS). A
        stop-mixed share like 住在/喜欢/每天 carries a generic pattern, not a
        topic: 住在杭州 vs 住在南京 share only 住在 (Dice 0.33) yet are different
        facts, while every measured genuine variant pair shares a strong
        bigram (杭州, 独生, 会计, 外包, 3号…).
      - Dice ≥ STALE_VARIANT_OVERLAP OR containment ≥ STALE_VARIANT_OVERLAP.
        The weaker first-run bar ("any distinctive shared bigram") let a topic
        word (游戏/儿子) alone qualify unrelated facts — measured over-fire in
        the L2 batch-2 run (see the STALE_VARIANT_OVERLAP comment). Containment
        (inter/min) keeps SHORT atomic facts eligible (教数学 vs 教的是语文不是
        数学: Dice 0.125 but containment 0.5) while fused episode entries stay
        protected (their unique 演出/感动/落泪 tail dilutes both ratios: 0.18).
    """
    if not a or not b:
        return False
    shared = a & b
    inter = len(shared)
    if inter == 0:
        return False
    if not any(bg[0] not in _BIGRAM_STOP_CHARS and bg[1] not in _BIGRAM_STOP_CHARS
               for bg in shared):
        return False
    return (2.0 * inter / (len(a) + len(b)) >= STALE_VARIANT_OVERLAP
            or inter / min(len(a), len(b)) >= STALE_VARIANT_OVERLAP)


@dataclass
class ShortItem:
    """A SHORT-tier candidate. count is 1 or 2; promotes to MEDIUM at 3."""
    id: str
    content: str
    count: int = 1


@dataclass
class MediumItem:
    """A MEDIUM-tier refined fact. count>=3. age = clock - entry_extraction_index."""
    id: str
    content: str
    count: int
    entry_extraction_index: int


@dataclass
class LongObject:
    """A LONG-tier snapshot. P2-lite: indexed into the prompt + recall_long()."""
    id: str
    title: str
    summary: str
    snapshot_turns: List[Tuple[str, str]]
    snapshot_time: float
    query_count: int = 0                                  # bumped by recall_long
    query_log: List[Any] = field(default_factory=list)    # capped recall audit trail
    linked_medium_id: str = ""                            # back-ref to MEDIUM twin (Decision G: twin kept)


@dataclass
class UserTierState:
    """Per-user container for all three tiers + the logical clock."""
    user_id: str
    extraction_count: int = 0
    short: List[ShortItem] = field(default_factory=list)    # index 0 = front, -1 = eviction
    medium: List[MediumItem] = field(default_factory=list)  # plain list, sorted on demand (§12.1: heap is YAGNI)
    long: List[LongObject] = field(default_factory=list)


class TieredMemory:
    """Three-tier memory engine (P1: SHORT/MEDIUM active, LONG create-only).

    base_dir is the SAME directory the old MemorySystem uses ({data_root}/memory);
    TieredMemory owns a `tiers/` subdirectory there, so the two stores never
    collide and rollback is trivial (delete tiers/).
    """

    def __init__(self, base_dir: str):
        self.base_dir = base_dir
        self.tiers_dir = os.path.join(base_dir, "tiers")
        self.long_dir = os.path.join(self.tiers_dir, "long")
        os.makedirs(self.tiers_dir, exist_ok=True)
        self._states: Dict[str, UserTierState] = {}

    # ── path helpers ──────────────────────────────────────────────

    @staticmethod
    def _safe_id(id_str: str) -> str:
        return "".join(c if c.isalnum() or c in "-_" else "_" for c in id_str)

    def _tier_path(self, user_id: str) -> str:
        return os.path.join(self.tiers_dir, self._safe_id(user_id) + ".json")

    def _long_user_dir(self, user_id: str) -> str:
        return os.path.join(self.long_dir, self._safe_id(user_id))

    # ── state access ──────────────────────────────────────────────

    def _get_state(self, user_id: str) -> UserTierState:
        st = self._states.get(user_id)
        if st is None:
            st = self.load_user(user_id)
        return st

    def _find_short(self, st: UserTierState, item_id: Optional[str]) -> Optional[ShortItem]:
        if not item_id:
            return None
        for s in st.short:
            if s.id == item_id:
                return s
        return None

    def _find_medium(self, st: UserTierState, item_id: Optional[str]) -> Optional[MediumItem]:
        if not item_id:
            return None
        for m in st.medium:
            if m.id == item_id:
                return m
        return None

    # ── logical clock ─────────────────────────────────────────────

    def touch_extraction_count(self, user_id: str) -> int:
        """Increment and return the user's extraction_count (once per successful batch)."""
        st = self._get_state(user_id)
        st.extraction_count += 1
        return st.extraction_count

    # ── judge list (for the merged extract+judge prompt) ──────────

    def get_judge_list(self, user_id: str) -> List[dict]:
        """MEDIUM (top JUDGE_MEDIUM_LIST_CAP by count) + SHORT (all), for the LLM judge.

        Each dict: {"id", "content", "count", "tier"}. The LLM returns one of
        new / reinforce_short / update / keep with matched_id referencing these.
        """
        st = self._get_state(user_id)
        out: List[dict] = []
        for m in sorted(st.medium, key=lambda x: -x.count)[:JUDGE_MEDIUM_LIST_CAP]:
            out.append({"id": m.id, "content": m.content, "count": m.count, "tier": "medium"})
        for s in st.short:
            out.append({"id": s.id, "content": s.content, "count": s.count, "tier": "short"})
        return out

    # ── core ingestion (synchronous state machine) ────────────────

    def ingest_candidates(
        self,
        user_id: str,
        candidates: List[Any],
        snapshot_turns: Optional[List[Tuple[str, str]]] = None,
    ) -> dict:
        """Route filtered candidates into the tier state machine. Synchronous, no await.

        Args:
            candidates: list of (content, judge, matched_id) tuples OR dicts with
                keys content/judge/matched_id. judge ∈ {new, reinforce_short,
                update, keep}. An invalid/missing matched_id falls back to "new".
            snapshot_turns: the K turns of this batch, stored on any LONG object
                created during this ingestion (Decision: snapshot = current batch).

        Returns a summary dict for observability logging.
        """
        st = self._get_state(user_id)
        turns = list(snapshot_turns or [])
        summary = {
            "new_short": 0, "reinforced_short": 0, "promoted_to_medium": 0,
            "reinforced_medium": 0, "promoted_to_long": 0, "demoted_to_short": 0,
            "corrected_in_place": 0, "near_dup_merged": 0,   # ⑨ guard hits
            "stale_swept": 0,                                # ⑨ M2b sibling drops
        }

        for cand in candidates:
            content, judge, matched_id = self._unpack_candidate(cand)
            content = (content or "").strip()[:300]
            if not content:
                continue
            judge = (judge or "new").strip().lower()

            if judge == "reinforce_short":
                item = self._find_short(st, matched_id)
                if item is None:
                    self._add_short(st, content, turns, summary)   # fallback: hallucinated id
                else:
                    self._reinforce_short_item(st, item, turns, summary)
            elif judge in ("update", "keep"):
                med = self._find_medium(st, matched_id)
                if med is None:
                    self._add_short(st, content, turns, summary)   # fallback: hallucinated id
                else:
                    if judge == "update":
                        med.content = content                      # LLM-refined version
                    self._reinforce_medium_item(st, med, turns, summary)
            else:
                # "new" (or any unknown judge value) → enter SHORT at count=1
                self._add_short(st, content, turns, summary)

        self._run_demotions(st, summary)
        return summary

    @staticmethod
    def _unpack_candidate(cand: Any) -> Tuple[str, Optional[str], Optional[str]]:
        """Accept either a (content, judge, matched_id) tuple or a dict."""
        if isinstance(cand, dict):
            return cand.get("content", ""), cand.get("judge"), cand.get("matched_id")
        try:
            content, judge, matched_id = cand
            return content, judge, matched_id
        except (ValueError, TypeError):
            return "", None, None

    def _add_short(self, st: UserTierState, content: str, turns, summary: dict):
        """Insert a new SHORT item at the front, with three deterministic guards.

        All guards are defense-in-depth against an LLM judge that lazily marks
        everything "new" (Risk: judge-quality degradation). Deep-semantic
        routing stays the judge's job (it sees SHORT/MEDIUM in its list); here
        we only fire on unambiguous surface signals, in order:
          1. exact dup (verbatim)         → reinforce the existing item
          2. ⑨ M2 correction replacement  → replace the corrected item in place
          3. ⑨ M1 near-dup merge (Dice)   → reinforce the existing item
          4. otherwise                    → new SHORT item at count=1
        """
        cl = content.strip().lower()
        for m in st.medium:
            if m.content.strip().lower() == cl:
                self._reinforce_medium_item(st, m, turns, summary)
                return
        for s in st.short:
            if s.content.strip().lower() == cl:
                self._reinforce_short_item(st, s, turns, summary)
                return

        cand_bg = _char_bigrams(content)

        # ⑨ M2 — correction replacement: the candidate carries a narrow
        # correction marker AND looks like a re-wording of an existing entry
        # (_is_stale_variant bar) → the corrected sentence supersedes that
        # entry's content in place (count+1, age reset via the normal reinforce
        # path), instead of coexisting with the stale old value. Sibling
        # restatements of the same stale fact (same bar against the OLD
        # content, e.g. an earlier phrasing-divergent variant) are swept out
        # with it — otherwise they survive as parallel stale copies. Entries
        # below the bar (topic-word-only overlap, fused episode entries) are
        # never touched: a correction that can't be surface-matched falls
        # through to M1/add and stays the LLM update-judge's job.
        target = self._find_correction_target(st, content, cand_bg)
        if target is not None:
            # Sweep only at FIRST correction of this entry. On reinforcement
            # passes the target content is already the corrected sentence,
            # whose entities (姐姐…) overlap compatible adjacent facts — sweeping
            # then would eat them (measured: 用户的姐姐是老师 lost).
            if not any(mk in target.content for mk in CORRECTION_MARKERS):
                self._sweep_stale_siblings(st, target, summary)
            target.content = content
            summary["corrected_in_place"] += 1
            if isinstance(target, MediumItem):
                self._reinforce_medium_item(st, target, turns, summary)
            else:
                self._reinforce_short_item(st, target, turns, summary)
            return

        # ⑨ M1 — near-dup merge: a verbatim-ish re-phrasing of an existing fact
        # (_is_near_dup) reinforces ONE item instead of piling up parallel
        # SHORT entries that each independently promote to MEDIUM. Wording is
        # RECENCY-WINS (candidate replaces content): near-dup pairs that differ
        # only in a value ("今年三十岁" → "今年三十五岁", no surface correction
        # marker) keep the newer fact instead of the stale first-seen one. For
        # true re-phrasings the swap is a no-op semantically.
        #
        # Digit guard: differing ASCII digit signatures ("事实1号" vs "事实10号",
        # 手机号/门牌号/编号…) mark ENUMERATED variants, not re-phrasings — never
        # merge those (a true identifier correction carries an M2 marker like
        # 改成/换成 and rides the replacement path above). Chinese-numeral value
        # changes (三十→三十五) have no ASCII digits and still merge recency-wins.
        cand_digits = _digit_signature(content)
        for m in st.medium:
            other_digits = _digit_signature(m.content)
            if (cand_digits and other_digits and cand_digits != other_digits):
                continue
            if _is_near_dup(cand_bg, _char_bigrams(m.content)):
                summary["near_dup_merged"] += 1
                m.content = content
                self._reinforce_medium_item(st, m, turns, summary)
                return
        for s in st.short:
            other_digits = _digit_signature(s.content)
            if (cand_digits and other_digits and cand_digits != other_digits):
                continue
            if _is_near_dup(cand_bg, _char_bigrams(s.content)):
                summary["near_dup_merged"] += 1
                s.content = content
                self._reinforce_short_item(st, s, turns, summary)
                return

        st.short.insert(0, ShortItem(id=_new_id(), content=content, count=1))
        summary["new_short"] += 1
        while len(st.short) > SHORT_MAX:
            st.short.pop()

    @staticmethod
    def _find_correction_target(st: UserTierState, content: str,
                                cand_bg: set) -> Optional[Any]:
        """⑨ M2 scan: locate the existing entry this candidate explicitly corrects.

        Two gates, BOTH required:
          - candidate contains one of the narrow CORRECTION_MARKERS
          - _is_stale_variant(candidate, entry) — distinctive shared bigram AND
            Dice/containment ≥ STALE_VARIANT_OVERLAP (two wordings of one fact).
            Fused episode entries (有一个上幼儿园的女儿,曾因女儿演出感动落泪) score
            below the bar against a 升小学 correction and are never replaced —
            the first L2 batch-2 run's looser gate erased p2/p4 canaries this way.

        Among all gate-passing entries the BEST match wins (most shared bigrams,
        Dice as tiebreak, MEDIUM before SHORT on equal scores): a correction
        sentence often mentions a new entity too ("不是独生女，有一个姐姐"), and
        first-match could wrongly target a compatible adjacent fact ("姐姐是老师")
        instead of the contradicted one ("是独生女"). Returns None when no entry
        passes, so compatible restatements ("已经在玩原神五年" vs "每天肝原神日常")
        fall through to the near-dup/add paths untouched.
        """
        if not any(mk in content for mk in CORRECTION_MARKERS):
            return None
        best, best_key = None, None
        for tier, lst in ((1, st.medium), (0, st.short)):
            for item in lst:
                item_bg = _char_bigrams(item.content)
                if not _is_stale_variant(cand_bg, item_bg):
                    continue
                key = (len(cand_bg & item_bg), _bigram_dice(cand_bg, item_bg), tier)
                if best_key is None or key > best_key:
                    best, best_key = item, key
        return best

    @staticmethod
    def _sweep_stale_siblings(st: UserTierState, target: Any, summary: dict):
        """⑨ M2b: drop sibling restatements of the fact being corrected.

        Keyed on the TARGET'S OLD content (pre-correction wording only — the
        caller skips the sweep on reinforcement passes): entries passing the
        _is_stale_variant bar against the superseded fact are restatements of it
        and would otherwise survive as parallel stale copies. The bar is
        deliberately below near-dup but above topic-word level: 住在杭州 vs
        家住杭州 (Dice/containment 0.33) are the same stale fact under
        differently-phrased variants — the exact multi-variant failure the L2
        baseline showed for p2 ct1/ct3 — while 游戏音乐外包 vs 第一款游戏亏损三万块
        (shares only 游戏, 0.10) must NOT sweep. The first L2 batch-2 run used
        "any distinctive shared bigram" here and halved p4's store (cl3 + all
        3 canaries lost); the numeric bar is the fix.

        Compatible facts related to the NEW sentence (用户的姐姐是老师 when the
        correction mentions 姐姐) share nothing with the OLD content and are
        safe. The earlier accepted-residual-risk case is now resolved by the
        bar: 用户的会计证考过了 vs old 是会计专业 scores 0.22/0.25 → kept.
        """
        old_bg = _char_bigrams(target.content)
        swept = 0
        for lst in (st.medium, st.short):
            keep = [item for item in lst
                    if item is target
                    or not _is_stale_variant(old_bg, _char_bigrams(item.content))]
            if len(keep) != len(lst):
                swept += len(lst) - len(keep)
                lst[:] = keep
        if swept:
            summary["stale_swept"] = summary.get("stale_swept", 0) + swept

    def _reinforce_short_item(self, st: UserTierState, item: ShortItem, turns, summary: dict):
        item.count += 1
        summary["reinforced_short"] += 1
        # move toward the front by SHORT_PRIORITY_STEP positions
        try:
            idx = st.short.index(item)
        except ValueError:
            idx = 0
        new_idx = max(0, idx - SHORT_PRIORITY_STEP)
        if new_idx != idx:
            st.short.pop(idx)
            st.short.insert(new_idx, item)
        # promote at count >= SHORT_PROMOTE_AT_COUNT
        if item.count >= SHORT_PROMOTE_AT_COUNT:
            self._promote_short_to_medium(st, item, turns, summary)

    def _promote_short_to_medium(self, st: UserTierState, item: ShortItem, turns, summary: dict):
        try:
            st.short.remove(item)
        except ValueError:
            pass
        med = MediumItem(
            id=item.id, content=item.content, count=item.count,
            entry_extraction_index=st.extraction_count,
        )
        st.medium.append(med)
        summary["promoted_to_medium"] += 1
        self._maybe_create_long(st, med, turns, summary)

    def _reinforce_medium_item(self, st: UserTierState, med: MediumItem, turns, summary: dict):
        med.count += 1
        med.entry_extraction_index = st.extraction_count   # age → 0
        summary["reinforced_medium"] += 1
        self._maybe_create_long(st, med, turns, summary)

    def _maybe_create_long(self, st: UserTierState, med: MediumItem, turns, summary: dict):
        """Create a LONG object when a MEDIUM item crosses count > MEDIUM_LONG_THRESHOLD.

        P1: CREATE + PERSIST ONLY. The MEDIUM twin is KEPT (Decision G model Y) and
        its count keeps rising. Idempotent: at most one LONG per MEDIUM id.
        """
        if med.count <= MEDIUM_LONG_THRESHOLD:
            return
        if any(lo.linked_medium_id == med.id for lo in st.long):
            return
        st.long.append(LongObject(
            id=_new_id(),
            title=med.content[:50],
            summary=med.content,
            snapshot_turns=list(turns or []),
            snapshot_time=time.time(),
            linked_medium_id=med.id,
        ))
        summary["promoted_to_long"] += 1

    def _run_demotions(self, st: UserTierState, summary: dict):
        """Post-ingestion sweep: MEDIUM age-demote + MEDIUM_MAX safety net."""
        current = st.extraction_count

        # age-based demotion: age > MEDIUM_DOWNGRADE_AGE → back to SHORT (count reset)
        for m in [m for m in st.medium if (current - m.entry_extraction_index) > MEDIUM_DOWNGRADE_AGE]:
            st.medium.remove(m)
            st.short.insert(0, ShortItem(id=m.id, content=m.content, count=MEDIUM_DOWNGRADE_COUNT_RESET))
            summary["demoted_to_short"] += 1

        # MEDIUM_MAX safety net: force-demote the oldest (highest age) first
        while len(st.medium) > MEDIUM_MAX:
            st.medium.sort(key=lambda m: -(current - m.entry_extraction_index))
            oldest = st.medium.pop(0)
            st.short.insert(0, ShortItem(id=oldest.id, content=oldest.content,
                                         count=MEDIUM_DOWNGRADE_COUNT_RESET))
            summary["demoted_to_short"] += 1

        # cap SHORT after demotions
        while len(st.short) > SHORT_MAX:
            st.short.pop()

    # ── injection (called by agent._build_messages) ───────────────

    def build_medium_injection(self, user_id: str) -> str:
        """Build the MEDIUM injection block. Returns "" if no MEDIUM items.

        Selection: top-12 by count + 3 most-recent promotions. Truncated to
        MEDIUM_INJECT_TOKEN_CAP chars, highest-count first (lowest-count dropped).
        count < LOW_CONFIDENCE_THRESHOLD gets a "(低置信)" label (Decision D).
        """
        st = self._get_state(user_id)
        if not st.medium:
            return ""

        by_count = sorted(st.medium, key=lambda m: -m.count)
        n_top = MEDIUM_INJECT_TOP_N - MEDIUM_INJECT_RECENT_SLOTS   # 12
        top = by_count[:n_top]
        top_ids = {m.id for m in top}
        recent = sorted(
            (m for m in st.medium if m.id not in top_ids),
            key=lambda m: -m.entry_extraction_index,
        )[:MEDIUM_INJECT_RECENT_SLOTS]

        selected = top + recent
        selected.sort(key=lambda m: -m.count)   # display + truncation priority

        lines: List[str] = []
        total = 0
        for m in selected:
            label = "(低置信) " if m.count < LOW_CONFIDENCE_THRESHOLD else ""
            line = f"- {label}{m.content}"
            if total + len(line) > MEDIUM_INJECT_TOKEN_CAP:
                break
            lines.append(line)
            total += len(line)

        if not lines:
            return ""
        return "\n## 用户记忆（中期）\n" + "\n".join(lines)

    def build_long_index(self, user_id: str) -> str:
        """Build the LONG-tier one-line-per-entry index block (P2-lite).

        Rationale (Memory-Optimization-Plan ①): LONG entries used to be
        write-only — when the MEDIUM twin age-demoted, the highest-confidence
        fact in the system became unreachable. The index gives the model
        *visibility* (one stable line per entry, Claude Code MEMORY.md
        pattern); the recall_memory tool gives it *access* (full text +
        conversation snapshot).

        Lines derive only from write-once fields (id/title/snapshot_time),
        so the block is byte-stable across turns — minimal L4-tail churn.
        Returns "" when the user has no LONG entries. Capped at
        LONG_INJECT_MAX_LINES (newest first) to bound the tail budget.
        """
        st = self._get_state(user_id)
        if not st.long:
            return ""
        entries = sorted(st.long, key=lambda lo: -lo.snapshot_time)[:LONG_INJECT_MAX_LINES]
        lines = []
        for lo in entries:
            when = (
                time.strftime("%Y-%m", time.localtime(lo.snapshot_time))
                if lo.snapshot_time else ""
            )
            suffix = f"（{when}）" if when else ""
            lines.append(f"- [{lo.id}] {lo.title}{suffix}")
        return (
            "\n## 用户长期记忆索引（高置信度）\n"
            + "\n".join(lines)
            + "\n（需要某条长期记忆的全文与对话快照时，调用 recall_memory 传入其 id 或关键词。）"
        )

    def recall_long(self, user_id: str, query: str) -> List[Tuple["LongObject", str]]:
        """Look up the user's OWN LONG entries by id-prefix or keyword.

        Returns up to LONG_RECALL_MAX_MATCHES pairs of (LongObject, formatted
        snapshot text capped at LONG_RECALL_SNAPSHOT_CAP chars), newest first.
        Empty list when nothing matches.

        Scoped strictly to this user's state — an id from another user never
        resolves. Each successful recall bumps query_count, appends a capped
        query_log entry and persists (query stats feed P2 ranking decisions).
        Synchronous like every TieredMemory method (§12.1 concurrency model).
        """
        st = self._get_state(user_id)
        q = (query or "").strip()
        if not q or not st.long:
            return []

        matches = [lo for lo in st.long if lo.id == q or lo.id.startswith(q)]
        if not matches:
            q_lower = q.lower()
            matches = [
                lo for lo in st.long
                if q_lower in lo.title.lower() or q_lower in lo.summary.lower()
            ]
        if not matches:
            return []

        matches.sort(key=lambda lo: -lo.snapshot_time)
        matches = matches[:LONG_RECALL_MAX_MATCHES]
        for lo in matches:
            lo.query_count += 1
            lo.query_log.append({"ts": time.time(), "query": q[:80]})
            del lo.query_log[:-LONG_RECALL_QUERY_LOG_CAP]
        self.save_user(user_id)
        return [(lo, self._format_snapshot_text(lo)) for lo in matches]

    @staticmethod
    def _format_snapshot_text(lo: "LongObject") -> str:
        """Render snapshot turns to text, capped at LONG_RECALL_SNAPSHOT_CAP."""
        lines: List[str] = []
        total = 0
        for um, ar in lo.snapshot_turns:
            piece = f"用户: {um}\n助手(Roxy): {ar}"
            if total + len(piece) > LONG_RECALL_SNAPSHOT_CAP:
                lines.append("…（快照过长，已截断）")
                break
            lines.append(piece)
            total += len(piece)
        return "\n".join(lines) if lines else "（无对话快照）"

    # ── persistence ───────────────────────────────────────────────

    def save_user(self, user_id: str):
        """Atomically persist one user's tier state (+ any new LONG snapshots)."""
        st = self._get_state(user_id)
        # write LONG snapshot markdowns that don't exist yet (write-once)
        for lo in st.long:
            path = os.path.join(self._long_user_dir(user_id), lo.id + ".md")
            if not os.path.exists(path):
                self._save_long_snapshot(user_id, lo)
        data = {
            "user_id": st.user_id,
            "extraction_count": st.extraction_count,
            "schema_version": TIER_SCHEMA_VERSION,
            "short": [{"id": s.id, "content": s.content, "count": s.count} for s in st.short],
            "medium": [{"id": m.id, "content": m.content, "count": m.count,
                        "entry_extraction_index": m.entry_extraction_index} for m in st.medium],
            "long": [{"id": lo.id, "title": lo.title, "summary": lo.summary,
                      "snapshot_time": lo.snapshot_time, "query_count": lo.query_count,
                      "query_log": lo.query_log, "linked_medium_id": lo.linked_medium_id}
                     for lo in st.long],
        }
        self._atomic_write_json(self._tier_path(user_id), data)

    def load_user(self, user_id: str) -> UserTierState:
        """Load (or create) one user's tier state from disk and cache it."""
        st = UserTierState(user_id=user_id)
        path = self._tier_path(user_id)
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                st.user_id = data.get("user_id") or user_id
                st.extraction_count = int(data.get("extraction_count", 0))
                st.short = [
                    ShortItem(id=s.get("id") or _new_id(), content=s.get("content", ""),
                              count=int(s.get("count", 1)))
                    for s in data.get("short", [])
                ]
                st.medium = [
                    MediumItem(id=m.get("id") or _new_id(), content=m.get("content", ""),
                               count=int(m.get("count", 0)),
                               entry_extraction_index=int(m.get("entry_extraction_index", 0)))
                    for m in data.get("medium", [])
                ]
                st.long = []
                for lo in data.get("long", []):
                    lid = lo.get("id") or _new_id()
                    st.long.append(LongObject(
                        id=lid, title=lo.get("title", ""), summary=lo.get("summary", ""),
                        snapshot_turns=self._load_long_snapshot(st.user_id, lid),
                        snapshot_time=float(lo.get("snapshot_time", 0.0)),
                        query_count=int(lo.get("query_count", 0)),
                        query_log=lo.get("query_log", []),
                        linked_medium_id=lo.get("linked_medium_id", ""),
                    ))
            except Exception:
                # corrupt/unreadable file → start fresh rather than crash the bot
                st = UserTierState(user_id=user_id)
        self._states[user_id] = st
        return st

    def load_all(self) -> int:
        """Startup: load every persisted user's tier state into the cache. Returns count."""
        if not os.path.exists(self.tiers_dir):
            return 0
        n = 0
        for fn in os.listdir(self.tiers_dir):
            if not fn.endswith(".json"):
                continue
            stem = fn[:-5]
            uid = stem
            try:
                with open(os.path.join(self.tiers_dir, fn), "r", encoding="utf-8") as f:
                    uid = json.load(f).get("user_id") or stem
            except Exception:
                pass
            self.load_user(uid)
            n += 1
        return n

    @staticmethod
    def _atomic_write_json(path: str, data: dict):
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    def _save_long_snapshot(self, user_id: str, lo: LongObject):
        d = self._long_user_dir(user_id)
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, lo.id + ".md")
        lines = [
            "---",
            f"id: {lo.id}",
            f"title: {lo.title}",
            f"linked_medium_id: {lo.linked_medium_id}",
            f"snapshot_time: {lo.snapshot_time}",
            f"query_count: {lo.query_count}",
            "---",
            "",
            f"## Snapshot ({len(lo.snapshot_turns)} turns)",
            "",
        ]
        for um, ar in lo.snapshot_turns:
            lines.append(f"用户: {um}")
            lines.append(f"助手(Roxy): {ar}")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        os.replace(tmp, path)

    def _load_long_snapshot(self, user_id: str, long_id: str) -> List[Tuple[str, str]]:
        """Best-effort parse of a LONG snapshot md back into (user, agent) turns.

        NOTE: lossy for messages containing newlines (the md format uses newline
        as the turn separator). Acceptable in P1 — LONG objects are create+persist
        only; snapshot_turns are not re-injected. P2 (RAG) can switch to a
        structured body if exact fidelity becomes necessary.
        """
        path = os.path.join(self._long_user_dir(user_id), long_id + ".md")
        turns: List[Tuple[str, str]] = []
        if not os.path.exists(path):
            return turns
        try:
            with open(path, "r", encoding="utf-8") as f:
                body = f.read()
            if body.startswith("---"):
                parts = body.split("---", 2)
                if len(parts) >= 3:
                    body = parts[2]
            cur_user: Optional[str] = None
            for line in body.split("\n"):
                if line.startswith("用户: "):
                    cur_user = line[len("用户: "):]
                elif line.startswith("助手(Roxy): ") and cur_user is not None:
                    turns.append((cur_user, line[len("助手(Roxy): "):]))
                    cur_user = None
        except Exception:
            pass
        return turns

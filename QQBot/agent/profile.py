"""
User Profile — Per-user TYPED profile slots with LLM-driven extraction.

Each user has a profile stored at data/users/{safe_user_id}/profile.json.

P0 画像瘦身 (Profile-Fact-Extraction-Plan.md §6): the profile is now a set of
TYPED, stable slots only —
- Basic info (nickname, first/last seen, interaction count)
- Preferences (language, response style)
- Interests (durable topics the user cares about)

The free-text `facts` field is DORMANT: it is no longer injected into the
system prompt nor extracted. Durable knowledge (occupation, location, skills,
ongoing projects) is re-routed to the three-tier memory engine in P1. The field
+ MAX_FACTS + merge_facts are kept (unused) so existing legitimate facts are not
lost; physical removal + migration are deferred to P1.

The profile is:
1. Loaded and injected into the system prompt each turn (typed slots only)
2. Updated asynchronously after each conversation via Layer 1 LLM extraction
   (litmus test + few-shot + third-person <conversation>), with every candidate
   passing the Layer 2 deterministic filter (fact_filter) before it is persisted.
"""

import asyncio
import json
import os
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

from .fact_filter import filter_many


# DORMANT (P0 画像瘦身): the free-text `facts` field is no longer injected into
# the system prompt nor extracted (see Profile-Fact-Extraction-Plan.md §6).
# The field + MAX_FACTS + merge_facts are kept dormant so we don't lose existing
# legitimate facts; physical removal + migration are deferred to P1.
MAX_FACTS = 40

# ── Layer 1 extraction caps (Profile-Fact-Extraction-Plan.md §7.7) ──
EXTRACT_USER_MSG_CAP = 500    # per-user-message truncation (chars)
EXTRACT_AGENT_RESP_CAP = 200  # per-agent-response truncation (disambig-only)

# ── Layer 3 batching (Profile-Fact-Extraction-Plan.md §7.1/§7.7, Decision J) ──
PROFILE_BATCH_K = 5       # flush a batch after this many buffered turns
EXTRACT_WINDOW_N = 8      # turns sent to the LLM = K batch + 3 preceding context
EXTRACT_TOTAL_CHAR_CAP = 6000  # total char cap on the conversation block
# delete-flush (Decision J): a session with > this many turns is "substantive"
# → its un-extracted tail is flushed; ≤ this → transient, tail dropped.
DELETE_FLUSH_MIN_TURNS = PROFILE_BATCH_K


@dataclass
class UserProfile:
    """Per-user profile with discovered facts and preferences."""

    user_id: str
    nickname: Optional[str] = None
    preferences: Dict[str, str] = field(default_factory=dict)
    facts: List[str] = field(default_factory=list)
    interests: List[str] = field(default_factory=list)
    first_seen: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    total_interactions: int = 0

    # ── Serialization ────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "nickname": self.nickname,
            "preferences": self.preferences,
            "facts": self.facts,
            "interests": self.interests,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "total_interactions": self.total_interactions,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "UserProfile":
        return cls(
            user_id=data.get("user_id", ""),
            nickname=data.get("nickname"),
            preferences=data.get("preferences", {}),
            facts=data.get("facts", []),
            interests=data.get("interests", []),
            first_seen=data.get("first_seen", time.time()),
            last_seen=data.get("last_seen", time.time()),
            total_interactions=data.get("total_interactions", 0),
        )

    # ── Prompt Context ───────────────────────────────────────────

    def to_prompt_context(self) -> str:
        """Generate the user context block to inject into the system prompt.

        P0 画像瘦身：只注入类型化稳定槽（nickname / interests / preferences）。
        自由文本 `facts` 已停用——不再注入、不再抽取（§6）。字段本身保留为休眠
        态，物理移除推迟到 P1，以免误删历史合法 facts。
        """
        parts = ["## 当前用户信息"]

        if self.nickname:
            parts.append(f"用户称呼: {self.nickname}")

        if self.interests:
            parts.append(f"兴趣话题: {', '.join(self.interests[:8])}")

        if self.preferences:
            pref_str = ", ".join(f"{k}={v}" for k, v in self.preferences.items())
            parts.append(f"偏好: {pref_str}")

        if self.total_interactions > 0:
            parts.append(f"历史交互次数: {self.total_interactions}")

        if len(parts) == 1:
            return ""  # No profile data yet
        return "\n".join(parts)

    # ── Update ───────────────────────────────────────────────────

    def touch(self):
        """Update last_seen and increment interaction count."""
        self.last_seen = time.time()
        self.total_interactions += 1

    def merge_facts(self, new_facts: List[str]):
        """DORMANT (P0): no longer called — facts are not extracted or injected.

        Kept (with the `facts` field) so existing profile data survives until
        the P1 migration to the three-tier memory engine. Do not wire new
        callers to this.

        Original behavior: add new facts, avoiding fuzzy duplicates, capped to
        MAX_FACTS (oldest dropped on overflow).
        """
        for fact in new_facts:
            if not any(self._similar(fact, existing) for existing in self.facts):
                self.facts.append(fact)
        if len(self.facts) > MAX_FACTS:
            self.facts = self.facts[-MAX_FACTS:]

    def merge_interests(self, new_interests: List[str]):
        """Add new interests, avoiding duplicates."""
        existing_lower = {i.lower() for i in self.interests}
        for interest in new_interests:
            if interest.lower() not in existing_lower:
                self.interests.append(interest)
                existing_lower.add(interest.lower())

    def merge_preferences(self, new_prefs: Dict[str, str]):
        """Merge preferences dict."""
        self.preferences.update(new_prefs)

    @staticmethod
    def _similar(a: str, b: str) -> bool:
        """Simple overlap check (not full semantic similarity)."""
        a_words = set(a.lower().split())
        b_words = set(b.lower().split())
        if not a_words or not b_words:
            return False
        overlap = len(a_words & b_words) / min(len(a_words), len(b_words))
        return overlap > 0.6


# Few-shot 反例/正例，嵌入抽取 prompt（§10 Layer 1）。反例 → {}，覆盖高频垃圾
# 根因（截图/助手解读当证据、一次性抽卡结果、bot 状态、瞬时决策态、可再推导的
# 公开信息）；一个正例演示「用户自述持久兴趣」该抽取。反例是主力——模型已证明
# 会无视纯文字禁令。例6 对应 Memory-Optimization-Plan ②（可再推导测试），
# 例7 对应 ⑧（用户纠正 → judge="update"，chat-export 冲突消解改进点），
# 例8 对应 ⑩（画像偏好槽位被推翻 → updated_preferences 覆盖/null 删除——
# 修规则4「只抽新信息」让陈旧槽位永不修订的死角，L2 两版本稳定复现 ×4）。
_PROFILE_FEW_SHOT = """例1（截图/助手解读 ≠ 用户事实 → {}）
输入:
<conversation>
用户: [上传了一张游戏战斗截图] 帮我看看这个阵容怎么优化
助手(Roxy): 这是「崩坏：星穹铁道」的战斗画面，场上有托帕和知更鸟，建议……
</conversation>
输出: {}
（游戏名与角色都来自助手对截图的解读；用户本人没说他玩这游戏。）

例2（一次性抽卡结果，石蕊测试不过 → {}）
输入:
<conversation>
用户: 我刚十连歪了，没出UP角色
助手(Roxy): 好惨，下次保底就稳了……
</conversation>
输出: {}
（本轮抽卡结果，用户不碰 bot 就不成立。）

例3（bot 状态，非用户属性 → {}）
输入:
<conversation>
用户: 我的工作区还剩多少空间？
助手(Roxy): 当前工作区剩余 250MB。
</conversation>
输出: {}
（工作区容量是 bot 状态，不是用户的持久属性。）

例4（用户自述持久兴趣 → 抽取）
输入:
<conversation>
用户: 我最近迷上原神了，天天肝
助手(Roxy): 原神挺耐玩的，注意别太肝……
</conversation>
输出: {"new_interests": ["原神"], "memory_candidates": [{"content": "用户最近迷上原神，每天都玩", "judge": "new", "category": "fact"}]}
（用户本人陈述的持久兴趣，石蕊测试通过。话题词「原神」进 new_interests；完整事实句进 memory_candidates。
「迷上原神每天玩」是持久习惯属性 → category=fact。）

例5（瞬时决策态 / 犹豫中 → {}）
输入:
<conversation>
用户: 我在想要不要给无线网络功能加个开关
助手(Roxy): 这个看你的使用场景，需要的话我可以帮你查……
</conversation>
输出: {}
（"在想要不要……"是瞬时决策态，石蕊测试不通过：用户不碰 bot 这条就不成立。
反之，【现实生活】的持续活动（如"用户正在做力量训练""正在通过饮食调整降血脂"）
通过石蕊测试，应作为持久信息抽取。）

例6（工具可查的公开信息，可再推导测试不过 → {}）
输入:
<conversation>
用户: 帮我查一下原神里流浪者突破要什么材料
助手(Roxy): 流浪者突破需要恒常机关阵列、蕈兽孢子等材料，90级突破共需……
</conversation>
输出: {}
（突破材料是 wiki 公开数据，工具随时可查且随版本变化，写进记忆只会变陈旧。
"帮我查一次X"也不构成持久兴趣的证据；若用户说"我主玩流浪者"，那才是关于
用户本人的私有事实，应抽取。）

例7（用户纠正旧记忆 → judge="update"，不可无视；对应 Memory-Optimization-Plan ⑧）
输入（假设「已有记忆」里有一条 {"id": "m7", "content": "用户每周跑步三次", "tier": "medium"}）:
<conversation>
用户: 我现在不跑步了，改游泳了，还是一周三次
助手(Roxy): 游泳也很好，对膝盖负担小……
</conversation>
输出: {"memory_candidates": [{"content": "用户已把每周跑步三次改为每周游泳三次", "judge": "update", "matched_id": "m7", "category": "fact"}]}
（用户明确纠正/更新旧信息时，必须给 update：content 写纠正后的完整事实句，
matched_id 指向被纠正的条目——绝不能无视纠正让旧记忆存活，也不要另建一条
与之矛盾的新记忆。若「已有记忆」里找不到可纠正的条目，则用 judge="new"。
运动习惯是持久属性 → category=fact；纠正替换时也要给出 category 覆盖旧类型。）

例8（用户推翻 existing 画像里的偏好槽位 → updated_preferences；对应 Memory-Optimization-Plan ⑩）
输入（假设 existing 画像为 {"preferences": {"设备": "安卓手机", "饮品": "不喝咖啡"}}）:
<conversation>
用户: 我上周换 iPhone 了，以后别给我推安卓教程；咖啡也戒了，改喝茶
助手(Roxy): 好的，之后按 iOS 的来，茶饮推荐也安排……
</conversation>
输出: {"updated_preferences": {"设备": "iPhone", "饮品": null}}
（existing 里已有的偏好槽位被用户推翻或修改时，必须用 updated_preferences 覆盖旧值；
偏好彻底不成立（戒了/不再/改成别的了）就把值设为 null 删除该槽位。规则4的
「只抽新信息」绝不意味着让旧槽位永远残留；也不要把修正值塞进 new_preferences
造成新旧并存。键名必须与 existing 画像里的键完全一致。）

例9（category 分类：critical / preference / event；对应 Memory-Optimization-Plan ④）
输入:
<conversation>
用户: 提醒一下我对青霉素过敏，别推荐含它的药；另外回复尽量简短，我上周刚做完近视手术
助手(Roxy): 记下了，会避开青霉素类，回复也从简……
</conversation>
输出: {"new_preferences": {"回复风格": "简短"}, "memory_candidates": [{"content": "用户对青霉素过敏", "judge": "new", "category": "critical"}, {"content": "用户上周做了近视手术", "judge": "new", "category": "event"}]}
（过敏禁忌记错代价高 → critical，此类记忆更耐降级；一次性、有明确时间点的经历 → event；
「回复简短」是互动偏好，走 new_preferences 槽位，不必再进 memory_candidates。）"""


class ProfileManager:
    """Manages user profiles with persistence and LLM-driven extraction.

    P0 画像瘦身：画像 = 类型化稳定槽（nickname / interests / preferences）。
    自由文本 `facts` 字段休眠（不注入、不抽取），持久知识改走 P1 三级记忆引擎。
    抽取精度由 Layer 1（重构 prompt：石蕊测试 + few-shot + 第三人称块）与
    Layer 2（fact_filter 确定性后置过滤）共同保证。
    """

    def __init__(self, base_dir: str, llm_client=None):
        """
        Args:
            base_dir: Directory for profile JSON files.
            llm_client: DeepSeekClient for fact extraction (optional, can be set later).
        """
        self.base_dir = base_dir
        self.client = llm_client
        self._memory = None  # P1: TieredMemory engine, wired via set_memory()
        os.makedirs(base_dir, exist_ok=True)
        self._cache: Dict[str, UserProfile] = {}
        # user_id → st_mtime_ns of profile.json as last seen by this process
        # (load or own save). Used by get() to revalidate the cache against
        # external writers (the WebUI panel edits profile.json directly).
        self._cache_mtime: Dict[str, Optional[int]] = {}

        # ── Layer 3 batching state (per-user) ──
        # _recent: rolling context window (maxlen=EXTRACT_WINDOW_N) of turns,
        #          used to build the <conversation> block (batch + 3 preceding
        #          turns so "该项目"-style references can be resolved).
        # _pending_n: count of buffered turns not yet extracted; flush at >= K.
        # _inflight: users with an extract_batch task currently running
        #            (single-flight — never two extractions for one user).
        # _force_flush: users whose pending tail must be flushed at next idle
        #            even if < K (set by delete-flush on a substantive session).
        # ⑦ (Memory-Optimization-Plan): _recent/_pending_n are mirrored to
        # {user_dir}/buffer.json on every mutation and lazily restored on the
        # first observe_turn after a restart, so a crash loses ≤1 turn instead
        # of up to K-1 un-extracted turns. _inflight/_force_flush stay
        # process-local (a crash mid-extraction re-buffers, never double-runs:
        # _flush persists pending_n=0 BEFORE scheduling the task).
        self._recent: Dict[str, "deque[Tuple[str, str]]"] = {}
        self._pending_n: Dict[str, int] = {}
        self._inflight: set = set()
        self._force_flush: set = set()

    def set_client(self, client):
        """Set or update the LLM client (for lazy initialization)."""
        self.client = client

    def set_memory(self, memory):
        """Wire the P1 three-tier memory engine (TieredMemory).

        When set, extract_batch merges semantic-overlap judging into the single
        flash call (§12.1) and routes accepted memory_candidates into the
        SHORT/MEDIUM/LONG state machine. When None, behavior is P0 (profile slots
        only) — keeps the single-turn extract_and_update path backward-compatible.
        """
        self._memory = memory

    # ── CRUD ──────────────────────────────────────────────────────

    @staticmethod
    def _stat_mtime(path: str) -> Optional[int]:
        """st_mtime_ns of path, or None when missing/unreadable."""
        try:
            return os.stat(path).st_mtime_ns
        except OSError:
            return None

    def get(self, user_id: str) -> UserProfile:
        """Get or create a user profile.

        The in-memory cache is revalidated against the file's mtime on every
        call: an external writer (WebUI panel editing profile.json) wins over
        the cached copy, so panel edits are no longer silently reverted by
        the next bot-side save. Unsaved in-memory deltas (e.g. touch() counts
        since the last save) are the accepted loss when a reload happens.
        """
        path = self._path(user_id)
        disk_mtime = self._stat_mtime(path)

        if user_id in self._cache:
            if disk_mtime is not None and disk_mtime != self._cache_mtime.get(user_id):
                reloaded = self._load(user_id)
                if reloaded is not None:
                    self._cache[user_id] = reloaded
                    self._cache_mtime[user_id] = disk_mtime
            return self._cache[user_id]

        profile = self._load(user_id)
        if profile is None:
            profile = UserProfile(user_id=user_id)

        self._cache[user_id] = profile
        self._cache_mtime[user_id] = disk_mtime
        return profile

    def save(self, profile: UserProfile):
        """Persist a profile to disk."""
        self._cache[profile.user_id] = profile
        path = self._path(profile.user_id)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(profile.to_dict(), f, ensure_ascii=False, indent=2)
            self._cache_mtime[profile.user_id] = self._stat_mtime(path)
        except Exception as e:
            # Non-fatal, but no longer silent: a failed persist used to be
            # invisible (panel showed "saved" while nothing was written).
            logger.warning("[profile] save failed user={}: {}", profile.user_id, e)

    # ── Layer 3: batched observation + extraction (§7.1, P0) ──────

    def observe_turn(self, user_id: str, user_message: str, agent_response: str,
                     persona_name: str = ""):
        """Per-turn hook (SYNC, no await): buffer the turn + touch the profile.

        Called from agent._schedule_profile_update on every turn. When the
        buffer reaches PROFILE_BATCH_K and no extraction is in flight for this
        user, a single-flight extract_batch is scheduled over the recent window.

        persona_name: the active persona's short display label for THIS turn
        (captured by the caller from _current_personality). Stored on the
        buffered turn so a LONG snapshot persisted from this batch labels the
        assistant with the persona that actually spoke, not a hardcoded default.
        Empty → snapshots fall back to 助手(Roxy).

        Concurrency model (§12.1): no explicit locks. This method only does
        synchronous in-memory appends; the LLM work happens in a background
        task that finishes all awaits BEFORE the synchronous atomic apply.
        Single-flight per user guarantees at most one extraction at a time.
        """
        profile = self.get(user_id)
        profile.touch()  # in-memory; persisted on the next changed save

        if not self.client:
            # No extraction client configured → touch counts the interaction but
            # there is nothing to batch. Avoids unbounded buffer growth + no-op
            # task scheduling on the client-less path.
            return

        if user_id not in self._recent:
            # ⑦: first observation in this process → try to restore the buffer
            # persisted before the last shutdown/crash (no-op when absent).
            self._restore_buffer(user_id)
        dq = self._recent.get(user_id)
        if dq is None:
            dq = deque(maxlen=EXTRACT_WINDOW_N)
            self._recent[user_id] = dq
        dq.append((user_message or "", agent_response or "", persona_name or ""))

        self._pending_n[user_id] = self._pending_n.get(user_id, 0) + 1
        self._save_buffer(user_id)
        if self._pending_n[user_id] >= PROFILE_BATCH_K and user_id not in self._inflight:
            self._flush(user_id)

    def flush_on_session_end(self, user_id: str, session_turn_count: int):
        """delete-flush (Decision J): on session deletion, submit or drop tail.

        session_turn_count > DELETE_FLUSH_MIN_TURNS → substantive session: force
        a flush of the pending tail (even if < K) once idle. ≤ → transient
        session: discard the un-extracted tail (loss explicitly accepted).
        """
        if session_turn_count > DELETE_FLUSH_MIN_TURNS:
            self._force_flush.add(user_id)
            if user_id not in self._inflight and self._pending_n.get(user_id, 0) > 0:
                self._flush(user_id)
        else:
            self._pending_n[user_id] = 0
            self._force_flush.discard(user_id)
            self._save_buffer(user_id)
            logger.debug(
                "[profile] delete-flush drop tail user={} turns={}",
                user_id, session_turn_count,
            )

    def _flush(self, user_id: str):
        """Snapshot the recent window and schedule a single-flight extract_batch."""
        window = list(self._recent.get(user_id, ()))
        self._pending_n[user_id] = 0
        self._force_flush.discard(user_id)
        # ⑦: persist pending_n=0 BEFORE scheduling — a crash mid-extraction
        # loses this batch (same as the old in-memory behavior) but can never
        # double-extract it after a restart.
        self._save_buffer(user_id)
        if not window:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # No running loop (e.g. synchronous test): cannot schedule a
            # background task. Drop the in-flight flag; callers/tests can drive
            # extract_batch() directly.
            return
        self._inflight.add(user_id)
        asyncio.create_task(self._extract_batch_guarded(user_id, window))

    async def _extract_batch_guarded(self, user_id: str, turns):
        """Single-flight wrapper: clear _inflight, then re-check pending tail."""
        try:
            await self.extract_batch(user_id, turns)
        except Exception as e:
            # Non-critical background task — never propagate.
            logger.warning("[profile] extract_batch failed user={}: {}", user_id, e)
        finally:
            self._inflight.discard(user_id)
            pending = self._pending_n.get(user_id, 0)
            if pending >= PROFILE_BATCH_K or (user_id in self._force_flush and pending > 0):
                self._flush(user_id)

    async def extract_batch(self, user_id: str, turns):
        """Run Layer-1 extraction over a window of turns via the flash model.

        Args:
            turns: list of (user_message, agent_response), oldest → newest,
                up to EXTRACT_WINDOW_N. Builds ONE multi-turn <conversation>
                block and makes a SINGLE LLM call — all awaits complete before
                the synchronous atomic apply (§12.1 并发: 先算完再原子写).
        """
        if not self.client or not turns:
            return

        profile = self.get(user_id)
        existing = {
            "nickname": profile.nickname,
            "interests": profile.interests,
            "preferences": profile.preferences,
        }
        # P1: read the judge list BEFORE the await (synchronous read). Per-user
        # single-flight guarantees no concurrent extraction mutates state mid-call.
        judge_list = self._memory.get_judge_list(user_id) if self._memory else []
        conversation_block = self._format_conversation(turns)
        prompt = self._build_extraction_prompt(conversation_block, existing, judge_list)

        result = await self.client.chat_completion(
            prompt, timeout_set=30.0, purpose="profile",
        )
        extracted = self._parse_json(result)
        # Distinguish a parse FAILURE (None — LLM error / unparseable) from a
        # SUCCESSFUL empty extraction ({} — nothing new to remember). Only a
        # failure skips the clock; an empty {} still advances extraction_count so
        # existing MEDIUM facts age normally (Decision 4: tick on every success).
        if extracted is None:
            return

        # ── synchronous atomic apply (no await between read and write) ──
        # Re-fetch the profile AFTER the LLM await: get() revalidates against
        # disk, so a panel edit that landed while the call was in flight is
        # picked up instead of being clobbered by the pre-await snapshot.
        profile = self.get(user_id)
        snapshot_turns = list(turns)
        if self._apply_extraction(profile, user_id, extracted, snapshot_turns):
            self.save(profile)
            logger.debug(
                "[profile] batch-updated user_id={} turns={}", user_id, len(turns)
            )

        # P1: advance the logical clock + persist tier state. Done AFTER a
        # successful parse (Decision 4: count increments only on success) and
        # after ingest (which ran inside _apply_extraction against the old clock).
        if self._memory:
            self._memory.touch_extraction_count(user_id)
            self._memory.save_user(user_id)

    async def extract_and_update(self, user_id: str, user_message: str, agent_response: str,
                                 persona_name: str = ""):
        """Single-turn convenience wrapper over extract_batch (backward-compat).

        The runtime path now uses observe_turn() (buffered batching, Layer 3).
        This extracts ONE turn immediately — kept for direct testing and any
        caller that wants per-turn extraction without batching.
        """
        if not self.client:
            return
        profile = self.get(user_id)
        profile.touch()
        await self.extract_batch(
            user_id, [(user_message or "", agent_response or "", persona_name or "")])

    def _apply_extraction(self, profile: UserProfile, user_id: str, extracted: dict,
                          snapshot_turns: Optional[list] = None) -> bool:
        """Merge a parsed extraction dict into the profile. Returns profile-changed?

        Synchronous, no await — runs as an atomic block after all LLM calls have
        completed (§12.1). Interests AND memory_candidates pass the Layer-2
        deterministic filter (fact_filter) before they are persisted; every drop
        is logged for observability-driven blacklist tuning (§10/§12.2).

        P1: memory_candidates are routed into the three-tier engine AFTER the
        Layer-2 filter (§4 principle I: litmus + L2 before entering SHORT). The
        return value reflects only profile.json changes; tier state is persisted
        separately by extract_batch via save_user.
        """
        changed = False

        # nickname
        nick = extracted.get("nickname")
        if nick and not profile.nickname:
            profile.nickname = str(nick)[:50]
            changed = True

        # new_interests → Layer 2 deterministic filter before persisting
        raw_interests = extracted.get("new_interests", []) or []
        if raw_interests:
            candidates = [str(i)[:50] for i in raw_interests]
            kept, dropped = filter_many(candidates)
            for text, matched, category in dropped:
                logger.debug(
                    "[profile] L2-drop interest user={} cat={} match={!r} text={!r}",
                    user_id, category, matched, text,
                )
            if kept:
                before = len(profile.interests)
                profile.merge_interests(kept)
                if len(profile.interests) > before:
                    changed = True
            logger.debug(
                "[profile] interests user={} kept={} dropped={}",
                user_id, len(kept), len(dropped),
            )

        # new_preferences
        new_prefs = extracted.get("new_preferences", {})
        if new_prefs and isinstance(new_prefs, dict):
            profile.merge_preferences(
                {str(k)[:50]: str(v)[:100] for k, v in new_prefs.items()}
            )
            changed = True

        # updated_preferences (⑩): revise or delete EXISTING slots. Rule 4's
        # "only extract NEW info" made preferences write-once — L2 showed stale
        # values (设备:安卓手机 / 从不氪金 / 只看日漫) surviving explicit
        # contradiction in BOTH engine versions. value None → slot no longer
        # holds → delete. Keys must match existing ones exactly (the model sees
        # the full existing profile in the prompt); an unknown key on update
        # simply creates the slot, on delete is a no-op.
        upd_prefs = extracted.get("updated_preferences")
        if upd_prefs and isinstance(upd_prefs, dict):
            for k, v in upd_prefs.items():
                key = str(k)[:50]
                if v is None:
                    if profile.preferences.pop(key, None) is not None:
                        changed = True
                        logger.debug("[profile] pref-delete user={} key={!r}", user_id, key)
                else:
                    new_val = str(v)[:100]
                    if profile.preferences.get(key) != new_val:
                        profile.preferences[key] = new_val
                        changed = True
                        logger.debug("[profile] pref-update user={} key={!r} val={!r}",
                                     user_id, key, new_val)

        # P1: route memory_candidates into the three-tier engine. The Layer-2
        # filter runs on candidate content BEFORE ingestion (§4 principle I).
        if self._memory:
            self._route_memory_candidates(user_id, extracted, snapshot_turns)

        return changed

    def _route_memory_candidates(self, user_id: str, extracted: dict,
                                 snapshot_turns: Optional[list]):
        """Layer-2 filter + route memory_candidates into TieredMemory. Sync, no await."""
        raw = extracted.get("memory_candidates", []) or []
        norm = []
        if isinstance(raw, list):
            for c in raw:
                if not isinstance(c, dict):
                    continue
                content = str(c.get("content", ""))[:300]
                if not content.strip():
                    continue
                norm.append({
                    "content": content,
                    "judge": c.get("judge", "new"),
                    "matched_id": c.get("matched_id"),
                    "category": c.get("category"),   # ④ may be None → engine defaults/keeps
                })
        dropped = []
        ingest_list = norm
        if norm:
            # Layer 2 deterministic filter on candidate content (same filter as interests)
            texts = [c["content"] for c in norm]
            kept_texts, dropped = filter_many(texts)
            for text, matched, category in dropped:
                logger.debug(
                    "[memory] L2-drop candidate user={} cat={} match={!r} text={!r}",
                    user_id, category, matched, text,
                )
            kept_set = set(kept_texts)
            ingest_list = [c for c in norm if c["content"] in kept_set]
        # ALWAYS ingest — even an empty list runs the demotion/aging sweep against
        # the current logical clock, so MEDIUM ages on every successful batch
        # (including empty {} extractions, which still tick extraction_count).
        summary = self._memory.ingest_candidates(user_id, ingest_list, snapshot_turns or [])
        if norm or summary.get("demoted_to_short"):
            logger.debug(
                "[memory] ingest user={} kept={} dropped={} summary={}",
                user_id, len(ingest_list), len(dropped), summary,
            )

    # ── Layer 1 prompt construction (shared by single-turn + batch) ──

    @staticmethod
    def _format_conversation(turns) -> str:
        """Format turns as ONE third-person <conversation> block (§10 Layer 1).

        第三人称标签 + 显式区分「用户」与「助手(Roxy)」，修掉旧 prompt 把 agent
        回复误标为 "Your response" 的歧义（那是把 bot 的话当用户证据的根因之一）。

        Applies EXTRACT_TOTAL_CHAR_CAP: when over the cap, the OLDEST turns are
        dropped first (recent turns matter more). Per-message truncation uses
        EXTRACT_USER_MSG_CAP / EXTRACT_AGENT_RESP_CAP.
        """
        open_tag = "<conversation>\n"
        close_tag = "\n</conversation>"
        pieces: List[str] = []
        total = len(open_tag) + len(close_tag)
        # accumulate newest-first so dropping happens at the oldest end
        for turn in reversed(list(turns)):
            um = turn[0] if len(turn) > 0 else ""
            ar = turn[1] if len(turn) > 1 else ""
            u = (um or "")[:EXTRACT_USER_MSG_CAP]
            a = (ar or "")[:EXTRACT_AGENT_RESP_CAP]
            # NOTE: the extraction prompt keeps a FIXED 助手(Roxy) third-person
            # label on purpose — its hard rules and few-shot examples are keyed
            # to that literal, and it is only a user/assistant disambiguation
            # device for the extractor. The per-turn persona affects persisted
            # LONG snapshots (memory.py), NOT this prompt label. Turns may be
            # 3-tuples (user, agent, persona); the persona is ignored here.
            piece = f"用户: {u}\n助手(Roxy): {a}"
            add = len(piece) + (1 if pieces else 0)  # +1 for the joining newline
            if total + add > EXTRACT_TOTAL_CHAR_CAP:
                break
            pieces.append(piece)
            total += add
        pieces.reverse()
        if not pieces:
            return open_tag + close_tag
        return open_tag + "\n".join(pieces) + close_tag

    @staticmethod
    def _build_extraction_prompt(conversation_block: str, existing: dict,
                                 judge_list: Optional[list] = None) -> str:
        """Build the Layer 1 profile-extraction prompt (§10).

        P1 merges the semantic-overlap judge into the SAME flash call (§12.1):
        alongside nickname/new_interests/new_preferences, the model emits
        memory_candidates, each tagged judge ∈ {new, reinforce_short, update,
        keep} against the 已有记忆 list (MEDIUM top-40 + SHORT, from
        TieredMemory.get_judge_list). 石蕊测试为首要判据 + few-shot 反例 +
        自包含 + agent_response 禁当证据。No new_facts (P0 schema).
        """
        if judge_list:
            mem_section = (
                "## 已有记忆（供 memory_candidates 语义重合判定）\n"
                "下面是该用户已有的记忆条目（tier=medium 中期 / tier=short 短期）。\n"
                "对每条候选事实，判断它与哪条已有记忆语义重合，并据此选择 judge：\n"
                '  - 与任何已有记忆都无重合 → judge="new"\n'
                '  - 与某条 tier=short 重合 → judge="reinforce_short"，matched_id=该条 id\n'
                '  - 与某条 tier=medium 重合，且你有更详细/更新的表述 → judge="update"，'
                'matched_id=该条 id，content 给精炼合并后的完整句\n'
                '  - 与某条 tier=medium 重合，但无新细节 → judge="keep"，matched_id=该条 id\n'
                '  - 用户【纠正/推翻】了某条已有记忆（"你记错了""我不再X了，改成Y了"）→ '
                '必须 judge="update"，matched_id=该条 id，content=纠正后的完整事实句；'
                '绝不能无视纠正让旧记忆继续存在，也不要另建一条与之矛盾的新记忆\n'
                f"{json.dumps(judge_list, ensure_ascii=False)}\n\n"
            )
        else:
            mem_section = (
                "## 已有记忆（供 memory_candidates 语义重合判定）\n"
                '（该用户暂无已有记忆；所有 memory_candidates 一律用 judge="new"。）\n\n'
            )
        return (
            "你是用户画像抽取器。从下面这轮对话中，抽取关于【用户本人】的、持久的画像信息。\n"
            "只返回合法 JSON，不要解释，不要 markdown 代码围栏。\n\n"
            "## 首要判据：石蕊测试\n"
            "对每一条候选，先问自己：\n"
            "  「如果这个用户从此再也不碰这个 bot，这条还成立吗？」\n"
            "成立 → 才可能是持久画像；不成立 → 直接丢弃。\n"
            "这一条即可干掉：工作区/磁盘用量、本轮抽卡结果、截图测速、"
            "「正在参与特殊会话」、bot 的口癖或角色扮演称谓。\n\n"
            "## 次要判据：可再推导测试（负面清单）\n"
            "通过石蕊测试后，再问一句：\n"
            "  「这条信息是工具（search_web / character_detail / get_changelog / 天气等）"
            "随时能查到的公开信息吗？」\n"
            "是 → 不进记忆。游戏角色数值/养成材料、版本更新内容、攻略数据、天气、新闻、"
            "汇率等公开信息，用工具现查永远比记忆副本新鲜准确；存进记忆反而会在版本更新后变陈旧错误。\n"
            "只记【用户本人】的属性、经历、偏好、约定——工具查不到的私有信息。\n"
            "边界示例：「用户玩原神，主玩钟离」是私有事实（记）；"
            "「原神角色等级上限是90」是公开信息（不记）。用户让 bot 查一次某公开数据，"
            "这个查询行为本身也不构成持久兴趣的证据。\n\n"
            "## 硬性规则\n"
            "1. 【助手(Roxy)】说的话只能用于消歧，绝不能当作关于用户的证据。"
            "助手回复可能含角色扮演称谓、假设、对截图/图片的解读——都不是用户的事实。\n"
            "2. 游戏名、角色归属、个人身份等，必须来自【用户】自己说的话，"
            "不能由用户上传的截图/图片/文件内容反推。一张战斗截图描述的是截图内容，不是用户。\n"
            "3. 每条候选必须【自包含】：把指代（该项目/这个/上面说的）解析成明确的命名实体；"
            "无法解析成一句独立成立的话，就丢弃。\n"
            "4. nickname/new_interests/new_preferences 只抽取 existing 画像里没有的【新】信息；"
            "但若用户【推翻或修改】了 existing 画像里已有的偏好（如换了手机、开始喝咖啡了），"
            "必须用 updated_preferences 覆盖旧值（偏好不再成立则填 null 删除）——"
            "「只抽新信息」绝不意味着让旧槽位永远残留，也不要把修正值塞进 new_preferences；\n"
            "   memory_candidates 则须对照「已有记忆」判定 judge（见下）。\n"
            "5. 客观、不臆测：记「玩原神」，不记「可能是开发者」。\n"
            "6. 只抽取关于用户的，不抽取关于助手(Roxy)的。\n"
            "7. 没有新信息就返回 {}。\n\n"
            "## 输出 schema（字段均可选，无则省略）\n"
            "{\n"
            '  "nickname": "用户提到的名字或希望被怎样称呼",\n'
            '  "new_interests": ["用户表现出的持久兴趣/话题", ...],\n'
            '  "new_preferences": {"语言/回复风格等偏好键": "偏好值"},\n'
            '  "updated_preferences": {"existing画像里已有的偏好键": "修正后的新值；该偏好已不成立则为 null"},\n'
            '  "memory_candidates": [\n'
            '    {"content": "关于用户的精炼、自包含事实句（通过石蕊测试）",\n'
            '     "judge": "new | reinforce_short | update | keep",\n'
            '     "matched_id": "仅 reinforce_short/update/keep 时填，对应已有记忆的 id",\n'
            '     "category": "preference | fact | event | critical"}\n'
            "  ]\n"
            "}\n\n"
            "## memory_candidates 规则\n"
            "1. content 必须是关于【用户本人】、通过石蕊测试与可再推导测试"
            "（可公开复查的信息不进记忆）、自包含的精炼事实句。\n"
            "2. 判定 judge 只能依据【用户】说的话；助手(Roxy) 的话仅用于消歧。\n"
            "3. 与 new_interests 的区别：兴趣是话题词（如「原神」），memory_candidates 是完整\n"
            "   事实句（如「用户是崩坏：星穹铁道的活跃玩家，每天做日常」）；两者可并存。\n"
            "4. 用户纠正旧信息时（\"你记错了\"\"我已经不X了\"）必须输出 memory_candidates：\n"
            "   已有记忆里有对应条目 → judge=\"update\" + matched_id，content 为纠正后的完整\n"
            "   事实句；找不到对应条目 → judge=\"new\"。无视纠正是最差的结果。\n"
            "5. 没有值得长期记住的事实就省略 memory_candidates。\n"
            "6. category 四选一（不确定就填 fact，它是默认类）：\n"
            "   - critical：涉及健康安全/过敏禁忌/家庭重大变故/重大财务或法律/绝对不能触碰的话题，\n"
            "     记错或遗忘代价高（如「用户对青霉素过敏」「用户父亲去年过世」）。此类记忆更耐降级。\n"
            "   - preference：用户对互动方式或内容消费的偏好（如「用户喜欢简短回复」「用户爱吃辣」）。\n"
            "   - event：一次性、有明确时间点的经历（如「用户上周去了上海漫展」「用户今年换了工作」）。\n"
            "   - fact：以上都不是的持久属性/身份/习惯（如「用户住在杭州」「用户是程序员」）——默认类。\n"
            "   category 只在 new/update/纠正替换时由你给出；纯 reinforce_short/keep 无需重判类型。\n\n"
            "## few-shot\n"
            + _PROFILE_FEW_SHOT + "\n\n"
            f"## existing 画像\n{json.dumps(existing, ensure_ascii=False)}\n\n"
            + mem_section +
            f"## 本轮对话\n{conversation_block}\n\n"
            "## 输出（仅 JSON）\n"
        )

    # ── Helpers ───────────────────────────────────────────────────

    def _path(self, user_id: str) -> str:
        safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in user_id)
        # New layout: {base_dir}/{safe_user_id}/profile.json
        return os.path.join(self.base_dir, safe_id, "profile.json")

    def _old_path(self, user_id: str) -> str:
        """Legacy path used before per-user workspace migration."""
        safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in user_id)
        return os.path.join(self.base_dir, f"{safe_id}.json")

    # ── ⑦ Layer-3 buffer crash recovery ──────────────────────────

    def _buffer_path(self, user_id: str) -> str:
        """{base_dir}/{safe_id}/buffer.json — sibling of profile.json."""
        return os.path.join(os.path.dirname(self._path(user_id)), "buffer.json")

    def _save_buffer(self, user_id: str):
        """Atomically mirror _recent/_pending_n to disk (tmp + os.replace).

        Called on every buffer mutation (observe_turn append, _flush reset,
        delete-flush drop). Best-effort: a failed write only degrades back to
        the old behavior (crash loses the in-memory buffer), never breaks the
        turn itself.
        """
        dq = self._recent.get(user_id)
        payload = {
            # 3-tuple (user_msg, agent_resp, persona_name) — persona survives a
            # restart so a recovered buffer still snapshots in-character. The
            # len guard tolerates any legacy 2-tuple still in the deque.
            "turns": [[t[0], t[1], (t[2] if len(t) > 2 else "")] for t in (dq or ())],
            "pending_n": int(self._pending_n.get(user_id, 0)),
        }
        path = self._buffer_path(user_id)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            os.replace(tmp, path)
        except Exception as e:
            logger.warning("[profile] buffer save failed user={}: {}", user_id, e)

    def _restore_buffer(self, user_id: str):
        """Rebuild the Layer-3 buffer from disk (lazy, on first observe).

        After a crash/restart the recovered pending_n lets the tail flush
        naturally on the next interactions instead of being silently lost.
        Corrupt/missing files are ignored (fresh buffer). Turns are re-capped
        to EXTRACT_WINDOW_N and malformed entries skipped.
        """
        try:
            with open(self._buffer_path(user_id), "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        turns = data.get("turns")
        if not isinstance(turns, list):
            return
        dq = deque(maxlen=EXTRACT_WINDOW_N)
        for t in turns[-EXTRACT_WINDOW_N:]:
            # Accept legacy 2-element turns (persona → "") and new 3-element
            # ones so a buffer.json written before the persona field still
            # restores instead of being discarded.
            if isinstance(t, (list, tuple)) and len(t) >= 2:
                persona = str(t[2]) if len(t) >= 3 else ""
                dq.append((str(t[0]), str(t[1]), persona))
        try:
            pending_n = max(0, int(data.get("pending_n") or 0))
        except (TypeError, ValueError):
            pending_n = 0
        if not dq and not pending_n:
            return
        self._recent[user_id] = dq
        # pending can never legitimately exceed the buffered turn count
        self._pending_n[user_id] = min(pending_n, len(dq))
        logger.debug(
            "[profile] buffer restored user={} turns={} pending={}",
            user_id, len(dq), self._pending_n[user_id],
        )

    def _load(self, user_id: str) -> Optional[UserProfile]:
        path = self._path(user_id)

        # Check new path first
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return UserProfile.from_dict(data)
            except Exception:
                return None

        # Auto-migrate from old path (flat file layout)
        old_path = self._old_path(user_id)
        if os.path.exists(old_path):
            try:
                with open(old_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                profile = UserProfile.from_dict(data)
                # Save to new location
                self.save(profile)
                # Remove old file
                try:
                    os.remove(old_path)
                except Exception:
                    pass
                return profile
            except Exception:
                return None

        return None

    @staticmethod
    def _parse_json(text: str) -> Optional[dict]:
        """Robust JSON extraction from LLM output."""
        if not text:
            return None
        # Try direct parse
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        # Try extracting from markdown code fence
        match = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass
        # Try finding JSON object braces
        match = re.search(r'\{[^{}]*\}', text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
        return None

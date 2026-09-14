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


# Few-shot 反例/正例，嵌入抽取 prompt（§10 Layer 1）。三个反例 → {}，覆盖
# 三类高频垃圾根因（截图/助手解读当证据、一次性抽卡结果、bot 状态）；一个正例
# 演示「用户自述持久兴趣」该抽取。反例是主力——模型已证明会无视纯文字禁令。
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
输出: {"new_interests": ["原神"]}
（用户本人陈述的持久兴趣，石蕊测试通过。）"""


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
        os.makedirs(base_dir, exist_ok=True)
        self._cache: Dict[str, UserProfile] = {}

        # ── Layer 3 batching state (per-user, in-memory only) ──
        # _recent: rolling context window (maxlen=EXTRACT_WINDOW_N) of turns,
        #          used to build the <conversation> block (batch + 3 preceding
        #          turns so "该项目"-style references can be resolved).
        # _pending_n: count of buffered turns not yet extracted; flush at >= K.
        # _inflight: users with an extract_batch task currently running
        #            (single-flight — never two extractions for one user).
        # _force_flush: users whose pending tail must be flushed at next idle
        #            even if < K (set by delete-flush on a substantive session).
        self._recent: Dict[str, "deque[Tuple[str, str]]"] = {}
        self._pending_n: Dict[str, int] = {}
        self._inflight: set = set()
        self._force_flush: set = set()

    def set_client(self, client):
        """Set or update the LLM client (for lazy initialization)."""
        self.client = client

    # ── CRUD ──────────────────────────────────────────────────────

    def get(self, user_id: str) -> UserProfile:
        """Get or create a user profile."""
        if user_id in self._cache:
            return self._cache[user_id]

        profile = self._load(user_id)
        if profile is None:
            profile = UserProfile(user_id=user_id)

        self._cache[user_id] = profile
        return profile

    def save(self, profile: UserProfile):
        """Persist a profile to disk."""
        self._cache[profile.user_id] = profile
        path = self._path(profile.user_id)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(profile.to_dict(), f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    # ── Layer 3: batched observation + extraction (§7.1, P0) ──────

    def observe_turn(self, user_id: str, user_message: str, agent_response: str):
        """Per-turn hook (SYNC, no await): buffer the turn + touch the profile.

        Called from agent._schedule_profile_update on every turn. When the
        buffer reaches PROFILE_BATCH_K and no extraction is in flight for this
        user, a single-flight extract_batch is scheduled over the recent window.

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

        dq = self._recent.get(user_id)
        if dq is None:
            dq = deque(maxlen=EXTRACT_WINDOW_N)
            self._recent[user_id] = dq
        dq.append((user_message or "", agent_response or ""))

        self._pending_n[user_id] = self._pending_n.get(user_id, 0) + 1
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
            logger.debug(
                "[profile] delete-flush drop tail user={} turns={}",
                user_id, session_turn_count,
            )

    def _flush(self, user_id: str):
        """Snapshot the recent window and schedule a single-flight extract_batch."""
        window = list(self._recent.get(user_id, ()))
        self._pending_n[user_id] = 0
        self._force_flush.discard(user_id)
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
        conversation_block = self._format_conversation(turns)
        prompt = self._build_extraction_prompt(conversation_block, existing)

        result = await self.client.chat_completion(
            prompt, timeout_set=30.0, purpose="profile",
        )
        extracted = self._parse_json(result)
        if not extracted:
            return

        # ── synchronous atomic apply (no await between read and write) ──
        if self._apply_extraction(profile, user_id, extracted):
            self.save(profile)
            logger.debug(
                "[profile] batch-updated user_id={} turns={}", user_id, len(turns)
            )

    async def extract_and_update(self, user_id: str, user_message: str, agent_response: str):
        """Single-turn convenience wrapper over extract_batch (backward-compat).

        The runtime path now uses observe_turn() (buffered batching, Layer 3).
        This extracts ONE turn immediately — kept for direct testing and any
        caller that wants per-turn extraction without batching.
        """
        if not self.client:
            return
        profile = self.get(user_id)
        profile.touch()
        await self.extract_batch(user_id, [(user_message or "", agent_response or "")])

    def _apply_extraction(self, profile: UserProfile, user_id: str, extracted: dict) -> bool:
        """Merge a parsed extraction dict into the profile. Returns changed?

        Synchronous, no await — runs as an atomic block after all LLM calls have
        completed (§12.1). Interests pass the Layer-2 deterministic filter
        (fact_filter) before they are merged; every drop is logged for
        observability-driven blacklist tuning (§10/§12.2).
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

        return changed

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
        for um, ar in reversed(list(turns)):
            u = (um or "")[:EXTRACT_USER_MSG_CAP]
            a = (ar or "")[:EXTRACT_AGENT_RESP_CAP]
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
    def _build_extraction_prompt(conversation_block: str, existing: dict) -> str:
        """Build the Layer 1 profile-extraction prompt (§10).

        石蕊测试为首要判据 + few-shot 反例 + 自包含 + agent_response 禁当证据。
        只产出 nickname / new_interests / new_preferences（无 new_facts）。
        """
        return (
            "你是用户画像抽取器。从下面这轮对话中，抽取关于【用户本人】的、持久的画像信息。\n"
            "只返回合法 JSON，不要解释，不要 markdown 代码围栏。\n\n"
            "## 首要判据：石蕊测试\n"
            "对每一条候选，先问自己：\n"
            "  「如果这个用户从此再也不碰这个 bot，这条还成立吗？」\n"
            "成立 → 才可能是持久画像；不成立 → 直接丢弃。\n"
            "这一条即可干掉：工作区/磁盘用量、本轮抽卡结果、截图测速、"
            "「正在参与特殊会话」、bot 的口癖或角色扮演称谓。\n\n"
            "## 硬性规则\n"
            "1. 【助手(Roxy)】说的话只能用于消歧，绝不能当作关于用户的证据。"
            "助手回复可能含角色扮演称谓、假设、对截图/图片的解读——都不是用户的事实。\n"
            "2. 游戏名、角色归属、个人身份等，必须来自【用户】自己说的话，"
            "不能由用户上传的截图/图片/文件内容反推。一张战斗截图描述的是截图内容，不是用户。\n"
            "3. 每条候选必须【自包含】：把指代（该项目/这个/上面说的）解析成明确的命名实体；"
            "无法解析成一句独立成立的话，就丢弃。\n"
            "4. 只抽取【新的】、existing 画像里没有的信息。\n"
            "5. 客观、不臆测：记「玩原神」，不记「可能是开发者」。\n"
            "6. 只抽取关于用户的，不抽取关于助手(Roxy)的。\n"
            "7. 没有新信息就返回 {}。\n\n"
            "## 输出 schema（字段均可选，无则省略）\n"
            "{\n"
            '  "nickname": "用户提到的名字或希望被怎样称呼",\n'
            '  "new_interests": ["用户表现出的持久兴趣/话题", ...],\n'
            '  "new_preferences": {"语言/回复风格等偏好键": "偏好值"}\n'
            "}\n\n"
            "## few-shot\n"
            + _PROFILE_FEW_SHOT + "\n\n"
            f"## existing 画像\n{json.dumps(existing, ensure_ascii=False)}\n\n"
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

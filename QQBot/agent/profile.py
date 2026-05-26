"""
User Profile — Per-user profile management with LLM-driven fact extraction.

Each user has a profile stored at data/users/{user_id}.json containing:
- Basic info (nickname, first/last seen)
- Preferences (language, response style)
- Discovered facts (location, occupation, tools used, etc.)
- Interests (topics the user cares about)

The profile is:
1. Loaded and injected into the system prompt each turn
2. Updated asynchronously after each conversation via LLM fact extraction
"""

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


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
        """Generate the user context block to inject into the system prompt."""
        parts = ["## 当前用户信息"]

        if self.nickname:
            parts.append(f"用户称呼: {self.nickname}")

        if self.facts:
            parts.append("已知信息:")
            for fact in self.facts[-10:]:  # Most recent 10 facts
                parts.append(f"  - {fact}")

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
        """Add new facts, avoiding duplicates (fuzzy)."""
        for fact in new_facts:
            if not any(self._similar(fact, existing) for existing in self.facts):
                self.facts.append(fact)

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


class ProfileManager:
    """Manages user profiles with persistence and LLM-driven fact extraction."""

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
            with open(path, "w", encoding="utf-8") as f:
                json.dump(profile.to_dict(), f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    # ── LLM Fact Extraction ──────────────────────────────────────

    async def extract_and_update(self, user_id: str, user_message: str, agent_response: str):
        """Asynchronously extract user facts from the conversation and update profile.

        This is designed to be called as a background task after the agent responds.
        It does NOT block the response to the user.
        """
        if not self.client:
            return

        profile = self.get(user_id)
        profile.touch()

        # Build extraction prompt
        existing = {
            "nickname": profile.nickname,
            "facts": profile.facts[-10:],
            "interests": profile.interests,
            "preferences": profile.preferences,
        }

        prompt = (
            "Analyze the conversation below and extract any new information about the user. "
            "Return ONLY valid JSON, no explanation, no markdown fences.\n\n"
            f"User message: {user_message[:500]}\n\n"
            f"Your response: {agent_response[:500]}\n\n"
            f"Existing profile: {json.dumps(existing, ensure_ascii=False)}\n\n"
            "Return JSON with these fields (all optional, omit if empty):\n"
            '{\n'
            '  "nickname": "name if user mentioned their name or how they want to be called",\n'
            '  "new_facts": ["objective fact about the user", ...],\n'
            '  "new_interests": ["topic user showed interest in", ...],\n'
            '  "new_preferences": {"pref_key": "pref_value"}\n'
            '}\n\n'
            "Guidelines:\n"
            "- Only include NEW information not already in the existing profile.\n"
            "- Facts should be objective, not speculative. E.g., 'uses Python' not 'might be a developer'.\n"
            "- If nothing new is discovered, return {}.\n"
            "- Do NOT extract facts about the assistant (Roxy), only about the user."
        )

        try:
            result = await self.client.chat_completion(prompt, timeout_set=30.0)
            extracted = self._parse_json(result)

            if not extracted:
                return

            changed = False

            if extracted.get("nickname") and not profile.nickname:
                profile.nickname = str(extracted["nickname"])[:50]
                changed = True

            new_facts = extracted.get("new_facts", [])
            if new_facts:
                before = len(profile.facts)
                profile.merge_facts([str(f)[:200] for f in new_facts])
                if len(profile.facts) > before:
                    changed = True

            new_interests = extracted.get("new_interests", [])
            if new_interests:
                profile.merge_interests([str(i)[:50] for i in new_interests])
                changed = True

            new_prefs = extracted.get("new_preferences", {})
            if new_prefs and isinstance(new_prefs, dict):
                profile.merge_preferences({str(k)[:50]: str(v)[:100] for k, v in new_prefs.items()})
                changed = True

            if changed:
                self.save(profile)

        except Exception:
            pass  # Non-critical background task — never crash

    # ── Helpers ───────────────────────────────────────────────────

    def _path(self, user_id: str) -> str:
        safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in user_id)
        return os.path.join(self.base_dir, f"{safe_id}.json")

    def _load(self, user_id: str) -> Optional[UserProfile]:
        path = self._path(user_id)
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return UserProfile.from_dict(data)
        except Exception:
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

"""
Personality Manager — Load and manage agent personality profiles.

Each personality is a markdown file in config/personalities/.
The first # heading in the file is used as the display name.
Content is injected at the top of the system prompt.

Follows OpenRubi's philosophy: personality switching is just
swapping a simple prompt string — no complex state machines.
"""

import json
import os
import re
from typing import Dict, List, Optional


# ── Paths ────────────────────────────────────────────────────────

_PERSONALITIES_DIR = os.path.join(
    os.path.dirname(__file__), "config", "personalities"
)
_SETTINGS_FILE = os.path.join(
    os.path.dirname(__file__), "..", "data", "personality_settings.json"
)
_DEFAULT_CONFIG_FILE = os.path.join(
    os.path.dirname(__file__), "..", "data", "personality_config.json"
)
_GROUP_CONFIG_FILE = os.path.join(
    os.path.dirname(__file__), "..", "data", "group_personality.json"
)


# ── PersonalityManager ────────────────────────────────────────────

class PersonalityManager:
    """Load, list, and manage personality profiles."""

    def __init__(self, personalities_dir: str = None):
        self._dir = personalities_dir or _PERSONALITIES_DIR
        self._cache: Dict[str, str] = {}  # name → raw markdown content

    # ── Discovery ─────────────────────────────────────────────────

    def _discover(self) -> List[str]:
        """Return list of personality file stems (name keys)."""
        if not os.path.isdir(self._dir):
            return []
        files = []
        for f in sorted(os.listdir(self._dir)):
            if f.endswith(".md") and not f.startswith("_"):
                files.append(f[:-3])  # Strip .md extension
        return files

    # ── Load ──────────────────────────────────────────────────────

    def load(self, name: str) -> Optional[str]:
        """Load a personality's raw markdown content.

        Caches in memory after first load. Returns None if not found.
        """
        if name in self._cache:
            return self._cache[name]

        path = os.path.join(self._dir, f"{name}.md")
        if not os.path.isfile(path):
            return None

        with open(path, "r", encoding="utf-8") as f:
            content = f.read().strip()

        if not content:
            return None

        self._cache[name] = content
        return content

    def get_display_name(self, name: str) -> str:
        """Extract the display name from a personality file.

        Uses the first # heading, falling back to the file stem.
        """
        content = self.load(name)
        if content:
            for line in content.split("\n"):
                line = line.strip()
                if line.startswith("# ") and not line.startswith("## "):
                    return line[2:].strip()
        return name

    def get_short_name(self, name: str) -> str:
        """Extract a short name from the display name for use in messages.

        "助手 Roxy" → "Roxy"
        "露比 (Rubi)" → "露比"
        "角色 Roxy (无职转生)" → "Roxy"
        """
        display = self.get_display_name(name)
        # Remove parenthetical at end: "露比 (Rubi)" → "露比"
        display = re.sub(r'\s*\([^)]*\)\s*$', '', display).strip()
        # Take the last word as the short name
        parts = display.split()
        return parts[-1] if parts else display

    # ── List ──────────────────────────────────────────────────────

    def list_personalities(self) -> List[dict]:
        """Return [{key, display_name, loaded}] for all personalities."""
        result = []
        for key in self._discover():
            result.append({
                "key": key,
                "display_name": self.get_display_name(key),
            })
        return result

    def get_default(self) -> str:
        """Return the default personality key.

        Reads ``data/personality_config.json`` (``default`` field). If the
        config is missing/invalid, or the configured key no longer exists,
        falls back to the first available personality, then "assistant".
        """
        default = self._load_default_key()
        available = self._discover()
        if default in available:
            return default
        return available[0] if available else "assistant"

    def _load_default_key(self) -> str:
        """Read the default personality key from data/personality_config.json."""
        try:
            with open(_DEFAULT_CONFIG_FILE, "r", encoding="utf-8") as f:
                config = json.load(f)
                key = config.get("default")
                if isinstance(key, str) and key.strip():
                    return key.strip()
        except (FileNotFoundError, json.JSONDecodeError, IOError):
            pass
        return "assistant"

    def resolve_name(self, name: str) -> Optional[str]:
        """Resolve a user-provided name to a personality key.

        Matching order:
        1. Exact key match (e.g., "rubi" → "rubi")
        2. Case-insensitive key match (e.g., "Rubi" → "rubi")
        3. Exact display name match (e.g., "助手 Roxy" → assistant)
        4. Display name substring match — must be unique (e.g., "露比" → rubi);
           "Roxy" is ambiguous between 助手 Roxy / 角色 Roxy, so it resolves to None
        5. Unique partial key match (e.g., "rub" → "rubi" if unique)

        Returns the matched key, or None if no match or ambiguous.
        """
        available = self._discover()
        if not available:
            return None

        name_lower = name.lower().strip()

        # 1. Exact key match
        if name in available:
            return name

        # 2. Case-insensitive key match
        for key in available:
            if key.lower() == name_lower:
                return key

        # 3. Exact display name match (full name, case-insensitive)
        exact = [k for k in available if self.get_display_name(k).lower() == name_lower]
        if len(exact) == 1:
            return exact[0]

        # 4. Display name substring match — reject ambiguity
        display_matches = [
            k for k in available
            if name_lower in self.get_display_name(k).lower()
        ]
        if len(display_matches) == 1:
            return display_matches[0]
        if len(display_matches) > 1:
            return None  # ambiguous — don't silently pick one

        # 5. Unique partial key match
        key_matches = [k for k in available if name_lower in k.lower()]
        if len(key_matches) == 1:
            return key_matches[0]

        return None

    def _resolve_or_raise(self, name: str) -> str:
        """Resolve name, or raise ValueError with a helpful message.

        Distinguishes "no match" from "ambiguous" so users get an
        actionable hint (e.g., 助手 vs 角色) instead of a wrong pick.
        """
        available = self._discover()
        if not available:
            raise ValueError("没有可用的人格配置。")

        resolved = self.resolve_name(name)
        if resolved is not None:
            return resolved

        name_lower = name.lower().strip()
        matches = [
            k for k in available
            if name_lower in self.get_display_name(k).lower()
        ]
        examples = "\n  ".join(
            f"{self.get_display_name(k)} ({k})" for k in available
        )
        if len(matches) > 1:
            raise ValueError(
                f"「{name}」匹配到多个人格，请更具体。\n\n可用人格:\n  {examples}"
            )
        raise ValueError(
            f"未找到匹配「{name}」的人格。\n\n可用人格:\n  {examples}"
        )

    # ── Settings Persistence ──────────────────────────────────────

    def _load_settings(self) -> dict:
        """Load per-user personality settings from JSON."""
        if not os.path.exists(_SETTINGS_FILE):
            return {}
        try:
            with open(_SETTINGS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {}

    def _save_settings(self, settings: dict):
        """Persist per-user personality settings to JSON."""
        os.makedirs(os.path.dirname(_SETTINGS_FILE), exist_ok=True)
        with open(_SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)

    def _load_group_settings(self) -> dict:
        """Load per-group personality bindings from JSON."""
        if not os.path.exists(_GROUP_CONFIG_FILE):
            return {}
        try:
            with open(_GROUP_CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {}

    def _save_group_settings(self, settings: dict):
        """Persist per-group personality bindings to JSON."""
        os.makedirs(os.path.dirname(_GROUP_CONFIG_FILE), exist_ok=True)
        with open(_GROUP_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)

    def get_user_personality(self, user_id: str) -> str:
        """Get the active personality for a user. Default: first available."""
        settings = self._load_settings()
        name = settings.get(user_id, self.get_default())
        # Validate that the personality still exists
        if name not in self._discover():
            return self.get_default()
        return name

    def get_personal_personality(self, user_id: str) -> Optional[str]:
        """Return the user's explicit personal setting, or None if unset."""
        settings = self._load_settings()
        name = settings.get(user_id)
        if name and name in self._discover():
            return name
        return None

    def set_user_personality(self, user_id: str, name: str) -> str:
        """Set the active personality for a user.

        Supports fuzzy matching: key, display name, or partial match.
        Returns the resolved personality key.
        Raises ValueError with helpful message if no match or ambiguous.
        """
        resolved = self._resolve_or_raise(name)
        settings = self._load_settings()
        settings[user_id] = resolved
        self._save_settings(settings)
        return resolved

    # ── Group-Bound Default Personality ────────────────────────────

    def get_group_personality(self, group_id: str) -> Optional[str]:
        """Return the personality key bound to a group, or None if unset.

        Validates that the stored key still exists; invalid/removed keys
        are treated as unset.
        """
        if not group_id:
            return None
        settings = self._load_group_settings()
        name = settings.get(group_id)
        if name and name in self._discover():
            return name
        return None

    def set_group_personality(self, group_id: str, name: str) -> str:
        """Bind a default personality to a group.

        Supports fuzzy matching via resolve_name. Returns the resolved key.
        Raises ValueError if no match or ambiguous.
        """
        resolved = self._resolve_or_raise(name)
        settings = self._load_group_settings()
        settings[group_id] = resolved
        self._save_group_settings(settings)
        return resolved

    def clear_group_personality(self, group_id: str):
        """Remove a group's default personality binding."""
        settings = self._load_group_settings()
        if group_id in settings:
            del settings[group_id]
            self._save_group_settings(settings)

    def resolve_effective_personality(
        self, user_id: str, group_id: str = None
    ) -> str:
        """Resolve the personality that should be in effect.

        Priority: user override > group default > global default.
        Falls back to the first available personality, then "assistant".
        """
        # 1. User override
        settings = self._load_settings()
        name = settings.get(user_id)
        if name and name in self._discover():
            return name

        # 2. Group default (only when group_id provided)
        group_name = self.get_group_personality(group_id or "")
        if group_name:
            return group_name

        # 3. Global default
        return self.get_default()


# ── Module-level singleton ────────────────────────────────────────

_personality_manager: Optional[PersonalityManager] = None


def get_personality_manager() -> PersonalityManager:
    """Get or create the module-level PersonalityManager singleton."""
    global _personality_manager
    if _personality_manager is None:
        _personality_manager = PersonalityManager()
    return _personality_manager

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
        """Return the default personality key."""
        available = self._discover()
        return available[0] if available else "assistant"

    def resolve_name(self, name: str) -> Optional[str]:
        """Resolve a user-provided name to a personality key.

        Matching order:
        1. Exact key match (e.g., "rubi" → "rubi")
        2. Case-insensitive key match (e.g., "Rubi" → "rubi")
        3. Display name substring match (e.g., "露比" matches "露比 (Rubi)")
        4. Case-insensitive display name match
        5. Unique partial key match (e.g., "rub" → "rubi" if unique)
        6. Unique partial display name match

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

        # 3. Display name substring match (e.g., "露比" matches "露比 (Rubi)")
        for key in available:
            display = self.get_display_name(key)
            if name in display:
                return key

        # 4. Case-insensitive display name match
        for key in available:
            display_lower = self.get_display_name(key).lower()
            if name_lower in display_lower:
                return key

        # 5. Unique partial key match
        key_matches = [k for k in available if name_lower in k.lower()]
        if len(key_matches) == 1:
            return key_matches[0]

        # 6. Unique partial display name match
        display_matches = [
            k for k in available
            if name_lower in self.get_display_name(k).lower()
        ]
        if len(display_matches) == 1:
            return display_matches[0]

        return None

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

    def get_user_personality(self, user_id: str) -> str:
        """Get the active personality for a user. Default: first available."""
        settings = self._load_settings()
        name = settings.get(user_id, self.get_default())
        # Validate that the personality still exists
        if name not in self._discover():
            return self.get_default()
        return name

    def set_user_personality(self, user_id: str, name: str):
        """Set the active personality for a user.

        Supports fuzzy matching: key, display name, or partial match.
        Raises ValueError with helpful message if no match or ambiguous.
        """
        resolved = self.resolve_name(name)
        if resolved is None:
            available = self._discover()
            if not available:
                raise ValueError("没有可用的人格配置。")
            examples = []
            for k in available:
                d = self.get_display_name(k)
                examples.append(f"{d} ({k})")
            raise ValueError(
                f"未找到匹配「{name}」的人格。\n\n可用人格:\n  " + "\n  ".join(examples)
            )
        settings = self._load_settings()
        settings[user_id] = resolved
        self._save_settings(settings)


# ── Module-level singleton ────────────────────────────────────────

_personality_manager: Optional[PersonalityManager] = None


def get_personality_manager() -> PersonalityManager:
    """Get or create the module-level PersonalityManager singleton."""
    global _personality_manager
    if _personality_manager is None:
        _personality_manager = PersonalityManager()
    return _personality_manager

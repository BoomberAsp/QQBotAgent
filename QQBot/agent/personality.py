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
        """Set the active personality for a user."""
        if name not in self._discover():
            raise ValueError(
                f"未知人格: '{name}'。可用: {', '.join(self._discover())}"
            )
        settings = self._load_settings()
        settings[user_id] = name
        self._save_settings()


# ── Module-level singleton ────────────────────────────────────────

_personality_manager: Optional[PersonalityManager] = None


def get_personality_manager() -> PersonalityManager:
    """Get or create the module-level PersonalityManager singleton."""
    global _personality_manager
    if _personality_manager is None:
        _personality_manager = PersonalityManager()
    return _personality_manager

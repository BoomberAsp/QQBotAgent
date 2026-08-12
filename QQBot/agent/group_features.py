"""
Group Feature Manager — Per-group feature toggles.

Superusers can enable/disable features (gacha, image sending, voice
recognition) per group chat via /toggle commands. Feature state is
persisted to data/group_features.json.

Default: all features are enabled for all groups (opt-out model).
"""

import json
import os
from pathlib import Path
from typing import Dict, Set


# ── Feature Definitions ────────────────────────────────────────────

# Feature key → display name (used in /toggle output and system prompt)
FEATURE_LABELS: Dict[str, str] = {
    "gacha": "抽卡功能",
    "image": "图片发送",
    "voice": "语音识别",
}

# Feature key → set of tool names to filter when disabled
FEATURE_TOOLS: Dict[str, Set[str]] = {
    "gacha": {"gacha_pull", "play_gacha_animation"},
    "image": {"play_gacha_animation"},
    "voice": set(),  # Hook for future voice recognition tools
}

# Ordered for display
FEATURE_ORDER = ["gacha", "image", "voice"]


class GroupFeatureManager:
    """Per-group feature toggle manager.

    Features default to ON. Superusers toggle them off/on per group.
    """

    def __init__(self, data_dir: str = None):
        if data_dir is None:
            data_dir = os.path.join(
                os.path.dirname(__file__), "..", "data"
            )
        self._config_path = os.path.join(data_dir, "group_features.json")
        self._config: Dict[str, Dict[str, bool]] = {}
        self._load()

    # ── File I/O ──────────────────────────────────────────────────

    def _load(self):
        """Load feature config from JSON file."""
        if os.path.exists(self._config_path):
            try:
                with open(self._config_path, "r", encoding="utf-8") as f:
                    self._config = json.load(f)
            except (json.JSONDecodeError, IOError):
                self._config = {}
        else:
            self._config = {}

    def refresh(self):
        """Re-read config from disk. Call before making feature queries.

        Ensures the singleton sees changes made by other processes
        or previous requests."""
        self._load()

    def _save(self):
        """Persist feature config to JSON file."""
        os.makedirs(os.path.dirname(self._config_path), exist_ok=True)
        with open(self._config_path, "w", encoding="utf-8") as f:
            json.dump(self._config, f, ensure_ascii=False, indent=2)

    # ── Feature Checks ────────────────────────────────────────────

    def is_enabled(self, group_id: str, feature: str) -> bool:
        """Check if a feature is enabled for a group. Default: True."""
        if feature not in FEATURE_LABELS:
            return True  # Unknown features default to enabled
        return self._config.get(group_id, {}).get(feature, True)

    def get_disabled_features(self, group_id: str) -> Dict[str, str]:
        """Return {feature_key: display_name} for all disabled features."""
        disabled = {}
        for key in FEATURE_ORDER:
            if not self.is_enabled(group_id, key):
                disabled[key] = FEATURE_LABELS[key]
        return disabled

    def get_disabled_tools(self, group_id: str) -> Set[str]:
        """Return set of tool names to remove from allowed_tools."""
        tools = set()
        for feature in FEATURE_ORDER:
            if not self.is_enabled(group_id, feature):
                tools.update(FEATURE_TOOLS.get(feature, set()))
        return tools

    def get_disabled_context(self, group_id: str) -> str:
        """Build the system-prompt context message for disabled features.

        Returns an empty string if no features are disabled.
        """
        disabled = self.get_disabled_features(group_id)
        if not disabled:
            return ""

        lines = [
            "\n## 当前群聊功能限制",
            "以下功能已被机器人超级用户在本群关闭：",
        ]
        for key in FEATURE_ORDER:
            if key in disabled:
                lines.append(f"- {FEATURE_LABELS[key]}：已关闭")
        lines.append(
            "如果用户请求这些功能，请礼貌告知该功能在当前群聊已被机器人超级用户限制，"
            "而不是群管理员。不要在受限功能上提供替代方案或模拟结果。"
        )
        return "\n".join(lines)

    # ── Toggle ────────────────────────────────────────────────────

    def set_feature(self, group_id: str, feature: str, enabled: bool):
        """Enable or disable a feature for a group."""
        if feature not in FEATURE_LABELS:
            raise ValueError(f"未知功能: '{feature}'。可选: {', '.join(FEATURE_ORDER)}")

        if group_id not in self._config:
            self._config[group_id] = {}

        current = self._config[group_id].get(feature, True)
        if current == enabled:
            return  # No change

        self._config[group_id][feature] = enabled

        # Clean up: remove group entry if all features are back to default (enabled)
        all_default = all(
            self._config[group_id].get(f, True) is True
            for f in FEATURE_LABELS
        )
        if all_default:
            del self._config[group_id]

        self._save()

    def get_all_features(self, group_id: str) -> Dict[str, bool]:
        """Return {feature_key: enabled} for all features in a group."""
        return {
            key: self.is_enabled(group_id, key)
            for key in FEATURE_ORDER
        }

    # ── Superuser Check ───────────────────────────────────────────

    def is_superuser(self, user_id: str) -> bool:
        """Check if a user is a superuser (admin)."""
        from agent.permissions import PermissionManager
        # Lazy import to avoid circular dependency at module level
        pm = PermissionManager()
        from agent.permissions import UserRole
        return pm.get_role(user_id) == UserRole.ADMIN


# ── Module-level singleton ────────────────────────────────────────

_group_features: GroupFeatureManager | None = None


def get_group_features() -> GroupFeatureManager:
    """Get or create the module-level GroupFeatureManager singleton."""
    global _group_features
    if _group_features is None:
        _group_features = GroupFeatureManager()
    return _group_features

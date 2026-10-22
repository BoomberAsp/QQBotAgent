"""
Permission Manager — Three-tier user permission system.

Roles:
- ADMIN: Full access (SUPERUSERS env var)
- VIP: Elevated access without server-level tools (VIP_USERS env var)
- REGULAR: Basic info tools only (default)

Tool categories:
- Public: search, time, weather, file reading (text/PDF), PDF summary,
  map/geolocation, entertainment (gacha, speed calc, code explain, translation)
- VIP extra: web_fetch, download_repo, get_system_load, execute_code (limited),
  read_file image/audio AI analysis
- Admin only: shell_exec
"""

import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Dict, Set


class UserRole(Enum):
    ADMIN = "admin"
    VIP = "vip"
    REGULAR = "regular"


@dataclass
class CodeLimits:
    """Tiered resource limits for execute_code."""
    max_timeout: int       # seconds
    max_output: int        # bytes
    max_memory_mb: int     # memory limit hint

    def to_dict(self) -> dict:
        return {
            "max_timeout": self.max_timeout,
            "max_output": self.max_output,
            "max_memory_mb": self.max_memory_mb,
        }


# ── Tool Categories ────────────────────────────────────────────────

# Tools available to ALL users
_PUBLIC_TOOLS: Set[str] = {
    "get_user_info",
    "delete_workspace_file",
    "search_web",
    "get_time",
    "get_weather",
    "read_file",           # Only text/PDF for regular; multimodal requires VIP
    "summarize_pdf",
    "geocode",
    "reverse_geocode",
    "search_poi",
    "plan_route",
    "gacha_pull",
    "play_gacha_animation",
    "calculate_speed",
    "compare_speed_probability",
    "explain_code",
    "translate_text",
    "character_detail",
    "bond_detail",
    "parse_battle_screenshots",
    "redeem_code",
    "get_changelog",         # 机器人自身的更新记录，所有用户可查
    "begin_task",
    "finalize_subtask",
    "recall_search_result",  # Reads own search archives only (user_id-scoped)
    "recall_memory",         # Reads own LONG-tier memories only (user_id-scoped)
}

# Additional tools for VIP users (on top of _PUBLIC_TOOLS)
_VIP_TOOLS: Set[str] = {
    "web_fetch",
    "download_repo",
    "get_system_load",
    "execute_code",        # Limited: shorter timeout, reduced output, basic imports only
}

# Additional tools for ADMIN users (on top of _VIP_TOOLS)
_ADMIN_TOOLS: Set[str] = {
    "shell_exec",
}


class PermissionManager:
    """Manages user roles and tool access permissions.

    Identity is resolved from environment variables:
    - SUPERUSERS: comma-separated QQ IDs for admin
    - VIP_USERS: comma-separated QQ IDs for VIP
    - Everyone else: regular
    """

    def __init__(self):
        # NOTE: We do NOT cache SUPERUSERS/VIP_USERS in __init__ because
        # NoneBot2 may load .env lazily (after plugin imports). Instead,
        # env vars are re-read on every get_role() call.

        # Pre-compute tool sets per role (static — does not depend on env)
        self._role_tools: Dict[UserRole, Set[str]] = {
            UserRole.ADMIN: _PUBLIC_TOOLS | _VIP_TOOLS | _ADMIN_TOOLS,
            UserRole.VIP: _PUBLIC_TOOLS | _VIP_TOOLS,
            UserRole.REGULAR: _PUBLIC_TOOLS.copy(),
        }

        # execute_code limits per role
        # max_output capped at 16KB for all roles (Cache-Plan Phase6-B1):
        # oversized stdout is re-paid at full price every in-run iteration
        # and the model cannot digest 25K+ tokens of raw output anyway.
        self._code_limits: Dict[UserRole, CodeLimits] = {
            UserRole.ADMIN: CodeLimits(max_timeout=60, max_output=16 * 1024, max_memory_mb=256),
            UserRole.VIP: CodeLimits(max_timeout=15, max_output=16 * 1024, max_memory_mb=128),
        }

        # Resource quotas per role
        self._quotas: Dict[UserRole, int] = {
            UserRole.ADMIN: 2048,   # 2 GB
            UserRole.VIP: 500,      # 500 MB
            UserRole.REGULAR: 100,  # 100 MB
        }

        # Special session limits per role
        self._max_sessions: Dict[UserRole, int] = {
            UserRole.ADMIN: 10,
            UserRole.VIP: 3,
            UserRole.REGULAR: 1,
        }

        # UGC character creation quota per role (Bond-System-Design.md §5.2)
        self._max_characters: Dict[UserRole, int] = {
            UserRole.ADMIN: 10,
            UserRole.VIP: 3,
            UserRole.REGULAR: 1,
        }

    # Resolve the project .env path at module load time
    # permissions.py is at QQBot/agent/permissions.py, so parent.parent = QQBot/
    _ENV_FILE = Path(__file__).resolve().parent.parent / ".env"

    # ── Identity Resolution ───────────────────────────────────────

    @staticmethod
    def _parse_id_list(raw: str) -> Set[str]:
        """Parse a comma-separated id list WITHOUT the digit-only filter.

        Official-route identities are openids (alphanumeric, opaque) —
        _parse_qq_list's isdigit() check would reject every one of them.
        Same JSON-bracket/quote stripping otherwise.
        """
        if not raw or not raw.strip():
            return set()
        raw = raw.strip()
        if raw.startswith("[") and raw.endswith("]"):
            raw = raw[1:-1]
        raw = raw.replace('"', '').replace("'", "")
        return {part.strip() for part in raw.split(",") if part.strip()}

    @staticmethod
    def _parse_qq_list(raw: str) -> Set[str]:
        """Parse a comma/space-separated list of QQ IDs.

        Handles JSON-like formats: ["123", "456"] or plain: 123,456
        """
        if not raw or not raw.strip():
            return set()

        raw = raw.strip()

        # Strip JSON array brackets if present
        if raw.startswith("[") and raw.endswith("]"):
            raw = raw[1:-1]

        # Strip quotes
        raw = raw.replace('"', '').replace("'", "")

        ids = set()
        for part in raw.split(","):
            part = part.strip()
            if part and part.isdigit():
                ids.add(part)
        return ids

    @classmethod
    def _read_env(cls, key: str) -> str:
        """Read a value from the .env file directly.

        NoneBot2 may not load .env values into os.environ (it uses its
        own pydantic config model instead). Reading the file directly
        ensures we always get the configured values, regardless of when
        or how NoneBot initializes.

        Falls back to os.environ for Docker/secrets-manager setups where
        env vars are injected without a .env file.
        """
        path = cls._ENV_FILE
        if path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                if k.strip() == key:
                    v = v.strip()
                    # Strip surrounding quotes
                    if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
                        v = v[1:-1]
                    return v
        # Fall back to os.environ (for Docker / production deployments)
        return os.environ.get(key, "")

    def get_role(self, user_id: str) -> UserRole:
        """Determine the user's role. Admin takes precedence over VIP.

        Reads .env file directly on every call — NoneBot2 may not expose
        .env values through os.environ.

        Stage 4 (§7.7): identity tables are per-platform. The qq_official
        route identifies users by opaque openids, so it consults
        QQ_OFFICIAL_SUPERUSERS / QQ_OFFICIAL_VIP_USERS; onebot keeps
        SUPERUSERS / VIP_USERS (QQ numbers). The platform comes from the
        _current_platform contextvar set per turn by agent.turn (default
        "onebot" — every non-turn caller, e.g. webui, keeps old behavior).
        """
        from agent.context import _current_platform
        official = _current_platform.get() == "qq_official"
        # openids are alphanumeric — the digit-only _parse_qq_list would
        # reject every one of them, so the official route uses _parse_id_list.
        parse = self._parse_id_list if official else self._parse_qq_list
        admin_key = "QQ_OFFICIAL_SUPERUSERS" if official else "SUPERUSERS"
        vip_key = "QQ_OFFICIAL_VIP_USERS" if official else "VIP_USERS"
        admins = parse(self._read_env(admin_key))
        if user_id in admins:
            return UserRole.ADMIN
        vips = parse(self._read_env(vip_key))
        if user_id in vips:
            return UserRole.VIP
        return UserRole.REGULAR

    # ── Tool Access ───────────────────────────────────────────────

    def get_allowed_tools(self, role: UserRole) -> Set[str]:
        """Return the set of tool names this role is allowed to use."""
        return self._role_tools.get(role, _PUBLIC_TOOLS)

    def can_use(self, user_id: str, tool_name: str) -> bool:
        """Check if a user can use a specific tool."""
        role = self.get_role(user_id)
        return tool_name in self.get_allowed_tools(role)

    # ── Resource Limits ───────────────────────────────────────────

    def get_code_limits(self, role: UserRole) -> CodeLimits:
        """Return execute_code limits for the given role.

        Returns None for roles that cannot use execute_code at all.
        """
        return self._code_limits.get(role)

    def get_workspace_quota_mb(self, role: UserRole) -> int:
        """Return workspace disk quota in MB for the given role."""
        return self._quotas.get(role, 100)

    def get_max_special_sessions(self, role: UserRole) -> int:
        """Return max special sessions for the given role."""
        return self._max_sessions.get(role, 1)

    def get_max_characters(self, role: UserRole) -> int:
        """Return max UGC characters the role may own."""
        return self._max_characters.get(role, 1)

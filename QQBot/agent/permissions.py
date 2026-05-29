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
        self._code_limits: Dict[UserRole, CodeLimits] = {
            UserRole.ADMIN: CodeLimits(max_timeout=60, max_output=100 * 1024, max_memory_mb=256),
            UserRole.VIP: CodeLimits(max_timeout=15, max_output=50 * 1024, max_memory_mb=128),
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

    # ── Identity Resolution ───────────────────────────────────────

    @staticmethod
    def _parse_qq_list(raw: str) -> Set[str]:
        """Parse a comma/space-separated list of QQ IDs from environment.

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

    def get_role(self, user_id: str) -> UserRole:
        """Determine the user's role. Admin takes precedence over VIP.

        Env vars are re-read on every call (not cached at init time)
        because NoneBot2 may load .env after plugin imports.
        """
        # Read env vars lazily to handle NoneBot2's deferred .env loading
        admins = self._parse_qq_list(os.environ.get("SUPERUSERS", ""))
        if user_id in admins:
            return UserRole.ADMIN
        vips = self._parse_qq_list(os.environ.get("VIP_USERS", ""))
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

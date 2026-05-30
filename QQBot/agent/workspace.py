"""
User Workspace Manager — Per-user isolated workspace directories.

Each user (QQ ID) gets their own workspace under {USER_DATA_ROOT}/{user_id}/.
Workspaces are fully isolated: a user can only access their own directory.
Disk quota enforcement prevents any single user from consuming all storage.
"""

import os
import shutil
from typing import Dict, Optional


class UserWorkspaceManager:
    """Manages per-user workspace directories with isolation and quotas."""

    def __init__(self, user_data_root: str, quota_mb: int = 500):
        """
        Args:
            user_data_root: Root directory for all user data
                            (from USER_DATA_ROOT env var).
            quota_mb: Per-user disk quota in MB.
        """
        self.user_data_root = os.path.abspath(user_data_root)
        self.quota_bytes = quota_mb * 1024 * 1024
        self._over_quota: Dict[str, bool] = {}  # user_id -> True if over quota
        os.makedirs(self.user_data_root, exist_ok=True)

    # ── Path Resolution ──────────────────────────────────────────

    def get_workspace(self, user_id: str) -> str:
        """Return the user's workspace root path.

        {USER_DATA_ROOT}/{sanitized_user_id}/workspace/
        """
        safe_id = self._safe_id(user_id)
        return os.path.join(self.user_data_root, safe_id, "workspace")

    def get_user_dir(self, user_id: str) -> str:
        """Return the user's top-level data directory.

        {USER_DATA_ROOT}/{sanitized_user_id}/
        """
        safe_id = self._safe_id(user_id)
        return os.path.join(self.user_data_root, safe_id)

    def get_sessions_dir(self, user_id: str) -> str:
        """Return the user's special sessions directory.

        {USER_DATA_ROOT}/{sanitized_user_id}/sessions/
        """
        return os.path.join(self.get_user_dir(user_id), "sessions")

    # ── Directory Management ─────────────────────────────────────

    def ensure_dirs(self, user_id: str):
        """Create all directories for a user."""
        workspace = self.get_workspace(user_id)
        sessions = self.get_sessions_dir(user_id)

        for d in [
            self.get_user_dir(user_id),
            sessions,
            workspace,
            os.path.join(workspace, "code"),
            os.path.join(workspace, "uploads"),
            os.path.join(workspace, "output"),
            os.path.join(workspace, "projects"),
        ]:
            os.makedirs(d, exist_ok=True)

    def ensure_root_dirs(self):
        """Ensure the user_data_root exists."""
        os.makedirs(self.user_data_root, exist_ok=True)

    # ── Quota Management ─────────────────────────────────────────

    def get_size(self, user_id: str) -> int:
        """Return the total bytes used by this user's directory."""
        user_dir = self.get_user_dir(user_id)
        if not os.path.exists(user_dir):
            return 0
        total = 0
        for dirpath, dirnames, filenames in os.walk(user_dir):
            for f in filenames:
                fp = os.path.join(dirpath, f)
                try:
                    total += os.path.getsize(fp)
                except OSError:
                    pass
        return total

    def check_quota(self, user_id: str, additional_bytes: int = 0) -> tuple[bool, str]:
        """Check if the user is within their disk quota.

        Returns:
            (ok, message) — ok is True if within quota or can proceed with warning.
        """
        current = self.get_size(user_id)
        projected = current + additional_bytes

        if projected < self.quota_bytes * 0.8:
            return True, ""

        usage_pct = (projected / self.quota_bytes) * 100
        usage_mb = projected / (1024 * 1024)

        if projected < self.quota_bytes:
            return True, (
                f"⚠️ 工作区使用量接近上限: {usage_mb:.0f} MB / "
                f"{self.quota_bytes // (1024 * 1024)} MB ({usage_pct:.0f}%)。"
                f"建议清理不需要的文件。"
            )

        # Over quota — hard reject
        self._over_quota[user_id] = True
        return False, (
            f"⛔ 工作区已超出配额: {usage_mb:.0f} MB / "
            f"{self.quota_bytes // (1024 * 1024)} MB ({usage_pct:.0f}%)。"
            f"请清理不需要的文件后重试。"
        )

    def is_over_quota(self, user_id: str) -> bool:
        """Check if user was previously flagged as over quota."""
        return self._over_quota.get(user_id, False)

    def clear_quota_flag(self, user_id: str):
        """Clear the over-quota flag (e.g., after user cleans up)."""
        self._over_quota.pop(user_id, None)

    def get_quota_context(self, user_id: str) -> str:
        """Generate a quota status message for the system prompt."""
        current = self.get_size(user_id)
        usage_mb = current / (1024 * 1024)
        quota_mb = self.quota_bytes // (1024 * 1024)
        pct = (current / self.quota_bytes) * 100 if self.quota_bytes > 0 else 0

        if pct >= 100:
            return (
                f"⚠️ 用户工作区已超出配额 ({usage_mb:.0f}/{quota_mb} MB, {pct:.0f}%)。"
                f"请在回复中礼貌地提醒用户清理不需要的文件。"
            )
        elif pct >= 80:
            return (
                f"用户工作区使用量: {usage_mb:.0f}/{quota_mb} MB ({pct:.0f}%) — 接近上限。"
            )
        return ""

    # ── Helpers ───────────────────────────────────────────────────

    @staticmethod
    def _safe_id(user_id: str) -> str:
        """Sanitize user ID for use as a directory name."""
        return "".join(c if c.isalnum() or c in "-_" else "_" for c in user_id)

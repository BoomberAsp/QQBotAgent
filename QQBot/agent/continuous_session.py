"""
Continuous Session Manager — Per-user group chat interaction windows.

When a user @mentions the bot in a group, a 5-minute window opens during
which the user can continue the conversation without @mentioning the bot.
Each message resets the timer. The window closes on cancel command or timeout.

Inspired by the old NoneBot matcher.pause() pattern, adapted for the
Agent architecture.
"""

import time
from dataclasses import dataclass, field
from typing import Dict, Tuple


@dataclass
class ContinuousSession:
    """A single user's continuous mode window in a specific group."""

    group_id: str
    user_id: str
    started_at: float = field(default_factory=time.time)
    expires_at: float = field(default_factory=lambda: time.time() + 300.0)

    def is_expired(self) -> bool:
        return time.time() > self.expires_at

    def touch(self, timeout_minutes: float = 5.0):
        self.expires_at = time.time() + (timeout_minutes * 60.0)


class ContinuousSessionManager:
    """Manages active continuous-mode windows for group chats.

    Sessions are keyed by (group_id, user_id) — the same user can have
    independent windows in different groups.
    """

    def __init__(self, timeout_minutes: float = 5.0):
        self.timeout_minutes = timeout_minutes
        self._sessions: Dict[Tuple[str, str], ContinuousSession] = {}

    # ── Core Operations ─────────────────────────────────────────────

    def start(self, group_id: str, user_id: str):
        """Start or renew a continuous session window.

        If the user already has an active session in this group,
        it is refreshed (touch). Otherwise a new session is created.
        """
        group_id = str(group_id)
        user_id = str(user_id)
        key = (group_id, user_id)

        if key in self._sessions:
            self._sessions[key].touch(self.timeout_minutes)
        else:
            self._sessions[key] = ContinuousSession(
                group_id=group_id,
                user_id=user_id,
            )

    def is_active(self, group_id: str, user_id: str) -> bool:
        """Check if a user has an active continuous window in this group.

        Returns True if active, False otherwise. Auto-cleans expired sessions.
        """
        group_id = str(group_id)
        user_id = str(user_id)
        key = (group_id, user_id)

        session = self._sessions.get(key)
        if session is None:
            return False

        if session.is_expired():
            del self._sessions[key]
            return False

        return True

    def touch(self, group_id: str, user_id: str):
        """Reset the expiry timer for an active session.

        No-op if the session doesn't exist or is expired.
        """
        group_id = str(group_id)
        user_id = str(user_id)
        key = (group_id, user_id)

        session = self._sessions.get(key)
        if session is None:
            return

        if session.is_expired():
            del self._sessions[key]
            return

        session.touch(self.timeout_minutes)

    def end(self, group_id: str, user_id: str):
        """Manually end a continuous session."""
        group_id = str(group_id)
        user_id = str(user_id)
        key = (group_id, user_id)
        self._sessions.pop(key, None)

    # ── Maintenance ─────────────────────────────────────────────────

    def cleanup(self) -> int:
        """Remove all expired sessions. Returns count of removed."""
        expired_keys = [
            key
            for key, session in self._sessions.items()
            if session.is_expired()
        ]
        for key in expired_keys:
            del self._sessions[key]
        return len(expired_keys)

    def active_count(self) -> int:
        """Number of currently active sessions (includes cleanup)."""
        self.cleanup()
        return len(self._sessions)

    def get_remaining_seconds(self, group_id: str, user_id: str) -> float:
        """Get remaining time in seconds for a session. Returns 0 if inactive."""
        group_id = str(group_id)
        user_id = str(user_id)
        key = (group_id, user_id)
        session = self._sessions.get(key)
        if session is None or session.is_expired():
            return 0.0
        return max(0.0, session.expires_at - time.time())

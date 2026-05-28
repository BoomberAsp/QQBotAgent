"""
Session Manager — Per-user conversation session state.

Each session holds:
- conversation context (message history for the LLM)
- metadata (timestamps, counters)
- optional persistence to disk
"""

import json
import os
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Session:
    """A single user's conversation session."""

    user_id: str
    context: List[Dict[str, str]] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)
    tool_call_count: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def add_message(self, role: str, content: str, reasoning_content: str = None):
        """Append a message to the conversation context.

        Args:
            role: Message role (user, assistant, system, tool).
            content: Message content text.
            reasoning_content: Optional thinking chain content from LLM.
                               Preserved for APIs that require it to be
                               echoed back (DeepSeek/Qwen thinking mode).
        """
        msg = {"role": role, "content": content}
        if reasoning_content:
            msg["reasoning_content"] = reasoning_content
        self.context.append(msg)
        self.last_active = time.time()

    def is_expired(self, timeout: float) -> bool:
        """Check if the session has timed out."""
        return (time.time() - self.last_active) > timeout

    def trim(self, max_messages: int):
        """Trim context to the most recent max_messages entries."""
        if len(self.context) > max_messages:
            self.context = self.context[-max_messages:]

    def clear(self):
        """Clear conversation context."""
        self.context.clear()

    def to_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "context": self.context,
            "created_at": self.created_at,
            "last_active": self.last_active,
            "tool_call_count": self.tool_call_count,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Session":
        return cls(
            user_id=data["user_id"],
            context=data.get("context", []),
            created_at=data.get("created_at", time.time()),
            last_active=data.get("last_active", time.time()),
            tool_call_count=data.get("tool_call_count", 0),
            metadata=data.get("metadata", {}),
        )


class SessionManager:
    """Manages all user sessions."""

    def __init__(
        self,
        max_context_messages: int = 20,
        session_timeout: float = 1800.0,
        persistence_dir: Optional[str] = None,
    ):
        self.max_context_messages = max_context_messages
        self.session_timeout = session_timeout
        self.persistence_dir = persistence_dir
        self._sessions: Dict[str, Session] = {}

        if persistence_dir:
            os.makedirs(persistence_dir, exist_ok=True)

    # ── CRUD ──────────────────────────────────────────────────────

    def get_or_create(self, user_id: str) -> Session:
        """Get an existing session or create a new one."""
        session = self._sessions.get(user_id)

        if session is None:
            # Try loading from disk
            session = self._load_from_disk(user_id)
            if session is None:
                session = Session(user_id=user_id)
            self._sessions[user_id] = session

        # Check timeout
        if session.is_expired(self.session_timeout):
            session.clear()

        return session

    def get(self, user_id: str) -> Optional[Session]:
        """Get a session without creating."""
        return self._sessions.get(user_id)

    def update(self, user_id: str, session: Session):
        """Update a session and optionally persist."""
        self._sessions[user_id] = session
        session.trim(self.max_context_messages)
        self._save_to_disk(user_id, session)

    def delete(self, user_id: str):
        """Delete a session."""
        self._sessions.pop(user_id, None)
        if self.persistence_dir:
            path = self._get_path(user_id)
            if os.path.exists(path):
                os.remove(path)

    def clear_context(self, user_id: str):
        """Clear conversation context but keep the session."""
        session = self._sessions.get(user_id)
        if session:
            session.clear()

    def cleanup_expired(self) -> int:
        """Remove expired sessions. Returns count of removed sessions."""
        expired = [
            uid
            for uid, s in self._sessions.items()
            if s.is_expired(self.session_timeout)
        ]
        for uid in expired:
            self.delete(uid)
        return len(expired)

    def active_count(self) -> int:
        return len(self._sessions)

    # ── Persistence ───────────────────────────────────────────────

    def _get_path(self, user_id: str) -> str:
        return os.path.join(self.persistence_dir, f"{user_id}.json")

    def _save_to_disk(self, user_id: str, session: Session):
        if not self.persistence_dir:
            return
        try:
            path = self._get_path(user_id)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(session.to_dict(), f, ensure_ascii=False, indent=2)
        except Exception:
            pass  # Persistence failure is non-fatal

    def _load_from_disk(self, user_id: str) -> Optional[Session]:
        if not self.persistence_dir:
            return None
        path = self._get_path(user_id)
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return Session.from_dict(data)
        except Exception:
            return None

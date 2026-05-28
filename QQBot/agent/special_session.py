"""
Special Session Manager — Persistent, named sessions with million-token context.

Each user can have up to 3 special sessions. Sessions are persisted using
a snapshot + delta storage scheme for fast loading even with thousands of messages.

Storage layout:
    {USER_DATA_ROOT}/{user_id}/sessions/
        _index.json            # Session index
        {session_name}/
            snapshot_00050.json  # Full snapshot at message 50
            snapshot_00100.json  # Full snapshot at message 100
            delta.jsonl          # Messages since last snapshot
"""

import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ── Constants ────────────────────────────────────────────────────

SNAPSHOT_INTERVAL = 50  # Generate a snapshot every N messages
DEFAULT_MAX_SESSIONS = 3
MAX_AUTO_NAME_LENGTH = 12


@dataclass
class SpecialSession:
    """A single persistent special session."""

    session_id: str              # UUID
    user_id: str                 # QQ ID
    name: str                    # Display name
    context: List[Dict] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)
    total_messages: int = 0
    metadata: Dict = field(default_factory=dict)
    # Internal: last snapshot sequence number
    _last_snapshot_seq: int = 0
    # Internal: total messages in delta since last snapshot
    _delta_count: int = 0

    # ── Serialization ────────────────────────────────────────────

    def to_index_entry(self) -> dict:
        return {
            "name": self.name,
            "session_id": self.session_id,
            "created_at": self.created_at,
            "last_active": self.last_active,
            "total_messages": self.total_messages,
            "metadata": self.metadata,
        }


class SpecialSessionManager:
    """Manages special sessions for all users."""

    def __init__(
        self,
        user_data_root: str,
        max_per_user: int = DEFAULT_MAX_SESSIONS,
        llm_client=None,
    ):
        self.user_data_root = user_data_root
        self.max_per_user = max_per_user
        self.llm_client = llm_client  # For auto-naming (can be set later)
        os.makedirs(user_data_root, exist_ok=True)

    def set_client(self, client):
        """Set or update the LLM client (for lazy initialization)."""
        self.llm_client = client

    # ── CRUD ──────────────────────────────────────────────────────

    def create(
        self, user_id: str, name: Optional[str] = None
    ) -> SpecialSession:
        """Create a new special session.

        Args:
            user_id: QQ user ID.
            name: Session name. If None, a rule-based temporary name is generated.
                  The LLM can refine it later via auto_name().

        Returns:
            The newly created SpecialSession.

        Raises:
            ValueError: If the user already has max_per_user sessions.
        """
        sessions = self.list_sessions(user_id)
        if len(sessions) >= self.max_per_user:
            names = ", ".join(s["name"] for s in sessions)
            raise ValueError(
                f"已达到最大特殊会话数 ({self.max_per_user})。"
                f"现有会话: {names}。请先删除一个再创建。"
            )

        if name is None:
            name = self._generate_temp_name()

        # Ensure unique name
        base_name = name
        counter = 1
        while any(s["name"] == name for s in sessions):
            name = f"{base_name}_{counter}"
            counter += 1

        session = SpecialSession(
            session_id=uuid.uuid4().hex[:12],
            user_id=user_id,
            name=name,
        )

        self._save(session)
        self._update_index(user_id, session)

        return session

    def get_active(self, user_id: str) -> Optional[SpecialSession]:
        """Get the currently active special session, if any."""
        index = self._load_index(user_id)
        active_name = index.get("active_session")
        if not active_name:
            return None
        return self._load(user_id, active_name)

    def get_by_name(self, user_id: str, name: str) -> Optional[SpecialSession]:
        """Get a special session by name."""
        index = self._load_index(user_id)
        if not any(s["name"] == name for s in index.get("sessions", [])):
            return None
        return self._load(user_id, name)

    def list_sessions(self, user_id: str) -> List[dict]:
        """List all special sessions for a user. Returns index entries."""
        index = self._load_index(user_id)
        return index.get("sessions", [])

    def switch_to(self, user_id: str, name: str) -> SpecialSession:
        """Switch the active session to the named one.

        Raises:
            ValueError: If the named session does not exist.
        """
        session = self._load(user_id, name)
        if session is None:
            available = [s["name"] for s in self.list_sessions(user_id)]
            raise ValueError(
                f"会话「{name}」不存在。可用会话: {', '.join(available) or '无'}"
            )

        # Save current active session first
        current = self.get_active(user_id)
        if current and current.name != name:
            self._save(current)

        # Update index
        index = self._load_index(user_id)
        index["active_session"] = name
        self._save_index(user_id, index)

        return session

    def rename(self, user_id: str, old_name: str, new_name: str) -> SpecialSession:
        """Rename a special session.

        Raises:
            ValueError: If old_name doesn't exist or new_name already exists.
        """
        session = self._load(user_id, old_name)
        if session is None:
            raise ValueError(f"会话「{old_name}」不存在。")

        existing = self.list_sessions(user_id)
        if any(s["name"] == new_name for s in existing):
            raise ValueError(f"会话「{new_name}」已存在。")

        # Remove old directory
        old_dir = self._session_dir(user_id, old_name)
        if os.path.exists(old_dir):
            import shutil
            shutil.rmtree(old_dir, ignore_errors=True)

        # Update session
        session.name = new_name
        self._save(session)

        # Update index
        index = self._load_index(user_id)
        for s in index["sessions"]:
            if s["name"] == old_name:
                s["name"] = new_name
        if index.get("active_session") == old_name:
            index["active_session"] = new_name
        self._save_index(user_id, index)

        return session

    def delete(self, user_id: str, name: str):
        """Delete a special session and all its data.

        Raises:
            ValueError: If the session doesn't exist.
        """
        session = self._load(user_id, name)
        if session is None:
            raise ValueError(f"会话「{name}」不存在。")

        # Remove session directory
        session_dir = self._session_dir(user_id, name)
        if os.path.exists(session_dir):
            import shutil
            shutil.rmtree(session_dir, ignore_errors=True)

        # Update index
        index = self._load_index(user_id)
        index["sessions"] = [
            s for s in index["sessions"] if s["name"] != name
        ]
        if index.get("active_session") == name:
            index["active_session"] = None
        self._save_index(user_id, index)

    def end_active(self, user_id: str):
        """End the active special session (return to temporary mode)."""
        current = self.get_active(user_id)
        if current:
            self._save(current)

        index = self._load_index(user_id)
        index["active_session"] = None
        self._save_index(user_id, index)

    # ── Message Operations ─────────────────────────────────────────

    def add_message(
        self,
        user_id: str,
        role: str,
        content: str,
        reasoning_content: Optional[str] = None,
    ):
        """Append a message to the active special session and persist."""
        session = self.get_active(user_id)
        if session is None:
            return

        msg = {"role": role, "content": content}
        if reasoning_content:
            msg["reasoning_content"] = reasoning_content

        session.context.append(msg)
        session.total_messages += 1
        session.last_active = time.time()

        # Append to delta
        self._append_delta(user_id, session.name, msg)

        # Generate snapshot if threshold reached
        if session.total_messages % SNAPSHOT_INTERVAL == 0:
            self._generate_snapshot(user_id, session)

        # Update index
        self._update_index(user_id, session)

    # ── Auto Naming ─────────────────────────────────────────────────

    async def auto_name(
        self, user_id: str, first_message: str, first_response: str
    ) -> Optional[str]:
        """Use LLM to generate a refined session name from the first interaction.

        Args:
            user_id: QQ user ID.
            first_message: The first user message in the session.
            first_response: The agent's first response.

        Returns:
            The new name (≤12 chars), or None if LLM is unavailable or fails.
        """
        if not self.llm_client:
            return None

        prompt = (
            "Based on the conversation below, generate a short session name "
            "(≤12 Chinese characters or ≤20 English characters) that summarizes "
            "the topic. Return ONLY the name, no explanation, no punctuation.\n\n"
            f"User: {first_message[:200]}\n\n"
            f"Assistant: {first_response[:200]}\n\n"
            "Name:"
        )

        try:
            result = await self.llm_client.chat_completion(
                prompt, timeout_set=20.0
            )
            if result:
                name = result.strip().strip("「」\"'《》")[:MAX_AUTO_NAME_LENGTH]
                if name:
                    # Rename the session
                    session = self.get_active(user_id)
                    if session and session.name != name:
                        try:
                            self.rename(user_id, session.name, name)
                            return name
                        except ValueError:
                            pass
        except Exception:
            pass

        return None

    # ── Snapshot + Delta Storage ────────────────────────────────────

    def _session_dir(self, user_id: str, name: str) -> str:
        safe_uid = self._safe_id(user_id)
        safe_name = self._safe_name(name)
        return os.path.join(self.user_data_root, safe_uid, "sessions", safe_name)

    def _save(self, session: SpecialSession):
        """Save the full session state."""
        session_dir = self._session_dir(session.user_id, session.name)
        os.makedirs(session_dir, exist_ok=True)

    def _append_delta(self, user_id: str, name: str, msg: dict):
        """Append a single message to the delta JSONL file."""
        session_dir = self._session_dir(user_id, name)
        delta_path = os.path.join(session_dir, "delta.jsonl")
        try:
            with open(delta_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(msg, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def _generate_snapshot(self, user_id: str, session: SpecialSession):
        """Generate a full snapshot of the current context."""
        session_dir = self._session_dir(user_id, session.name)
        seq = session.total_messages
        snapshot_path = os.path.join(
            session_dir, f"snapshot_{seq:05d}.json"
        )
        try:
            with open(snapshot_path, "w", encoding="utf-8") as f:
                json.dump(session.context, f, ensure_ascii=False, indent=2)
        except Exception:
            return

        # Clean old snapshots
        for old_seq in [
            session._last_snapshot_seq,
            session._last_snapshot_seq - SNAPSHOT_INTERVAL,
        ]:
            if old_seq > 0 and old_seq != seq:
                old_path = os.path.join(
                    session_dir, f"snapshot_{old_seq:05d}.json"
                )
                if os.path.exists(old_path):
                    try:
                        os.remove(old_path)
                    except Exception:
                        pass

        # Clear delta since it's now covered by the snapshot
        delta_path = os.path.join(session_dir, "delta.jsonl")
        if os.path.exists(delta_path):
            try:
                os.remove(delta_path)
            except Exception:
                pass

        session._last_snapshot_seq = seq
        session._delta_count = 0

    def _load(self, user_id: str, name: str) -> Optional[SpecialSession]:
        """Load a special session from disk using snapshot + delta."""
        session_dir = self._session_dir(user_id, name)
        if not os.path.exists(session_dir):
            return None

        context = []

        # Find latest snapshot
        snapshots = []
        for f in os.listdir(session_dir):
            if f.startswith("snapshot_") and f.endswith(".json"):
                try:
                    seq = int(f.replace("snapshot_", "").replace(".json", ""))
                    snapshots.append((seq, f))
                except ValueError:
                    pass

        latest_snapshot_seq = 0
        if snapshots:
            snapshots.sort(key=lambda x: x[0])
            latest_snapshot_seq, latest_file = snapshots[-1]
            snapshot_path = os.path.join(session_dir, latest_file)
            try:
                with open(snapshot_path, "r", encoding="utf-8") as f:
                    context = json.load(f)
            except Exception:
                pass

        # Load delta messages
        delta_path = os.path.join(session_dir, "delta.jsonl")
        delta_count = 0
        if os.path.exists(delta_path):
            try:
                with open(delta_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            context.append(json.loads(line))
                            delta_count += 1
            except Exception:
                pass

        # Load metadata from index
        index = self._load_index(user_id)
        meta = {}
        for s in index.get("sessions", []):
            if s["name"] == name:
                meta = s
                break

        return SpecialSession(
            session_id=meta.get("session_id", uuid.uuid4().hex[:12]),
            user_id=user_id,
            name=name,
            context=context,
            created_at=meta.get("created_at", time.time()),
            last_active=meta.get("last_active", time.time()),
            total_messages=len(context),
            metadata=meta.get("metadata", {}),
            _last_snapshot_seq=latest_snapshot_seq,
            _delta_count=delta_count,
        )

    # ── Index Management ────────────────────────────────────────────

    def _index_path(self, user_id: str) -> str:
        safe_uid = self._safe_id(user_id)
        return os.path.join(
            self.user_data_root, safe_uid, "sessions", "_index.json"
        )

    def _load_index(self, user_id: str) -> dict:
        path = self._index_path(user_id)
        if not os.path.exists(path):
            return {"active_session": None, "sessions": []}
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {"active_session": None, "sessions": []}

    def _save_index(self, user_id: str, index: dict):
        path = self._index_path(user_id)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(index, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _update_index(self, user_id: str, session: SpecialSession):
        """Update the index entry for a session."""
        index = self._load_index(user_id)
        entry = session.to_index_entry()

        # Update or insert
        found = False
        for i, s in enumerate(index["sessions"]):
            if s["name"] == session.name:
                index["sessions"][i] = entry
                found = True
                break
        if not found:
            index["sessions"].append(entry)

        self._save_index(user_id, index)

    # ── Helpers ────────────────────────────────────────────────────

    @staticmethod
    def _safe_id(user_id: str) -> str:
        return "".join(c if c.isalnum() or c in "-_" else "_" for c in user_id)

    @staticmethod
    def _safe_name(name: str) -> str:
        # Keep Chinese characters and alphanumeric
        safe = []
        for c in name:
            if c.isalnum() or c in "-_." or '\u4e00' <= c <= '\u9fff':
                safe.append(c)
            else:
                safe.append("_")
        return "".join(safe).strip("_") or "session"

    @staticmethod
    def _generate_temp_name() -> str:
        """Generate a rule-based temporary name: {MMDD}_{time}."""
        return time.strftime("%m%d_%H%M")

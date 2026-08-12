"""
Memory System — Long-term persistent memory for the agent.

Memory types:
- user: Per-user facts, preferences, interaction summaries (scoped to user_id)
- knowledge: Agent-learned information (shared across users)
- system: Agent self-reflection and configuration history (shared across users)

Memory is stored as markdown files with frontmatter, with an index in MEMORY.md.
User-type memories are isolated: stored in per-user subdirectories and only
returned when the matching user_id is provided.
"""

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class MemoryEntry:
    """A single memory entry."""

    name: str
    description: str
    type: str  # user, knowledge, system
    content: str
    user_id: Optional[str] = None  # Owner user_id (required for user-type memories)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)


class MemorySystem:
    """File-based long-term memory system with per-user isolation.

    User-type memories are stored in {base_dir}/user/{user_id}/ subdirectories
    and are only returned when queried with the matching user_id. Knowledge
    and system memories are stored globally and shared across users.
    """

    def __init__(self, base_dir: str):
        self.base_dir = base_dir
        self.index_path = os.path.join(base_dir, "MEMORY.md")
        os.makedirs(base_dir, exist_ok=True)
        self._ensure_index()

    def _ensure_index(self):
        if not os.path.exists(self.index_path):
            with open(self.index_path, "w", encoding="utf-8") as f:
                f.write(
                    "# Memory Index\n\n"
                    "## User Memories\n\n"
                    "## Knowledge Memories\n\n"
                    "## System Memories\n\n"
                )

    # ── Path resolution ───────────────────────────────────────────

    def _get_storage_dir(self, mem_type: str, user_id: str = None) -> str:
        """Get the storage directory for a memory type.

        User memories go to {base_dir}/user/{user_id}/ for isolation.
        Knowledge and system memories go to {base_dir}/{type}/ (flat, shared).
        """
        if mem_type == "user" and user_id:
            dir_path = os.path.join(self.base_dir, "user", self._safe_id(user_id))
        else:
            dir_path = os.path.join(self.base_dir, mem_type)
        os.makedirs(dir_path, exist_ok=True)
        return dir_path

    @staticmethod
    def _safe_id(id_str: str) -> str:
        """Sanitize an ID for use as a directory name."""
        return "".join(c if c.isalnum() or c in "-_" else "_" for c in id_str)

    # ── CRUD ──────────────────────────────────────────────────────

    def save(self, entry: MemoryEntry) -> str:
        """Save a memory entry. Returns the file path.

        User-type memories are stored in a per-user subdirectory.
        """
        user_id = entry.user_id if entry.type == "user" else None
        type_dir = self._get_storage_dir(entry.type, user_id)

        filename = self._sanitize_filename(entry.name) + ".md"
        filepath = os.path.join(type_dir, filename)

        # Include user_id in frontmatter for user-type memories
        user_id_line = f"user_id: {entry.user_id}\n" if entry.user_id else ""

        content = (
            f"---\n"
            f"name: {entry.name}\n"
            f"description: {entry.description}\n"
            f"type: {entry.type}\n"
            f"{user_id_line}"
            f"created_at: {entry.created_at}\n"
            f"updated_at: {entry.updated_at}\n"
            f"---\n\n"
            f"{entry.content}\n"
        )

        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)

        self._update_index(entry, filepath)
        return filepath

    def recall(self, name: str, mem_type: str = None, user_id: str = None) -> Optional[MemoryEntry]:
        """Recall a memory by name, optionally filtered by type and user_id.

        For user-type memories, user_id is required to find the memory.
        """
        search_dirs = self._get_search_dirs(mem_type, user_id)
        for search_dir in search_dirs:
            filename = self._sanitize_filename(name) + ".md"
            filepath = os.path.join(search_dir, filename)
            if os.path.exists(filepath):
                return self._load_file(filepath)
        return None

    def forget(self, name: str, mem_type: str = None, user_id: str = None) -> bool:
        """Delete a memory by name. For user memories, user_id scopes the deletion."""
        search_dirs = self._get_search_dirs(mem_type, user_id)
        for search_dir in search_dirs:
            filename = self._sanitize_filename(name) + ".md"
            filepath = os.path.join(search_dir, filename)
            if os.path.exists(filepath):
                os.remove(filepath)
                self._remove_from_index(name)
                return True
        return False

    def search(self, query: str, mem_type: str = None, user_id: str = None) -> List[MemoryEntry]:
        """Search memories by content keyword. Simple, not semantic.

        User-type memories are only returned when user_id matches.
        Knowledge and system memories are always included (they're shared).
        """
        results = []
        search_dirs = self._get_search_dirs(mem_type, user_id)
        for search_dir in search_dirs:
            if not os.path.exists(search_dir):
                continue
            for filename in os.listdir(search_dir):
                if filename.endswith(".md") and filename != "MEMORY.md":
                    filepath = os.path.join(search_dir, filename)
                    entry = self._load_file(filepath)
                    if entry and query.lower() in entry.content.lower():
                        results.append(entry)
        return results

    def list_all(self, mem_type: str = None, user_id: str = None) -> List[MemoryEntry]:
        """List all memories, optionally filtered by type and user_id."""
        results = []
        search_dirs = self._get_search_dirs(mem_type, user_id)
        for search_dir in search_dirs:
            if not os.path.exists(search_dir):
                continue
            for filename in os.listdir(search_dir):
                if filename.endswith(".md") and filename != "MEMORY.md":
                    filepath = os.path.join(search_dir, filename)
                    entry = self._load_file(filepath)
                    if entry:
                        results.append(entry)
        return results

    # ── Search directory resolution ───────────────────────────────

    def _get_search_dirs(self, mem_type: str, user_id: str) -> List[str]:
        """Get the list of directories to search based on type and user_id.

        - Specific type provided: return directories for that type only.
          For user type, scoped to the given user_id.
        - No type: return all applicable directories:
          - knowledge/ and system/ (shared, always included)
          - user/{user_id}/ if user_id is given (scoped isolation)
        """
        if mem_type:
            if mem_type == "user":
                if user_id:
                    return [self._get_storage_dir("user", user_id)]
                else:
                    # No user_id — search ALL user subdirectories (admin/debug use)
                    user_base = os.path.join(self.base_dir, "user")
                    if os.path.exists(user_base):
                        return [
                            os.path.join(user_base, d)
                            for d in os.listdir(user_base)
                            if os.path.isdir(os.path.join(user_base, d))
                        ]
                    return []
            else:
                return [self._get_storage_dir(mem_type)]
        else:
            # All types
            dirs = [
                self._get_storage_dir("knowledge"),
                self._get_storage_dir("system"),
            ]
            if user_id:
                dirs.append(self._get_storage_dir("user", user_id))
            else:
                # No user_id — include all user subdirectories
                user_base = os.path.join(self.base_dir, "user")
                if os.path.exists(user_base):
                    for d in os.listdir(user_base):
                        d_path = os.path.join(user_base, d)
                        if os.path.isdir(d_path):
                            dirs.append(d_path)
            return dirs

    # ── Helpers ───────────────────────────────────────────────────

    def _load_file(self, filepath: str) -> Optional[MemoryEntry]:
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                content = f.read()
            # Parse frontmatter
            if content.startswith("---"):
                parts = content.split("---", 2)
                if len(parts) >= 3:
                    meta = {}
                    for line in parts[1].strip().split("\n"):
                        if ":" in line:
                            k, v = line.split(":", 1)
                            meta[k.strip()] = v.strip()
                    return MemoryEntry(
                        name=meta.get("name", ""),
                        description=meta.get("description", ""),
                        type=meta.get("type", "knowledge"),
                        user_id=meta.get("user_id"),
                        content=parts[2].strip(),
                        created_at=float(meta.get("created_at", time.time())),
                        updated_at=float(meta.get("updated_at", time.time())),
                    )
        except Exception:
            pass
        return None

    def _update_index(self, entry: MemoryEntry, filepath: str):
        """Add entry pointer to MEMORY.md index."""
        try:
            with open(self.index_path, "r", encoding="utf-8") as f:
                index_content = f.read()

            rel_path = os.path.relpath(filepath, self.base_dir)
            entry_line = f"- [{entry.name}]({rel_path}) — {entry.description}\n"

            section_marker = {
                "user": "## User Memories",
                "knowledge": "## Knowledge Memories",
                "system": "## System Memories",
            }.get(entry.type, "## Knowledge Memories")

            if entry_line.strip() not in index_content:
                # Insert after section marker
                marker_pos = index_content.find(section_marker)
                if marker_pos != -1:
                    next_section = index_content.find("##", marker_pos + len(section_marker))
                    if next_section == -1:
                        next_section = len(index_content)
                    # Find end of section marker line
                    line_end = index_content.find("\n", marker_pos) + 1
                    new_content = (
                        index_content[:line_end]
                        + entry_line
                        + index_content[line_end:]
                    )
                    with open(self.index_path, "w", encoding="utf-8") as f:
                        f.write(new_content)
        except Exception:
            pass

    def _remove_from_index(self, name: str):
        """Remove entry from MEMORY.md index."""
        try:
            with open(self.index_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            lines = [l for l in lines if not l.startswith(f"- [{name}]")]
            with open(self.index_path, "w", encoding="utf-8") as f:
                f.writelines(lines)
        except Exception:
            pass

    @staticmethod
    def _sanitize_filename(name: str) -> str:
        return "".join(c if c.isalnum() or c in "-_" else "_" for c in name)

"""
Memory System — Long-term persistent memory for the agent.

Memory types:
- user: Per-user facts, preferences, interaction summaries
- knowledge: Agent-learned information
- system: Agent self-reflection and configuration history

Memory is stored as markdown files with frontmatter, with an index in MEMORY.md.
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
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)


class MemorySystem:
    """File-based long-term memory system."""

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

    # ── CRUD ──────────────────────────────────────────────────────

    def save(self, entry: MemoryEntry) -> str:
        """Save a memory entry. Returns the file path."""
        type_dir = os.path.join(self.base_dir, entry.type)
        os.makedirs(type_dir, exist_ok=True)

        filename = self._sanitize_filename(entry.name) + ".md"
        filepath = os.path.join(type_dir, filename)

        content = (
            f"---\n"
            f"name: {entry.name}\n"
            f"description: {entry.description}\n"
            f"type: {entry.type}\n"
            f"created_at: {entry.created_at}\n"
            f"updated_at: {entry.updated_at}\n"
            f"---\n\n"
            f"{entry.content}\n"
        )

        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)

        self._update_index(entry, filepath)
        return filepath

    def recall(self, name: str, mem_type: str = None) -> Optional[MemoryEntry]:
        """Recall a memory by name, optionally filtered by type."""
        search_dirs = [mem_type] if mem_type else ["user", "knowledge", "system"]
        for t in search_dirs:
            type_dir = os.path.join(self.base_dir, t)
            if not os.path.exists(type_dir):
                continue
            filename = self._sanitize_filename(name) + ".md"
            filepath = os.path.join(type_dir, filename)
            if os.path.exists(filepath):
                return self._load_file(filepath)
        return None

    def forget(self, name: str, mem_type: str = None):
        """Delete a memory by name."""
        search_dirs = [mem_type] if mem_type else ["user", "knowledge", "system"]
        for t in search_dirs:
            type_dir = os.path.join(self.base_dir, t)
            filename = self._sanitize_filename(name) + ".md"
            filepath = os.path.join(type_dir, filename)
            if os.path.exists(filepath):
                os.remove(filepath)
                self._remove_from_index(name)
                return True
        return False

    def search(self, query: str, mem_type: str = None) -> List[MemoryEntry]:
        """Search memories by content keyword. Simple, not semantic."""
        results = []
        search_dirs = [mem_type] if mem_type else ["user", "knowledge", "system"]
        for t in search_dirs:
            type_dir = os.path.join(self.base_dir, t)
            if not os.path.exists(type_dir):
                continue
            for filename in os.listdir(type_dir):
                if filename.endswith(".md") and filename != "MEMORY.md":
                    filepath = os.path.join(type_dir, filename)
                    entry = self._load_file(filepath)
                    if entry and query.lower() in entry.content.lower():
                        results.append(entry)
        return results

    def list_all(self, mem_type: str = None) -> List[MemoryEntry]:
        """List all memories, optionally filtered by type."""
        results = []
        search_dirs = [mem_type] if mem_type else ["user", "knowledge", "system"]
        for t in search_dirs:
            type_dir = os.path.join(self.base_dir, t)
            if not os.path.exists(type_dir):
                continue
            for filename in os.listdir(type_dir):
                if filename.endswith(".md") and filename != "MEMORY.md":
                    filepath = os.path.join(type_dir, filename)
                    entry = self._load_file(filepath)
                    if entry:
                        results.append(entry)
        return results

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

            entry_line = f"- [{entry.name}]({os.path.relpath(filepath, self.base_dir)}) — {entry.description}\n"

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

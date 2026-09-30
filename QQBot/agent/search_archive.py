"""
SearchArchive — on-disk archive of search_web / web_fetch tool results.

Full tool results only live in the current turn's context: the audit log keeps
a 200-char summary (agent.py) and a folded TaskRecord keeps 500 chars
(task_record.py). After context folding/compression the model loses URLs and
details it already triaged, and must re-search.

This module closes that gap: when search_web / web_fetch succeed, the complete
formatted result is persisted here, and an archive id is appended to the tool
return text. The id travels with the append-only message stream (never in the
system prompt, so the provider-side prefix cache is unaffected). The model can
later retrieve the full result via the ``recall_search_result`` tool.

Design mirrors MemorySystem(base_dir) (agent/memory.py):
  - per-user subdirectories ({base}/{safe_uid}/{archive_id}.json)
  - retention: TTL_SECONDS expiry + MAX_PER_USER cap, swept lazily on save
  - all failures are silent — archiving must never break the tool main flow
"""

import json
import os
import re
import time
import uuid
from datetime import datetime
from typing import Optional

# ── Retention policy ─────────────────────────────────────────────
TTL_SECONDS = 7 * 24 * 3600   # archives expire after 7 days
MAX_PER_USER = 50             # max archive files kept per user

# Archive ids are uuid4().hex[:12]; validated strictly to prevent
# path traversal on recall.
_ARCHIVE_ID_RE = re.compile(r"^[0-9a-f]{12}$")

# Default location: QQBot/data/search_cache/ (project-relative resolution
# identical to task_record.py). Deliberately OUTSIDE data/workspace/ so
# archives do not count against user workspace quotas.
_DEFAULT_BASE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "search_cache",
)

_default_archive: Optional["SearchArchive"] = None


def get_default_archive() -> "SearchArchive":
    """Lazy module-level singleton; SEARCH_ARCHIVE_DIR env var overrides."""
    global _default_archive
    if _default_archive is None:
        base = os.environ.get("SEARCH_ARCHIVE_DIR") or _DEFAULT_BASE_DIR
        _default_archive = SearchArchive(base_dir=base)
    return _default_archive


class SearchArchive:
    """Per-user JSON archive of tool results with TTL + count retention."""

    def __init__(self, base_dir: str = None):
        self.base_dir = base_dir or _DEFAULT_BASE_DIR

    # ── Path helpers ──────────────────────────────────────────────

    @staticmethod
    def _safe_id(id_str: str) -> str:
        """Sanitize an ID for use as a directory name (same as memory.py)."""
        return "".join(c if c.isalnum() or c in "-_" else "_" for c in id_str)

    def _user_dir(self, user_id: str) -> str:
        return os.path.join(self.base_dir, self._safe_id(str(user_id)))

    @staticmethod
    def _valid_id(archive_id) -> bool:
        return isinstance(archive_id, str) and bool(_ARCHIVE_ID_RE.match(archive_id))

    # ── CRUD ──────────────────────────────────────────────────────

    def save(
        self,
        user_id: str,
        tool: str,
        query: str,
        content: str,
        raw=None,
    ) -> Optional[str]:
        """Persist one tool result. Returns the archive id, or None on failure.

        Args:
            user_id: owner of the archive (per-user isolation).
            tool: originating tool name ("search_web" / "web_fetch").
            query: search query or fetched URL.
            content: the complete formatted text the model saw.
            raw: optional raw payload (e.g. SearXNG result dicts).
        """
        try:
            if not user_id or not content:
                return None

            archive_id = uuid.uuid4().hex[:12]
            user_dir = self._user_dir(user_id)
            os.makedirs(user_dir, exist_ok=True)

            record = {
                "id": archive_id,
                "ts": time.time(),
                "iso_time": datetime.now().astimezone().isoformat(timespec="seconds"),
                "user_id": str(user_id),
                "tool": tool or "",
                "query": (query or "").strip(),
                "content": content,
                "raw_results": raw,
            }

            path = os.path.join(user_dir, f"{archive_id}.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(record, f, ensure_ascii=False, default=str)

            # Lazy retention sweep for this user's directory.
            self._cleanup_user_dir(user_dir)
            return archive_id
        except Exception:
            return None  # archiving must never break the tool main flow

    def recall(self, user_id: str, archive_id: str) -> Optional[dict]:
        """Retrieve one archive record, strictly scoped to its owner.

        Returns the record dict, or None if the id is invalid, belongs to
        another user, does not exist, or has expired.
        """
        try:
            if not user_id or not self._valid_id(archive_id):
                return None

            path = os.path.join(self._user_dir(user_id), f"{archive_id}.json")
            if not os.path.isfile(path):
                return None

            # Treat expired archives as gone (file is swept on next save).
            if time.time() - os.path.getmtime(path) > TTL_SECONDS:
                return None

            with open(path, "r", encoding="utf-8") as f:
                record = json.load(f)
            return record if isinstance(record, dict) else None
        except Exception:
            return None

    # ── Retention ─────────────────────────────────────────────────

    def _cleanup_user_dir(self, user_dir: str) -> None:
        """Best-effort sweep: drop expired files, then oldest-first over cap.

        mtime-ascending enumeration + batch deletion follows the
        quota_cleanup.py pattern.
        """
        try:
            now = time.time()
            alive = []
            for name in os.listdir(user_dir):
                if not name.endswith(".json"):
                    continue
                p = os.path.join(user_dir, name)
                try:
                    mtime = os.path.getmtime(p)
                except OSError:
                    continue
                if now - mtime > TTL_SECONDS:
                    try:
                        os.remove(p)
                    except OSError:
                        pass
                else:
                    alive.append((mtime, p))

            alive.sort()  # oldest first
            excess = len(alive) - MAX_PER_USER
            for _, p in alive[:max(excess, 0)]:
                try:
                    os.remove(p)
                except OSError:
                    pass
        except Exception:
            pass  # cleanup must never break the main flow

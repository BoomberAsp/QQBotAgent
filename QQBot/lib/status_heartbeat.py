"""
Atomic status-file helpers for bot→panel file IPC.

The WebUI panel runs as a separate process with no bot handle, so bot
plugins publish observability state as a small JSON file (same pattern
as the changelog broadcast queue in tools/changelog.py). This module
provides the read/merge/atomic-write core, kept free of nonebot imports
so it is trivially testable and reusable by any plugin.

Writers must treat failures as non-fatal (observability never breaks
message handling) — callers decide how to log.
"""

import json
import os
import time
from pathlib import Path


def read_status(path: str | Path) -> dict:
    """Current status dict; {} when missing/corrupt/not-an-object."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            cur = json.load(f)
        return cur if isinstance(cur, dict) else {}
    except (OSError, ValueError):
        return {}


def write_status(path: str | Path, **patch) -> dict:
    """Merge patch into the status file atomically (tmp + os.replace).

    Always stamps written_at=now. Returns the merged dict that was
    written. Raises OSError on write failure — callers should catch
    and log, never propagate into request handling.
    """
    path = Path(path)
    cur = read_status(path)
    cur.update(patch)
    cur["written_at"] = time.time()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cur, f, ensure_ascii=False)
    os.replace(tmp, path)
    return cur

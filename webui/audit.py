"""
WebUI action audit log — every mutating panel action lands in
``webui/data/audit.jsonl`` (JSONL, same convention as the bot's audit logs).
"""

import json
import time
from datetime import datetime

from . import config

_AUDIT_PATH = config.WEBUI_DATA / "audit.jsonl"


def log_action(action: str, detail: str = "", ip: str = "") -> None:
    """Append one audit entry. Never raises."""
    try:
        entry = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "epoch": int(time.time()),
            "ip": ip or "",
            "action": action,
            "detail": detail[:500],
        }
        with open(_AUDIT_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def read_recent(limit: int = 100) -> list:
    """Return the most recent audit entries (newest first)."""
    try:
        with open(_AUDIT_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
        entries = []
        for line in lines[-limit:]:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        entries.reverse()
        return entries
    except OSError:
        return []

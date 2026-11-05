"""Shared bond naming policy; preserve replaced names for lookup."""
import json
import os
import re
from pathlib import Path


def artifact_path():
    root = os.environ.get("GAMEDATA_DIR")
    if not root and os.environ.get("USER_DATA_ROOT"):
        root = str(Path(os.environ["USER_DATA_ROOT"]) / "gamedata")
    return Path(root) / "artifacts.json" if root else None


def load_artifacts():
    path = artifact_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8")).get("artifacts", {}) if path else {}
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, AttributeError):
        return {}


from lib.game_names import reconcile  # backward-compatible artifact API

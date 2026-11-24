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


def artifact_id_redirects(artifacts):
    """Known legacy catalogue IDs superseded by the current game data."""
    return {old: current for old, current in (("A0129", "A0159"), ("A0165", "A0162"))
            if current in artifacts and old not in artifacts}


from lib.game_names import localized_text, reconcile  # reconcile remains the cache naming API


def artifact_detail(rid, artifact, wiki=None):
    """Convert an unpacked artifact into the detail/card schema, filling gaps from Wiki."""
    from tools.wiki_glossary import load_glossary

    entry = dict(wiki or {})
    names = artifact.get("names") or {}
    en = str(names.get("en") or "").strip()
    entry["id"] = rid
    entry["title"] = en if en and en.casefold() != rid.casefold() and not en.startswith("T_") else entry.get("title") or rid
    reconcile(entry, {rid: artifact}, prefer_official=True)
    entry["name_aliases"] = list(dict.fromkeys(filter(None, [
        *entry["name_aliases"], (wiki or {}).get("title"), *(artifact.get("aliases") or ()),
    ])))
    if artifact.get("star") is not None:
        entry["stars"] = artifact["star"]
    job = artifact.get("job")
    if job:
        entry["class_en"] = job
        entry["class_cn"] = "全职业" if job.lower() in {"any", "other"} else load_glossary()["class"].get(job, job)
    stats = artifact.get("stats") or {}
    for stage in ("base", "max"):
        for field in ("atk", "hp"):
            value = (stats.get(stage) or {}).get(field)
            if value is not None and value != "":
                entry[f"{field}_{stage}"] = value
    for target, source in (("desc", "description"), ("effect", "effect")):
        official = localized_text(artifact.get(source))
        if official:
            entry[target] = official
            entry[f"{target}_source"] = "gamedata"
            if target == "effect":
                entry["effect_template"] = bool(re.search(r"\[[^]\n]+:[^]\n]+:[A-Za-z_]+\]|\{\d+\}", official))
    release = artifact.get("release_date") or artifact.get("release")
    if release and release != "?":
        entry["release"] = release
    entry.pop("_card_hash", None)
    return entry


def has_official_artifact_name(rid, artifact):
    return any(value and str(value).casefold() != rid.casefold() and not str(value).startswith("T_")
               for value in (artifact.get("names") or {}).values())

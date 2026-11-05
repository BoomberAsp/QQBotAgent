"""Shared character and artifact naming policy."""
import json
import os
import re
from pathlib import Path


def roles_path():
    root = os.environ.get("GAMEDATA_DIR")
    if not root and os.environ.get("USER_DATA_ROOT"):
        root = str(Path(os.environ["USER_DATA_ROOT"]) / "gamedata")
    return Path(root) / "roles.json" if root else None


def file_stamp(path):
    try:
        p = Path(path); s = p.stat()
        return (str(p), s.st_mtime_ns, s.st_size)
    except (OSError, TypeError):
        return (str(path), None)


def load_roles():
    try:
        p = roles_path()
        data = json.loads(p.read_text(encoding="utf-8")).get("roles", {}) if p else {}
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, AttributeError):
        return {}


def reconcile(entry, artifacts, previous=None):
    """Mutate a cache entry; manual > valid official Chinese > old name > title.

    Match ID first. Only an absent ID allows unique exact English-title fallback;
    never guess across conflicting nonempty IDs.
    """
    title = str(entry.get("title") or "").strip()
    aid = str(entry.get("id") or "").strip()
    artifact = artifacts.get(aid, {})
    if not aid and title:
        matches = [v for v in artifacts.values() if isinstance(v, dict)
                   and str(v.get("names", {}).get("en", "")).strip().casefold() == title.casefold()]
        if len(matches) == 1:
            artifact = matches[0]
            aid = str(artifact.get("id") or "")
            if aid:
                entry["id"] = aid
    names = artifact.get("names", {}) if isinstance(artifact, dict) else {}
    official = next((str(names.get(k) or "").strip() for k in ("zh_hans", "zh_hant")
                     if names.get(k) and str(names[k]).strip() != aid
                     and not str(names[k]).startswith("T_")
                     and re.search(r"[\u3400-\u9fff]", str(names[k]))), "")
    old = str(entry.get("name_cn") or "").strip()
    prior = str((previous or {}).get("name_cn") or "").strip()
    if "name_cn" in (entry.get("_manual_fields") or []) and old:
        selected, source = old, "manual"
    elif official:
        selected, source = official, "official"
    else:
        selected = next((x for x in (old, prior) if x and x != title), old or prior or title)
        source = "existing" if selected != title else "title"
    aliases = []
    for owner in (previous or {}, entry):
        values = owner.get("name_aliases", [])
        if isinstance(values, list):
            aliases.extend(x for x in values if isinstance(x, str) and x.strip())
    aliases.extend(x for x in (prior, old) if x and x != selected and x != title)
    entry["name_aliases"] = list(dict.fromkeys(aliases))
    if old != selected:
        entry.pop("_card_hash", None)  # renderer must not reuse a card with the old name
    entry["name_cn"] = selected
    entry["name_source"] = source
    return entry

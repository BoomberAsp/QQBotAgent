#!/usr/bin/env python3
"""
One-off cleanup: remove blacklisted junk from existing user profiles.

Why (Profile-Fact-Extraction-Plan.md §10 / §12.1):
  Already-polluted profile.json data does not clean itself. Layer 1 (rewritten
  extraction prompt) + Layer 2 (fact_filter) stop NEW junk from entering, but the
  junk that accumulated before the fix is still on disk. This script applies the
  SAME deterministic Layer-2 filter (agent/fact_filter.py) to the persisted
  `facts` and `interests` lists and drops only the entries it blacklists.

Scope (P0 = delete only, NO migration):
  - P0 deletes blacklisted entries in place; it does NOT move anything anywhere.
  - Merging surviving facts into the three-tier memory engine is a P1 concern.
  - `facts` is dormant in P0 (not injected / not extracted), but cleaning it now
    is cheap hygiene and prepares for the P1 migration. `interests` IS still
    injected, so cleaning it removes live pollution.

Safety:
  - DRY-RUN by default: prints what WOULD be dropped, writes nothing.
  - `--apply` to actually write; every modified file is backed up first
    (timestamped mirror dir) unless `--no-backup`.
  - Atomic writes (temp file + os.replace) — never leaves a half-written JSON.
  - Only UNAMBIGUOUS Layer-2 blacklist hits are removed. Ambiguous bare words
    and specific character/nickname names are NEVER touched (that is Layer 1's
    job, by design — see fact_filter.py header). A false positive here would
    silently delete a legitimate fact, so the filter is deliberately narrow.
  - Per-file errors are caught and reported; one bad file never aborts the run.

Usage:
  # dry-run on the default profile store (env USER_DATA_ROOT, else
  # QQBot/data/users_store):
  python scripts/cleanup_profile_facts.py

  # dry-run, show every dropped entry:
  python scripts/cleanup_profile_facts.py -v

  # inspect a specific store:
  python scripts/cleanup_profile_facts.py --base-dir /mnt/datadisk0/QQBotUserData

  # actually apply (backs up first):
  python scripts/cleanup_profile_facts.py --apply

  # apply, clean only interests (facts left untouched):
  python scripts/cleanup_profile_facts.py --apply --field interests

⚠️ Run on the PRODUCTION server against the real user store only after a dry-run
   review and explicit operator confirmation. The dev machine has no live data.
"""

import argparse
import json
import os
import shutil
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Dict, List, Tuple

# Make `agent.fact_filter` importable regardless of the invocation cwd.
_QQBOT_DIR = Path(__file__).resolve().parent.parent
if str(_QQBOT_DIR) not in sys.path:
    sys.path.insert(0, str(_QQBOT_DIR))

from agent.fact_filter import filter_candidate  # noqa: E402

# Profile store default: env USER_DATA_ROOT, else QQBot/data/users_store
# (mirrors plugins/agent_router.py:_USER_DATA_ROOT).
DEFAULT_BASE_DIR = os.environ.get(
    "USER_DATA_ROOT", str(_QQBOT_DIR / "data" / "users_store")
)
DEFAULT_BACKUP_ROOT = _QQBOT_DIR / "data" / "profile_cleanup_backups"

# A JSON file is treated as a profile only if it carries these schema keys.
_PROFILE_KEYS = ("user_id", "facts", "interests", "preferences", "nickname")


def find_profile_files(base_dir: str) -> List[Tuple[Path, str]]:
    """Return [(path, layout)] for every profile JSON under base_dir.

    layout is "new" ({base}/{safe_id}/profile.json) or "legacy"
    ({base}/{safe_id}.json — the pre-migration flat layout). Non-profile JSON at
    the top level (e.g. .hardware.json) is skipped by a schema guard.
    """
    base = Path(base_dir)
    out: List[Tuple[Path, str]] = []
    if not base.is_dir():
        return out
    for p in sorted(base.glob("*/profile.json")):
        out.append((p, "new"))
    for p in sorted(base.glob("*.json")):
        # Skip our own temp/backup artifacts.
        if p.name.endswith(".tmp"):
            continue
        out.append((p, "legacy"))
    return out


def _looks_like_profile(data: Dict) -> bool:
    return isinstance(data, dict) and any(k in data for k in _PROFILE_KEYS)


def clean_profile(data: Dict, fields: List[str]) -> List[Tuple[str, str, str, str]]:
    """Drop blacklisted entries from the given list fields, in place.

    Returns a list of (field, text, matched, category) for every dropped entry.
    Non-string list items are preserved untouched.
    """
    dropped: List[Tuple[str, str, str, str]] = []
    for field in fields:
        items = data.get(field)
        if not isinstance(items, list):
            continue
        kept = []
        for it in items:
            if not isinstance(it, str):
                kept.append(it)
                continue
            r = filter_candidate(it)
            if r.keep:
                kept.append(it)
            else:
                dropped.append((field, it, r.matched or "", r.category or ""))
        if len(kept) != len(items):
            data[field] = kept
    return dropped


def atomic_write_json(path: Path, data: Dict) -> None:
    """Write JSON atomically: temp file in the same dir + os.replace."""
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def backup_file(path: Path, base_dir: Path, backup_root: Path) -> Path:
    """Mirror `path` (relative to base_dir) into the timestamped backup root."""
    try:
        rel = path.relative_to(base_dir)
    except ValueError:
        rel = Path(path.name)
    dest = backup_root / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, dest)
    return dest


def _clip(s: str, n: int = 60) -> str:
    s = s.replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def main(argv: List[str] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Remove Layer-2-blacklisted junk from existing user profiles.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--base-dir",
        default=DEFAULT_BASE_DIR,
        help=f"Profile store root (default: {DEFAULT_BASE_DIR})",
    )
    ap.add_argument(
        "--apply",
        action="store_true",
        help="Actually write changes (default is DRY-RUN: report only).",
    )
    ap.add_argument(
        "--field",
        choices=["facts", "interests", "both"],
        default="both",
        help="Which list field(s) to clean (default: both).",
    )
    ap.add_argument(
        "--no-backup",
        action="store_true",
        help="With --apply: skip the pre-write backup (NOT recommended).",
    )
    ap.add_argument(
        "--backup-dir",
        default=None,
        help=f"Backup root for --apply (default: {DEFAULT_BACKUP_ROOT}/<timestamp>).",
    )
    ap.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Print every dropped entry (default: only per-file counts).",
    )
    args = ap.parse_args(argv)

    fields = ["facts", "interests"] if args.field == "both" else [args.field]
    base_dir = Path(args.base_dir)
    mode = "APPLY" if args.apply else "DRY-RUN"

    print("=" * 64)
    print(f"  profile junk cleanup — {mode}")
    print(f"  base-dir : {base_dir}")
    print(f"  fields   : {', '.join(fields)}")
    print("=" * 64)

    if not base_dir.is_dir():
        print(f"[ERROR] base-dir not found: {base_dir}")
        return 2

    backup_root = None
    if args.apply and not args.no_backup:
        ts = time.strftime("%Y%m%d_%H%M%S")
        backup_root = Path(args.backup_dir) if args.backup_dir else (DEFAULT_BACKUP_ROOT / ts)
        backup_root.mkdir(parents=True, exist_ok=True)
        print(f"  backups  : {backup_root}")
        print("=" * 64)

    files = find_profile_files(str(base_dir))
    if not files:
        print("[INFO] no profile files found — nothing to do.")
        return 0

    total_files = 0
    changed_files = 0
    total_dropped = 0
    cat_counter: Counter = Counter()
    field_counter: Counter = Counter()
    errors = 0

    for path, layout in files:
        total_files += 1
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            errors += 1
            print(f"[WARN] skip unreadable {path}: {e}")
            continue

        if not _looks_like_profile(data):
            # Not a profile (e.g. .hardware.json at the top level) — skip silently.
            total_files -= 1
            continue

        dropped = clean_profile(data, fields)
        if not dropped:
            continue

        changed_files += 1
        total_dropped += len(dropped)
        for field, text, matched, category in dropped:
            cat_counter[category] += 1
            field_counter[field] += 1

        uid = data.get("user_id", path.parent.name if layout == "new" else path.stem)
        verb = "dropping" if args.apply else "would drop"
        print(f"\n[{layout}] {uid}  ({path})")
        print(f"  {verb} {len(dropped)} entr"
              f"{'y' if len(dropped) == 1 else 'ies'}:")
        if args.verbose:
            for field, text, matched, category in dropped:
                print(f"    - [{field}] {category} match={matched!r} :: {_clip(text)}")
        else:
            for field, text, matched, category in dropped[:3]:
                print(f"    - [{field}] {category} :: {_clip(text)}")
            if len(dropped) > 3:
                print(f"    … +{len(dropped) - 3} more (use -v to list all)")

        if args.apply:
            try:
                if backup_root is not None:
                    backup_file(path, base_dir, backup_root)
                atomic_write_json(path, data)
                print(f"  ✓ cleaned + backed up")
            except Exception as e:
                errors += 1
                print(f"  [ERROR] failed to write {path}: {e}")

    # ── Summary ──
    print("\n" + "=" * 64)
    print(f"  {mode} summary")
    print(f"  profiles scanned : {total_files}")
    print(f"  profiles affected: {changed_files}")
    print(f"  entries dropped  : {total_dropped}"
          + ("" if args.apply else "  (would-drop; use --apply to write)"))
    if field_counter:
        print("  by field         : "
              + ", ".join(f"{k}={v}" for k, v in field_counter.most_common()))
    if cat_counter:
        print("  by category      : "
              + ", ".join(f"{k}={v}" for k, v in cat_counter.most_common()))
    if errors:
        print(f"  errors           : {errors}")
    print("=" * 64)
    if not args.apply and total_dropped:
        print("  Next: re-run with --apply to write (backs up automatically).")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())

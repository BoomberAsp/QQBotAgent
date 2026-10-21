#!/usr/bin/env python3
"""
P2 migration: upgrade persisted three-tier memory files to TIER_SCHEMA_VERSION 2.

Why (Memory-Optimization-Plan ④③, batch 2):
  Batch 2 adds two per-item fields to the SHORT/MEDIUM tiers in
  agent/memory.py:TieredMemory:

    - `category` (④): one of preference | fact | event | critical. Drives the
      per-category MEDIUM age-demote threshold (critical facts decay 3× slower)
      and gates the ③ staleness label (only `fact` entries get 「截至 X 月」).
    - `last_confirmed_ts` (③): wall-clock seconds of the last reinforce/update/
      create. The injection renderer uses it to stamp stale `fact` entries.

  Files already on disk (schema_version 1, or a v1 file with no version key)
  lack both fields. TieredMemory.load_user already tolerates this — it defaults
  missing category to `fact` and last_confirmed_ts to 0.0 (unknown → no label),
  so the bot keeps working WITHOUT this migration. This script exists to make
  the on-disk state explicit and to seed a usable last_confirmed_ts so that,
  going forward, genuinely stale facts get labelled instead of staying silent.

Backfill policy (conservative):
  - category  → "fact" (DEFAULT_CATEGORY) for every legacy item. We cannot
    recover the true type from v1 data; `fact` is the safe default (normal demote
    age, eligible for the ③ label). Re-typing happens naturally on the next
    update/reinforce as the live extraction LLM tags new candidates.
  - last_confirmed_ts → the tier file's mtime. This is the best available proxy
    for "last touched". Because the ③ label only fires after
    STALE_LABEL_AFTER_DAYS (30d), nothing is labelled until 30 days AFTER this
    migration runs — no sudden flood of 「截至」labels on day one.

Safety (mirrors scripts/migrate_memory_p1.py):
  - DRY-RUN by default: prints what WOULD change, writes nothing.
  - `--apply` to write; a timestamped backup of the tiers/ tree is taken first
    unless --no-backup.
  - IDEMPOTENT: a file already at schema_version >= 2 is skipped (its items
    already carry the fields). Use --force to re-stamp v2 files anyway.
  - Atomic writes (temp + os.replace), same as TieredMemory._atomic_write_json.
  - Per-file errors are caught and reported; one bad file never aborts the run.

Usage:
  # dry-run with defaults (memory-dir = QQBot/data/memory → tiers/ underneath):
  python scripts/migrate_memory_p2_categories.py

  # PRODUCTION (pass the real memory root explicitly):
  python scripts/migrate_memory_p2_categories.py \
      --memory-dir /home/ubuntu/QQBotAgent/QQBot/data/memory

  # actually apply (backs up tiers/ first):
  python scripts/migrate_memory_p2_categories.py --apply --memory-dir ...

⚠️ Run on the PRODUCTION server only after a dry-run review and explicit operator
   confirmation. The dev machine has no live data.
"""

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import List, Optional

# Make `agent.*` importable regardless of the invocation cwd.
_QQBOT_DIR = Path(__file__).resolve().parent.parent
if str(_QQBOT_DIR) not in sys.path:
    sys.path.insert(0, str(_QQBOT_DIR))

from agent.memory import (                                 # noqa: E402
    TIER_SCHEMA_VERSION,
    DEFAULT_CATEGORY,
    VALID_CATEGORIES,
    STALE_LABEL_AFTER_DAYS,
)

DEFAULT_MEMORY_DIR = str(_QQBOT_DIR / "data" / "memory")
DEFAULT_BACKUP_ROOT = _QQBOT_DIR / "data" / "memory_p2_migration_backups"

# Item lists that carry the new per-item fields.
_ITEM_LISTS = ("short", "medium")


def _clip(s: str, n: int = 50) -> str:
    s = (s or "").replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def _read_json(path: Path) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _atomic_write_json(path: Path, data: dict) -> None:
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, str(path))


def discover_tier_files(memory_dir: Path) -> List[Path]:
    """Every tiers/*.json state file (skips the long/ subdir and .tmp partials)."""
    tiers = memory_dir / "tiers"
    if not tiers.is_dir():
        return []
    out: List[Path] = []
    for p in sorted(tiers.glob("*.json")):
        if p.name.endswith(".tmp"):
            continue
        out.append(p)
    return out


def upgrade_data(data: dict, default_ts: float, force: bool) -> tuple:
    """Return (new_data, changed, n_items, already_v2).

    Mutates a deep-ish copy: backfills category/last_confirmed_ts on each short
    and medium item and bumps schema_version. `already_v2` is True when the file
    was at target version and --force is off (caller should skip writing).
    """
    cur_version = 0
    try:
        cur_version = int(data.get("schema_version", 0) or 0)
    except Exception:
        cur_version = 0
    already_v2 = cur_version >= TIER_SCHEMA_VERSION
    if already_v2 and not force:
        return data, False, 0, True

    changed = False
    n_items = 0
    for key in _ITEM_LISTS:
        items = data.get(key)
        if not isinstance(items, list):
            continue
        for it in items:
            if not isinstance(it, dict):
                continue
            n_items += 1
            # category: keep a valid existing tag, else backfill the default.
            cat = it.get("category")
            if not (isinstance(cat, str) and cat.strip().lower() in VALID_CATEGORIES):
                it["category"] = DEFAULT_CATEGORY
                changed = True
            else:
                it["category"] = cat.strip().lower()
            # last_confirmed_ts: keep a positive existing value, else file mtime.
            ts = it.get("last_confirmed_ts")
            try:
                ts_f = float(ts) if ts is not None else 0.0
            except Exception:
                ts_f = 0.0
            if ts_f <= 0:
                it["last_confirmed_ts"] = default_ts
                changed = True

    if int(data.get("schema_version", 0) or 0) != TIER_SCHEMA_VERSION:
        data["schema_version"] = TIER_SCHEMA_VERSION
        changed = True
    return data, changed, n_items, False


def main(argv: List[str] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Upgrade persisted three-tier memory files to schema v2 (category + last_confirmed_ts).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--memory-dir", default=DEFAULT_MEMORY_DIR,
                    help=f"Memory root (tiers/ lives underneath). Default: {DEFAULT_MEMORY_DIR}")
    ap.add_argument("--apply", action="store_true",
                    help="Actually write (default is DRY-RUN: report only).")
    ap.add_argument("--force", action="store_true",
                    help="Re-stamp files already at schema_version >= 2 (default: skip them).")
    ap.add_argument("--no-backup", action="store_true",
                    help="With --apply: skip the pre-write backup of tiers/ (NOT recommended).")
    ap.add_argument("--backup-dir", default=None,
                    help=f"Backup root for --apply (default: {DEFAULT_BACKUP_ROOT}/<timestamp>).")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="Print every upgraded item.")
    args = ap.parse_args(argv)

    memory_dir = Path(args.memory_dir)
    mode = "APPLY" if args.apply else "DRY-RUN"

    print("=" * 68)
    print(f"  P2 memory schema v{ TIER_SCHEMA_VERSION} migration (category + last_confirmed_ts) — {mode}")
    print(f"  memory-dir  : {memory_dir}")
    print(f"  backfill    : category -> {DEFAULT_CATEGORY!r}, last_confirmed_ts -> file mtime")
    print(f"  ③ label     : fires only after {STALE_LABEL_AFTER_DAYS}d, so nothing is stamped until then")
    print("=" * 68)

    if not memory_dir.is_dir():
        print(f"[ERROR] memory-dir not found: {memory_dir}")
        return 2

    files = discover_tier_files(memory_dir)
    if not files:
        print("[INFO] no tiers/*.json files found — nothing to do.")
        return 0

    # Pre-write backup of the tiers/ tree.
    if args.apply and not args.no_backup:
        ts = time.strftime("%Y%m%d_%H%M%S")
        backup_root = Path(args.backup_dir) if args.backup_dir else (DEFAULT_BACKUP_ROOT / ts)
        src = memory_dir / "tiers"
        backup_root.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, backup_root / "tiers", dirs_exist_ok=True)
        print(f"  backup      : {backup_root}/tiers")
        print("=" * 68)

    n_upgraded = n_skipped = n_unchanged = errors = 0
    total_items = 0

    for path in files:
        try:
            data = _read_json(path)
            if data is None:
                errors += 1
                print(f"  [ERROR] {path.name}: unreadable / not a JSON object — skipped")
                continue
            try:
                default_ts = path.stat().st_mtime
            except Exception:
                default_ts = time.time()

            new_data, changed, n_items, already_v2 = upgrade_data(data, default_ts, args.force)

            if already_v2:
                n_skipped += 1
                if args.verbose:
                    print(f"[skip] {path.name}: already schema v{int(data.get('schema_version', 0))}")
                continue
            if not changed:
                n_unchanged += 1
                continue

            uid = new_data.get("user_id") or path.stem
            n_upgraded += 1
            total_items += n_items
            print(f"\n[user] {uid}  ({path.name})  {n_items} item(s) -> v{TIER_SCHEMA_VERSION}")
            if args.verbose:
                for key in _ITEM_LISTS:
                    for it in (new_data.get(key) or []):
                        if isinstance(it, dict):
                            print(f"    + {key:6} [{it.get('category')}] "
                                  f"ts={it.get('last_confirmed_ts')} {_clip(it.get('content', ''))}")

            if args.apply:
                _atomic_write_json(path, new_data)
                print("  ✓ written")
        except Exception as e:
            errors += 1
            print(f"  [ERROR] {path.name}: {e}")

    print("\n" + "=" * 68)
    print(f"  {mode} summary")
    print(f"  tier files found : {len(files)}")
    print(f"  files upgraded   : {n_upgraded}  ({total_items} items stamped)")
    print(f"  files skipped    : {n_skipped}  (already v{TIER_SCHEMA_VERSION})")
    if n_unchanged:
        print(f"  files unchanged  : {n_unchanged}")
    if errors:
        print(f"  errors           : {errors}")
    print("=" * 68)
    if not args.apply:
        print("  Next: re-run with --apply to write (backs up the tiers/ tree automatically).")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())

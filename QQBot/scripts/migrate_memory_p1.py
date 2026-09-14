#!/usr/bin/env python3
"""
P1 migration: seed the three-tier memory engine from existing on-disk state.

Why (Profile-Fact-Extraction-Plan.md §7 / P1 plan Stage 5):
  P1 replaces the old `_maybe_remember` raw-dump mechanism with the three-tier
  engine (SHORT/MEDIUM/LONG, see agent/memory.py:TieredMemory). The engine starts
  empty, but two kinds of real data already exist on disk and must not be lost:

    1. Raw interaction dumps written by the old MemorySystem at
       {memory-dir}/user/{safe_uid}/interaction_*.md  → ARCHIVED (moved, never
       deleted) to {memory-dir}/user/_archive/{safe_uid}/ so they stop being
       picked up by the legacy substring search but stay recoverable.

    2. The dormant `facts` list in each profile.json (P0 made it dormant but
       deliberately kept it). These are real, P0-cleaned durable facts → SEEDED
       into SHORT (count=1) so the engine can re-promote the ones the user
       actually repeats.

    3. (Optional) Facts that P0's cleanup WRONGLY dropped before Stage 0
       tightened the `正在` rule. With --restore-false-drops <backup-dir> we diff
       the pre-cleanup backup against the current profile, keep only the dropped
       facts that the CURRENT (Stage-0-tightened) filter now accepts, and seed
       those into MEDIUM (count=3) — restoring e.g. user 1114144652's two
       real-life "正在…" facts without re-admitting genuine junk.

  Per-user `extraction_count` (the logical clock) starts at 0; knowledge/ and
  system/ memories are NOT touched (P1 engine is per-user; those are shared and
  deferred to P3 — Decision 8).

Safety (mirrors scripts/cleanup_profile_facts.py):
  - DRY-RUN by default: prints what WOULD happen, writes/moves nothing.
  - `--apply` to actually write; a timestamped backup of the memory user/ tree is
    taken first unless --no-backup.
  - IDEMPOTENT: a user whose tiers/{safe_uid}.json already exists is skipped
    (use --force to reseed, which overwrites that user's tier file).
  - Atomic writes via TieredMemory.save_user (temp + os.replace).
  - Per-user errors are caught and reported; one bad user never aborts the run.

Usage:
  # dry-run with defaults (memory-dir = QQBot/data/memory,
  #   profile-dir = $USER_DATA_ROOT or QQBot/data/users_store):
  python scripts/migrate_memory_p1.py

  # PRODUCTION (SSH has no USER_DATA_ROOT — pass both roots explicitly):
  python scripts/migrate_memory_p1.py \
      --profile-dir /home/ubuntu/datadisk/QQBotData \
      --memory-dir  /home/ubuntu/QQBotAgent/QQBot/data/memory

  # actually apply (backs up the memory user/ tree first):
  python scripts/migrate_memory_p1.py --apply --profile-dir ... --memory-dir ...

  # also restore P0 false-drops from a cleanup backup, into MEDIUM:
  python scripts/migrate_memory_p1.py --apply \
      --restore-false-drops data/profile_cleanup_backups/20260914_120000 \
      --profile-dir ... --memory-dir ...

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
from typing import Dict, List, Optional, Tuple

# Make `agent.*` importable regardless of the invocation cwd.
_QQBOT_DIR = Path(__file__).resolve().parent.parent
if str(_QQBOT_DIR) not in sys.path:
    sys.path.insert(0, str(_QQBOT_DIR))

from agent.fact_filter import filter_candidate            # noqa: E402
from agent.memory import (                                 # noqa: E402
    TieredMemory,
    UserTierState,
    ShortItem,
    MediumItem,
    SHORT_MAX,
)

# Defaults mirror plugins/agent_router.py:
#   _DATA_DIR        = QQBot/data            → memory-dir = QQBot/data/memory
#   _USER_DATA_ROOT  = $USER_DATA_ROOT or QQBot/data/users_store → profile-dir
DEFAULT_MEMORY_DIR = str(_QQBOT_DIR / "data" / "memory")
DEFAULT_PROFILE_DIR = os.environ.get(
    "USER_DATA_ROOT", str(_QQBOT_DIR / "data" / "users_store")
)
DEFAULT_BACKUP_ROOT = _QQBOT_DIR / "data" / "memory_p1_migration_backups"

# Restored false-drops enter MEDIUM already-confirmed (count=3, the promotion
# threshold) with age anchored at clock 0 so they decay normally from here.
RESTORE_MEDIUM_COUNT = 3
RESTORE_ENTRY_INDEX = 0


def _safe_id(id_str: str) -> str:
    """Same sanitization TieredMemory / ProfileManager / MemorySystem use."""
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in id_str)


def _clip(s: str, n: int = 60) -> str:
    s = (s or "").replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def _read_json(path: Path) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


# ── discovery ─────────────────────────────────────────────────────

def _uid_from_interaction(name: str) -> Optional[str]:
    """Parse the uid out of a legacy dump filename `interaction_<uid>_<ts>.md`.

    The old MemorySystem wrote some raw dumps FLAT under user/ (pre per-user
    isolation) instead of user/{uid}/; the uid is embedded in the filename. We
    rsplit on the last '_' so a uid containing '_' still parses (ts is last).
    """
    if not name.startswith("interaction_") or not name.endswith(".md"):
        return None
    core = name[len("interaction_"):-len(".md")]
    return core.rsplit("_", 1)[0] if "_" in core else (core or None)


def loose_interaction_files(memory_dir: Path) -> Dict[str, List[Path]]:
    """Legacy raw dumps written FLAT under user/ → {uid: [paths]}."""
    out: Dict[str, List[Path]] = {}
    ud = memory_dir / "user"
    if not ud.is_dir():
        return out
    for p in sorted(ud.glob("interaction_*.md")):   # maxdepth 1 (not recursive)
        uid = _uid_from_interaction(p.name)
        if uid:
            out.setdefault(uid, []).append(p)
    return out


def discover_users(profile_dir: Path, memory_dir: Path) -> Dict[str, str]:
    """Return {safe_uid: real_uid} for every user with a profile or old memories.

    real_uid is taken from profile.json's user_id when available (so the tier
    file is keyed by the true id); otherwise it falls back to the safe_uid stem.
    _safe_id is idempotent over already-safe stems, so tiers/{safe_uid}.json
    resolves identically either way. Sources: profiles, per-user memory subdirs,
    AND legacy loose interaction_*.md under user/ (uid parsed from filename).
    """
    users: Dict[str, str] = {}

    if profile_dir.is_dir():
        for p in sorted(profile_dir.glob("*/profile.json")):
            safe = p.parent.name
            data = _read_json(p) or {}
            uid = data.get("user_id") or safe
            users[safe] = uid
        # legacy flat layout: {profile_dir}/{safe}.json
        for p in sorted(profile_dir.glob("*.json")):
            if p.name.endswith(".tmp"):
                continue
            safe = p.stem
            data = _read_json(p) or {}
            if "user_id" not in data and "facts" not in data:
                continue  # not a profile (e.g. .hardware.json)
            users.setdefault(safe, data.get("user_id") or safe)

    user_mem = memory_dir / "user"
    if user_mem.is_dir():
        for d in sorted(user_mem.iterdir()):
            if not d.is_dir() or d.name == "_archive":
                continue
            users.setdefault(d.name, d.name)

    # legacy loose dumps flat under user/ (uid only recoverable from filename)
    for uid in loose_interaction_files(memory_dir):
        users.setdefault(_safe_id(uid), uid)

    return users


def profile_facts(profile_dir: Path, safe: str) -> List[str]:
    """Surviving `facts` from a user's profile.json (new layout, then legacy)."""
    for path in (profile_dir / safe / "profile.json", profile_dir / f"{safe}.json"):
        data = _read_json(path)
        if data and isinstance(data.get("facts"), list):
            return [f for f in data["facts"] if isinstance(f, str) and f.strip()]
    return []


def interaction_files(memory_dir: Path, safe: str, uid: str) -> List[Path]:
    """All raw dumps for a user: those in user/{safe}/ PLUS legacy loose ones
    flat under user/ whose filename embeds this uid."""
    out: List[Path] = []
    d = memory_dir / "user" / safe
    if d.is_dir():
        out.extend(sorted(d.glob("interaction_*.md")))
    for p in loose_interaction_files(memory_dir).get(uid, []):
        if p not in out:
            out.append(p)
    return sorted(out, key=lambda p: p.name)


# ── seeding ───────────────────────────────────────────────────────

def seed_short_from_facts(facts: List[str]) -> List[ShortItem]:
    """Newest SHORT_MAX facts → SHORT items (count=1), newest at the front.

    Profile `facts` are appended over time (oldest first), so the tail is the
    newest. We keep the newest SHORT_MAX and reverse so index 0 = newest; on
    overflow the engine evicts from the tail (oldest) — the desired behavior.
    """
    recent = facts[-SHORT_MAX:]
    return [ShortItem(id=os.urandom(6).hex(), content=c, count=1) for c in reversed(recent)]


def false_drops(backup_dir: Path, profile_dir: Path, safe: str) -> List[str]:
    """Facts P0 dropped that the CURRENT filter now accepts (true false-drops).

    dropped = backup_facts − current_facts. We then re-run the Stage-0-tightened
    filter: only facts it now KEEPS were false-drops (e.g. real-life "正在…");
    facts it still rejects were correctly-dropped junk and stay dropped.
    """
    cur = set(profile_facts(profile_dir, safe))
    out: List[str] = []
    for path in (backup_dir / safe / "profile.json", backup_dir / f"{safe}.json"):
        data = _read_json(path)
        if not data or not isinstance(data.get("facts"), list):
            continue
        for f in data["facts"]:
            if not isinstance(f, str) or not f.strip() or f in cur:
                continue
            if filter_candidate(f).keep:
                out.append(f)
        break
    return out


def build_state(uid: str, short: List[ShortItem], medium: List[MediumItem]) -> UserTierState:
    return UserTierState(
        user_id=uid,
        extraction_count=0,
        short=short,
        medium=medium,
        long=[],
    )


# ── main ──────────────────────────────────────────────────────────

def main(argv: List[str] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Seed the P1 three-tier memory engine from profiles + old interaction dumps.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--memory-dir", default=DEFAULT_MEMORY_DIR,
                    help=f"Memory root (old user/ dumps + new tiers/). Default: {DEFAULT_MEMORY_DIR}")
    ap.add_argument("--profile-dir", default=DEFAULT_PROFILE_DIR,
                    help=f"Profile store root (profile.json facts). Default: {DEFAULT_PROFILE_DIR}")
    ap.add_argument("--restore-false-drops", metavar="BACKUP_DIR", default=None,
                    help="P0 cleanup backup dir; dropped facts the current filter now keeps "
                         "are seeded into MEDIUM (count=3).")
    ap.add_argument("--apply", action="store_true",
                    help="Actually write/move (default is DRY-RUN: report only).")
    ap.add_argument("--force", action="store_true",
                    help="Reseed even if tiers/{uid}.json already exists (overwrites it).")
    ap.add_argument("--no-archive", action="store_true",
                    help="Do not move old interaction_*.md into _archive/ (seed only).")
    ap.add_argument("--no-backup", action="store_true",
                    help="With --apply: skip the pre-write backup of memory user/ (NOT recommended).")
    ap.add_argument("--backup-dir", default=None,
                    help=f"Backup root for --apply (default: {DEFAULT_BACKUP_ROOT}/<timestamp>).")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="Print every seeded/restored/archived entry.")
    args = ap.parse_args(argv)

    memory_dir = Path(args.memory_dir)
    profile_dir = Path(args.profile_dir)
    backup_dir = Path(args.restore_false_drops) if args.restore_false_drops else None
    mode = "APPLY" if args.apply else "DRY-RUN"

    print("=" * 68)
    print(f"  P1 three-tier memory migration — {mode}")
    print(f"  memory-dir  : {memory_dir}")
    print(f"  profile-dir : {profile_dir}")
    print(f"  restore     : {backup_dir if backup_dir else '(off)'}")
    print("=" * 68)

    if not memory_dir.is_dir():
        print(f"[ERROR] memory-dir not found: {memory_dir}")
        return 2
    if backup_dir is not None and not backup_dir.is_dir():
        print(f"[ERROR] --restore-false-drops dir not found: {backup_dir}")
        return 2

    users = discover_users(profile_dir, memory_dir)
    if not users:
        print("[INFO] no users found — nothing to do.")
        return 0

    tm = TieredMemory(base_dir=str(memory_dir))

    # Pre-write backup of the user/ tree we're about to move files within.
    backup_root: Optional[Path] = None
    if args.apply and not args.no_backup and not args.no_archive:
        ts = time.strftime("%Y%m%d_%H%M%S")
        backup_root = Path(args.backup_dir) if args.backup_dir else (DEFAULT_BACKUP_ROOT / ts)
        src = memory_dir / "user"
        if src.is_dir():
            backup_root.mkdir(parents=True, exist_ok=True)
            shutil.copytree(src, backup_root / "user", dirs_exist_ok=True)
            print(f"  backup      : {backup_root}/user")
            print("=" * 68)

    n_seeded = n_restored = n_archived = n_skipped = errors = 0
    total_short = total_medium = total_files = 0

    for safe, uid in sorted(users.items()):
        try:
            tier_path = Path(tm._tier_path(uid))
            # Seed only when there is no tier yet (or --force). NEVER clobber a
            # tier the now-live bot may have created since restart. Archiving is
            # decoupled: it runs even when seeding is skipped, so legacy dumps
            # still get tidied regardless of live-bot timing.
            seed = (not tier_path.exists()) or args.force

            short: List[ShortItem] = []
            medium: List[MediumItem] = []
            if seed:
                facts = profile_facts(profile_dir, safe)
                short = seed_short_from_facts(facts)
                if backup_dir is not None:
                    for c in false_drops(backup_dir, profile_dir, safe):
                        medium.append(MediumItem(
                            id=os.urandom(6).hex(), content=c,
                            count=RESTORE_MEDIUM_COUNT, entry_extraction_index=RESTORE_ENTRY_INDEX,
                        ))

            ifs = interaction_files(memory_dir, safe, uid)
            do_archive = bool(ifs) and not args.no_archive

            if not seed and not do_archive:
                n_skipped += 1
                if args.verbose:
                    print(f"[skip] {uid}: tier exists, nothing to archive")
                continue
            if seed and not short and not medium and not do_archive:
                continue  # nothing for this user

            if short:
                n_seeded += 1
            if medium:
                n_restored += 1
            total_short += len(short)
            total_medium += len(medium)
            total_files += len(ifs)

            head = f"\n[user] {uid}  (safe={safe})" + ("" if seed else "  [tier exists → seed skipped]")
            print(head)
            if seed:
                print(f"  SHORT  <- {len(short)} dormant fact(s) (count=1)")
                if medium:
                    print(f"  MEDIUM <- {len(medium)} restored false-drop(s) (count={RESTORE_MEDIUM_COUNT})")
            if do_archive:
                print(f"  archive: {len(ifs)} interaction_*.md -> user/_archive/{safe}/")
            if args.verbose:
                for s in short:
                    print(f"    + SHORT  {_clip(s.content)}")
                for m in medium:
                    print(f"    + MEDIUM {_clip(m.content)}")
                for p in ifs:
                    print(f"    > arch   {p.name}")

            if args.apply:
                acted = []
                # 1. seed tier state (atomic JSON) — only when seeding, never clobber
                if seed and (short or medium):
                    tm._states[uid] = build_state(uid, short, medium)
                    tm.save_user(uid)
                    acted.append("seeded")
                # 2. archive old raw dumps (move, never delete) — independent of seeding
                if do_archive:
                    dest_dir = memory_dir / "user" / "_archive" / safe
                    dest_dir.mkdir(parents=True, exist_ok=True)
                    for p in ifs:
                        shutil.move(str(p), str(dest_dir / p.name))
                        n_archived += 1
                    acted.append("archived")
                print(f"  ✓ {' + '.join(acted) if acted else 'no-op'}")
        except Exception as e:
            errors += 1
            print(f"  [ERROR] {uid}: {e}")

    print("\n" + "=" * 68)
    print(f"  {mode} summary")
    print(f"  users discovered : {len(users)}")
    print(f"  users seeded     : {n_seeded}  ({total_short} SHORT items)")
    print(f"  users restored   : {n_restored}  ({total_medium} MEDIUM items)")
    print(f"  users skipped    : {n_skipped}  (tier exists & nothing to archive)")
    if not args.no_archive:
        print(f"  files archived   : {total_files if not args.apply else n_archived}"
              + ("" if args.apply else "  (would-move; use --apply to write)"))
    if errors:
        print(f"  errors           : {errors}")
    print("=" * 68)
    if not args.apply:
        print("  Next: re-run with --apply to write (backs up the user/ tree automatically).")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())

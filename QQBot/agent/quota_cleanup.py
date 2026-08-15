"""
Quota cleanup — pure helpers for the elastic-quota cleanup protocol.

Kept free of NoneBot / plugin / network / singleton dependencies so the
decision logic (candidate enumeration, deletion ordering, response parsing)
can be unit-tested on a development machine.

Used by ``agent_router`` for Feature 2 (``delete_workspace_file`` + the
quota-cleanup protocol) and by ``UserWorkspaceManager.list_files_by_mtime``.
"""

import os
import re
import shutil
import time
from dataclasses import dataclass
from typing import List, Optional

from agent.workspace_snapshot import fmt_bytes, should_skip

# Items under workspace are deletable units. Hidden files (`.` prefix) and
# cache dirs / compiled files (handled by ``should_skip``) are never touched.
def _skip(name: str) -> bool:
    return should_skip(name) or name.startswith(".")


@dataclass
class CleanupCandidate:
    rel_path: str   # path relative to workspace root (e.g. "uploads/a.png")
    abs_path: str   # absolute path on disk
    size: int       # bytes (for repos: recursive sum of non-skipped files)
    mtime: float    # epoch seconds (for repos: earliest file mtime inside)
    kind: str       # "file" | "repo"


@dataclass
class CleanupDecision:
    mode: str      # "confirm" | "skip" | "keep" | "explicit" | "unclear"
    targets: list  # selected CleanupCandidate objects to delete


# ── Candidate enumeration ─────────────────────────────────────────

def list_candidates(workspace_root: str) -> List[CleanupCandidate]:
    """Enumerate deletable items (files + top-level repos) sorted by mtime asc.

    - Regular files anywhere except the ``repos/`` subtree (handled below).
    - Each top-level directory under ``repos/`` as a single "repo" unit.
    - Skips hidden files, cache dirs / compiled files, and symlinks.
    """
    items: List[CleanupCandidate] = []
    if not os.path.isdir(workspace_root):
        return items

    # 1. Regular files (skip the repos/ subtree — enumerated as whole repos).
    for dirpath, dirnames, filenames in os.walk(workspace_root):
        dirnames[:] = [d for d in dirnames if not _skip(d)]
        rel_dir = os.path.relpath(dirpath, workspace_root)
        if rel_dir == "repos" or rel_dir.startswith("repos" + os.sep):
            dirnames[:] = []
            continue
        for f in filenames:
            if _skip(f):
                continue
            abs_path = os.path.join(dirpath, f)
            try:
                st = os.stat(abs_path)
            except OSError:
                continue
            items.append(CleanupCandidate(
                rel_path=os.path.relpath(abs_path, workspace_root),
                abs_path=abs_path,
                size=st.st_size,
                mtime=st.st_mtime,
                kind="file",
            ))

    # 2. Top-level repos as whole units.
    repos_dir = os.path.join(workspace_root, "repos")
    if os.path.isdir(repos_dir):
        for entry in sorted(os.listdir(repos_dir)):
            if _skip(entry):
                continue
            repo_path = os.path.join(repos_dir, entry)
            if not os.path.isdir(repo_path) or os.path.islink(repo_path):
                continue
            size = 0
            earliest = None
            for rp, rdirs, rfiles in os.walk(repo_path):
                rdirs[:] = [d for d in rdirs if not _skip(d)]
                for rf in rfiles:
                    if _skip(rf):
                        continue
                    try:
                        st = os.stat(os.path.join(rp, rf))
                    except OSError:
                        continue
                    size += st.st_size
                    if earliest is None or st.st_mtime < earliest:
                        earliest = st.st_mtime
            if earliest is None:
                try:
                    earliest = os.stat(repo_path).st_mtime
                except OSError:
                    earliest = 0.0
            items.append(CleanupCandidate(
                rel_path=os.path.relpath(repo_path, workspace_root),
                abs_path=repo_path,
                size=size,
                mtime=earliest,
                kind="repo",
            ))

    items.sort(key=lambda c: (c.mtime, c.rel_path))
    return items


def workspace_size(workspace_root: str) -> int:
    """Total bytes under workspace_root (skipping caches / hidden entries)."""
    total = 0
    for dirpath, dirnames, filenames in os.walk(workspace_root):
        dirnames[:] = [d for d in dirnames if not _skip(d)]
        for f in filenames:
            if _skip(f):
                continue
            try:
                total += os.path.getsize(os.path.join(dirpath, f))
            except OSError:
                pass
    return total


# ── Deletion ──────────────────────────────────────────────────────

def _within(path: str, root: str) -> bool:
    root_real = os.path.realpath(root)
    path_real = os.path.realpath(path)
    return path_real == root_real or path_real.startswith(root_real + os.sep)


def delete_candidate(candidate: CleanupCandidate) -> bool:
    """Delete a candidate file/repo. Returns True on success."""
    try:
        if candidate.kind == "repo":
            shutil.rmtree(candidate.abs_path)
        else:
            os.remove(candidate.abs_path)
        return True
    except OSError:
        return False


def execute_cleanup(workspace_root: str, quota_bytes: int,
                    candidates: Optional[List[CleanupCandidate]] = None,
                    target_ratio: float = 0.8) -> dict:
    """Delete candidates (mtime asc) until usage drops under the target.

    Args:
        workspace_root: absolute path to the workspace.
        quota_bytes: per-user quota in bytes.
        candidates: snapshot of candidates to consider (defaults to a fresh
            ``list_candidates``). Only objects in this list are ever deleted,
            so files added after the snapshot are untouched.
        target_ratio: stop when usage <= quota_bytes * target_ratio.
            Pass 0.0 to always delete every candidate (explicit user choice).

    Returns a dict: deleted (list), freed_bytes, usage_before, usage_after.
    """
    if candidates is None:
        candidates = list_candidates(workspace_root)
    usage_before = workspace_size(workspace_root)
    target = int(quota_bytes * target_ratio)
    deleted: List[CleanupCandidate] = []
    freed = 0
    for c in candidates:
        if usage_before - freed <= target:
            break
        if not os.path.exists(c.abs_path):
            continue
        if not _within(c.abs_path, workspace_root):
            continue
        if delete_candidate(c):
            deleted.append(c)
            freed += c.size
    return {
        "deleted": deleted,
        "freed_bytes": freed,
        "usage_before": usage_before,
        "usage_after": workspace_size(workspace_root),
    }


# ── Response parsing ──────────────────────────────────────────────

_CONFIRM_KEYWORDS = {
    "删吧", "删", "确认", "确定", "删除", "ok", "好的", "可以",
    "就按这个", "按这个", "全部删", "全删", "清理", "清理吧",
}
_SKIP_KEYWORDS = (
    "不删", "不清理", "跳过", "取消", "先不", "先不清理", "不要", "算了", "暂不",
)
_CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩"


def match_candidates(text: str, candidates: List[CleanupCandidate]) -> List[CleanupCandidate]:
    """Return candidates referenced by index (1-based arabic or circled) or
    by their rel_path substring appearing in ``text``."""
    matched: List[CleanupCandidate] = []
    for i, c in enumerate(candidates, 1):
        if c.rel_path in text or os.path.basename(c.rel_path) in text:
            matched.append(c)
            continue
        if re.search(rf'(?<!\d){i}(?!\d)', text):
            matched.append(c)
            continue
        if i <= len(_CIRCLED) and _CIRCLED[i - 1] in text:
            matched.append(c)
    seen = set()
    out: List[CleanupCandidate] = []
    for c in matched:
        if c.rel_path not in seen:
            seen.add(c.rel_path)
            out.append(c)
    return out


def resolve_targets(text: str, candidates: List[CleanupCandidate]) -> CleanupDecision:
    """Interpret a user's cleanup-protocol response.

    Branches (in priority order):
    - skip keywords → reject (must clean up)
    - exact confirm keyword → delete all candidates
    - "保留"/"留下" → keep the matched candidates, delete the rest
    - explicit path/index matches → delete only those
    - otherwise → unclear (caller prompts for clarification)
    """
    t = text.strip()
    tl = t.lower()
    for k in _SKIP_KEYWORDS:
        if k in tl:
            return CleanupDecision("skip", [])
    if tl in _CONFIRM_KEYWORDS:
        return CleanupDecision("confirm", list(candidates))
    if "保留" in t or "留下" in t:
        kept = match_candidates(t, candidates)
        if not kept:
            return CleanupDecision("unclear", [])
        targets = [c for c in candidates if c not in kept]
        if not targets:
            return CleanupDecision("unclear", [])
        return CleanupDecision("keep", targets)
    explicit = match_candidates(t, candidates)
    if explicit:
        return CleanupDecision("explicit", explicit)
    return CleanupDecision("unclear", [])


# ── Formatting ────────────────────────────────────────────────────

def format_candidate(candidate: CleanupCandidate, index: int) -> str:
    mtime = time.strftime("%Y-%m-%d %H:%M", time.localtime(candidate.mtime))
    return f"  {index}. {candidate.rel_path}  {fmt_bytes(candidate.size)}  {mtime}"


def format_cleanup_prompt(candidates: List[CleanupCandidate],
                          usage_mb: float, quota_mb: int) -> str:
    """Render the CLEANUP_WAITING entry message."""
    lines = [
        f"⚠️ 你的工作区已超出配额（{usage_mb:.1f} MB / {quota_mb} MB）。",
        "为继续使用，我需要删除「修改时间最早」的文件：",
        "",
    ]
    for i, c in enumerate(candidates[:6], 1):
        lines.append(format_candidate(c, i))
    if len(candidates) > 6:
        lines.append(f"  … 及其余 {len(candidates) - 6} 项")
    lines += [
        "",
        "你可以指定要保留的文件、或指定删除其他文件来替换清单。",
        "此操作无法跳过。",
        "",
        "⏳ 10 分钟内未收到你的指令，我将自动删除上述候选文件。",
    ]
    return "\n".join(lines)

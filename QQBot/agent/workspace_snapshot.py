"""
Workspace snapshot — pure helpers for rendering a user workspace tree.

Kept free of NoneBot / plugin / network / singleton dependencies so these can be
unit-tested on a development machine (no NapCat, no running bot).

Used by ``agent_router.get_user_info`` to build the「工作区目录快照」section.
"""

import os
from pathlib import Path
from typing import List

# Cache directories / files skipped from the snapshot.
SKIP_DIRS = {
    ".git", "__pycache__", "node_modules", ".cache", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".ipynb_checkpoints",
}
SKIP_EXT = (".pyc", ".pyo")
MAX_TREE_ENTRIES = 20  # max entries listed per directory
MAX_TREE_DEPTH = 4     # max recursion depth for the tree


def fmt_bytes(size: int) -> str:
    """Format a byte count as a human-readable string."""
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    if size < 1024 * 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    return f"{size / (1024 * 1024 * 1024):.2f} GB"


def should_skip(name: str) -> bool:
    """Return True for cache dirs / compiled files that should be hidden."""
    return name in SKIP_DIRS or name.endswith(SKIP_EXT)


def list_entries(path: Path) -> List[Path]:
    """List entries, skipping caches, sorted dirs-first then case-insensitive name."""
    try:
        entries = [e for e in path.iterdir() if not should_skip(e.name)]
    except (PermissionError, OSError):
        return []
    entries.sort(key=lambda e: (not e.is_dir(), e.name.lower()))
    return entries


def dir_size(path: Path) -> int:
    """Recursive byte size of a directory, skipping caches."""
    total = 0
    try:
        for dirpath, dirnames, filenames in os.walk(path):
            dirnames[:] = [d for d in dirnames if not should_skip(d)]
            for f in filenames:
                if should_skip(f):
                    continue
                try:
                    total += os.path.getsize(os.path.join(dirpath, f))
                except OSError:
                    pass
    except OSError:
        pass
    return total


def rel_to_root(path: str, root: str) -> str:
    """Return ``path`` relative to ``root``, hiding the absolute prefix.

    Example: ``rel_to_root("/data/root/2578260985/workspace", "/data/root")``
    returns ``"2578260985/workspace"``. Falls back to the absolute path when the
    two paths share no common prefix (e.g. different drives on Windows).
    """
    try:
        return os.path.relpath(path, root)
    except ValueError:
        return path


def build_tree(root: Path, root_label: str, indent: str = "  ") -> List[str]:
    """Render the workspace as a human-readable tree.

    - directories first, then files, both alphabetical (case-insensitive)
    - ``.git`` / cache dirs / ``.pyc`` files skipped
    - each node annotated with size; per-directory entry cap + depth cap
    - symlinks rendered as leaf nodes (never recursed into)
    """
    lines = [f"{indent}{root_label}"]
    if not root.is_dir():
        lines.append(f"{indent}(工作区目录不存在)")
        return lines

    def render(path: Path, prefix: str, is_last: bool, depth: int) -> None:
        connector = "└── " if is_last else "├── "
        if path.is_symlink():
            lines.append(f"{indent}{prefix}{connector}{path.name} -> (符号链接)")
            return
        if path.is_dir():
            children = list_entries(path)
            if depth >= MAX_TREE_DEPTH and children:
                lines.append(f"{indent}{prefix}{connector}{path.name}/ "
                             f"(…深度限制, {len(children)} 项)")
                return
            if not children:
                lines.append(f"{indent}{prefix}{connector}{path.name}/ (空)")
                return
            lines.append(f"{indent}{prefix}{connector}{path.name}/ "
                         f"({len(children)} 项, {fmt_bytes(dir_size(path))})")
            child_prefix = prefix + ("    " if is_last else "│   ")
            shown = children[:MAX_TREE_ENTRIES]
            for i, child in enumerate(shown):
                render(child, child_prefix, i == len(shown) - 1, depth + 1)
            if len(children) > len(shown):
                lines.append(f"{indent}{child_prefix}… 还有 "
                             f"{len(children) - len(shown)} 项未显示")
        else:
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            lines.append(f"{indent}{prefix}{connector}{path.name} ({fmt_bytes(size)})")

    children = list_entries(root)
    if not children:
        lines.append(f"{indent}(空)")
        return lines
    shown = children[:MAX_TREE_ENTRIES]
    for i, child in enumerate(shown):
        render(child, "", i == len(shown) - 1, 0)
    if len(children) > len(shown):
        lines.append(f"{indent}… 还有 {len(children) - len(shown)} 项未显示")
    return lines

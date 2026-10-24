#!/usr/bin/env python3
"""Small follow-up tweaks to the already-patched agent/turn.py."""
from __future__ import annotations

import os
import sys

ROOT = os.path.expanduser("~/QQBotAgent/QQBot")
PATH = os.path.join(ROOT, "agent", "turn.py")

OLD = """    if not text or text.startswith("/") or len(text) < 4:
        return ""
    return text[:limit]"""

NEW = """    # 2-char pings ("在吗" / "ok") are not story questions, but a 3-char
    # Chinese ask ("这是谁") can be — so the floor is 3.
    if not text or text.startswith("/") or len(text) < 3:
        return ""
    return text[:limit]"""


def main() -> int:
    with open(PATH, "r", encoding="utf-8") as f:
        src = f.read()
    if NEW in src:
        print("== skip (already applied)")
        return 0
    if src.count(OLD) != 1:
        print(f"!! anchor matched {src.count(OLD)}x")
        return 1
    with open(PATH, "w", encoding="utf-8") as f:
        f.write(src.replace(OLD, NEW, 1))
    print("ok patched turn.py (query floor 4 -> 3)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

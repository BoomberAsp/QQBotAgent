#!/usr/bin/env python3
"""Final textual touches: .gitignore note + copy the design doc into the repo."""
from __future__ import annotations

import os
import shutil
import sys

ROOT = os.path.expanduser("~/QQBotAgent")
GITIGNORE = os.path.join(ROOT, ".gitignore")

OLD = """!QQBot/data/personality_config.json
!QQBot/data/group_personality.json"""
NEW = """!QQBot/data/personality_config.json
!QQBot/data/group_personality.json
# 原作剧情知识库（wiki 快照 + LLM 中文增强 + SQLite，全部为可再生产物）
# 重建：cd QQBot && python scripts/build_story_kb.py --all
QQBot/data/story_kb/"""

DOC_SRC = "/mnt/c/Users/Windows11/Documents/deepseek-harness/default-workspace/staging/Story-Knowledge-Base-Design.md"
DOC_DST = os.path.join(ROOT, "Story-Knowledge-Base-Design.md")


def main() -> int:
    rc = 0
    with open(GITIGNORE, "r", encoding="utf-8") as f:
        src = f.read()
    if "QQBot/data/story_kb/" in src:
        print("== skip gitignore (already present)")
    elif src.count(OLD) != 1:
        print(f"!! gitignore anchor matched {src.count(OLD)}x")
        rc = 1
    else:
        with open(GITIGNORE, "w", encoding="utf-8") as f:
            f.write(src.replace(OLD, NEW, 1))
        print("ok .gitignore annotated")

    shutil.copyfile(DOC_SRC, DOC_DST)
    print(f"ok design doc -> {DOC_DST} "
          f"({os.path.getsize(DOC_DST)} bytes)")
    return rc


if __name__ == "__main__":
    sys.exit(main())

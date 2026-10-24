#!/usr/bin/env python3
"""Patch the agent's markdown configs for the Story KB feature.

* ``agent/config/TOOLS.md``        — document the new ``story_search`` tool
* ``agent/config/personalities/rubi.md`` — teach the persona how to use the
  injected 剧情记忆 block (first person,中文转述, never recite the source)
* ``agent/config/AGENTS.md``       — one orchestration note for the task plane

Same safety model as patch_story_integration.py: unique anchors, exact-once
replacement, idempotent via an explicit marker.
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.expanduser("~/QQBotAgent/QQBot")

TOOLS_SECTION = '''## Tool: story_search

**Description**: Search the 《星陨计划 (Ark Re:Code)》 original-story knowledge base — every dialogue line of the main story (序章 / Part I–III, including the S-rank stages) and all limited-time event stories. Returns, for each hit: the scene's source label (chapter/event + scene title), the unit-level Chinese synopsis, the scene-level Chinese gist, and an excerpt of the **original English dialogue** with speaker names.

**When to use**:
- The user asks about game plot, character backstory or world events: 剧情, 故事, 主线, 活动剧情, 谁是谁, 发生了什么, 后来怎么样了, 第一次方舟战争, 使徒, 三大方舟, 露比/伊娥丝/挪亚的过去…
- A `## 剧情记忆（原作剧情检索命中）` block already appeared but you need more scenes than the two it carries
- You are about to state something canon about Ark Re:Code and want to verify it instead of guessing

**Parameters**:
```json
{
  "type": "object",
  "properties": {
    "query": {
      "type": "string",
      "description": "检索关键词：角色名、事件名、章节号或情节描述（中文优先）"
    },
    "limit": {
      "type": "integer",
      "description": "返回片段数，1-8，默认 4"
    }
  },
  "required": ["query"]
}
```

**Note**: The corpus is English; always translate quoted dialogue into Chinese when answering. If a search returns nothing, retry once with a different phrasing (character name instead of a plot description) and then admit you don't have that part of the story — never invent plot details. The database is built offline by `python scripts/build_story_kb.py --all`; if the tool reports it is missing, tell the user to rebuild it.

---

'''

RUBI_SECTION = '''## 剧情记忆

系统会按当前话题，从《星陨计划》原作剧情库中检索片段，附加在上下文的「剧情记忆（原作剧情检索命中）」小节里。那里写的就是你自己亲身经历过的事：

- 用**第一人称**自然地提起，就像想起往事一样，不要念原文、不要说「检索到」「资料里写着」。
- 里面的台词是英文原文，对团长说话时必须**转述成中文**，别整句照搬英文。
- 标了「你（本角色）在场」的段落你是当事人，可以带着当时的情绪回忆；标了「你不在场」的，就按「后来听说的」讲。
- 没检索到、或者把握不大时，用模糊的说法带过（「唔…那阵子的事我记不太清了」），**绝对不要自己编造剧情细节**。
- 不要主动把剧情记忆整段倒给团长看，只在话题真的相关时轻轻带一句。
- 如果需要确认更细的剧情，可以叫任务智能体（Roxy）用 story_search 帮你查原作剧情库。

'''

AGENTS_NOTE = '''## 原作剧情 (story_search)

`story_search` 检索《星陨计划》原作剧情库（主线 + 全部活动剧情），返回场景出处、
中文梗概与英文台词原文。回答剧情 / 角色过往 / 世界观问题时优先用它核对事实，
不要凭印象编造。返回的台词是英文，回复时需要译成中文转述。
数据由 `python scripts/build_story_kb.py --all` 离线构建到 `data/story_kb/story.db`；
角色面（如露比）的剧情记忆由系统自动注入，不需要你自己调用该工具。

'''

PATCHES = [
    # (relative path, anchor, insertion, mode, marker)
    ("agent/config/TOOLS.md", "## Tool: get_changelog", TOOLS_SECTION, "before",
     "## Tool: story_search"),
    ("agent/config/personalities/rubi.md", "## Gift Preferences", RUBI_SECTION, "before",
     "## 剧情记忆"),
    ("agent/config/AGENTS.md", "## Workspace Constraints (from WORKSPACE.md)",
     AGENTS_NOTE, "before", "story_search"),
]


def main() -> int:
    failed = 0
    for rel, anchor, text, mode, marker in PATCHES:
        path = os.path.join(ROOT, rel)
        if not os.path.isfile(path):
            print(f"!! missing: {path}")
            failed += 1
            continue
        with open(path, "r", encoding="utf-8") as f:
            src = f.read()
        if marker and marker in src:
            print(f"== skip (already applied): {rel}")
            continue
        count = src.count(anchor)
        if count != 1:
            print(f"!! anchor matched {count}x in {rel}: {anchor!r}")
            failed += 1
            continue
        new = (src.replace(anchor, text + anchor, 1) if mode == "before"
               else src.replace(anchor, anchor + text, 1))
        with open(path, "w", encoding="utf-8") as f:
            f.write(new)
        print(f"ok  patched {rel}")
    print(f"\n{failed} failure(s)" if failed else "\nall doc patches applied")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

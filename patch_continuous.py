#!/usr/bin/env python3
"""Add story-memory injection to the continuous-mode (group) turn path.

handle_agent_turn was patched at implementation time; handle_continuous_turn
has the same plane/contextvar structure (12-space indentation) and needs the
same block so a character keeps her story memory inside a group continuous
window.
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.expanduser("~/QQBotAgent/QQBot")
PATH = os.path.join(ROOT, "agent", "turn.py")

ANCHOR = '''            _delegate_depth.set(0)
            _personality_transition.set(_personality_switch_marker.get(user_id, ""))

            response = await asyncio.wait_for(
                agent.run(augmented_message, user_id, client=client,
                           progress_callback=lambda msg: _safe_send(OutProgress(msg), sender=continuous_sender),'''

ADDITION = '''            _delegate_depth.set(0)
            _personality_transition.set(_personality_switch_marker.get(user_id, ""))

            # ── 原作剧情记忆注入（与 handle_agent_turn 同一策略）──
            # 群聊连续模式下角色面同样可见剧情记忆；任务面不注入，
            # 改由 story_search 工具深挖。
            _story_block, _story_char = "", ""
            if character_plane or persona_mode != "split":
                if persona_key and persona_key != "assistant":
                    _story_block, _story_char = _build_story_context(
                        persona_key, text_content or augmented_message, pm)
            _current_story_context.set(_story_block)
            _current_story_char_id.set(_story_char)

            response = await asyncio.wait_for(
                agent.run(augmented_message, user_id, client=client,
                           progress_callback=lambda msg: _safe_send(OutProgress(msg), sender=continuous_sender),'''


def main() -> int:
    with open(PATH, "r", encoding="utf-8") as f:
        src = f.read()
    if src.count(ADDITION) == 1:
        print("== skip (already applied)")
        return 0
    if src.count(ANCHOR) != 1:
        print(f"!! anchor matched {src.count(ANCHOR)}x")
        return 1
    with open(PATH, "w", encoding="utf-8") as f:
        f.write(src.replace(ANCHOR, ADDITION, 1))
    print("ok patched continuous-mode story injection")
    return 0


if __name__ == "__main__":
    sys.exit(main())

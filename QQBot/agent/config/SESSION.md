# Session — Conversation Session Configuration

## Session Parameters

| Parameter | Value | Description |
|-----------|-------|-------------|
| **max_context_messages** | 20 | Maximum conversation history messages kept per user |
| **max_context_turns** | 10 | Maximum conversation turns (user + assistant pairs) |
| **session_timeout** | 1800 | Session inactivity timeout in seconds (30 minutes) |
| **max_tool_calls_per_turn** | 20 | Maximum tool invocations per user message |
| **tool_timeout** | 60 | Default tool execution timeout in seconds |
| **thinking_timeout** | 180 | Maximum time for LLM reasoning in seconds |
| **reminder_interval** | 15 | Seconds between "still thinking" reminders |

## Session Lifecycle

```
User sends message
      │
      ▼
┌──────────────┐     Session exists?
│  Lookup       │────Yes──→ Load context from memory
│  user_id      │
└──────┬───────┘
       │ No
       ▼
┌──────────────┐
│  Create new   │  Initialize empty context
│  session      │  Load USER.md template
└──────┬───────┘
       │
       ▼
┌──────────────┐
│  Process      │  Agent loop (THINK→ACT→OBSERVE→RESPOND)
│  message      │
└──────┬───────┘
       │
       ▼
┌──────────────┐
│  Update       │  Append user msg + agent response to context
│  session      │  Trim to max_context_messages
└──────┬───────┘
       │
       ▼
┌──────────────┐
│  Persist      │  Save session state to disk
│  (optional)   │  (enables session survival across restarts)
└──────────────┘
```

## Context Trimming Strategy

When context exceeds `max_context_messages`:
1. Keep the most recent messages (FIFO)
2. Always preserve the system prompt (SOUL + IDENTITY + TOOLS + AGENTS)
3. Truncate from the oldest user/assistant pairs first

## Session Persistence

系统有三套独立的存储系统，功能不同、路径不同、互不干扰：

### 存储架构总览

| 存储系统 | 路径 | 管理者 | 用途 |
|---------|------|--------|------|
| **临时会话** | `data/sessions/{uid}.json` | `SessionManager` (`agent/session.py`) | 默认会话，最近 20 条消息，30 分钟过期 |
| **特殊会话** | `{USER_DATA_ROOT}/{uid}/sessions/{name}/` | `SpecialSessionManager` (`agent/special_session.py`) | 持久化长对话，完整上下文，快照+增量双层存储 |
| **用户工作区** | `{USER_DATA_ROOT}/{uid}/workspace/` | `UserWorkspaceManager` (`agent/workspace.py`) | 用户独立文件空间，500MB 配额，QQ 号隔离 |

**重要**: 临时会话 (`data/sessions/`) 和特殊会话 (`users_store/.../sessions/`) 是完全独立的存储系统，互不关联。不要将它们描述为同一系统的不同"层"。

### 临时会话持久化

临时会话存储为单个 JSON 文件：

```
QQBot/data/sessions/{user_id}.json
```

Each session file contains:
```json
{
  "user_id": "123456789",
  "created_at": "2026-05-26T10:00:00",
  "last_active": "2026-05-26T10:30:00",
  "context": [
    {"role": "user", "content": "..."},
    {"role": "assistant", "content": "..."}
  ],
  "tool_call_count": 5,
  "metadata": {}
}
```

## 会话人格绑定

特殊会话可在 `_index.json` 的 entry `metadata.persona_key` 上注册人格，切换会话时自动切换到该会话绑定的人格（系统发提示消息）。

- **绑定时机**：`/新会话`、`/保存为会话` 创建时快照当前生效人格；剧情会话 (bond_scene) 创建时钉住 `char_id`；角色创作会话 (char_creation) 不绑定（固定中性助手）。
- **解析优先级**：剧情钉住 > 会话绑定 > 个人设置 > 群默认 > 全局默认（`CharacterRegistry.resolve_effective_personality` 单点解析，活跃会话经 `set_session_provider` 注入）。
- **切换规则**：特殊会话内 `/人格切换` 只写本会话绑定（用户默认不变）；`/人格切换 <名称> --全局` 修改用户默认；临时会话内切换仍写用户默认（临时会话不绑定人格）；剧情/创作会话内拒绝切换。
- **懒校验**：绑定 key 失效（内置卡被删/UGC 角色被删）时静默穿透到下一优先级层，存量无绑定的旧会话同样穿透，零迁移。
- `/clear` 清空会话历史但**保留**人格绑定（清历史 ≠ 换人格）。

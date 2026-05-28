# Session — Conversation Session Configuration

## Session Parameters

| Parameter | Value | Description |
|-----------|-------|-------------|
| **max_context_messages** | 20 | Maximum conversation history messages kept per user |
| **max_context_turns** | 10 | Maximum conversation turns (user + assistant pairs) |
| **session_timeout** | 1800 | Session inactivity timeout in seconds (30 minutes) |
| **max_tool_calls_per_turn** | 5 | Maximum tool invocations per user message |
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

Sessions are stored at: `QQBot/data/sessions/{user_id}.json`

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

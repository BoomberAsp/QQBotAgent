# Bootstrap — Agent Initialization Sequence

This document defines what happens when the agent starts up.

## Initialization Order

```
1. Load IDENTITY.md       → Agent name, version, capabilities
2. Load SOUL.md           → Personality and behavior rules
3. Load TOOLS.md          → Available tool definitions
4. Load AGENTS.md         → Orchestration rules
5. Load SESSION.md        → Session configuration
6. Load MEMORY.md         → Long-term memory index
7. Register built-in tools → ToolRegistry initialization
8. Verify LLM connection   → Health check to DeepSeek API
9. Verify QQ connection    → Health check to Napcat WebSocket
10. Start HEARTBEAT.md     → Begin periodic health checks
11. Agent READY            → Begin accepting messages
```

## Tool Registration

On bootstrap, the following tools are registered by default:

| Tool Name | Module | Status |
|-----------|--------|--------|
| `search_web` | `tools.builtin_tools` | Required |
| `check_weather` | `tools.builtin_tools` | Required |
| `execute_code` | `tools.builtin_tools` | Required |
| `translate_text` | `tools.builtin_tools` | Required |
| `get_time` | `tools.builtin_tools` | Required |
| `gacha_pull` | `tools.legacy_tools` | Optional |
| `calculate_speed` | `tools.legacy_tools` | Optional |
| `compare_speed_probability` | `tools.legacy_tools` | Optional |
| `explain_code` | `tools.legacy_tools` | Optional |

## Startup Health Checks

- **DeepSeek API**: Send a minimal chat completion request. If it fails, log error and retry 3 times with 5s interval.
- **Napcat WebSocket**: Check if the reverse WebSocket connection is established. If not, log warning (QQ may still be logging in).
- **Disk Space**: Ensure at least 100MB free for session/memory persistence.

## Fallback Behavior

If the DeepSeek API is unreachable after retries:
1. Log critical error
2. Enter degraded mode: respond to all messages with "Roxy 正在维护中，请稍后再试~"
3. Retry connection every 60 seconds

If tools fail to register:
- Required tools → critical error, agent won't start
- Optional tools → log warning, agent starts without them

# Bootstrap — Agent Initialization Sequence

This document defines what happens when the agent starts up.

## Initialization Order

```
1. Load IDENTITY.md       → Agent name, version, capabilities
2. Load SOUL.md           → Personality and behavior rules
3. Load TOOLS.md          → Available tool definitions
4. Load AGENTS.md         → Orchestration rules
5. Load WORKSPACE.md      → Capability boundaries and workspace constraints
6. Load SESSION.md        → Session configuration
7. Load MEMORY.md         → Long-term memory index
8. Register built-in tools → ToolRegistry initialization
9. Verify LLM connection   → Health check to DeepSeek API
10. Verify QQ connection    → Health check to Napcat WebSocket
11. Start HEARTBEAT.md     → Begin periodic health checks
12. Agent READY            → Begin accepting messages
```

## Tool Registration

On bootstrap, the following tools are registered by default:

**Built-in tools** (`tools.builtin_tools`) — Required:
| Tool Name | Description |
|-----------|-------------|
| `search_web` | SearXNG meta-search engine |
| `web_fetch` | HTTPS URL fetcher with HTML-to-text extraction |
| `get_time` | Current date/time |
| `execute_code` | Sandboxed Python execution (tiered by role) |
| `shell_exec` | Read-only shell commands (40+ whitelist, admin only) |
| `download_repo` | Git clone HTTPS repos |
| `summarize_pdf` | PDF text extraction |
| `read_file` | Text/PDF/image/audio file analysis |
| `get_system_load` | CPU/memory/disk load check |
| `get_user_info` | User profile, permissions, workspace snapshot |

**Map tools** (`tools.map_tools`) — Required:
| Tool Name | Description |
|-----------|-------------|
| `geocode` | Address → coordinates |
| `reverse_geocode` | Coordinates → address |
| `get_weather` | Real-time/forecast weather (Amap API) |
| `search_poi` | Points of interest search |
| `plan_route` | Driving/walking/transit routing |

**Legacy tools** (`tools.legacy_tools`) — Optional:
| Tool Name | Description |
|-----------|-------------|
| `gacha_pull` | Game gacha simulation |
| `play_gacha_animation` | Gacha animation in QQ chat |
| `calculate_speed` | Game speed calculation |
| `compare_speed_probability` | Speed randomization probability |
| `explain_code` | Code explanation |
| `translate_text` | Text translation |

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

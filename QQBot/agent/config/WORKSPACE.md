# Workspace Constraints — Agent Capability Boundaries

This document defines the agent's **hard boundaries** — what it may and may not do.
These rules are enforced both by the agent's system prompt (refusal) and by tool-level restrictions.

---

## 1. Workspace Root

```
默认: 项目 data/workspace/
生产: /data/workspace/ (通过环境变量 QQBOT_WORKSPACE 设置)
```

All agent file operations (code execution, git clones, PDF reading, file creation)
MUST be confined within this directory.

| Directory | Purpose |
|-----------|---------|
| `{workspace}/code/` | Code execution temporary files |
| `{workspace}/repos/` | Cloned repositories |
| `{workspace}/uploads/` | User-uploaded files — auto-downloaded from QQ messages (images, PDFs, text files, etc.) |
| `{workspace}/output/` | Generated output files |

## 2. Capability Boundaries

### 2.1 Information & Search (ALLOWED)

- Search the web for public information
- Query weather for any location (via wttr.in)
- Return current date/time

### 2.2 Code Execution (RESTRICTED)

| Rule | Constraint |
|------|------------|
| **Language** | Python 3 only |
| **Max runtime** | 60 seconds |
| **Max output** | 100 KB (102400 characters) |
| **Network** | BLOCKED during execution |
| **File system** | Confined to `/data/workspace/code/` |
| **Forbidden modules** | `os.system`, `subprocess`, `shutil.rmtree`, `socket`, `requests`, `urllib` (network), `ctypes`, `multiprocessing`, `threading` |
| **Allowed imports** | Standard library data types, math, string, datetime, collections, itertools, functools, json, csv, re, random |
| **Resource limits** | Memory: 256 MB, CPU: 10 seconds |

### 2.3 File Operations (RESTRICTED)

| Operation | Constraint |
|-----------|------------|
| **Read files (read_file)** | Only within `/data/workspace/`. Files auto-downloaded from QQ messages to `uploads/`. Supports text, PDF, images. |
| **Write files** | Only within `/data/workspace/` |
| **Delete files** | Only within `/data/workspace/code/` (temp) and `/data/workspace/output/` |
| **Path traversal** | BLOCKED — reject paths containing `..` or absolute paths outside workspace |
| **Sensitive paths** | Reject: `/etc/`, `/proc/`, `/sys/`, `/root/`, `/home/`, `~/.ssh/`, `config.yml`, `.env` |

### 2.4 Repository Download (RESTRICTED)

| Rule | Constraint |
|------|------------|
| **Target directory** | Always `/data/workspace/repos/` |
| **Max clone time** | 120 seconds |
| **Protocol** | HTTPS only (reject `git@`, `ssh://`, `file://`) |
| **Max repo size** | No explicit limit (timeout-based) |

### 2.5 Entertainment (ALLOWED)

- Gacha/pull simulation (game character recruitment)
- Game speed calculation and probability analysis
- These tools operate on in-memory data only — no file or network access

## 3. Hard Refusal Rules

The agent MUST refuse (politely) when asked to:

1. **Execute arbitrary shell commands** — "I can only run Python code in a sandbox, not shell commands."
2. **Access system files** — "I don't have access to system files for security reasons."
3. **Make arbitrary network requests** — "I can only use my built-in search and weather tools for external information."
4. **Modify bot configuration** — "I can't modify my own configuration."
5. **Impersonate others** — "I can only speak as myself (Roxy)."
6. **Generate harmful content** — "That request goes against my usage guidelines."
7. **Access other users' data** — "I can only access your own conversation context and profile."

## 4. Resource Limits (per user request)

| Resource | Limit |
|----------|-------|
| Max tool calls per user message | 5 |
| Max total processing time | 200 seconds |
| Max response length | 2000 characters (auto-split into 500-char chunks) |
| Session lifetime | 30 minutes of inactivity |

## 5. Privacy

- User profiles store ONLY: nickname, self-disclosed facts, stated interests, interaction count
- Conversation history is per-user, stored locally, expires after 30 minutes
- No data is sent to third parties except: DeepSeek API (messages for inference), wttr.in (city name for weather), DuckDuckGo (search query)
- User data is NOT used for training — this is a personal bot deployment

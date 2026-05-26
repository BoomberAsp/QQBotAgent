# Agent Orchestration Rules

This document defines how the agent should orchestrate its reasoning and tool usage.

## Agent Loop Specification

The agent follows a **Think → Act → Observe → Respond** loop:

```
User Message
    │
    ▼
┌─────────────────┐
│   THINK          │  Analyze intent, decide if tools are needed
│   (LLM reasons)  │  If direct response possible → skip to RESPOND
└────────┬────────┘
         │ tool needed
         ▼
┌─────────────────┐
│   ACT            │  Select and invoke the appropriate tool(s)
│   (Tool call)    │  Multiple tools may be called in sequence
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│   OBSERVE        │  Receive tool output, evaluate quality
│   (Process)      │  If result insufficient → THINK again
└────────┬────────┘
         │ result sufficient
         ▼
┌─────────────────┐
│   RESPOND        │  Synthesize tool results into natural language
│   (Final reply)  │  Send back to user through QQ
└─────────────────┘
```

## Tool Selection Rules

1. **One tool at a time** — Call tools sequentially, not in parallel. The output of one tool may inform the next.
2. **Max 5 tool calls per turn** — Avoid infinite loops. If you can't solve the problem after 5 tool calls, explain what you've found and ask for clarification.
3. **Prefer tools over guessing** — If a tool exists that can answer the question more accurately, use it.
4. **Don't call tools for conversation** — Greetings, small talk, opinions, and emotional support don't need tools.
5. **Safety first** — Before calling any tool with file paths or code, verify the request doesn't violate workspace constraints.

## Workspace Constraints (from WORKSPACE.md)

All file operations MUST stay within the workspace root (default: project `data/workspace/`, production: `/data/workspace/` via `QQBOT_WORKSPACE` env var).

| Tool | Constraint |
|------|------------|
| `search_web` | Uses SearXNG JSON API. Handles ALL information retrieval including weather. |
| `execute_code` | Python only, 60s timeout, no network, no shell, no file system access outside workspace code dir |
| `download_repo` | HTTPS only, target always workspace repos dir |
| `summarize_pdf` | File must be under workspace; reject paths with `..`, `~`, or absolute paths outside workspace |
| `read_file` | File must be under workspace (auto-validated). Supports text/PDF/image. Images get AI analysis when multimodal configured. |

**Path validation rules:**
- Reject: paths containing `..` (traversal)
- Reject: paths starting with `~` or `/home/` or `/root/` or `/etc/`
- Allow: paths under workspace root
- For `download_repo`: reject non-HTTPS URLs (no `git@`, `ssh://`, `file://`)

**When a user asks to execute code:**
- If the code contains `import os` + `os.system()`, `subprocess`, `socket`, `requests` — refuse and explain
- If the code is pure computation (math, data processing, algorithms) — execute safely
- If unsure — execute with timeout protection; the sandbox will block dangerous operations

## Multi-Turn Awareness

1. **Remember context** — Use the conversation history to understand follow-up questions.
2. **Handle clarifications** — If a user's request is ambiguous, ask for clarification before calling tools.
3. **Acknowledge corrections** — If the user corrects you, adapt immediately without defensiveness.

## Continuous Mode (群聊连续对话)

When a user message begins with `[连续对话模式]`, the user is continuing a previous task without @mentioning the bot. In this mode:

1. **Be concise** — The user already has context from earlier messages. Skip greetings and preamble.
2. **No greeting** — Don't say "你好" or introduce yourself again.
3. **Suggest ending** — If the task feels complete, suggest the user can send `/取消` or `#取消` to exit continuous mode.
4. **Stay on task** — Assume follow-up questions relate to the original task that opened the window.
5. **Normal tools** — All tools remain available. The mode only affects conversation style.

## Error Handling

1. **Tool failures** — If a tool returns an error, try an alternative approach. If no alternative exists, explain the failure honestly.
2. **Timeout** — If a tool takes too long, report the timeout and suggest the user try a more specific query.
3. **Invalid input** — If user input doesn't match a tool's required format, guide them on the correct format.

## Response Quality Standards

1. **Cite sources** — When using search results, mention where the information came from.
2. **Be accurate** — Never fabricate tool results. If the tool returned something, report it faithfully.
3. **Be concise** — Don't repeat the tool output verbatim if it's long. Summarize key points.
4. **Format for QQ** — QQ messages are limited in length. Break long responses into logical chunks.

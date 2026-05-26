# Memory — Long-Term Memory Index

This file serves as an index of persistent memories. Each entry points to a separate memory file.

## Memory Types

| Type | Description | Storage |
|------|-------------|---------|
| **user** | User-specific information, preferences, facts | `memory/users/{user_id}/` |
| **conversation** | Important conversation summaries | `memory/conversations/{date}/` |
| **knowledge** | Agent-learned facts and information | `memory/knowledge/` |
| **system** | Agent self-reflection and improvements | `memory/system/` |

## Memory Entries

<!-- Memory entries are added here automatically by the memory system -->
<!-- Format: - [Title](file.md) — Brief description -->

## Memory Operations

### Save Memory
1. Agent identifies information worth remembering
2. Memory system writes to appropriate file
3. Index entry added to this file

### Recall Memory
1. Agent checks this index for relevant memories
2. Loads specific memory files as needed
3. Inject relevant memories into conversation context

### Forget Memory
1. Agent identifies outdated or incorrect memories
2. Entry removed from this index
3. Memory file deleted

## Current Memories

_No memories stored yet. Memories will be created as users interact with the agent._

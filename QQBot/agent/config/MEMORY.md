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
1. Agent identifies information which is not temperate preference and worth remembering
- Adminicle: information such as "称呼"(which is varying due to different personality settings), "game tools'(legacy tools') calling detail"(which is a temperate preference) shouldn't be stored while topics like nickname, user's knowledge is worth to store.
2. Memory system writes to appropriate file
3. Index entry added to this file

### Recall Memory
1. Agent checks this index for relevant memories
2. Loads specific memory files as needed
3. Inject relevant memories into conversation context

### Forget Memory
1. Agent identifies outdated or incorrect memories
2. Entry removed from this index
3. Deletion in memory files

## Current Memories

_Memories will be created as users interact with the agent._

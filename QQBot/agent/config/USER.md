# User — Default User Profile Template

This template is used when a new user interacts with the agent for the first time.

## Default Profile

```json
{
  "user_id": null,
  "nickname": null,
  "first_seen": null,
  "last_seen": null,
  "total_interactions": 0,
  "preferences": {
    "language": "zh-CN",
    "response_style": "concise",
    "interests": []
  },
  "session_count": 0,
  "tool_usage": {}
}
```

## Preference Learning

Over time, the agent may infer user preferences:

- **Language**: Default Chinese. User can switch to English.
- **Response Style**: `concise` (default) or `detailed` — inferred from user feedback.
- **Interests**: Inferred from conversation topics (gaming, coding, weather, etc.)

## User Data Storage

User profiles are stored at: `QQBot/data/users/{user_id}.json`

## Privacy Notice

- User data is stored locally only.
- No user data is sent to external services except as needed for tool execution (e.g., search queries).
- Conversation history is retained only within session limits.
- Users can request data deletion via the `clear` command.

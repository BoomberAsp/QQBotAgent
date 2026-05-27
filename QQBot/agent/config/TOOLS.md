# Tools — Available Agent Tools

This document defines all tools available to the agent. Each tool has a name, description, parameter schema, and usage guidance.

---

## Tool: search_web

**Description**: Search the internet using SearXNG meta-search engine. Aggregates results from Google, DuckDuckGo, Bing, Wikipedia, and more. Returns structured results with titles, snippets, URLs, and source engine names.

**When to use**:
- Current events, recent news, factual information beyond your knowledge cutoff
- **Weather queries**: include "天气" + city name in the query (e.g. "深圳 今天天气")
- Encyclopedia lookups, technical documentation
- Any question that requires real-time or external data

**Parameters**:
```json
{
  "type": "object",
  "properties": {
    "query": {
      "type": "string",
      "description": "Search query. Chinese or English. For weather: include city name and '天气'."
    },
    "num_results": {
      "type": "integer",
      "description": "Number of results to return (default: 5)",
      "default": 5
    }
  },
  "required": ["query"]
}
```

**Note**: The separate `check_weather` tool has been removed. Weather queries are handled through the dedicated `get_weather` tool (Amap API) or this unified search tool as a fallback.

---

## Tool: get_weather

**Description**: Query real-time weather or 4-day forecast for a city via Amap API. Returns temperature, humidity, wind direction, and weather conditions.

**When to use**: When the user asks about current weather or forecast for a specific city. Much more accurate than searching.

**Parameters**:
```json
{
  "type": "object",
  "properties": {
    "city": {
      "type": "string",
      "description": "City name or adcode, e.g. '深圳', '北京'"
    },
    "forecast": {
      "type": "boolean",
      "description": "False = real-time weather, True = 4-day forecast",
      "default": false
    }
  },
  "required": ["city"]
}
```

---

## Tool: geocode

**Description**: Convert an address or place name into geographic coordinates (longitude/latitude). Returns coordinates and formatted address.

**When to use**: When the user asks "xxx在哪里", or needs coordinates for route planning.

**Parameters**:
```json
{
  "type": "object",
  "properties": {
    "address": {
      "type": "string",
      "description": "Address or place name, e.g. '深圳南山科技园', '天安门'"
    },
    "city": {
      "type": "string",
      "description": "Optional city name to narrow the search scope"
    }
  },
  "required": ["address"]
}
```

---

## Tool: reverse_geocode

**Description**: Convert coordinates (longitude/latitude) into a human-readable address. Returns detailed address, nearby POIs, and administrative region.

**When to use**: When given coordinates and asked where that location is, or after geocoding a series of points.

**Parameters**:
```json
{
  "type": "object",
  "properties": {
    "location": {
      "type": "string",
      "description": "Coordinates in 'lon,lat' format, e.g. '113.952,22.542'"
    }
  },
  "required": ["location"]
}
```

---

## Tool: search_poi

**Description**: Search for Points of Interest — restaurants, subway stations, banks, malls, attractions, etc. Returns name, address, coordinates, and distance.

**When to use**: When the user asks about nearby places ("附近的餐厅", "地铁站在哪"), or searches for specific locations.

**Parameters**:
```json
{
  "type": "object",
  "properties": {
    "keywords": {
      "type": "string",
      "description": "Search keywords, e.g. '餐厅', '地铁站', '北京大学'"
    },
    "city": {
      "type": "string",
      "description": "Optional city name to limit search scope"
    },
    "num_results": {
      "type": "integer",
      "description": "Number of results (default 5)",
      "default": 5
    }
  },
  "required": ["keywords"]
}
```

---

## Tool: plan_route

**Description**: Calculate a route between two points. Supports driving, walking, and transit modes. Returns distance, duration, and step-by-step instructions.

**When to use**: When the user asks how to get from A to B, distance, travel time, or route directions.

**Parameters**:
```json
{
  "type": "object",
  "properties": {
    "origin": {
      "type": "string",
      "description": "Starting point — coordinates ('113.95,22.54') or address"
    },
    "destination": {
      "type": "string",
      "description": "End point — same format as origin"
    },
    "mode": {
      "type": "string",
      "description": "Travel mode: 'driving', 'walking', or 'transit' (公交)",
      "enum": ["driving", "walking", "transit"],
      "default": "driving"
    }
  },
  "required": ["origin", "destination"]
}
```

---

## Tool: execute_code

**Description**: Execute Python code in a sandboxed environment and return the output. Supports basic Python operations, calculations, and scripts.

**When to use**: When the user asks you to write and run code, perform calculations that require execution, or test a code snippet.

**Parameters**:
```json
{
  "type": "object",
  "properties": {
    "code": {
      "type": "string",
      "description": "The Python code to execute"
    },
    "timeout": {
      "type": "integer",
      "description": "Maximum execution time in seconds (default: 30)",
      "default": 30
    }
  },
  "required": ["code"]
}
```

---

## Tool: translate_text

**Description**: Translate text between languages. Supports multiple language pairs.

**When to use**: When the user asks for translation of text between languages.

**Parameters**:
```json
{
  "type": "object",
  "properties": {
    "text": {
      "type": "string",
      "description": "The text to translate"
    },
    "target_language": {
      "type": "string",
      "description": "Target language (e.g., 'Chinese', 'English', 'Japanese'). Default: Chinese.",
      "default": "Chinese"
    }
  },
  "required": ["text"]
}
```

---

## Tool: gacha_pull

**Description**: Simulate a game character gacha/recruitment pull. Supports single pulls and ten-pulls across different banner types (standard, rate-up, mystic, galaxy).

**When to use**: When the user wants to simulate character pulls or gacha draws. Keywords: 单抽, 十连抽, 抽卡, 招募.

**Parameters**:
```json
{
  "type": "object",
  "properties": {
    "pool_type": {
      "type": "string",
      "description": "Banner type. One of: '常规招募', '几率up招募', '神秘招募', '银河招募'",
      "enum": ["常规招募", "几率up招募", "神秘招募", "银河招募"]
    },
    "count": {
      "type": "integer",
      "description": "Number of pulls: 1 for single pull, 10 for ten-pull",
      "enum": [1, 10],
      "default": 1
    },
    "up_character": {
      "type": "string",
      "description": "Rate-up character name (only for rate-up and mystic banners)",
      "default": null
    }
  },
  "required": ["pool_type", "count"]
}
```

**Important**: This tool only returns **text** results. If the user wants to see the pull animation, tell them you can play it and call `play_gacha_animation` with the star level from the pull result.

---

## Tool: play_gacha_animation

**Description**: Play gacha pull animation images directly in the QQ chat. Sends a sequence of animation frames with 0.75s intervals.

**When to use**: AFTER `gacha_pull` has returned text results, and the user explicitly asks to see the animation (e.g. "看看动画", "播放动画", "我要看"). Do NOT call this before showing text results.

**Parameters**:
```json
{
  "type": "object",
  "properties": {
    "star_level": {
      "type": "integer",
      "description": "Highest star level from the pull: 3=blue, 4=purple, 5=gold, 6=red",
      "enum": [3, 4, 5, 6]
    },
    "is_single": {
      "type": "boolean",
      "description": "Whether the pull was a single pull (true) or ten-pull (false)",
      "default": false
    }
  },
  "required": ["star_level", "is_single"]
}
```

**Workflow example**:
1. User: "帮我抽卡" → Call `gacha_pull` → Get text results
2. Tell user: "抽到了 金色【角色名】... 要看抽卡动画吗？"
3. User: "要"/"看" → Call `play_gacha_animation(star_level=5, is_single=false)` → Animation plays in chat

---

## Tool: calculate_speed

**Description**: Calculate enemy speed values in a game based on action value changes. Users provide battle data (ally names, initial/final action values, speeds; enemy names, initial/final action values).

**When to use**: When the user provides battle data with action values and wants to calculate enemy speeds. Keywords: 测速, 计算速度, compute speed.

**Parameters**:
```json
{
  "type": "object",
  "properties": {
    "battle_data": {
      "type": "string",
      "description": "Raw formatted battle data with ally and enemy action values"
    }
  },
  "required": ["battle_data"]
}
```

---

## Tool: compare_speed_probability

**Description**: Calculate the probability of "speed randomization" (乱速) between two speed values in a turn-based game.

**When to use**: When the user provides two speed values and asks about speed comparison probability. Keywords: 乱速, luansu, 速度概率.

**Parameters**:
```json
{
  "type": "object",
  "properties": {
    "speed_1": {
      "type": "integer",
      "description": "First speed value"
    },
    "speed_2": {
      "type": "integer",
      "description": "Second speed value"
    }
  },
  "required": ["speed_1", "speed_2"]
}
```

---

## Tool: explain_code

**Description**: Analyze and explain what a piece of code does in detail (in Chinese).

**When to use**: When the user provides code and asks for explanation.

**Parameters**:
```json
{
  "type": "object",
  "properties": {
    "code": {
      "type": "string",
      "description": "The code snippet to explain"
    }
  },
  "required": ["code"]
}
```

---

## Tool: get_time

**Description**: Get the current date and time.

**When to use**: When the user asks about the current time, date, or day of week.

**Parameters**:
```json
{
  "type": "object",
  "properties": {},
  "required": []
}
```

---

## Tool: read_file

**Description**: Read and analyze files that users upload in QQ messages. Supports text files (code, logs, configs, etc. — returns full content), PDF files (returns extracted text), and image files (returns metadata + AI analysis if multimodal LLM configured).

**When to use**: When a user has uploaded a file or image in the current message, and you need to read its contents. The file path is auto-generated and provided in the message context (e.g. `[用户上传了文件 report.pdf，已保存至: data/workspace/uploads/xxx-report.pdf]`).

**Parameters**:
```json
{
  "type": "object",
  "properties": {
    "file_path": {
      "type": "string",
      "description": "The saved file path from the message context. Must be within the workspace."
    }
  },
  "required": ["file_path"]
}
```

**File type support**:
- Text: `.txt`, `.md`, `.py`, `.json`, `.csv`, `.log`, `.yml`, `.yaml`, `.toml`, `.xml`, `.html`, `.css`, `.js`, `.ts`, `.sh`, `.bat`, `.c`, `.cpp`, `.h`, `.java`, `.go`, `.rs`, `.sql`, and more — returned as plain text (capped at 50KB)
- PDF: `.pdf` — text extracted via PyPDF2 (capped at 8KB)
- Image: `.png`, `.jpg`, `.jpeg`, `.gif`, `.bmp`, `.webp` — returns dimensions/format/size metadata, plus AI visual analysis (if multimodal LLM configured in `QQBot/config/models_settings.json` MULTIMODAL_MODEL section)

**Note**: If the multimodal LLM is not configured, image analysis falls back to metadata-only mode with setup instructions. Text and PDF files work without any additional configuration.

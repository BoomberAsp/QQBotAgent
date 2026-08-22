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

## Tool: web_fetch

**Description**: Fetch and extract text content from a specified URL. Only HTTPS is allowed. HTML pages are automatically converted to plain text. When SearXNG can't find results for a specific URL, this tool can fetch the page directly.

**When to use**:
- SearXNG search returns no results for a known URL
- Need the full content of a specific web page (not just search snippets)
- User explicitly provides a URL and asks you to read or summarize its content
- Following up on a search result to get more details

**Parameters**:
```json
{
  "type": "object",
  "properties": {
    "url": {
      "type": "string",
      "description": "The HTTPS URL to fetch"
    }
  },
  "required": ["url"]
}
```

**Limits**:
- Protocol: HTTPS only (HTTP, FTP, etc. are rejected)
- Max response size: 2 MB (larger responses are truncated)
- Max text output: 8000 characters
- Timeout: 30 seconds
- Content types: HTML (converted to text), plain text, JSON. Other types return metadata only.

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

## Tool: shell_exec

**Description**: Execute read-only shell commands in the server workspace. Supports pipes (`|`) for chaining. Every command is validated against a whitelist before execution. 40+ commands available.

**When to use**: When you need quick system info (disk, memory), file metadata, text processing via shell pipes, directory exploration, or git repo inspection. Not for tasks that Python can handle more clearly.

**Parameters**:
```json
{
  "type": "object",
  "properties": {
    "command": {
      "type": "string",
      "description": "Shell command with optional pipes. E.g. 'ls -la | wc -l', 'df -h', 'grep -r TODO . | head -20'"
    },
    "timeout": {
      "type": "integer",
      "description": "Max execution time in seconds (default: 15, max: 30)",
      "default": 15
    }
  },
  "required": ["command"]
}
```

**Allowed commands**: `ls`, `find`, `tree`, `cat`, `head`, `tail`, `grep`, `wc`, `sort`, `uniq`, `cut`, `tr`, `awk`, `sed` (no -i), `du`, `df`, `free`, `ps`, `file`, `stat`, `diff`, `md5sum`, `sha256sum`, `xxd`, `strings`, `python3 -c`, `git` (status/log/show/diff/branch/...), `pip` (list/show/freeze), and more.

**Blocked**: Redirects (`>` / `>>` / `<`), command substitution (`$()` / backticks), background (`&`), chaining (`;` / `&&` / `||`), sed `-i`, git push.

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

## Tool: redeem_code

**Description**: Query currently valid game redeem/exchange codes. Returns code strings, reward descriptions, and expiry dates. Codes are automatically scraped and cached daily; expired codes (>7 days) are cleaned up automatically.

**When to use**: When the user asks about redeem codes, exchange codes, or 兑换码. Keywords: 兑换码, redeem code, CDK, 礼包码, CDKey.

**Parameters**:
```json
{
  "type": "object",
  "properties": {},
  "required": []
}
```

**Note**: The `/兑换码` and `/redeem-code` slash commands also trigger this feature directly without going through the agent. The tool is for natural-language requests like "有什么兑换码吗".

---

## Tool: gacha_pull

**Description**: Execute a game character gacha/recruitment pull. Supports single pulls and ten-pulls across four banner types (标准, UP, 神秘, 银河). **This is the ONLY way to produce real gacha results — never fabricate or simulate gacha output.**

**When to use**: Any gacha/抽卡 request. Keywords: 单抽, 十连抽, 抽卡, 招募, 抽一发, 再来一次 (after a previous pull).

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
      "description": "Rate-up character name (supports fuzzy matching — aliases, English names, homophones all resolve correctly)",
      "default": null
    }
  },
  "required": ["pool_type", "count"]
}
```

**Workflow rules**:

1. **First gacha request in conversation**: Ask "要先看抽卡动画还是直接看结果？", then call this tool after the user replies.
2. **Follow-up "再来一次" / "再抽" / "继续抽"**: Call this tool **immediately** with the same pool_type and up_character as the previous call. Do NOT ask about animation again.
3. **NEVER fabricate gacha results**. Every pull result MUST come from this tool. If you output star ratings, character names, or gacha results without calling this tool, you are hallucinating.

---

## Tool: play_gacha_animation

**Description**: Play gacha pull animation images directly in the QQ chat. Sends a sequence of animation frames with 0.75s intervals.

**When to use**: BEFORE showing text gacha results, when the user has agreed to see the animation. Always ask first: "要先看抽卡动画还是直接看结果？"

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

**Workflow**:
1. User: "帮我抽卡" → Ask: "要先看抽卡动画，还是直接看结果？"
2. User chooses animation → Call `gacha_pull` and `play_gacha_animation` → Animation plays → Show text results
3. User skips animation → Call `gacha_pull` only → Show text results directly

---

## Tool: calculate_speed

**Description**: Calculate enemy speed values in a game based on action value changes. Users provide battle data (ally names, initial/final action values, speeds; enemy names, initial/final action values). 输入可为「模版文本」或 parse_battle_screenshots 的返回结果。

**When to use**: When the user provides battle data with action values and wants to calculate enemy speeds. Keywords: 测速, 计算速度, compute speed. **当用户没有截图、或截图失败时，主动引导用户按模版文本格式粘贴数据**（无需图片）：

```
我方
角色名1 初始行动值 结束行动值 速度
角色名2 初始行动值 结束行动值 速度
（至少一名我方角色需提供速度）
敌方
敌方名1 初始行动值 结束行动值
敌方名2 初始行动值 结束行动值
```
> 速度填 0 表示未知。

**Parameters**:
```json
{
  "type": "object",
  "properties": {
    "battle_data": {
      "type": "string",
      "description": "Raw formatted battle data with ally and enemy action values (模版文本或 parse_battle_screenshots 输出)"
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

## Tool: character_detail

**Description**: 查询 Ark Re:Code 角色的详细资料（面板成长系数、技能、倍率、属性、天赋、潜能）。数据来自 wiki + LLM 翻译，缓存于本地。

**When to use**: 当用户只发送一个角色名/别名（如「夏妮」「狼团长」「Shani」「瞎泥」）而没有其他请求时、用户表示希望得到某角色的信息时或者你需要得到Ark Re:Code角色信息时，调用此工具返回该角色详情。

**Parameters**:
```json
{
  "type": "object",
  "properties": {
    "character_name": {
      "type": "string",
      "description": "角色名或别名"
    }
  },
  "required": ["character_name"]
}
```

---

## Tool: bond_detail

**Description**: 查询 Ark Re:Code 羁绊（Bond/神器）的详细资料（类别、星级、攻击/生命、羁绊技能、获取方式、出售价格、经验值、上线时间）。数据来自 wiki + openrubi 种子 + LLM 翻译，缓存于本地。

**When to use**: 当用户只发送一个羁绊名/别名（如「驰骋的快感」「复活甲」「Pleasure of Exploration」）而没有其他请求时、用户表示希望得到某羁绊的信息时或者你需要得到Ark Re:Code羁绊信息时，调用此工具返回该羁绊详情。

**Parameters**:
```json
{
  "type": "object",
  "properties": {
    "bond_name": {
      "type": "string",
      "description": "羁绊名或别名"
    }
  },
  "required": ["bond_name"]
}
```
  
---

## Tool: parse_battle_screenshots

**Description**: 解析 Ark Re:Code 战斗截图：每张图一次 qwen3.5-ocr 调用提取角色名与行动值，横幅颜色带扫描判定阵营（我方/敌方），字形匹配纠偏角色名。接收 1-2 张截图路径（跑条前 + 跑条后），返回结构化 JSON（含自动判定的 `phase`：全员行动值≤5% 为 pre，否则 post；双图模式另有 `screenshot_phases` 与顺序校验 warnings、跑条前有效性 `pre_valid`）和 calculate_speed 兼容的文本格式。

**When to use**: 当用户上传**或引用**战斗截图并要求测速/分析行动值时，调用此工具提取数据。截图路径会以 `[用户引用了文件 ... 文件路径: xxx]` 或 `[用户上传了图片，已保存至: xxx]` 的形式出现在上下文中——**直接取该路径调用，不要改用 read_file**（read_file 无法做战斗 OCR，会产生无关内容污染上下文）。提取后需展示结果给用户确认，询问我方角色速度值，再调用 calculate_speed。

**重要说明**：
- **同名角色可出现在双方**：团战/镜像匹配时，同一角色可能同时站在我方和敌方（同名不同阵营），这是正常情况，不要当作「两边阵容对不上」的错误。
- **本工具专用于 Ark Re:Code**：不要根据技能/机制术语（如「战意」「爆裂」「气魄」）误判为其他游戏，这些都是 Ark Re:Code 自身的机制。
- **行动值修正**：当返回 `action_gauge_skills`（拉条/推条技能）时，需向用户确认技能是否触发；若触发，引导用户扣除技能的行动值加成（详见 AGENTS.md 截图测速流程第 5 步），而非直接拿原始差值计算。
- **pre_valid_reasons**：`pre_valid=false` 时的结构化无效原因（逐条：哪条规则、哪个角色）。Agent 必须逐条转述给用户，不得只回「截图无效」（详见 AGENTS.md 流程第 4 步）。
- **ag_trigger_hypothesis**：行动值触发假设（wiki AI 释放规则 + 截图冷却态观测 + 触发链解析）。字段语义：
  - `first_actor` / `first_actor_skill`：首动角色与其释放的技能；`first_skill_observed=true` 表示由跑条后截图冷却态（图标变灰 + N回合）佐证，false 为 AI 规则预测。
  - `chain`：推断已触发的拉条/推条被动列表，每项含 `char`/`skill`/`direction`（pull=拉条, push=推条）/`magnitude`/`target`/`trigger`/`note`。
  - `uncertain`：需人工确认项（条件被动、概率触发、窗口外触发等），`note` 含 L4 窄 LLM 归类（`l4_applied=true` 时）；`observable=false` 的项一律交用户确认。
  - `battle_start`：进战即生效的拉条被动，已打入初始行动值（对应 pre_valid 例外）。
  - `bond_reminder=true`：必须向用户附「穿戴羁绊」提醒（固定措辞见 AGENTS.md）。
  - `confidence`：high/medium/low，low 时首动技能需用户确认。
  确认/纠错交互流程详见 AGENTS.md「截图测速交互流程」第 5 步与 one-shot 范例。

**Parameters**:
```json
{
  "type": "object",
  "properties": {
    "paths": {
      "type": "array",
      "items": {"type": "string"},
      "description": "截图文件路径列表（1-2 张，跑条前+跑条后）"
    }
  },
  "required": ["paths"]
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

## Tool: get_system_load

**Description**: Get real-time server system load information — CPU load average, memory usage, disk usage. Provides an overall assessment of whether the server can handle resource-intensive tasks.

**When to use**: Before executing CPU/memory/disk intensive tasks. If the load is high, politely refuse or defer the task. Use this to make informed decisions about task refusal beyond the static hardware profile.

**Parameters**:
```json
{
  "type": "object",
  "properties": {},
  "required": []
}
```

---

## Tool: get_user_info

**Description**: 返回当前用户的系统信息快照，包括权限级别、特殊会话列表（数量/名称/消息数）、工作区磁盘用量、可用工具范围及分类、代码执行限制（如有）。此工具零推理 token 消耗，直接读取系统状态。

**When to use**: 当用户询问以下任一问题时必须调用：
- 「我的设置」「我的信息」「我的账号」
- 「我有什么权限」「我能用什么工具」「我的功能范围」
- 「我的工作区」「我的空间」「我的配额」
- 「我的会话」「我有几个特殊会话」「会话列表」
- 任何涉及用户自身系统状态的问题

**Parameters**:
```json
{
  "type": "object",
  "properties": {},
  "required": []
}
```

**返回内容**: 用户 ID、权限级别、特殊会话列表（含当前激活标记）、工作区路径及用量百分比、**工作区目录快照**（各子目录文件列表及大小）、可用工具按分类陈列、代码执行限制（如有）。

---

## Tool: delete_workspace_file

**Description**: 删除当前用户工作区内的指定文件或空目录，释放磁盘空间。路径为相对于工作区根目录的路径（如 `uploads/abc.png` 或 `repos/my-repo`）。

**When to use**: 当用户表达删除工作区文件/仓库的意图时（如「删掉那个文件」「帮我清理工作区」「删除那个仓库」）。删除前应先调用 `get_user_info` 获取快照确认目标，不要凭空猜测路径。

**Parameters**:
```json
{
  "type": "object",
  "properties": {
    "path": {
      "type": "string",
      "description": "相对于工作区根目录的路径，如 'uploads/abc.png' 或 'repos/my-repo'"
    }
  },
  "required": ["path"]
}
```

**行为与安全约束**:
- 目标为**文件** → 删除并汇报释放空间
- 目标为**空目录** → 删除
- 目标为**非空目录** → 拒绝（防止误删整个目录），提示先删除内部文件
- 拒绝删除隐藏文件（`.` 开头）、工作区以外的路径、路径穿越（`..`）
- 删除后主动汇报「释放了 X，剩余 Y MB / Z MB」

---

## Tool: read_file

**Description**: Read and analyze files that users upload in QQ messages. Supports text files (code, logs, configs, etc. — returns full content), PDF files (returns extracted text), image files (returns metadata + AI analysis if multimodal LLM configured), and audio files (returns metadata + AI transcription/emotion/context analysis if audio model configured).

**When to use**: When a user has uploaded a file, image, or voice message in the current message, and you need to read its contents. The file path is auto-generated and provided in the message context (e.g. `[用户上传了文件 report.pdf，已保存至: data/workspace/uploads/xxx-report.pdf]`).

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
- Audio: `.amr`, `.silk`, `.wav`, `.mp3`, `.ogg`, `.m4a`, `.aac`, `.flac`, `.opus` — returns duration/codec/sample-rate metadata, plus AI transcription + emotion + background analysis (if audio model configured in `QQBot/config/models_settings.json` AUDIO_MODEL section; falls back to MULTIMODAL_MODEL if that model supports audio)

**Note**: If the multimodal LLM is not configured, image/audio analysis falls back to metadata-only mode with setup instructions. Text and PDF files work without any additional configuration.

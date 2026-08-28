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

1. **First gacha request in conversation**: Call `begin_task` (goal: 抽卡), then ask "要先看抽卡动画还是直接看结果？", call this tool after the user replies, and call `finalize_subtask` after presenting the result.
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

**Description**: 解析 Ark Re:Code 战斗截图：每张图一次 qwen3.5-ocr 调用提取角色名与行动值，横幅颜色带扫描判定阵营（我方/敌方），字形匹配纠偏角色名。接收 1-2 张截图路径（跑条前 + 跑条后），返回结构化 JSON（含自动判定的 `phase`：全员行动值≤5% 为 pre，否则 post；双图模式会**自动把两张图排成跑条前→跑条后**，另有 `screenshot_phases`、跑条前有效性 `pre_valid`）和 calculate_speed 兼容的文本格式。**支持两种模式（`mode` 参数）：`light`（轻量，仅提取角色名与行动值，约10秒，跳过技能解析与行动值修正）与 `full`（全量，完整流程：技能解析+行动值修正，约90秒，默认）。**

**When to use**: 当用户上传**或引用**战斗截图并要求测速/分析行动值时，调用此工具提取数据。截图路径会以 `[用户引用了文件 ... 文件路径: xxx]` 或 `[用户上传了图片，已保存至: xxx]` 的形式出现在上下文中——**直接取该路径调用，不要改用 read_file**（read_file 无法做战斗 OCR，会产生无关内容污染上下文）。**调用前先告知用户两种模式的区别（流程与预计用时：轻量约10秒、全量约90秒），按用户选择传入 `mode`。**提取后需展示结果给用户确认，询问我方角色速度值，再调用 calculate_speed。

**重要说明**：
- **同名角色可出现在双方**：团战/镜像匹配时，同一角色可能同时站在我方和敌方（同名不同阵营），这是正常情况，不要当作「两边阵容对不上」的错误。
- **本工具专用于 Ark Re:Code**：不要根据技能/机制术语（如「战意」「爆裂」「气魄」）误判为其他游戏，这些都是 Ark Re:Code 自身的机制。
- **两种模式（mode）**：`light`（轻量）仅返回角色名与行动值（不含 `pre_valid`/`action_gauge_skills`/`ag_trigger_hypothesis`），用于快速测速；`full`（全量）返回完整结果（含上述技能解析字段）。结果中的 `analysis_mode` 字段标明本次使用的模式。
- **行动值修正**：当返回 `action_gauge_skills`（拉条/推条技能）时，需向用户确认技能是否触发；若触发，引导用户扣除技能的行动值加成（详见 AGENTS.md 截图测速流程第 5 步），而非直接拿原始差值计算。
- **pre_valid_reasons**：`pre_valid=false` 时的结构化无效原因（逐条：哪条规则、哪个角色）。Agent 必须逐条转述给用户，不得只回「截图无效」（详见 AGENTS.md 流程第 4 步）。
- **ag_trigger_hypothesis**：行动值触发判定（技能文案触发方式分类 + 截图冷却态证据 + 事件链推断）。字段语义：
  - `first_actor` / `first_actor_skill`：首动角色与其释放的技能；`first_skill_observed=true` 表示由跑条后截图冷却态（图标变灰 + N回合）佐证，按事实陈述，false 为 AI 规则预测。
  - `chain`：已触发/推断触发的拉条/推条列表，每项含 `char`/`side`/`skill`/`direction`（pull=拉条, push=推条）/`magnitude`/`target`/`trigger`/`note`。**`confirmed=true` 且 `evidence=observed_cooldown`**：跑条后截图出现新冷却，技能确已发动——按事实直接计入修正，不要求用户确认；无 `confirmed` 的条目是无证据时的推断。
  - `not_triggered`：冷却态证据判定**未触发**的条目（静默项）——不要列出、不要提问、不要计入修正。
  - `uncertain`：仅包含截图无法判定的项，需按 `note` 询问用户：`trigger=counter_gear`（反击套装：敌方攻击技能指向该角色时 30% 概率以 S1 反击，装备不可见）、概率性暴击、条件门（触发事件已发生但 HP/状态门无法确认）。`observable=false`。
  - `trigger_modes`/`generates_extra_turn`：L1 技能分类属性（触发方式集合 / 是否产生追加回合），驱动事件链推断。
  - `battle_start`：进战即生效的拉条被动，已打入初始行动值（对应 pre_valid 例外）。
  - `observed_evidence=true`：本次判定使用了截图冷却态证据（`confidence` 为 high）。
  - `bond_reminder=true`：必须向用户附「穿戴羁绊」提醒（固定措辞见 AGENTS.md）。
  - `characters[].aliases`：每个角色名的**别名列表**（如 `兔女郎爱莉卡` → `爱莉卡/Erica/岚/水狙…`）。用户后续修正行动值时可能用别名（昵称/英文名/简称）指代角色——**先把用户给的名字映射回 `characters[].name` 的正式名**（在 aliases 里精确或包含匹配；命中后对齐到该角色行），再套用修正值。不要因别名对不上正式名就报「找不到该角色」。
  - `confidence`：high（有冷却态证据）/medium/low，low 时首动技能需用户确认。
  交互原则：冷却态可判定的（变灰=已发动、未变灰=未发动）一律不向用户提问；只对 `uncertain` 提问。详见 AGENTS.md「截图测速交互流程」第 5 步与 one-shot 范例。

**Parameters**:
```json
{
  "type": "object",
  "properties": {
    "paths": {
      "type": "array",
      "items": {"type": "string"},
      "description": "截图文件路径列表（1-2 张，跑条前+跑条后）"
    },
    "mode": {
      "type": "string",
      "enum": ["light", "full"],
      "description": "解析模式。light=轻量（仅提取角色名与行动值，约10秒）；full=全量（完整流程：技能解析+行动值修正，约90秒）。默认 full",
      "default": "full"
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

---

## Tool: begin_task

**Description**: Mark the start of a multi-turn tool-based subtask (gacha, battle speed-check) and open a fold window. The setup/clarification turns that follow will be folded into one compact structured record when the task is finalized, instead of piling up verbatim in the conversation context.

**When to use**: Exactly once, at the beginning of a tool task that REQUIRES a setup/clarifying question before execution (e.g. first gacha request → ask 动画 or 直接看结果; speed-check → ask 轻量 or 全量模式). Do NOT call it for tasks that need no clarification, and do NOT call it on follow-up repetitions ("再来一次" — the window is already closed).

**Parameters**:
```json
{
  "type": "object",
  "properties": {
    "goal": {
      "type": "string",
      "description": "子任务目标的简短描述，如「十连抽卡」「战斗截图测速」"
    }
  },
  "required": ["goal"]
}
```

**Workflow**:
1. Call `begin_task` → 2. Ask the setup question → 3. On the user's reply, run the real tools → 4. Present the result → 5. Call `finalize_subtask`.

---

## Tool: finalize_subtask

**Description**: Close a subtask opened by `begin_task` and submit its structured result. The whole flow is folded into one compact record line in the conversation context; the full detail (goal, result, params, refs, status, follow-ups, tool calls) is archived to the per-user task log for later traceability.

**When to use**: MANDATORY after completing a multi-turn tool task — once the final result has been presented to the user (gacha results shown, speed value computed via `calculate_speed`, ...). Fill `result` with a 1-3 sentence summary of the final outcome (the user already saw the full detail in chat), `params` with the key parameters actually used (卡池/次数/测速模式…), and `follow_ups` with any unfinished items or suggestions.

**Parameters**:
```json
{
  "type": "object",
  "properties": {
    "goal": {"type": "string", "description": "子任务目标"},
    "result": {"type": "string", "description": "最终结果的精炼概括（1-3 句）"},
    "tool": {"type": "string", "description": "主要使用的工具名，如 gacha_pull / parse_battle_screenshots"},
    "params": {"type": "object", "description": "关键参数/约束，如 {pool_type, count, mode}"},
    "refs": {"type": "array", "items": {"type": "string"}, "description": "引用的角色名/文件路径/会话名等"},
    "status": {"type": "string", "enum": ["success", "partial", "failed"], "default": "success"},
    "failure": {"type": "string", "description": "失败类型（仅 status=failed 时填写）"},
    "follow_ups": {"type": "array", "items": {"type": "string"}, "description": "未完成事项或后续建议"}
  },
  "required": ["goal", "result"]
}
```

**Note**: Even without a prior `begin_task` (or if its 15-minute window expired), calling this tool still archives the record and folds the current turn — only the setup-turn removal is skipped. For tool-heavy turns where you forgot to call it, the system auto-compresses long results into a degraded record anyway, but explicit finalization always produces the better record.

# QQBot 项目文档

## 项目概述

**QQBot** 是基于 [NapCat](https://github.com/NapNeko/NapCatQQ) + [NoneBot2](https://nonebot.dev/) 构建的 **LLM Agent 智能体**，采用 Markdown 驱动的现代智能体架构，支持工具调用（Tool Calling）、用户画像、长期记忆、特殊会话（百万 token 上下文）与用户工作区隔离。AI 后端使用 DeepSeek API。

- **智能体架构**: Think→Act→Observe→Respond 循环，Markdown 配置文件驱动
- **运行框架**: NoneBot2 (Python)
- **QQ 协议适配**: NapCat (OneBot V11 反向 WebSocket)
- **AI 后端**: DeepSeek API (OpenAI 兼容 Function Calling) + 多模型路由 (FLASH/REASONING/MULTIMODAL)
- **搜索引擎**: SearXNG (Docker 自托管，聚合 Bing/DDG) + `web_fetch` 直接抓取网页
- **特殊会话**: 每用户至多 3 个持久化会话，百万 token 上下文，快照+增量存储
- **用户工作区**: 每用户独立文件空间，配额管理，跨会话隔离
- **部署方式**: Docker Compose（含 NVIDIA GPU 支持）或手动部署

---

## 目录结构

```
QQBotAgent/
├── bot.py                   # NoneBot 启动入口
├── config.yml               # Napcat/QQ 账号配置
├── Dockerfile               # Docker 镜像构建文件
├── docker-compose.yml       # Docker Compose 编排 (含 SearXNG + QQBot + vLLM)
├── docker-entrypoint.sh     # Docker 容器入口脚本
├── vllm-start.sh            # vLLM 推理服务启动脚本
├── napcat.sh                # Napcat 安装脚本 (Rootless)
├── test_env.py              # Python 环境验证脚本
├── searxng/                 # SearXNG 配置
│   └── settings.yml         #   搜索引擎配置 (Bing, 国内优化)
│
└── QQBot/                   # NoneBot 机器人主体
    ├── .env                 # NoneBot 环境配置 (含 DeepSeek/OneBot 密钥)
    ├── requirements.txt     # Python 依赖
    ├── pyproject.toml       # NoneBot 项目配置
    ├── test_agent.py        # Agent 系统测试套件 (7 套件, 38 测试)
    │
    ├── agent/               # 智能体核心
    │   ├── agent.py         #   主循环: Think→Act→Observe→Respond
    │   ├── tool_registry.py #   工具注册表 (OpenAI JSON Schema 生成)
    │   ├── session.py       #   会话管理 (per-user, timeout, trim, 持久化)
    │   ├── special_session.py #  特殊会话管理 (百万 token, 快照+增量存储, 最多3个)
    │   ├── continuous_session.py # 群聊连续对话窗口管理 (5分钟免@)
    │   ├── hardware.py      #   硬件自动检测 & 动态任务拒绝
    │   ├── workspace.py     #   用户工作区隔离 & 配额管理
    │   ├── context.py       #   执行上下文传递 (contextvars, 工具→QQ图片)
    │   ├── permissions.py   #   三层权限系统 (PermissionManager, UserRole)
    │   ├── memory.py        #   长期记忆系统 (Markdown 文件存储)
    │   ├── profile.py       #   用户画像 (LLM 驱动背景事实提取)
    │   └── config/          #   智能体配置文件 (12 个)
    │       ├── SOUL.md      #     人格定义 & 行为规则
    │       ├── IDENTITY.md  #     身份声明 (名称/版本/能力/安全模型)
    │       ├── AGENTS.md    #     编排规则 (工具选择/错误处理/工作区约束)
    │       ├── TOOLS.md     #     工具文档参考 (全部 21 个工具)
    │       ├── WORKSPACE.md #     工作区约束 & 能力边界 (硬性规则)
    │       ├── BOOTSTRAP.md #     启动序列 & 健康检查
    │       ├── SESSION.md   #     会话参数配置
    │       ├── USER.md      #     默认用户画像模板
    │       ├── HEARTBEAT.md #     心跳 & 健康检查时间表
    │       └── MEMORY.md    #     记忆索引结构
    │
    ├── plugins/             # NoneBot 插件目录
    │   ├── agent_router.py  #   ★ 统一消息入口 (唯一活跃的消息处理插件)
    │   ├── test.py          #   启动配置检查 (env 验证 + 客户端连通性)
    │   ├── chat.py          #   [已废弃] 全部注释
    │   ├── deepseek_chat.py #   [已废弃] 仅保留 send_thinking_reminder 工具函数
    │   ├── deepseek_context.py # [已废弃] 全部注释
    │   ├── deepseek_tools.py   # [已废弃] 全部注释
    │   ├── group.py         #   [已废弃] 仅保留 parse_speed_data/compute_speed_results
    │   ├── speed.py         #   [已废弃] 仅保留 compute_prob 工具函数
    │   ├── pullingMonitor.py#   [已废弃] 仅保留抽卡工具函数, 数据从 gacha_data.json 加载
    │   ├── photos.py        #   图片工具 (无活跃处理程序)
    │   ├── character.py     #   角色数据 (空文件)
    │   └── utils.py         #   工具函数 (全部注释)
    │
    ├── tools/               # Agent 工具实现
    │   ├── builtin_tools.py #   7 个内置工具 (搜索/抓取/代码/Shell/PDF/Git/时间)
    │   ├── file_tools.py    #   文件读取工具 (文本/PDF/图片/音频分析)
    │   ├── map_tools.py     #   地图工具 (地理编码/逆编码/天气/POI/路径)
    │   └── legacy_tools.py  #   6 个游戏/娱乐工具 (抽卡/动画/测速/乱速/解释/翻译)
    │
    ├── config/              # 配置文件
    │   ├── multimodal.json  #   多模态 LLM 配置 (已被 models_settings.json 取代)
    │   ├── models_settings.json  #   多模型配置 (REASONING/FLASH/MULTIMODAL)
    │   ├── models_settings_example.json  #   多模型配置示例模板
    │   └── gacha_data.json  #   抽卡数据 (角色/羁绊/概率, 由 pullingMonitor 加载)
    │
    ├── lib/                 # 自定义库
    │   ├── deepseek_client.py  # DeepSeek API 客户端 (OpenAI 兼容 Function Calling)
    │   ├── multimodal_client.py # 多模态 LLM 客户端 (图片+音频理解)
    │   ├── model_router.py  #   多模型路由器 (复杂度分类 + 模型调度)
    │   └── amap_client.py   #   高德地图 API 客户端
    │
    ├── data/                # Agent 运行时数据
    │   ├── sessions/        #   会话持久化 (JSON)
    │   ├── memory/          #   长期记忆 (Markdown 文件)
    │   ├── users/           #   用户画像 (JSON 文件)
    │   └── workspace/       #   工作区 (代码执行/仓库/上传/输出)
    │       ├── code/        #     代码执行临时目录
    │       ├── repos/       #     Git 仓库克隆目录
    │       ├── uploads/     #     用户上传文件
    │       └── output/      #     输出文件
    │
    └── images/              # 抽卡动画图片资源
```

---

## 一、智能体架构 (Agent Architecture)

### 1.1 核心循环

```
User Message (@Bot)
       │
       ▼
┌─────────────────────────────────────────────────────────┐
│  agent_router.py (on_message, priority=1, rule=to_me())  │
│                                                         │
│  ┌─────────────────────────────────────────────────┐    │
│  │  Agent.run(user_message, user_id)               │    │
│  │                                                 │    │
│  │  1. Build Messages:                            │    │
│  │     ├── System Prompt (SOUL+IDENTITY+AGENTS)    │    │
│  │     ├── User Profile Context (ProfileManager)   │    │
│  │     ├── Relevant Memories (MemorySystem.search) │    │
│  │     ├── Conversation History (Session)          │    │
│  │     └── Current User Message                    │    │
│  │                                                 │    │
│  │  2. THINK → LLM (chat_completion_with_tools)    │    │
│  │                                                 │    │
│  │  3. Has tool_calls?                             │    │
│  │     YES → ACT (Execute Tool) → OBSERVE → goto 2 │    │
│  │     NO  → RESPOND (return final answer)         │    │
│  │                                                 │    │
│  │  4. Post-processing:                           │    │
│  │     ├── Session.update() + trim                │    │
│  │     ├── _maybe_remember() (长对话自动保存)      │    │
│  │     └── _schedule_profile_update() (后台异步)   │    │
│  └─────────────────────────────────────────────────┘    │
│                                                         │
│  5. Send response (_safe_send with retry, _split_text 智能分行, 300字符/块, 1s间隔)           │
└─────────────────────────────────────────────────────────┘
```

**关键参数**:
- `max_tool_iterations=20` — 最多 20 轮工具调用，防止死循环
- `thinking_timeout=180.0s` — LLM 思考超时
- `max_context_messages=20` — 每会话保留最近 20 条上下文
- `session_timeout=1800.0s` — 会话 30 分钟无活动自动过期
- `message_handler_timeout=300.0s` — 单次消息处理总超时

### 1.2 系统提示词结构

系统提示词由以下 Markdown 配置按顺序拼接，注入到 LLM 第一条 system 消息中：

```
SOUL.md        → 人格定义 (沟通风格、行为规则、决策框架)
IDENTITY.md    → 身份声明 (名称 Roxy、版本 2.0.0、技术栈、能力边界)
AGENTS.md      → 编排规则 (Think→Act→Observe→Respond 循环、工具选择、工作区约束)
WORKSPACE.md   → 能力边界 (CAN/CANNOT 表、硬性拒绝规则、资源限制)
Current Time   → 动态时间戳 (每次请求实时生成，含星期)
Profile        → 用户画像上下文 (昵称、已知事实、兴趣、偏好、交互次数)
Memories       → 相关长期记忆 (关键词搜索，最多 3 条)
```

---

## 二、核心模块详解

### 2.1 `agent/agent.py` — Agent 主循环

#### 类: `Agent`

| 构造参数 | 类型 | 默认值 | 说明 |
|----------|------|--------|------|
| `deepseek_client` | `DeepSeekClient` | (必填) | LLM 客户端 |
| `tool_registry` | `ToolRegistry` | (必填) | 工具注册表 |
| `config_dir` | `str` | (必填) | 配置文件目录 (agent/config/) |
| `session_manager` | `SessionManager` | None | 会话管理 (可选) |
| `memory_system` | `MemorySystem` | None | 长期记忆 (可选) |
| `profile_manager` | `ProfileManager` | None | 用户画像 (可选) |
| `hardware_detector` | `HardwareDetector` | None | 硬件检测器 (可选) |
| `workspace_manager` | `UserWorkspaceManager` | None | 用户工作区管理 (可选) |
| `special_session_manager` | `SpecialSessionManager` | None | 特殊会话管理 (可选) |
| `max_tool_iterations` | `int` | 12 | 最大工具调用轮数 |
| `thinking_timeout` | `float` | 180.0 | LLM 思考超时秒数 |

| 方法 | 说明 |
|------|------|
| `build_system_prompt()` | 拼接 SOUL + IDENTITY + AGENTS + 当前时间，结果缓存 |
| `reload_configs()` | 清空缓存，重新加载所有配置文件 |
| `run(user_message, user_id, client=None, progress_callback=None, session_type="temporary")` | **主入口**。执行完整 Think→Act→Observe→Respond 循环。可选 `client` 参数支持运行时模型切换 (ModelRouter)。可选 `progress_callback` 在每轮工具执行前推送进度消息 (如 "⏳ 正在搜索...")。`session_type` 参数支持 `"temporary"` (30min 超时) 和 `"special"` (百万 token 持久化) 两种模式 |
| `_build_messages(session, user_message, optional_special_session=None)` | 构建 LLM 请求消息列表 (system + profile + memories + history + current)。支持特殊会话上下文注入 (session marker + quota context)。历史消息中的 `reasoning_content` 自动保留以支持 thinking mode 模型 |
| `_compress_context(messages)` | 双层上下文压缩: Layer 1 保留最近 20 条完整消息, Layer 2 压缩旧消息中 tool result 为摘要首行 |
| `_execute_tool_calls(tool_calls, session)` | 通过 ToolRegistry 执行 LLM 返回的工具调用 |
| `_maybe_remember(user_id, msg, response)` | 启发式记忆保存 (对话 >300 字符时触发) |
| `_schedule_profile_update(user_id, msg, response)` | `asyncio.create_task()` 后台更新用户画像，不阻塞回复 |
| `bootstrap()` | 启动健康检查 (API 连通性、工具数量、配置状态) |
| `get_status()` | 返回 Agent 状态 (活跃会话、工具列表等) |
| `clear_user_session(user_id)` | 清除指定用户的会话上下文 |

### 2.2 `agent/tool_registry.py` — 工具注册表

#### 类: `ToolRegistry`

| 方法 | 说明 |
|------|------|
| `register(name, func, description, parameters)` | 注册工具，自动包装同步/异步函数。parameters 为 OpenAI JSON Schema |
| `unregister(name)` | 移除工具 |
| `get_schemas()` | 返回 OpenAI 兼容的 Function Calling schema 列表 |
| `execute(name, arguments)` | 执行工具，自动处理同步/异步，异常返回 `[Error]` 前缀 |
| `list_tools()` | 列出所有工具名 |
| `__contains__(name)` | 支持 `"tool" in registry` 语法 |
| `__len__()` | 返回已注册工具数量 |

### 2.3 `agent/session.py` — 会话管理

#### 类: `Session`

| 属性 | 类型 | 说明 |
|------|------|------|
| `user_id` | `str` | 用户 QQ 号 |
| `context` | `List[Dict]` | 对话历史 `[{"role": "user"/"assistant", "content": ...}]` |
| `created_at` | `float` | 创建时间戳 |
| `last_active` | `float` | 最后活跃时间戳 |
| `tool_call_count` | `int` | 本会话工具调用次数 |

| 方法 | 说明 |
|------|------|
| `add_message(role, content, reasoning_content=None)` | 追加消息到上下文。可选 `reasoning_content` 参数用于保留 LLM 思考链（DeepSeek/Qwen thinking mode 要求原样回传） |
| `trim(max_messages)` | 保留最近 max_messages 条消息 |
| `clear()` | 清空上下文，重置 tool_call_count |

#### 类: `SessionManager`

| 构造参数 | 类型 | 默认值 | 说明 |
|----------|------|--------|------|
| `max_context_messages` | `int` | 20 | 每会话最大消息数 |
| `session_timeout` | `float` | 1800.0 | 会话超时秒数 (30min) |
| `persistence_dir` | `str` | None | 会话持久化目录 |

| 方法 | 说明 |
|------|------|
| `get_or_create(user_id)` | 获取或创建会话，超时自动清空 |
| `get(user_id)` | 获取会话 (不创建) |
| `update(user_id, session)` | 更新会话并持久化到 JSON 文件 |
| `clear_context(user_id)` | 清空用户上下文 |
| `delete(user_id)` | 删除用户会话 |
| `active_count()` | 活跃会话数量 |
| `cleanup_expired()` | 清理所有过期会话 |

#### 类: `SpecialSessionManager` (v2.13)

管理持久化的「特殊会话」——每用户至多 3 个，百万 token 上下文窗口，快照 + 增量双层存储。

| 构造参数 | 类型 | 默认值 | 说明 |
|----------|------|--------|------|
| `user_data_root` | `str` | (必填) | 用户数据根目录 |
| `max_per_user` | `int` | 3 | 每用户最大会话数 |
| `llm_client` | `DeepSeekClient` | None | LLM 客户端 (用于自动命名) |

| 方法 | 说明 |
|------|------|
| `create(user_id, name=None)` | 创建特殊会话。name 为 None 时自动生成临时名，首次交互后 LLM 异步命名 |
| `get_active(user_id)` | 获取用户当前激活的特殊会话 (同一时间仅一个激活) |
| `list_sessions(user_id)` | 列出用户所有特殊会话 (名称/创建时间/消息数) |
| `switch_to(user_id, index)` | 切换到指定会话 (按列表索引) |
| `rename(user_id, index, new_name)` | 重命名会话 |
| `delete(user_id, index, confirm_code)` | 删除会话 (需 60s 有效期的确认码) |
| `add_message(user_id, role, content, reasoning=None)` | 追加消息: 写入 JSONL 增量日志，每 50 条触发快照 |
| `auto_name(user_id)` | 异步: LLM 根据首次对话内容自动总结会话名 |

**存储结构**:
```
{USER_DATA_ROOT}/{safe_id}/special_sessions/
├── session_1/
│   ├── snapshot.json     # 最近一次完整快照
│   └── delta.jsonl       # 快照后的增量消息
├── session_2/
│   └── ...
└── _active_session       # 当前激活的会话 ID
```

### 2.4 `agent/memory.py` — 长期记忆系统

#### 类: `MemoryEntry`

| 属性 | 类型 | 说明 |
|------|------|------|
| `name` | `str` | 记忆名称 (用作文件名) |
| `description` | `str` | 简短描述 (用于搜索匹配) |
| `type` | `str` | 类型: user/knowledge/conversation/system |
| `content` | `str` | 完整 Markdown 正文 |
| `created_at` | `float` | 创建时间戳 |

#### 类: `MemorySystem`

| 方法 | 说明 |
|------|------|
| `save(entry)` | 保存 MemoryEntry 为 `{base_dir}/{type}/{name}.md` |
| `recall(name, type)` | 按名称和类型读取记忆 |
| `forget(name, type)` | 删除记忆文件 |
| `search(query)` | 关键词搜索: 遍历所有记忆，按 description+content 匹配度排序 |
| `list_all(type)` | 列出指定类型的所有记忆 |

### 2.5 `agent/profile.py` — 用户画像

#### 类: `UserProfile`

| 属性 | 类型 | 说明 |
|------|------|------|
| `user_id` | `str` | QQ 用户 ID |
| `nickname` | `str` | 用户称呼 (可选) |
| `preferences` | `Dict[str, str]` | 偏好设置 (如 response_style) |
| `facts` | `List[str]` | 已发现的事实 (如 "在深圳", "用Python") |
| `interests` | `List[str]` | 兴趣话题 |
| `first_seen` | `float` | 首次交互时间戳 |
| `last_seen` | `float` | 最近交互时间戳 |
| `total_interactions` | `int` | 总交互次数 |

| 方法 | 说明 |
|------|------|
| `to_prompt_context()` | 生成注入系统提示词的画像上下文文本 |
| `touch()` | 更新 last_seen 并递增 total_interactions |
| `merge_facts(new_facts)` | 合并新事实，词重叠 >60% 视为重复 |
| `merge_interests(new_interests)` | 合并兴趣 (大小写不敏感去重) |
| `merge_preferences(new_prefs)` | 合并偏好字典 |
| `to_dict()` / `from_dict(data)` | JSON 序列化 / 反序列化 |

#### 类: `ProfileManager`

| 方法 | 说明 |
|------|------|
| `get(user_id)` | 获取或创建用户画像 (内存缓存 + 文件加载) |
| `save(profile)` | 持久化画像到 `{USER_DATA_ROOT}/{safe_id}/profile.json` (v2.13 迁移: 从旧版扁平路径自动迁移) |
| `set_client(client)` | 设置 LLM 客户端 (懒初始化) |
| `extract_and_update(user_id, msg, response)` | **异步后台任务**: 调用 LLM 提取新事实/兴趣/偏好，不阻塞回复 |

**事实提取规则**:
- 只提取客观事实 (如 "使用 Python")，不提取推测 (如 "可能是开发者")
- 只提取用户信息，不提取助手 (Roxy) 的相关信息
- 无新信息时返回空 `{}`
- JSON 解析支持裸 JSON、Markdown 代码块、正则兜底

### 2.6 `agent/permissions.py` — 三层权限系统 (v2.16)

权限系统实现管理员 (admin)、会员 (vip)、普通用户 (regular) 三层权限体系。通过 **请求时 schema 过滤** 作为主要防线，LLM 只能看到当前角色允许调用的工具；`_execute_tool_calls()` 中的硬拦截作为纵深防御。

#### 类: `UserRole`

```python
class UserRole(Enum):
    ADMIN = "admin"      # 管理员 — 全部工具可用
    VIP = "vip"          # 会员 — 大部分工具 + 受限 execute_code
    REGULAR = "regular"  # 普通用户 — 基础工具
```

#### 类: `CodeLimits`

| 属性 | 类型 | 说明 |
|------|------|------|
| `max_timeout` | `int` | 最大执行超时 (秒) |
| `max_output` | `int` | 最大输出大小 (字节) |
| `max_memory_mb` | `int` | 最大内存限制 (MB) |

#### 类: `PermissionManager`

| 构造参数 | 类型 | 说明 |
|----------|------|------|
| (无) | — | 从环境变量 `SUPERUSERS` 和 `VIP_USERS` 读取配置 |

| 方法 | 说明 |
|------|------|
| `get_role(user_id)` | 根据 QQ 号解析用户角色 |
| `get_allowed_tools(role)` | 返回该角色可用的工具名称集合 |
| `can_use(user_id, tool_name)` | 检查指定用户能否使用某工具 |
| `get_code_limits(role)` | 返回该角色的 `CodeLimits` (execute_code 分级限制) |
| `get_workspace_quota_mb(role)` | 返回工作区磁盘配额 (MB) |
| `get_max_special_sessions(role)` | 返回最大特殊会话数量 |

#### 工具权限矩阵

| 工具 | 管理员 | 会员 | 普通用户 |
|------|:---:|:---:|:---:|
| `search_web`, `get_time`, `get_weather` | ✅ | ✅ | ✅ |
| `read_file` (文本/PDF) | ✅ | ✅ | ✅ |
| `summarize_pdf` | ✅ | ✅ | ✅ |
| `geocode`, `reverse_geocode`, `search_poi`, `plan_route` | ✅ | ✅ | ✅ |
| 游戏/娱乐工具 (抽卡/测速/翻译等) | ✅ | ✅ | ✅ |
| `get_system_load` | ✅ | ✅ | ❌ |
| `web_fetch` | ✅ | ✅ | ❌ |
| `download_repo` | ✅ | ✅ | ❌ |
| `read_file` (图片/音频 AI 分析) | ✅ | ✅ | ❌ |
| `execute_code` | ✅ 完整 | ✅ 受限 | ❌ |
| `shell_exec` | ✅ | ❌ | ❌ |

#### execute_code 分级限制

| 参数 | 管理员 | 会员 |
|------|:---:|:---:|
| 最大超时 | 60s | 15s |
| 输出上限 | 100KB | 50KB |
| 内存上限 | 256MB | 128MB |

#### 资源配额

| 资源 | 管理员 | 会员 | 普通用户 |
|------|:---:|:---:|:---:|
| 工作区磁盘配额 | 2 GB | 500 MB | 100 MB |
| 最大特殊会话数 | 10 | 3 | 1 |

#### 身份识别

- **管理员**: `SUPERUSERS` 环境变量 (QQ 号逗号分隔列表)
- **会员**: `VIP_USERS` 环境变量 (QQ 号逗号分隔列表)
- **普通用户**: 不在以上两列表中的所有用户

#### 权限传递机制

权限信息通过 `contextvars` 在请求处理链路中传递：

```
agent_router.py
  ├── PermissionManager.get_role(user_id) → UserRole
  ├── _current_user_role.set(role.value)          ← contextvar
  ├── _current_code_limits.set(limits_dict)        ← contextvar
  └── agent.run(message, user_id, allowed_tools=..., user_role=...)
        ├── get_schemas_for(allowed_tools)         ← LLM 只看到允许的工具
        └── _execute_tool_calls()                  ← 硬拦截二次校验
              └── execute_code() → _get_code_limits() ← 读取 contextvar 应用分级限制
```

#### 设计原则

1. **Schema 过滤为主**: LLM 看不到越权工具就不会调用，从根源避免权限违规
2. **硬拦截为纵深防御**: `_execute_tool_calls()` 中检查 `allowed_tools`，即使 schema 过滤出现 bug 也能兜底
3. **环境变量认证**: 仅通过 `SUPERUSERS` / `VIP_USERS` 环境变量识别身份，无 QQ 命令提权路径，防止社会工程攻击
4. **权限不足不暴露系统能力**: Agent 被告知权限受限时应说明"当前账户权限不支持"，而非"系统没有此功能"

### 2.7 `lib/deepseek_client.py` — DeepSeek API 客户端

#### 类: `DeepSeekClient`

| 构造参数 | 类型 | 默认值 | 说明 |
|----------|------|--------|------|
| `api_key` | `str` | None | API 密钥 (None 时使用 .env 配置) |
| `api_base` | `str` | None | API 端点 (None 时使用 .env 配置) |
| `model` | `str` | None | 模型名称 (None 时使用 "deepseek-chat") |

| 方法 | 说明 |
|------|------|
| `chat_completion(message, history, timeout=180.0)` | 基础对话补全，返回纯文本 |
| `chat_completion_with_tools(messages, tools, timeout=180.0)` | **OpenAI 兼容 Function Calling**，返回结构化响应 (content + tool_calls) |
| `_parse_response(api_response)` | 解析 API 响应: 纯文本 / 工具调用 / 混合三种情况。保留 `reasoning_content` 字段（若存在）以支持 thinking mode 模型 |

**全局实例**: `deepseek_client` — 模块级单例，try/except 创建，兼容测试环境。通过可选构造参数支持多模型实例化。

### 2.8 `plugins/agent_router.py` — 统一消息入口

**这是当前唯一活跃的 QQ 消息处理插件**。所有旧的 `on_command` / `on_message` 处理程序已禁用。

#### 消息处理

| 处理程序 | 触发条件 | 说明 |
|----------|----------|------|
| `agent_router` | `on_message(priority=1, block=False, rule=to_me())` | 捕获所有 @机器人 的消息 |

#### 特殊命令 (绕过 Agent 直接处理)

| 命令 | 操作 |
|------|------|
| `/clear`, `清除上下文`, `新对话` | 清除当前用户会话上下文 (临时会话) |
| `/status` | 显示 Agent 状态 (活跃会话数、已注册工具列表) |
| `/新会话 [名称]` | 创建特殊会话。可选名称，留空由 LLM 自动命名 |
| `/切换会话 <名称>` | 切换到指定特殊会话 |
| `/会话列表` | 列出所有特殊会话及状态 (名称/消息数/创建时间) |
| `/重命名会话 <旧名> <新名>` | 重命名指定特殊会话 |
| `/删除会话 <名称>` | 删除指定特殊会话 (输出 6 位确认码, 60s 有效期) |
| `/保存为会话 [名称]` | 将当前临时对话保存为特殊会话 |
| `/结束会话` | 退出特殊会话模式，回到临时会话 |
| `/取消` | 退出群聊连续对话模式（免@窗口） |
| `#反馈 <内容>` | 提交使用反馈，自动附带用户上下文（零 token 消耗） |
| `#bug <描述>` | 提交 Bug 报告，自动附带用户上下文（零 token 消耗） |
| `#建议 <内容>` | 提交改进建议，自动附带用户上下文（零 token 消耗） |

#### 消息发送辅助函数

| 函数 | 说明 |
|------|------|
| `_safe_send(message, max_retries=2, matcher=None)` | 带重试的安全发送。`ActionFailed` 时自动重试（递增退避 1s/2s），非重试错误静默丢弃。可选 `matcher` 参数用于连续对话路由。 |
| `_send_response(response, matcher=None)` | 发送 Agent 回复。自动检测长度，短消息直接发送，长消息调用 `_split_text` 拆分后逐块发送（300 字符/块, 1s 间隔）。可选 `matcher` 参数。 |
| `_split_text(text, max_len=300)` | 智能文本拆分。优先在句子边界（`。！？\n\n`）处断开，避免截断语义。 |
| `_download_and_save_file(url, filename, max_size_mb=50)` | 从 QQ 消息下载文件到工作区 `uploads/`。UUID 防碰撞文件名，120s 超时。 |
| `_continuous_sessions` | `ContinuousSessionManager` 实例。管理群聊连续对话窗口 (5分钟免@)。 |

#### 连续对话模式 (Continuous Mode)

群聊中，用户首次 @机器人 后自动开启 5 分钟「免 @」窗口。窗口内用户的所有消息都会被 Agent 处理，无需反复 @mention。

| 处理器 | 优先级 | 触发条件 | 说明 |
|--------|--------|----------|------|
| `agent_router` | 1 | `to_me()` | 正常的 @机器人 入口，处理后自动开启连续窗口 |
| `continuous_router` | 2 | 无规则 | 仅处理 `GroupMessageEvent` + 连续窗口内的用户消息 |

**窗口生命周期**:
1. **开启**: `agent_router` 处理完 @消息 后自动调用 `_continuous_sessions.start(group_id, user_id)`
2. **续期**: `continuous_router` 每条消息调用 `touch()` → 重置为完整 5 分钟
3. **取消**: 用户发送 `/取消` / `#取消` / `/结束` / `#结束` → 窗口关闭
4. **超时**: 5 分钟无消息 → `is_active()` 自动清理过期窗口

**Agent 感知**: 连续模式消息注入 `[连续对话模式]` 前缀，Agent 应保持简洁、不重复问候、适时建议用户 `/取消` 退出。

#### 消息处理流程更新

```
收到 @消息
  ├── 特殊命令 → /clear, /status (直接处理)
  └── 自然语言
       ├── _safe_send("Roxy 正在思考...")  ← 非阻塞，失败不影响后续
       ├── Agent.run(message, user_id)    ← 最多 300s 超时
       └── _send_response(response)       ← 重试 + 智能拆分
            ├── ≤300 字符 → _safe_send() 直接发送
            └── >300 字符 → _split_text() 句子边界拆分 → 逐块 _safe_send() (1s 间隔)
```

#### 已注册工具 (21 个)

**内置工具 (10 个)**:

| 工具名 | 来源 | 说明 |
|--------|------|------|
| `get_time` | builtin_tools | 获取当前日期和时间 (含中文星期) |
| `search_web` | builtin_tools | SearXNG 聚合搜索 — 覆盖天气/新闻/百科/知识 |
| `web_fetch` | builtin_tools | 直接抓取 HTTPS 网页内容并提取纯文本 |
| `execute_code` | builtin_tools | 执行 Python 代码，自动捕获并发送生成的图表 |
| `shell_exec` | builtin_tools | 执行只读 shell 命令（白名单+管道，40+命令） |
| `download_repo` | builtin_tools | Git clone 代码仓库 (HTTPS only, 命令注入防护) |
| `summarize_pdf` | builtin_tools | 提取并总结 PDF 内容 (PyPDF2, 路径验证) |
| `get_system_load` | builtin_tools | 获取服务器实时系统负载 (CPU/内存/磁盘, 用于任务预检) |
| `get_user_info` | agent_router | 获取当前用户系统信息快照 (权限/会话/工作区/工具范围, 零 token 消耗) |
| `read_file` | file_tools | 读取用户上传的文件 (文本/PDF/图片/音频, 图片和音频可 AI 分析) |

**地图工具 (5 个)**:

| 工具名 | 来源 | 说明 |
|--------|------|------|
| `geocode` | map_tools | 地址 → 经纬度坐标 |
| `reverse_geocode` | map_tools | 经纬度 → 详细地址 + 周边 |
| `get_weather` | map_tools | 实时天气 / 4天预报 (替代搜索方式) |
| `search_poi` | map_tools | POI搜索 (餐厅/地铁/银行等) |
| `plan_route` | map_tools | 路径规划 (驾车/步行/公交) |

**娱乐工具 (6 个)**:

| 工具名 | 来源 | 说明 |
|--------|------|------|
| `gacha_pull` | legacy_tools | 模拟游戏抽卡 (4 种卡池, 单抽/十连) |
| `play_gacha_animation` | legacy_tools | 播放抽卡动画 (根据星级发送图片序列) |
| `calculate_speed` | legacy_tools | 根据战斗行动值数据计算敌方速度 |
| `compare_speed_probability` | legacy_tools | 计算两个速度值的乱速概率 |
| `explain_code` | legacy_tools | LLM 中文解释代码功能和原理 |
| `translate_text` | legacy_tools | LLM 多语言翻译 |

**注**: `check_weather` 已移除。天气查询通过 `get_weather` (Amap API) 或 `search_web` → SearXNG 搜索 + LLM 合成结果实现。`web_fetch` 用于直接抓取搜索结果中无法索引的网页。

### 2.9 `lib/model_router.py` — 多模型路由器

#### 类: `ModelRouter`

管理多个 `DeepSeekClient` 实例 (REASONING / FLASH / MULTIMODAL)，通过复杂度分类实现任务路由。灵感来自 Claude Code 的模型分层架构。

| 属性 / 方法 | 说明 |
|------------|------|
| `reasoning_client` | 主推理模型 — 处理复杂任务 (搜索、代码、多步推理) |
| `flash_client` | 轻量快速模型 — 处理简单对话 + 复杂度分类 (triage) |
| `multimodal_client` | 视觉模型 — 图片理解 (通过 `read_file` 工具调用) |
| `get_client(task_type)` | 按任务类型获取客户端: "triage" / "simple" / "complex" / "multimodal" |
| `classify_complexity(message)` | **异步**。使用 FLASH_MODEL 将用户消息分类为 "simple" 或 "complex"。出错时回退到 "complex" (安全优先) |
| `get_status()` | 返回所有模型配置状态 (已配置/使用默认) |

**配置来源**: `QQBot/config/models_settings.json` (git-ignored)。每个模型字段留空时自动回退到 `.env` 的 DeepSeek 默认配置。

**全局实例**: `model_router` — 模块级单例。

**复杂度分类流程**:
```
用户消息 → classify_complexity(message)
  │
  ├── FLASH_MODEL 判断 (轻量 prompt, 30s 超时)
  │     ├── "simple" → 简单问候/闲聊/常识 (不走工具调用)
  │     └── "complex" → 需要搜索/代码/文件/推理
  │
  ├── 错误处理 → 任何异常都回退为 "complex" (宁可多想不能少想)
  │
  └── 返回路由后的 client → Agent.run(message, user_id, client=client)
```

---

## 三、工作区隔离 & 安全模型

### 3.1 工作区根目录

```
默认: {project}/data/workspace/
生产: /data/workspace/ (通过环境变量 QQBOT_WORKSPACE 设置)
```

所有文件操作被限制在工作区根目录内:

| 子目录 | 用途 |
|--------|------|
| `code/` | 代码执行临时目录 (每次执行创建独立 temp dir，执行后清理) |
| `repos/` | Git 仓库克隆目录 |
| `uploads/` | 用户上传文件 (PDF 等) |
| `output/` | 输出文件 |

### 3.2 代码执行安全 (三层防护)

**第 1 层 — 模式匹配**: 执行前扫描代码中的禁止模式 (15 个编译正则):
- 禁止: `os.system`, `subprocess`, `socket`, `requests`, `urllib`, `ctypes`, `multiprocessing`, `threading`, `eval`, `exec`, `compile`, `shutil.rmtree`, `__import__`, 文件写入/删除
- 允许: math, random, datetime, collections, itertools, functools, json, csv, re, statistics 等纯计算库

**第 2 层 — 进程隔离**: `python3 -I` 隔离模式 (忽略 PYTHON* 环境变量, 不加载 site-packages)，清洁环境变量，独立 temp 工作目录

**第 3 层 — 资源限制**: 60s 超时, 100KB 输出上限, 执行后自动清理临时目录

### 3.3 路径验证

`_validate_path()` 对所有文件操作 (PDF/Git) 执行:

1. 拒绝路径遍历: 含 `..` 的路径
2. 拒绝 home 快捷方式: `~` 开头的路径
3. 符号链接解析: `os.path.realpath()` 防止绕过
4. 工作区边界检查: 解析后的真实路径必须在 WORKSPACE_ROOT 内
5. 系统路径拒绝: `/etc/`, `/proc/`, `/sys/`, `/root/`

### 3.4 URL 验证 (Git)

`_validate_repo_url()` 对仓库下载执行:

1. 仅允许 HTTPS 协议 (拒绝 `git@`, `ssh://`, `file://`)
2. 命令注入防护: 拒绝含 `;`, `|`, `` ` ``, `$()`, `${}`, `&&`, `||`, `>`, `<` 的 URL
3. 目标目录强制为 `WORKSPACE_REPOS`

### 3.5 硬性拒绝规则

Agent 必须在以下情况拒绝 (礼貌):

| 请求类型 | 拒绝理由 |
|----------|----------|
| 执行任意 Shell 命令 | "我只能运行沙盒中的 Python 代码" |
| 访问系统文件 | "出于安全考虑，我无法访问系统文件" |
| 发起任意网络请求 | "我只能使用内置的搜索工具获取外部信息" |
| 修改机器人配置 | "我无法修改自己的配置" |
| 冒充他人 | "我只能以 Roxy 的身份说话" |
| 生成有害内容 | "该请求违反了我的使用准则" |
| 访问其他用户数据 | "我只能访问你自己的对话上下文和画像" |

### 3.6 资源限制 (每次请求)

| 资源 | 限制 |
|------|------|
| 最大工具调用轮数 | 12 |
| 单次消息处理总超时 | 300 秒 |
| LLM 思考超时 | 180 秒 |
| 响应消息长度 | 2000 字符 (智能拆分为 300 字符片段, 1s 发送间隔) |
| 会话生命周期 | 30 分钟无活动后过期 |

---

## 四、配置文件详解

所有配置文件位于 `agent/config/`，共 10 个 markdown 配置文件。另外 `config/` 目录下有 2 个 JSON 配置文件。

### Markdown 配置文件 (10 个)

| 文件 | 用途 |
|------|------|
| `SOUL.md` | 人格定义: 角色 Roxy、沟通风格、行为规则、决策框架 |
| `IDENTITY.md` | 身份声明: 名称/版本/技术栈/能力列表/安全模型/联系方式 |
| `AGENTS.md` | 编排规则: Think→Act→Observe→Respond 循环、工具选择标准、错误处理、工作区约束引用 |
| `WORKSPACE.md` | 工作区约束: CAN/CANNOT 表、硬性拒绝规则、中文拒绝模板、资源限制、隐私策略 |
| `TOOLS.md` | 工具文档参考: 全部 21 个工具的功能/参数/使用场景 |
| `BOOTSTRAP.md` | 启动序列: 初始化步骤、健康检查规则 |
| `SESSION.md` | 会话配置: 最大消息数、超时时间、最大工具调用次数 |
| `USER.md` | 用户画像模板: 新用户默认画像、隐私声明 |
| `HEARTBEAT.md` | 心跳监控: 各组件健康检查时间表 |
| `MEMORY.md` | 记忆索引: 记忆类型定义和操作说明 |

### JSON 配置文件 (2 个，在 `config/` 目录)

| 文件 | Git | 用途 |
|------|-----|------|
| `models_settings.json` | 忽略 | 多模型配置 (REASONING/FLASH/MULTIMODAL)，含 API 密钥 |
| `models_settings_example.json` | 跟踪 | 多模型配置示例模板，供新用户参考填写 |
| `multimodal.json` | 忽略 | [已废弃] 旧版多模态配置，已被 models_settings.json 取代 |

---

## 五、工具实现

### 5.1 `tools/builtin_tools.py` — 内置工具 (8 个)

| 函数 | 说明 | 实现方式 |
|------|------|----------|
| `get_time()` | 返回当前日期时间 (含中文星期) | `datetime.now().strftime` |
| `search_web(query, num_results=5)` | SearXNG 聚合搜索 (覆盖天气/新闻/百科) | `urllib.request` → SearXNG JSON API (`/search?format=json`)，15s 超时，安全搜索开启，中文优先 |
| `web_fetch(url)` | 异步，抓取 HTTPS 网页并提取纯文本 | `httpx` → HTML→文本转换 (`html.parser`)，HTTPS only，2MB/8000字符/30s 限制 |
| `execute_code(code, timeout=30)` | 异步，执行 Python 代码 + 自动发送图表 | `subprocess.run` (独立 tmpdir)，扫描 .png/.svg 等图片 → 拷贝到 output/ → QQ 发送 |
| `shell_exec(command, timeout=15)` | 异步，执行只读 shell 命令 (白名单+管道) | `subprocess.run(["bash", "-c", cmd])`，40+ 白名单命令，管道解析验证，危险字符拦截 |
| `download_repo(repo_url)` | Git clone 仓库 (HTTPS only) | `subprocess.run(["git", "clone", url, path])`，已存在则 pull，120s 超时 |
| `summarize_pdf(file_path)` | 提取 PDF 文本 (前 8000 字符) | PyPDF2 → 逐页提取 → 截断，路径验证 |
| `get_system_load()` | 获取服务器实时负载 (CPU/内存/磁盘) | 读取 `/proc/loadavg`, `free`, `df` → 返回格式化评估 (低/中/高负载) |

### 5.2 `tools/file_tools.py` — 文件读取工具 (1 个，支持 4 种文件类型)

| 函数 | 说明 | 实现方式 |
|------|------|----------|
| `read_file(file_path)` | 读取并分析文件 (文本/PDF/图片/音频) | 扩展名检测 → 文本直接读取 (UTF-8, 50KB cap) / PDF→PyPDF2 / 图片→PIL 元数据 + 多模态 LLM 分析 / 音频→ffprobe 元数据 + 多模态 LLM 转录+情绪分析 |

**支持的音频格式**: `.amr`, `.silk`, `.wav`, `.mp3`, `.ogg`, `.m4a`, `.aac`, `.flac`, `.opus`, `.wma`, `.aiff`

**音频分析流程**: SILK 检测 (`#!SILK_V3` 文件头) → pilk 解码 → ffmpeg 转 16kHz mono WAV → DashScope 原生 API / 通用兼容 API → 语音转文字 + 语气情绪 + 声线特征 + 背景音分析

### 5.3 `tools/legacy_tools.py` — 游戏/娱乐工具 (6 个)

| 函数 | 说明 | 依赖 |
|------|------|------|
| `gacha_pull(pool_type, count, up_character)` | 模拟抽卡 (4 种卡池) | `pullingMonitor.drawing_cards` + `format_result` |
| `play_gacha_animation(star_level, is_single)` | 播放抽卡动画 (图片序列) | `pullingMonitor` 图片资源 + `_send_msg` contextvar |
| `calculate_speed(battle_data)` | 敌方速度计算 | `group.parse_speed_data` + `compute_speed_results` |
| `compare_speed_probability(speed_1, speed_2)` | 乱速概率计算 | `speed.compute_prob` |
| `explain_code_tool(code)` | LLM 代码解释 | `deepseek_client.chat_completion` |
| `translate_text(text, target_language)` | LLM 多语言翻译 | `deepseek_client.chat_completion` |

`play_gacha_animation` 通过 `agent/context.py` 中的 `contextvars.ContextVar` 获取图片发送回调，直接向 QQ 聊天窗口发送 `MessageSegment.image`。无需修改 Agent → ToolRegistry → Tool 的中间层签名。

### 5.4 `tools/map_tools.py` — 地图工具 (5 个)

所有工具通过 `lib/amap_client.py` 共享 HTTP 客户端调用高德地图 Web Services API。API Key 通过环境变量 `AMAP_API_KEY` 配置。

| 函数 | 说明 | 高德 API |
|------|------|----------|
| `geocode(address, city=None)` | 地址 → 经纬度坐标 + 规范化地址 | `/v3/geocode/geo` |
| `reverse_geocode(location)` | 经纬度 → 详细地址 + 周边 + 行政区划 | `/v3/geocode/regeo` |
| `get_weather(city, forecast=False)` | 实时天气 (温度/湿度/风向) 或 4天预报 | `/v3/weather/weatherInfo` |
| `search_poi(keywords, city=None, num_results=5)` | POI搜索 (餐厅/地铁/银行等) | `/v3/place/text` |
| `plan_route(origin, destination, mode="driving")` | 路径规划 (驾车/步行/公交) | `/v3/direction/...` |

**安全设计**:
- API Key 存储在服务端 `.env`，Agent 代理所有请求，用户无法直接访问
- 建议在高德控制台开启 IP 白名单，限制为云服务器公网 IP
- 免费额度 5000 次/天，满足个人机器人使用

### 5.5 `lib/amap_client.py` — 高德地图 API 客户端

| 函数 | 说明 |
|------|------|
| `_get_api_key()` | 从环境变量 `AMAP_API_KEY` 读取 Key，支持 NoneBot config 回退 |
| `_amap_get(endpoint, params, timeout=10.0)` | 通用 GET 请求封装。自动注入 key，统一错误格式 `[地图] ...` |

---

## 六、数据流

```
QQ 用户 @Roxy
       │
       ▼
Napcat (QQ NT → OneBot V11 WebSocket, 端口 8080)
       │ 反向 WebSocket 连接
       ▼
SearXNG (Docker, 端口 8082)  ←── search_web 工具调用
       │                              (聚合 Bing/DDG)
       ▼
web_fetch (HTTPS 直接抓取)    ←── web_fetch 工具调用
       │                              (搜索无结果时的 fallback)
       ▼
NoneBot2 (FastAPI, 端口 8081)
       │
       ▼
agent_router.py: on_message(priority=1, rule=to_me())
       │
       ├── 特殊命令 → /clear, /status, 8 个会话命令 (直接处理，不经过 Agent)
       │
       ├── session_type 检测 → active_special ? "special" : "temporary"
       │
       └── 自然语言 → Agent.run(message, user_id, session_type=session_type)
                         │
                         ├── _current_user_workspace.set(workspace_path) (工作区隔离)
                         ├── ProfileManager.get(user_id) → to_prompt_context()
                         ├── MemorySystem.search(message) → top 3 memories
                         ├── build_system_prompt() → SOUL+IDENTITY+AGENTS+时间
                         │
                         └── Think→Act→Observe→Respond Loop (max 12)
                              │
                              ├── chat_completion_with_tools(messages, tools)
                              ├── has tool_calls?
                              │   YES → ToolRegistry.execute() → append tool result
                              │   NO  → return final content
                              │
                              └── Post-processing:
                                   ├── Session.update() + trim
                                   ├── _maybe_remember() (>300 字符触发)
                                   └── _schedule_profile_update() (asyncio.create_task)
                              │
                              └── Send response:
                                   ├── ≤300 字符 → _safe_send() (retry 2x)
                                   └── >300 字符 → _split_text() → _safe_send() × N (1s 间隔)
```

---

## 七、SearXNG 集成

### 7.1 架构

SearXNG 作为自托管元搜索引擎，聚合多个搜索引擎的结果，无需 API Key。Docker 容器部署，通过 JSON API 与 Agent 交互。

```
Agent search_web(query)
       │
       ▼
SearXNG JSON API  ←── Docker 容器 (searxng/searxng:latest)
  http://localhost:8082/search?format=json&q=...
       │
       ├── bing ────────── 主要搜索引擎
       ├── bing news ───── 新闻搜索
       ├── duckduckgo ──── 备用引擎
       └── mwmbl ───────── 备用引擎

  → 搜索有结果 → LLM 基于摘要作答
  → 搜索无结果 → 若有已知 URL → web_fetch(url) → 抓取完整页面文本
  → 搜索无结果 + 无已知 URL → 告知用户无法获取
```

### 7.2 配置文件

配置文件位于项目根目录 `searxng/settings.yml`，通过 Docker volume 挂载到容器 `/etc/searxng/settings.yml:ro`。

**关键配置项**:

| 配置项 | 值 | 说明 |
|--------|-----|------|
| `use_default_settings` | `true` | 继承 SearXNG 内置默认值 |
| `server.limiter` | `false` | 禁用速率限制 (仅本地 Agent 访问) |
| `server.port` | `8080` | 容器内端口 (映射到 host 8082) |
| `search.formats` | `[html, json]` | 启用 JSON API |
| `search.default_lang` | `zh-CN` | 默认中文 |
| `search.safe_search` | `1` (moderate) | 中等安全搜索 |
| `outgoing.request_timeout` | `15.0s` | 上游引擎请求超时 |
| `outgoing.max_request_timeout` | `20.0s` | 最大超时 |

**注意**: 不再挂载 `limiter.toml`。最新版 SearXNG 的 limiter.toml schema 不兼容自定义配置，通过 `server.limiter: false` 禁用即可。

### 7.3 国内网络适配 (GFW)

从中国大陆访问时，Google / DuckDuckGo / Wikipedia 等默认引擎全部 ConnectTimeout。配置中显式启用了两个可在国内访问的引擎:

| 引擎 | 国内可达 | 说明 |
|------|----------|------|
| **bing** | 是 | 主要搜索引擎，国内可直接访问 |
| **bing news** | 是 | 新闻搜索 |
| google | 不稳定 | 默认引擎，偶有超时但不影响使用 |
| duckduckgo | 不稳定 | 同上 |
| wikipedia | 不稳定 | 初始化 SPARQL 查询超时概率高 |

### 7.4 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `SEARXNG_ENDPOINT` | `http://localhost:8082` | SearXNG JSON API 地址 (Docker 内用 `http://searxng:8080`) |

### 7.5 启动与验证

```bash
# 启动 SearXNG
docker compose up -d searxng

# 验证服务可用
curl "http://localhost:8082/search?format=json&q=test"

# 查看日志 (排查引擎超时)
docker logs searxng --tail 20

# 常见问题: 容器反复重启 → 检查 settings.yml 语法 + 移除 limiter.toml 挂载
# 常见问题: 0 结果 → 等待 15s 让引擎初始化完成
# 常见问题: 全部引擎超时 → 检查容器出站网络 (docker exec searxng python3 -c "import urllib; ...")
```

### 7.6 天气查询

天气通过 `search_web` 统一处理 —— Agent 搜索 "城市名 天气"，SearXNG 返回天气网站结果，LLM 从结果中提取温度/湿度/风力等信息并整合为自然语言回复。无需独立的天气 API。

---

## 八、多模态 LLM 集成

### 8.1 架构

当用户发送图片或语音时，Agent 自动下载并保存到工作区，然后通过 `read_file` 工具分析。若配置了多模态 LLM，图片会发给视觉模型进行 AI 分析，音频会发给音频模型进行语音转文字+情绪分析。

```
QQ 用户发送图片/语音
       │
       ▼
agent_router: 检测 seg.type == "image" / seg.type == "record"
       │
       ├── 图片: _download_and_save_file(url) → data/workspace/uploads/{uuid}-image.png
       ├── 语音: _download_voice(bot, seg_data, message_id)
       │          ├── 策略1: 本地文件读取 (path/url 字段)
       │          ├── 策略2: OneBot API (get_record/get_file + 多参数名)
       │          └── 策略3: NapCat HTTP API (多端点+多方法)
       │
       ▼
augmented_message = "[用户发送了语音消息，已保存至: .../voice.amr]\n用户发送了语音消息，可以使用 read_file 工具分析音频内容。"
       │
       ▼
Agent calls read_file(".../voice.amr")
       │
       ├── ffprobe: 元数据 (格式/时长/采样率/声道)
       │
       ├── Audio configured?
       │   YES → SILK检测 → pilk解码 → ffmpeg转WAV → DashScope原生API /
       │          通用video_url API → "转写: ... 情绪: ... 背景: ..."
       │   NO  → 返回元数据 + 配置指引
       │
       ▼
Agent synthesizes response
```

### 8.2 图片分析

多模态配置统一存储在 `QQBot/config/models_settings.json` (git-ignored) 的 `MULTIMODAL_MODEL` 部分。旧版 `multimodal.json` 仍作为向后兼容的回退。

```json
{
  "MULTIMODAL_MODEL": {
    "api_key": "your-api-key",
    "api_base": "https://api.openai.com/v1",
    "model": "gpt-4o",
    "max_tokens": 2048,
    "temperature": 0.7
  }
}
```

| 字段 | 说明 |
|------|------|
| `api_key` | API 密钥 (留空则禁用图片分析) |
| `api_base` | API 端点地址 (支持 OpenAI 兼容的 vision API) |
| `model` | 视觉模型名称 (如 gpt-4o, claude-3-opus, Qwen2.5-VL 等) |
| `max_tokens` | 最大输出 token 数 (默认 2048) |
| `temperature` | 采样温度 (默认 0.7) |

**支持的视觉 API 格式**: OpenAI-compatible (GPT-4V/Azure/vLLM)。任何支持 `/chat/completions` + `image_url` content 格式的 API 均可使用。

**无配置时的降级行为**: 图片分析仅返回元数据 (尺寸/格式/大小) + 配置指引信息，不会出错。

**Thinking Mode 兼容**: 若多模态模型返回 `reasoning_content`（如 Qwen thinking mode），客户端会在回复中保留并以 `[思考]...[回复]...` 格式呈现。由于多模态调用为单轮请求（无对话历史），reasoning_content 无需回传，不会触发 API 400 错误。

### 8.3 音频分析

音频分析通过 `AUDIO_MODEL` 配置实现，支持 DashScope 原生多模态 API 和通用 OpenAI 兼容 API 两种模式。

```json
{
  "AUDIO_MODEL": {
    "api_key": "your-dashscope-key",
    "api_base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "model": "qwen3-omni-flash",
    "max_tokens": 20480,
    "temperature": 0.7
  }
}
```

| 字段 | 说明 |
|------|------|
| `api_key` | API 密钥 (留空则禁用音频分析) |
| `api_base` | API 端点。DashScope 地址会自动走原生多模态 API；其他地址走通用 video_url fallback |
| `model` | 支持音频的多模态模型 (如 qwen3-omni-flash, GPT-4o-audio-preview) |
| `max_tokens` | 最大输出 token 数 (默认 2048) |
| `temperature` | 采样温度 (默认 0.7) |

**音频处理流程**:

```
QQ语音(SILK_V3) → NapCat(.amr) → pilk解码 → ffmpeg → 16kHz mono WAV PCM S16LE
  → base64(data:audio/wav;base64,...）
  → DashScope原生API ({"audio": data_uri} in message content)
  → 返回: 语音转文字 + 语气/情绪 + 声线特征 + 背景音 + 场景描述
```

**关键依赖**:
- `ffmpeg`: 音频格式转换（AMR/SILK → WAV），已包含在 setup.sh
- `pilk`: Python SILK_V3 解码库（`#!SILK_V3` 文件头检测 + silk_to_wav），已加入 requirements.txt
- DashScope 原生 multimodal-generation API endpoint: `/api/v1/services/aigc/multimodal-generation/generation`

**API 路径选择**:
| api_base 特征 | 使用的 API | content type |
|--------------|-----------|-------------|
| 包含 `dashscope.aliyuncs.com` | DashScope 原生多模态 API | `{"audio": data_uri}` |
| 其他 | OpenAI 兼容 `/chat/completions` | `{"video_url": {"url": data_uri}}` |

**无配置时的降级行为**: 音频分析仅返回元数据 (格式/时长/采样率/声道/文件大小) + 配置指引信息。

---

## 九、测试系统

`test_agent.py` 包含 7 个测试套件, 38 个测试用例，覆盖所有核心组件:

| # | 测试套件 | 测试数 | 覆盖内容 |
|---|----------|--------|----------|
| 1 | `TestToolRegistry` | 7 | 注册/列表/Schema/同步执行/异步执行/错误/移除/包含 |
| 2 | `TestSessionManager` | 6 | 创建/超时/裁剪/清空/删除/持久化 |
| 3 | `TestMemorySystem` | 4 | 保存/读取/遗忘/搜索/列出 |
| 4 | `TestAgentCore` | 9 | 启动/提示词构建/纯文本/工具循环/会话持久化/清空/最大迭代/画像注入/记忆注入 |
| 5 | `TestUserProfile` | 5 | 创建/保存/空上下文/完整上下文/去重/持久化 |
| 6 | `TestDeepSeekClientParsing` | 3 | 解析纯文本/工具调用/混合响应 |
| 7 | `TestBuiltinTools` | 4 | get_time/execute_code(成功/错误)/search_web |

**运行方式**:
```bash
cd QQBot
python test_agent.py
# 或
python -m pytest test_agent.py -v
```

---

## 十、部署方式

### 手动部署 (当前开发环境)

1. **安装系统依赖**: 参考 `napcat.sh` 的 `install_dependency` 函数
2. **安装 Napcat** (Rootless Shell): `bash napcat.sh --docker n`
3. **配置 Napcat WebUI**: 反向 WebSocket → `ws://127.0.0.1:8081/onebot/v11/ws`，Access Token 与 `.env` 一致
4. **启动 SearXNG**:
   ```bash
   docker compose up -d searxng
   # 或: docker run -d --name searxng -p 8082:8080 searxng/searxng
   ```
5. **配置 Python 虚拟环境**:
   ```bash
   python3 -m venv ~/.virtualenvs/QQBotAgent
   source ~/.virtualenvs/QQBotAgent/bin/activate
   pip install -r QQBot/requirements.txt
   ```
6. **配置 `.env`**: 设置 `DRIVER=~fastapi`, `HOST=0.0.0.0`, `PORT=8081`, `ONEBOT_ACCESS_TOKEN`, `SUPERUSERS`, `SEARXNG_ENDPOINT`
7. **启动 NoneBot**: `cd QQBot && nb run`
8. **启动 Napcat**: `xvfb-run -a /path/to/qq --no-sandbox`
9. **验证**: 发送 `@Roxy /status` 确认 21 个工具已注册、SearXNG 可连通
10. **配置多模型 (可选)**: 编辑 `QQBot/config/models_settings.json` 填入各模型的 API 信息，参考 `models_settings_example.json` 格式

### Docker 部署

```bash
docker compose up -d
```

包含 SearXNG + Napcat + NoneBot + vLLM 完整栈。

---

## 十一、多模型架构 (Multi-Model Architecture)

### 11.1 设计理念

参考 Claude Code 的模型分层策略，QQBot 使用三种不同能力的模型分工合作，以合理节约 token 消耗：

| 模型层级 | 用途 | 特点 |
|----------|------|------|
| **FLASH_MODEL** | 复杂度分类 + 简单对话 | 轻量、快速、低成本。处理问候/闲聊/常识问答 |
| **REASONING_MODEL** | 复杂推理 + 工具调用 | 强大、高能力。处理搜索/代码/多步推理/专业知识 |
| **MULTIMODAL_MODEL** | 图片理解 | 视觉能力。处理用户发送的图片 AI 分析 |

### 11.2 任务路由流程

```
用户消息 → agent_router
  │
  ├── 特殊命令 (/clear, /status) → 直接处理，不做分类
  │
  └── 自然语言
       │
       ▼
     ModelRouter.classify_complexity(message)
       │  FLASH_MODEL (轻量 prompt, 30s 超时)
       │  判断: "simple" 或 "complex"
       │
       ├── "simple" → Agent.run(message, user_id, client=flash_client)
       │                FLASH_MODEL 直接回复 (低 token 消耗)
       │
       ├── "complex" → Agent.run(message, user_id, client=reasoning_client)
       │                REASONING_MODEL + 工具调用 (搜索/代码/文件)
       │
       └── 错误/超时 → 安全回退到 "complex"
                       REASONING_MODEL (宁可多想不能少想)
```

### 11.3 配置文件

所有模型配置统一在 `QQBot/config/models_settings.json` (git-ignored):

```json
{
  "REASONING_MODEL": {
    "api_key": "", "api_base": "", "model": "",
    "max_tokens": 4096, "temperature": 0.7
  },
  "FLASH_MODEL": {
    "api_key": "", "api_base": "", "model": "",
    "max_tokens": 1024, "temperature": 0.7
  },
  "MULTIMODAL_MODEL": {
    "api_key": "", "api_base": "", "model": "",
    "max_tokens": 2048, "temperature": 0.7
  },
  "task_routing": {
    "triage": "FLASH_MODEL",
    "simple_task": "FLASH_MODEL",
    "complex_task": "REASONING_MODEL",
    "image_analysis": "MULTIMODAL_MODEL",
    "triage_prompt": "请判断以下用户消息的复杂度..."
  }
}
```

### 11.4 回退机制

| 场景 | 行为 |
|------|------|
| 所有模型配置留空 | 全部回退到 `.env` 的 DeepSeek 默认配置 |
| 部分模型配置留空 | 已配置的模型使用指定 API，未配置的回退到默认 |
| 复杂度分类失败 | 回退到 "complex" → REASONING_MODEL (安全优先) |
| `models_settings.json` 不存在 | multimodal_client 回退到 `multimodal.json` |
| `multimodal.json` 也不存在 | 图片仅返回元数据，音频返回元数据+配置指引 |
| AUDIO_MODEL 未配置 | 音频分析回退到 MULTIMODAL_MODEL（若支持音频），否则仅返回元数据 |

### 11.5 Token 优化效果

| 场景 | 无路由 | 有路由 | 节省 |
|------|--------|--------|------|
| 简单问候 "你好" | REASONING_MODEL 回复 (~200 tokens) | FLASH_MODEL 回复 (~200 tokens) | 分类 prompt ~50 tokens |
| 闲聊 "今天心情好" | REASONING_MODEL 回复 (~300 tokens) | FLASH_MODEL 回复 (~300 tokens) | 分类 prompt ~50 tokens |
| 复杂搜索 | REASONING_MODEL 回复 (~1000 tokens) | REASONING_MODEL 回复 (~1000 tokens) | 几乎相同 (+分类开销) |
| 图片分析 | REASONING_MODEL (不支持图片) | MULTIMODAL_MODEL 分析 (~500 tokens) | 功能实现 |

**净收益**: 大量简单对话由低成本 FLASH_MODEL 处理，高成本 REASONING_MODEL 仅用于需要推理的复杂任务。分类开销固定 (~50 tokens)，在每次对话中被节省的推理成本覆盖。

---

## 十二、架构演进

> 注: 本节内容已移至上方 "十一、多模型架构" 独立章节。此处保留演进历史。

### v1.x — 分布式命令 (已废弃)

```
用户消息 → on_command / on_message 分发器
  ├── hello → handle_hello()
  ├── 测速 → handle_speed_test()
  ├── 单抽 → handle_draw()
  ├── deepseek → handle_deepseek()
  ├── chat → handle_context_chat()
  └── group_msg → handle_group_msg()  (关键字匹配)
```

**问题**: 每个命令独立处理，无统一智能路由，关键字匹配僵硬，无法组合工具调用。

### v2.0 — Agent 统一入口 (当前)

```
用户消息 → on_message(to_me) → Agent 统一入口
  ├── LLM 理解意图
  ├── 自主选择工具 (OpenAI Function Calling)
  ├── 多轮 Think→Act→Observe→Respond (最多 12 轮)
  └── 智能回复 + 用户画像 + 长期记忆
```

### v2.1 — SearXNG + 工作区隔离

```
v2.0 基础上增加:
  ├── SearXNG 自托管元搜索 (替代 DDG HTML 抓取)
  ├── 天气通过搜索 + LLM 合成 (移除独立 check_weather)
  ├── 工作区隔离: python3 -I + 模式匹配 + 路径验证
  ├── Git URL 验证: HTTPS only + 命令注入防护
  └── 可配置工作区根目录: QQBOT_WORKSPACE 环境变量
```

### v2.2 — 国内网络适配 + 消息发送加固

```
v2.1 基础上增加:
  ├── SearXNG 国内优化: 仅启用 bing/bing_news, 禁用被墙引擎
  ├── limiter.toml 移除: 新版 SearXNG schema 不兼容, 用 server.limiter=false 替代
  ├── _safe_send() 重试机制: ActionFailed 自动重试 (递增退避)
  ├── _split_text() 智能拆分: 句子边界断句, 300 字符/块
  ├── 消息速率控制: 1s 发送间隔, 避免 QQ 限速 retcode 1200
  └── thinking 提示非阻塞: 发送失败不影响主流程
```

### v2.3 — 文件阅读 + 多模态 LLM

```
v2.2 基础上增加:
  ├── 文件附件检测: 扫描 MessageSegment image/file 类型, 自动下载到 uploads/
  ├── read_file 工具: 扩展名检测 → 文本/PDF/图片分类处理
  ├── 多模态 LLM 客户端: OpenAI 兼容 vision API, JSON 配置文件 (git-ignored)
  ├── 优雅降级: 未配置多模态时, 图片仅返回元数据 + 配置指引
  └── 下载安全: 50MB 大小限制, UUID 防碰撞文件名, 120s 超时
```

### v2.4 — 多模型路由

```
v2.3 基础上增加:
  ├── 三模型架构: REASONING_MODEL / FLASH_MODEL / MULTIMODAL_MODEL
  ├── ModelRouter: 统一管理多客户端, 复杂度分类, 任务路由
  ├── 复杂度分类 (triage): FLASH_MODEL 判断 simple/complex, 安全回退
  ├── DeepSeekClient 可配置构造: 支持多实例 (不同 api_key/base/model)
  ├── Agent.run() 模型切换: 可选 client 参数, 运行时路由
  ├── 统一配置文件: models_settings.json (含 task_routing 规则)
  └── Token 优化: 简单对话→FLASH_MODEL (轻量), 复杂任务→REASONING_MODEL (强力)
```
### v2.5 — 群聊连续对话

```
v2.4 基础上增加:
  ├── ContinuousSessionManager: 群聊免@窗口管理 (per-group, per-user)
  ├── continuous_router: 第二消息处理器 (priority=2, 无 to_me 规则)
  ├── 自动开启: @机器人 回复后自动为发起者打开 5 分钟窗口
  ├── 消息续期: 每条消息重置计时器为完整 5 分钟
  ├── 取消命令: /取消, #取消, /结束, #结束 → 手动关闭窗口
  ├── Agent 感知: [连续对话模式] 前缀 → 简洁回复, 适时建议退出
  ├── 超时清理: 5 分钟无消息自动过期, 静默清理
  └── 防重复: continuous_router 检查 is_tome() 避免与 agent_router 重复处理

### v2.6 — reasoning_content 全链路保留

```
v2.5 基础上增加:
  ├── DeepSeekClient._parse_response: 解析时保留 reasoning_content 字段
  ├── Session.add_message: 新增 reasoning_content 可选参数, 持久化到 context
  ├── Agent 主循环: assistant 消息携带 reasoning_content 回传 API
  ├── Agent 最终回复: 通过 session.add_message 保留 reasoning_content
  └── MultimodalClient: 单轮请求中 reasoning_content 以 [思考]...[回复] 格式呈现

**解决问题**: DeepSeek v4 pro / Qwen 等 thinking mode 模型要求 reasoning_content
必须在后续请求中原样回传, 否则返回 HTTP 400:
"The reasoning_content in the thinking mode must be passed back to the API."

### v2.7 — 实时进度推送

```
v2.6 基础上增加:
  ├── Agent.run(): 新增 progress_callback 可选参数
  ├── 触发时机: 每轮工具执行前, 发送 "⏳ 正在{tool_names}..."
  ├── 去重逻辑: 相邻轮次相同工具集不重复推送
  ├── 轮次感知: 第3轮起追加 "⏳ 第{n}轮: 正在..."
  └── 容错: callback 异常不影响 Agent 主循环

**设计原则**: Agent 不依赖 QQ 层, progress_callback 由 agent_router
通过 _safe_send 包装后注入。失败时静默忽略, 不影响消息处理。
```

### v2.8 — 地图 & 位置服务 (当前)

```
v2.7 基础上增加:
  ├── lib/amap_client.py: 高德地图 API 共享客户端 (GET + 错误处理)
  ├── tools/map_tools.py: 5 个地图工具
  │   ├── geocode: 地址 → 经纬度
  │   ├── reverse_geocode: 经纬度 → 地址 + 周边
  │   ├── get_weather: 实时天气 / 4天预报 (替代搜索方式)
  │   ├── search_poi: POI 搜索 (餐厅/地铁/银行等)
  │   └── plan_route: 路径规划 (驾车/步行/公交)
  └── 配置: .env 中新增 AMAP_API_KEY

**API**: 高德地图 Web Services, 免费 5000 次/天, 无需实名。
工具数量: 11 → 16
```

### v2.9 — 抽卡动画工具 (2026-05-27)
```
新增: play_gacha_animation
  - 拆分为两个工具: gacha_pull (文字结果) + play_gacha_animation (图片动画)
  - 通过 contextvars 实现工具→QQ 图片发送, 无需修改中间层签名
  - agent/context.py: _send_msg ContextVar, agent_router 在执行前 set, 执行后 reset
  - _safe_send 签名扩展: 同时接受 str 和 MessageSegment
  - 动画帧: 6~7 张图片, 0.75s 间隔, 根据星级和单/十连选择动画分支
  - 文件: agent/context.py (新增), tools/legacy_tools.py (修改), plugins/agent_router.py (修改)

工具数量: 16 → 17
```

### v2.10 — 抽卡数据外部化 (2026-05-27)
```
重构: pullingMonitor.py + gacha_data.json
  - 80 行硬编码数据 (10 个数据结构) 迁移至 config/gacha_data.json
  - JSON 结构: pools (角色/羁绊池) + banners (卡池概率配置)
  - 五星羁绊池 (bonds_five_star_all/tricolor) 由加载器从角色数据自动派生
  - drawing_cards() 重写为数据驱动: 遍历 banner.categories, 按 pool 引用分发
  - 动态池标记 (up_character, up_bond, non_up_bonds_five_star, up_character_special) 在代码中处理
  - 添加/修改角色只需编辑 JSON, 无需改 Python 代码
  - 文件: config/gacha_data.json (新增), plugins/pullingMonitor.py (重构)
```

### v2.11 — 代码执行图表输出 + 高负载任务拒绝 (2026-05-27)
```
1. execute_code 支持图表输出:
  - execute_code 改为 async, 执行后扫描工作目录中的生成图片 (.png/.jpg/.svg/.gif/.webp/.pdf)
  - 图片拷贝到 data/workspace/output/ 持久化保存
  - 通过 _send_msg contextvar 自动发送到 QQ 聊天窗口
  - 文本结果中列出生成的图表路径

2. 高负载任务拒绝:
  - WORKSPACE.md 新增 §4: 服务器硬件规格 (2核/4GB/50GB+50GB/无GPU)
  - 必须拒绝: 训练 ML 模型、视频处理、>50MB 数据、本地 LLM 推理、编译大型项目、大规模爬虫
  - 警告后执行: 10-50MB 数据、3-10 张图表
  - SOUL.md 新增 "高负载任务" 拒绝规则 + "服务器硬件上下文" 说明

文件: tools/builtin_tools.py (修改), agent/config/WORKSPACE.md (修改), agent/config/SOUL.md (修改)
```

### v2.12 — Shell 命令执行工具 (2026-05-27)
```
新增: shell_exec
  - 白名单制: 40+ 命令 (ls/find/cat/grep/wc/du/df/free/git/pip/python3 -c 等)
  - 子命令限制: git (status/log/show/diff/branch/...), pip (list/show/freeze)
  - 管道支持: 每个管道段独立验证
  - 安全拦截: 重定向 (>/>>/<)、命令替换 ($()/``)、链式执行 (;/&&/||)、后台 (&)、sed -i
  - 工作区锁定: cwd 固定在 WORKSPACE_ROOT, 干净环境变量
  - 输出截断: 100KB, 超时 30s
  - 验证函数与执行函数分离, 便于测试

工具数量: 17 → 18
```

### v2.13 — 特殊会话 + 用户工作区 + 硬件检测 (2026-05-27)
```
v2.12 基础上增加:
  ├── agent/hardware.py: 硬件自动检测 (CPU/内存/磁盘/GPU/OS)，首次启动缓存到 .hardware.json
  ├── agent/workspace.py: 用户工作区隔离 (per-user 目录, contextvars 传递), 配额管理 (3 级策略)
  ├── agent/special_session.py: 特殊会话 (百万 token 上下文, 快照+增量双层存储, 最多 3 个)
  ├── agent/agent.py 更新:
  │   ├── build_system_prompt(): 动态注入硬件上下文 (替换硬编码规格表)
  │   ├── run(): 新增 session_type 参数 → 路由到 SpecialSessionManager
  │   └── _build_messages(): 注入 session marker + quota context
  ├── agent/profile.py 更新: 画像存储路径迁移到 {USER_DATA_ROOT}/{safe_id}/profile.json (自动迁移旧路径)
  ├── plugins/agent_router.py 更新:
  │   ├── _handle_session_command(): 8 个特殊会话管理命令
  │   ├── session_type 检测: active_special → "special", 否则 → "temporary"
  │   └── contextvars 工作区隔离: 每次请求前 set 用户工作区路径
  ├── tools/builtin_tools.py 更新:
  │   ├── get_system_load(): 实时系统负载查询 (CPU/内存/磁盘评估)
  │   └── _get_workspace_root(): 优先检查 _current_user_workspace contextvar
  ├── agent/config/WORKSPACE.md 更新: 硬件规格改为动态检测引用
  ├── agent/config/TOOLS.md 更新: 新增 get_system_load 工具文档
  └── .env 更新: 新增 USER_DATA_ROOT / MAX_SPECIAL_SESSIONS / USER_WORKSPACE_QUOTA_MB

工具数量: 18 → 19
```

### v2.14 — web_fetch 网页抓取工具 (2026-05-27)
```
v2.13 基础上增加:
  ├── web_fetch(url): 直接抓取 HTTPS 网页并提取纯文本
  ├── HTML→文本转换: html.parser 剥离标签, 保留段落结构
  ├── 安全限制: HTTPS only, 2MB 响应上限, 8000 字符输出, 30s 超时
  ├── 搜索协作: SearXNG 搜索 → 返回 URL → web_fetch 抓取完整内容
  ├── explain_code_tool / translate_text: 改用 model_router.flash_client
  │   (修复 DeepSeek API 401 认证错误, 使用 models_settings.json 配置的 Flash 模型)
  └── 文档更新: TOOLS.md + WORKSPACE.md + DOCUMENTATION.md

工具数量: 19 → 20
```

### v2.15 — 音频理解工具 (2026-05-28)
```
v2.14 基础上增加:
  ├── QQ语音消息处理:
  │   ├── agent_router 新增 _download_voice(): 3 层 fallback 策略
  │   │   (本地文件 → OneBot API (4 action × 2 param) → HTTP API (5 endpoint))
  │   ├── 检测 seg.type == "record" → 下载语音 → 注入上下文
  │   └── NoneBot2 依赖注入: bot: Bot 参数获取 OneBot API
  │
  ├── SILK_V3 解码:
  │   ├── QQ 语音实际格式为 SILK_V3 (NapCat 误导性标签为 .amr)
  │   ├── pilk (Python SILK 解码库): 检测 #!SILK_V3 文件头 → silk_to_wav(rate=16000)
  │   └── requirements.txt 新增 pilk 依赖, setup.sh ffmpeg 已包含
  │
  ├── MultimodalClient 音频分析 (analyze_audio):
  │   ├── _convert_audio_format(): 始终通过 ffmpeg 转 16kHz mono WAV PCM S16LE
  │   ├── _extract_raw_pcm(): wave 模块提取裸 PCM 采样
  │   ├── DashScope 原生多模态 API (qwen3-omni-flash):
  │   │   └── endpoint: /api/v1/services/aigc/multimodal-generation/generation
  │   │   └── audio 嵌入消息 content: {"audio": "data:audio/wav;base64,..."}
  │   ├── 通用兼容模式 fallback: video_url content type
  │   └── 分析内容: 语音转文字 + 语气/情绪 + 声线特征 + 背景音 + 场景描述
  │
  ├── file_tools.py 扩展:
  │   ├── _read_audio_file(): ffprobe 元数据 + MultimodalClient AI 分析
  │   ├── 支持 11 种音频格式 (.amr/.wav/.mp3/.ogg/.m4a/.aac/.flac/.opus/.wma/.aiff/.silk)
  │   └── read_file 工具描述更新: 提及音频/语音支持
  │
  ├── 配置:
  │   ├── models_settings.json 新增 AUDIO_MODEL 层级 (api_key/api_base/model)
  │   ├── is_audio_available(): 优先 AUDIO_MODEL, 回退 MULTIMODAL_MODEL
  │   ├── .env 新增 NAPCAT_HTTP_BASE=http://127.0.0.1:6099
  │   └── AUDIO_MODEL 未配置时返回元数据 + 配置指引
  │
  └── Agent 循环放宽:
      ├── max_tool_iterations: 8 → 12
      └── asyncio.wait_for timeout: 200s → 300s

工具数量: 20 (不变, read_file 功能扩展)
```

### v2.16 — 三层权限系统 (2026-05-29)
```
v2.14 基础上增加:
  ├── agent/permissions.py: UserRole 枚举 + CodeLimits 数据类 + PermissionManager
  │   ├── 身份识别: SUPERUSERS 环境变量 → admin, VIP_USERS → vip, 默认 → regular
  │   ├── 工具权限矩阵: _PUBLIC_TOOLS (15), _VIP_TOOLS (4), _ADMIN_TOOLS (shell_exec)
  │   └── 资源配额: 工作区磁盘 (admin=2GB, vip=500MB, regular=100MB), 特殊会话数 (10/3/1)
  │
  ├── agent/context.py: 新增 _current_user_role + _current_code_limits contextvars
  │   └── 权限信息在请求链路中通过 contextvars 传递，无需修改工具函数签名
  │
  ├── agent/tool_registry.py: 新增 get_schemas_for(allowed_names)
  │   └── 根据用户角色过滤工具 schema，LLM 只看到允许调用的工具
  │
  ├── agent/agent.py 修改:
  │   ├── run(): 新增 allowed_tools + user_role 参数
  │   ├── LLM 调用使用 get_schemas_for() 过滤 schema (schema 过滤防线)
  │   └── _execute_tool_calls(): 硬拦截非允许工具调用 (纵深防御)
  │
  ├── tools/builtin_tools.py: execute_code 读取 _current_code_limits contextvar
  │   └── 按角色应用分级限制: admin (60s/100KB/256MB), vip (15s/50KB/128MB)
  │
  ├── plugins/agent_router.py: 集成 PermissionManager
  │   ├── 请求入口解析角色 → 设置 contextvars → 传递 allowed_tools 给 agent.run()
  │   └── 权限不足处理: 礼貌说明，建议联系管理员，不暴露系统完整能力
  │
  └── agent/config/AGENTS.md: 新增权限系统文档段
      ├── 用户层级表 (角色/识别方式/权限范围)
      ├── 权限不足处理原则 (5 条)
      └── Permission 错误拦截说明

设计原则: Schema 过滤为主, 硬拦截为纵深防御, 环境变量认证, 权限不足不暴露系统能力
```
# QQBot 项目文档

## 项目概述

**QQBot** 是基于 [NapCat](https://github.com/NapNeko/NapCatQQ) + [NoneBot2](https://nonebot.dev/) 构建的 **LLM Agent 智能体**，采用 Markdown 驱动的现代智能体架构，支持工具调用（Tool Calling）、用户画像、长期记忆、特殊会话（百万 token 上下文）与用户工作区隔离。AI 后端使用 DeepSeek API。

- **智能体架构**: Think→Act→Observe→Respond 循环，Markdown 配置文件驱动
- **运行框架**: NoneBot2 (Python)
- **QQ 协议适配**: NapCat (OneBot V11 反向 WebSocket)
- **AI 后端**: DeepSeek API (OpenAI 兼容 Function Calling) + 多模型路由 (REASONING/FLASH/MULTIMODAL/AUDIO/OCR)，模型配置可经 Web 面板结构化编辑（保存前真实探测 API 可用性）并热重载
- **搜索引擎**: SearXNG (Docker 自托管，聚合 Bing/DDG) + `web_fetch` 直接抓取网页
- **特殊会话**: 每用户持久化会话（按角色：管理员 10 / 会员 3 / 普通 1），百万 token 上下文，快照+增量存储
- **用户工作区**: 每用户独立文件空间，配额管理，跨会话隔离
- **三层记忆引擎**: `TieredMemory`（SHORT/MEDIUM/LONG），LLM judge 归类 + 频次晋升/年龄降级状态机
- **Web 管理面板**: FastAPI 独立服务（`webui/`），进程管理、配置热编辑、记忆/画像/别名管理、Token 看板、反馈处理、更新记录编辑与群发、Playground
- **更新记录**: 结构化更新日志（版本/日期/分类变更条目），用户经 `/更新日志 [d]`、`/update record [d]` 或自然语言查询，管理员在面板编辑并群发更新公告；QQ 端以图片卡片呈现，并同步脱敏快照到公开站点 `bot.oneweblog.cn`
- **部署方式**: Docker Compose（含 NVIDIA GPU 支持）或手动部署

---

## 目录结构

```
QQBotAgent/
├── bot.py                   # NoneBot 启动入口（备用；生产用 `cd QQBot && nb run`）
├── main.py                  # PyCharm 占位入口（非实际入口）
├── setup.sh / start.sh / stop.sh / start_bot.sh  # 安装与启动脚本
├── start_webui.sh           # Web 管理面板 启动/停止/重启/状态
├── test.sh                  # 测试脚本（10 个脚本，其中 8 个在被 gitignore 的 test/ 下）
├── Dockerfile               # Docker 镜像构建文件
├── docker-compose.yml       # Docker Compose 编排 (含 SearXNG + QQBot + vLLM)
├── docker-entrypoint.sh     # Docker 容器入口脚本
├── vllm-start.sh            # vLLM 推理服务启动脚本
├── napcat.sh                # Napcat 安装脚本 (Rootless)
├── searxng/                 # SearXNG 配置
│   └── settings.yml         #   搜索引擎配置 (Bing, 国内优化)
│
├── webui/                   # ★ Web 管理面板（FastAPI 独立服务，绑定 127.0.0.1:8090）
│   ├── main.py              #   面板入口（71 个 /api/ 端点 + 16 个页面路由 + 2 个 WebSocket）
│   ├── auth.py              #   登录鉴权（scrypt 口令哈希 + 内存会话）
│   ├── process_manager.py   #   进程管理（启动/停止 bot，看门狗状态持久化）
│   ├── config_editor.py     #   配置热编辑（密钥脱敏；模型配置分段 prepare/commit + 原子写）
│   ├── model_probe.py       #   模型 API 可用性探测（最小真实调用 + 分级判定）
│   ├── data_reader.py       #   记忆/画像/会话/Wiki 别名/更新记录等数据读写（写前备份）
│   ├── log_viewer.py / audit.py / hardware_monitor.py / playground.py
│   ├── config.py            #   面板配置（从 QQBot/.env 读 USER_DATA_ROOT 等）
│   ├── templates/ static/   #   前端页面（19 个模板）与静态资源
│   └── data/                #   面板运行时数据（webui.pid / logs / watchdog_state.json）
│
├── public_site/             # ★ 公开使用站点（bot.oneweblog.cn，纯静态只读，无登录/无后端）
│   ├── index.html / style.css / app.js   # 单页：场景 / 怎么用 / 演示 / 更新记录
│   ├── changelog.json       #   ← 自动生成（WebUI 保存时脱敏导出），勿手改
│   └── assets/              #   演示截图/GIF（缺失时占位降级）
│
├── docs/                    # 开发者向文档（更新日志.md、bot-site-nginx.md 等）
│
└── QQBot/                   # NoneBot 机器人主体
    ├── .env                 # NoneBot 环境配置 (含 DeepSeek/OneBot 密钥) ⚠ git-ignored
    ├── .env.example         #   .env 说明文档 + 模板 (逐行注释每个变量)
    ├── requirements.txt     # Python 依赖
    ├── pyproject.toml       # NoneBot 项目配置
    ├── config.yml           # NoneBot 配置
    ├── test_agent.py        # Agent 系统测试套件 (14 套件)
    ├── test_workspace.py    # 工作区/会话文件测试 (11 类)
    │
    ├── agent/               # 智能体核心
    │   ├── agent.py         #   主循环: Think→Act→Observe→Respond
    │   ├── tool_registry.py #   工具注册表 (OpenAI JSON Schema 生成)
    │   ├── session.py       #   会话管理 (per-user, timeout, trim 迟滞, 持久化, mtime 重校验)
    │   ├── special_session.py #  特殊会话管理 (百万 token, 快照+增量存储, 按角色限制数量)
    │   ├── continuous_session.py # 群聊连续对话窗口管理 (90秒免@)
    │   ├── hardware.py      #   硬件自动检测 & 动态任务拒绝
    │   ├── workspace.py     #   用户工作区隔离 & 配额管理
    │   ├── workspace_snapshot.py # 工作区目录树快照 (get_user_info 用)
    │   ├── quota_cleanup.py #   配额清理协议
    │   ├── context.py       #   执行上下文传递 (contextvars, 工具→QQ图片)
    │   ├── permissions.py   #   三层权限系统 (PermissionManager, UserRole)
    │   ├── memory.py        #   长期记忆: MemorySystem (Markdown) + TieredMemory (三层引擎)
    │   ├── fact_filter.py   #   确定性事实过滤 (画像/记忆抽取前置)
    │   ├── profile.py       #   用户画像 (LLM 批量提取, mtime 重校验)
    │   ├── task_record.py   #   子任务结构化记录 (begin_task/finalize_subtask 配套)
    │   ├── search_archive.py #  搜索结果存档 (search_web/web_fetch 全文落盘, recall_search_result 配套)
    │   ├── group_features.py #  群聊功能开关 (按群控制抽卡/图片/语音)
    │   ├── personality.py   #   人格管理 (多套人格切换)
    │   └── config/          #   智能体配置 (Markdown 文件)
    │       ├── SOUL.md / IDENTITY.md / AGENTS.md / MEMORY.md  # 注入系统提示词 (4 个)
    │       ├── TOOLS.md / BOOTSTRAP.md / SESSION.md           # 加载但不注入 (3 个)
    │       ├── HELP.md / FEATURES.md       # /帮助、/功能 命令读取
    │       ├── WORKSPACE.md / USER.md / HEARTBEAT.md          # 未加载 (文档遗留/失效)
    │       └── personalities/  # 人格定义 (assistant / roxy_character / rubi)
    │
    ├── plugins/             # NoneBot 插件目录
    │   ├── agent_router.py  #   ★ 统一消息入口 (唯一活跃的消息处理插件; 含配置看门狗)
    │   ├── pullingMonitor.py#   抽卡监控 & 卡池数据加载
    │   ├── check_redeem_code.py # 兑换码爬取 & 缓存管理
    │   ├── test.py          #   启动配置检查 (env 验证 + 客户端连通性)
    │   └── chat.py / deepseek_*.py / group.py / speed.py / photos.py / character.py / utils.py
    │                        #   [已废弃] 仅保留个别工具函数或全部注释
    │
    ├── tools/               # Agent 工具实现
    │   ├── builtin_tools.py #   内置工具 (搜索/抓取/代码/Shell/PDF/Git/时间/系统负载/删除文件)
    │   ├── file_tools.py    #   文件读取工具 (文本/PDF/图片/音频分析)
    │   ├── map_tools.py     #   地图工具 (地理编码/逆编码/天气/POI/路径)
    │   ├── legacy_tools.py  #   游戏/娱乐工具 (抽卡/动画/测速/乱速/解释/翻译)
    │   ├── character_detail.py # 角色详情查询 (面板/技能/倍率/属性/天赋/潜能)
    │   ├── bond_detail.py   #   羁绊详情查询 (类别/星级/攻击/生命/技能/获取/售价)
    │   ├── card_renderer.py #   角色/羁绊卡片图片渲染
    │   ├── battle_parser.py #   团战截图 OCR 解析 (行动值修正模块)
    │   ├── ocr_name_matcher.py # OCR 角色名模糊匹配
    │   ├── ag_skill_index.py   # 技能分类索引 (Skill 类 + 阵营触发方式分类)
    │   ├── ag_trigger_engine.py # 行动值效果触发链推断
    │   ├── ag_llm_resolver.py  # 拉/推条技能 LLM 解析
    │   ├── buff_vocab_dump.py  # buff 词表导出 (模板采集辅助)
    │   ├── wiki_scraper.py  #   Wiki 爬虫 (角色/羁绊数据 + buff 图标)
    │   ├── name_resolver.py #   角色别名解析 (模糊匹配; 别名字典支持面板编辑+热重载)
    │   └── changelog.py     #   更新记录读写/格式化 (纯 IO; 命令+工具+群发轮询共用)
    │
    ├── scripts/             # 运维脚本
    │   ├── migrate_memory_p1.py     # 旧记忆 → 三层结构迁移
    │   └── cleanup_profile_facts.py # 画像事实存量清理
    │
    ├── config/              # 配置文件
    │   ├── multimodal.json  #   多模态 LLM 配置 (已被 models_settings.json 取代)
    │   ├── models_settings.json  #   多模型配置 (REASONING/FLASH/MULTIMODAL/AUDIO/OCR) ⚠ 含密钥
    │   ├── models_settings_example.json  #   多模型配置示例模板
    │   ├── characters/      #   角色别名字典 (character_dic.json / bonds_search_dic.json, 面板可编辑)
    │   ├── font/            #   卡片渲染字体
    │   ├── ocr_digit_templates/ # OCR 数字模板
    │   └── gacha_data.json  #   抽卡数据 (角色/羁绊/概率, 由 pullingMonitor 加载)
    │
    ├── lib/                 # 自定义库
    │   ├── deepseek_client.py  # DeepSeek API 客户端 (OpenAI 兼容 Function Calling)
    │   ├── multimodal_client.py # 多模态 LLM 客户端 (图片+音频理解; reload() 热重载)
    │   ├── model_router.py  #   多模型路由器 (复杂度分类 + 模型调度; reload() 热重载)
    │   ├── token_ledger.py  #   Token 用量记账 (WebUI 看板数据源)
    │   ├── amap_client.py   #   高德地图 API 客户端
    │   ├── ocr_engine.py    #   OCR 引擎 (BuffDetector 模板匹配 + 技能冷却检测)
    │   ├── buff_alias.py    #   buff 名称 → 图标标签别名映射 (提取/归一化)
    │   └── status_icons.py  #   状态图标文件名 → 中文标签 (STATUS_ICON_CN)
    │
    ├── data/                # Agent 运行时数据 (生产环境位于 USER_DATA_ROOT)
    │   ├── sessions/        #   会话持久化 (JSON)
    │   ├── memory/          #   长期记忆 (tiers/{uid}.json 三层结构 + tiers/long/ 快照 + 旧版 Markdown)
    │   ├── users/           #   用户画像 (JSON 文件)
    │   ├── token_usage/     #   Token 用量记录
    │   ├── task_log/        #   子任务结构化日志 ({uid}.jsonl)
    │   ├── search_cache/    #   搜索结果存档 ({uid}/{id}.json, 7天TTL+50条上限, 不占工作区配额)
    │   ├── audit/           #   审计日志 (JSONL)
    │   ├── feedback/        #   用户反馈/Bug 报告
    │   ├── name_index/      #   角色名索引缓存
    │   ├── wiki_cache/      #   Wiki 爬取缓存 (角色/羁绊/兑换码)
    │   ├── redeem_code/     #   兑换码缓存
    │   ├── changelog/       #   更新记录 (changelog.json 主数据 + known_groups/pending_broadcast/broadcast_status 面板↔bot 通信)
    │   ├── personality_config.json # 人格选择持久化
    │   └── workspace/       #   工作区 (代码执行/仓库/上传/输出, 按 QQ 号隔离)
    │
    └── images/              # 图像资源 (抽卡动画 / 测速样本数据)
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
│  │  1. Build Messages (四层分级, 见 §1.2):          │    │
│  │     ├── L1-L3 System Prompt (静态→用户级稳定)    │    │
│  │     ├── Conversation History (Session)          │    │
│  │     ├── L4 易变尾部 (时间/画像/MEDIUM 记忆注入)  │    │
│  │     └── Current User Message                    │    │
│  │                                                 │    │
│  │  2. THINK → LLM (chat_completion_with_tools)    │    │
│  │                                                 │    │
│  │  3. Has tool_calls?                             │    │
│  │     YES → ACT (Execute Tool) → OBSERVE → goto 2 │    │
│  │     NO  → RESPOND (return final answer)         │    │
│  │                                                 │    │
│  │  4. Post-processing:                           │    │
│  │     ├── Session.update() + trim (迟滞修剪)      │    │
│  │     └── _schedule_profile_update()              │    │
│  │         → ProfileManager.observe_turn() 缓冲,   │    │
│  │           每 5 轮后台批量 extract+judge          │    │
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

### 1.2 系统提示词结构（四层分级，前缀缓存友好）

v2.26 起消息序列按**变化频率**分四层构建（`agent.py:_build_messages`），越靠前越稳定，最大化提供商侧前缀缓存命中率：

```
L1 全局静态 (system)   → SOUL.md + IDENTITY.md + AGENTS.md + MEMORY.md + 硬件信息
                          所有用户共享同一前缀，字节级稳定（无时间戳）
L2 人格/群 (system)    → personality 块 + 群功能限制（同人格/同群共享）
L3 用户级稳定 (system) → 工作区路径 + 权限角色说明（同用户跨轮稳定）
   Conversation History → 会话上下文（迟滞修剪，头部每几轮才变一次）
L4 易变尾部 (system)   → 真实当前时间、特殊会话标记、quota 用量、
                          用户画像 (ProfileManager)、MEDIUM 记忆注入
                          (TieredMemory.build_medium_injection, top 15 条 ≤600 字符)
```

L4 置于历史**之后**：时间/画像/记忆每轮变化只影响尾部，不破坏 L1-L3 共享前缀。`build_system_prompt()` 只负责 L1（拼接 SOUL/IDENTITY/AGENTS/MEMORY 并缓存），`reload_configs()` 清空缓存重读。

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
| `memory_system` | `MemorySystem` | None | 旧版长期记忆 (可选) |
| `tiered_memory` | `TieredMemory` | None | 三层记忆引擎 (可选; P1 起 MEDIUM 注入的唯一来源) |
| `profile_manager` | `ProfileManager` | None | 用户画像 (可选) |
| `hardware_detector` | `HardwareDetector` | None | 硬件检测器 (可选) |
| `workspace_manager` | `UserWorkspaceManager` | None | 用户工作区管理 (可选) |
| `special_session_manager` | `SpecialSessionManager` | None | 特殊会话管理 (可选) |
| `max_tool_iterations` | `int` | 5 | 最大工具调用轮数（`agent_router.py:985` 实例化为 20） |
| `thinking_timeout` | `float` | 180.0 | LLM 思考超时秒数 |

| 方法 | 说明 |
|------|------|
| `build_system_prompt()` | 拼接 L1 全局静态层: SOUL + IDENTITY + AGENTS + MEMORY(三层记忆规则) + 硬件信息，结果缓存；不含时间戳（时间在 L4 尾部） |
| `reload_configs()` | 清空缓存，重新加载所有配置文件（配置看门狗调用） |
| `run(user_message, user_id, client=None, progress_callback=None, session_type="temporary")` | **主入口**。执行完整 Think→Act→Observe→Respond 循环。可选 `client` 参数支持运行时模型切换 (ModelRouter)。可选 `progress_callback` 在每轮工具执行前推送进度消息 (如 "⏳ 正在搜索...")。`session_type` 参数支持 `"temporary"` (30min 超时) 和 `"special"` (百万 token 持久化) 两种模式 |
| `_build_messages(session, user_message, optional_special_session=None)` | 按 §1.2 四层结构构建消息列表 (L1-L3 system + history + L4 易变尾部 + current)。L4 含时间/特殊会话标记/quota/画像/MEDIUM 记忆注入。历史消息中的 `reasoning_content` 自动保留以支持 thinking mode 模型 |
| `_compress_context(context, recent_full=20)` | 特殊会话上下文压缩: 最近 20 条保留完整，更早的 tool result 压缩为摘要首行；压缩边界按 `COMPRESS_STEP=4` 步进（相邻 4 轮字节级稳定，保护前缀缓存） |
| `_execute_tool_calls(tool_calls, session)` | 通过 ToolRegistry 执行 LLM 返回的工具调用；抽卡/测速类工具回复超 600 字自动折叠为降级记录 (`task_record`) |
| `_schedule_profile_update(user_id, msg, response)` | 调用 `ProfileManager.observe_turn()`（同步缓冲，达 5 轮后台批量 extract+judge），不阻塞回复。旧 `_maybe_remember` 原始转储路径已移除——长期记忆完全由画像提取流水线驱动 |
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
| `trim(max_messages)` | 迟滞修剪：仅当上下文超过 `max_messages + TRIM_HYSTERESIS`（=4）条时才一次性裁回最近 max_messages 条。避免每轮修剪头部导致提供商侧前缀缓存整体失效（Cache-Hit-Rate-Plan Phase 4） |
| `clear()` | 清空上下文 |

#### 类: `SessionManager`

| 构造参数 | 类型 | 默认值 | 说明 |
|----------|------|--------|------|
| `max_context_messages` | `int` | 20 | 每会话最大消息数 |
| `session_timeout` | `float` | 1800.0 | 会话超时秒数 (30min) |
| `persistence_dir` | `str` | None | 会话持久化目录 |

| 方法 | 说明 |
|------|------|
| `get_or_create(user_id)` | 获取或创建会话，超时自动清空。**mtime 重校验**：缓存命中时对比会话文件的 `st_mtime_ns` 与 `_disk_mtime` 记录，外部删改（WebUI 面板「清除临时会话」）优先于内存缓存——被删除的会话不会带着旧上下文复活 |
| `get(user_id)` | 获取会话 (不创建，纯缓存查询) |
| `message_count(user_id)` | 用户上下文消息数（无会话返回 0；用于建议将长临时会话升级为特殊会话） |
| `update(user_id, session)` | 更新会话并持久化到 JSON 文件 |
| `clear_context(user_id)` | 清空用户上下文 |
| `delete(user_id)` | 删除用户会话（同时清理 `_disk_mtime` 记录与磁盘文件） |
| `active_count()` | 活跃会话数量 |
| `cleanup_expired()` | 清理所有过期会话 |

> 📝 **跨进程一致性（commit f9c36c7）**：WebUI 面板与 bot 是独立进程，面板直接操作磁盘。`_save_to_disk()` 写回前若发现文件已被外部删除（请求处理中途面板清了会话），则跳过写回并失效缓存，下一次消息从全新会话开始。持久化失败不再静默，会记录 warning 日志。

#### 类: `SpecialSessionManager` (v2.13)

管理持久化的「特殊会话」——按角色限制数量（管理员 10 / 会员 3 / 普通 1），百万 token 上下文窗口，快照 + 增量双层存储。

| 构造参数 | 类型 | 默认值 | 说明 |
|----------|------|--------|------|
| `user_data_root` | `str` | (必填) | 用户数据根目录 |
| `max_per_user` | `int` | 3 | 每用户最大会话数（默认值，实际按角色：管理员 10 / 会员 3 / 普通 1） |
| `llm_client` | `DeepSeekClient` | None | LLM 客户端 (用于自动命名) |

所有会话操作**按名称寻址**（非列表索引）；删除的二次确认码由 `agent_router` 在命令层实现，不在本类中。无内存缓存——每次操作直接读盘（面板/bot 跨进程天然一致）。

| 方法 | 说明 |
|------|------|
| `create(user_id, name=None)` | 创建特殊会话。name 为 None 时生成规则临时名 `{MMDD}_{HHMM}`，首次交互后 LLM 异步命名；重名自动追加 `_N` 后缀。按角色的数量上限（10/3/1）由 `agent_router._handle_session_command()` 在上游强制，`create()` 本身不检查 |
| `get_active(user_id)` | 获取用户当前激活的特殊会话 (同一时间仅一个激活，读 `_index.json`) |
| `get_by_name(user_id, name)` | 按名称获取会话 |
| `list_sessions(user_id)` | 列出用户所有特殊会话 (索引条目: 名称/创建时间/消息数) |
| `switch_to(user_id, name)` | 切换到指定名称的会话；不存在时抛 `ValueError`（附可用会话列表） |
| `rename(user_id, old_name, new_name)` | 重命名会话（新名已存在则抛 `ValueError`） |
| `delete(user_id, name)` | 删除会话及其名下工作区文件（`repos/` 记录但保留；文件删除幂等），返回 `{"deleted", "freed_bytes", "kept_repos"}` 摘要 |
| `end_active(user_id)` | 退出特殊会话，回到临时模式 |
| `clear_context(user_id)` | 清空激活会话的消息历史但保留会话（`/clear` 用；删除磁盘 snapshot+delta 并重置计数） |
| `add_message(user_id, role, content, reasoning_content=None)` | 追加消息: 写入 JSONL 增量日志，每 50 条 (`SNAPSHOT_INTERVAL`) 触发快照并清空 delta |
| `add_file(user_id, name, file_path)` / `get_files(user_id, name)` | 记录/查询会话名下的工作区文件（文件溯源，供 delete 清理） |
| `auto_name(user_id, first_message, first_response)` | 异步: LLM 根据首次对话内容自动总结会话名（≤12 字符），成功则 rename |

**存储结构**:
```
{USER_DATA_ROOT}/{safe_id}/sessions/
├── _index.json              # 会话索引 + active_session（按名称）
└── {session_name}/
    ├── snapshot_00050.json  # 第 50 条消息时的完整快照（滚动清理旧快照）
    ├── snapshot_00100.json  # 第 100 条消息时的快照
    └── delta.jsonl          # 上次快照以来的增量消息
```

### 2.4 `agent/memory.py` — 长期记忆系统

同文件包含两套引擎：旧版 `MemorySystem`（Markdown 文件）与 P1 起的三层记忆引擎 `TieredMemory`。

#### 类: `MemoryEntry`

| 属性 | 类型 | 说明 |
|------|------|------|
| `name` | `str` | 记忆名称 (用作文件名) |
| `description` | `str` | 简短描述 (用于搜索匹配) |
| `type` | `str` | 类型: user/knowledge/system（`user` 类按用户隔离存储） |
| `content` | `str` | 完整 Markdown 正文 |
| `created_at` | `float` | 创建时间戳 |

#### 类: `MemorySystem`

Markdown 文件 + frontmatter 存储，`MEMORY.md` 索引。**无内存缓存**——每次操作直接读盘，因此 WebUI 面板的记忆编辑与 bot 天然一致。P1 起 user 类记忆已迁移至 `TieredMemory`，`MemorySystem` 仍服务 knowledge/system 类记忆与面板「记忆与画像」页的查看/编辑。

| 方法 | 说明 |
|------|------|
| `save(entry)` | 保存 MemoryEntry 为 `{base_dir}/{type}/{name}.md`（user 类为 `{base_dir}/user/{uid}/{name}.md`） |
| `recall(name, type)` | 按名称和类型读取记忆 |
| `forget(name, type)` | 删除记忆文件 |
| `search(query)` | 关键词子串搜索: 返回**全部**命中（无条数上限），按 description+content 匹配度排序 |
| `list_all(type)` | 列出指定类型的所有记忆 |

#### 类: `TieredMemory` — 三层记忆引擎 (P1)

频次驱动的用户事实记忆状态机：**SHORT**（近期原文候选）→ **MEDIUM**（按频次强化的要点，唯一注入提示词的层）→ **LONG**（长期对象快照；P1 仅创建+持久化，查询/RAG 留待 P2）。所有方法为同步实现（asyncio 单线程下无需锁），与 `MemorySystem` 并存。

**数据结构**（`dataclass`）：

| 类 | 字段 | 说明 |
|---|---|---|
| `ShortItem` | `id, content, count` | count 为 1 或 2，达 3 晋升 MEDIUM |
| `MediumItem` | `id, content, count, entry_extraction_index` | count≥3；age = 逻辑时钟 − entry_extraction_index |
| `LongObject` | `id, title, summary, snapshot_turns, snapshot_time, query_count, query_log, linked_medium_id` | 长期对象；晋升时保留 MEDIUM 孪生条目（Decision G），创建幂等 |
| `UserTierState` | `user_id, extraction_count, short, medium, long` | 每用户容器 + 逻辑时钟 |

**常量**（`memory.py:324-337`）：

| 常量 | 值 | 含义 |
|---|---|---|
| `SHORT_MAX` | 50 | SHORT 容量；溢出淘汰队尾（最旧且低 count） |
| `SHORT_PROMOTE_AT_COUNT` | 3 | count≥3 晋升 MEDIUM |
| `SHORT_PRIORITY_STEP` | 10 | reinforce 时将条目向队首移动的步长 |
| `MEDIUM_LONG_THRESHOLD` | 10 | count>10 晋升 LONG（创建快照，MEDIUM 孪生保留） |
| `MEDIUM_DOWNGRADE_AGE` | 30 | age>30 降级回 SHORT |
| `MEDIUM_DOWNGRADE_COUNT_RESET` | 2 | 降级时 count 重置为 2（再被提及一次即回归 MEDIUM） |
| `MEDIUM_MAX` | 60 | MEDIUM 容量安全网（超出按 count 淘汰最低者） |
| `MEDIUM_INJECT_TOP_N` | 15 | 注入条数 = 12 条按 count 最高 + 3 个最近晋升名额（`MEDIUM_INJECT_RECENT_SLOTS`） |
| `MEDIUM_INJECT_TOKEN_CAP` | 600 | 注入文本字符上限 |
| `LOW_CONFIDENCE_THRESHOLD` | 5 | count<5 的条目注入时标注「(低置信)」 |
| `JUDGE_MEDIUM_LIST_CAP` | 40 | judge 提示词中嵌入的 MEDIUM 列表上限（SHORT 全量嵌入） |
| `TIER_SCHEMA_VERSION` | 1 | 存储 schema 版本 |

**方法**：

| 方法 | 说明 |
|------|------|
| `touch_extraction_count(user_id)` | 逻辑时钟 +1（每次成功的 `extract_batch` 调用一次），返回新值 |
| `get_judge_list(user_id)` | 生成嵌入提取提示词的已有记忆清单（≤40 条 MEDIUM + 全部 SHORT），供 LLM judge 归类 |
| `ingest_candidates(user_id, candidates, snapshot_turns=None)` | 按 judge 结果落库（candidates 为 `(content, judge, matched_id)` 元组或 dict，judge ∈ new/reinforce_short/update/keep）：`new` → 入 SHORT 队首（count=1，逐字重复自动转为 reinforce）；`reinforce_short` → SHORT count+1 并向队首前移 10 位，达 3 晋升 MEDIUM；`update` → 以 LLM 精修内容替换 MEDIUM 并强化；`keep` → MEDIUM count+1；无效 matched_id 回退为 new。随后执行降级扫描（age 超期 → SHORT）与 LONG 晋升；`snapshot_turns`（本批 K 轮对话）存入本次创建的 LONG 对象。返回观测摘要 dict |
| `build_medium_injection(user_id)` | 生成注入系统提示词的 MEDIUM 摘要文本（top-N + 最近晋升 + 字符上限 + 低置信标注；空态返回空串） |
| `save_user(user_id)` / `load_user(user_id)` / `load_all()` | 原子写/读 `tiers/{uid}.json`；LONG 快照另存 `tiers/long/{uid}/{id}.md`（write-once）；启动时 `load_all()` 全量载入 `_states` |

**存储结构**:
```
data/memory/
├── tiers/
│   ├── {uid}.json           # UserTierState 全量（原子写: tmp + replace）
│   └── long/{uid}/{id}.md   # LONG 对象快照（write-once，含 snapshot_turns）
└── *.md                     # 旧版 MemorySystem 文件（knowledge/system 类仍在使用）
```

> ⚠️ **面板一致性预留**：`TieredMemory._states` 为内存缓存且 `save_user()` 全量写回，与已修复的 profile/session 是同一模式。当前面板只编辑 `MemorySystem` 的 Markdown（安全）；**若未来给面板加三层记忆编辑器，必须先给 `_states` 加 mtime 重校验**（参照 `profile.py` / `session.py`，commit f9c36c7）。

### 2.5 `agent/profile.py` — 用户画像

#### 类: `UserProfile`

| 属性 | 类型 | 说明 |
|------|------|------|
| `user_id` | `str` | QQ 用户 ID |
| `nickname` | `str` | 用户称呼 (可选) |
| `preferences` | `Dict[str, str]` | 偏好设置 (如 response_style) |
| `facts` | `List[str]` | **P0 起休眠**：不再提取、不再注入提示词（存量已由 `scripts/cleanup_profile_facts.py` 清理）；自由文本事实由 `TieredMemory` 三层引擎接管，画像只保留类型化槽位 |
| `interests` | `List[str]` | 兴趣话题 |
| `first_seen` | `float` | 首次交互时间戳 |
| `last_seen` | `float` | 最近交互时间戳 |
| `total_interactions` | `int` | 总交互次数 |

| 方法 | 说明 |
|------|------|
| `to_prompt_context()` | 生成注入系统提示词的画像上下文文本（仅 nickname/interests/preferences） |
| `touch()` | 更新 last_seen 并递增 total_interactions |
| `merge_facts(new_facts)` | （遗留）合并事实，词重叠 >60% 视为重复，上限 `MAX_FACTS=40` |
| `merge_interests(new_interests)` | 合并兴趣 (大小写不敏感去重) |
| `merge_preferences(new_prefs)` | 合并偏好字典 |
| `to_dict()` / `from_dict(data)` | JSON 序列化 / 反序列化 |

#### 类: `ProfileManager` — 三层提取流水线

**Layer 1 — LLM 提取**：石蕊测试（litmus test：「没有这条信息，回复会变差吗？」）+ few-shot 反例（截图/助手解读 ≠ 用户事实）+ 第三人称 `<conversation>` 块，单次 flash 调用合并完成画像提取与三层记忆 judge（`extract_batch`）。
**Layer 2 — 确定性过滤**：`fact_filter` 在 LLM 输出后做规则过滤（复合词/正则/结构规则），拦截幻觉与垃圾候选。
**Layer 3 — 批量调度**：`observe_turn()` 缓冲对话轮，攒够 `PROFILE_BATCH_K=5` 轮触发一次后台提取（单飞 `_inflight` 防并发），提取窗口 `EXTRACT_WINDOW_N=8` 轮（5 批 + 3 前置上下文）、总字符上限 `EXTRACT_TOTAL_CHAR_CAP=6000`。

| 方法 | 说明 |
|------|------|
| `get(user_id)` | 获取或创建用户画像。**mtime 重校验**（commit f9c36c7）：缓存命中时对比 `profile.json` 的 `st_mtime_ns`，面板外部编辑优先于内存缓存，不会被 bot 下次保存静默回滚 |
| `save(profile)` | 持久化画像到 `{USER_DATA_ROOT}/{safe_id}/profile.json` 并刷新 mtime 记录；失败记录 warning（不再静默） |
| `set_client(client)` / `set_memory(memory)` | 设置 flash LLM 客户端 / `TieredMemory` 实例（懒初始化） |
| `observe_turn(user_id, msg, response)` | 运行时主路径：缓冲一轮对话，达 K 轮后台触发 `_flush` |
| `flush_on_session_end(user_id, turn_count)` | delete-flush（Decision J）：会话被删除时，实质会话（>`DELETE_FLUSH_MIN_TURNS=5` 轮）强制冲刷尾批，短暂会话丢弃未提取的尾部 |
| `extract_batch(user_id, turns)` | 单次合并的 extract+judge flash 调用；解析失败跳过时钟，空 `{}` 仍推进 `extraction_count`（Decision 4）；**LLM await 之后重新 `get()`** 以拾取调用期间落盘的面板编辑，再同步原子应用（`_apply_extraction` → `save` → `TieredMemory.ingest_candidates`/`touch_extraction_count`/`save_user`） |
| `extract_and_update(user_id, msg, response)` | 单轮便捷包装（向后兼容/测试用）；运行时路径为 `observe_turn` 批量调度 |
| `_route_memory_candidates(...)` | 将提取结果中记忆类候选路由给 `TieredMemory`，画像类字段留给 `UserProfile` |

**提取规则**:
- 石蕊测试：只保留会影响回复质量的用户信息；截图内容、助手自己的话不算用户事实
- 只提取用户信息，不提取助手 (Roxy) 的相关信息
- 无新信息时返回空 `{}`（仍推进逻辑时钟）
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
| `recall_search_result` (仅本人存档) | ✅ | ✅ | ✅ |
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
| `/结束会话` 或 `/退出特殊会话` 或 `/退出会话` 或 `/临时会话` | 退出特殊会话模式，回到临时会话 |
| `/帮助` 或 `/help` 或 `/命令` | 显示完整系统命令列表（读取 `HELP.md`） |
| `/personality [名称]` 或 `/人格切换 [名称]` | 查看/切换人格（assistant / roxy_character / rubi） |
| `/toggle [功能] [on/off]` | 群聊功能开关（仅超级用户，仅群聊；gacha / image / voice） |
| `/兑换码` 或 `/redeem-code` | 查询当前有效游戏兑换码（零 token，不经智能体） |
| `/角色详情 <名称>` | 查询角色详情并渲染卡片图片（零 token） |
| `/羁绊详情 <名称>` | 查询羁绊详情并渲染卡片图片（零 token） |
| `/刷新角色数据` | 仅管理员：强制后台刷新角色/羁绊数据库 |
| `/功能` 或 `/features` | 渲染 `FEATURES.md` 为功能卡片图片（零 token） |
| `/更新日志 [d]` 或 `/update record [d]`（亦支持 `#更新日志`、`/更新记录`、`/update log`、`/changelog`） | 查询机器人最近 d 条更新记录（默认 3，最多 20；零 token，读 `data/changelog/changelog.json`） |
| `/管理工作区` | 引导智能体调用 `get_user_info` 展示工作区快照（落入 Agent 处理，非拦截命令） |
| `/取消` 或 `#取消` 或 `/结束` 或 `#结束` | 退出群聊连续对话模式（四个写法等价） |
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
| `_continuous_sessions` | `ContinuousSessionManager` 实例。管理群聊连续对话窗口 (90秒免@)。 |

#### 连续对话模式 (Continuous Mode)

群聊中，用户首次 @机器人 后自动开启 90 秒「免 @」窗口。窗口内用户的所有消息都会被 Agent 处理，无需反复 @mention。

| 处理器 | 优先级 | 触发条件 | 说明 |
|--------|--------|----------|------|
| `agent_router` | 1 | `to_me()` | 正常的 @机器人 入口，处理后自动开启连续窗口 |
| `continuous_router` | 2 | 无规则 | 仅处理 `GroupMessageEvent` + 连续窗口内的用户消息 |

**窗口生命周期**:
1. **开启**: `agent_router` 处理完 @消息 后自动调用 `_continuous_sessions.start(group_id, user_id)`
2. **续期**: `continuous_router` 每条消息调用 `touch()` → 重置为完整 90 秒
3. **取消**: 用户发送 `/取消` / `#取消` / `/结束` / `#结束` → 窗口关闭
4. **超时**: 90 秒无消息 → `is_active()` 自动清理过期窗口

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

#### 已注册工具 (31 个)

**内置工具 (13 个)**:

| 工具名 | 来源 | 说明 |
|--------|------|------|
| `get_time` | builtin_tools | 获取当前日期和时间 (含中文星期) |
| `search_web` | builtin_tools | SearXNG 聚合搜索 — 新闻/百科/知识查询 |
| `web_fetch` | builtin_tools | 直接抓取 HTTPS 网页内容并提取纯文本 |
| `execute_code` | builtin_tools | 执行 Python 代码，自动捕获并发送生成的图表 |
| `shell_exec` | builtin_tools | 执行只读 shell 命令（白名单+管道，40+命令） |
| `download_repo` | builtin_tools | Git clone 代码仓库 (HTTPS only, 命令注入防护) |
| `summarize_pdf` | builtin_tools | 提取并总结 PDF 内容 (PyPDF2, 路径验证) |
| `get_system_load` | builtin_tools | 获取服务器实时系统负载 (CPU/内存/磁盘, 用于任务预检) |
| `get_user_info` | agent_router | 获取当前用户系统信息快照 (权限/会话/工作区/工具范围, 零 token 消耗) |
| `delete_workspace_file` | builtin_tools | 删除工作区文件/空目录 (释放磁盘配额) |
| `read_file` | file_tools | 读取用户上传的文件 (文本/PDF/图片/音频, 图片和音频可 AI 分析) |
| `recall_search_result` | agent_router → search_archive | 按存档 id 取回 search_web/web_fetch 的完整结果 (7天保留, per-user 隔离) |
| `get_changelog` | agent_router → changelog | 查询机器人自身的更新记录 (自然语言通道, 与 `/更新日志` 命令共用 `tools/changelog.py`) |

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

**游戏工具 (4 个)**:

| 工具名 | 来源 | 说明 |
|--------|------|------|
| `redeem_code` | agent_router → check_redeem_code | 游戏兑换码查询 (自动爬取+缓存) |
| `character_detail` | character_detail | 角色详情查询 (面板/技能/倍率/属性/天赋) |
| `bond_detail` | bond_detail | 羁绊详情查询 (类别/星级/技能/获取方式) |
| `parse_battle_screenshots` | battle_parser | 团战截图 OCR 解析 (轻量/全量两种模式) |

**会话编排 (3 个)**:

| 工具名 | 来源 | 说明 |
|--------|------|------|
| `begin_task` | agent_router | 标记多轮工具型子任务起点 (抽卡/测速)，折叠问答避免污染上下文 |
| `finalize_subtask` | agent_router | 结束子任务并提交结构化结果 (详情归档到 `data/task_log/{uid}.jsonl`) |
| `end_continuous_mode` | agent_router | 智能体主动结束群聊连续对话窗口 (用户表达告别意图时) |

**注**: `check_weather` 已移除。天气查询通过专用的 `get_weather` 工具（高德地图 API）实现。`web_fetch` 用于直接抓取搜索结果中无法索引的网页。`search_web` / `web_fetch` 的完整结果会存档到 `data/search_cache/{uid}/{id}.json`（`agent/search_archive.py`，7 天 TTL + 每用户 50 条上限，save 时惰性清扫），返回文本末尾附 `[存档] id: xxx` 行；上下文折叠/压缩后模型可经 `recall_search_result` 按 id 取回全文而无需重搜。存档 id 只随 append-only 的工具消息流转、不进系统提示词，与 v2.26 的前缀缓存优化不冲突。

**权限分布**: 31 个工具中 24 个为公共工具（`_PUBLIC_TOOLS`），4 个为会员工具（`web_fetch` / `download_repo` / `get_system_load` / `execute_code`），1 个为管理员工具（`shell_exec`）；`end_continuous_mode` 不固定归属，由连续对话上下文动态并入。注册分布：26 个在 `_build_tool_registry()`，其余 5 个（`get_user_info` / `end_continuous_mode` / `begin_task` / `finalize_subtask` / `recall_search_result`）在模块级单独注册。

#### 配置看门狗（热重载）

`_start_config_watcher()`（`agent_router.py:1007`）后台任务每 5 秒轮询三组文件的 mtime（各组独立 5 秒冷却），配合 WebUI 面板实现保存即生效、无需重启：

| 监听对象 | 变化时的动作 |
|---|---|
| `agent/config/*.md`（含 `personalities/`） | `reload_configs()` 重建系统提示词（只有 SOUL/IDENTITY/AGENTS/MEMORY 四个注入文件会即时改变行为；HELP/FEATURES 在下次命令触发时生效） |
| `config/characters/character_dic.json` / `bonds_search_dic.json` | `NameResolver.reload()` + 角色/羁绊详情缓存失效（面板「Wiki 缓存」页可编辑别名） |
| `config/models_settings.json` | `model_router.reload()`（含模块级单例与 `MultimodalClient`）+ 重挂 3 处固化 client 引用（`agent.client` / `_profile_manager.set_client` / `_special_sessions.set_client`），约 10 秒生效 |

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
| `reload()` | 重读 `models_settings.json` 并原地重建三个 client + `task_routing`，清除 `_default` 缓存。由配置看门狗调用（面板保存模型配置后约 10 秒生效）；client 仅是 key/base/model 载体（`httpx.AsyncClient` 每请求新建），属性替换原子，最坏当次在途请求用旧配置 |

`MultimodalClient`（`lib/multimodal_client.py`）同样提供 `reload()`——其 API 方法均为调用时读取 `self._config`，重读配置即生效。

**配置来源**: `QQBot/config/models_settings.json` (git-ignored)。每个模型字段留空时自动回退到 `.env` 的 DeepSeek 默认配置。可在 WebUI 面板「配置热管理 → 模型」以结构化表单编辑，保存前经真实 API 探测验证（见 §11.6）。

**全局实例**: `model_router` — 模块级单例（tools 中经函数内 lazy import 使用，reload 后自动跟随）。

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

**第 3 层 — 资源限制**: 分级超时与输出上限（管理员 60s/100KB，会员 15s/50KB），执行后自动清理临时目录

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
| 最大工具调用轮数 | 20 |
| 单次消息处理总超时 | 300 秒 |
| LLM 思考超时 | 180 秒 |
| 响应消息长度 | 2000 字符 (智能拆分为 300 字符片段, 1s 发送间隔) |
| 会话生命周期 | 30 分钟无活动后过期 |

---

## 四、配置文件详解

所有 Markdown 配置文件位于 `agent/config/`，共 12 个 + `personalities/` 人格目录。另外 `config/` 目录下有 JSON 配置文件，运行时敏感配置（密钥 / 服务地址）在 `.env`。

### Markdown 配置文件 (12 个)

按代码实际用途分为四类（详见 README「智能体配置」）：

| 文件 | 加载方式 | 用途 |
|------|------|------|
| `SOUL.md` | ① 注入系统提示词 | 人格定义: 角色 Roxy、沟通风格、行为规则、决策框架 |
| `IDENTITY.md` | ① 注入系统提示词 | 身份声明: 名称/版本/技术栈/能力列表/安全模型/联系方式 |
| `AGENTS.md` | ① 注入系统提示词 | 编排规则: Think→Act→Observe→Respond 循环、工具选择标准、错误处理 |
| `MEMORY.md` | ① 注入系统提示词 | 三层记忆引擎规则（P1 起接入提示词） |
| `TOOLS.md` | ② 加载但不注入 | 工具文档参考: 全部工具的功能/参数/使用场景 |
| `BOOTSTRAP.md` | ② 加载但不注入 | 启动序列: 初始化步骤、健康检查规则 |
| `SESSION.md` | ② 加载但不注入 | 会话配置: 最大消息数、超时时间、最大工具调用次数 |
| `HELP.md` | ③ 命令时读取 | `/帮助` 命令输出内容 |
| `FEATURES.md` | ③ 命令时读取 | 功能一览（`/功能` 命令渲染为卡片图片展示） |
| `WORKSPACE.md` | ④ 未加载（遗留） | 工作区约束文档——其硬件约束已由 `HardwareDetector` 动态检测取代 |
| `USER.md` / `HEARTBEAT.md` | ④ 未加载（遗留） | 历史遗留文件，代码中无引用 |
| `personalities/` | 按需加载 | 人格定义（assistant / roxy_character / rubi），由 `personality.py` 加载 |

①② 类共 7 个文件由 `reload_configs()` 读取，配置看门狗监听全部 `*.md` 的 mtime（5 秒轮询）实现热重载。

### JSON 配置文件（在 `config/` 目录）

| 文件 | Git | 用途 |
|------|-----|------|
| `models_settings.json` | 忽略 | 多模型配置 (REASONING/FLASH/MULTIMODAL/AUDIO/OCR)，含 API 密钥；面板可结构化编辑，保存后热重载（§11.6） |
| `models_settings_example.json` | 跟踪 | 多模型配置示例模板，供新用户参考填写 |
| `multimodal.json` | 忽略 | [已废弃] 旧版多模态配置，已被 models_settings.json 取代 |
| `gacha_data.json` | 跟踪 | 抽卡数据（角色/羁绊/概率，由 pullingMonitor 加载） |
| `characters/character_dic.json` / `characters/bonds_search_dic.json` | 跟踪 | 角色/羁绊别名字典（NameResolver 数据源）；面板「Wiki 缓存」页可编辑，watcher 热重载 |

### 环境变量（`.env`）

`.env` 是运行时敏感配置（密钥 / 服务地址），逐行说明见 **`QQBot/.env.example`**（可直接 `cp .env.example .env`）。模型凭据遵循「`models_settings.json` 优先、`.env` 回退」：某模型段在 JSON 中填了 `api_key`/`api_base` 即覆盖 `.env` 的 `DEEPSEEK_API_KEY`/`DEEPSEEK_API_BASE`；仅 JSON 段留空时才回退到 `.env` 的 DeepSeek 配置。

非模型关键变量包括 `ONEBOT_ACCESS_TOKEN`、`SUPERUSERS`/`VIP_USERS`、`AMAP_API_KEY`、`USER_DATA_ROOT`、`MAX_SPECIAL_SESSIONS`、`USER_WORKSPACE_QUOTA_MB`、`BUFF_DETECTOR_WORKERS`（团战测速 buff 图标匹配并行线程数，默认 2）。

---

## 五、工具实现

### 5.1 `tools/builtin_tools.py` — 内置工具 (9 个)

| 函数 | 说明 | 实现方式 |
|------|------|----------|
| `get_time()` | 返回当前日期时间 (含中文星期) | `datetime.now().strftime` |
| `search_web(query, num_results=5)` | SearXNG 聚合搜索 (新闻/百科/知识) | `urllib.request` → SearXNG JSON API (`/search?format=json`)，15s 超时，安全搜索开启，中文优先 |
| `web_fetch(url)` | 异步，抓取 HTTPS 网页并提取纯文本 | `httpx` → HTML→文本转换 (`html.parser`)，HTTPS only，2MB/8000字符/30s 限制 |
| `recall_search_result(archive_id)` | 按 id 取回 search_web/web_fetch 的完整存档 | `agent/search_archive.py` → `data/search_cache/{uid}/{id}.json`；id 严格校验（12位hex，防路径穿越），按 contextvar user_id 作用域，过期/跨用户返回未找到；输出头带存档时间与距今小时数 |
| `execute_code(code, timeout=30)` | 异步，执行 Python 代码 + 自动发送图表 | `subprocess.run` (独立 tmpdir)，扫描 .png/.svg 等图片 → 拷贝到 output/ → QQ 发送 |
| `shell_exec(command, timeout=15)` | 异步，执行只读 shell 命令 (白名单+管道) | `subprocess.run(["bash", "-c", cmd])`，40+ 白名单命令，管道解析验证，危险字符拦截 |
| `download_repo(repo_url)` | Git clone 仓库 (HTTPS only) | `subprocess.run(["git", "clone", url, path])`，已存在则 pull，120s 超时 |
| `summarize_pdf(file_path)` | 提取 PDF 文本 (前 8000 字符) | PyPDF2 → 逐页提取 → 截断，路径验证 |
| `get_system_load()` | 获取服务器实时负载 (CPU/内存/磁盘) | 读取 `/proc/loadavg`, `free`, `df` → 返回格式化评估 (低/中/高负载) |
| `delete_workspace_file(path)` | 删除工作区文件/空目录 (释放磁盘) | 相对工作区根目录解析，拒绝非空目录与隐藏文件 |

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

### 5.6 `tools/character_detail.py` / `tools/bond_detail.py` — 角色 / 羁绊查询

数据来源于 Wiki 爬虫 (`tools/wiki_scraper.py`) 缓存的 `data/wiki_cache/character_details.json`，通过 `tools/name_resolver.py` 做角色别名模糊匹配，`tools/card_renderer.py` 渲染卡片图片。

| 函数 | 说明 |
|------|------|
| `character_detail(character_name)` | 查询角色详情：面板成长系数、技能、倍率、属性、天赋、潜能 |
| `bond_detail(bond_name)` | 查询羁绊详情：类别、星级、攻击/生命、羁绊技能、获取方式、出售价格、经验值、上线时间 |

同时提供直接命令 `/角色详情 <名称>`、`/羁绊详情 <名称>`（不经智能体，零 token 消耗）。

### 5.7 `tools/battle_parser.py` — 团战截图测速 (OCR + 行动值修正)

`parse_battle_screenshots(paths, mode)` 是团战截图测速的入口：对 1-2 张战斗截图（跑条前 + 跑条后）执行 OCR，提取角色名、行动值、阵营，并对行动值做修正后返回 `calculate_speed` 兼容格式。

- **两种模式** (`mode` 参数)：
  - `light` — 轻量：仅提取角色名与行动值（约 10s），跳过技能解析与行动值修正
  - `full` — 全量：完整流程（技能解析 + 行动值修正，约 90s），默认
- **OCR 管线** (`lib/ocr_engine.py`)：EasyOCR 提取文本 + `BuffDetector` 用 `cv2.matchTemplate`（多尺度 TM_CCOEFF_NORMED）匹配 buff/debuff 图标，`BUFF_DETECTOR_WORKERS` 控制按行并行匹配的线程数。
- **buff 图标来源**：手工图标 `images/cal-speed-data/` + Wiki 爬取的状态图标（`lib/status_icons.STATUS_ICON_CN` 维护文件名 → 中文标签）。
- **buff 模板集裁剪**：`lib/buff_alias.py` 从角色技能文案（`(x回合)` / `(x turns)` 时长标记）提取 buff 名并归一化到图标标签，使每个角色只在其「可能出现的 buff」子集内匹配；未知角色仅该行回退全量。
- **技能分类与触发推断**：`tools/ag_skill_index.py`（Skill 类 + 阵营触发方式分类）、`tools/ag_trigger_engine.py`（行动值效果触发链推断）、`tools/ag_llm_resolver.py`（嵌套子技能条件的 L4 窄 LLM 回退）。
- 团战/镜像匹配时同一角色可能同时出现在我方和敌方（同名不同阵营），属正常情况。

### 5.8 `tools/changelog.py` — 更新记录 (纯 IO/格式化)

面向用户的「更新记录」读写与格式化模块，无 nonebot 依赖，被三处共用：`/更新日志` 命令拦截、`get_changelog` LLM 工具、群发轮询器。数据存 `data/changelog/changelog.json`（结构 `{entries: [{version, date, changes: [{type, text}], created_at, broadcast_at}]}`，数组新→旧）。

- **变更类型** `CHANGE_TYPES = ["新增", "修复", "优化", "调整", "移除"]`；面板下拉选择。
- **读取**：`load_entries()` / `get_recent(d=3)`（clamp 1..20，按 `created_at` 降序，不足返回全部）。
- **格式化**：`format_for_qq(entries, requested, site_url=None)` 生成 QQ 结构化纯文本（`【版本】 日期` 标题 + `  • 类型：内容` 条目，不含 markdown）；`format_announcement(entry, site_url=None)` 用于群发（加 `📢 更新公告` 头）。两者 `site_url` 非空时在末尾追加一行公开站点指路（`📖 使用说明与演示：<url>`，纯文本）。空数据返回「暂无更新记录」提示行。
- **图片卡片**：`card_renderer.render_changelog_card(entries, out_path=None, title="更新记录", site_url=None)` 复用 help/feature 卡骨架渲染暗色 PNG（版本 pill 循环色 + 变更类型 badge 色 + 像素宽换行），命令/群发在文本后 best-effort 追加发送；`entries` 空返回 `None`。
- **面板↔bot 通信**：`write_known_groups`/`read_known_groups`（群列表）、`claim_pending`（原子改名认领群发请求）/`finish_pending_claim`/`recover_stale_claim`（启动清理崩溃残留的 `.processing`）、`write_status`/`read_status`（群发结果回写）、`mark_broadcast`（仅当至少成功发出一群时回写 `broadcast_at`）。
- **原子写** `_atomic_write_json`（tmp + `os.replace`）贯穿所有写操作。
- 详细的面板/后台任务/文件队列设计见 §十二「更新记录 / 群发」。

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
       ├── 特殊命令 → 会话管理 / 反馈 / 元命令 (直接处理，不经过 Agent)
       │
       ├── session_type 检测 → active_special ? "special" : "temporary"
       │
       └── 自然语言 → Agent.run(message, user_id, session_type=session_type)
                         │
                         ├── _current_user_workspace.set(workspace_path) (工作区隔离)
                         ├── _build_messages() 四层分级 (§1.2):
                         │     L1 SOUL+IDENTITY+AGENTS+MEMORY+硬件 (全局共享)
                         │     L2 人格/群限制  L3 工作区/权限
                         │     L4 尾部: 时间 + 画像(ProfileManager.get →
                         │        to_prompt_context) + MEDIUM 记忆注入
                         │        (TieredMemory.build_medium_injection)
                         │
                         └── Think→Act→Observe→Respond Loop (max 20)
                              │
                              ├── chat_completion_with_tools(messages, tools)
                              ├── has tool_calls?
                              │   YES → ToolRegistry.execute() → append tool result
                              │   NO  → return final content
                              │
                              └── Post-processing:
                                   ├── Session.update() + trim (迟滞修剪)
                                   └── _schedule_profile_update()
                                        → ProfileManager.observe_turn() 缓冲,
                                          每 5 轮后台 extract_batch (extract+judge
                                          → 画像 + TieredMemory.ingest_candidates)
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

天气通过专用工具 `get_weather`（高德地图 API）处理，支持实时天气和 4 天预报。无需通过搜索获取天气信息。

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

`test_agent.py` 包含 **15 个测试套件, 105 个测试用例**（全部离线，使用 mock client，无网络依赖），覆盖所有核心组件:

| # | 测试套件 | 测试数 | 覆盖内容 |
|---|----------|--------|----------|
| 1 | `TestToolRegistry` | 7 | 注册/列表/Schema/同步执行/异步执行/错误/移除/包含 |
| 2 | `TestSessionManager` | 7 | 创建/超时/裁剪/清空/删除/持久化/**外部删除生效**（面板清除临时会话不被缓存复活，含请求中途删除跳过写回） |
| 3 | `TestMemorySystem` | 4 | 保存/读取/遗忘/搜索/列出 |
| 4 | `TestUserProfile` | 7 | 创建/保存/空上下文/完整上下文/去重/持久化/**外部编辑优先**（面板改画像后 bot 缓存按 mtime 重读） |
| 5 | `TestProfileExtraction` | 11 | Layer 2 确定性过滤（复合词/正则/结构规则）/提示词约束/对话窗口格式化/批量调度（observe_turn 达 K 冲刷、单飞、delete-flush） |
| 6 | `TestAgentCore` | 11 | 启动/提示词构建/纯文本/工具循环/会话持久化/清空/最大迭代/压缩回退/任务折叠/画像注入/MEDIUM 记忆注入 |
| 7 | `TestDeepSeekClientParsing` | 3 | 解析纯文本/工具调用/混合响应 |
| 8 | `TestBuiltinTools` | 4 | get_time/execute_code(成功/错误)/search_web |
| 9 | `TestSearchArchive` | 7 | 存档 round-trip/用户隔离/路径穿越拒绝/TTL 过期（recall 拒绝+惰性清扫）/每用户上限/无上下文降级/helper 全链路（footer 携带可召回 id） |
| 10 | `TestPersonality` | 4 | 人格优先级链/群人格模糊匹配/清除回退/歧义拒绝 |
| 11 | `TestTokenLedger` | 7 | usage 解析（DeepSeek/DashScope 格式、缺失、缓存钳制）/记账/按日聚合/摘要格式化 |
| 12 | `TestTieredMemory` | 20 | 三层状态机（入队/去重/强化前移/晋升/溢出淘汰/降级/安全网）/LONG 创建幂等+快照/judge 清单构成/注入规则（空态/低置信/top-N+最近晋升/字符上限）/逻辑时钟/持久化往返/全量加载 |
| 13 | `TestMergedExtraction` | 5 | 合并 extract+judge 单次调用（候选路由/L2 过滤/judge 清单嵌入提示词/时钟推进/reinforce 晋升） |
| 14 | `TestMemoryMigration` | 2 | 旧记忆 → 三层结构迁移（seed+归档、散落 legacy dump 归档） |
| 15 | `TestCacheStability` | 6 | 前缀缓存稳定性（提示词头部用户不变量/易变内容置尾/画像变化头部字节稳定/历史头部迟滞修剪/压缩边界步进/无时间戳） |

另有 `test_workspace.py`（11 个工作区/会话文件测试类）与 `test/` 下 8 个离线测试脚本（被 gitignore，仅存于开发机），由 `bash test.sh` 统一调度。

**运行方式**:
```bash
cd QQBot
python test_agent.py
# 或全量:
bash test.sh
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
9. **验证**: 发送 `@Roxy /status` 确认 29 个工具已注册、SearXNG 可连通
10. **配置多模型 (可选)**: 编辑 `QQBot/config/models_settings.json` 填入各模型的 API 信息，参考 `models_settings_example.json` 格式；或在 WebUI 面板「配置热管理 → 模型」结构化编辑（保存前自动探测 API 可用性，保存后约 10 秒热生效，见 §11.6）
11. **启动 Web 管理面板 (可选)**: `bash start_webui.sh`（绑定 `127.0.0.1:8090`，经 SSH 隧道访问）

### Docker 部署

```bash
docker compose up -d
```

包含 SearXNG + Napcat + NoneBot + vLLM 完整栈。

---

## 十一、多模型架构 (Multi-Model Architecture)

### 11.1 设计理念

参考 Claude Code 的模型分层策略，QQBot 使用多个不同能力的模型分工合作，以合理节约 token 消耗：

| 模型层级 | 用途 | 特点 |
|----------|------|------|
| **FLASH_MODEL** | 复杂度分类 + 简单对话 | 轻量、快速、低成本。处理问候/闲聊/常识问答 |
| **REASONING_MODEL** | 复杂推理 + 工具调用 | 强大、高能力。处理搜索/代码/多步推理/专业知识 |
| **MULTIMODAL_MODEL** | 图片理解 | 视觉能力。处理用户发送的图片 AI 分析 |
| **AUDIO_MODEL** | 音频/语音理解 | 语音转文字 + 情绪/声线分析 (qwen3-omni-flash) |
| **OCR_MODEL** | 战斗截图 OCR | 团战测速的文本提取 (qwen3.5-ocr) |

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
    "max_tokens": 409600, "temperature": 0.7
  },
  "FLASH_MODEL": {
    "api_key": "", "api_base": "", "model": "",
    "max_tokens": 102400, "temperature": 0.7
  },
  "MULTIMODAL_MODEL": {
    "api_key": "", "api_base": "", "model": "",
    "max_tokens": 20480, "temperature": 0.7
  },
  "AUDIO_MODEL": {
    "api_key": "", "api_base": "", "model": "",
    "max_tokens": 20480, "temperature": 0.7
  },
  "OCR_MODEL": {
    "api_key": "", "api_base": "", "model": "qwen3.5-ocr",
    "max_tokens": 2048, "temperature": 0.0
  },
  "task_routing": {
    "triage": "FLASH_MODEL",
    "simple_task": "FLASH_MODEL",
    "complex_task": "REASONING_MODEL",
    "image_analysis": "MULTIMODAL_MODEL",
    "audio_analysis": "AUDIO_MODEL",
    "triage_prompt": "请判断以下用户消息的复杂度..."
  }
}
```

（完整模板见 `config/models_settings_example.json`，含各段用途注释。）

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

### 11.6 面板化编辑、可用性探测与热重载 (2026-09, commit 55fad24)

WebUI「配置热管理 → 模型」提供 5 段结构化表单（REASONING/FLASH/MULTIMODAL/AUDIO/OCR），取代裸 JSON 编辑（原始 JSON 入口保留在折叠的「高级」区）。

**保存流程 = prepare → probe → commit**：

1. **prepare**（`webui/config_editor.py: models_section_prepare`）：白名单段名/字段类型校验，段落级密钥还原（前端留空的 api_key 以脱敏占位 `***` 提交，后端还原为磁盘旧值），生成替换该段后的全量 JSON。
2. **probe**（`webui/model_probe.py: probe_model`）：对改动过凭据的段做**最小真实调用**——`POST {api_base}/chat/completions`（URL 拼接与 `deepseek_client.py` 逐字一致），body `max_tokens=8`，Bearer 头，15s 超时。分类：200→ok；401/403→auth_failed；连接/DNS/超时→unreachable；404→bad_path；其他 4xx/5xx→rejected。**分级判定**：AUDIO/OCR 段对 400/422 宽判为 ok_with_warning（qwen-omni 强制 stream、OCR 要求图像输入，纯文本探测被拒属预期，凭据与端点仍有效）。api_key/api_base/model 三项与磁盘全同（只改 max_tokens/temperature）→ 跳过 probe。
3. **commit**：备份 `.bak` + 原子写整份 JSON。**probe 失败 → 400，不写盘**——错误配置不会落盘再等 bot 调用时才暴露。

**bot 侧热重载**：配置看门狗（§2.8）监听 `models_settings.json` mtime，变化时执行 `model_router.reload()`（原地重建 client + task_routing）与 `MultimodalClient.reload()`（重读配置，API 方法调用时取值即生效），并重挂 3 处固化的 client 引用（`agent.client` / `_profile_manager.set_client` / `_special_sessions.set_client`）；模块级单例 `model_router` 同步 reload，tools 中的 lazy import 自动跟随。保存后约 10 秒生效，无需重启 NoneBot。竞态安全：client 仅是 key/base/model 载体（`httpx.AsyncClient` 每请求新建），属性替换原子，最坏当次在途请求用旧配置。

**测试端点**：`POST /api/config/models/test` 单独探测某段（同样先还原脱敏密钥再 probe，脱敏占位符本身永不出网）。

---

## 十二、Web 管理面板 (WebUI)

位于仓库根 `webui/`，是与 bot **独立的 FastAPI 进程**（`start_webui.sh` 管理，默认绑定 `127.0.0.1:8090`，经 SSH 隧道访问；scrypt 口令哈希 + 内存会话鉴权）。入口 `webui/main.py`：71 个 `/api/` 端点 + 16 个页面路由 + 2 个 WebSocket；页面配置见 `PAGES` 列表。

| 页面 | 功能 |
|------|------|
| `/` 仪表盘 | 服务状态、硬件监控、快捷入口 |
| `/processes` 进程管理 | 启动/停止 bot，看门狗状态持久化（`process_manager.py`） |
| `/logs` 日志查看 | bot 日志实时尾部（WebSocket） |
| `/terminal` Web 终端 | 服务器 shell（WebSocket） |
| `/tokens` Token 消耗 | `lib/token_ledger.py` 数据源；缓存命中率分口径观测（按提供商/模型分离） |
| `/audit` 工具审计 | `data/audit/` JSONL 审计日志 + 面板自身操作审计（`audit.py`） |
| `/feedback` 用户反馈 | `#反馈`/`#bug`/`#建议` 提交的处理流 |
| `/sessions` 会话管理 | 临时会话查看/清除（写前备份到 `BACKUP_DIR/sessions`）、特殊会话查看 |
| `/tasks` 任务日志 | `data/task_log/{uid}.jsonl` 子任务记录浏览 |
| `/memory` 记忆与画像 | MemorySystem Markdown 编辑、三层记忆查看、画像 JSON 编辑 |
| `/workspace` 工作区磁盘 | 按用户配额/占用展示（角色感知） |
| `/config` 配置热管理 | 提示词 `*.md`、人格、`.env`、模型（结构化表单 + API 探测，§11.6） |
| `/playground` Playground | 面板内直接对话调试（进程内构造 client，无需 nonebot driver） |
| `/wiki` Wiki 缓存 | 角色/羁绊数据浏览、**别名字典编辑**（`character_dic.json` / `bonds_search_dic.json`） |
| `/changelog` 更新日志 | 编辑更新记录（版本/日期/分类变更条目，`data/changelog/changelog.json`）、勾选群聊**群发更新公告** |

### 更新记录 / 群发（`data/changelog/`）

面向用户的「更新记录」以结构化 JSON 存储，与开发者向的 `docs/更新日志.md`（git 提交整理）分开维护，面板是唯一编辑入口。面板与 bot 是两个进程，通过 `QQBot/data/changelog/` 下的共享文件通信（沿用 `redeem_code` 的文件队列范式）：

| 文件 | 写入方 | 读取方 | 用途 |
|------|--------|--------|------|
| `changelog.json` | 面板 | bot + 面板 | 更新记录主数据（唯一真源，数组新→旧） |
| `known_groups.json` | bot 后台任务 | 面板 | 群列表（群号+群名+成员数），供勾选 |
| `pending_broadcast.json` | 面板 | bot 轮询器 | 群发请求队列（entry_index + targets） |
| `broadcast_status.json` | bot 轮询器 | 面板 | 群发结果回写（供面板轮询显示进度） |

- **读写工具** `tools/changelog.py`（纯 IO/格式化，无 nonebot 依赖）：`load_entries`/`save_entries`/`get_recent(d)`（默认 3，clamp 1..20）/`format_for_qq`/`format_announcement`/`mark_broadcast`，命令拦截、`get_changelog` 工具、群发轮询器三方共用。
- **bot 后台任务**（`agent_router.py` `_start_changelog_tasks()`，`@get_driver().on_startup`）：① 群列表导出——启动 15s 后 + 每 600s 调 `bot.get_group_list()` 写 `known_groups.json`；② 群发轮询器——启动先 `recover_stale_claim()` 清理崩溃残留并把卡住的 `sending` 改写为中断；随后每 5s `claim_pending()`（原子改名为 `.processing` 认领）→ 解析 targets（`all` 现场枚举 / 指定群号）→ `_split_text` 分段 + `bot.send_group_msg` 逐群发送（群间 1s 限速）→ 回写 `broadcast_status.json`；仅当至少成功发出一群时才 `mark_broadcast`（全失败则状态置 `error`，不误标"已群发"）。
- **面板端点**：`GET/PUT /api/changelog`、`GET /api/changelog/groups`、`POST /api/changelog/broadcast`、`GET /api/changelog/broadcast/status`（`data_reader.changelog_*`，保存经 `config_editor.json_config_write` 原子写）。
- **公开站点同步**（`public_site/` → `bot.oneweblog.cn`）：面板每次保存成功（`changelog_save` 的 `result.ok` 分支）调 `data_reader._export_public_changelog(entries)`，把**脱敏**快照（仅 version/date/changes；剔除 created_at/broadcast_at/_class，绝不含群列表/用户数据）原子写 `<PUBLIC_SITE_DIR>/changelog.json`；nginx root 指向该目录即发布（示例见 `docs/bot-site-nginx.md`）。导出失败静默，不阻断保存。站点为纯静态只读单页（index.html/style.css/app.js），`app.js` fetch `./changelog.json` 渲染「更新记录」区，缺失/404 优雅降级显示「暂无更新记录」。
- **QQ 图片卡片 + 指路短链**：命令 `/更新日志` 与群发 `_do_broadcast` 在文本后 best-effort 追加 `render_changelog_card` 渲染的 PNG（群发在循环外渲染一次、逐群先发图再发文本；发卡失败降级为仅文本、不计入 failed）。指路短链 `_PUBLIC_SITE_URL`（env `BOT_PUBLIC_SITE_URL`，默认 `https://bot.oneweblog.cn`）经 `format_for_qq`/`format_announcement` 的 `site_url` 参数附到文本尾行，降低 /命令 使用门槛。

### 面板 ↔ bot 跨进程一致性

面板直接写盘，bot 是另一个进程——凡是 bot 侧持有**内存缓存且会整体写回**的数据，面板编辑都可能被陈旧缓存静默回滚。当前状态（2026-09 审计）：

| 数据 | bot 侧缓存 | 一致性机制 |
|------|-----------|-----------|
| 用户画像 `profile.json` | `ProfileManager._cache` | **mtime 重校验**（f9c36c7）：`get()` 命中时对比 `st_mtime_ns`，外部编辑优先；`extract_batch` 在 LLM await 后重新 `get()` |
| 临时会话 `data/sessions/` | `SessionManager._sessions` | **mtime 重校验**（f9c36c7）：外部删除优先；`_save_to_disk` 检测请求中途的外部删除并跳过写回 |
| 三层记忆 `tiers/*.json` | `TieredMemory._states` | ⚠️ 面板目前只读；**若将来加编辑器必须先补 mtime 校验**（同模式陷阱） |
| MemorySystem `.md` | 无缓存 | 每次操作直接读盘，天然一致 |
| 群功能开关 / 人格 JSON | 无常驻缓存 | 查询/toggle 前 `refresh()`；人格 set_* 均读盘→改→写 |
| 提示词 `*.md` / `.env` / `models_settings.json` / 别名字典 | bot 只读 | 配置看门狗 mtime 热重载（§2.8），保存即生效 |
| 更新记录 `changelog.json` | 无缓存 | bot 每次查询直接读盘，面板保存后下一条 `/更新日志` 即生效；群发经文件队列（`pending_broadcast.json` 原子改名认领）解耦 |
| 特殊会话 | 无缓存 | 每次操作直接读盘 |

面板写操作普遍**写前备份**（`.bak` / `BACKUP_DIR`）+ 原子写（tmp + replace），并记录面板侧审计日志。

---

## 十三、架构演进

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
  ├── 多轮 Think→Act→Observe→Respond (最多 20 轮)
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
  ├── 自动开启: @机器人 回复后自动为发起者打开 90 秒窗口
  ├── 消息续期: 每条消息重置计时器为完整 90 秒
  ├── 取消命令: /取消, #取消, /结束, #结束 → 手动关闭窗口
  ├── Agent 感知: [连续对话模式] 前缀 → 简洁回复, 适时建议退出
  ├── 超时清理: 90 秒无消息自动过期, 静默清理
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
  ├── agent/special_session.py: 特殊会话 (百万 token 上下文, 快照+增量双层存储, 按角色限制数量)
  ├── agent/agent.py 更新:
  │   ├── build_system_prompt(): 动态注入硬件上下文 (替换硬编码规格表)
  │   ├── run(): 新增 session_type 参数 → 路由到 SpecialSessionManager
  │   └── _build_messages(): 注入 session marker + quota context
  ├── agent/profile.py 更新: 画像存储路径迁移到 {USER_DATA_ROOT}/{safe_id}/profile.json (自动迁移旧路径)
  ├── plugins/agent_router.py 更新:
  │   ├── _handle_session_command(): 特殊会话管理命令（含 /帮助）
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

### v2.17 — 角色 / 羁绊查询 + 卡片渲染 (2026-08-20)
```
新增: character_detail / bond_detail
  - tools/character_detail.py + tools/bond_detail.py: 查询角色/羁绊详情
  - 数据源: Wiki 爬虫缓存 data/wiki_cache/character_details.json
  - tools/name_resolver.py: 角色别名模糊匹配
  - tools/card_renderer.py: 角色/羁绊卡片图片渲染
  - 直接命令: /角色详情 <名称>、/羁绊详情 <名称> (不经智能体, 零 token)
  - 修复: 中文命名图片文件路径解析、卡片图片排版/中英文混杂
```

### v2.18 — 团战截图测速工具 (OCR) (2026-08-21)
```
新增: parse_battle_screenshots
  - tools/battle_parser.py: 战斗截图 OCR 解析 (角色名/行动值/阵营)
  - lib/ocr_engine.py: EasyOCR 文本提取 + BuffDetector (cv2 多尺度模板匹配)
  - lib/status_icons.py: 状态图标文件名 → 中文标签 (STATUS_ICON_CN)
  - 修复: 测速工具智能体循环冲突、测速意图匹配两项方法工具候选
```

### v2.19 — 行动值修正模块 + buff 图标爬取 (2026-08-22)
```
v2.18 基础上增加:
  - 行动值修正模块: 技能/羁绊/免疫套带来的行动值修正
  - Wiki buff 图标爬取 + 测速时匹配 (status_icons.STATUS_ICON_CN)
  - lib/buff_alias.py: buff 名 → 图标标签别名归一化 + 按角色裁剪模板集
  - 修复: 敌我双方可存在相同角色、特殊会话/清空命令适配、buffDetector 未生效
  - 连续会话有效窗口期 300s → 90s
```

### v2.20 — 技能分类层重建 + 触发链推断 (2026-08-23)
```
v2.19 基础上增加:
  - tools/ag_skill_index.py: Skill 对象类 + 阵营触发方式分类
  - tools/ag_trigger_engine.py: 行动值效果触发链推断 (以截图冷却态为证据)
  - tools/ag_llm_resolver.py: 嵌套子技能条件的 L4 窄 LLM 回退 (15 类 trigger 分类)
  - 删除: 战斗解析器中被全行冷却观测取代的死代码 _row_skill_cells
```

### v2.21 — 轻量/全量模式 + 正在行动角色修正 (2026-08-23)
```
v2.20 基础上增加:
  - parse_battle_screenshots 新增 mode 参数: light (仅角色名+行动值, 约10s) / full (完整流程, 约90s)
  - 正在行动角色不再被剔除: 其结束行动值按满条 100% 折算 (raw_format 已内置)
  - 截图十六进制文件名丢字自校正 + 工具循环熔断话术
```

### v2.22 — 配置迁移 (.env.example + 并行线程数) (2026-08-23)
```
v2.21 基础上增加:
  - .env.example: .env 说明文档 + 模板 (单一事实来源, setup.sh 直接 cp)
  - BUFF_DETECTOR_WORKERS: BuffDetector 按行并行匹配线程数, 由 config/performance.json 迁至 .env (默认 2)
  - model router 回退统一: ProfileManager/SpecialSessionManager/Agent 默认客户端改用
    _model_router.reasoning_client (models_settings.json 优先, .env 回退), 修复仅读 .env 而跳过 JSON 的问题
```

### v2.23 — 子任务结构化记录 + 连续对话主动结束 (2026-08-28)
```
v2.22 基础上增加:
  - agent/task_record.py: 子任务完整记录 (目标/结果/参数/工具调用链/追溯索引),
    全量归档到 data/task_log/{uid}.jsonl, 单行摘要进入上下文
  - begin_task / finalize_subtask 工具: 任务开窗 → 收尾折叠, 避免多轮问答污染上下文;
    抽卡/测速类工具回复超 600 字时自动压缩为降级记录
  - end_continuous_mode 工具: LLM 识别用户告别意图后主动结束连续对话窗口
  - 修复: 角色头像重绘不生效、人格切换上下文混乱 (切换后询问保留/清空)、/取消 @机器人 不生效
```

### v2.24 — Web UI 管理面板 + Token 计量 (2026-09-09)
```
v2.23 基础上增加:
  - webui/: FastAPI 独立服务 (127.0.0.1:8090, SSH 隧道访问), scrypt 登录鉴权
  - 功能页: 进程管理 (含看门狗)、配置热编辑 (提示词/人格/.env/模型, 密钥脱敏)、
    记忆/画像管理、会话管理、日志查看、审计、硬件监控、Token 用量看板、反馈处理、Playground
  - lib/token_ledger.py: Token 用量记账 (data/token_usage/, 看板数据源)
  - start_webui.sh: 面板 启动/停止/重启/状态; start.sh/stop.sh 联动
```

### v2.25 — 画像瘦身 + 确定性过滤 + P1 三层记忆引擎 (2026-09-15)
```
v2.24 基础上增加:
  - P0 画像瘦身: UserProfile.facts 休眠 (不再提取/注入), 只保留类型化槽位
    (nickname/interests/preferences); scripts/cleanup_profile_facts.py 清理存量
  - agent/fact_filter.py: Layer 2 确定性过滤 (复合词/正则/结构规则), 拦截 LLM 幻觉候选
  - Layer 3 批量调度: observe_turn 缓冲, 每 PROFILE_BATCH_K=5 轮单次合并 extract+judge
    flash 调用 (extract_batch), 单飞防并发, delete-flush 收尾
  - P1 TieredMemory 三层记忆引擎 (§2.4): SHORT/MEDIUM/LONG 状态机,
    extraction_count 逻辑时钟, 频次晋升/年龄降级, MEDIUM 注入提示词;
    存储 data/memory/tiers/{uid}.json + tiers/long/ 快照
  - scripts/migrate_memory_p1.py: 旧记忆 → 三层结构迁移 (播种 + 归档遗留转储)
  - MEMORY.md 接入系统提示词 (三层记忆引擎规则)
```

### v2.26 — 前缀缓存命中率优化 (2026-09-22, Cache-Hit-Rate-Plan Phase 1/3/4)
```
v2.25 基础上增加:
  - 提示词四层分级: L1 用户不变量头部 (跨用户共享前缀) → L4 易变内容置尾,
    画像/时间变化不再破坏头部字节稳定; 系统提示词去时间戳
  - 历史迟滞修剪: Session.trim 仅在超出 max+TRIM_HYSTERESIS(4) 条时一次裁回,
    头部每几轮才变一次而非每轮; 压缩边界按 4 步进
  - webui 缓存命中率分口径观测 (Token 看板, 多提供商按模型分离)
  - 修复: web_fetch 对 GBK/GB2312 页面强制 UTF-8 解码乱码
```

### v2.27 — 面板热管理三件套 (2026-09-30)
```
v2.26 基础上增加:
  - Wiki 别名管理 (8b6ecc1): 面板「Wiki 缓存」页编辑 config/characters/ 别名字典,
    bot 侧 NameResolver 经配置看门狗 mtime 监听热重载 (§2.8)
  - 模型配置结构化编辑 (55fad24, §11.6): 5 段表单 + 保存前最小真实调用探测
    (webui/model_probe.py, 分级判定), 仅验证通过的配置落盘;
    ModelRouter.reload()/MultimodalClient.reload() + 固化引用重挂, ~10s 热生效
  - 面板↔bot 缓存一致性修复 (f9c36c7): ProfileManager/SessionManager 缓存命中时
    按 st_mtime_ns 重校验, 面板编辑画像/清除临时会话不再被 bot 内存缓存静默回滚;
    extract_batch 在 LLM await 后重新 get(); 会话写回前检测外部删除
  - 配置看门狗扩为三组监听: *.md / 别名字典 / models_settings.json (各 5s 轮询+冷却)
```

### v2.28 — 搜索结果存档与按需召回 (2026-09-30)
```
v2.27 基础上增加:
  - agent/search_archive.py: SearchArchive (仿 MemorySystem(base_dir) 构造),
    search_web/web_fetch 完整结果落盘 data/search_cache/{uid}/{id}.json;
    保留策略 7 天 TTL + 每用户 50 条 (save 时惰性清扫, mtime 升序删最旧),
    独立于 workspace 配额; 存档失败静默, 不影响工具主流程
  - builtin_tools.py: _archive_tool_result() 辅助 (双层 try/except 导入 contextvar),
    search_web/web_fetch 成功返回末尾附「[存档] id: xxx」行 —
    id 只随 append-only 工具消息流转、不进系统提示词, 与 v2.26 前缀缓存优化不冲突
  - recall_search_result 工具 (agent_router 模块级注册, _PUBLIC_TOOLS):
    上下文折叠/压缩后按 id 取回全文, 免去重搜; id 严格校验 (^[0-9a-f]{12}$,
    防路径穿越) + contextvar user_id 作用域 (跨用户读取天然不可行);
    输出头带存档时间与距今小时数供模型判断时效
  - 补齐 TaskRecord「指针+落盘详情」设计的另一半: 折叠摘要 refs 可携带存档 id
  - TOOLS.md: 新增 recall_search_result 章节; search_web/web_fetch 补 Archive 说明
  - test_agent.py: 新增 TestSearchArchive 套件 (7 用例: round-trip/用户隔离/
    路径穿越/TTL/上限/无上下文降级/helper 全链路), 14→15 套件, 98→105 用例

工具数量: 29 → 30
```

### v2.29 — 更新记录 / 更新日志 (2026-10-02)
```
v2.28 基础上增加:
  - tools/changelog.py: 面向用户的更新记录读写/格式化 (纯 IO, 无 nonebot 依赖),
    命令拦截 + get_changelog 工具 + 群发轮询器三方共用; 数据 data/changelog/changelog.json
    (结构: entries[] = {version, date, changes[{type,text}], created_at, broadcast_at}, 新→旧);
    变更类型 新增/修复/优化/调整/移除; get_recent(d) 默认 3, clamp 1..20;
    format_for_qq 输出结构化纯文本 (不含 markdown, 遵循 SOUL.md #8)
  - 双通道范式 (仿 redeem_code):
    · /更新日志 [d] / /update record [d] (亦 #更新日志、/更新记录、/update log、/changelog)
      命令直连拦截, 零 token (_handle_changelog_command, 接入 _handle_agent_message_impl 分发链)
    · get_changelog 工具 (_build_tool_registry 注册, _PUBLIC_TOOLS) 供自然语言「最近有什么更新」
  - 后台任务 _start_changelog_tasks() (@get_driver().on_startup, 两个协程):
    · 群列表导出: 启动 15s 后 + 每 600s bot.get_group_list() → known_groups.json
    · 群发轮询器: 每 5s claim_pending() (原子改名 .processing 认领) → _do_broadcast
      (targets all 现场枚举 / 指定群号; _split_text 分段 + send_group_msg 逐群 1s 限速)
      → 回写 broadcast_status.json + mark_broadcast
  - webui: /changelog 页 (更新记录编辑 + 群发卡), data_reader.changelog_* (校验 + 原子写),
    5 个端点 (GET/PUT /api/changelog, GET groups, POST broadcast, GET broadcast/status);
    面板↔bot 经 data/changelog/ 共享文件解耦 (沿用 redeem_code 文件队列范式), bot 每次查询直接读盘
  - 文档: HELP.md / FEATURES.md / TOOLS.md 补 /更新日志 与 get_changelog

工具数量: 30 → 31
```

### v2.30 — 更新公告图片卡片 + 公开站点 (2026-10-02)
```
v2.29 基础上增加:
  - tools/card_renderer.py: render_changelog_card(entries, out_path, title, site_url)
    复用 help/feature 卡骨架渲染暗色 PNG (版本 pill 循环色 + 变更类型 badge 色
    新增绿/修复红/优化蓝/调整金/移除灰 + _wrap 像素宽换行 + 站点短链副标题);
    entries 空返回 None; 输出 data/wiki_cache/changelog_card.png
  - QQ 端发卡 (agent_router.py):
    · /更新日志 命令: 文本后 best-effort 追加卡片 (asyncio.to_thread 渲染 + _safe_send 图)
    · 群发 _do_broadcast: 循环外渲染一次, 逐群先发图再发文本; 发卡失败降级仅文本不计 failed
    · 指路短链 _PUBLIC_SITE_URL (env BOT_PUBLIC_SITE_URL, 默认 https://bot.oneweblog.cn)
  - tools/changelog.py: format_for_qq / format_announcement 增可选 site_url,
    非空时尾部追加纯文本指路行「📖 使用说明与演示：<url>」(不含 markdown)
  - 公开站点 public_site/ (bot.oneweblog.cn, 纯静态只读, 独立于 admin webui):
    index.html/style.css/app.js 单页 (hero 场景卡 / 怎么用: 命令 chip 点击复制+自然语言说法 /
    演示 assets 缺失占位降级 / 更新记录 fetch changelog.json / 反馈页脚)
  - webui: config.PUBLIC_SITE_DIR (env WEBUI_PUBLIC_SITE_DIR) / PUBLIC_SITE_BASE_URL;
    data_reader._export_public_changelog 在 changelog_save 成功分支把脱敏快照
    (仅 version/date/changes, 剔除 created_at/broadcast_at/_class, 绝不含群/用户数据)
    原子写 <PUBLIC_SITE_DIR>/changelog.json, 失败静默不阻断保存 → 保存即同步
  - docs/bot-site-nginx.md: nginx server 示例 (静态服务 + changelog.json no-cache + HTTPS)

工具数量: 31 (不变)
```
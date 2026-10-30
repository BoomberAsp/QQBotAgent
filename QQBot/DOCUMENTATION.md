# QQBot 项目文档

## 项目概述

**QQBot** 是基于 [NoneBot2](https://nonebot.dev/) 构建的 **LLM Agent 智能体**，支持工具调用（Tool Calling）、用户画像、三层长期记忆、特殊会话（持久化上下文 + `/压缩会话` 深度压缩）与用户工作区隔离。当前同时接入 **两条 QQ 协议渠道**：NapCat / OneBot V11（QQ 号身份）与 **QQ 官方机器人 API**（openid 身份，单聊与群 @）。

- **智能体架构**: Think→Act→Observe→Respond 循环，Markdown 配置文件驱动；平台无关内核（`agent/turn.py`）+ 入口适配器（`adapters/`）
- **运行框架**: NoneBot2 (Python)
- **QQ 协议适配**: NapCat (OneBot V11 反向 WebSocket) / QQ 官方机器人 API（`nonebot-adapter-qq`，WebSocket 网关）
- **AI 后端**: OpenAI 兼容接口 + 多模型路由（REASONING / FLASH / MULTIMODAL / AUDIO / OCR）；图像生成与 TTS 走 DashScope 原生接口（IMAGE_GEN / TTS 段），模型配置可经 Web 面板结构化编辑（保存前真实探测）并热重载
- **双平面路由**: `split` 人格下，社交面（角色本人）与任务面（中立 Roxy，agent-as-tool）分离，任务结果回角色口吻转述
- **角色养成**: 好感度 10 级（只涨不掉）+ 心情值 + 陪伴统计；礼物 / 戳一戳 / 称呼演化 / 档案解锁 / 剧情场景 / 纪念日 / 久别重逢；UGC 自创角色（含参考立绘与声纹）
- **影音生成与记忆**: 角色画图（Lv4）与语音（Lv5）、主动发媒体（Lv7+）；资产卡（文本化本体）+ 二进制 LRU + 按 id 召回 + 事件蒸馏
- **原作剧情知识库**: 本地 SQLite FTS5 检索（约 5 万句对白），按人格白名单注入 L4；`story_search` 工具主动检索
- **直连识图（Direct Vision）**: 模型段开启 `supports_vision` 时，本轮图片以 `image_url` content part 直接进主模型
- **搜索引擎**: SearXNG (Docker 自托管，聚合 Bing 等) + `web_fetch` 直接抓取网页（TLS 指纹伪装 + 可选无头浏览器兜底）
- **特殊会话**: 每用户持久化会话（按角色：管理员 10 / 会员 3 / 普通 1），快照+增量存储，读时分层截断适配模型窗口，`/压缩会话` 可将早期历史浓缩为摘要（保留最近 10 条原文）；**人格绑定在会话上**（`metadata.persona_key`），切换会话自动切换人格（解析链：剧情钉住 > 会话绑定 > 个人设置 > 群默认 > 全局默认）
- **用户工作区**: 每用户独立文件空间，配额管理（2GB/500MB/100MB），跨会话隔离，80% 预警 + 超限清理协议
- **三层记忆引擎**: `TieredMemory`（SHORT/MEDIUM/LONG），LLM judge 归类 + 频次晋升/年龄降级状态机；LONG 支持索引注入与 `recall_memory` 召回
- **Web 管理面板**: FastAPI 独立服务（`webui/`，102 个 API + 18 个页面），进程管理、配置热编辑、记忆/画像/别名管理、**角色管理**（用户自创角色卡的浏览/编辑/体检/版本回滚/立绘）、Token 看板、反馈处理、更新记录/公告编辑与双渠道群发、**邮件管理**（信箱只读 + 系统邮件/撤回队列）、Playground、官方路由状态卡
- **更新记录与公告**: 结构化更新日志（版本/日期/分类变更条目）+ 自由公告（标题/正文/markdown），用户经 `/更新日志 [d]`、`/update record [d]` 或自然语言查询，管理员在面板编辑并**双渠道群发**（OneBot 群 + QQ 官方群，可按渠道勾选或全选）；QQ 端以图片卡片/markdown 呈现，并同步脱敏快照到公开站点 `bot.oneweblog.cn`
- **部署方式**: Docker Compose（searxng + qqbot；vLLM 由 `vllm-start.sh` 可选单独拉起）或手动部署

---

## 目录结构

```
QQBotAgent/
├── setup.sh / start.sh / stop.sh / start_bot.sh   # 安装 / 启动 / 停止 / 仅启动 NoneBot
├── start_webui.sh              # Web 管理面板 启动|停止|重启|状态（127.0.0.1:8090）
├── test.sh                     # 测试调度：3 个主测试 + 记忆 benchmark + 11 个离线脚本
├── bot.py                      # NoneBot 备用入口（仅注册 OneBot 适配器；生产用 `cd QQBot && nb run`）
├── main.py                     # PyCharm 占位入口（非实际入口）
├── docker-compose.yml          # Docker 编排（searxng + qqbot 两服务）
├── Dockerfile / docker-entrypoint.sh / vllm-start.sh
├── napcat.sh                   # NapCat 安装脚本（Rootless / Docker）
├── deploy_public_site.sh       # 公开站点发布脚本
├── searxng/settings.yml        # SearXNG 搜索引擎配置（国内可用引擎）
│
├── webui/                      # ★ Web 管理面板（FastAPI 独立进程）
│   ├── main.py                 #   入口（93 个 /api/ 端点 + 18 个页面路由 + 2 个 WebSocket）
│   ├── auth.py                 #   登录鉴权（scrypt 口令哈希 + 内存会话）
│   ├── process_manager.py      #   启停 bot / 看门狗状态持久化
│   ├── config_editor.py        #   提示词 / 人格 / .env / JSON 配置热编辑（密钥脱敏 + 原子写）
│   ├── character_admin.py      #   角色管理：UGC 卡读写/体检/备份版本/修复/立绘（写路径经 CharacterRegistry.update_character）
│   ├── card_parser.py          #   card.md 结构化解析与「偏移切片」编辑（纯函数，零 IO）
│   ├── model_probe.py          #   模型 API 可用性探测（最小真实调用 + 分级判定）
│   ├── data_reader.py          #   记忆/画像/会话/Wiki 别名/兑换码/更新记录/公告群发/邮件管理 数据读写
│   ├── log_viewer.py / audit.py / hardware_monitor.py / playground.py / napcat_login.py
│   ├── official_status.py      #   QQ 官方路由状态（只读 official_status.json，mtime 判新鲜度）
│   ├── config.py               #   面板配置（从 QQBot/.env 读 USER_DATA_ROOT 等）
│   ├── templates/ static/      #   21 个页面模板与静态资源
│   └── data/                   #   面板运行时数据（webui.pid / logs / watchdog_state.json / backups）
│
├── public_site/                # ★ 公开使用站点（bot.oneweblog.cn，纯静态只读）
├── docs/                       # 开发文档
│   ├── qqbot-api-v2-full-docs.md          #   官方 API v2 离线全量文档（192 章）
│   ├── qqbot-official-buttons-design.md   #   官方渠道按钮交互设计（功能盘点 + 接入方案）
│   ├── 更新日志.md / aliyun_models_api.md / COMPARISON-QQBOTAGENT.md
│   └── QQBOTAGENT-IMPROVEMENT-PLAN.md / openrubi_migration_plan.md / bot-site-nginx.md
├── test/                       # 离线测试与实验脚本（gitignore，仅开发机；test.sh 调用其中 11 个）
├── evidence/                   # 验证证据 / 截图
├── *.md                        # 设计与计划文档（Bond / Story-KB / Voice / Cache-Hit-Rate / Memory 等）
│
└── QQBot/                      # NoneBot 机器人主体
    ├── .env / .env.example     # 环境配置（密钥）与逐行注释模板
    ├── requirements.txt / pyproject.toml / config.yml
    ├── test_agent.py           # 主测试套件（53 个 Test 类 / 316 个用例）
    ├── test_workspace.py       # 工作区 / 会话文件测试（11 个类）
    ├── test_story_kb.py        # 剧情知识库检索测试（37 个用例，无网络）
    ├── DOCUMENTATION.md / PLAN.md / README.md
    │
    ├── agent/                  # 智能体核心
    │   ├── turn.py             #   ★ 平台无关内核：命令分发、回合编排、发送/进度、权限上下文
    │   ├── agent.py            #   Think→Act→Observe→Respond 主循环
    │   ├── inbound.py / outbound.py        # 入站视图 / 出站契约（Sender、OutImage/OutAudio/OutProgress）
    │   ├── tool_registry.py    #   工具注册（OpenAI JSON Schema）
    │   ├── context.py          #   contextvars：用户/角色/工作区/平台/Sender/人格/记忆注入 …
    │   ├── permissions.py      #   三层角色 + 按平台身份表 + 工具过滤 + 配额
    │   ├── session.py / special_session.py / continuous_session.py
    │   ├── memory.py           #   MemorySystem（Markdown）+ TieredMemory（SHORT/MEDIUM/LONG）
    │   ├── fact_filter.py / profile.py     # 画像提取流水线（LLM + 确定性过滤 + 批量调度）
    │   ├── personality.py / character_registry.py   # 内置人格 + UGC 角色叠加层
    │   ├── bond.py / bond_content.py       # 好感度 / 心情 / 送礼 / 戳一戳 / 剧情场景 / 重逢 / 纪念日
    │   ├── asset_memory.py     #   多模态资产卡（文本化本体 + 去重 + 检索 + LRU）
    │   ├── delegation.py       #   双平面路由：任务面委派 + 角色口吻转述
    │   ├── greeting.py         #   主动消息候选索引 + 配额簿记（官方渠道；日上限共享，无月配额）
    │   ├── mailbox.py          #   角色信箱存储：每用户信件/摘要 JSON（28 天保留 + 200 封上限）
    │   ├── rate_limit.py       #   主动消息令牌桶（c2c 10/s、group 1/s，按渠道复用）
    │   ├── group_registry.py   #   官方群登记表（GROUP_AT 入站纯记账，60s 节流）
    │   ├── portrait.py         #   参考立绘校验 / 规整 / 描述
    │   ├── hardware.py / workspace.py / workspace_snapshot.py / quota_cleanup.py
    │   ├── task_record.py / search_archive.py / group_features.py
    │   └── config/             #   SOUL/IDENTITY/AGENTS/MEMORY（注入）+ TOOLS/BOOTSTRAP/SESSION（加载）
    │                           #   + HELP/FEATURES（命令时读取）+ personalities/ + portraits/
    │
    ├── adapters/               # 平台入口适配器
    │   ├── onebot.py           #   OneBot V11：事件→InboundMessage、附件下载、OneBotSender
    │   ├── qq_official.py      #   QQ 官方：build_view、附件解析、QQOfficialSender（缓冲+合并+配额+流式能力位）、主动消息（含 markdown+按钮信封阶梯降级）
    │   ├── qq_official_buttons.py  # 官方键盘 wire 格式 + 回调编解码 + 交互回执发送器 + 点击遥测
    │   └── qq_official_stream.py   # 官方 c2c 流式消息：MessageStreamer 双流传输（replace 累计正文）+ 断流强制收尾 + 熔断器 + stream_snapshot
    │
    ├── plugins/                # NoneBot 插件
    │   ├── agent_router.py     #   OneBot 入口 + 配置看门狗 + 更新记录/公告双渠道群发 + 群列表导出（OneBot+官方）
    │   ├── qq_official.py      #   官方入口 + 每日主动循环（角色信箱信封 + 主动发媒体，双向互斥）+ 拒收/恢复事件 + 状态心跳 + 邮件管理队列 worker（面板删信/系统邮件，§16.2）
    │   ├── qq_official_buttons.py  # 官方 INTERACTION_CREATE 回调入口（幂等+限频+审计）
    │   ├── pullingMonitor.py / check_redeem_code.py / ruyipage_fetch.py / offline_logger.py
    │   └── group.py / speed.py / photos.py / test.py / utils.py（旧插件，仅保留个别工具函数）
    │
    ├── tools/                  # Agent 工具实现
    │   ├── builtin_tools.py    #   搜索/抓取/代码/Shell/Git/PDF/时间/系统负载/删除文件/搜索存档召回
    │   ├── file_tools.py       #   文本 / PDF / 图片 / 音频读取与分析
    │   ├── map_tools.py        #   地理编码 / 天气 / POI / 路径规划
    │   ├── legacy_tools.py     #   抽卡 / 抽卡动画 / 测速 / 乱速 / 代码解释 / 翻译
    │   ├── social_tools.py     #   画图 / 语音 / 资产召回 / 送礼（养成工具）
    │   ├── creation_tools.py   #   自创角色
    │   ├── character_detail.py / bond_detail.py / card_renderer.py
    │   ├── battle_parser.py / ocr_name_matcher.py
    │   ├── ag_skill_index.py / ag_trigger_engine.py / ag_llm_resolver.py / buff_vocab_dump.py
    │   ├── wiki_scraper.py / wiki_glossary.py / name_resolver.py
    │   └── changelog.py        #   更新记录 + 公告读写/格式化/群发队列/群列表快照（纯 IO）
    │
    ├── lib/                    # 自定义库
    │   ├── deepseek_client.py / model_router.py / multimodal_client.py
    │   ├── token_ledger.py     #   Token 用量记账（面板看板数据源）
    │   ├── media_gen.py        #   DashScope 图像生成 + TTS（并发闸 / 硬超时 / 优雅失败）
    │   ├── image_parts.py      #   Direct Vision：图片 → OpenAI image_url part 编码
    │   ├── story_kb.py         #   原作剧情检索（SQLite FTS5 + BM25/RRF）
    │   ├── ocr_engine.py / buff_alias.py / status_icons.py
    │   ├── browser_fetch.py    #   ruyiPage 无头浏览器抓取共享运行器
    │   ├── status_heartbeat.py #   状态文件原子读写（面板↔bot 文件 IPC）
    │   ├── errors.py           #   错误分类 + 诊断编号
    │   └── amap_client.py
    │
    ├── scripts/                # 运维脚本
    │   ├── build_story_kb.py           # 离线构建剧情知识库（story.db）
    │   ├── migrate_memory_p1.py        # 旧记忆 → 三层结构
    │   ├── migrate_memory_p2_categories.py  # 记忆条目类型化（schema v2）
    │   └── cleanup_profile_facts.py    # 画像事实存量清理
    │
    ├── benchmarks/memory/      # 记忆引擎 benchmark（场景流 / 端到端 / personas / 基线报告）
    ├── images/                 # 抽卡动画、测速样本、buff 图标
    ├── logs/                   # 运行日志（loguru）
    ├── docs/                   # 机器人侧开发文档
    │
    ├── config/                 # 配置（部分 git-ignored）
    │   ├── models_settings.json / models_settings_example.json  # 7 段模型 + task_routing
    │   ├── multimodal.json     # 旧版多模态配置（已被 models_settings 取代）
    │   ├── gacha_data.json     # 抽卡数据
    │   ├── story_personas.json # 剧情 KB 人格白名单 / 角色映射
    │   ├── translation_glossary.json  # 翻译对照表
    │   ├── characters/         # 角色/羁绊别名字典（面板可编辑）
    │   ├── font/               # 卡片渲染字体
    │   └── ocr_digit_templates/
    │
    └── data/                   # 运行时数据（生产环境由 USER_DATA_ROOT 等 env 指向数据盘）
        ├── sessions/           #   临时会话持久化
        ├── memory/             #   三层记忆（tiers/{uid}.json + long/ 快照 + 旧版 Markdown）
        ├── users/              #   用户画像
        ├── workspace/          #   用户工作区（代码/仓库/上传/输出）
        ├── bonds/              #   好感度/心情/陪伴（按用户 × 角色）
        ├── characters/         #   UGC 角色卡（card.md + meta.json）
        ├── assets/             #   影音资产卡 + 二进制（可 LRU 淘汰）+ 去重索引 + 审计
        ├── story_kb/           #   剧情知识库（story.db + 构建报告 + 原始语料）
        ├── token_usage/        #   Token 用量
        ├── audit/              #   工具调用审计（JSONL）
        ├── feedback/           #   用户反馈 / Bug 报告
        ├── name_index/         #   角色名索引缓存
        ├── wiki_cache/         #   Wiki 爬取缓存与卡片图
        ├── redeem_code/        #   兑换码缓存
        ├── mailboxes/          #   角色信箱（每用户信件/摘要 JSON，28 天保留；bot 单写、面板只读）
        ├── mail_admin/         #   面板邮件管理队列（queue/processing/status；面板写、bot 认领执行回写）
        ├── changelog/          #   更新记录 + 公告 + 群发/群列表文件队列（按需创建）
        ├── user_prefs/         #   用户偏好（disclaimer.json 免责尾开关，按渠道 user_id）
        ├── personality_config.json / personality_settings.json / group_personality.json
        ├── bond_greet_targets.json  # c2c 联系人登记表（每个私聊回合落库；面板邮件页用户列表并集来源）
        ├── group_registry.json #   官方群登记表（GROUP_AT 入站记账，v1）
        └── official_status.json #  官方路由状态（面板仪表盘读取；按需生成）
```

---

## 一、智能体架构 (Agent Architecture)

### 1.1 核心循环

```
QQ 消息
  ├─ OneBot 渠道（QQ 号）      adapters/onebot.py      ─┐
  └─ 官方渠道（openid）        adapters/qq_official.py  ─┤
                                                         ▼
                          agent/turn.py（平台无关内核）
   │  run_agent_turn(view) / run_continuous_turn(view)
   │
   ├─ 平台化身份：_current_platform → PermissionManager（QQ 号表 / openid 表）
   ├─ 直连命令层（零 token，命中即 return）：会话 / 人格 / 养成 / 游戏 / 群管理 / 反馈 …
   │
   └─ 自然语言 → Agent.run(user_message, user_id, client=…)
         │
         ├─ 1. Build Messages（四层分级，见 §1.2）
         │      L1–L3 system + 会话历史 + L4 易变尾 + 当前用户消息
         │
         ├─ 2. THINK → LLM（chat_completion_with_tools）
         │
         ├─ 3. Has tool_calls?
         │      YES → ACT（ToolRegistry.execute）→ OBSERVE → 回到 2
         │      NO  → RESPOND（最终回复）
         │
         ├─ 4. 收尾：
         │      Session.update() + trim（迟滞修剪）
         │      ProfileManager.observe_turn()（每 5 轮批量 extract+judge）
         │      羁绊结算 / 剧情收尾 / 记忆注入准备
         │
         └─ 5. Sender 出站（_safe_send + _split_text，300 字符/块、1s 间隔）
                官方渠道：缓冲合并 → 被动回复配额控制 → 失败降级
```

**关键参数**：

| 参数 | 值 | 说明 |
|---|---|---|
| `max_tool_iterations` | 20（生产实例） | 最多 20 轮工具调用；默认构造值为 5 |
| `thinking_timeout` | 180.0s | 单次 LLM 调用超时 |
| 外层总超时 | 300.0s | 单条消息处理（`asyncio.wait_for`） |
| `max_context_messages` | 20 | 临时会话保留条数（+ `TRIM_HYSTERESIS=4` 迟滞） |
| `session_timeout` | 1800.0s | 临时会话 30 分钟无活动过期 |
| 连续对话窗口 | 90s | 群聊 @ 一次后免 @ 窗口 |
| 重复工具熔断 | 同工具同参数失败 ≥2 次 | 中断循环并给出说明 |
| 上下文压缩 | `COMPRESS_STEP=4` / 尾部全文 20 条 | 特殊会话读时阶梯压缩，保前缀稳定 |
| 会话压缩阈值 | `context_window` 的 85% / 95% | ≥85% 时在 L4 注入「记忆负荷提醒」，让 agent 以人格口吻建议 `/压缩会话`（每 10 条消息限流一次）；≥95% 触发 `_emergency_truncate` 读时紧急截断，防 API 400 |
| 自动折叠 | 结果 >600 字（抽卡/测速/截图解析） | 折叠为 `TaskRecord` 摘要 |

### 1.2 系统提示词结构（四层分级，前缀缓存友好）

v2.26 起消息序列按**变化频率**分四层构建（`agent.py::_build_messages`），越靠前越稳定，最大化提供商侧前缀缓存命中率：

```
L1 全局静态 (system)   → SOUL.md + IDENTITY.md + AGENTS.md + MEMORY.md
                          (+ 硬件上下文，仅 bootstrap() 后存在)
                          所有用户共享同一前缀，字节级稳定（无时间戳）
L2 人格/群 (system)    → 角色卡正文（内置/UGC，CharacterRegistry）
                          + 群功能限制块（同人格/同群共享）
L3 用户级稳定 (system) → 工作区路径 + 权限角色说明（同用户跨轮稳定）
   Conversation History → 会话上下文（迟滞修剪；特殊会话走阶梯压缩）
L4 易变尾部 (system)   → 真实当前时间、人格切换提示、羁绊状态（等级/进度/心情/
                          陪伴/重逢/纪念日/已解锁档案）、原作剧情记忆块、
                          影音资产注入（top 5）、特殊会话标记 + 创作/剧情指令、
                          记忆负荷提醒（≥85% 窗口）/ 上下文溢出保护通告（≥95%）、
                          工作区配额用量、用户画像、MEDIUM 记忆 + LONG 索引行
```

关键约束（均为代码注释中明确的设计决策）：

- **时间不进 L1**：早期实现把时间块拼进缓存的系统提示词，导致时间冻结；现改为每轮重建的 L4。
- **L4 位于历史之后**：所有易变内容（时间/羁绊/剧情/资产/画像/记忆）都在 append-only 历史之后，不打断 L1–L3 共享前缀。
- **人格块在 L1 之后**：避免按人格切分约 10k token 的共享前缀。
- **硬件段有门控**：只有 `Agent.bootstrap()` 检测过硬件才拼接；当前入口未调用 `bootstrap()`，故该段默认缺席（需要时在启动处调用一次）。

### 1.3 双协议入口与出站契约

| 层 | 实现 | 说明 |
|---|---|---|
| 入站视图 | `agent/inbound.py::InboundMessage` | 统一字段：`user_id / is_group / group_id / text / message_id / self_id / mentioned / reply_context / resolver / platform`；附件惰性下载，失败也记录为 `Attachment.error` |
| OneBot 入口 | `plugins/agent_router.py` + `adapters/onebot.py` | 群消息手工判 `@`（不依赖 NapCat `to_me`）；附件经 NapCat `get_file`/`get_record` 或 HTTP 兜底 |
| 官方入口 | `plugins/qq_official.py` + `adapters/qq_official.py` | 只处理 `C2CMessageCreateEvent` / `GroupAtMessageCreateEvent`；附件是公网 URL，直接下载；引用消息按 `msg_elements` + `ref_msg_idx` 解析 |
| 出站契约 | `agent/outbound.py::Sender` | `send_text / send_markdown / send_keyboard / send_image / send_audio / send_progress / open_stream`；`OutImage` / `OutAudio` / `OutMarkdown` / `OutKeyboard` / `OutProgress` 描述语义，平台细节在适配器；`supports_markdown` / `supports_keyboard` / `supports_stream` 能力位是渠道隔离唯一判据（官方 True / OneBot False）。`OutKeyboard(markdown, rows)` 的 rows 是 `lib/button_spec.py` 的平台无关按钮 DSL（`Btn`/`Rows`/`validate`/`to_text`）；不支持键盘的渠道 `send_keyboard` 自动降级为 `send_text(markdown + to_text(rows))`。回调链：官方渠道 INTERACTION_CREATE → `plugins/qq_official_buttons.py`（幂等+限频+审计）→ `agent/button_actions.py`（scene/role 复核 + 2.5s 预算）→ `agent/session_render.py`（共享键盘页渲染），毫秒级本地状态变更走回调按钮（type=1），需 LLM/>3s 的一律指令按钮（type=2，替用户打字复用命令链）。`supports_stream`/`open_stream()`（仅官方 c2c）返回 `MessageStreamer` 承载流式进度+回答，`None` 即回退缓冲路径 |
| OneBot 出站 | `OneBotSender` | 文本原样发、300 字符分块由内核负责；图片 `MessageSegment.image(Path)`；`send_markdown` 降级纯文本；`supports_stream=False`、`open_stream()` 恒 `None` |
| 官方出站 | `QQOfficialSender` | 缓冲 + 合并 + 配额（单聊 4 次/60min、群 5 次/5min）；图片/语音 base64 上传（本地文件不可直读）；语音被拒降级文件卡；markdown（msg_type=2）单独成组、按区块 ≤2000 字切分，被拒（40034011 等）同内容纯文本重发 + 连续 3 拒熔断 10 分钟，`QQ_OFFICIAL_MARKDOWN=off` 一键禁用（热读）；配额不足丢弃优先级 正文 > 免责 > 图片；进度行默认吞掉，仅「正在思考…」即时发送；**流式消息**（`adapters/qq_official_stream.py`，仅 c2c）走 `POST /v2/users/{openid}/stream_messages`：双流同锚点（进度流 `msg_seq` 基 900、回答流基 1800），工具进度「⏳ …」与 LLM 回答逐字上屏，免责声明随回答流末片（`input_state=10`）；**回退只补未上屏尾巴**（`_delivered_prefix_len` 容忍开场白/进度行污染；一次没开成才整篇重发，断流也补 `state=10` 收尾片；`input_mode=replace` 累计正文使分片重试幂等；`remain_msg_len` 实测为挂起长度遥测值而非预算，不参与门控），熔断器（3 连拒 600s / ApiNotAvailable 长熔断）+ `QQ_OFFICIAL_STREAM=off` 热读逃生门；split 人格与群聊不流式 |
| 失败语义 | `SendFailed` | 只有它是可重试错误；其它异常记日志后丢弃 |

### 1.4 双平面路由（`split` 人格）

```
用户消息（角色人格 rubi / UGC 角色）
  │
  ├─ 角色面（social）   allowed_tools = SOCIAL_TOOLS
  │     ├─ delegate_task  —— 把工具任务委托给任务面（agent-as-tool）
  │     ├─ give_gift / create_image / send_voice / recall_asset
  │     └─ 以角色人设直接答复闲聊、情绪与剧情
  │
  └─ 任务面（task）     run_delegated_task(persist=False)
        ├─ 以中立 Roxy 人格运行，隔离上下文（不写会话历史）
        ├─ 按用户角色过滤工具（与权限系统同一套 schema 过滤）
        └─ 结果 → revoice_task_result：角色口吻开场白 + 正文原样透传
```

- 角色卡可通过 HTML 注释声明 `mode: split\|unified`（默认 unified）；`task_voice: character|template` 控制任务结果是否用 LLM 转述。
- `_delegate_depth` 守卫单层嵌套，禁止递归委派。
- UGC 角色同样走双平面；内置助手 Roxy 不拆分，直接走任务面。

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
| `asset_memory` | `AssetMemory` | None | 影音资产卡（P5 起 L4 注入来源） |
| `max_tool_iterations` | `int` | 5 | 最大工具调用轮数（`turn.py` 生产实例为 20） |
| `thinking_timeout` | `float` | 180.0 | LLM 思考超时秒数 |

| 方法 | 说明 |
|------|------|
| `build_system_prompt()` | 拼接 L1 全局静态层: SOUL + IDENTITY + AGENTS + MEMORY(三层记忆规则) + 硬件信息，结果缓存；不含时间戳（时间在 L4 尾部） |
| `reload_configs()` | 清空缓存，重新加载所有配置文件（配置看门狗调用） |
| `run(user_message, user_id, client=None, progress_callback=None, session_type="temporary")` | **主入口**。执行完整 Think→Act→Observe→Respond 循环。可选 `client` 参数支持运行时模型切换 (ModelRouter)。可选 `progress_callback` 在每轮工具执行前推送进度消息 (如 "⏳ 正在搜索...")。`session_type` 参数支持 `"temporary"` (30min 超时) 和 `"special"` (持久化特殊会话，读时截断适配模型窗口) 两种模式 |
| `_build_messages(session, user_message, optional_special_session=None)` | 按 §1.2 四层结构构建消息列表 (L1-L3 system + history + L4 易变尾部 + current)。L4 含时间/特殊会话标记/quota/画像/MEDIUM 记忆注入。历史消息中的 `reasoning_content` 自动保留以支持 thinking mode 模型 |
| `_compress_context(context, recent_full=20)` | 特殊会话读时压缩（Layer 1+2）: 最近 20 条保留完整，更早的 tool result 压缩为摘要首行；压缩边界按 `COMPRESS_STEP=4` 步进（相邻 4 轮字节级稳定，保护前缀缓存）。Layer 3 深度压缩不在读时做——由写侧 `/压缩会话`（turn.py）+ `_emergency_truncate` + L4 记忆负荷提示承担 |
| `_emergency_truncate(context, budget_tokens)` | classmethod。上下文估算 token ≥95% 窗口时的读时兜底: 保留头部 `[会话摘要]` system 消息，插入「中间 N 条历史本轮未载入」系统注记，从尾部倒序回填至预算内；病态场景（单条超预算）退化为仅保留最后一条并截尾 4000 字 |
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

管理持久化的「特殊会话」——按角色限制数量（管理员 10 / 会员 3 / 普通 1），快照 + 增量双层存储；上下文按模型 `context_window` 读时截断，支持 `apply_compression()` 写侧深度压缩（`/压缩会话`）。

| 构造参数 | 类型 | 默认值 | 说明 |
|----------|------|--------|------|
| `user_data_root` | `str` | (必填) | 用户数据根目录 |
| `max_per_user` | `int` | 3 | 每用户最大会话数（默认值，实际按角色：管理员 10 / 会员 3 / 普通 1） |
| `llm_client` | `DeepSeekClient` | None | LLM 客户端 (用于自动命名) |

所有会话操作**按名称寻址**（非列表索引）；删除的二次确认码由 `agent_router` 在命令层实现，不在本类中。无内存缓存——每次操作直接读盘（面板/bot 跨进程天然一致）。

| 方法 | 说明 |
|------|------|
| `create(user_id, name=None, metadata=None)` | 创建特殊会话。name 为 None 时生成规则临时名 `{MMDD}_{HHMM}`，首次交互后 LLM 异步命名；重名自动追加 `_N` 后缀。metadata 写入 `_index.json` entry（如 `persona_key` 会话人格绑定、`kind`/`char_id` 剧情会话标记）。按角色的数量上限（10/3/1）由 `agent_router._handle_session_command()` 在上游强制，`create()` 本身不检查 |
| `get_active(user_id)` | 获取用户当前激活的特殊会话 (同一时间仅一个激活，读 `_index.json`) |
| `get_by_name(user_id, name)` | 按名称获取会话 |
| `list_sessions(user_id)` | 列出用户所有特殊会话 (索引条目: 名称/创建时间/消息数) |
| `update_metadata(user_id, name, updates)` | 合并 updates 进指定会话的 metadata 并持久化 `_index.json`（`/人格切换` 会话内写绑定用）；会话不存在返回 False |
| `switch_to(user_id, name)` | 切换到指定名称的会话；不存在时抛 `ValueError`（附可用会话列表） |
| `rename(user_id, old_name, new_name)` | 重命名会话（新名已存在则抛 `ValueError`） |
| `delete(user_id, name)` | 删除会话及其名下工作区文件（`repos/` 记录但保留；文件删除幂等），返回 `{"deleted", "freed_bytes", "kept_repos"}` 摘要 |
| `end_active(user_id)` | 退出特殊会话，回到临时模式 |
| `clear_context(user_id)` | 清空激活会话的消息历史但保留会话（`/clear` 用；删除磁盘 snapshot+delta 并重置计数） |
| `add_message(user_id, role, content, reasoning_content=None)` | 追加消息: 写入 JSONL 增量日志，每 50 条 (`SNAPSHOT_INTERVAL`) 触发快照并清空 delta |
| `add_file(user_id, name, file_path)` / `get_files(user_id, name)` | 记录/查询会话名下的工作区文件（文件溯源，供 delete 清理） |
| `auto_name(user_id, first_message, first_response)` | 异步: LLM 根据首次对话内容自动总结会话名（≤12 字符），成功则 rename |
| `set_context_window_provider(provider)` / `get_context_window()` | 上下文窗口感知（PLAN.md Idea 4 Phase B）：turn.py 注入 `lambda: _model_router.effective_context_window`（min(REASONING, FLASH) 的 `context_window`，面板热保存即时生效）；无 provider 时回退 `DEFAULT_CONTEXT_WINDOW=131072` |
| `apply_compression(user_id, name, summary_text, keep_recent=10)` | `/压缩会话` 的存储写回：新 context = [`[会话摘要]` system 消息] + 尾部 `keep_recent` 条原文；旧 context 全量存一代备份 `pre_compress_backup.json`（下次压缩覆盖、`/clear` 删除）；删旧 snapshot+delta、写新 snapshot、index metadata 记 `compressed_at`/`compress_count`/`pre_compress_messages`/`pre_compress_tokens_est`。返回前后统计。摘要生成（flash 滚动分块）由 turn.py 编排，LLM 失败时不落盘 |

模块级还提供启发式 token 估算 `estimate_text_tokens()` / `estimate_context_tokens()`（CJK≈1 token/字、其余≈4 字/token，正则计数、无 tiktoken 依赖），供压缩阈值与 `/压缩会话` 展示口径使用。

**剧情归档与配额豁免**（详见 `docs/scene-session-archive-design.md`）：`metadata.kind` 为 `bond_scene`（剧情）/`char_creation`（角色创作）的会话**不计入**按角色的数量配额（10/3/1 只约束普通特殊会话）。剧情走到结局由 `agent/turn.py::_finish_bond_scene` 经 `update_metadata` 写入 `completed=true` + `completed_at`（封档真源），随后 `end_active`——会话保留，可 `/切换会话` 回看，回看时 `is_scene=false`（不再注入剧情指令/剧本、工具集无 `complete_scene`）。`completed` 缺失的存量会话按“未完结”处理，并在 `/剧情` 去重与 `is_scene` 判定两个热点懒回填。剧情会话总量上限由 `MAX_SCENE_ARCHIVES`（默认 20，含进行中与已完结）在 `turn.py` 层强制；`SCENE_ARCHIVE_ENABLED=0`（热读）为紧急回滚开关，退回统一配额语义。`create()` 本身仍不检查任何配额——普通配额与剧情上限都在 `turn.py` 命令层强制。

**存储结构**:
```
{USER_DATA_ROOT}/{safe_id}/sessions/
├── _index.json              # 会话索引 + active_session（按名称）
└── {session_name}/
    ├── snapshot_00050.json  # 第 50 条消息时的完整快照（滚动清理旧快照）
    ├── snapshot_00100.json  # 第 100 条消息时的快照
    ├── delta.jsonl          # 上次快照以来的增量消息
    └── pre_compress_backup.json  # /压缩会话 前一代完整上下文（回滚保险，仅保留一代）
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

频次驱动的用户事实记忆状态机：**SHORT**（近期原文候选）→ **MEDIUM**（按频次强化的要点，唯一注入提示词的层）→ **LONG**（长期对象快照 + 一行索引注入 + `recall_memory` 按 id 召回；快照 write-once，超过 critical 阈值 90 天降级）。所有方法为同步实现（asyncio 单线程下无需锁），与 `MemorySystem` 并存。

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
| `TIER_SCHEMA_VERSION` | 2 | 存储 schema 版本（v2 增加 `category` + `last_confirmed_ts`） |
| `LONG_INJECT_MAX_LINES` / `LONG_RECALL_MAX_MATCHES` / `LONG_RECALL_SNAPSHOT_CAP` | 20 / 3 / 1200 | LONG 索引注入行数 / 单次召回条数 / 单条快照字符上限 |
| 
EARDUP_MERGE_DICE` | 0.6 | 近重复合并阈值（引擎级去重） |

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
| 搜索 / 时间 / 地图 / 天气 / PDF / 文件读取 | ✅ | ✅ | ✅ |
| 游戏工具（抽卡 / 测速 / 角色 / 羁绊 / 兑换码 / 截图解析） | ✅ | ✅ | ✅ |
| 记忆与编排（`begin_task` / `finalize_subtask` / `recall_memory` / `recall_search_result` / `get_changelog` / `get_user_info`） | ✅ | ✅ | ✅ |
| `story_search` | ✅ | ✅ | ✅（仅剧情白名单人格出现在 schema） |
| `get_system_load` | ✅ | ✅ | ❌ |
| `web_fetch` | ✅ | ✅ | ❌ |
| `download_repo` | ✅ | ✅ | ❌ |
| `execute_code` | ✅ 完整 | ✅ 受限 | ❌（`get_code_limits()` 返回 None） |
| `shell_exec` | ✅ | ❌ | ❌ |
| 角色面工具（`delegate_task` / `give_gift` / `create_image` / `send_voice` / `recall_asset`） | 动态 | 动态 | 动态 |
| `create_character` / `complete_scene` / `end_continuous_mode` | 场景动态 | 场景动态 | 场景动态 |

> 📌 **实现细节**：静态集合共 32 项（public 27 / vip 4 / admin 1），角色可用数为 admin 32、vip 31、regular 27；另外 8 个工具不在静态集合中，只经 `allowed_tools` 按场景下发（角色面 / 创作会话 / 剧情会话 / 连续对话），执行层硬拒绝越权调用。
> `read_file` 的图片/音频 AI 分析**没有角色门**（只要对应模型段配置可用即可），权限表只限制文本/PDF 之外工具本身的可见性——文档与注释里"仅 VIP"的说法以实现为准。

#### execute_code 分级限制

| 参数 | 管理员 | 会员 |
|------|:---:|:---:|
| 最大超时 | 60s | 15s |
| 输出上限 | 16KB | 16KB |
| 内存提示 | 256MB | 128MB |

> 输出上限自缓存优化 Phase 6-B1 起统一压到 16KB（超长 stdout 会在每轮工具循环里重复计费，模型也消化不了）；`max_memory_mb` 目前仅用于展示（`get_user_info`），未落 rlimit。

#### 资源配额

| 资源 | 管理员 | 会员 | 普通用户 |
|------|:---:|:---:|:---:|
| 工作区磁盘配额 | 2 GB | 500 MB | 100 MB |
| 最大特殊会话数 | 10 | 3 | 1 |
| 自创角色数 | 10 | 3 | 1 |
| 每日画图 / 语音配额 | 50 / 50 | 10 / 10 | 3 / 3 |
| 影音资产磁盘配额 | 1000 MB | 300 MB | 100 MB |

工作区另有 80% 预警与超限清理协议（10 分钟未响应则按最旧文件自动清理到 80% 以下）。

#### 身份识别（按平台切换）

| 平台 | 管理员 | 会员 | 解析方式 |
|---|---|---|---|
| OneBot（QQ 号） | `SUPERUSERS` | `VIP_USERS` | 逗号分隔，逐项 `isdigit()` 过滤 |
| QQ 官方（openid） | `QQ_OFFICIAL_SUPERUSERS` | `QQ_OFFICIAL_VIP_USERS` | 逗号分隔，**不过滤数字**（openid 是字母数字串） |

- 平台由回合开始时设置的 `_current_platform` contextvar 决定（`run_agent_turn` / `run_continuous_turn` 各设一次，默认 `"onebot"`）；
- `.env` 由 `PermissionManager` **每次调用直接读文件**（NoneBot 不一定把 `.env` 灌进 `os.environ`），所以面板改权限名单无需重启；
- 管理员优先于会员；其余为普通用户。

#### 权限传递机制

```
turn.py / 入口插件
  ├── PermissionManager.get_role(user_id) → UserRole        （按 _current_platform 选表）
  ├── _current_user_role.set(role.value)                    ← contextvar
  ├── _current_code_limits.set(limits_dict)                 ← 仅 admin/vip 有值
  └── agent.run(message, user_id, allowed_tools=..., user_role=...)
        ├── get_schemas_for(allowed_tools)                  ← LLM 只看到允许的工具
        └── _execute_tool_calls()                           ← 硬拦截二次校验
              └── execute_code() → _get_code_limits()       ← 读取 contextvar 应用分级限制
```

#### 设计原则

1. **Schema 过滤为主**：LLM 看不到越权工具就不会调用，从根源避免权限违规；
2. **硬拦截为纵深防御**：`_execute_tool_calls()` 检查 `allowed_tools`，即使 schema 过滤出现 bug 也能兜底；
3. **身份只来自环境变量**：没有任何 QQ 命令可以提权，防止社会工程；
4. **双渠道不串号**：openid 与 QQ 号各自使用独立名单，官方渠道用户不会意外匹配到 OneBot 管理员；
5. **权限不足不暴露系统能力**：Agent 被告知权限受限时应说"当前账户权限不支持"，而非"系统没有此功能"。

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

### 2.8 `plugins/agent_router.py` — OneBot 入口与后台任务

> 说明：v2.31 起**回合内核**已迁移到 `agent/turn.py`（见 §2.10），本插件只保留 OneBot 平台胶水与后台任务；QQ 官方入口见 `plugins/qq_official.py`。

#### 职责

| 职责 | 说明 |
|---|---|
| OneBot 匹配器 | `agent_router`（priority=1，手工判群 @，不依赖 NapCat `to_me`）、`continuous_router`（priority=2，连续对话窗口）、`poke_router`（priority=5，戳一戳通知） |
| 事件转换 | `adapters/onebot.py::build_view` → `agent.inbound.InboundMessage` → `run_agent_turn()` |
| 后台任务 | 配置看门狗（三组 mtime）、更新记录/公告双渠道群发轮询器（`pending_broadcast.json` 认领制）、群列表导出（OneBot `get_group_list` + 官方群登记表合并 → `known_groups.json`） |
| Sender 槽位 | 导入时把 `OneBotSender` 写入 `turn.default_sender / continuous_sender / poke_sender`，内核据此发送 |

#### 消息处理

| 处理程序 | 触发条件 | 说明 |
|---|---|---|
| `agent_router` | `on_message(priority=1, block=False)` | 捕获所有消息；群消息要求 @ 机器人（或 `is_tome`） |
| `continuous_router` | `on_message(priority=2)` | 仅群聊、仅连续窗口内、且未 @ 机器人；续期 90 秒窗口 |
| `poke_router` | `on_notice(priority=5)` | 仅"戳了机器人自己"；`split` 人格下结算好感并回角色口吻反应 |

#### 直连命令一览（实现位于 `agent/turn.py`）

| 类别 | 命令（含别名） |
|---|---|
| 会话 | `/新会话`、`/会话列表`（`/会话`）、`/切换会话`、`/重命名会话`、`/删除会话`（回复「确认删除 <名称>」）、`/压缩会话`（回复「确认压缩会话」；仅私聊，剧情/创作会话不支持）、`/结束会话`（`/临时会话`/`/退出会话`/`/退出特殊会话`）、`/保存为会话`、`/clear`（`清除上下文`/`新对话`）、`/帮助`（`/help`/`/命令`）、`/status` |
| 人格 | `/personality`、`/人格切换`（均支持带名称；无 `#` 变体；特殊会话内只写会话绑定，`--全局` 后缀改写用户默认） |
| 养成 | `/好感度`（`#好感度`/`/bond`）、`/剧情`、`/设好感 <1-10>`、`/创建角色`、`/删除角色`（回复「确认删除角色 <名称>」）、`/设形象`、`/形象`（`/查看形象`）、`/清除形象`（`/删形象`）、`/邮件`（角色信箱，仅官方单聊：列表/`下一页`/`p<N>`/`MM-DD`/信件编号看全文并标记已读；系统邮件带【系统】前缀，面板删除过的信件显示「已被撤回」） |
| 游戏 | `/兑换码`（`/redeem-code`）、`/长期兑换码`、`/角色详情 <名>`、`/羁绊详情 <名>`、`/刷新角色数据`（仅 admin） |
| 群管理 | `/toggle`、`/toggle <gacha\|image\|voice> <on\|off>`、`/toggle personality [名称\|默认]`（仅群聊 + admin） |
| 其他 | `/功能`（`/features`）、`/更新日志 [d]`（多个别名）、`/管理工作区`（落入 agent）、`/取消`（`#取消`/`/结束`/`#结束`，连续对话窗口内）、`/免责声明`（`#免责声明`；查看/`关`/`开` 免责尾追加，三级用户均可，按渠道独立） |
| 反馈 | `#反馈 <内容>`、`#bug <描述>`、`#建议 <内容>`（**必须用空格分隔**，且三者都零 token 直达开发者） |

> 分发顺序（命中即 return）：反馈 → `/取消` → `/toggle` → `/兑换码` → `/角色详情` → `/羁绊详情` → `/邮件` → `/免责声明` → `/刷新角色数据` → `/功能` → `/更新日志` → `/人格切换` → `/好感度` → `/设好感` → `/剧情` → 会话族 → 形象族 → 元命令（`/clear`、`/status` 等）→ Agent。
> 特例：`/帮助` 由会话族处理（渲染 HELP.md 卡片）；`/管理工作区` 与私聊 `/取消` 会落入 Agent。

#### 已注册工具（40 个）

静态工具 32 个（注册于 `agent/turn.py::_build_tool_registry()`，权限集合在 `agent/permissions.py`）：

| 分组 | 工具 |
|---|---|
| 信息与通用 | `search_web`、`get_time`、`read_file`、`summarize_pdf`、`delete_workspace_file`、`get_user_info`、`recall_search_result`、`get_changelog` |
| 地图天气 | `geocode`、`reverse_geocode`、`get_weather`、`search_poi`、`plan_route` |
| 游戏 | `gacha_pull`、`play_gacha_animation`、`calculate_speed`、`compare_speed_probability`、`parse_battle_screenshots`、`character_detail`、`bond_detail`、`redeem_code` |
| 文本 | `explain_code`、`translate_text` |
| 记忆编排 | `begin_task`、`finalize_subtask`、`recall_memory`、`story_search` |
| 会员（vip） | `web_fetch`、`download_repo`、`get_system_load`、`execute_code` |
| 管理员（admin） | `shell_exec` |

动态工具 8 个（不在静态集合中，按场景经 `allowed_tools` 下发）：

| 工具 | 下发场景 |
|---|---|
| `delegate_task`、`give_gift`、`create_image`、`send_voice`、`recall_asset` | 角色面（`SOCIAL_TOOLS`） |
| `create_character` | 仅在 `char_creation` 创作会话 |
| `complete_scene` | 仅在 `bond_scene` 剧情会话 |
| `end_continuous_mode` | 仅在连续对话窗口 |

> 📌 `story_search` 虽在 `_PUBLIC_TOOLS`，但 schema 会按人格白名单裁剪（非白名单人格看不到该工具）。
> 注册位置：`_build_tool_registry()` 33 个 + 模块级 7 个（`get_user_info` / `end_continuous_mode` / `begin_task` / `finalize_subtask` / `recall_search_result` / `recall_memory` / `story_search`）。

#### 配置看门狗（热重载）

`_start_config_watcher()`（`plugins/agent_router.py` 的 `@get_driver().on_startup`）每 5 秒轮询三组文件的 mtime（各组独立 5 秒冷却），配合 WebUI 面板实现保存即生效、无需重启：

| 监听对象 | 变化时的动作 |
|---|---|
| `agent/config/**/*.md`（含 `personalities/`） | `agent.reload_configs()` 重建系统提示词（只有 SOUL/IDENTITY/AGENTS/MEMORY 四个注入文件会即时改变行为；HELP/FEATURES 在下次命令触发时生效） |
| `config/characters/character_dic.json` / `bonds_search_dic.json` | `NameResolver.reload()` + 角色/羁绊详情缓存失效（面板「Wiki 缓存」页可编辑别名） |
| `config/models_settings.json` | `model_router.reload()`（含模块级单例与 `MultimodalClient`）+ 重挂固化 client 引用（`agent.client` / `_profile_manager.set_client` / `_special_sessions.set_client`），约 10 秒生效 |

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


### 2.10 `agent/turn.py` — 平台无关内核

v2.31 起，原本集中在 `plugins/agent_router.py` 的回合逻辑被整体搬迁到这里，使 OneBot 与 QQ 官方两条入口共用同一内核（入口插件只保留平台胶水）。

| 职责 | 说明 |
|---|---|
| 回合编排 | `run_agent_turn(view)` / `run_continuous_turn(view)`：平台上下文、权限、工作区、群功能限制、双平面选面、Agent 调用、收尾结算 |
| 命令直连层 | 反馈 → `/取消` → `/toggle` → `/兑换码` → `/角色详情` → `/羁绊详情` → `/邮件` → `/免责声明` → `/刷新角色数据` → `/功能` → `/更新日志` → `/人格切换` → `/好感度` → `/设好感` → `/剧情` → 会话族 → 形象族；随后元命令 `/clear`、`/status` 等 |
| 发送语义 | `_safe_send()` / `_send_response()` / `_send_text_chunks()`：文本、图片、语音、进度统一走 `Sender`；300 字符分块 + 1s 间隔 |
| 上下文 | 用户/角色/工作区/平台/群/Sender/人格/羁绊/剧情/委派深度等 contextvars 在回合开始时统一设置 |
| 单例装配 | `Agent`、`SessionManager`、`SpecialSessionManager`、`TieredMemory`、`ProfileManager`、`BondManager`、`AssetMemory`、`CharacterRegistry`、`ToolRegistry` 在此构造并互相注入 |

**关键常量**：任务开窗 TTL 900s、近期文件表 200 条、配额清理超时 600s、临时会话升级提示阈值 15 条（每 10 条复提）。

### 2.11 `adapters/` + `agent/inbound.py` + `agent/outbound.py` — 平台适配层

| 模块 | 职责 |
|---|---|
| `agent/inbound.py` | `InboundMessage`（平台无关视图）+ `Attachment`（kind/path/name/error/quoted）+ `AttachmentResolver` 协议 |
| `agent/outbound.py` | `Sender` 协议 + `OutImage` / `OutAudio` / `OutProgress` + `SendFailed`（唯一可重试错误） |
| `adapters/onebot.py` | OneBot 事件 → 视图；附件下载（`get_file`/`get_record` + HTTP 兜底）；引用消息解析；`OneBotSender` |
| `adapters/qq_official.py` | 官方事件 → 视图（剥离内联表情伪 XML）；公网 URL 附件下载；引用消息（`message_type=103` + `msg_elements`）；`QQOfficialSender`；主动消息 `send_active_c2c` / `send_active_c2c_media` / `send_active_c2c_markdown`（信封阶梯：kb→md→plain，喂共享熔断器，40034029/304062 剪按钮重试） |

**官方渠道的现实约束**（代码注释与实现）：

- 无法读本地文件：图片/语音必须 base64 上传（`MessageSegment.file_image/file_audio`，单文件 >5MB 且有公网 URL 时改走 URL 上传，URL 软限 20MB）；
- 被动回复配额极小：单聊 4 次/60min、群 5 次/5min，所以 Sender 采用「缓冲 → 合并 → 超配额丢弃 + WARNING」；
- 语音条被平台拒收（如 850019）时降级为文件卡（`file_type=4`），用户仍能拿到 mp3；
- 「正在思考…」是唯一值得即时占用配额的进度行（`OutProgress.immediate=True`），其余进度行默认吞掉。

### 2.12 `agent/bond.py` + `agent/bond_content.py` — 角色养成核心

三值模型：

| 维度 | 范围 | 规则 |
|---|---|---|
| Bond（长期） | Lv1–Lv10（陌生→相识→熟悉→朋友→密友→知己→暧昧→恋人→誓约→灵魂伴侣） | 只涨不掉，永不衰减；阈值见 `_DEFAULT_LEVELS` |
| Mood（短期） | 0–100 | 围绕基线 60 回归（5/h），影响加经验系数 0.8–1.2 与提示词语气行 |
| Stats（陪伴） | 相识天数 / 累计消息 / 礼物数 | 驱动重逢与纪念日判定 |

**加经验**：每日首聊 20、每消息 3（心情系数后 2–4）、每日聊天软上限 150（超出减半、2 倍后 1/4）、≥10 轮长会话 +30（每日一次）。
**互动**：送礼三档（喜爱 30 / 普通 15 / 讨厌 5 经验，每日 3 次）、戳一戳（+5 经验、+3 心情，每日 3 次）、称呼演化、按级解锁档案。
**剧情**：`complete_scene(title)` 结算（+100 经验、+10 心情），记录完成状态（`has_done_scene` 真源），供 `/剧情` 门槛判定与纪念卡使用。**剧情场景生命周期**：`/剧情 <标题>` 启动 → 建 `kind=bond_scene` 会话（钉住 `char_id`，不占普通配额）→ 分幕共同演绎 → LLM 调 `complete_scene` 收尾 → `turn.py::_finish_bond_scene` 结算呈现（升级通知 + 羁绊纪念卡 + 事件蒸馏）并写 `metadata.completed=true` **封档**（会话保留、`end_active`、可 `/切换会话` 回看；回看态 `is_scene=false`）。`/剧情 <同标题>` 命中既有会话时**恢复**而非新建；剧情会话总量受 `MAX_SCENE_ARCHIVES`（默认 20）约束。详见 `docs/scene-session-archive-design.md`。
**重逢 / 纪念日**：≥14 天未见触发重逢标记；相识 30/100/365/730/1095 天里程碑。
**媒体门槛与配额**：画图 Lv4、语音 Lv5、主动语音 Lv7；每日配额 admin 50 / vip 10 / regular 3；影音资产磁盘配额 1000/300/100MB。
**持久化**：`data/bonds/{user_id}.json`（按角色分区），进程内缓存 + `st_mtime_ns` 校验；数值可被 `config/bond_settings.json` 覆盖（该文件当前不存在，走默认值；`config/bond_settings.example.json` 提供角色信箱 `mail_*` 组示例，复制后按需修改）。

### 2.13 `agent/character_registry.py` + `agent/personality.py` + `agent/portrait.py` — 人格与 UGC 角色

| 模块 | 职责 |
|---|---|
| `PersonalityManager` | 内置人格目录（`agent/config/personalities/*.md`）、卡片 HTML 注释元数据（`mode: split\|unified`、`task_voice`、`voice_id`）、用户级/群级绑定、模糊匹配与歧义拒绝（`CharacterRegistry` 基类） |
| `CharacterRegistry` | 在人格之上叠加 UGC：`data/characters/{char_id}/card.md + meta.json`；按作者可见性隔离；解析显示名/模式/参考立绘/声纹；`_discover()` 仍只列内置，避免影响默认回退链。**人格解析单点**：`resolve_effective_personality` 五层链（见下）+ `resolve_effective_source` 来源标签（剧情钉住 / 剧情归档〔已封档剧情〕/ 会话绑定 / 个人设置 / 群默认 / 全局默认） |
| `portrait.py` | 参考立绘规整纯函数：解码校验、EXIF 旋转、alpha 铺白、长边 ≤1536（兜底 1024）、最小边 128、输出 ≤4MB |

**参考立绘双层路径**：出厂层 `agent/config/portraits/`（内置人格随仓库分发）+ 覆盖层 `CHARACTERS_DIR/_builtin/<key>/assets`（管理员上传）。
**声纹解析链**：UGC meta → 角色卡注释 → `voice_pool` 按 `blake2b(char_id)` 取模 → 默认音色，保证同一角色声线稳定。

**会话人格绑定**（2026-10-27）：人格注册在特殊会话上（`_index.json` entry 的 `metadata.persona_key`），不再只是用户级默认。
- **解析优先级**：`剧情钉住（bond_scene char_id）> 会话绑定（persona_key）> 个人设置 > 群默认 > 全局默认`；绑定 key 失效（UGC 角色被删）时懒校验穿透到下一层，存量无绑定的旧会话零迁移。
- **provider 注入 = 单点解析**：`turn.py` 初始化时 `set_session_provider(lambda uid: _special_sessions.get_active(uid))`，15+ 个 resolve 调用点与连续会话路径零改动自动一致；注释掉注入行即整体回滚（provider=None → 会话层跳过）。
- **切换规则**：特殊会话内 `/人格切换` 只写本会话绑定（经 `update_metadata`），`--全局` 逃生门改写用户默认；临时会话不绑定人格，其中切换仍写用户默认；剧情/角色创作会话拒绝切换（`--全局` 放行）。
- **删除角色联动**：`CharacterRegistry.purge_references(char_id)` 清理**用户默认人格**与**群默认人格**中指向该角色的条目（`/删除角色` 回复「已解除 N 处人格设置引用」）；会话级 `metadata.persona_key` 不批量清理，由 `_exists` 懒校验自动穿透；好感数据（`data/bonds/`）保留，重逢时仍在。
- **创建快照**：`/新会话`、`/保存为会话` 在 create/switch_to **之前** resolve 当前生效人格写入 `persona_key`；bond_scene 冗余写 `persona_key=char_id`（展示统一读 persona_key，解析按 kind 优先 char_id）。
- 切会话/结束会话发人格提示消息但不注入 personality marker；`/clear` 清历史保留绑定。

**面板写路径**（`docs/character-admin-ui-design.md`，`webui/character_admin.py`）：UGC 卡的编辑统一走
`CharacterRegistry.update_character(char_id, card_content=…, meta_patch=… | meta_replace=…)` ——
写盘用 tmp + `os.replace`，**mtime 必然变化**，于是 bot 进程里 `load()` 的 mtime 缓存自动失效
（这正是「面板↔bot 缓存一致性」教训的正解；反例是用 `copy2` 恢复备份，它会还原 mtime 让 bot 继续读旧卡）。
`card.md` 的段落编辑由 `webui/card_parser.py` 做**偏移切片**：只替换被改段落所在的字符区间，
其余段落字节级不变（围栏代码块内的 `#` 不当标题、`###` 归父段），保存时行尾统一 LF。
面板侧额外负责：每写必先全量快照（`webui/data/backups/characters/<char_id>/<ts>/`）、
sha256 乐观锁（并发改 → 409）、保存前静态体检（L1–L14：非 UTF-8 / 无 H1 / 体积 / 元数据值域等 error 阻断）、
版本列表与一键回滚。生效时机：**`card.md` 下一轮对话生效**，`meta.json`（显示名/音色/可见性）立即生效。

### 2.14 `agent/asset_memory.py` + `lib/media_gen.py` + `lib/image_parts.py` — 影音资产与生成

| 模块 | 职责 |
|---|---|
| `asset_memory.py` | 资产卡（markdown + frontmatter）为**本体**，二进制是可淘汰缓存：建卡 / 读卡 / 列表 / `prompt_hash` 去重（168h 窗 + 4096 索引护栏）/ 关键词检索 / LRU 淘汰 / 事件蒸馏（≤300 字）/ 审计单行追加 / L4 注入（top 5） |
| `media_gen.py` | DashScope 生成运行器：qwen-image（文生图与参考图 I2I 共用端点，同步返回图片 URL）+ qwen-audio-tts（`SpeechSynthesizer`，音频 URL 仅 24h → 必须立即落盘）；进程级并发闸 2、硬超时（图 60s/TTS 30s）、一次重试、TTS 硬顶 600 字、生成前 LLM 内容审查、失败统一返 None |
| `image_parts.py` | Direct Vision 编码：本地图 → base64 `image_url` part（最长边 1568、≤20MB 原始、≤5MB 编码后、≤4 张/轮、GIF 取首帧、魔数嗅探 MIME、损坏静默跳过） |

**存储布局**：`data/assets/{uid}/cards/{asset_id}.md`（卡，永不淘汰）、`data/assets/{uid}/{char_id}/{asset_id}.png|mp3`（二进制）、`data/assets/_dedupe_index.json`、`data/assets/audit.log`。

### 2.15 `lib/story_kb.py` — 原作剧情知识库

- **构建**：`scripts/build_story_kb.py` 四阶段（crawl → parse → index → enrich）产出 `data/story_kb/story.db`（当前约 11.7 MB）。
- **检索**：SQLite FTS5 双索引 —— 中文按重叠 bigram（`unicode61` 无 CJK 分词器），英文 `unicode61` + BM25 权重；两路排序经 RRF（k=60）融合，再按「角色出现在场景 / 主线优先」等加分重排；候选召回放大 `max(limit*6, 24)`，查询词上限 24。
- **注入**：`format_injection(limit=2, max_chars=2200)`，仅在人格白名单（默认 `{rubi, assistant}`，见 `config/story_personas.json`）且有匹配时进入 L4；UGC 等解析不到剧情角色的情况**不做检索**，避免跨角色污染。
- **工具**：`story_search`（public，但 schema 按人格裁剪），返回带出处的检索结果。
- **测试**：`test_story_kb.py` 6 个类 / 37 个用例，离线合成语料建临时 SQLite。

### 2.16 `agent/delegation.py` + `agent/greeting.py` + `agent/mailbox.py` — 双平面与主动消息（角色信箱）

- `delegation.py`：`SOCIAL_TOOLS` 白名单、`run_delegated_task`（中立 Roxy、`persist=False`、权限过滤、单层嵌套、默认 240s 超时）、`revoice_task_result`（角色开场白 + 正文透传，群聊用固定模板）、`repatch_last_assistant`（把持久化历史改写成角色口吻）。
- `greeting.py`：官方 c2c 主动消息簿记（纯函数、可注入 rng/now）。候选索引 `data/bond_greet_targets.json` 只由 **c2c 入站**写入（群 member_openid 不能用于 c2c 主动消息）；配额 `data/official_active_quota.json` 按自然日/月滚动，文本与媒体**分账**（日配额各自独立；信箱信封走 `monthly_cap=10**9` 免月配额——平台已无主动消息月限额）。旧问候选靶四门（等级 ≥5、冷却 7 天、概率 0.25、批量 ≤5）保留为 `mail_enabled=false` 时的回滚路径。
- **角色信箱（proactive-mail-design.md v0.5，v1）**：`plugins/qq_official.py::_daily_active_loop` 每日 `mail_hour`（默认 10 点，+0–600s 抖动）先跑 mail pass 再跑 media pass，启动时若在 10–22 点窗口内做减半补发。mail pass 三段式：A 收集候选（串行本地读，先按**并集**播种 `known_personas`——会话索引 `metadata.persona_key`（`_known_personas_from_sessions`）∪ 羁绊文件中 msgs>0 的角色（`_known_personas_from_bonds`，§4.2 权威源；兜住「靠默认人格攒羁绊、特殊会话没绑 persona_key」的用户，否则他们永远过不了 known 门、信箱永不建档）——再跑 `_mail_candidates` 过滤链：非 assistant、`split` 模式、Lv≥5、有消息、在 known_personas、沉默 ≥24h、人格 7 天冷却，按等级降序）→ B 生成（跨用户 `Semaphore(4)`，用户内串行，角色面 flash 写 ≤60 字留言，失败即丢弃**无模板兜底**，pass 预算 20 分钟）→ C 发送（串行，令牌桶 10 qps + 2–6s 随机间隔，`send_active_c2c_markdown` 信封 = markdown 摘要 + 「展开」按钮，共享 md/kb 熔断器逐档降级纯文本）。发送成功才记账：`record_digest`（同日幂等）+ 配额（kind=text）+ bond 事件（信件全文）。与媒体 pass **双向互斥**（同日只发一类）。`C2C_MSG_REJECT`/`RECEIVE` 事件（`on_notice`）翻 `active_enabled`，拒收即停发。
- `mailbox.py`：信箱存储（纯同步、可单测）。每用户一 JSON（`data/mailboxes/{openid}.json`，原子写、损坏降级空箱）；信件 28 天保留 + 200 封上限裁剪；`letters`（id `L-YYYYMMDD-<4hex>`、preview、read）与 `digests`（id `D-…`，1 digest = 1 次主动调用）；`make_preview`（首行去舞台指示、20 字 + …）、`escape_md_text`（全角转义）为纯函数。`/邮件` 命令（turn.py 直连层）读侧再过滤保留期。面板邮件管理追加三个写入口：`delete_letter`（**撤回**：从 letters[] 移除但信封 letter_ids 保留该 id → `is_withdrawn()` 成立，`/邮件 <id>` 显示「这封信已被撤回。」）、`write_system_letter`（系统信：persona=`__system__`、`system=true`，不写 personas/known_personas、不动信封记账）、`set_active_enabled`。
- **邮件管理队列（§16.2，面板→bot 文件 IPC）**：`plugins/qq_official.py::_mail_admin_worker` 每 3–5s 扫 `data/mail_admin/queue/`，`os.replace` 改名到 `processing/` 原子认领，执行后回写 `status/`（操作 id `mailop_YYYYMMDD_<4hex>` 幂等；状态保留 7 天由 bot 清理）；操作类型白名单 `delete_letter` / `send_system`。系统邮件经 `send_active_c2c_markdown` 真实主动发送（markdown 信封样式 `## 系统邮件 · {title}`，共享熔断降级纯文本），**成功才** `write_system_letter` 入账；用户已拒收（`active_enabled=false`）→ 发送失败不入账。启动时 `_mail_admin_recover` 把上次进程中断的残留认领标记 `failed: bot restarted`。worker 不受 `mail_enabled` 开关影响（撤回/系统邮件独立于每日信封）。
- `rate_limit.py`：`TokenBucket`/`ChannelLimiter`（c2c 10/s、group 1/s，按渠道懒建复用，可注入 monotonic）。
- `group_registry.py`：官方群登记表 `data/group_registry.json`（v1 纯记账）：`GROUP_AT_MESSAGE_CREATE` 入站 upsert（60s 粒度节流防文件 churn），status `active/left/unknown` 状态机 + `active_enabled` 开关，为 P1 群主动消息预铺。

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

所有 Markdown 配置文件位于 `agent/config/`，共 12 个 + `personalities/`（人格定义）与 `portraits/`（内置人格出厂立绘）。另外 `config/` 目录下有 JSON 配置文件，运行时敏感配置（密钥 / 服务地址）在 `.env`。

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
| `story_personas.json` | 跟踪 | 剧情知识库的人格白名单与角色映射（默认 `rubi` / `assistant`） |
| `translation_glossary.json` | 跟踪 | 角色/羁绊数据翻译对照表（抓取更新时使用） |

### 环境变量（`.env`）

`.env` 是运行时敏感配置（密钥 / 服务地址），逐行说明见 **`QQBot/.env.example`**（可直接 `cp .env.example .env`）。模型凭据遵循「`models_settings.json` 优先、`.env` 回退」：某模型段在 JSON 中填了 `api_key`/`api_base` 即覆盖 `.env` 的 `DEEPSEEK_API_KEY`/`DEEPSEEK_API_BASE`；仅 JSON 段留空时才回退到 `.env` 的 DeepSeek 配置。

非模型关键变量包括 `ONEBOT_ACCESS_TOKEN`、`SUPERUSERS`/`VIP_USERS`、`AMAP_API_KEY`、`USER_DATA_ROOT`、`MAX_SPECIAL_SESSIONS`、`USER_WORKSPACE_QUOTA_MB`、`BUFF_DETECTOR_WORKERS`（团战测速 buff 图标匹配并行线程数，默认 2）。

---

## 五、工具实现

### 5.1 `tools/builtin_tools.py` — 内置工具 (9 个) + 搜索存档召回

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

### 5.8 `tools/changelog.py` — 更新记录 + 公告 (纯 IO/格式化)

面向用户的「更新记录」与「公告」读写与格式化模块，无 nonebot 依赖，被多处共用：`/更新日志` 命令拦截、`get_changelog` LLM 工具、群发轮询器。两类数据分文件存于 `data/changelog/`：
- **更新记录** `changelog.json`（`{entries: [{version, date, changes: [{type, text}], created_at, broadcast_at}]}`，数组新→旧）——结构化变更条目。
- **公告** `announcements.json`（`{entries: [{id, title, body, format, created_at, updated_at, sent_at?, last_targets?}]}`，按服务端生成的 `id` 定位）——自由标题/正文/markdown，§16.3。

- **变更类型** `CHANGE_TYPES = ["新增", "修复", "优化", "调整", "移除"]`；面板下拉选择。
- **读取**：`load_entries()` / `get_recent(d=3)`（clamp 1..20，按 `created_at` 降序）；`load_announcements()` / `get_announcement(id)` / `mark_announcement_sent(id, targets)`。
- **格式化**：`format_for_qq(entries, requested, site_url=None)` 生成 QQ 结构化纯文本（`【版本】 日期` 标题 + `  • 类型：内容` 条目，不含 markdown）；`format_announcement(entry, site_url=None)` 用于更新记录群发（加 `📢 更新公告` 头）；`format_announcement_text(ann)` 用于自由公告群发（`📢 公告\n{title}\n\n{body}`）。`site_url` 非空时在末尾追加一行公开站点指路（`📖 使用说明与演示：<url>`，纯文本）。空数据返回「暂无更新记录」提示行。
- **图片卡片**：`card_renderer.render_changelog_card(entries, out_path=None, title="更新记录", site_url=None)` 复用 help/feature 卡骨架渲染暗色 PNG（版本 pill 循环色 + 变更类型 badge 色 + 像素宽换行），命令/群发在文本后 best-effort 追加发送；`entries` 空返回 `None`。
- **面板↔bot 通信**：`write_known_groups`/`read_known_groups`（群列表：OneBot `groups` + 官方 `official_groups` 双段）、`claim_pending`（原子改名认领群发请求 `{kind: changelog|announcement, ref, targets}`）/`finish_pending_claim`/`recover_stale_claim`（启动清理崩溃残留的 `.processing`）、`write_status`/`read_status`（群发结果回写，含 `kind`/`ref`/`skipped`/`by_channel`）、`mark_broadcast`（更新记录：仅当至少成功发出一群时回写 `broadcast_at`）。
- **原子写** `_atomic_write_json`（tmp + `os.replace`）贯穿所有写操作。
- 详细的面板/后台任务/文件队列/双渠道分流设计见 §十二「更新记录 / 公告群发」。


### 5.9 `tools/social_tools.py` / `tools/creation_tools.py` — 养成与创作工具

这两个模块实现的工具由 `turn.py` 以闭包方式注册（需要注入 `BondManager` / `AssetMemory` / `CharacterRegistry` / LLM 客户端），**只在对应场景经 `allowed_tools` 下发**：

| 工具 | 模块 | 职责 |
|---|---|---|
| `give_gift` | social_tools | 送礼结算：按角色卡礼物偏好分三档加经验与心情，受每日上限约束 |
| `create_image` | social_tools | 角色画图：等级门（Lv4）+ 每日配额 + `prompt_hash` 去重 + 参考立绘图生图 + 资产卡落盘 + 磁盘配额淘汰 |
| `send_voice` | social_tools | 角色语音：等级门（Lv5）+ 声纹解析 + 台词内容审查 + TTS + 资产卡落盘 |
| `recall_asset` | social_tools | 按记忆召回旧图/旧语音（含"二进制已淘汰但资产卡仍在"的文本回忆） |
| `complete_scene` | social_tools | 剧情场景收尾：结算经验/心情、标记完成、生成纪念卡素材 |
| `create_character` | creation_tools | 创作访谈落库：字段清洗与校验（`validate_fields`）、角色卡构建（`build_card`）、LLM 审核（`review_card`）、声纹分配（`assign_voice_id`）、注册到 `CharacterRegistry` |

辅助函数（非工具）：`build_image_prompt`（外观锚 + 场景 + 显示名拼装）、`enforce_asset_quota`（按角色判定资产磁盘配额并触发 LRU 淘汰）。

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
turn.py / agent_router（OneBot 入口）: on_message(priority=1)
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

**Thinking Mode 处理**: 多模态调用为单轮请求（无对话历史），`reasoning_content` 无需回传，不会触发 API 400。`analyze_image`/`analyze_audio` 默认**丢弃** `reasoning_content`，只返回正文——因为下游程序性消费方（资产卡文本化 `_textualize_upload`、OCR 名称匹配、read_file 懒分析）会对返回文本做定长截断/关键词解析，思考前缀会把它们全部带偏（曾导致 user_upload 卡在 `[思考]` 中间被切断、正文丢失）。仅当显式传 `include_reasoning=True`（人工调试）时，才以 `[思考]...[回复]...` 格式拼接返回。作为纵深防御，`turn._clean_textualization` 仍会剥离 `[思考]/[回复]` 脚手架、丢弃残留 `[思考]`/`[DEBUG]` 前缀串，并在句末标点处收口截断。

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


### 8.4 Direct Vision（主模型直连识图）

除"图片 → 视觉模型 → 文本"的老链路外，主对话模型可直接看到当轮图片：

```
用户上传/引用图片
   └─ turn.py 落盘（uploads/）+ 生成占位文本行（历史里只存文本，base64 不落盘）
        └─ 若本轮选中的模型段 supports_vision=true
             └─ lib/image_parts.build_image_parts()
                  ├─ 魔数嗅探 MIME（QQ 下载常无扩展名）
                  ├─ 最长边 1568px LANCZOS 缩放；GIF 取首帧；alpha 铺白
                  ├─ 单图 ≤20MB 原始 / ≤5MB 编码后；每轮 ≤4 张；损坏静默跳过
                  └─ 拼进当轮 user 消息的 image_url parts（只在内存消息里）
```

- 配置：`models_settings.json` 的模型段加 `"supports_vision": true`（显式 opt-in，默认 false；面板 REASONING/FLASH 卡片有勾选框）；
- 自愈：首轮若返回 `API HTTP错误 (400)`，自动剥离图片重试一次纯文本（避免历史图片/模型不支持导致整轮失败）；
- 回退：未开启、编码失败或超限时，仍走占位文本 + `read_file` 懒分析（多模态模型）老路径。

### 8.5 影音生成（画图 / 语音）

| 项 | 说明 |
|---|---|
| 配置段 | `IMAGE_GEN_MODEL`（qwen-image）/ `TTS_MODEL`（qwen-audio-tts），DashScope 原生接口，每次调用现读配置（面板热重载即时生效） |
| 图像 | 文生图与参考图（图生图）共用 `multimodal-generation` 端点；同步返回图片 URL，立即下载落盘 |
| 语音 | `SpeechSynthesizer` 端点，`voice` 来自角色声纹（`voice_pool` 分配 + 角色卡/UGC meta 覆盖）；返回音频 URL 仅 24h，必须立即落盘 |
| 门槛 | 画图 Lv4、点播语音 Lv5、主动语音 Lv7（`media_image_min_level` 等可配） |
| 配额 | 每日 admin 50 / vip 10 / regular 3（图、语音各自计算）；资产磁盘 1GB/300MB/100MB |
| 安全 | 台词先过 LLM 内容审查（超时/失败按策略降级）；TTS 硬顶 600 字；并发闸 2；硬超时（图 60s / TTS 30s） |
| 失败 | 任何失败返回 None，调用方降级为纯文本回复，不阻断回合 |

### 8.6 多模态资产记忆

- **资产卡**（`data/assets/{uid}/cards/{id}.md`）：markdown + frontmatter，记录类型、角色、模型、prompt、台词、标签、`prompt_hash`、`proactive` 标记 —— 这是"本体"，永不淘汰。
- **二进制**（`data/assets/{uid}/{char_id}/{id}.png|mp3`）：可被 LRU 淘汰（score = recall×exp(-age/30)），淘汰后资产卡仍在，角色可"凭记忆描述"。
- **去重**：`prompt_hash`（blake2b-16）窗口 168h，命中直接复用旧图/旧语音，避免重复计费；索引超过 4096 条丢最旧一半。
- **注入**：`build_asset_injection(top_k=5)` 把最近/常被回指的资产以文本摘要注入 L4，供角色主动提及"上次那张图"。
- **召回**：`recall_asset` 工具按关键词/时间召回；上传的图片/语音也会被文本化（≤60/≤80 字）后入卡。
- **事件蒸馏**：里程碑（场景收尾、升级）蒸馏为 ≤300 字事件文本进三层记忆（`category=event`）。

---

## 九、测试系统

`test_agent.py` 包含 **56 个测试套件 / 354 个测试用例**（全部离线，使用 mock client，无网络依赖），覆盖核心组件、养成、双平面、官方渠道解析、影音资产、Direct Vision、会话人格绑定、会话压缩、官方渠道流式消息与面板逻辑。下表保留 v2.x 时代的 15 个基础套件（编号 1–15），新增套件按主题列在表后:

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

**新增套件（16–55）**

| 主题 | 套件 |
|---|---|
| 养成 / 角色 | `TestBondManager`、`TestBondContent`、`TestBondGiftPoke`、`TestBondEvents`、`TestBondScene`、`TestSceneSession`、`TestSceneArchive`（封档/配额豁免：T1-T15 + 回滚开关 + 懒回填 + WebUI 摘要）、`TestBondReunionAnniversary`、`TestInitialBond`、`TestCharacterRegistry`（含会话人格五层链单测）、`TestSessionPersonaBinding`（端到端：创建快照/会话内切换/`--全局`/拒绝/提示消息/`/clear` 保留绑定）、`TestCreateCharacterTool`、`TestCharCreationInterview`、`TestPersonalityMeta`、`TestReferencePortrait` |
| 渠道 / 多模态 / 面板 | `TestQQOfficialQuoteParsing`、`TestOfficialMarkdown`、`TestOfficialButtons`、`TestOfficialStatusHeartbeat`、`TestWebuiOfficialStatus`、`TestMediaGen`、`TestProactiveMedia`、`TestMediaQuota`、`TestAnchorVoiceId`、`TestDirectVision`、`TestDelegation`、`TestAgentPersist`、`TestActiveGreeting`、`TestModelRouterClassify`、`TestPersonalityMarkerPersistence`、`TestWebuiPermissionsEditor`、`TestWebuiModelSections` |
| 资产记忆 / 剧情 | `TestAssetCardsP4`、`TestAssetSearch`、`TestAssetRecall`、`TestUploadTextualization`、`TestEpisodeDistill`、`TestAssetEvictionInjection`、`TestStoryKbInjectionGate` |
| 会话压缩 | `TestSessionCompression`（token 估算 / `apply_compression` 写回+备份滚动 / `clear_context` 清理压缩态 / 命令守卫（群聊·无会话·豁免 kind·消息过少）/ 确认流端到端 / LLM 失败中止零改动 / `_emergency_truncate` / L4 记忆负荷提示+限流+溢出保护） |

另有 `test_workspace.py`（11 个类）、`test_story_kb.py`（6 个类 / 37 个用例，离线合成语料）、`benchmarks/memory/bench_engine.py`（确定性场景流 benchmark），以及 `test/` 下 17 个离线脚本（其中 11 个被 `test.sh` 调用；`test/` 整体被 gitignore，仅存于开发机）。

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
9. **验证**: 发送 `@Roxy /status` 确认 40 个工具已注册、SearXNG 可连通（官方渠道另发一条观察 `[qq_official]` 日志）
10. **配置多模型 (可选)**: 编辑 `QQBot/config/models_settings.json` 填入各模型的 API 信息，参考 `models_settings_example.json` 格式；或在 WebUI 面板「配置热管理 → 模型」结构化编辑（保存前自动探测 API 可用性，保存后约 10 秒热生效，见 §11.6）
11. **启动 Web 管理面板 (可选)**: `bash start_webui.sh`（绑定 `127.0.0.1:8090`，经 SSH 隧道访问）

### Docker 部署

```bash
docker compose up -d
```

包含 searxng + qqbot 两个服务（NapCat + NoneBot）；vLLM 需用 `vllm-start.sh` 另行拉起（非 compose 服务）。

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
| **IMAGE_GEN_MODEL** | 角色画图 | qwen-image（文生图 / 参考图 I2I，DashScope 原生） |
| **TTS_MODEL** | 角色语音 | qwen-audio-tts（声纹 + 台词，DashScope 原生） |

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
    "max_tokens": 409600, "temperature": 0.7, "supports_vision": false
  },
  "FLASH_MODEL": {
    "api_key": "", "api_base": "", "model": "",
    "max_tokens": 102400, "temperature": 0.7, "supports_vision": false
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
  "IMAGE_GEN_MODEL": {
    "api_key": "", "api_base": "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation",
    "model": "qwen-image"
  },
  "TTS_MODEL": {
    "api_key": "", "api_base": "https://dashscope.aliyuncs.com/api/v1/services/audio/tts/SpeechSynthesizer",
    "model": "qwen-audio-tts"
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

> `supports_vision` 是**显式 opt-in**：只有该模型段为 true 时，`turn.py` 才把本轮图片作为 `image_url` part 直传主模型（Direct Vision，§8.4）。
> `IMAGE_GEN_MODEL` / `TTS_MODEL` 不走 ModelRouter，由 `lib/media_gen.py` 每次调用现读（面板热重载即时生效）；`api_base` 为 DashScope 原生端点。

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
| IMAGE_GEN_MODEL 未配置 | `create_image` 直接返回不可用说明；主动发图跳过并降级文本 |
| TTS_MODEL 未配置 | `send_voice` 返回不可用说明；主动语音跳过并降级文本 |
| 主模型段未开 `supports_vision` | 不走 Direct Vision，仍由 MULTIMODAL_MODEL + `read_file` 懒分析 |

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

位于仓库根 `webui/`，是与 bot **独立的 FastAPI 进程**（`start_webui.sh` 管理，默认绑定 `127.0.0.1:8090`，经 SSH 隧道访问；scrypt 口令哈希 + 内存会话鉴权）。入口 `webui/main.py`：**74 个 `/api/` 端点 + 16 个页面路由（+ `/login`）+ 2 个 WebSocket**；页面配置见 `PAGES` 列表，模板 19 个。

| 页面 | 功能 |
|------|------|
| `/` 仪表盘 | 服务状态、硬件监控、快捷入口、**QQ 官方路由卡片**（读 `data/official_status.json`，mtime 判新鲜度） |
| `/processes` 进程管理 | 启动/停止 bot，看门狗状态持久化（`process_manager.py`） |
| `/logs` 日志查看 | bot 日志实时尾部（WebSocket） |
| `/terminal` Web 终端 | 服务器 shell（WebSocket） |
| `/tokens` Token 消耗 | `lib/token_ledger.py` 数据源；缓存命中率分口径观测（按提供商/模型分离） |
| `/audit` 工具审计 | `data/audit/` JSONL 审计日志 + 面板自身操作审计（`audit.py`） |
| `/feedback` 用户反馈 | `#反馈`/`#bug`/`#建议` 提交的处理流 |
| `/sessions` 会话管理 | 临时会话查看/清除（写前备份到 `BACKUP_DIR/sessions`）、特殊会话查看（剧情/已完结/角色创作徽标 + 普通配额摘要 `q/max · 剧情归档 k`；`special_list` 展平 `kind`/`completed`/`counts_quota`） |
| `/tasks` 任务日志 | `data/task_log/{uid}.jsonl` 子任务记录浏览 |
| `/memory` 记忆与画像 | MemorySystem Markdown 编辑、三层记忆查看、画像 JSON 编辑 |
| `/workspace` 工作区磁盘 | 按用户配额/占用展示（角色感知） |
| `/config` 配置热管理 | 提示词 `*.md`、人格、`.env`、模型（结构化表单 + API 探测，§11.6） |
| `/playground` Playground | 面板内直接对话调试（进程内构造 client，无需 nonebot driver） |
| `/wiki` Wiki 缓存 | 角色/羁绊数据浏览、**别名字典编辑**（`character_dic.json` / `bonds_search_dic.json`） |
| `/changelog` 更新日志与公告 | 编辑**更新记录**（版本/日期/分类变更条目，`changelog.json`）与**自由公告**（标题/正文/markdown，`announcements.json`），按渠道勾选**双渠道群发**（OneBot 群 + 官方群） |
| `/mail` 邮件管理 | 用户信箱只读查看（列表 = 信箱建档 ∪ c2c 联系人登记表，openid 脱敏 W4）、勾选发送**系统邮件**（text/markdown）、**撤回**信件；经 `data/mail_admin/` 文件队列由 bot 执行并回写状态（§16.2/§16.4） |
| `/redeem` 兑换码管理 | 限时/长期兑换码人工维护（权威源，`_source=panel`），支持导入/清理与自动抓取联动 |

### 更新记录 / 公告群发（`data/changelog/`，双渠道）

面向用户的「更新记录」（结构化变更）与「公告」（自由标题/正文）以 JSON 存储，与开发者向的 `docs/更新日志.md`（git 提交整理）分开维护，面板是唯一编辑入口。面板与 bot 是两个进程，通过 `QQBot/data/changelog/` 下的共享文件通信（沿用 `redeem_code` 的文件队列范式）：

| 文件 | 写入方 | 读取方 | 用途 |
|------|--------|--------|------|
| `changelog.json` | 面板 | bot + 面板 | 更新记录主数据（唯一真源，数组新→旧） |
| `announcements.json` | 面板 | bot + 面板 | 公告主数据（按 `id` 定位；`sent_at`/`last_targets` 由 bot 回写） |
| `known_groups.json` | bot 后台任务 | 面板 | 群列表：OneBot `groups`（群号+群名+成员数）+ 官方 `official_groups`（openid+status+active_enabled），供分渠道勾选 |
| `pending_broadcast.json` | 面板 | bot 轮询器 | 群发请求队列（`{kind: changelog\|announcement, ref, targets}`；旧裸 entry_index 格式仍兼容） |
| `broadcast_status.json` | bot 轮询器 | 面板 | 群发结果回写（`kind`/`ref`/`sent`/`skipped`/`by_channel`，供面板轮询显示进度） |

- **读写工具** `tools/changelog.py`（纯 IO/格式化，无 nonebot 依赖）：更新记录 `load_entries`/`save_entries`/`get_recent(d)`/`format_for_qq`/`format_announcement`/`mark_broadcast`；公告 `load_announcements`/`save_announcements`/`get_announcement(id)`/`mark_announcement_sent(id, targets)`/`format_announcement_text`；群列表 `write_known_groups(groups, official_groups)`/`read_known_groups`；队列 `claim_pending`/`finish_pending_claim`/`recover_stale_claim`/`write_status`/`read_status`。命令拦截、`get_changelog` 工具、群发轮询器共用。
- **bot 后台任务**（`agent_router.py` `_start_changelog_tasks()`，`@get_driver().on_startup`）：
  - ① **群列表导出** `_group_exporter`——启动 15s 后 + 每 600s：OneBot `bot.get_group_list()` + 官方群登记表（`group_registry`）合并写 `known_groups.json`（拉取失败时**保留上一版快照**，不清空）。
  - ② **群发轮询器** `_broadcast_poller`——启动先 `recover_stale_claim()` 清理崩溃残留并把卡住的 `sending` 改写为中断；随后每 5s `claim_pending()`（原子改名为 `.processing` 认领）→ `_do_broadcast` 按 `kind` 分流（更新记录渲染卡片 + `format_announcement`；公告用 `format_announcement_text` + 可选 markdown）→ targets 归一化（`{channel, id}` / `all` 现场枚举，OneBot 枚举失败回落 known_groups 快照）→ **OneBot 渠道** `_split_text` 分段 + `send_group_msg`（群间 1s 限速，先发卡片图再发文本）；**官方渠道**先按 `group_registry` 预检（`status=active` 且 `active_enabled` 否则计入 `skipped`），`send_active_group` 发送（markdown 拒绝码降级纯文本 + 熔断器；`left` 码 → `mark_left`）→ 回写 `broadcast_status.json`（`by_channel` 分渠道计数）；仅当**至少成功发出一群**时才回写 `broadcast_at`/`sent_at`（全失败置 `error`，不误标"已群发"）。
- **`send_active_group`**（`adapters/qq_official.py`）：官方群主动消息统一出口，返回 `"ok"|"left"|"failed"`；`_GROUP_LEFT_CODES`（40034101/40034105）判退群，markdown 命中 `_MD_REJECT_CODES` 降级纯文本重试一次，共享熔断器（连续 3 次拒绝开 10 分钟），`NetworkError`/`RateLimitException` 每档重试一次。
- **面板端点**：`GET/PUT /api/changelog`、`GET /api/changelog/groups`、`POST /api/changelog/broadcast`、`GET /api/changelog/broadcast/status`、`GET/PUT /api/changelog/announcements`、`POST /api/changelog/announcements/broadcast`（`data_reader.changelog_*` / `announcement_*`；公告保存校验 id 唯一、非法 id 自动重生成为 `ann_YYYYMMDD_<6hex>`，保留 `created_at`/`sent_at`/`last_targets`；保存经 `config_editor.json_config_write` 原子写）。
- **公开站点同步**（`public_site/` → `bot.oneweblog.cn`）：面板每次保存成功（`changelog_save` 的 `result.ok` 分支）调 `data_reader._export_public_changelog(entries)`，把**脱敏**快照（仅 version/date/changes；剔除 created_at/broadcast_at/_class，绝不含群列表/用户数据）原子写 `<PUBLIC_SITE_DIR>/changelog.json`；nginx root 指向该目录即发布（示例见 `docs/bot-site-nginx.md`）。导出失败静默，不阻断保存。站点为纯静态只读单页（index.html/style.css/app.js），`app.js` fetch `./changelog.json` 渲染「更新记录」区，缺失/404 优雅降级显示「暂无更新记录」。
- **QQ 图片卡片 + 指路短链**：命令 `/更新日志` 与更新记录群发 `_do_broadcast` 在文本后 best-effort 追加 `render_changelog_card` 渲染的 PNG（群发在循环外渲染一次、逐群先发图再发文本；发卡失败降级为仅文本、不计入 failed）。指路短链 `_PUBLIC_SITE_URL`（env `BOT_PUBLIC_SITE_URL`，默认 `https://bot.oneweblog.cn`）经 `format_for_qq`/`format_announcement` 的 `site_url` 参数附到文本尾行，降低 /命令 使用门槛。

### 邮件管理（`data/mail_admin/`，面板 → bot 文件队列）

面板对 `data/mailboxes/*.json`（bot 单写）**只读**；删除信件 / 发送系统邮件经 `data/mail_admin/` 文件队列下发给 bot 执行，回写 `status/` 供面板轮询（沿用群发/兑换码的文件队列范式，§16.2/§16.4）：

| 目录 | 写入方 | 读取方 | 用途 |
|------|--------|--------|------|
| `queue/` | 面板 | bot worker | 待处理操作（`{id, type, openid, ...}`；type ∈ `delete_letter`/`send_system`） |
| `processing/` | bot worker | bot worker | `os.replace` 改名认领（原子，避免重复处理） |
| `status/` | bot worker | 面板 | 结果回写（`state` ∈ `done`/`failed` + `error`；保留 7 天由 bot 清理） |

- **用户列表并集**（W4 脱敏）：`mail_users()`/`mail_overview()` 取 **信箱建档用户 ∪ `bond_greet_targets.json`（c2c 联系人登记表）**——只扫描信箱目录会漏掉「聊过天但还没收到过信」的用户（信箱文件惰性建档），而他们正是系统邮件的合法发送目标（`_mail_openid_known` 承认登记表 openid）。仅登记未建档者以空信箱行出现（`has_mailbox=false`、`active_enabled` 视为开启——拒收状态只存信箱文件里），排序键 `max(last_digest_at, last_seen)` 保证活跃联系人不沉底；列表展示 openid 一律脱敏（`abcd…wxyz`），完整值仅详情页。
- **人格数直接读羁绊**：信箱 `known_personas` 是惰性缓存（每日 mail pass 才播种），靠默认人格攒羁绊的用户在建档前会一直显示 0 个人格——面板 `_bond_personas()` 直接读 `BONDS_DIR/{openid}.json`（§4.2 权威源，msgs>0、排除 assistant）：列表 `personas` 计数与搜索取 **信箱 ∪ 羁绊** 并集，详情 `known_personas` 合并羁绊兜底（时间戳记 0 → 前端显示「—」，信箱里的真实记录不被覆盖）。bot 侧同源修复：mail pass 播种改为 `_known_personas_from_sessions` ∪ `_known_personas_from_bonds`（§2.16）。
- **面板端点**：`GET /api/mail/overview`、`GET /api/mail/users`、`GET /api/mail/user`、`POST /api/mail/letters/delete`、`POST /api/mail/send`、`GET /api/mail/queue`（`data_reader.mail_*`）。入队**幂等去重**（delete 按 type+openid+letter_id；send 按 type+openid+title，同标题待处理时拒绝重复入队）。
- **bot worker**：见 §2.16「邮件管理队列」——`plugins/qq_official.py::_mail_admin_worker` 认领执行、系统邮件经 `send_active_c2c_markdown` 真实发送后 `write_system_letter` 入账、撤回经 `delete_letter`（保留信封 id → `/邮件 <id>` 显示「已被撤回」）。

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
| 更新记录 `changelog.json` / 公告 `announcements.json` | 无缓存 | bot 每次查询直接读盘，面板保存后下一条 `/更新日志` 即生效；群发经文件队列（`pending_broadcast.json` 原子改名认领）解耦 |
| 信箱 `mailboxes/*.json` | 无缓存 | **面板只读**；写操作（删信/系统邮件）经 `mail_admin/` 文件队列下发 bot 执行，bot 是单写者——不存在双写竞争 |
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
```

### v2.6 — reasoning_content 全链路保留

```
v2.5 基础上增加:
  ├── DeepSeekClient._parse_response: 解析时保留 reasoning_content 字段
  ├── Session.add_message: 新增 reasoning_content 可选参数, 持久化到 context
  ├── Agent 主循环: assistant 消息携带 reasoning_content 回传 API
  ├── Agent 最终回复: 通过 session.add_message 保留 reasoning_content
  └── MultimodalClient: 单轮请求中 reasoning_content 以 [思考]...[回复] 格式呈现
```

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
  ├── agent/special_session.py: 特殊会话 (快照+增量双层存储, /压缩会话 深度压缩, 按角色限制数量)
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

### 2026-10 之后的演进（按提交日期，尚未并入 v2.x 编号）

> 说明：以下按 git 提交时间排列，暂不套用 v2.x 版本号；功能以代码为准，详细设计见仓库根目录的 `Bond-System-Design.md`、`Story-Knowledge-Base-Design.md`、`Cache-Hit-Rate-Plan.md` 等文档。

#### 2026-10-20 / 10-21 — 记忆优化批次 1–2 + 缓存 Phase 6

- **LONG 层落地**：LONG 对象索引行注入 L4，新增 `recall_memory` 工具按 id 召回快照（3 条 / 1200 字上限，带 query_log 计数）。
- **记忆 schema v2**：条目类型化（`category`）+ `last_confirmed_ts` 时效标注；迁移脚本 `scripts/migrate_memory_p2_categories.py`。
- **纠正与近重复合并**：引擎级纠正替换、Dice 系数 0.6 的近重复合并、preferences 修订路径。
- **缓存 Phase 6-B1**：工具输出上限 100KB → 16KB（避免每轮循环重复计费），`read_file` 增加分段读取。
- **错误治理**：`lib/errors.py` 异常分类 + 诊断编号，原始异常不再直接回给用户。

#### 2026-10-21 — Bond 系统 P0：双平面路由 + 好感度核心

- `agent/bond.py`：三值模型（Bond/Mood/Stats）、10 级阈值、加经验/心情回归、礼物/戳一戳/称呼演化/档案解锁。
- `agent/delegation.py`：`SOCIAL_TOOLS`、`delegate_task`（任务面隔离 + 权限过滤 + 单层嵌套）、`revoice_task_result`。
- `agent/character_registry.py` + `tools/creation_tools.py`：UGC 角色叠加层与创作落库（访谈 UI 在后续提交补齐）。

#### 2026-10-22 — 内核抽取（阶段 1–3）+ QQ 官方渠道上线（阶段 4）

- `agent/turn.py`：平台无关内核（命令分发、回合编排、发送、上下文设置）。
- `agent/inbound.py` / `agent/outbound.py`：入站视图与出站契约（`Sender`/`OutImage`/`OutAudio`/`OutProgress`/`SendFailed`）。
- `adapters/onebot.py`：OneBot 事件解析、附件下载、`OneBotSender` 下沉到适配层。
- `adapters/qq_official.py` + `plugins/qq_official.py`：官方渠道入口、`QQOfficialSender`（缓冲/合并/配额/降级）、引用消息解析、openid 权限表、状态心跳；WebUI 仪表盘「QQ 官方路由」卡片。
- 「正在思考…」在官方渠道独立即时发送，其余进度行吞掉（保护被动回复配额）。

#### 2026-10-23 — Bond P3a/P3b：剧情场景 + 主动问候

- **剧情场景**：`bond_scene` 特殊会话、开场演绎、`complete_scene` 结算、羁绊纪念卡、久别重逢与纪念日提示。
- **主动问候**：`agent/greeting.py` 候选索引 + 日/月配额 + 冷却/概率门；`HEARTBEAT` 低频主动消息（仅官方 c2c）。

#### 2026-10-24 — Bond P4/P5：影音生成、主动媒体、影音记忆、原作剧情 KB

- `lib/media_gen.py`（qwen-image + qwen-audio-tts）+ `lib/image_parts.py` + `agent/asset_memory.py`：画图/语音生成、资产卡、`recall_asset`、事件蒸馏、LRU 淘汰、主动发媒体（Lv7+，与文本配额分账）。
- `lib/story_kb.py` + `scripts/build_story_kb.py` + `test_story_kb.py`：原作剧情知识库（FTS5 + RRF），按人格白名单注入，`story_search` 工具。
- WebUI 模型配置新增 `IMAGE_GEN_MODEL` / `TTS_MODEL` 段。

#### 2026-10-25 — 数据目录 env 化 + 初始羁绊

- `CHARACTERS_DIR` / `BONDS_DIR` / `MEMORY_DIR` / `ASSETS_DIR` 可整体迁到数据盘；UGC 非 UTF-8 文件优雅降级。
- 自创角色支持设定初始羁绊（Lv1–5）；`/设好感` 下放给所有用户（仅作用于本人，可升可降）。

#### 2026-10-26 — 剧情注入门控与数据修正

- 剧情 KB 注入增加人格门控：解析不到剧情角色（UGC 等）时**不再全库检索**，避免把其他角色的剧情注入当前对话；`story_search` 主动检索不受影响。
- `/刷新角色数据` 不再覆盖人工标注的别名/翻译。

#### 2026-10-27 — 人格形象、剧情开场演绎、Direct Vision、会话人格绑定

- 预置人格参考立绘（`agent/config/portraits/`）+ 用户上传人格形象（`/设形象`，`agent/portrait.py` 规整）。
- 剧情场景开场改为角色口吻的 flash 演绎，不再发送第三人称提示。
- **Direct Vision**：`models_settings.json` 模型段开启 `supports_vision` 后，本轮图片以 `image_url` part 直传主模型（1568px、≤4 张、≤5MB；首轮 400 自动剥图重试）。
- `/帮助`、`/功能` 文档更新（HELP.md / FEATURES.md），功能卡渲染修复长文本溢出。
- **会话人格绑定**：人格注册在特殊会话 `metadata.persona_key` 上，解析优先级 剧情钉住 > 会话绑定 > 个人设置 > 群默认 > 全局默认（`CharacterRegistry.set_session_provider` 单点注入，失效懒穿透、存量零迁移）；特殊会话内 `/人格切换` 只写会话绑定，`--全局` 逃生门改用户默认；`/新会话`、`/保存为会话` 创建时快照当前人格；切会话/结束会话/`/会话列表` 展示各会话人格；剧情/创作会话拒绝切换；`/clear` 保留绑定（§2.13、SESSION.md）。

#### 2026-10-28 — QQ 官方渠道 markdown + 消息按钮（P0+P1）

- **官方 markdown**（msg_type=2）：unified 人格回复 + `/帮助`/`/兑换码` 走原生 md，每轮 `_build_render_context` 判定（绝不按用户缓存），白名单声明只注入 L4 易变尾（缓存前缀不动）；区块 ≤2000 字切分、拒绝码降级纯文本 + 连续 3 拒熔断 10 分钟、`QQ_OFFICIAL_MARKDOWN=off` 热读逃生门。
- **消息按钮**（keyboard，P0 指令按钮 + P1 回调按钮）：平台无关 DSL `lib/button_spec.py`（`Btn`/`Rows`/`validate`/`to_text`）→ `adapters/qq_official_buttons.py`（wire 转换 + `qb1:` 回调编解码 + 交互回执发送器 + 点击遥测）；回调链 `plugins/qq_official_buttons.py`（幂等+限频+审计）→ `agent/button_actions.py`（scene/role 复核 + 2.5s 预算）→ `agent/session_render.py`；降级梯 键盘 → md-only → 纯文本命令清单；`QQ_OFFICIAL_BUTTONS=auto/on/off` + 熔断 + 灰度白名单；`/按钮探测` 管理员诊断命令。

#### 2026-10-29 — QQ 官方渠道流式消息（c2c，§6.10.11.2）

- `lib/deepseek_client.py::chat_completion_with_tools_stream`：SSE 流式变体，`content` 增量经 `content_callback` 实时转发（工具碎片出现后抑制、`reasoning_content` 永不转发），工具碎片按 index 累积、usage 末片入账，返回与非流式**逐字同形**（保 Direct Vision 400 自愈）。
- `adapters/qq_official_stream.py`：`MessageStreamer` 双流传输（同 `msg_id` 被动锚点；进度流 `msg_seq` 基 900 承载「⏳ …」工具进度、回答流基 1800 承载 LLM 增量 + 免责声明末片 `input_state=10`）+ `_StreamBreaker`（3 连拒 600s / ApiNotAvailable 长熔断 / 半开恢复）+ `stream_snapshot()`。裸 HTTP `POST /v2/users/{openid}/stream_messages`（`bot._request`），is_wakeup 开流重试、50001/50002 退避；`remain_msg_len` 仅作遥测（真机实测每片接受后返回 0，是挂起长度而非发送预算）。
- 接线：`agent/outbound.py` Sender 增 `supports_stream`/`open_stream()`；`QQOfficialSender`（仅 c2c + md 能力）实现，OneBot 恒 `None`；`agent/agent.py::run` 增 `content_callback`（getattr 探测流式方法，mock 零改动兼容）；`agent/turn.py` 开流决策（render context 设定后、triage 前）+ `_progress`/`_content_delta` 路由 + 回退树（happy/truncated/broken/未开流）+ 键盘预约时禁开回答流 + `/流式探测` 管理员命令（验证双流同锚点这一平台未证假设）。
- 纪律：OneBot 与群聊零变化；split 人格（revoice）不流式（render context 为空自动排除）；流式仅传输层，会话持久化不变。
- 配置：`QQ_OFFICIAL_STREAM=auto/on/off`、`QQ_OFFICIAL_STREAM_INTERVAL=1.0`、`QQ_OFFICIAL_STREAM_CIRCUIT_HOURS=24`（均热读）；心跳 `official_status.json` 增 `stream=` 段；新增 `TestOfficialStream`（17 用例，离线）。

#### 2026-10-30 — 角色信箱 v1（proactive-mail-design.md v0.5）+ 免责尾用户开关

- **角色信箱**：`HEARTBEAT` 低频问候升级为每日「信封」摘要（仅官方 c2c）。`plugins/qq_official.py::_daily_active_loop`（`mail_hour` 默认 10 点 + 0–600s 抖动，启动窗口内减半补发）→ `_run_mail_pass` 三段式（A 收集候选过滤链 / B 跨用户 `Semaphore(4)` 角色面 flash 生成 ≤60 字留言，失败即丢弃无模板 / C 串行发送：令牌桶 10 qps + 2–6s 抖动），成功才记账（`record_digest` 同日幂等 + 配额 kind=text + bond 事件全文）；与媒体 pass 同日**双向互斥**。
- **信封渲染**：`adapters/qq_official.py::send_active_c2c_markdown`（kb→md→plain 阶梯，喂 md/kb 共享熔断器，40034029/304062 剪按钮重试）；单封短文 B2 全文无按钮，多封 B1 列表 + 「展开」指令按钮（`data=/邮件 <id>`），超 `mail_max_chars` 截断为「另有 N 封」。
- **存储**：`agent/mailbox.py`（`data/mailboxes/{openid}.json`，原子写、损坏降级、28 天 + 200 封裁剪，letter id `L-YYYYMMDD-<4hex>` 随机后缀防重号）；`agent/rate_limit.py` 令牌桶；`agent/group_registry.py` 官方群登记表（GROUP_AT 入站 60s 节流 upsert，v1 纯记账，P1 群发送预铺）。
- **事件**：`C2C_MSG_REJECT`/`C2C_MSG_RECEIVE`（`on_notice`）翻 mailbox `active_enabled`，拒收即停发、恢复即续。
- **命令**：`/邮件`（列表分页/`下一页`/`p<N>`/`MM-DD`/编号看全文并标记已读，仅官方单聊，零 token 不带免责尾）；`/免责声明 [关|开]`（免责尾追加开关，三级用户均可，`data/user_prefs/disclaimer.json` 按渠道独立、mtime 热读，bot 单写者；信件/信封本就不带免责尾）。
- **配置**：`bond_settings.json` 新增 `mail_*` 组（`mail_enabled` 为总回滚开关——关掉即回旧 `_greeting_loop`）；新增 `TestProactiveMail`（16 用例，离线）。
- WebUI 邮件管理页、群公告通道（P1）、`check_mail` 工具与信件编辑（v2）不在本版（次日补齐 WebUI 邮件管理 + 公告群发，见下条）。

#### 2026-10-30（续）— WebUI 邮件管理 + 公告双渠道群发（proactive-mail-design.md §16）

- **公告存储（§16.3）**：`tools/changelog.py` 新增 `announcements.json`（自由标题/正文/markdown，按服务端生成 `id` 定位）——`load/save_announcements`、`get_announcement(id)`、`mark_announcement_sent`、`format_announcement_text`（`📢 公告\n{title}\n\n{body}`）。面板保存校验 id 唯一、非法 id 自动重生成为 `ann_YYYYMMDD_<6hex>`，保留 `created_at`/`sent_at`/`last_targets`。
- **双渠道群发（§16.5）**：`pending_broadcast.json` 升级为 `{kind: changelog|announcement, ref, targets}`（旧裸格式兼容）；targets 归一化为 `{channel, id}` / `all`。`agent_router._do_broadcast` 按 kind 分流，**OneBot 渠道**分段 + 卡片图，**官方渠道**先按 `group_registry` 预检（`status=active && active_enabled` 否则 `skipped`）再 `adapters/qq_official.send_active_group`（返回 ok/left/failed，markdown 拒绝码降级纯文本 + 熔断，`left` 码 `mark_left`）；回写 `broadcast_status.json` 带 `by_channel` 分渠道计数，仅 ≥1 群成功才回写 `broadcast_at`/`sent_at`。`_group_exporter` 合并 OneBot `get_group_list` + 官方登记表写 `known_groups.json`（双段）。
- **WebUI 邮件管理（§16.2/§16.4）**：新增 `/mail` 页——信箱**只读**查看（列表 = 信箱建档 ∪ `bond_greet_targets.json` c2c 登记表，openid 脱敏 W4）、勾选发送系统邮件（text/markdown）、撤回信件；经 `data/mail_admin/{queue,processing,status}/` 文件队列下发 bot。bot 侧 `plugins/qq_official.py::_mail_admin_worker` 原子认领执行：`delete_letter`（撤回，保留信封 id → `/邮件 <id>` 显示「已被撤回」）、`send_system`（`send_active_c2c_markdown` 真实发送，成功才 `write_system_letter` 入账，拒收用户失败不入账），启动 `recover` 清残留。`mailbox.py` 增 `write_system_letter`/`delete_letter`/`is_withdrawn`。
- **面板端点**：`GET/PUT /api/changelog/announcements`、`POST /api/changelog/announcements/broadcast`、`/api/mail/{overview,users,user,letters/delete,send,queue}`（`data_reader.announcement_*` / `mail_*`；入队幂等去重）。
- **修复**：邮件页用户列表最初只扫 `mailboxes/` → 漏掉「聊过天但还没收过信」的用户（信箱惰性建档），改为并集 `bond_greet_targets.json` 登记表后名单完整、可勾选发送。
- **测试**：新增 `TestMailAdmin`（§16.2/§16.4：系统信/撤回/worker/WebUI API）与 `TestAnnouncementBroadcast`（§16.3/§16.5：公告存储/send_active_group 分类/_do_broadcast 分流/WebUI 公告 API），离线共 59 套件全绿。

#### 2026-10-30（晚）— 官方流式「重复输出 / 重复发送」修复（P0–P5）

线上现象：官方 c2c 流式回合里，同一条答案偶尔出现两遍（A 形态：流式消息里一遍 + 一条完整普通消息）。
根因是回退树把「已上屏文本不是最终回复的严格前缀」当成"内容缺失"直接整篇重发；而工具轮开场白
（SSE 在工具碎片出现前照发 content）或并进回答流的「⏳ …」进度行一混入，这个前缀判定必然失败 →
必然整篇重发，且该分支**不打任何日志**（所以表现为"偶现、无痕"）。同日另有一次 404/405 被误判成
"接口不存在"，把流式熔断了 24h（日志 `streaming disabled for 24h`，靠重启才恢复）。

- **P0 回退树重写**（`agent/turn.py`）：新增 `_delivered_prefix_len()`（取「已上屏文本后缀 ==
  response 前缀」最长解，容忍开场白/进度行污染）+ `_plan_stream_delivery()` 纯决策函数：
  `buffered_full`（一次没开成才允许整篇）/ `stream_only`（正文已全覆盖，一条不补）/ `remainder`
  （断流只补未上屏尾巴）。**铁律：回答流上过屏就绝不整篇重发。**
- **P1 断流也收尾**（`adapters/qq_official_stream.py`）：`close()` 对断流补发 `state=10` 收尾片
  （只重复平台已接受的文本），修掉"流卡在生成中 + 平台把整条消息回滚到首包"；`_flush_slot` 被取消
  时按"已送出"记账，避免取消 in-flight 请求后重发造成同段重复。
- **P2 404 不再误熔断**：`HTTP 404/405 = 未找到 API`（docs :510）是路由级错误。只有**开流首片**
  连续 2 次 404/405 才判能力缺失（长熔断默认 24h→1h，冷却后半开重探）；续片 404/405 只断当前流
  （`shard_404_total` 遥测），能力位不动。`/流式探测` 先 `stream_force_probation()` 再实测。
- **P3 append → replace（幂等）**：分片改 `input_mode=replace` + 累计全文（与 docs §20 示例、
  腾讯 dsh-qqbot StreamingWriter 一致）。重试/超时/取消后重发同一份累计正文是幂等的；`40007`
  （前缀不可修改）在本实现里不可能由自己触发。顺带修好免责尾分隔（旧版 buffer 已空时拼成「正文免责句」）。
- **P4 可观测性**：每轮一条 `[stream] turn done user= ans_open= broken= sent= resp= decision=
  remainder= shards=/ retries= last_err=` INFO；`stream_snapshot()` 增 `api_rejects` /
  `shard_404_total` / `retry_total` / `delivery{stream_only,remainder,buffered_full}`（面板
  `official_status.json` 的 `stream=` 段直接可见）。
- **P5 入站事件去重**（`plugins/qq_official.py`）：按事件锚点（msg_id）60s 窗口 + 512 上限 LRU
  去重，挡平台重推同一被动回复锚点造成的两条相同答复（docs :2154「相同 msg_id 可能多次推送」）。
- **测试**：`TestOfficialStream` 17 → 23 用例（replace 累计语义 / 续片重试幂等 / 断流强制收尾 /
  续片 404 不动能力位 / 锚点过期码才 is_wakeup / 交付决策三态 / 事件去重）；`test_agent.py` 59 套全绿。
- **配置**：`QQ_OFFICIAL_STREAM_CIRCUIT_HOURS` 默认 24→1（热读）；`QQ_OFFICIAL_STREAM=off` 仍是总闸。
- 已知残余：若某片在服务端已生效而客户端判定失败（平台无读回接口），最多重复该片一次；replace 语义下
  下一片或收尾片会整体覆盖，最终正文不会重复。

#### 2026-10-31 — 信箱 known_personas 漏档修复（默认人格攒羁绊进不了邮件候选）

- **现象**：生产用户（`92E1…8950`）邮件页人格显示 0、信箱文件从未建档、每日信封永远收不到——尽管她有 Lv6 / 382 条消息的 UGC 角色「夏以昼」（split 模式、本人创建）。
- **根因（系统性）**：`known_personas` 的唯一播种源是会话索引 `metadata.persona_key`，而该字段只由特殊会话内 `/人格切换`（或剧情会话）写入；靠**个人默认人格**（resolve 链的个人层）攒羁绊的用户，会话 metadata 没有绑定 → known 恒空 → `_mail_candidates` 的 `persona not in known` 门永远拦下 → 信箱永不建档。
- **bot 修复**：`plugins/qq_official.py` 新增 `_known_personas_from_bonds()`（羁绊文件 msgs>0 的角色，排除 assistant；§4.2 数据源 1 权威），mail pass Phase A 播种改为 **sessions ∪ bonds** 并集（每用户锁内 `ensure_known_personas`）。部署后下一次每日 mail pass 即为存量用户补建档。
- **面板修复**：`webui/data_reader.py` 新增 `_bond_personas()` 直接读 `BONDS_DIR`——列表 `personas` 计数与搜索取信箱 ∪ 羁绊并集（未建档用户立即显示正确人格数、可按角色名搜到），详情 `known_personas` 合并羁绊兜底（时间戳 0 → 前端显示「—」，信箱真实记录不被覆盖）。
- **测试**：新增 `test_known_personas_from_bonds`（并集播种 → known 门放行 → 进候选），`test_webui_mail_api` 扩展羁绊 fixture（msgs>0 计入 / msgs=0 与 assistant 排除 / 角色名可搜 / 详情合并）；59 套件全绿。

#### 设计稿 — QQ 官方渠道消息按钮

- `docs/qqbot-official-buttons-design.md`：官方 API 按钮的能力边界、39 个可按钮化功能盘点、8 种交互模式、接入架构与降级方案、准入政策与申请材料。
- 现状：**P0（指令按钮）+ P1（回调按钮）已落地**（见 2026-10-28 条）；自定义回调按钮需平台「内邀开通」，未开通时自动降级为纯 markdown/文本命令清单。流式消息（§6.10.11.2）已于 2026-10-29 落地。

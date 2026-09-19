# QQBotAgent 智能体缓存命中率优化规划

> **实施状态（2026-09-19 更新）**：🔨 **Phase 1 / 3 / 4 已实施**（开发机工作区，
> 未提交；Phase 1 数据层 23 项 + HTTP 冒烟 11 项验证全过，脚本存
> `test/test_webui_cache_views.py` / `test_webui_cache_http_smoke.py`；Phase 3/4
> 由 `test_agent.py` 新增 TestCacheStability 套件 6 项 + 全量 14/14 套件回归覆盖）。
> **Phase 2 / 5（生产部署 + 对照观测）未开始**。
> **基线**：改动前仪表盘全局命中率 ≈**63%**（混合口径，含多模态/triage 稀释，
> 用户 2026-09-19 观测）；经用户确认跳过独立基线采集期，部署后直接观察分口径对比。
> 前置依赖已就绪：Token 计量总表（`QQBot/lib/token_ledger.py`，每次 LLM 调用记录
> input/output/cached/uncached 四维）与 WebUI Token 页（缓存命中率卡片、按模型堆叠图）
> 已合入 dev（commit `924fb62`）；生产服务器尚未部署（见 `.claude/deploy-server.md`）。
> 实施进展请沿用在各 Phase 标题后追加 ✅/⚠️ 注记的惯例（参照 Web-UI-Plan.md）。

## Context

DeepSeek/dashscope 均提供服务提供商侧的**自动前缀缓存**（无需 API 参数）：请求前缀
（tools schema → 系统提示词 → 历史消息）与近期请求的公共 token 前缀按缓存价计费
（DeepSeek 约为全价 1/10）。本 bot 的系统提示词 ≈ 36KB 配置 + 每用户注入块，历史最多
20 条，agent 主循环单次 run 可迭代至多 5 轮工具调用——输入 token 是成本大头，命中率
每提升 10% 都直接省钱。此前缺乏观测手段，优化无从验证；现在 webui Token 页 +
token_ledger 已能按模型/按用途/按日给出 `cached_input_tokens`，**优化窗口已打开**。

**用户已确认的决策**：
1. **先做 webui 观测增强（Phase 1），再部署采基线（Phase 2）**——观测增强是零 bot 风险
   的纯面板改动，先增强后部署可少折腾生产环境一次；影响命中率的是 bot 侧 Phase 3/4，
   基线对照关系不受影响。
2. **多提供商观测口径纳入 Phase 1**——缓存是提供商侧、按模型隔离的，命中率必须分模型
   统计，不能混算（详见 Phase 1）。

## 已核实的现状（影响设计的事实）

### 提供商配置（models_settings.json，2026-09-19 开发机核实）

| Tier | 模型 | 提供商 |
|---|---|---|
| REASONING_MODEL | deepseek-v4-pro | api.deepseek.com |
| FLASH_MODEL | deepseek-v4-flash | api.deepseek.com |
| MULTIMODAL_MODEL | qwen3.7-plus | dashscope compatible-mode |
| AUDIO_MODEL | qwen3-omni-flash | dashscope compatible-mode |
| OCR_MODEL | qwen3.5-ocr | dashscope compatible-mode |

**两家缓存互相隔离、回报字段不同**：DeepSeek 走 `usage.prompt_cache_hit_tokens`，
dashscope 兼容模式走 `usage.prompt_tokens_details.cached_tokens`——
`token_ledger.py:82-87` 已双格式兼容。qwen 系多模态请求以图片/音频为主，前缀缓存
收益天然有限，**不应与文本模型混在一个命中率里看**。

### 请求构造链路（缓存视角）

- **前缀顺序**：tools schema（`tool_registry.py:57-64`，注册 dict 序，同角色内字节级
  稳定 ✅）→ messages[0] 系统提示词 → 历史 → 当前用户消息。
- **messages[0] 组装顺序**（`agent.py:_build_messages`，:423-533）：
  personality 前置（:447-455）→ SOUL+IDENTITY+AGENTS+MEMORY+冻结时间+硬件
  （`build_system_prompt()`，:137-169，构建后缓存于 `_system_prompt`）→ 人格过渡提示
  （:457-459）→ 特殊会话标记（:463-471）→ 工作区+quota（:474-484）→ profile 注入
  （:489-493）→ 权限说明（:496-503）→ 群功能限制（:506-509）→ MEDIUM 记忆注入
  （:511-519）→ 历史（:522-528）→ 用户消息（:531）。
- **agent 主循环**（`agent.py:232-346`）：messages 只追加不改写 → 单次 run 的多轮工具
  迭代天然 append-only ✅；反重试 system 警告（:334-341）只活在本次 run、不进 session ✅。
- **其它 LLM 调用点**：triage（`model_router.py:127-169`，静态模板+尾部变体 ✅）、
  画像抽取（`profile.py:600-644`，静态规则在前、existing/judge/对话块在尾 ✅）、
  多模态/audio/OCR（走各自 dashscope 客户端）。
- **webui 观测现状**：Token 页有缓存命中率卡片（`tokens.html:29`）、按模型表格含
  cached 列（`tokens.js:27`）、每日堆叠柱状图含命中/未命中（`tokens.js:44-45`）；
  `data_reader.py:84-132` 已有 `_hit_rate()` 与 daily cached 字段。
  **缺**：按用途（by_purpose）命中率表、每日命中率趋势线——ledger 的 `by_purpose`/
  `daily` 桶已含 `cached_input_tokens`（`token_ledger.py:198-216`），纯展示层补齐。
- **生产部署状态**：线上 bot 是旧代码（无 webui/无热重载 watcher，
  `.claude/deploy-server.md` §当前线上 bot 是旧代码）；基线采集必须等 Phase 2 部署后
  才开始计时。

## 诊断：五个缓存破坏点（按影响排序）

| # | 问题 | 位置 | 后果 |
|---|---|---|---|
| B1 | **特殊会话标记每轮都变**：`会话消息数: {total_messages}` 嵌在 messages[0] | `agent.py:463-471` | 特殊会话用户每轮整段历史全价重算，命中率≈0 |
| B2 | **慢变块位于历史之前**：profile 注入（每次抽取批后变）、MEDIUM 记忆注入（top-12 按 count 排序，强化即变，`memory.py:626-651`）、quota 上下文（写文件即变，`workspace.py:150-167`）、人格过渡提示 | `agent.py:457-519` | 每变一次，该用户整段历史 miss |
| B3 | **历史头部修剪**：`session.trim(20)` 满员后每轮从头部丢消息 | `session.py:49-52`、`agent.py:381/396/407` | 会话满员后每轮历史前缀都变 → 系统提示词之后全部 miss |
| B4 | **特殊会话压缩边界漂移**：`_compress_context(recent_full=20)` 每轮把新滑出窗口的旧消息压缩一次 | `agent.py:524,537-571` | 历史中段每轮出现分歧点，之后全部 miss |
| B5 | **人格块前置到全局配置之前** | `agent.py:447-455` | 全局静态前缀（≈1 万 token）被拆成 3 份人格变体，各自流量变小、缓存更易被逐出；切换人格时配置段全部 miss |
| B6 | **附带正确性 bug**：`Current time` 随 `_system_prompt` 首次构建冻结（`agent.py:161`），之后永远是 bot 启动时刻，直到某次 reload_configs | | 模型拿到假时间。⚠️ 不能简单改成每轮重建（会亲手毁掉缓存），必须挪到尾部易变块（Phase 3） |

## 分阶段实施计划

**Phase 1 — webui 观测增强（含多提供商口径）** ✅（2026-09-19 实施，开发机工作区）：
纯面板改动，零 bot 风险，先行合入。

> ✅ 落地实况：`data_reader.py` 新增 `_provider_of()`（模型名前缀推断 DeepSeek /
> 阿里云 dashscope）、`tokens_summary()` 增加 `models` 注解视图（provider + hit_rate，
> 按总 token 降序）、新增 `tokens_daily_by_purpose()`（按日按用途聚合，仪表盘口径用）；
> `main.py` 仪表盘 payload 增加 `hit_rate_agent_loop`（今日）；`tokens.js` 用途表补
> 「未命中」列、新增按模型命中率表（#tbl-model）、每日堆叠图叠加命中率虚线（右轴 %）；
> `tokens.html` 增加提供商分离口径说明卡；`dashboard.js` 副标题优先显示今日 agent_loop
> 命中率、全局值降级为 title 悬浮提示。
> ⚠️ 规划外修复：`tokens_users` 的 glob 原为 `token_usage_*.jsonl`，与 ledger 实际
> 文件名 `usage_YYYY-MM-DD.jsonl`（token_ledger.py `_daily_path`）不匹配 → 用户排行
> 此前恒为空，已改为 `usage_*.jsonl`。
> 验证：数据层 23 项（真实 TokenLedger 造数 → tokens_summary/tokens_daily_by_purpose/
> tokens_users 一致性 + 空数据防御）+ HTTP 冒烟 11 项（ASGI 进程内，含「多模态稀释
> 全局命中率 0.8→0.5」口径验证）全过，脚本存 `test/test_webui_cache_views.py`、
> `test/test_webui_cache_http_smoke.py`；bot 侧 `test_agent.py` 13/13 套件无回归。

1. **按用途命中率表**：`data_reader.py` 把 `totals.json` 的 `by_purpose` 各桶透传
   （`_hit_rate()` 已存在），`tokens.js` 渲染「purpose / requests / 输入 / 命中率 /
   未命中输入」表——这是验证 Phase 3/4 效果的核心视图（`agent_loop` 命中率应显著
   上升，`triage`/`profile` 基本不变）。
2. **按模型命中率分离（多提供商口径，用户确认纳入本 Phase）**：
   - 现有按模型表格已含 cached 列 ✅，补一列**显式 hit_rate 百分比**并标注提供商
     （按 api_base 域名或模型名前缀推断 deepseek / dashscope）；
   - 仪表盘卡片副标题的全局命中率会被短小 triage 请求与低收益的多模态请求**稀释**，
     改为展示 **agent_loop（或 deepseek 文本模型）命中率**，全局值降级为悬浮提示；
   - 页面注明：qwen 系多模态/audio/OCR 以图片音频为主，命中率天然偏低属正常，
     不与文本模型混算、不作为优化目标。
3. **每日命中率折线**：daily 桶已含 `cached_input_tokens`（`data_reader.py:132`），
   在现有堆叠柱状图上叠加 hit_rate 折线（双 y 轴），部署日前后对比一目了然。

验证项：by_purpose/by_model 命中率与手工 `TokenLedger.aggregate_day()` +
`format_summary()`（`token_ledger.py:280-319`）结果一致；Playground 跑一轮后
purpose=playground/agent_loop 记录出现在表中。

**Phase 2 — 生产部署 + 基线采集**：

1. 按 `.claude/deploy-server.md` 部署当前 dev（webui + token_ledger + 新
   agent_router + Phase 1 增强）并重启；前置：服务器 venv `pip install -r
   webui/requirements.txt`（缺 psutil）、处理好 screen→面板的管理权迁移。
2. **采集 3–7 天基线**：Token 页记录总体/按模型/按用途命中率与日均未命中输入 token，
   数字回填本文档。预期现状：agent_loop 临时会话用户中等命中、特殊会话用户接近 0
   （B1）、满员会话每轮历史 miss（B3）。

> ⚠️ 基线口径调整（2026-09-19，用户决定）：用户在 Phase 3/4 改动**之前**已观测到
> 仪表盘全局命中率 ≈**63%**（当时线上为旧代码 + 混合口径），以此作为记录基线；
> 跳过部署后独立采集 3–7 天基线的步骤，Phase 5 部署后直接用分模型/分用途口径对比
> 63% 全局值与优化后的 agent_loop 命中率。注意两者口径不同（63% 含多模态/triage
> 稀释），对比时以「优化后 agent_loop（deepseek 文本）命中率 ≥63% 且日均未命中
> 输入 token 下降」为准，不做同口径强求。

**Phase 3 — 提示词四层分级重构（bot 侧核心）** ✅（2026-09-19 实施，开发机工作区）：
原则=**按变化频率分层，越稳定越靠前；
一切每轮/每几轮会变的内容全部挪到历史之后**，作为一条独立 system 消息紧贴当前用户
消息（代码已有中途插 system 消息先例——反重试警告 `agent.py:334-341`，模型接受度
无虞）。改动集中在 `agent.py`：

```
tools schema（按角色稳定，不动）
├─ L1 全局静态   SOUL + IDENTITY + AGENTS + MEMORY + 硬件块   ← 全体用户共享一份热前缀
├─ L2 群/人格    personality 块 + group feature 限制上下文     ← 同人格用户共享
├─ L3 用户级稳定 工作区路径 + 权限角色说明                     ← 同用户跨轮稳定
├─ 历史（append-only，配合 Phase 4 迟滞修剪）
├─ L4 易变尾部   独立 system 消息：真实当前时间 + 特殊会话标记(名称/消息数)
│               + quota 用量 + profile 注入 + MEDIUM 记忆注入 + 人格过渡提示
└─ 当前用户消息（upgrade hint / 群功能提醒前缀本就在这里 ✅ 不动）
```

1. `build_system_prompt()`：删 `Current time` 段（修 B6 假时间 bug）；硬件块保留。
2. `_build_messages()`：人格块从前置改为接在 L1 之后（修 B5）；:457-459/:463-471/
   :482-484/:489-493/:516-519 全部迁入 L4 尾部 system 消息，并加入每轮真实时间。
3. 同步修改 `SOUL.md` 首段「personality is injected at the very top」的描述，
   避免提示词自相矛盾。

效果：每轮未命中部分只剩「L4 尾块 + 新用户消息 + 新工具结果」，L1–L3+历史全部命中。
以 20 条历史、平均每条 200 token 估算，满员会话每轮省 ~4K token 全价输入；特殊会话
从 0 命中直接转为高命中。

风险：profile/记忆注入位置头→尾，注意力权重理论上略变——Playground 多轮验证 +
`test_agent.py` 13 套件回归 + 上线后抽查真实对话质量（反馈页）。

> ✅ 落地实况（2026-09-19，开发机工作区）：`_build_messages()` 按上图重写——
> L1=`build_system_prompt()`（已删 `Current time` 段，修 B6）；L2=personality 块
> 改为以 `---` 分隔符**接在** L1 之后（修 B5），其后拼群上下文；L3=工作区路径 +
> 权限角色块；历史（特殊会话走压缩、临时会话原样）；L4=历史之后的独立 system
> 消息（每轮真实时间、人格过渡提示、特殊会话标记、quota、profile、MEDIUM 记忆），
> 再接当前用户消息。
> ⚠️ 规划外联动：① `SOUL.md` 首段与 `personality.py` 模块 docstring 措辞同步
> （「injected right after these shared rules, marked by a `---` separator」），
> 避免提示词自相矛盾；② `webui/playground.py` 的 `preview_system_prompt()` 改为
> 拼接**全部** system 消息（四层分级后 system 分布在头尾两条）；③ 修复
> `test_profile_injection` 死代码——原已定义但从未注册进 `run()`，且捕获函数签名
> 过时（2 位置参数），修正签名、注册进 AgentCore 套件，断言改为「profile 内容在
> 尾部 system、不在头部」；`test_medium_memory_injection` 同步改断言到尾部（头部
> 断言用用户事实「力量训练」而非标题串——MEMORY.md 文档本身就提到该标题）。
> 验证：新增 `TestCacheStability` 套件 6 项（头部跨用户字节一致、易变内容位于
> 历史之后的尾部、profile 变更不动头部、5 轮历史头部仅变 1 次、压缩边界阶梯化
> 且步内字节稳定、系统提示词无冻结时间戳），`test_agent.py` **14/14** 套件全绿；
> webui 两支冒烟脚本重跑通过；playground 预览 sanity（头部无时间戳、尾部含真实
> 时间、总长 23.9K 字符）。

**Phase 4 — 历史修剪与压缩的迟滞策略** ✅（2026-09-19 实施，开发机工作区）：

1. **迟滞修剪（修 B3）**：`session.py:49-52` 改高水位触发——超过 `max+4`（24 条）
   才一次性裁回 `max`（20 条）。满员会话历史头部从每轮都变 → 每 2 轮变一次；多带的
   4 条按缓存价（~1/10）计费，净收益为正。`max_context_messages` 对外语义不变。
2. **压缩边界对齐（修 B4）**：`_compress_context` 边界向下取整到 4 的倍数（阶梯式
   移动，每 4 条才重算一次）；或更彻底——特殊会话快照直接存已压缩形态
   （`special_session.py` snapshot+delta 存储支持），压缩一次永久生效。
3. 反重试警告不进 session ✅ 保持现状。

验证项：单测——满员会话连续 5 轮，历史头部仅变 1–2 次；特殊会话 >20 条后压缩段
字节级稳定。

> ✅ 落地实况（2026-09-19，开发机工作区）：`session.py` 新增模块常量
> `TRIM_HYSTERESIS = 4`，`trim()` 改高水位触发（`len > max+4` 才一次裁回 `max`，
> 修 B3）；`agent.py` 的 `_compress_context` 改 `@classmethod` + `COMPRESS_STEP = 4`，
> 压缩边界 `n_compress = (len−recent_full)//4*4` 阶梯推进（修 B4），步内已产出的
> 前缀字节级不变。
> ⚠️ 「特殊会话快照直接存压缩形态」备选方案**未实施**——阶梯边界已消除每轮漂移，
> 快照方案改动面大，留待 Phase 5 观测后按需评估。
> 验证：`test_trimming` 增补迟滞带断言（9 条/max=5 不裁、头部不动；第 10 条触发
> 一次裁回 5）；`TestCacheStability.test_history_head_moves_rarely`（20 条满员 +
> 5 轮 → 头部实际仅变 **1** 次，≤24 条上限）与 `test_compress_boundary_steps`
> （40 条上下文，步内 +1..+3 前缀逐字节比对、跨步边界恰好推进 4）全过。

**Phase 5 — 部署与 7 天对照验证**：Phase 3/4 合入后受控部署（`git pull` + 重启，
生产有真实用户，遵守 deploy-server.md 安全节）。重启后 `_system_prompt` 重建、提供商
侧旧缓存自然过期，**前 1–2 天命中率偏低属正常**。跑满 7 天后在 Token 页对比基线：
agent_loop（deepseek 文本模型）命中率预期升至 60–80%+；日均未命中输入 token 的下降
幅度 ≈ 实际省钱幅度。结果回填本文档。

## 明确不做 / 低优先项（避免过度设计）

| 项 | 结论 |
|---|---|
| triage 短消息跳过（<15 字直接判 simple） | 可选。省一次 flash 调用是省钱不是提命中率，且 triage prompt 太短、本就在缓存最小命中长度附近 |
| 画像抽取 prompt 重排 | 不动。静态前置已正确（`profile.py:600-644`） |
| httpx 连接复用（现每请求新建 AsyncClient，`deepseek_client.py:81/132`） | 不动。只影响延迟不影响命中率 |
| qwen 多模态系命中率优化 | 不追。图片/音频为主，前缀缓存收益天然有限；Phase 1 已把它与文本模型分开呈现 |

## 风险与回滚

| 风险 | 缓解 / 回滚 |
|---|---|
| L4 尾部注入导致行为漂移（profile/记忆位置变化） | Playground 多轮 + test_agent.py 回归先行；上线后盯反馈页；回滚=还原 `_build_messages` 单函数 |
| 迟滞修剪使窗口临时超出 20 条 | 上限 max+4，token 增量按缓存价计费；单测覆盖 |
| 压缩阶梯改变特殊会话历史内容 | 只影响压缩时机不影响内容；快照存储方案落地前先做只读 diff 验证 |
| 部署窗口影响线上用户 | 单次受控重启（Phase 2 与 Phase 5 各一次）；避免频繁上线 |
| 命中率数据被多模态/triage 稀释误判 | Phase 1 已按模型+按用途分离口径 |

## 端到端验证（全部完成后）

1. `test_agent.py` 14 套件（Phase 3/4 新增 TestCacheStability 后）+
   `test_workspace.py` 全绿（bot 侧零回归）
2. Token 页：by_purpose 命中率表、by_model 提供商标注、每日命中率折线均出数，
   与手工 `aggregate_day()` 一致
3. 特殊会话连发 5 轮 → 第 2 轮起 agent_loop 命中率显著高于基线（B1 修复生效）
4. 临时会话满员后连发 5 轮 → 历史头部仅变 1–2 次（B3 修复生效）
5. 模型回复中引用的「当前时间」为真实时间（B6 修复生效）
6. 7 天对照：deepseek 文本模型 agent_loop 命中率、日均未命中输入 token 均较基线
   改善，数字回填本文档

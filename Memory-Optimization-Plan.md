# 三层记忆系统优化与评估方案（Memory-Optimization-Plan）

> 状态：设计定稿，待实施
> 日期：2026-10-20
> 关联文档：`Bond-System-Design.md`（§8 多模态记忆、P5 记忆闭环）、`Cache-Hit-Rate-Plan.md`（提示词分层约束）、`Profile-Fact-Extraction-Plan.md`（抽取管线）、`docs/QQBOTAGENT-IMPROVEMENT-PLAN.md`（基线先行方法论）
>
> 本文所有"现状"结论均来自对 `QQBot/agent/memory.py`、`agent/profile.py`、`agent/fact_filter.py`、`agent/agent.py`、`test_agent.py` 的实读，常量与行为已逐项核对。参考系为 Claude 记忆机制（Claude Code 自动记忆 / API memory tool / context editing）的公开架构。

---

## 0. 摘要

三层记忆引擎（SHORT→MEDIUM→LONG）的**机械层是健全的**——20 个引擎单测 + 12 个抽取管线单测全部通过，缓存分层（L4 尾部注入）纪律执行到位。它缺的是三样东西：

1. **一个评估体系**：没有任何量化指标回答"记忆系统到底有没有用"，架构改动无法验收；
2. **LONG 层的读通路**：置信度最高的记忆（count>10）目前**只写不读**，且 MEDIUM twin 被 age 降级后该记忆彻底不可达——这是 bug 级设计缺口；
3. **对"事件"和"关键低频事实"的通道**：count 驱动的晋升机制是为高频事实设计的，Bond P5 的事件卡（一次性、高显著）在当前机制下活不下来。

本方案 = **一套三层 benchmark（第 3 节） + 七个优化点（第 4 节） + 实施顺序（第 5 节）**。方法论沿用 Cache-Hit-Rate-Plan 的纪律：**基线先行**——任何架构改动动手前先跑 benchmark 存基线，改完跑对比出回归表。

---

## 1. 现状：已验证的引擎事实

### 1.1 三层引擎常量（`agent/memory.py:324-340`）

```
SHORT_MAX = 50                # 尾部淘汰
SHORT_PROMOTE_AT_COUNT = 3    # count>=3 → MEDIUM
MEDIUM_LONG_THRESHOLD = 10    # count>10 → 创建 LONG（twin 留在 MEDIUM）
MEDIUM_DOWNGRADE_AGE = 30     # age>30 → 降回 SHORT
MEDIUM_DOWNGRADE_COUNT_RESET = 2  # 降级后 count 重置为 2（再提 1 次即回 MEDIUM）
MEDIUM_MAX = 60               # 超限强制降级最高 age
MEDIUM_INJECT_TOP_N = 15      # 12 条按 count 排序 + 3 条新晋升
MEDIUM_INJECT_TOKEN_CAP = 600 # 字符
LOW_CONFIDENCE_THRESHOLD = 5  # count<5 → "(低置信)" 标签
```

`age = extraction_count - entry_extraction_index`，逻辑时钟**每成功抽取一批 +1**；`PROFILE_BATCH_K = 5`（`agent/profile.py:50`），即 **age 的单位 ≈ 5 个对话轮**。`age>30` ≈ 150 轮未提及才降级——比直觉温和，降级后再提一次即回升。

### 1.2 管线与注入

```
用户消息 → agent.run() 回合结束
  → _schedule_profile_update(user_id, user_message, final_content)   # 每轮触发，与 fold/压缩解耦
    → ProfileManager.observe_turn 缓冲（K=5 轮）
      → Layer1: flash 模型合并抽取（事实候选 + judge 判定 update/keep/new）
      → Layer2: fact_filter 规则过滤（bot_state/gacha/regex/未解析指代/纯数字…）
      → TieredMemory.ingest_candidates（同步原子 apply，无锁）
注入：MEDIUM top-15（600 字 cap）→ L4 易变尾部 system 消息（Cache-Hit-Rate Phase 3）
```

已验证的两个重要事实：

- **"save before clear" 已天然覆盖**：task fold / 阶梯压缩折叠的是**会话历史**，抽取管线消费的是**实时轮次**（`agent.py` 的 `_schedule_profile_update` 在 fold 分支之外、每轮携带完整 `final_content` 调用）。唯一缺口：observe_turn 的 5 轮缓冲仅在内存，进程崩溃丢 ≤5 轮（见 ⑦）。
- **LONG 层是坟场**：`memory.py:309` 注释明确 "P1 = create + persist ONLY (no index injection, no query tool)"。晋升 LONG 时 twin 留在 MEDIUM 维持可达性，但 **twin 仍受 age>30 降级**——降级后 LONG 副本无任何读通路，这条全系统置信度最高的记忆从注入中消失。

### 1.3 现有测试资产

- `TestTieredMemory`（20 项）：单点机制断言——晋升/降级/LONG 创建幂等/注入选择与截断/持久化 round-trip；
- `TestMergedExtraction`（5 项）：Layer1 合并管线；
- `TestProfileExtraction`（11 项）：Layer2 过滤精度；
- `TestCacheStability`（6 项）：L1 头不变量 / L4 尾部 / 前缀字节稳定。

**缺的是场景流测试**：长时间序列下的动力学（存活曲线、容量压力、搅动率）没有任何覆盖。这正是 benchmark L1 要补的。

---

## 2. 参考系：Claude 记忆机制的可迁移要点

对 Claude 三套记忆系统（Claude Code 自动记忆、API memory tool + context editing、Claude.ai 消费端记忆）的考察结论，只列对本项目有迁移价值的模式：

| Claude 模式 | 机制 | 对本项目的映射 |
|---|---|---|
| **索引常驻 + 详情按需** | MEMORY.md 一行式索引始终在上下文（~200 行封顶），详情文件按需 Read | LONG 层索引行注入 + `recall_memory` 工具（优化点 ①） |
| **负面清单：可再推导的不记** | 代码模式/架构/git 历史/调试解法一律不存——可从现状重新推导 | Layer1 litmus 加"工具随时可查的不进记忆"（优化点 ②） |
| **写入时类型化** | user/feedback/project/reference 四类，写入时刻打标，feedback 强制 Why + How to apply 三段式 | 抽取 schema 加 category 字段（优化点 ④） |
| **事件记忆 = agent 环内当场写** | Claude Code 记"这次发现了什么"是主动 Write，不经频率累积 | 剧情场景结束时 `save_episode` 直入 MEDIUM（优化点 ⑤） |
| **记忆 = 写入时刻为真的断言** | 使用前验证，冲突信现状，记忆随现状更新 | 注入行渲染"（截至 X 月）"时效标注（优化点 ③） |
| **无向量库** | 文件导航 + 模型自主检索；量级不需要 embedding | 与 Bond §8.1 判断一致，维持 |
| **save before clear** | context editing 清除旧 tool result 前通知模型持久化 | 已天然覆盖（§1.2），仅需补缓冲落盘（优化点 ⑦） |

---

## 3. Benchmark 设计

### 3.0 要回答的三个架构问题

benchmark 不只是验收工具，它的首要产出是这三个问题的量化答案：

- **Q1 低频关键事实的存活曲线是什么形状？**（花生过敏只说一次，多少周期后从注入中消失？降级后 SHORT 里还能活多久？）
- **Q2 一次性事件在当前引擎下的存活率是多少？**（预期 ≈0——这个失败数字就是 Bond P5 架构改造的验收基线）
- **Q3 注入块的逐轮字节搅动率是多少？**（P5 后动态层还要塞 bond 块 ~200 token + 影音记忆 top-5，需要现在的基线数字）

### 3.1 L1 引擎级（确定性、零 LLM、进 CI）

直接驱动 `TieredMemory.ingest_candidates()`，JSON 场景文件喂脚本化 candidate 流 + judge 判定，检查点断言。与 TestTieredMemory 的区别：**那是单点机制断言，这是长时间序列的场景流**。

| 场景类 | 测什么 | 指标 |
|---|---|---|
| 存活曲线 | 一条记忆晋升后不再强化，逐周期记录层级 | 存活周期数、降级时刻、最终消失时刻（Q1） |
| 容量压力 | SHORT 灌满 50 / MEDIUM 灌满 60 | 淘汰顺序符合设计（尾部 / 最高 age 优先） |
| episodic canary | 一次性事件只 ingest 一次 | N 周期后是否存活（**预期当前失败**，Q2 的回归门） |
| LONG 可达性 | count>10 晋升后 twin 降级 | LONG 条目能否被任何通路读到（**预期当前失败**，① 的回归门） |
| 注入稳定性 | 合成 60 条 MEDIUM，模拟连续强化 | 注入串逐轮字节 diff 率、600 cap、top-15 选择确定性（Q3） |
| 持久化 | 批量中途崩溃重启 | round-trip 一致性（mtime 校验纪律） |

### 3.2 L2 管线级（真 LLM、离线跑、env 门控不进 CI）

合成 persona 脚本走完整抽取管线（observe_turn → Layer1 → Layer2 → judge → 引擎）。**每条事实带类别标签作为 ground truth**：

```
persona 事实清单（每类 3-5 条）：
  stable_preference   「只玩辅助」           → 期望：晋升且保留
  evolving_fact       「养猫」→20轮后「改养狗」 → 期望：judge 覆盖，终态单值
  one_shot_junk       「今天天气不错」×5      → 期望：Layer2 拦截，永不进 MEDIUM
  re_derivable        「雷电将军是雷系」（wiki 可查）→ 期望：不进记忆（② 的回归门）
  critical_lowfreq    「花生过敏」仅提 1 次    → 期望：存活（Q1）
  salient_episode     「我们吵了一架和好了」    → 期望：存活（Q2）
  contradiction       两条矛盾事实各提 2 次    → 期望：judge update 而非并存
  implanted_false     从未说过的事实          → 期望：不出现（假记忆率）
+ 50 轮闲聊干扰流穿插
```

**指标**（对照 fact sheet 确定性打分；QA 阶段 LLM judge + 期望实体表）：

| 指标 | 定义 | 对应优化点 |
|---|---|---|
| 抽取 P/R（按类别分解） | Layer1+Layer2 对 fact sheet 的查准/查全 | ② |
| judge 正确率 | evolving/contradiction 的 update 正确性 | ③ |
| 假晋升率 | junk / re_derivable 进 MEDIUM 的比例 | ② |
| 关键事实存活率 | critical_lowfreq 在 30/60/90 周期后留存 | ③④ |
| LONG 可达 QA | twin 降级后 LONG 事实的问答准确率 | ① |
| 干扰后 QA 准确率 | 50 轮闲聊后问 persona 事实 | 全局 |
| 假记忆率 | implanted_false 被接受的比例 | 全局 |
| token 成本 | 每 10 轮抽取+注入 token（接 TokenLedger） | ⑥ |

成本控制：6-8 个 persona × ~80 轮，抽取全走 flash，判分走 reasoning（每 persona ~30 题）。单次完整跑成本个位数元，架构改动前后各跑一次做 A/B。

### 3.3 L3 端到端（可选，只在里程碑跑）

完整 `agent.run()` + LLM 模拟用户对话，rubric 判分"是否真的用上了记忆"（称呼、偏好遵循、跨天任务续接）。方差大成本高，不作回归门，只在 Bond P0/P5 上线前跑基线。

### 3.4 多模态扩展位（P4/P5 启用，现在只留接口）

对应 Bond §8 三层结构，复用 L1/L2 runner：

1. **资产卡检索**：合成 100-500 张资产卡/用户，模糊描述查询（"雨夜那张立绘"），测关键词检索 + LLM 重排的 Recall@5、跨角色/跨用户误检率（隔离性）；
2. **淘汰策略模拟**：脚本化 recall 访问轨迹，测 `recall_count × 时间衰减` LRU 对 oracle 价值模型的 regret（未来 30 天会被用到的资产被淘汰的比例）；
3. **动态层总预算**：bond 块 + 影音记忆 top-5 + MEDIUM top-15 合并后的总尺寸与搅动率；断言 L0-L2 前缀逐字节不变（复用 TestCacheStability 的 hash 手法）；
4. **事件卡蒸馏质量**（若保留事后蒸馏路径）：N 张资产卡 → 1 条 episode，LLM judge 对照源高光打分。

### 3.5 落地形态与纪律

```
QQBot/benchmarks/memory/
├── scenarios/        # L1 JSON 场景流
├── personas/         # L2 persona 卡 + fact sheet（带类别标签）
├── bench_engine.py   # L1 runner（零依赖，挂 test.sh；报告/基线/对比已并入，
│                     #   不再单设 report.py——L1 报告逻辑 ~100 行不值得独立模块）
├── bench_pipeline.py # L2 runner（需 BENCH_API_KEY，env 门控）
└── baselines/        # 基线快照进 git，对比模式出回归表
```

> **批次 0 L1 实施记录（2026-10-20）**：7 场景（survival_curve / short_capacity /
> medium_capacity / episodic_canary / long_reachability / injection_stability /
> persistence_roundtrip）。场景 JSON 支持 loop 宏 + `{expr}` 安全求值（ast，无 eval）、
> track 存活曲线采样、probe 搅动率采样、save/reload 持久化步骤；旧引擎缺失 API 以
> `api_missing` 如实 FAIL（worktree@0c6e39b 复跑即"优化前"基线）。
> **基线答案**：Q1 = 3 次提及晋升（cycle 3）、age>30 于 cycle 35 降级、普通流量下
> cycle 85 彻底不可达（1 抽取批次≈5 轮对话 → 降级≈175 轮、消失≈425 轮）；
> Q2 = 一次性事件 cycle 51 被 SHORT 容量淘汰，存活率 0（CANARY-FAIL 如实记录，⑤ 的回归门）；
> Q3 = churn mean 3.1% 字节/轮、68% 的轮次注入块有字节变化（⑥ 的基线数字）。
> 批次 1 前后对比：long_reachability、persistence_roundtrip FAIL→PASS（IMPROVED），
> 其余动力学数字逐项一致（批次 1 未扰动晋升/降级/容量行为），回归 0。

> **批次 0 L2 实施记录（2026-10-20）**：`bench_pipeline.py` + 6 persona 卡
> （p1 手游学生 / p2 二胎职场妈妈 / p3 健身教练 / p4 独立开发者 / p5 二次元 / p6 退休教师）
> + 共享闲聊干扰池 `fillers.json`（55 条）。每 persona 24 条 ground-truth 事实
> × 8 类别（stable_preference / evolving_fact / one_shot_junk / re_derivable /
> critical_lowfreq / salient_episode / contradiction / implanted_false）。
> 走**真实生产管线**（observe_turn → Layer1 LLM 抽取+judge → Layer2 过滤 → 三层引擎），
> 确定性脚本：事实事件占偶数轮 round-robin（同事实跨批次间隔 ≈42 轮，考验 judge 路由
> 而非同批去重），pre 45 提及（轮 2-90）→ post 12 提及（evolves/contradicts，轮 92-114）
> → 共 120 轮/persona ≈ 24 抽取批次。打分 = 关键词组匹配（元素间 AND、元素内同义备选
> OR——对抗 LLM 改写漂移；implanted_false 用 OR+否定前缀护栏，「不能吃辣」不误报植入词
> 「能吃辣」）；语料 = short/medium 内容 + long title/summary + 画像槽位，**不含** long
> 快照原文（避免 absent 类假阳性）。evolved/single_value 共用替换判定：融合改写
> （「本命从初音换成崩铁」）算新状态在场 → MERGED 通过；独立旧值残留 → CONTRADICTED/STALE 失败。
> canary 只报告不计分。BenchClient 本地累计 token（不碰生产 TokenLedger）；
> BENCH_API_KEY env 门控，**不进 test.sh**；退出码恒 0（诊断），--strict 才判 1。
> 开放式 QA（向 bot 提问核对记忆）推迟到 L3。跑法：
> `BENCH_API_KEY=sk-xxx python benchmarks/memory/bench_pipeline.py --save-baseline baselines/l2_post_batch1.json`。
>
> **L2 基线结果（2026-10-21，engine@e69f011，deepseek-chat，两次全量运行总数一致）**：
> **106/126 = 84%**，canary 17/18，泄漏 5，每 persona 24 批次 / ~66k token（cache 命中 ~68%）/ ~27s。
> 满分项：re_derivable 18/18、implanted_false 18/18 全 absent（② 负面清单+石蕊测试有效）；
> critical_lowfreq 18/18 全存（单次提及的关键事实进 SHORT 存活）。
> **失败画像（20 项计分失败，全部为真实管线缺陷，打分工件已在冒烟阶段校准清零）**：
> ⑴ **纠正后旧值残留 ×11**（主要失败模式）：明确纠正到达后 judge 走 "new" 而非
>   "update"+matched_id，旧 SHORT 条目带着 count 继续存活并与新值并存
>   （「用户住在杭州」+「已搬到南京」）——⑧ 的 prompt 级修复命中率不足，
>   需要引擎级兜底（ingest 前矛盾扫描/近重复合并，候选新优化点 ⑨）；
> ⑵ **profile.preferences 槽位无纠正路径 ×4**（结构性缺口）：「只看日漫」「从不氪金」
>   「不喝咖啡」「安卓手机」等偏好槽写入后永不修订——抽取规则只允许"新增"偏好，
>   需支持 revise/delete 槽位（候选新优化点 ⑩）；
> ⑶ **重复 junk 晋升 ×5**：3-5 次提及的一次性抱怨（腿酸/快递/门票/蛋白粉）过了
>   Layer1 并被 count 机制晋升 MEDIUM——重复提及 ≠ 石蕊测试通过，负面清单对
>   "一次性状态抱怨"拦截不稳定（p3 三条全漏）。
> 波动说明：LLM 抽取非确定，两次运行总分同为 84% 但逐 fact 有 ±3 项漂移；
> 基线应在**类别级**解读，逐 fact 对比仅用于抓大回归。
> p5 ct2 基线状态 STALE 为标签工件（「氪金」改写躲过关键词「氪了」），卡片与
> marker 已修，修复后同场景判 CONTRADICTED——同为 FAIL，不影响总数。

- **基线先行**：任何记忆架构改动动手前跑 L1+L2 存基线，改完跑对比——与 Cache-Hit-Rate-Plan 阶段 2 同一套方法论，共享 prefix-hash 与 TokenLedger 基础设施；
  ⚠️ 实际执行偏离（用户 2026-10-20 决定）：批次 1（①②⑦⑧）先于 benchmark 实施，以单测代偿验证；批次 0 建基线时需**额外回补**"优化后 vs 优化前"的对照口径（旧行为可由 git 历史复现）；
- 不用真实用户聊天记录做 ground truth（隐私 + 标注成本）；如需真实分布基线，仅用 admin 自己的会话且只统计聚合指标（假晋升率、注入尺寸），不做逐条对错判定；
- 判分在 benchmark 侧做，**不要求 bot 主回复输出结构化 JSON**（docs/QQBOTAGENT-IMPROVEMENT-PLAN.md 第 5 节的教训）。

---

## 4. 优化点

### 第一梯队：便宜、独立、立即可做

#### ① LONG 层索引注入 + `recall_memory` 工具（修 twin 漏洞） ✅ 已实施（2026-10-20）

> 实施记录：`memory.py` 新增 `build_long_index()`（上限 `LONG_INJECT_MAX_LINES=20`，newest-first，仅取 write-once 字段 → 逐字节稳定）+ `recall_long()`（id 前缀/关键词，≤3 条，快照 cap 1200 字，query_count/query_log 统计落盘供 P2 用）；`agent.py` L4 尾部注入索引；`agent_router.py` 注册 `recall_memory` 工具（contextvar 用户隔离）；permissions 归 `_PUBLIC_TOOLS`；TOOLS.md 补文档；单测 3 处（index 空/截断/字节稳定、recall 查找/统计/隔离、L4 尾部注入断言）。

- **现状**：LONG 只写不读；twin 降级后高置信记忆不可达（§1.2）。
- **做法**：搬 Claude Code 的"索引常驻 + 详情按需"——
  - LONG 条目各注入一行索引进 L4 尾部（`- 用户花生过敏（长期记忆）`，~30 字/条）；
  - 注册 `recall_memory(id|关键词)` 工具，返回 LONG 全文 + `snapshot_turns`；
  - 注入行提示模型"更早/更深的记忆用 recall_memory 查"。
- **成本**：`memory.py` 加 `build_long_index()`；工具注册 + permissions 归 `_PUBLIC_TOOLS`；LONG 集合变化极慢，索引行搅动天然低于 MEDIUM。
- **注意**：LONG 目前无 cap 无降级（P2 议题），索引注入要预留行数上限（如 20 行，超限截断最旧）防远期膨胀。
- **Benchmark 钩子**：L1"LONG 可达性"场景转正；L2"LONG 可达 QA"从 0 到可测。

#### ② 负面清单补全："可再推导的不记" ✅ 已实施（2026-10-20，仅 Layer1）

> 实施记录 + **刻意偏离**：只在 Layer1 抽取 prompt 做——石蕊测试后新增「次要判据：可再推导测试」章节（含边界示例：「用户玩原神」记 /「等级上限90」不记 / 查询行为≠兴趣）+ few-shot 例6（查流浪者突破材料 → {}）。**未**给 `fact_filter.py` 加 `re_derivable` 规则类：游戏/wiki 词在「可再推导数据」与「合法兴趣」间高度歧义（"原神"既出现在 wiki 事实也出现在用户自述），按 fact_filter 设计原则 1（只黑无歧义复合词，歧义归 Layer1）否决了 L2 兜底。单测锁定 prompt token + L2-X 无误杀。

- **现状**：Layer2 挡"不值得记的"（bot_state/gacha/regex 类），不挡"能重新查到的"。
- **做法**：Layer1 抽取 prompt litmus 加一条——"如果 Roxy 用工具随时能查到（wiki 数据/天气/时间/兑换码状态），它不是记忆"；~~`fact_filter.py` 加 `re_derivable` 规则类兜底~~（否决，见上）。
- **收益**：直接压假晋升率分母、缩小 600 字预算的浪费、减少必然过期的内容。
- **Benchmark 钩子**：L2 persona 的 `re_derivable` 类别。

#### ⑦ observe_turn 缓冲落盘（顺手做） ✅ 已实施（2026-10-20，原子 JSON 而非 JSONL）

> 实施记录 + **刻意偏离**：不用 JSONL append，而是 `{base_dir}/{safe_id}/buffer.json`（profile.json 的兄弟文件）整体原子写（tmp + os.replace）——缓冲本身上限 8 轮，整文件比 append 日志更简单且无半行损坏问题。写入点：observe_turn 每次 append、_flush 重置、delete-flush 丢弃尾部。恢复点：懒加载（重启后首次 observe_turn 触发 `_restore_buffer`），不在启动时全量扫描。**关键顺序纪律**：_flush 在调度抽取任务**之前**持久化 pending_n=0 → 崩溃于抽取中途丢该批（与旧行为一致）但绝不重启后双重抽取。corrupt/缺失文件静默忽略。单测：跨"进程"round-trip + flush-once + 损坏容忍。

- **现状**：5 轮缓冲仅在内存，崩溃丢 ≤5 轮抽取原料。
- **做法**：~~缓冲 append 写 JSONL（`data/profiles/{user}_buffer.jsonl`）~~ → buffer.json 原子整体写（见上）；启动时若有残留先补抽。

#### ⑧ 用户纠正处理（chat-export 冲突消解改进点，批次 1 追加） ✅ 已实施（2026-10-20）

> 来源：docs/chat-export-1792425090111.json 的"负面反馈/冲突消解"建议。做法：Layer1 judge 路由加一条纠正规则（用户纠正/推翻已有记忆 → 必须 judge="update" + matched_id，content=纠正后完整事实句，禁止无视纠正或另建矛盾新记忆）+ few-shot 例7（"不跑步改游泳" → update m7）。已核实纠正句（"改为/不再/现在…"）不被 L2 任何规则误杀（transient_state 类别此前已整体移除），并在 L2-X 单测加两条纠正句防回归。

### 第二梯队：schema 改动，是 ③⑤ 的地基

#### ④ 写入时类型化（category 字段）

- **现状**：抽取产物是扁平"事实"，无类别；Bond §8.2 反正要加 `mem_type: asset/episode`。
- **做法**：Layer1 JSON schema 加 `category: preference | fact | event | critical`，存入 `ShortItem`/`MediumItem`（`TIER_SCHEMA_VERSION` +1，迁移脚本沿用 `scripts/migrate_memory_p1.py` 模式，缺省回填 `fact`）。
- **解锁**：critical 类独立降级参数（age 阈值 ×3，配置表）；event 类走 ⑤ 通道；注入渲染按类分组（配合 ⑥）。
- **时机**：在 Bond P0 动 schema 前做掉，迁移一次完成。

#### ⑤ Episode"当场写"通道（salience 取代 frequency）

- **现状**：Bond §8.2B 设想"事件卡直接进现成晋升机制"——**不成立**：事件一次性发生，count 永远到不了 3；就算用户反复提起进了 MEDIUM，age>30 照样降级。频率驱动的晋升是为事实设计的，不是为事件设计的。
- **做法**（Claude Code 范式：事件记忆 = agent 环内当场写）：
  - 角色面社交工具 `save_episode(摘要, salience)`：剧情场景结束 / 用户显式"帮我记住这件事"时调用；
  - **直入 MEDIUM**（跳过 count>=3 门），`category=event`；
  - 降级用 salience 加权 age（高 salience 事件 age 阈值 ×N 或豁免），不再事后从对话流蒸馏。
- **收益**：保真度高于事后蒸馏（场景上下文在生成时刻还在环内）、省一次 LLM 调用、顺带给用户显式记忆入口。
- **依赖**：④ 的 category 字段；Bond P0 的角色面工具集。
- **Benchmark 钩子**：L1"episodic canary"转正；L2 `salient_episode` 存活率。

#### ③ 时效标注："截至 X 月"

- **现状**：evolving 事实（住址/工作/感情）注入时无时态，模型把旧断言当现在时。
- **做法**：注入渲染时，`last_confirmed`（或条目创建批次换算的日期）超过阈值 → 渲染为 `用户住在北京（截至 3 月）`。不 pin、不加交互摩擦，只让模型说话自带时态——Claude"记忆 = 写入时刻为真的断言"纪律的最便宜实现。
- **依赖**：④ 之后按 category 精确控制；之前可先用条目 age 粗做。
- **可选延伸**（贵，缓做）：critical 类事实临近降级时主动向用户确认一次（"上次你说花生过敏，现在还是吗？"），确认即强化 count——防陈旧效果最好，但加交互摩擦，仅对 critical 类开。

#### ⑥ 注入排序稳定化

- **现状**：top-12 按 count 排序，每次强化都可能重排整个注入块。
- **做法**：count 分档（低 <5 / 中 5-9 / 高 ≥10）+ 档内按插入序；+1 强化通常不改字节，跨档才重排。
- **收益定位要准确**：注入在 L4 尾部，重排**不破坏** L1-L3+历史的缓存前缀——省的是每轮 L4 token 成本和输出稳定性，**不计入缓存命中率账**。
- **前置**：等 benchmark Q3 搅动率基线出来，数字难看才做。

---

## 5. 依赖关系与实施顺序

```
① LONG索引+recall ──独立──→ 第 1 批（直接修 twin 漏洞，收益/成本比最高）
② 负面清单       ──独立──→ 第 1 批
⑦ 缓冲落盘       ──独立──→ 第 1 批（顺手）
④ category 类型化 ──┬→ ③ 时效标注（可先用 age 粗做提前上）
                   └→ ⑤ episode 通道（Bond P5 前置，P0 动 schema 前完成 ④）
⑥ 排序稳定化     ──独立──→ 等 Q3 基线数据再定
```

| 批次 | 内容 | 前置 | 验收 |
|---|---|---|---|
| **0** ✅ 已完成（2026-10-21） | benchmark L1（含 episodic canary、LONG 可达性、存活曲线）+ L2 persona 与 runner | 无 | 基线快照进 `baselines/`（L1 双基线 + L2 post_batch1: **84%**，canary 17/18 如实记录）；L2 失败画像见 §3.5：纠正残留 ×11 / 偏好槽无纠正 ×4 / 重复 junk 晋升 ×5 → 候选优化点 ⑨⑩ |
| **1** ✅ 已实施（2026-10-20，先于批次 0，用户决定） | ① LONG 索引+recall；② 负面清单；⑦ 缓冲落盘；⑧ 纠正处理（追加） | ~~批次 0~~ → 单测代偿（test_agent.py 15/15） | L1 两个失败场景转正；L2 假晋升率对比基线下降（**待批次 0 建基线后回补验证**） |
| **2** | ④ category 类型化（含迁移脚本）→ ③ 时效标注 | 批次 1（避免同期多变量） | L2 evolving/critical 类别指标 |
| **3** | ⑤ episode 通道（随 Bond P0/P5 排期） | ④ | episodic canary 转正；`salient_episode` 存活率 |
| **4** | ⑥ 排序稳定化（视 Q3 数据决定做不做）；多模态扩展位启用（随 Bond P4/P5） | 批次 0 基线 | 搅动率下降且 QA 不降 |

---

## 6. 明确不做的

| 项 | 理由 |
|---|---|
| 向量库 / embedding 检索 | 单用户量级百~千，关键词 + 歧义时一次 LLM 重排足够（Bond §8.1 已论证）；Claude 全线同样不用 |
| 语义检索器（Cross-Encoder）按 query 动态召回 | 与 L4 注入稳定性冲突、服务器无余量；MEDIUM top-15 跨轮稳定是特性不是缺陷 |
| 主回复强制结构化输出配合判分 | QQ 场景大量闲聊/RP，判分在 benchmark 侧做（改进方案第 5 节教训） |
| 真实用户聊天记录做 ground truth | 隐私 + 标注成本；仅 admin 自会话做聚合指标 |
| reward 驱动的记忆权重调整 | QQ 闲聊的隐式反馈信噪比过低，会污染晋升；如做也只"记录不生效" |
| 给记忆加"缺席惩罚"式衰减 | 与 Bond"好感永不衰减"同一产品哲学：机器人是陪伴品不是电子鸡 |

---

## 7. 一句话总结

先用三层 benchmark 把"记忆系统有没有用"变成可回归的数字，然后按依赖序做四件事：**给 LONG 层装读通路（修最实际的漏洞）、给抽取加"可再推导不记"的负面清单、给记忆条目加类型、给事件开一条 salience 驱动的旁路**——前两件立即做，后两件是 Bond P5 记忆闭环的地基。

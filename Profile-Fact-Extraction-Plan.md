# 画像瘦身与三级记忆引擎方案（Profile Slimming & Three-Tier Memory Engine）

> 状态（2026-09-19 按代码实况更新）：**P0 与 P1 均已实现并合入 dev**（文件名沿用 `Profile-Fact-Extraction-Plan.md`）
> - **P0 止血** ✅：commit `361ea54`（fact_filter.py / cleanup_profile_facts.py / profile.py 瘦身等），
>   已于 2026-09-14 部署生产并完成存量清理（见 `.claude/deploy-server.md` §P0 部署记录）。
> - **P1 三级引擎** ✅：commit `9d93dd2`（TieredMemory）+ `43df292`（迁移脚本修复）+ `9da0f12`
>   （config/MEMORY.md 重写并接入 system prompt——原属 P3 的 Decision K 提前落地）。
>   迁移脚本 `QQBot/scripts/migrate_memory_p1.py` 已实现，生产部署验收见 `P1-Deployment-Acceptance-Plan.md`。
> - **P2（agentic RAG）** ❌ 未动手：LONG 目前仅 create+persist，无 query 工具/索引注入/上限降级。
> - **P3（死文件）** 部分完成：MEMORY.md 已重写接入（9da0f12）；TOOLS.md 裁剪接入、弃置文件标记未做。
>
> 范围：机制①画像瘦身（`profile.py`）+ 机制②三级记忆引擎（`memory.py` / `agent.py`）+ 抽取精度三层 + 死文件处理
> 落地顺序：本地改 → `bash test.sh` 验零回归 → 审 diff → 部署（commit → 服务器 pull → 重启 bot）
> ⚠️ 生产环境有真实用户在用，所有改动走既定部署流程，不在服务器上直接验证。
>
> 📝 阅读提示：§1–§4 是**动手前的问题分析**（描述的是 P0 之前的旧代码状态，行号已过时，保留作根因档案）；
> §5–§13 中已实现部分的**实际落地形态**以各节的 ✅ 实况注记为准。

---

## 1. 问题

「用户画像」（ProfileManager 产出的 facts）里堆积了大量**临时性、无价值、甚至错误**的事实。真实例子：

- `用户工作区总用量约250 MB/2048 MB` —— bot 内部状态
- `用户在抽取结果中获得了2个4星紫色羁绊（【催促的回眸】、【潜藏的爱意】）和8个3星蓝色物品` —— 本轮瞬时结果
- `用户在进行常规招募十连抽时指定了UP角色为'蝶子'，但未抽到该角色` —— 本轮瞬时结果
- `用户识别为"团长"称呼` —— **来自 bot 自己的人格口癖**，不是用户说的
- `用户有角色'露比'，其性格与游戏角色相似` —— 截图/人格派生，非用户陈述
- `用户使用战斗截图测速/行动值分析，提及角色速度数值（猫祭135）` —— 截图派生 + 瞬时数值
- `用户正在参与一个名为「hadoop 期末项目」的特殊会话` —— **泄漏 bot 内部架构**（"特殊会话"是系统概念）
- `用户要求总结该项目的匹配方法` —— **模糊指代**（"该项目"脱离上下文无法解析）

这些垃圾 fact 通过 `to_prompt_context()`（旧 `profile.py:80`，注入 `facts[-10:]`）回流进 system prompt，造成提示词污染、误导后续回复。

> ✅ **实况（P0 后）**：`to_prompt_context()`（现 `profile.py:100-124`）只注入 nickname/interests/preferences
> 类型化槽，**不再注入 facts**；`facts` 字段保留在 schema 中但已 **dormant**（不抽取、不注入，见 §6 实况注记）。

---

## 2. 根因分析（均已在代码中核实）

### 2.1 两套记忆机制，都是纯代码驱动，都不读 `config/MEMORY.md`

| 机制 | 实现 | 如何决定存什么 | 受 md 控制？ |
|---|---|---|---|
| ① 用户画像 facts | `profile.py` `extract_and_update()` | reasoning 模型 + **硬编码 prompt（`profile.py:204-236`）** | ❌ 不读任何 md |
| ② 长期记忆 | `agent.py` `_maybe_remember()`（`:649`） | **纯长度阈值** `MIN_REMEMBER_LEN=800`，超阈值就把对话**原样 dump**，无 LLM 判断 | ❌ 不读任何 md |

调用链：`agent.py:410` `_schedule_profile_update(user_id, user_message, final_content)` → `:694` `create_task(extract_and_update(...))`，**每轮都触发**。`final_content` = `response.get("content","")`（`agent.py:346`）= Roxy 回给用户的最终文本。

> ✅ **实况（P1 后，行号已变）**：
> - 机制①：`extract_and_update` 单轮抽取已被 **`observe_turn` 缓冲（`profile.py:299-328`）+ 满 `PROFILE_BATCH_K=5`
>   触发 `extract_batch`（`profile.py:379-427`）** 取代；抽取 prompt 从硬编码单轮改为 `_build_extraction_prompt`
>   （`profile.py:572` 起）的 8 轮窗口 + judge 清单合并版；客户端为 flash（`agent_router.py:924` `set_client(_model_router.flash_client)`）。
> - 机制②：`_maybe_remember` **函数体已删除**（`grep "def _maybe_remember"` = 0；agent.py 仅存 2 处注释提及），
>   由 `TieredMemory`（`memory.py:385`）三级引擎取代。
> - 现调用链：`agent.py:414` `_schedule_profile_update(...)` → `:667` `profiles.observe_turn(...)`；
>   会话删除时 `agent_router.py:1644/:2032` 调 `flush_on_session_end`（`profile.py:330`，Decision J 尾批 flush）。

### 2.2 `config/MEMORY.md` 是死文件；TOOLS/SESSION/BOOTSTRAP 同样死（已坐实）

`agent.py` 全量 `_configs` 访问点核实：
- 加载：`_load_configs`（:121-133）把 6 个 md（SOUL/IDENTITY/TOOLS/AGENTS/BOOTSTRAP/SESSION）读进 `self._configs`。
- **使用：只有 `soul`(:146)、`identity`(:150)、`agents`(:154) 被拼进 system prompt。** 其余三个内容**从未被读取**。
- 唯一另一处引用 `:712` `"configs_loaded": list(self._configs.keys())` 只列**文件名**做状态汇报，不碰内容。
- `config/MEMORY.md` 更彻底：连 `_load_configs` 列表都不在；真正的 MemorySystem 索引是 `data/memory/MEMORY.md`（`agent_router.py:911`），与之无关。

**三个 md 的"真实来源"其实在代码里：**
- 工具说明 → `ToolRegistry` 注册时每个工具自带的 description + JSON Schema（`agent_router._build_tool_registry`），**非 TOOLS.md**。
- 会话参数 → 硬编码 `agent_router.py:905-907`（`max_context_messages=20, session_timeout=1800`）等，**非 SESSION.md**。
- 启动序列 → `bootstrap()`（:702）跑硬编码健康检查，**不解析 BOOTSTRAP.md**（:703 仅 docstring）。

→ 含义：若期望智能体遵守 TOOLS/SESSION/BOOTSTRAP/MEMORY 的规则，**它现在并没有**。详见 §9。

> ✅ **实况（`9da0f12` 后）——MEMORY.md 已复活，其余不变**：
> - `config/MEMORY.md` **已重写为三层引擎说明**（SHORT/MEDIUM/LONG 表格、`## 用户记忆（中期）` 注入格式、
>   `(低置信)` 用法，全文 33 行），并加入 `_load_configs` 加载列表（`agent.py:127`）、
>   注入 `build_system_prompt`（`agent.py:156-158`）。§2.2 首段"连加载列表都不在"对 MEMORY.md **已不成立**。
> - TOOLS / BOOTSTRAP / SESSION 仍是"加载但不注入"；HEARTBEAT / USER / WORKSPACE 仍全死。
> - 另有第二层记忆提示：MEDIUM 层内容经 `build_medium_injection`（`memory.py:626`）在 `_build_messages`
>   （`agent.py:516-519`）追加为 `## 用户记忆（中期）` 块。

### 2.3 抽取 prompt 早已禁止这些垃圾，但模型无视

`profile.py:222-235` 已明文禁止：workspace disk usage、session state、one-off requests、specific conversation outcomes（涵盖抽卡）、截图/工具输出派生 fact（`:225-232`，涵盖露比/测速/团长）、关于 assistant 的 fact（`:235`）。**规则齐全，是 reasoning 模型在忽略 guidelines** → 单纯往 prompt/md 加文字边际收益已很低，必须有代码兜底。

### 2.4 抽取输入的三个结构缺陷

1. **整段 prompt 作为单条 `role:user` 消息发出**（`deepseek_client.py:70-73`，无 system、无 history）。
   - 注：对"抽取"这种**元任务**，把对话拍平成带标签的**数据**其实是对的——若按 native roles 喂，模型会以为 assistant 轮是它自己说的话而出戏。**问题不在扁平本身，在下面的标签与窗口。**
2. **`"Your response: {agent_response[:500]}"` 误标 + 把 bot 回复当事实来源**：`agent_response` 是 Roxy 的回复，"Your" 却把它说成抽取模型自己的回复（角色错配）；更糟的是它让 bot 口癖（团长/露比）被洗成"用户事实"。`:235` 的"别抽取关于 assistant 的 fact"拦不住，因为这些被表述成关于*用户*的事实。
3. **单轮 + 粗暴 `[:500]` 截断**：抽取器只看最新一轮，**永远看不到更早轮次** → "该项目"的先行词不在输入里 → 模糊指代被原样存下。

### 2.5 中文去重形同虚设

`_similar()`（`profile.py:128-136`）用 `a.lower().split()` 做词重叠。中文无空格 → 整句变一个 token → 实际只认完全相同的串。"用户有露比" vs "用户拥有角色露比" 判为不相似，双双留存。

### 2.6 MAX_FACTS=40 cap 错了维度

按"最近"而非"质量"淘汰（`facts[-40:]` 丢最旧），且存 40 只注入 10、30 条仅供（已坏的）查重。**本方案直接移除画像的自由文本 facts（§6），此问题对画像自动消解**；上限概念移入三级记忆（§7）。

---

## 3. 对提交 `0c44a58` 的评估（"优化了智能体记忆相关规则，减少提示词污染"）

**结论：方向对，但不健全——核心问题是改错了机制，对画像垃圾零效果。**

- 该提交**只改了 `IDENTITY.md` 和 `MEMORY.md`，没碰 `profile.py`**。
- `MEMORY.md` 改动 → **完全不生效**（死文件，§2.2）。
- `IDENTITY.md` 改动 → 生效但**与垃圾无关**：去掉 "DeepSeek API +" 合理；端口 `8081/8080` → `a specific port` 把具体改模糊，不如直接删端口。
- `MEMORY.md` 措辞即便生效也有硬伤：`temperate`→应为 `temporary`（×2）；`Adminicle` 生僻词；`称呼 vs nickname` 自相矛盾；`Deletion in memory files` 语法退化。

---

## 4. 设计原则

1. **石蕊测试取代一长串禁令**：对每条候选问——
   > "如果这个用户从此再也不碰这个 bot，这条还成立吗？"

   一条即可干掉：工作区用量、本轮抽卡、截图测速、"正在参与特殊会话"、bot 口癖"团长"。
2. **确定性兜底 > prompt 说教**：模型已证明会无视 guidelines，防线落在**代码**里。
3. **精度优先于召回**：现状精度极差；抬精度但不误杀真实持久事实。
4. **频次 ≠ 持久性（关键共识 I）**：三级巩固引擎按出现次数升级，但复现型垃圾也会累积次数。**石蕊测试 + 确定性过滤必须在任何候选进入短期之前先跑**，否则只是更高效地巩固垃圾。

---

## 5. 架构决策总览（A–K，已与用户确认）

| # | 决策 |
|---|---|
| **A** | 三级记忆引擎 = **机制②（MemorySystem）**。画像（机制①）**瘦身为类型化槽**（nickname / interests / preferences），**移除自由文本 `facts`**。画像瘦身的持久知识改流入机制②三级。 |
| **B** | 语义重合判定 = **embedding 余弦 + LLM judge**；但因每用户记忆集小，**实际用 LLM judge 直接判即可，不需 embedding**（§8）。 |
| **C** | 长期检索 = **agentic RAG**：每轮注入精简标题/摘要索引，智能体选中后调 `query(name)` 展开完整记录。**无需 embedding / 无需训练词向量。** |
| **D** | 纯文本 prompt 里的"权重" = 给低置信项加 **`(低置信)` 标签**（顺序无法体现权重）。 |
| **E** | 短期 = **有界双端队列**（位置即优先级，容量 50）：提及→向入口前移 10 位；溢出→从出口淘汰。**升级在 count 跨过 2（即 +1 到 3）的那一刻立即触发，与队列位置无关**；队列位置只负责淘汰仍 ≤2 的项。 |
| **F** | 中期 = **最大堆，key = age**（age = 当前用户抽取次数 − 进入时间戳）。加权和的堆序对纯文本注入无意义，age 直接服务升降级。 |
| **G** | 中→长升级用**模型 Y**：新建长期对象（快照+溯源）并**保留中期孪生**（count 继续涨）。长→中降级：**删除长期快照 + 把中期孪生 count 设为 8**（否则 count>10 下一轮立即重新升级，释放上限无意义）。两对象不同粒度：中期=精炼事实句(注入)，长期=对话快照(深检索)。 |
| **H** | 短期**完全不注入**（它本就在近几轮会话上下文里可见），仅作巩固暂存区。 |
| **I** | 石蕊测试 + Layer 2 过滤**在进短期之前先跑**（见原则 4）。 |
| **J** | `PROFILE_BATCH_K = 5`；**不加空闲超时 flush**；改为**删除会话时**判断该会话轮数 >5 则把未抽取的尾批递交抽取，≤5 视为临时会话不抽取。**接受临时会话尾批丢失。** |
| **K** | `config/MEMORY.md` **接入 `build_system_prompt`**，作为机制②保存记忆时的规则注入；**前提是先把内容改成与三级引擎真实行为一致**（§9）。 |

> ✅ **落地对照（2026-09-19）**：
> - **A** ✅ P0 落地，但采取**保守形态**：`facts` 字段**保留在 schema**（向后兼容旧 profile.json）而非物理删除，
>   实际 dormant——不抽取（`_apply_extraction` 不处理 `new_facts`）、不注入（`to_prompt_context` 只用类型化槽）、
>   `merge_facts`/`_similar`/`MAX_FACTS` 保留但无调用方（`profile.py:43/:133/:162`，注释标 DORMANT）。
> - **B/D/E/H/I/J** ✅ P1 落地。E 的实现结构是**普通 list**（`insert(0,…)`/`pop()` 模拟双端队列），非 collections.deque。
> - **C/G** ⏸ 部分：LONG 仅 create+persist（`memory.py:309-311` 注释 "those are P2"）；`query(name)` 工具、
>   索引注入、`LONG_MAX` 上限降级、`query_count/query_log` 均属 **P2，未实现**。
> - **F** ✅ 但结构为普通 list + 按需排序（§12.1 YAGNI 决策落地，`memory.py:381` 注释），非真正 heapq 堆。
> - **K** ✅ **提前在 P1 完成**（commit `9da0f12`，原计划属 P3）。

---

## 6. 机制① 画像瘦身（`profile.py`）

**思路：让画像在 schema 上就不可能装垃圾——删掉自由文本 facts 字段。**

- `UserProfile` **移除 `facts: List[str]`**，只保留类型化画像槽：
  - `nickname`、`interests`、`preferences`（语言 / 回复风格等，本就"画像"）、元数据（`first_seen` / `last_seen` / `total_interactions`）。
- `to_prompt_context()` 只注入上述稳定槽 → **垃圾 fact 的载体消失**（字段不存在，无从堆积）。
- 原本进 `facts` 的持久知识（职业、地点、技能、长期项目）**改流入机制②三级记忆**（§7），在那里享受去重 / 巩固 / RAG。
- `extract_*` 的 JSON schema 去掉 `new_facts`，只留 `nickname` / `new_interests` / `new_preferences`；持久知识候选改走记忆管线（§7.1）。
- `MAX_FACTS` / `merge_facts` / `_similar`（中文去重 bug）随 facts 字段一并移除；去重逻辑迁到三级引擎的语义重合判定（§7.3）。

> ✅ **实况（P0/P1 后）**：瘦身按"dormant 而非物理删除"落地——`facts: List[str]` 仍在 `UserProfile`
> （`profile.py:65`）与 `to_dict`/`from_dict`（向后兼容），但 `_apply_extraction`（`:442`）不写它、
> `to_prompt_context`（`:100-124`）不读它；`merge_facts`（`:133-147`，注释 DORMANT）与 `_similar`（`:162-169`）
> 保留但零调用方；`MAX_FACTS=40`（`:43`）仅被 dormant 的 `merge_facts` 引用。
> 抽取 JSON schema 现为 `nickname`/`new_interests`/`new_preferences` + `memory_candidates[]`（后者进三级引擎）。
> 旧 facts 的持久知识经 `migrate_memory_p1.py` 播种进 SHORT、P0 误删项经 `--restore-false-drops` 恢复到 MEDIUM。

---

## 7. 机制② 三级记忆引擎（`memory.py` / `agent.py`）

> 取代 `_maybe_remember` 的"长度阈值原样 dump"。短/中/长三级，按出现次数 + age 巩固，长期走 agentic RAG。

### 7.1 统一抽取管线

- **`observe_turn(user_id, user_message, agent_response)`**：每轮调用，**只做内存缓冲追加**（`_pending[user_id]`）+ `touch()`；当 `len(buffer) >= PROFILE_BATCH_K(5)` → `create_task(extract_batch(user_id, turns))` 并清空缓冲。`agent.py:_schedule_profile_update`（:687）改调它。
- **尾批 flush（Decision J）**：在特殊会话删除处（`agent_router.py:1621` `_special_sessions.delete`）挂钩——若被删会话轮数 >5，把尚未抽取的尾批递交 `extract_batch`；≤5 视为临时、丢弃。临时会话自动清除/超时的尾批**接受丢失**。
- **`extract_batch(user_id, turns)`**：拼最近 N 轮 `<conversation>` 块（第三人称标签）→ **flash** 调用 → 产出 JSON：
  - 画像槽（`nickname` / `new_interests` / `new_preferences`）→ 更新瘦身后的 `UserProfile`；
  - 记忆候选 `memory_candidates[]` → **先过石蕊 + Layer 2 过滤（I）** → 逐条路由进三级（§7.2-7.4）。
- 客户端：`agent_router.py:923` 改 `set_client(_model_router.flash_client)`（`model_router.py:84`）。

> ✅ **实况**：管线已按此落地——`observe_turn`（`profile.py:299-328`，deque(maxlen=EXTRACT_WINDOW_N=8) 缓冲 +
> `_pending_n` 计数 + `_inflight` 单飞）、`extract_batch`（`:379-427`）、尾批 flush `flush_on_session_end`
> （`:330`，挂钩 `agent_router.py:1644/:2032`）、`set_client(flash_client)`（`agent_router.py:924`）。
> **抽取与 judge 已合并为一次 flash 调用**（§7.9/§12.1 决策落地）：`get_judge_list()`（`memory.py:447`）取
> MEDIUM top-`JUDGE_MEDIUM_LIST_CAP`(40) + SHORT 全量嵌入同一 prompt，返回 JSON 同时含画像槽与
> `memory_candidates[]`（每条带 `judge` + `matched_id`），purpose=`"profile"` 计入 TokenLedger。

### 7.2 短期记忆 SHORT

- **内容**：count ≤ 2 的候选。
- **结构**：有界双端队列，位置即优先级；入口=前，出口=后；容量 `SHORT_MAX=50`。
- **入队**：新候选 count=1，置于入口（前）。
- **强化**（再次提及/抽取命中同一短期项）：count+1；并向入口方向前移 `SHORT_PRIORITY_STEP=10` 位（不超过最前）。
- **升级**：count 达到 3（跨过 2）→ **立即**升级为中期（与位置无关），移出短期、入中期堆。
- **淘汰**：入队致长度 > `SHORT_MAX` → 移除最靠近出口（后）的元素（必为 count≤2，直接丢弃）。
- **注入**：**不注入**（H）。
- ✅ **实况**：语义一致；实现结构为**普通 `List[ShortItem]`**（`memory.py:380`），`insert(0,…)` 入队首、
  `pop()` 尾部淘汰（`:542/:545`），非 `collections.deque`（需支持按位前移，deque 反而不便）。

### 7.3 中期记忆 MEDIUM

- **内容**：count ≥ 3 的**精炼事实句**。
- **结构**：**最大堆，key = age**（age = `当前用户抽取次数 − 进入时间戳`）→ 最旧（age 最大）在堆顶。
- **进入时间戳**：逻辑时钟 = 进入/最近一次强化时的"用户抽取次数"序号（**非物理时间**）。需对每用户维护 `extraction_count`。
- **注入**：**主动注入**（纯文本）。堆序**不**用于注入排序（纯文本无法体现顺序权重）；置信度用 `(低置信)` 标签表达（D）。
- **强化**（抽取候选与某中期项语义重合，**LLM judge** 判定）：LLM 在「更新细节 / 保持原样」二选一 → 该项 count+1（不会 <3）、`进入时间戳` 刷新为当前 `extraction_count`（age 归零）→ **re-heapify**（key 变动）。
- **降级 中→短**：`age > MEDIUM_DOWNGRADE_AGE` → 降为短期，count 设为 2。age 最大堆可从堆顶高效 poll 候选。
- **升级 中→长**：`count > MEDIUM_LONG_THRESHOLD(10)` → **新建长期对象**（携带触发升级那次抽取的 K 轮对话快照 + 物理时间）；**保留中期孪生**（count 继续涨，无上限）。[模型 Y]
- **实现注记**：强化改 age → 堆 key 变动 → 需 re-heapify 或带索引堆（indexed heap / 懒删除）。
- ✅ **实况**：按 §12.1 YAGNI 决策，**未用堆**——普通 `List[MediumItem]`（`memory.py:381` 注释
  "plain list, sorted on demand (§12.1: heap is YAGNI)"），注入/降级时按需排序（`:614`）。
  注入实现 = `build_medium_injection`（`:626`）：top-12 by count + 3 最近晋升（`MEDIUM_INJECT_RECENT_SLOTS=3`），
  正文截断至 `MEDIUM_INJECT_TOKEN_CAP=600` **字符**（非 token），count<`LOW_CONFIDENCE_THRESHOLD=5` 的项
  输出为 `- (低置信) <content>`。age 降级（>30 降回 SHORT、count 置 2）与 `MEDIUM_MAX=60` 安全网均已实现。

### 7.4 长期记忆 LONG

> ⏸ **实况（P1 边界）**：本节大部分属 **P2，尚未实现**。P1 只做了 **create + persist**：
> `_maybe_create_long`（`memory.py:582-600`）在 MEDIUM count 越过 `MEDIUM_LONG_THRESHOLD=10` 时创建 LONG
> 对象并把 K 轮快照写入 `data/memory/tiers/long/{uid}/{id}.md`（frontmatter + 正文）。
> **索引注入、`query(name)` 工具、`query_count`/`query_log`、`LONG_MAX` 上限降级均不存在**
> （`LONG_MAX`/`LONG_DOWNGRADE_COUNT_RESET` 两个常量在代码中 grep 为 0；`memory.py:309-311` 注释明示
> "no index injection, no query tool, no cap demotion — those are P2, §11"）。以下为 P2 设计原文。

- **不主动注入其对话快照上下文**；但注入一份**索引**（标题/摘要清单），来源 = 中期 `count>10` 的项 → 供 **agentic RAG**。
- **每个长期对象存**：触发升级那次抽取的 K 轮对话快照（含物理时间）、`query_count`（被查询次数）、`query_log`（查询日志）。
- **查询**：智能体调 `query(name)`（**待实现工具**）→ `query_count+1`；追加查询时上下文（往前追溯一条用户提示 + 当前封装完工具调用的智能体回复，共一对）；记录物理时间。
- **优先级** = `query_count`（越多越高）。
- **上限降级**：长期对象数 > `LONG_MAX` → 按优先级从低到高（`query_count` 最小）降级：**删除该长期快照** + **把其中期孪生 count 设为 8**（`LONG_DOWNGRADE_COUNT_RESET=8`；8<10 → 不会下一轮立即重新升级，提供滞回）。`query_count` 随长期对象删除而消失。

### 7.5 状态机

```
                  抽取候选（已过石蕊+Layer2过滤）
                              │
                              ▼
        ┌─────────────── SHORT (count≤2, 有界双端队列≤50, 不注入) ──────────────┐
        │  新候选 count=1 入队(前)                                                │ 溢出: 出口淘汰(丢弃)
        │  再次命中: count+1, 前移10位                                            │
        └───────────────┬───────────────────────────────────────────────────────┘
                        │ count 达到 3（立即, 与位置无关）
                        ▼
        ┌──────── MEDIUM (count≥3, 最大堆 key=age, 主动注入) ────────┐
        │  语义重合(LLM judge): update细节/保持原样 → count+1,        │
        │                       进入时间戳刷新(age归零), re-heapify   │
        │  age > 阈值 ──────────────────────────────► 降级 SHORT(count=2)
        └───────────────┬────────────────────────────────────────────┘
                        │ count > 10（保留中期孪生, count 继续涨）
                        ▼
        ┌──────── LONG (快照+溯源, 索引注入, query 工具按需展开) ────────┐
        │  query(name): query_count+1, 追加上下文一对, 记物理时间         │
        │  对象数 > LONG_MAX: 删最低 query_count 的快照                  │
        │                     + 中期孪生 count 设为 8（滞回）            │
        └────────────────────────────────────────────────────────────────┘
```

### 7.6 字段表

| 级 | 字段 |
|---|---|
| SHORT | `content`, `count`(1–2), `position`(队列内派生) |
| MEDIUM | `content`(精炼句), `count`(≥3), `entry_extraction_index`(进入时间戳), `age`(派生) |
| LONG | `name/id`, `title/summary`(索引用), `snapshot`(K轮+物理时间), `query_count`, `query_log[]`(user_prompt, agent_reply, phys_time), `linked_medium_id` |
| 每用户状态 | `extraction_count`(逻辑时钟) |

### 7.7 常量表（† = 本轮敲定初版，instrument 后微调；见 §12.2）

| 常量 | 值 | 含义 |
|---|---|---|
| `PROFILE_BATCH_K` | 5 | 每 5 轮触发一次抽取 |
| `SHORT_MAX` | 50 | 短期队列容量 |
| `SHORT_PROMOTE_AT_COUNT` | 3 | count 达 3 即升中期 |
| `SHORT_PRIORITY_STEP` | 10 | 提及前移位数 |
| `MEDIUM_LONG_THRESHOLD` | 10 | count>10 触发建长期 |
| `MEDIUM_DOWNGRADE_AGE` | 30 † | age（抽取次数）超此值降回短期；≈150 轮，**最敏感、建议区间 20–50** |
| `MEDIUM_DOWNGRADE_COUNT_RESET` | 2 | 降回短期时 count |
| `MEDIUM_MAX` | 60 † | 中期存储安全网；超限从堆顶（age 最大）强制降级 |
| `MEDIUM_INJECT_TOP_N` | 15 † | 每轮注入条数 = top-12 by count + 3 最近晋升 |
| `MEDIUM_INJECT_TOKEN_CAP` | ≈600 † | 中期注入块 token 上限；超则按 count 升序截断 |
| `JUDGE_MEDIUM_LIST_CAP` | 40 † | 喂给抽取/judge 的中期清单上限（top-40 by count，控成本） |
| `LONG_MAX` | 30 † | 每用户长期对象上限；超限删最低 query_count 快照 + 中期孪生 count=8 |
| `LONG_DOWNGRADE_COUNT_RESET` | 8 | 长→中降级时中期孪生 count |
| `EXTRACT_WINDOW_N` | 8 † | 抽取窗口轮数 = 5（本批）+ 3（前置上下文，解指代） |
| `EXTRACT_USER_MSG_CAP` | 500 † | 每条用户消息截断字符数 |
| `EXTRACT_AGENT_RESP_CAP` | 200 † | 每条 agent 回复截断（仅消歧、非证据，故更短） |
| `EXTRACT_TOTAL_CHAR_CAP` | 6000 † | 抽取输入总字符上限；超则从最旧轮截起 |
| `(低置信)` 阈值 | count<5 † | 中期注入时给 count∈{3,4} 打 `(低置信)` 标签 |

> ✅ **代码核对（2026-09-19）**：除以下两项外，表中常量均已按同值落地——
> - **未实现（P2）**：`LONG_MAX`、`LONG_DOWNGRADE_COUNT_RESET` 在代码中**不存在**（LONG 无上限降级）。
> - **命名/单位差异**：`MEDIUM_INJECT_TOKEN_CAP=600` 实际按**字符**截断（`memory.py:633` 注释 "chars"）；
>   `(低置信)` 阈值的常量名为 `LOW_CONFIDENCE_THRESHOLD=5`（`memory.py:334`）。
> - **代码位置**：`memory.py:324-337`（SHORT_MAX/SHORT_PROMOTE_AT_COUNT/SHORT_PRIORITY_STEP/
>   MEDIUM_LONG_THRESHOLD/MEDIUM_DOWNGRADE_AGE/MEDIUM_DOWNGRADE_COUNT_RESET/MEDIUM_MAX/
>   MEDIUM_INJECT_TOP_N/MEDIUM_INJECT_RECENT_SLOTS/MEDIUM_INJECT_TOKEN_CAP/LOW_CONFIDENCE_THRESHOLD/
>   JUDGE_MEDIUM_LIST_CAP/TIER_SCHEMA_VERSION）；`profile.py:50-51`（PROFILE_BATCH_K/EXTRACT_WINDOW_N）。
> - **规划未列、代码新增**的常量：`MEDIUM_INJECT_RECENT_SLOTS=3`（"最近晋升"名额，§12.1 防 rich-get-richer）、
>   `TIER_SCHEMA_VERSION=1`（tiers JSON 版本戳）。
> - `EXTRACT_USER_MSG_CAP=500` / `EXTRACT_AGENT_RESP_CAP=200` / `EXTRACT_TOTAL_CHAR_CAP=6000` 在
>   `profile.py:47-49` 一带，值与规划一致。

### 7.8 持久化 schema

- 机制②现为 `data/memory/*.md`（frontmatter）。三级属性（`tier`/`count`/`entry_extraction_index`/`query_count`）入 frontmatter 或 `metadata`；快照与查询日志入 md 正文；每用户 `extraction_count` 入小状态文件（如 `data/memory/user/{uid}/_state.json`）。
- 运行期的双端队列 / 最大堆是**派生结构**，启动时按持久化属性重建。
- **迁移**：现有 `data/memory` 内容 + `profile.json` 的旧 `facts` 如何并入新结构 = TODO（§12）。

> ✅ **实况（实际落地的 schema 与上述草案不同）**：
> - 每用户一个 **JSON 文件** `data/memory/tiers/{uid}.json`（`memory.py:395-396` 目录常量、`:673-684` 序列化、
>   `:685` `_atomic_write_json` temp+replace 原子写），内含 `short`/`medium`/`long` 三层条目与
>   `extraction_count` 逻辑时钟、`TIER_SCHEMA_VERSION`。**没有** `_state.json`，三级属性**不入 frontmatter**。
> - LONG 快照单独存 `tiers/long/{uid}/{id}.md`（frontmatter + K 轮正文，`memory.py:754-776`）。
> - 旧 `MemorySystem`（`data/memory/MEMORY.md` 索引 + `user|knowledge|system/` md）原样保留、未触碰。
> - **迁移已实现**：`QQBot/scripts/migrate_memory_p1.py`（`43df292` 修订版含平铺文件处理
>   `_uid_from_interaction`/`loose_interaction_files`、播种与归档解耦），旧 `user/**/interaction_*.md`
>   归档至 `user/_archive/{uid}/`，画像 dormant facts 播种 SHORT，P0 误删项可 `--restore-false-drops`
>   恢复到 MEDIUM。回归测试 `test_migration_archives_loose_legacy_dumps`（`test_agent.py:2282`）。

### 7.9 LLM 调用点与成本

- 每 K 轮：1 次 **flash 抽取**调用。
- 每批：1 次 **flash judge** 调用（候选 vs 中期清单 → 重合判定 + update/keep）。**可考虑与抽取合并为一次调用**（让抽取直接产出"是否命中已有中期 + 更新建议"）以省调用——TBD（§12）。
- `query(name)`：**不额外调 LLM**（仅展开已存快照进智能体上下文）。

> ✅ **实况**：已按 §12.1 决策**合并为一次调用**——`extract_batch`（`profile.py:379-427`）单次
> `chat_completion(purpose="profile")` 同时产出画像槽更新与带 `judge`+`matched_id` 的 `memory_candidates`；
> 无独立 judge 调用。`query(name)` 属 P2 未实现。

---

## 8. 关于 embedding（为何本方案不需要）

- **不需要训练任何词向量。** word2vec 是静态词向量，不适合事实级语义；要语义比对应该用**句向量**（预训练，拿来即用）。
- **DeepSeek 无 embedding 接口**。若将来需要：API（OpenAI `text-embedding-3-small` / DashScope `text-embedding-v3` / SiliconFlow `BGE-M3` / 智谱 `embedding-3`）或本地 `sentence-transformers` + `BAAI/bge-small-zh-v1.5`（需 torch，重）。
- **本方案不需要**：记忆按用户隔离，短期 ≤50、中期集合小，能整段进 prompt →
  - **B** 用 LLM judge 直接判语义重合；
  - **C** 用 agentic RAG（标题索引 + `query` 工具）。
  - 零新依赖、零训练。
- **扩展边界**：若某用户长期记忆涨到"标题清单都塞不进 prompt"，再引入 embedding 向量检索。届时再加。

---

## 9. 死文件处理（对账完成，处置已定 — 证据见 §13）

> 全仓 12 个 md，逐个对账真实代码后定下最终处置：

| 文档 | 处置 |
|---|---|
| SOUL / IDENTITY / AGENTS | 现状即注入 prompt，不动 |
| **TOOLS.md** | ✅ **接入**，但**裁成纯编排策略**（删所有 JSON schema 复述），补 `download_repo`/`summarize_pdf` 策略 |
| **config/MEMORY.md** | ✅ **接入（Decision K）**，但**先按三级引擎真实行为重写**（现内容类型/路径/流程全错）——**已完成**（`9da0f12`：重写为三层引擎说明 + `_load_configs`/`build_system_prompt` 接入） |
| **SESSION.md** | ❌ **弃置**（不接入；**标记但不删除**）。参数大半过时/虚构，模型也改不了 |
| **BOOTSTRAP.md** | ❌ **弃置**（不接入；**标记但不删除**）。纯启动描述且严重失真，属开发文档 |
| HELP.md / FEATURES.md | 🚫 **勿动**（被 plugin 实时读取：/help、feature 卡片） |
| HEARTBEAT.md / USER.md / WORKSPACE.md | ❌ **弃置**（全死、无 .py 引用；**标记但不删除**） |

- **"标记但不删除"** = 在文件顶部加一行弃置注释（如 `<!-- DEPRECATED: 不再加载/注入，保留作历史参考，详见 Profile-Fact-Extraction-Plan.md §13 -->`），文件留在原处。
- ~~当前仅 **SOUL / IDENTITY / AGENTS** 真正进 system prompt~~ → **实况（`9da0f12` 后）**：进 system prompt 的是
  **SOUL / IDENTITY / AGENTS / MEMORY（重写版）** 四个；**P3 完成态** = 上述四个 + 裁剪后的 TOOLS.md（详见 §13.6）。
- **处置进度**：MEMORY.md ✅ 已重写接入（9da0f12）；TOOLS.md 裁剪接入 ❌ 未做；SESSION/BOOTSTRAP/HEARTBEAT/
  USER/WORKSPACE 的弃置标记 ❌ 未做（文件仍原样躺在 `agent/config/`）。

---

## 10. 抽取精度三层（三级引擎的前提，共识 I）

> 这三层产出的候选**喂给 §7 的短期**，而非旧的 `profile.facts`。

- **Layer 1 — 重构抽取 prompt**：石蕊测试为首要判据 + **2–3 个 few-shot 反例**（输入 → 应返回 `{}`）；对话块第三人称 + `<conversation>` 分隔（修掉 `Your response` 误标）；**传最近 N 轮窗口**（解决"该项目"模糊指代）；每条候选**自包含**（指代解析成命名实体，否则丢弃）；**agent_response 仅作消歧上下文，禁当证据**（"assistant 的回复可能含角色扮演称谓与假设，绝不能作为关于用户的证据"）。
- **Layer 2 — 确定性后置过滤**（进短期前用代码拦；**只拦无歧义项，歧义交 Layer 1**）。三原则：① 只黑**无歧义复合词 + 正则模式**，歧义裸词不黑（误杀静默不可见，比漏拦更危险）；② **不按具体名字拉黑**（团长/露比/蝶子 是"agent_response 当证据 / tool-output 派生"的症状，根因在 Layer 1，名字拉黑=打地鼠）；③ **每拦一条记 (候选, 命中项, 类目)** 供 instrument 迭代。初版词表：

  **L2-A 硬丢词表（子串匹配，无歧义复合词）**
  - bot 架构/状态（类目 `bot_state`，`fact_filter.py:50-55`）：`工作区` `特殊会话` `临时会话` `连续对话` `连续模式` `对话窗口` `系统提示词` `权限级别` `工具范围` `可用工具` `代码执行限制` `磁盘用量` `剩余空间` `存储配额`
  - 工具操作产物（类目 `tool_artifact`，`:57-61`）：`文件路径` `截图路径` `行动值` `跑条` `拉条` `推条` `测速` `兑换码` `礼包码` `CDK` `CDKey`
  - gacha（类目 `gacha`，`:63-68`，无歧义复合）：`十连` `单抽` `卡池` `抽卡` `常规招募` `几率up招募` `神秘招募` `银河招募`
    ——✅ 落地版**另加 4 词**：`连抽` `抽取结果` `抽卡结果` `招募结果`
  - bot 自身（类目 `bot_self`，`:70-73`）：`Roxy` `机器人` `智能体`（关于 bot 的 fact 永不属用户）

  **L2-B 硬丢正则**
  - 容量单位（类目 `capacity_unit`，`:88`）：`\d+(?:\.\d+)?\s*(?:MB|GB|KB)`（含 `250/2048 MB` 形态）
  - gacha 结果（类目 `gacha_result`，`:92-94`）：`抽到|未抽到|没抽到|歪了|出了金|\d\s*星` 等（落地版比初稿更精细）
  - 错误标记（类目 `error_marker`，`:96`）：`(?:报错|失败|超时|异常|崩溃|卡住)`
  - ~~瞬时态：`(?:正在|当前正|刚刚|刚才|这次|本次)`~~ → ✅ **P1 已整类删除**（选择了 §12.2 的方向②）：
    `fact_filter.py:97-108` 注释明示 "the former `transient_state` category … was REMOVED ENTIRELY"。
    bot 瞬态改由 L2-A 复合词兜住（`连续对话`/`特殊会话` 等）、`本次会话` 由 L2-C 兜住，
    现实生活持续活动（"正在…降血脂"）不再误伤。

  **L2-C 结构规则（非词表）**
  - 未解析指代（类目 `unresolved_ref`，`fact_filter.py:117`）：含 `(?:该|此|上述|这个|那个|本项目|该项目)` 且无命名实体（`「」`/`《》`/专名）→ 丢
  - 纯数字+单位、无主语语义（类目 `pure_numeric`，`:124`）→ 丢

  **L2-X 刻意不拦（交 Layer 1 语义判断 + few-shot）**
  - 具体角色/称谓名（团长/露比/蝶子…）：根因在 Layer 1 的"agent_response 禁当证据"+"石蕊测试"，非名字问题。
  - 歧义裸词（`会话`/`工具`/`速度`/`角色`/`羁绊`/`截图`/`上传`/`搜索`/`招募` 等）：误杀风险高（"会话分析"研究者、"短跑速度"、"从事招募工作"），不硬丢，靠 Layer 1 石蕊+few-shot 判。
- **Layer 3 — 批量 + flash**：见 §7.1（`observe_turn` 缓冲、K=5、flash 客户端）。✅ 已落地。
- **可观测**：记录"抽取了什么 / 保留 / 丢弃 / 升降级"，面板"记忆画像"页可视化 → 拿真实数据调黑名单与阈值。
  （部分落地：Web 面板已有记忆与画像页可读写 tier/画像数据；逐条 filter 命中日志的 instrument 未做。）
- **存量清理** ✅ 已完成：`QQBot/scripts/cleanup_profile_facts.py`（`--apply --field both --base-dir …`）
  已于 2026-09-14 对生产画像库执行——9 个画像中 6 个受影响、共删 51 条（facts=36/interests=15），
  备份在 `QQBot/data/profile_cleanup_backups/20260914_212159/`，复跑 dry-run=0（幂等）。
  详见 `.claude/deploy-server.md` §P0 部署记录；被误删的 2 条真实事实经 P1 迁移 `--restore-false-drops` 恢复。

---

## 11. 实施范围与分期

| 期 | 状态 | 内容 | 主要文件 |
|---|---|---|---|
| **P0 止血（前提）** | ✅ `361ea54`，2026-09-14 已部署生产 | Layer 1/2/3 抽取精度 + 画像瘦身（`facts` dormant，留 nickname/interests/prefs）+ 存量清理脚本 + 可观测 | `profile.py`, `agent.py`, `agent_router.py`, `fact_filter.py`, `scripts/cleanup_profile_facts.py` |
| **P1 三级引擎** | ✅ `9d93dd2`+`43df292`+`9da0f12`，生产验收见 `P1-Deployment-Acceptance-Plan.md` | 短/中/长数据结构（LONG 仅 create+persist）+ 统一抽取管线（替换 `_maybe_remember`）+ 合并 extract+judge + 状态机 + `tiers/{uid}.json` 持久化 + 迁移脚本 + 测试（4.7/4.8/4.9 套件） | `memory.py`, `agent.py`, `profile.py`, `scripts/migrate_memory_p1.py` |
| **P2 agentic RAG** | ❌ 未动手 | 长期索引注入 + `query(name)` 工具 + `query_count`/`query_log` + 长期上限降级（`LONG_MAX`） | `memory.py`, `agent_router.py`(注册工具), `TOOLS.md`(若接入) |
| **P3 死文件** | 🔶 部分（MEMORY.md 项已由 `9da0f12` 提前完成） | ~~`MEMORY.md` 重写 + 接入 `build_system_prompt`~~ ✅；`TOOLS.md` 裁成纯策略 + 接入 ❌；`SESSION.md`/`BOOTSTRAP.md`/`HEARTBEAT.md`/`USER.md`/`WORKSPACE.md` 弃置标记 ❌（均未做）；HELP/FEATURES 勿动 ✅（本就未动） | `config/MEMORY.md`, `config/TOOLS.md`, `agent.py`(`_load_configs`/`build_system_prompt`) |

**不碰**：主消息流路径。**机制②的 `_maybe_remember` 在 P1 被三级管线替换** ✅（函数体已删，agent.py 仅存 2 处注释提及）。

---

## 12. 待定 TODO（本轮讨论后更新）

### 12.1 已决（2026-09-11 讨论收敛）

- [x] **中期显式上限** → **拆成"注入预算"与"存储上限"两件事**，不引入复合淘汰 key：
  - **注入预算**（解决"全量注入膨胀"）：每轮注入 **按 `count` 取 top-N + token 上限**（sort by count desc → slice）。中期存多大都不影响 prompt 体积。
  - **存储上限**：主要靠 age-降级自然清空；安全网 = 超 `MEDIUM_MAX` 时**从堆顶（age 最大）强制降级到 ≤MAX**（复用 age-降级动作，非新机器）。**不需要 age×count 复合淘汰策略**（那才是真难点）。
  - **防 rich-get-richer**：注入除 top-N by count 外，留 **2–3 个"最近晋升"名额**（by `entry_index` desc），给新晋项曝光。注意强化不只来自注入——用户再次提及时抽取+judge 仍会 `count+1`，与是否被注入无关。
- [x] **抽取调用与 judge 调用合并**（§7.9）→ **倾向合并为一次调用**。强论据来自并发：合并 → 单次 await → apply 必为同步原子块。**回退**：若一次调用同时做"抽取+重合判定"质量退化，再拆回两次。
- [x] **并发处理** → **尽量不显式加锁**，靠 asyncio 协作式原子性：
  - **每用户单飞（single-flight）**：同用户同时只允许一个抽取在飞；新轮次只同步追加 `_pending`；delete-flush 排队等在飞任务结束。
  - **先算完再原子写**：所有 LLM 调用（抽取+judge）在前 await 完，**结构变更放在无 await 的同步块里一次性 apply** → 天然原子，无需锁保护中期堆/profile。
  - **落盘**：`temp + os.replace` 原子写防崩溃损坏；如需串行只在**写盘那一小段**持每用户 `asyncio.Lock`。
  - **铁律**：**绝不在持锁时 await LLM**（会串行化所有用户、卡死）。
- [x] **md 文档处置（对账后最终定，证据见 §13）**：
  - **TOOLS.md → 接入**，但**只放编排策略、删所有 JSON schema 复述**（schema 已由 function-calling `tools` 参数喂模型；prose 重复 = 漂移源，已实证 `play_gacha_animation.interval` 漂移）；补 `download_repo`/`summarize_pdf`。prose 与 schema 冲突时**以 schema 为准**。`query(name)` 文档落进接入后的 TOOLS.md（与 P2 协同）。
  - **config/MEMORY.md → 接入（Decision K）**，先按三级引擎重写。
  - **SESSION.md / BOOTSTRAP.md → 弃置**（不接入，**标记但不删除**）：SESSION 参数大半过时/虚构、模型改不了；BOOTSTRAP 是失真的开发文档。
  - **HEARTBEAT.md / USER.md / WORKSPACE.md → 弃置**（全死，标记不删除）；**HELP.md / FEATURES.md → 勿动**（plugin 实时在用）。
- [x] **re-heapify 实现** → **YAGNI**：中期仅几十条，**先用普通 list，注入/降级时按需排序**；不预上 indexed heap / 懒删除，真涨大了再优化（大概率不会）。
- [x] **迁移脚本时机** → **P0 不迁移**。P0 存量清理**只删** profile.json 中命中黑名单的旧 fact（不搬去任何地方）。facts/memory 并入三级结构留 P1（结构建好后）。
- [x] **`query(name)` 权限** → **所有角色可用**（只读调用者自己的记忆，memory.py 已按 `user_id` 隔离，低风险）+ **rate-limit** + **`query_log` 封顶**防无限增长。

### 12.2 仍待数据/实验（instrument-first，跑真实流量再收敛）

- [x] **阈值取值（初版已敲定，见 §7.7 † 标记）**：`EXTRACT_WINDOW_N=8`、`MEDIUM_DOWNGRADE_AGE=30`（最敏感，区间 20–50）、`LONG_MAX=30`（⚠️ 属 P2，代码中尚不存在）、`MEDIUM_MAX=60`、`MEDIUM_INJECT_TOP_N=15`、`(低置信)` 阈值 `count<5`（代码常量 `LOW_CONFIDENCE_THRESHOLD`）、截断 user 500 / agent 200 / 总 6000。以上除 LONG_MAX 外均已按同值落地（§7.7 代码核对）。**仍需 instrument**：全量记录升降级，跑约一周真实流量后微调（尤其 `MEDIUM_DOWNGRADE_AGE`）。
- [x] **Layer 2 黑名单初版词条（已敲定，见 §10 L2-A/B/C/X）**：只拦无歧义复合词 + 正则；歧义裸词与具体名字**刻意不拦**（交 Layer 1）。**仍需 instrument**：每拦一条记 (候选, 命中项, 类目)，靠可观测迭代收紧/纠错。
  - **已知边缘项 — `正在` 误伤现实生活持续活动（2026-09-14 生产清理实证）**：L2B `transient_state` 的裸词 `正在` 不只会拦 bot 瞬态（"正在使用连续对话模式"），也会**误删通过石蕊测试的真实用户事实**——如"用户正在通过饮食调整降血脂"、"用户正在做力量训练，训练思路是按酸痛程度自由分割训练循环…"（用户 1114144652；落在 **dormant 的 `facts` 字段**、已备份、无线上影响，但 P1 迁移前须正视）。根因：`正在` 既表 bot 瞬态又表现实生活持续态，裸词无法区分。**P1 收紧方向（二选一）**：① `正在` 改为须与 bot-state 词（会话/模式/工作区/对话/连续）**共现**才拦；② 干脆从 L2B 删 `正在`，把"现实持续 vs bot 瞬态"的判断交给 Layer 1 石蕊+few-shot。其余瞬时词（当前正/刚刚/刚才/这次/本次）均为 bot-session 作用域，未见此误伤。
    - ✅ **已解决（P1，选了方向②并更进一步）**：`transient_state` **整类删除**（不只删 `正在`；
      `fact_filter.py:97-108` 注释记录了删除原因与替代兜底）。误删的 2 条真实事实经
      `migrate_memory_p1.py --restore-false-drops` 恢复到该用户 MEDIUM（count=3，带 `(低置信)` 注入）。
      回归验证：`test_agent.py` 4.6 套件含 "降血脂 keep=True / 连续对话 keep=False" 单测点。
- [x] ~~**持久化 schema 细节** + P1 迁移脚本实现~~ → **已实现**（与 §7.8 草案不同）：每用户单 JSON
  `data/memory/tiers/{uid}.json`（short/medium/long + `extraction_count` + `TIER_SCHEMA_VERSION`，
  temp+`os.replace` 原子写），LONG 快照存 `tiers/long/{uid}/{id}.md`；迁移脚本
  `QQBot/scripts/migrate_memory_p1.py`（含 `43df292` 平铺文件修复），详见 §7.8 实况注记。

---

## 13. md 文档对账结果（2026-09-11，对照真实代码核实）

### 13.1 加载/注入现状总账（12 个 md，只 3 个进 prompt）

> ⚠️ 本表为 **2026-09-11 对账快照**；`9da0f12` 后 MEMORY.md 状态已变，表内已按实况修订。

| 状态 | 文件 | 证据 |
|---|---|---|
| 注入 prompt（活） | SOUL / IDENTITY / AGENTS | `build_system_prompt` agent.py:145-154 |
| 注入 prompt（活）**← `9da0f12` 新接入** | **config/MEMORY.md**（重写版） | `_load_configs` agent.py:127 加载；`build_system_prompt` agent.py:156-158 注入；内容为三层引擎说明（33 行） |
| 加载但不注入（半死） | TOOLS / BOOTSTRAP / SESSION | `_load_configs` agent.py:121-133 读进 `_configs`，build_system_prompt 从不用；agent.py 注释自承 TOOLS.md 为 "documentation reference" |
| plugin 实时读取（活，勿动） | HELP.md / FEATURES.md | agent_router.py:101（`_HELP_MD_PATH`）/:2880-2891（/帮助 发 HELP.md + 渲染卡片）、:2506-2513（/功能 渲染 FEATURES.md 卡片）、card_renderer.py 解析 |
| 完全无引用（全死） | HEARTBEAT.md / USER.md / WORKSPACE.md | grep 全仓 .py 无读取；WORKSPACE.md 仅 builtin_tools.py 一句注释指向，硬件探测已 replace WORKSPACE.md §4 |

注：`config/MEMORY.md`（本次已重写接入）≠ `data/memory/MEMORY.md`（memory.py:45 实时维护的 MemorySystem 索引，活）。

### 13.2 TOOLS.md 漂移（行号已按当前 dev `c68ca8e` 修订）
真实来源 = `_build_tool_registry`(agent_router.py:528-897，25 个) + 函数体外后注册(`get_user_info`:1435 / `end_continuous_mode`:1463 / `begin_task`:1534 / `finalize_subtask`:1550) = **29 工具**；权限集(permissions.py:48-85) = **28**（`end_continuous_mode` 不在任何权限集，仅连续对话路径动态 union 注入，agent_router.py:1460-1462 注释）。
- **缺 2 个已注册且有权限的工具**：`download_repo`(:588, VIP)、`summarize_pdf`(:597, PUBLIC)。
- **schema 实证漂移**：`play_gacha_animation` 注册 schema 有 `interval`，TOOLS.md 的 JSON 漏了它。
- `end_continuous_mode`(:1463) 未文档化（特殊工具）。
- 其余 26 个工具名 + 权限归属对得上（`begin_task`/`finalize_subtask` 在 `_PUBLIC_TOOLS`，permissions.py:70-71）。
- 权限分层实况：`_PUBLIC_TOOLS` 22 个 + `_VIP_TOOLS` 4 个（web_fetch/download_repo/get_system_load/execute_code）+ `_ADMIN_TOOLS` 1 个（shell_exec）。

### 13.3 SESSION.md 漂移
真实参数：`max_context_messages=20`(session.py:85)、`session_timeout=1800`、`thinking_timeout=180`(agent.py:91)、`max_tool_iterations=20`(agent_router.py:974 / 循环 agent.py:228)。
- `max_context_turns=10` → 代码无此常量，**虚构**。
- `max_tool_calls_per_turn=20` → 值对**名字错**（真实 `max_tool_iterations`）。
- `tool_timeout=60` → **无此全局常量**（execute_code 默认30 / shell_exec 默认15 / `CodeLimits.max_timeout=60` 仅 admin）。
- `reminder_interval=15` → **错**；真实"思考中"提示在 deepseek_chat.py:25 `interval=10`、文案"仍在思考中"，属另一插件、未必在主路径生效。
- 裁剪策略 §2 "preserve system prompt (SOUL+IDENTITY+**TOOLS**+AGENTS)" → 错（实际无 TOOLS）。
- 生命周期 "Load **USER.md** template" → USER.md 无 .py 引用，**虚构**。
- 持久化 "工作区 **500MB** 配额" → 不准（按角色 admin 2GB / vip 500MB / regular 100MB，permissions.py:116-120）。
- 临时会话 vs 特殊会话独立性 ✓ 准确（保留）。

### 13.4 BOOTSTRAP.md 漂移
真实 `bootstrap()`(agent.py:702-749)：只做 ①硬件探测 ②DeepSeek ping；docstring 自称 "defined in BOOTSTRAP.md" 但**不读 `_configs["bootstrap"]`**、不按其序列走。
- 初始化顺序声称加载 WORKSPACE.md(5)/MEMORY.md(7) → `_load_configs` 不加载这两个；声称 TOOLS/SESSION 进 prompt → 不注入。
- 健康检查声称 Napcat WebSocket(10)/HEARTBEAT 周期检查(11)/磁盘 100MB → **bootstrap() 一个都没有**；DeepSeek 仅 ping 一次，无"3× 重试/5s"。
- Fallback "Roxy 正在维护中" + 60s 重试循环 → bootstrap() 无此逻辑。
- 工具表缺 7 个新工具（delete_workspace_file/character_detail/bond_detail/parse_battle_screenshots/redeem_code/begin_task/finalize_subtask）+ end_continuous_mode；Required/Optional 分类与代码不符（注册无条件，无 try/except 分级）。

### 13.5 config/MEMORY.md 漂移
> ✅ **已失效（历史档案）**：`9da0f12` 把 config/MEMORY.md 整体重写为三层引擎说明（旧版 Memory Index
> 风格内容已不存在），下列漂移项随之全部消解。保留原文供考古。

真实 = memory.py。
- 类型表列 **4 类**(user/conversation/knowledge/system) → memory.py 只实现 **3 类**(user/knowledge/system，见 `_get_storage_dir`/`_get_search_dirs`/section markers)；**conversation 类不存在**。
- 路径错：写 `memory/users/{uid}/`(复数) → 实际 `{base_dir}/user/{uid}/`(单数, memory.py:68)；`memory/conversations/{date}/` 不存在。
- Recall/Forget 工作流失真：描述"Agent 检查此索引找记忆" → 实际 agent 不读 config/MEMORY.md，走 `recall(name)`/`search(子串)`，索引由 `_update_index` 自动维护、非 agent 入口。
- 措辞硬伤：`temperate`→temporary、`Adminicle`→Note、`Deletion in memory files`。

### 13.6 接入后 system prompt 组成（P3 完成态）
`SOUL + IDENTITY + AGENTS` + **重写后的 MEMORY.md**（机制②记忆规则）+ **裁剪后的 TOOLS.md**（纯编排策略）。SESSION/BOOTSTRAP/HEARTBEAT/USER/WORKSPACE 不进 prompt（弃置标记保留）；HELP/FEATURES 仍由 plugin 各自读取，与 system prompt 无关。

> ✅ **当前实况**：`SOUL + IDENTITY + AGENTS + MEMORY`（四个，`9da0f12` 后）；TOOLS.md 裁剪接入未做。
> 另有两个规划时未列的动态注入块：MEDIUM 记忆注入（`agent.py:516-519`，`## 用户记忆（中期）`）
> 与人格切换过渡标记（`_personality_switch_marker`，`agent.py:457-459`）。
> 完整注入顺序见 `agent.py` `build_system_prompt`（:137-169）+ `_build_messages`（:423-533）：
> system prompt 正文 → 人格 override → 会话类型标记 → 工作区上下文 → 画像类型化槽 → 权限上下文 →
> 群聊上下文 → MEDIUM 注入 → 历史 → 当前消息。

---

## 附：关键代码位置速查（✅ 已按 P1 后代码更新；~~删除线~~ = 旧行号仅作历史参考）

**新管线（P0/P1 落地后）**：
- `profile.py:50-51` `PROFILE_BATCH_K=5` / `EXTRACT_WINDOW_N=8` · `:100-124` `to_prompt_context`（只注入类型化槽） · `:299-328` `observe_turn`（缓冲+单飞+满 K 触发） · `:330` `flush_on_session_end`（尾批 flush） · `:379-427` `extract_batch`（合并 extract+judge 单次 flash 调用） · `:442` `_apply_extraction` · `:572` 起 `_build_extraction_prompt`（8 轮窗口+judge 清单）
- dormant 遗留：`profile.py:43` `MAX_FACTS=40` · `:65` `facts` 字段 · `:133-147` `merge_facts` · `:162-169` `_similar`（均保留、零调用方）
- `memory.py:324-337` 三级常量区 · `:385` `class TieredMemory` · `:395-397` `tiers/`、`tiers/long/` 目录 · `:447` `get_judge_list` · `:542-545` SHORT 入队/淘汰 · `:582-600` `_maybe_create_long`（P1 仅 create+persist） · `:614` MEDIUM 按需排序 · `:626-661` `build_medium_injection`（`(低置信)` 标签） · `:673-685` JSON 序列化+原子写
- `agent.py:414/:667` `_schedule_profile_update` → `observe_turn` · `:516-519` MEDIUM 注入（`_build_messages`） · `:127/:156-158` MEMORY.md 加载与注入 · `:733` `has_tiered_memory` 状态汇报
- `agent_router.py:924` `set_client(flash_client)` · `:931` `TieredMemory(base_dir=data/memory)` · `:1644/:2032` 会话删除挂钩 flush
- `fact_filter.py:50-124` L2-A/B/C 词表与正则（`:97-108` transient_state 删除记录） · `scripts/cleanup_profile_facts.py`（P0 存量清理） · `scripts/migrate_memory_p1.py:121/:134/:147/:197`（`_uid_from_interaction`/`loose_interaction_files`/`discover_users`/`interaction_files`）
- 测试：`test_agent.py` 4.6 `TestProfileExtraction`(:1037) · 4.7 `TestTieredMemory`(:1702) · 4.8 `TestMergedExtraction`(:2095) · 4.9 `TestMemoryMigration`(:2209，含 `test_migration_archives_loose_legacy_dumps`:2282)

**旧机制（历史参考，行号为 P0 前）**：
- ~~`profile.py:204-236` 硬编码抽取 prompt · `:71-95` 注入 `facts[-10:]`~~（已被上述新管线取代）
- ~~`agent.py:649-683` `_maybe_remember`~~（函数体已删；仅 `:26`、`:413` 两处注释提及）
- `agent_router.py` 会话参数（硬编码） · `_build_tool_registry`（工具说明真实来源，:528-897 + 后注册 :1435/:1463/:1534/:1550）
- `memory.py` 旧 MemorySystem 仍在：`search()`（子串匹配）/ `save()`（frontmatter md）/ `MEMORY.md` 索引维护（`data/memory/MEMORY.md`）
- `deepseek_client.py:27-35` 显式 api_key+api_base early-return（WebUI Playground 用） · `model_router.py` `flash_client`

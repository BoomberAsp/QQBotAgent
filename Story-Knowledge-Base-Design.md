# Story Knowledge Base — 原作剧情知识库（露比记忆层）设计与实施

> 状态：已实施并验证（数据已构建、检索与注入链路已跑通）
> 日期：2026-10-24
> 关联文档：`Bond-System-Design.md`（双平面架构、L4 易变层约束）、`Cache-Hit-Rate-Plan.md`（提示词分层与缓存命中）、`QQBot/agent/config/TOOLS.md`
> 代码：`QQBot/lib/story_kb.py`（运行时检索）、`QQBot/scripts/build_story_kb.py`（离线构建）、`QQBot/test_story_kb.py`（测试）

---

## 1. 背景与目标

角色的「人设」由 `agent/config/personalities/*.md` 提供，但**人设 ≠ 记忆**：露比的人格卡只有几百字的世界观摘要，遇到「第一次方舟战争发生了什么」「你昏迷那十年都在做什么」「挪亚的复制体计划是什么」这类问题时，模型只能凭常识编造或答不出来。

本项目的目标是给角色补上**原作剧情的可检索记忆**：

1. 从 `arkrecodewiki.miraheze.org` 的 `Main_Story` 与 `Events/Event_Story` 索引页出发，采集**主线 + 全部活动剧情**的逐句对白；
2. 建成**中文可检索**的本地索引（中文提问可直接命中英文原文）；
3. 在露比等角色人格的**每一轮对话前自动检索并注入**（角色不调用工具）；
4. 同时给任务面（Roxy）一个 `story_search` 工具，供深挖与事实核对；
5. 全链路离线可重建、可断点续跑，不引入任何新运行时依赖。

**非目标**：不做全文翻译（成本高且无必要——模型转述即可）、不引入向量库、不影响提示词缓存命中。

---

## 2. 数据源勘察

### 2.1 索引页与真实内容页

两个索引页本身**不含剧情**，它们只做表格 + 模板转写（`{{:Main Story/Episode 1}}`、`{{:Events/Event Story/Erolabs}}`）。剧情在它们链接的子页里：

| 前缀 | 页面总数 | 有剧情的候选 | 实际入库 |
|---|---|---|---|
| `Main Story/*` | 1 000+（含每关战斗数据页） | ~180 | 88 单元 |
| `Event Story/*` | 500+（含每个活动的 A.1~A.13 战斗页） | ~85 | 65 单元（去重后） |
| `Events/*` | 19（索引/商店等） | — | — |

关键结论：`Main Story/Episode 1/1-1` 这类页面只有 `{{Stage}}` 战斗数据（怪物、掉落、波次），**没有一句对白**；`Event Story/<活动>/A.3` 同理。真正承载剧情的是：

* 主线：`Main Story/Episode N/Chapter M/Stage X-Y`、`Main Story/Prologue/Chapter 0`
* 活动：`Event Story/<活动名>`（活动聚合页，全部场景都在这一页）
* `Main Story/Episode 1 (Hard)/Chapter N` 虽然有页面，但**没有对白**（纯关卡数据），因此入库为 0 单元。

### 2.2 两套对白模板

剧情是模板化的结构化对白，不是自由文本：

```text
主线段（MainStory 模板的 Story 参数）
{{Chat|Stage 1-1: Intro||
{{SceneChatAudio|<big>Chapter 1: Resonant Awakening</big>|title||}}
{{SceneChatAudio|*Gasp*... Apathy! What're you doing here?|Rubi|H076|H076_Story_15}}
{{SceneChatAudio|Apathy's cheeks blush ever so slightly.|no||}}
{{SceneChatAudio|Wait! Wh-Where are we going?|player||}}
}}

活动段（EventStory 模板的 Story 参数）
{{Chat|Scene 1: Part 1||
{{SceneChat|Mercenary Union Commander's Room|scene}}
{{SceneChat|Reporting in, Commander.|Saku & Mizuki||Sound/Dialog/Effect_Footsteps_04_HighHeel}}
{{SceneChat|Wahhh—!|Saku|H613|Sound/Dialog/Effect_Hit_03}}
}}
```

行模板参数是**按位置**的：`台词 | 说话人 | 角色立绘 ID | 语音/音效`。第 2 参数除了角色名，还有四种标记：

| 标记 | 含义 | 处理方式 |
|---|---|---|
| `player` | 玩家角色（团长） | 说话人显示为「团长」 |
| `no` | 旁白 | 渲染为「（…）」 |
| `title` | 章节/分段标题 | 渲染为「— … —」，并用于抽取章节名 |
| `scene` | 场景地点字幕 | 渲染为「[场景] …」 |
| 空 | 同上（多为分段标题） | 按 `Part N` / `Chapter N` 正则判定标题，否则当旁白 |

第 3 参数是立绘 ID（`H076`），可直接映射到角色，用于「本角色是否在场」判断。

### 2.3 章节名与活动名

章节标题（`Resonant Awakening`）只出现在**每章第一页**的 `<big>Chapter 1: …</big>` 行里，直接从页面内抽取会有两个问题：同一章其他页面抽不到；部分页面会抽到 `Chapter I: End` 这类分段标题。

正确来源是索引表格（`Main Story/Episode N`、`Events/Event Story/Erolabs|Nutaku`）：表格第三列就是章节名。活动的中文名也在同一张表里（`管理員小姐今天也不敢說話<br>(Miss Administrator…)`），但**只有极少数活动有中文名**，其余靠构建期 LLM 生成。

### 2.4 抓取可行性（Cloudflare）

Miraheze 走 Cloudflare 指纹校验，且生产服务器 IP 被地区级封锁：

* 生产机：`curl_cffi` + `impersonate="chrome116"` + `WIKI_PROXY`（宿主机 sing-box 1081）可用 —— 与 `tools/wiki_scraper.py` 完全一致；
* 本机（开发机）：`curl_cffi` / `httpx` 一律 403 挑战页，**只有系统 `curl` 能拿到 200**。

因此采集器实现为**多后端自动降级**：`curl_cffi+proxy → curl_cffi → curl+proxy → curl`，谁先成功就把谁记下来，后续请求优先复用。策略见 `api_get()`。

---

## 3. 总体架构

```text
              ┌──────────────── 离线构建（scripts/build_story_kb.py）────────────────┐
 allpages ──► │ crawl  MediaWiki revisions API（50 页/请求）                        │
              │          ├─ 标题形状过滤：只留真有对白的页面                        │
              │          ├─ 索引页 → 章节名 / 活动中文名                            │
              │          └─ data/story_kb/raw/pages/<sha>.txt + manifest.json       │
              │ parse  模板解析 → Unit / Scene / Line（说话人、旁白、标题结构化）     │
              │          └─ 内容哈希去重（Hard、Rerun、跨页重复）                    │
              │ enrich 每页一次 LLM 调用 → 单元梗概 + 逐场景中文 gist + 关键词       │
              │          └─ data/story_kb/zh/units.json（可断点续跑）                │
              │ index  写 SQLite + 两个 FTS5 索引 → data/story_kb/story.db           │
              └────────────────────────────────────────────────────────────────────┘
                                          │
              ┌──────────────── 在线（lib/story_kb.py）────────────────────────────┐
  用户消息 ──► │ search  中文 bigram FTS + 英文 BM25 FTS → RRF 融合 → 加成/重排     │
              │ gate    实体命中？否则要求极强词法匹配                               │
              │ render  「## 剧情记忆（原作剧情检索命中）」L4 易变块                  │
              └────────────────────────────────────────────────────────────────────┘
                                          │
        agent/turn.py 每轮写入 contextvar → agent.py `_build_messages()` 追加到 L4 尾
```

**三个产物分离**是刻意的：`raw/`（wiki 快照）、`zh/`（昂贵的 LLM 产物）、`story.db`（纯派生物）。改 chunk 参数只需 `--index`（秒级），换模型只需 `--enrich`，都不会重抓或重付翻译费。

---

## 4. 数据模型

```sql
-- schema_version 3
unit    (id, page_title, work, episode, chapter_no, chapter_title_en, chapter_title_zh,
         event_name, event_name_zh, member, url, scene_count, zh_summary, zh_keywords, zh_ver)
scene   (id, unit_id, ord, scene_title, scene_title_zh, n_lines, char_ids, speakers,
         zh_gist, zh_kw, cast_line, content_hash)   -- cast_line: "Trist=翠丝特、…"
chunk   (id, scene_id, ord, text_en, n_lines)          -- 长场景切块（22 行/块，重叠 4 行）
chunk_fts     fts5(title, text_en, chars, content='', tokenize="unicode61")
chunk_zh_fts  fts5(zh, content='', tokenize="unicode61")
```

设计要点：

* **一个 `{{Chat}}` 块 = 一个 scene**。活动页每场景 15–25 行、主线页 12 个场景左右，正好是「一幕」的粒度，适合整段注入。
* **长场景切块**（>28 行）而不是直接截断，保证单块平均 19 行 ≈ 1459 字符，注入预算可控。
* **`chunk_fts` / `chunk_zh_fts` 用 `content=''`（contentless）**，`rowid == chunk.id` 手动对齐，索引体积小、只做打分，正文统一从 `chunk` 表读。
* **中文用 bigram 而非 trigram**：FTS5 的 `trigram` 分词器要求查询 ≥3 字符，「露比」「方舟」这类两字实体查不到；因此建库时把中文文本按**重叠二元组**切好再入索引，查询侧同样切分，两字词即可命中。`unicode61` 负责切分这些 bigram。
* `scene.zh_kw`（逐场景中文关键词）参与排序与注入闸门；`unit.zh_keywords` 是页面级关键词。

---

## 5. 检索设计

### 5.1 双语双索引 + RRF

```text
中文查询 → zh_query_bigrams（去停用二元组，AND 太严 / OR 太松 → OR + 重排）
        → chunk_zh_fts MATCH → bm25 排序 top-60
英文查询 → en_query_terms（去停用词）
        → chunk_fts MATCH（title 权重 3 / 正文 1 / 人名 2）→ bm25 top-60
两路 → Reciprocal Rank Fusion（k=60）→ 候选 24 条 → Python 侧加成重排
```

RRF 的分数区间很窄（每条列表第一名 ≈ 0.0164，双榜第一 ≈ 0.0328），所以**加成项必须是同一量级**，否则会把排序彻底带偏。

### 5.2 加成项（`StoryKB._bonus`）

| 信号 | 权重 | 理由 |
|---|---|---|
| 本角色在场景中 | +0.0035 | 「她当时在场」的强先验，只是平手时打破僵局 |
| 主线 / 序章 | +0.0025 | 主线是设定主干，活动多为支线 |
| 紧急任务 | −0.003 | 数据量少、内容单薄 |
| 有中文 gist | +0.001 | 可注入性更好 |
| 与场景关键词重合 | ≤0.014 | LLM 为这一幕挑的词，命中即高度相关 |
| 与场景 gist 重合 | ≤0.012 | 主题级命中 |
| **与章节/活动标题重合** | **≤0.018** | 「第一次方舟战争」必须命中第 5 章 —— 实测该项把命中率从 1/3 提升到 3/3 |

### 5.3 自动注入闸门（关键）

自动注入**每轮都跑**，所以「不该注入时不注入」和「该注入时注入」一样重要。朴素阈值（分数 ≥ x）行不通：闲聊「今天天气怎么样」会因「今天 / 天气」两个 bigram 命中某条 gist 而拿到 0.024–0.027 分，和真正的弱匹配分数重叠。

因此改成**实体闸门 + 强匹配兜底**：

```text
注入  ⇔  命中语料实体（角色/活动/章节/关键词）  OR  融合分数 ≥ 0.033
```

实体表（约 3 000 条）来自 `unit.zh_keywords`、`chapter_title_zh`、`event_name_zh`、`scene.zh_kw`，并做两层清理：

1. 场景级关键词跨 >3 个场景出现 → 视为泛化词，不当作实体；
2. 日常名词/动词黑名单（`电影 / 时间 / 推荐 / 解释 …` 约 110 词）—— 这些词世界设定里也会出现，但不构成「在问剧情」。

实测（12 条正样本 / 12 条负样本）：**正样本 12/12 注入，负样本 0/12 误注入**。

### 5.4 译名一致（以官方中文包为准）

语料由 LLM 读英文对白生成，人名最初是模型自己音译的（阿帕西/玛莎/崔斯特…），与官方中文包的
**埃帕蒂/玛夏/翠丝特**…不一致。现在改为**让 LLM 直接按官方对照表书写**（增强提示 v4），机械替换只作兜底：

1. **增强提示里给三张表**：本页出场角色（English=官方名）、全库高频前 40、世界观固定译名（方舟/使徒/星源/真理教团…）。
   每个场景标题下再标注「本场出场」，让姐妹二人能按上下文区分。
2. **兜底一：变体表**（`config/characters/story_name_variants.json`，77 条）修正模型仍写错的少数情况。
3. **兜底二：`cast_exclusive` 规则**：名字只差一字的同貌角色（Lucia=璐茜娅 / Lucima=璐茜玛 姐妹），
   当场景里只出现其中一人且文本**没有同时提到两人**时，按 `char_ids` 纠正；若两人都被提到则不动（那可能是合法提及）。
4. **兜底三：自我重复检查**：「璐茜玛与璐茜玛外貌相似」这类比较句被写成同名时，自动改写后半。
5. **人物对照行**：每场景存 `cast_line`，注入时输出「本段人物：Trist=翠丝特、Marsha=玛夏」，并加硬规则「人名一律按此译」。

实测：民间译名残留 **0**；姐妹误判 **0**；官方译名分布 埃帕蒂 75 / 菈法瑞 65 / 璐茜娅 74 / 璐茜玛 43 / 玛夏 145 / 翠丝特 153。

> 注意 `members_info.json` 的 `name` 是官方中文包译名（从官方包解出），`e7_name` 才是 E7 对应名 ——
> 两者不能混用。世界观词（方舟/使徒/星源）不在人物表里，必须单列，否则模型会把 Ark 音译成「阿卡」。

---


## 6. 提示词接入

### 6.1 为什么放在 L4 易变尾

`Cache-Hit-Rate-Plan.md` 的核心约束：每轮变化的文本必须放在历史之后，否则 provider 的 prefix cache 全废。剧情检索结果逐轮不同，因此：

* `agent/turn.py` 在 `agent.run()` 之前算好块，写进 `_current_story_context` contextvar；
* `agent/agent.py::_build_messages()` 把它追加到 **L4 volatile tail**（`## 剧情记忆…`），排在时间戳之后、当前用户消息之前；
* 缓存前缀（L0–L3）字节不变。

### 6.2 双平面分工

| 平面 | 谁在说话 | 剧情记忆怎么来 |
|---|---|---|
| 角色面（split + social，或 unified 角色人格） | 角色本人 | **自动检索注入**，第一人称口径（带「你（本角色）在场」标记） |
| 任务面（split + task → Roxy） | 中性助手 | 不注入；需要时调用 `story_search` 工具 |

**人格白名单**：剧情记忆是**逐人格开关**，不是全局能力。`config/story_personas.json` 列出允许使用的人格（默认 `["rubi", "assistant"]`），改完按 mtime 即时生效。白名单外的人格（用户自建角色、`roxy_character` 等）三处同时收紧：

1. 不注入 `## 剧情记忆` 块；
2. `allowed_tools` 里剥掉 `story_search`（schema 层看不见，执行层也会硬拒）；
3. `delegate_task` 委派出的任务面同样剥掉 —— 避免「自建角色靠委派绕过限制」。

`assistant` 得到的是**中性口径**：注入块写明「你是助手而不是剧中角色，用第三人称/说书口吻」，且不带「你在场」标记；`rubi` 等有角色映射的人格走第一人称口径。

这样任务面的回答不会「借角色的第一人称」，而角色面永远不用自己调工具（符合 `rubi.md` 里「露比不直接操作工具」的设定）。创建访谈（`char_creation`）与羁绊场景（`bond_scene`）会跳过注入，避免与剧本冲突。

### 6.3 注入预算与文体

默认 2 段 / ≤1800 字符（约 700–900 token）。块头写明四条使用规则：自然提及不朗读、英文台词需中文转述、在场段落用第一人称、把握不大不要编造。

---

## 7. 中文增强（构建期 LLM）

每页一次调用（`deepseek-chat`，`response_format=json_object`），要求返回：

```json
{"unit_summary": "120-200 字剧情梗概",
 "unit_keywords": ["8-15 个中文关键词"],
 "chapter_title_zh": "章节/活动中文名",
 "scenes": [{"i": 1, "title_zh": "…", "gist": "30-60 字", "keywords": ["3-6 个"]}]}
```

* **场景数强校验**：返回的 `scenes` 数量必须等于解析出的场景数，否则整页丢弃（宁缺不错位）。
* **人物译名**：提示词给定露比（Rubi）/团长/伊娥丝/挪亚等既定译名，模型其余按游戏中文习惯译。
* **断点续跑**：每 5 页落盘一次 `zh/units.json`；`ZH_PROMPT_VERSION` 变更才重跑。
* **同章聚合**：一章跨多页，各页 LLM 给的章名可能不同 → 建库时按出现次数投票取众数，保证同章标签一致。
* 实测 154 页 / 并发 4 / 约 6 分钟 / 0 失败 / 覆盖率 100%。

---

## 8. 实施结果

### 8.1 数据规模

| 指标 | 数值 |
|---|---|
| 剧情页入库 | 169 页 → 去重后 **154 单元** |
| 主线 | 88 单元（序章 1 + Part I 33 + Part II 39 + Part III 16），**25 章** |
| 活动剧情 | **65 个活动**，948 个场景 |
| 场景 / 台词行 | **1 906** 场景 / **52 217** 行对白 |
| 检索块 | **2 956** 块（平均 19.1 行 ≈ 1 459 字符） |
| 中文覆盖 | 单元梗概 154/154、场景 gist 1 906/1 906、章节名 100% |
| 数据库体积 | **10.71 MB** |

### 8.2 检索效果（真实数据抽查）

| 提问 | Top-1 命中 |
|---|---|
| 第一次方舟战争发生了什么 | 主线 Part I 第5章《第一次方舟战争》Stage 5-5 |
| 团长昏迷那十年你都在做什么 | 主线 Part III 第3章 S3：十年前露比守在床边 |
| 咲踩到鞋带摔倒的那次 | 活动《A Budding Heart》Scene 1: Part 2 |
| 荣耀圣诞派对里你们做了什么 | 活动《荣耀圣诞派对》Scene 1: Part 2 |
| 挪亚搞的复制体计划到底是什么 | 主线 Part II 第6章《诺亚的目的》Stage 6-S4 |
| 三大方舟的领袖都有谁 | 主线 Part III 第1章《方舟之影》 |

### 8.3 回归验证

* `test_story_kb.py`：30 项全过（分词、建库、双语检索、加成、闸门、渲染、schema 校验、生产库 smoke）。
* `test_agent.py`：改动前后 **85 failed / 156 passed 完全一致**（该 85 项为本仓库既有的环境相关失败，与本功能无关，A/B 已比对）。
* 端到端：真实 `Agent._build_messages()` 调用确认剧情块落在 L4 尾、位于历史之后、当前用户消息之前。
* 工具面：`story_search` 已注册（40 个工具之一），regualr/vip/admin 三种角色均可调用。

---

## 9. 部署位置（数据盘）

知识库是**构建产物**：不进 git，也不该待在系统盘上。位置按以下顺序解析（`lib/story_kb.py`）：

| 优先级 | 来源 | 说明 |
|---|---|---|
| 1 | `STORY_KB_DIR` | 显式覆盖，构建与读取都听它的 |
| 2 | `$USER_DATA_ROOT/story_kb` | 部署用的数据盘（该目录存在即生效） |
| 3 | `<QQBot>/data/story_kb` | 纯开发检出（`USER_DATA_ROOT` 未设或指向不存在的目录时） |

解析是**确定性的**：配置了数据盘就只用数据盘，不会因为仓库里留了一份旧拷贝而「悄悄读旧库」。数据盘上还没有库时 `available=False`，角色不会凭空记忆，`story_search` 也会明确回复「知识库尚未构建」。

> `lib/story_kb.py` 首次解析路径时会自己读 `QQBot/.env`（不覆盖已有环境变量）。这样无论入口是机器人、构建脚本、WebUI 还是测试，`USER_DATA_ROOT` 都一致生效 —— 不会出现「脚本以为数据盘、机器人以为仓库目录」的分裂。
>
> `available` 的否定结果会在库文件出现后自动重新探测，因此「先发代码、再放数据」的部署顺序不需要重启进程。

### 9.1 当前部署实况（106.52.32.91）

| 项 | 值 |
|---|---|
| 数据盘 | `/dev/vdb` 30 G，挂载于 `/home/ubuntu/datadisk`（28 G 可用） |
| `USER_DATA_ROOT` | `/home/ubuntu/datadisk/QQBotData`（可写，278 M 用户数据） |
| 知识库实际位置 | `/home/ubuntu/datadisk/QQBotData/story_kb/` —— 19 M（story.db 11 M + raw 7.2 M + zh 660 K） |
| 仓库目录 | `/home/ubuntu/QQBotAgent/QQBot/data/story_kb` **不存在**（正确：没有落在系统盘） |
| 系统盘余量 | `/dev/vda2` 40 G，已用 79%，剩 8.1 G |

### 9.2 迁移 / 跨机部署

```bash
cd ~/QQBotAgent/QQBot

# 把仓库里的构建产物搬到解析出的数据目录（幂等：已存在的文件不覆盖）
python scripts/build_story_kb.py --migrate

# 只重建索引（改 chunk 参数、换机器之后）
python scripts/build_story_kb.py --index --stats
```

跨机配方（开发机构建 → 上传 → 服务器重建索引，服务器无需访问 wiki）：

```bash
# 1) 开发机：打包产物（≈6 MB gz）
tar -czf /tmp/story_kb_bundle.tar.gz -C QQBot/data story_kb

# 2) 上传并解压到数据盘
scp /tmp/story_kb_bundle.tar.gz ubuntu@<server>:/tmp/
ssh ubuntu@<server> 'tar -xzf /tmp/story_kb_bundle.tar.gz -C /home/ubuntu/datadisk/QQBotData'

# 3) 服务器：用本机 venv 重建索引并验证
cd ~/QQBotAgent/QQBot && ~/.virtualenvs/QQBotAgent/bin/python \
    scripts/build_story_kb.py --index --stats
```

---

## 9.3 运维手册


```bash
cd ~/QQBotAgent/QQBot

# 全量重建（约 10 分钟：采集 3 分钟 + 增强 6 分钟 + 建库 20 秒）
~/.virtualenvs/QQBotAgent/bin/python scripts/build_story_kb.py --all

# 分阶段
... --crawl                    # 只更新 wiki 快照（增量：已抓过的页跳过）
... --crawl --refetch          # 强制重抓
... --index                    # 只重建 SQLite（改 chunk 参数后用这个，秒级）
... --enrich --concurrency 4   # 只补中文增强（断点续跑）
... --enrich --force-enrich    # 强制重跑（提示词改版后）
... --stats                    # 打印库统计
... --export-md /tmp/dump.md   # 导出可读 markdown，人工抽查解析质量

# 测试
python -m pytest test_story_kb.py -q
```

**换机器/换代理**：设置 `WIKI_PROXY`（空字符串表示直连）。采集器会自动在 4 种 HTTP 后端中挑选可用的那个。

**数据是否需要入库**：不需要。`QQBot/data/*` 已被 `.gitignore` 忽略，`data/story_kb/` 是纯构建产物（wiki 快照 + LLM 产物 + SQLite），用上面的命令重建即可。

**常见问题**

| 现象 | 原因 / 处理 |
|---|---|
| `story_search` 回「知识库尚未构建」 | `data/story_kb/story.db` 不存在 → 跑 `--all` |
| 采集全部 403 | Cloudflare 拦了当前出口 IP → 配 `WIKI_PROXY` 或换网络 |
| 版本不匹配（`available=False`） | schema 版本升级后未重建 → `--index` |
| 中文检索空结果 | `zh/units.json` 为空或未 `--index` → 先 `--enrich` 再 `--index` |
| 注入过于频繁/几乎不注入 | 调 `lib/story_kb.py` 的 `_STRONG_SCORE`、`_SCENE_TERM_MAX_DF`、`_GENERIC_TERMS` |

---

## 10. 已知限制与后续可选

1. **语义鸿沟**：BM25 + bigram 是词法检索。「露比是什么性格」这类没有具体实体的抽象提问召回弱 —— 但这本就该由人格卡而不是剧情库回答。
2. **译名已统一到官方包**（原「译名不统一」的限制已解决）。关键转折是**让 LLM 重新按对照表翻译**（增强提示 v4，见 §5.4），而不是事后替换：
   - `config/characters/members_info.json` 的 `name` 字段是官方译名（从官方中文包解出，`e7_name` 才是 E7 对应名）；
   - `config/characters/story_name_variants.json` 是兜底用的「民间写法 → 官方写法」对照（统计候选 + 一次 LLM 甄别 + 手工补充），另有 `cast_exclusive` 规则处理只差一字的同貌角色（璐茜娅/璐茜玛 姐妹）；
   - 归一后的反向映射（官方 → 民俗写法）仍写入检索块，所以**用任一种写法提问都能命中**；
   - 每个场景额外存 `cast_line`（`Trist=翠丝特、Marsha=玛夏…`），注入块里以「本段人物」一行给出，模型不需要自己音译。
   - 覆盖率：`cast_line` 覆盖 1 838/1 906 场景（96%）；归一后 gist 中残留的民俗写法为 0。
3. **成人向内容**：源站含 R18 剧情文本，知识库原样保留（人格卡本身即成人向设定）。若需过滤，应在 `parse_page` 增加内容分级标记。
4. **可选增强**：
   * 剧情「通读」超长上下文模式（`/剧情 <章节>` 一次性注入整章）；
   * 与羁绊系统联动：好感等级到达阈值时解锁对应章节摘要（复用 `bond.py` 的等级事件）；
   * 逐句翻译缓存（只翻译被注入过的片段，按命中热度摊销成本）。

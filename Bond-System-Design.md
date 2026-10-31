# Bond System 规划文档 — 角色养成系统（双平面架构 + 好感度 + 多模态创作与记忆）

> 状态：设计定稿，待实施
> 日期：2026-10-20
> 关联文档：`Cache-Hit-Rate-Plan.md`（提示词分层约束）、`Profile-Fact-Extraction-Plan.md`（用户事实抽取）、`QQBot/DOCUMENTATION.md`

---

## 1. 背景与目标

本机器人已具备可切换的多人格系统（`QQBot/agent/personality.py`，markdown 角色卡注入 L2 提示词层，支持用户级与群级绑定）。在此基础上拟建设**角色养成系统**：

1. **常驻陪伴**：角色人格是默认的对话面，好感度随自然交互持续累积；
2. **UGC 角色**：用户可通过 LLM 引导式访谈创作自己的角色；
3. **养成反馈**：好感等级驱动称呼演化、语气演化、档案解锁、专属剧情场景；
4. **多模态创作**：角色可生成/发送语音（TTS）与图片（图像生成），并对多模态内容建立文本化记忆。

**核心定位（第一轮设计结论）：**

> 养成系统是「常驻的对话面」，不是「可进出的模式」；工具执行是它路由出去的任务，不是它自己的能力。

否决「用户进入/退出 RP 子模块」方案的理由：

- 与养成产品目标冲突：进入/退出使好感累积断续，且增加用户心智成本；常驻才有主动问候、纪念日、重逢事件的立足点；
- 状态机成本高：现有状态维度已有人格绑定（用户/群）、连续会话窗口、特殊会话、权限角色，再叠加「RP 模式开关」会产生大量交叉状态（如群内部分用户处于 RP 模式）；
- 现有 L2 人格注入本身就是嵌入式设计，子模块方案反而推倒重来；
- 「退出扮演」需求 = 把人格切回 assistant，现有 `set_user_personality` 已覆盖，无需新模式。

---

## 2. 市面游戏参照与取舍

| 参照 | 借鉴点 | QQ 场景取舍 |
|---|---|---|
| FGO 羁绊等级 | 等级 → 解锁专属台词/剧情，纯陪伴累积 | ✅ 核心骨架 |
| 原神好感度 | 聊天/邀约自然涨好感，无操作负担 | ✅ 主涨点来源 = 自然对话，不做点击刷分 |
| 明日方舟信赖 | 档案分段解锁（25%/50%/100%） | ✅ 角色背景按级解锁，喂给 LLM 演绎 |
| 星露谷/动森 | 礼物喜好分档、心事件 | ✅ 礼物按角色偏好表分档；❌ 不做缺席惩罚 |
| 女神异闻录 Confidant | Rank Up 事件、专属剧情 | ✅ 用 special_session 承载「剧情场景」 |
| 恋与深空 | 短期亲密/心情状态 | ✅ 短期「心情值」与长期「好感度」双轨 |
| MemVerse (arXiv:2512.03627) | 多模态经验蒸馏为结构化文本记忆；双通路（快速参数化 + 分层检索） | ✅ 文本化记忆 + 锚点/检索双通路；❌ 不引入知识图谱与向量库（量级不需要） |
| MemCanvas（对 MemVerse 的批评） | 纯文本化丢失视觉结构 | ✅ 对策：文本化是检索层不是存储层，二进制保留为可淘汰缓存 |

**QQ 机器人特有约束：**

- 不能高频主动推送（风控 + 打扰），主动互动只能低频（挂 HEARTBEAT）；
- 戳一戳（notice 事件）是天然的「摸头」交互；
- 群聊是公共场合：养成主场景在私聊，群聊内「只涨不演」（不触发亲密剧情）；
- 提示词缓存分层（Cache-Hit-Rate-Plan）：好感状态每轮变化，**绝不进 L0–L2 缓存前缀**，只进动态层。

---

## 3. 系统定位：双平面常驻路由架构

### 3.1 为什么角色面不能直接持有工具（现状问题）

现有 RP 人格（如 rubi）采用单平面混跑（「你依然可以访问搜索、代码等工具，但会用可爱的方式表达结果」），有四个硬伤：

| 问题 | 说明 |
|---|---|
| 任务质量退化 | RP 约束（句末加♡、禁 markdown、回复简短）与工具任务输出要求（精确、结构化、代码块）直接冲突 |
| **安全耦合（最严重）** | RP 人格抗 jailbreak 能力弱（可用剧情诱导），直连 `shell_exec`/`execute_code` 意味着角色扮演层越狱可换取工具层提权。rubi.md 中「不受人伦道德法律约束」类语句 + 危险工具 = 现实风险 |
| 缓存碎片化 | 工具 schema + 角色卡 + 任务指令挤同一前缀，人格越花哨 L1/L2 命中越差 |
| 权限防御面膨胀 | schema 过滤 + 硬拒绝双层防御按「单 agent」设计，角色直连工具后防御面随人格数量线性增长 |

### 3.2 架构图

```
QQ 消息 → agent_router
            │
            ▼
   ┌─ 扩展 ModelRouter.classify_complexity ─┐   ← 已有的每消息 LLM 分类调用，
   │  输出: { plane: social | task,          │      加一个字段，零额外成本
   │          complexity: flash | reasoning }│
   └──────────────┬─────────────────────────┘
        social    │           task / mixed
          ▼       │                ▼
  ┌───────────────┴──┐    ┌──────────────────────┐
  │ 角色面（常驻）      │    │ 任务面 = 现有 Agent    │
  │ · L2 = 角色卡      │    │ · L2 = Roxy 中立人格   │
  │ · flash 模型       │    │ · 全量工具 + 权限双层防御│
  │ · 仅社交工具:       │    │ · ModelRouter 决定     │
  │   好感查询/送礼/    │    │   flash vs reasoning  │
  │   场景触发/创角/    │    │ · 自己的 20 轮循环     │
  │   发语音/画图      │    │                      │
  │ · 兜底: delegate_  │───▶│                      │
  │   task 工具        │◀───│ 结果(含一句话摘要)      │
  └───────┬──────────┘    └──────────────────────┘
          ▼
   转述层: 角色口吻包装任务结果
   → 好感结算钩子（无论走哪个平面，每轮都结算 → 「常驻」）
```

### 3.3 路由的三条关键设计

1. **分类复用 ModelRouter**：它本就对每条消息做一次 LLM 分类（flash vs reasoning），输出扩展为 `{plane, complexity}` 是免费的。`social` → 角色面独立回答；`task` → 任务面执行，角色只做转述。
2. **`delegate_task` 兜底工具**（agent-as-tool 模式）：分类器误判 social 但角色发现需要工具时，可主动调用 `delegate_task`——注册在 ToolRegistry 的普通工具，内部运行任务面 Agent，结果作为 observation 回到角色自身的 Think→Act→Observe→Respond 循环，**转述是循环的天然产物，无需额外 LLM 调用**。任务面禁止持有 `delegate_task`（防递归，仅单层嵌套）。
3. **转述层分级**（控制成本与噪音）：
   - 私聊默认：任务面返回「结果 + 一句话摘要」，角色面用一次 flash 小调用把摘要转成 in-character 开场白，**结果正文强制原样透传**（prompt 与代码双层约束，防篡改事实）；
   - 群聊默认：模板化包装（免 LLM 调用）；
   - 角色卡可配 `task_voice: character | template`。

### 3.4 兼容与迁移

人格卡增加元数据 `mode: unified | split`：

- **assistant（Roxy）= unified**：本身即任务智能体，行为零变化，bot 的本职功能（搜索/代码/兑换码/wiki）完全不受影响；
- **rubi / roxy_character / 所有 UGC 角色 = split**：走双平面；
- 迁移 = 换卡 + 删除卡内「你依然可以访问工具」段落，工具能力经 delegate 自动回归且质量更好。

### 3.5 子系统归属表

| 子系统 | 归属 | 说明 |
|---|---|---|
| BondManager（好感/心情/结算） | agent_router 回合结束钩子 | 不分平面每轮结算；等级/称呼/心情/已解锁档案只注入**角色面**动态层 |
| 剧情场景、创角访谈 | 角色面社交工具 | 本质是 special_session，天然归角色面 |
| 提示词缓存 | 两平面各自前缀稳定 | 角色面 L2 = 角色卡（user×char）；任务面 L2 = Roxy 卡为**全局共享前缀**，命中率高于现状 |
| 权限 | 收敛到任务面单点 | 角色面 schema 无危险工具，第一层过滤变物理隔离 |
| 上下文隔离 | 任务面不接收 RP 历史 | delegate/路由只传「任务请求 + 最近几轮事实性摘要」（防污染、省 token） |
| 失败路径 | 错误 observation 回角色面 | 角色 in-character 承认失败，审计日志照旧记在任务面 |

---

## 4. Bond 核心设计

### 4.1 三值模型

```
好感度 Bond（长期，只涨不掉，等级制）
   └─ 决定：称呼、语气演化、档案解锁、剧情场景解锁
心情 Mood（短期，0–100，基线 60，数小时回归）
   └─ 决定：当轮回复语气色彩、涨点系数（×0.8~×1.2）
陪伴统计 Stats（累计天数/消息数/礼物数/共同事件）
   └─ 决定：纪念日、重逢事件、卡片渲染素材
```

**关键原则：好感永不衰减。** 久别（≥14 天）触发「重逢惊喜事件」而非惩罚——机器人是陪伴品不是电子鸡，不制造焦虑。心情在数小时内线性回归基线。

### 4.2 等级曲线（10 级，称号可在角色卡自定义）

| Lv | 默认称号 | 累计 EXP | 解锁示例 |
|---|---|---|---|
| 1 | 陌生 | 0 | 基础人设 |
| 2 | 相识 | 100 | 记住用户的称呼（读 ProfileManager） |
| 3 | 熟悉 | 300 | 语气软化 + 档案Ⅰ（角色秘密背景） |
| 4 | 朋友 | 650 | 剧情场景Ⅰ + 图片生成解锁 |
| 5 | 密友 | 1200 | 档案Ⅱ + 专属表情包/卡片 + **语音解锁** |
| 6 | 知己 | 2000 | 语气完全解禁（傲娇→坦率）+ 剧情场景Ⅱ |
| 7 | 暧昧 | 3000 | 称呼升格 + 低频主动问候（心跳挂点）+ 主动语音 |
| 8 | 恋人 | 4300 | 告白事件 + 剧情场景Ⅲ |
| 9 | 誓约 | 6000 | 纪念日系统 + 誓约档案 |
| 10 | 灵魂伴侣 | 8000 | 全解锁，专属结局剧情 |

### 4.3 涨点经济与防刷

| 行为 | EXP | 限制 |
|---|---|---|
| 每日首次对话 | +20 | 每角色每日 1 次 |
| 自然聊天 | +2~5/轮（心情加权） | **每日软上限 150**，超出后减半再减半 |
| 长会话奖励（≥10 轮） | +30 | 每日 1 次 |
| 送礼 | +5 / +15 / +30（讨厌/普通/喜爱） | 每日 3 次 |
| 戳一戳（notice 事件） | +5 | 每日 3 次 |
| 完成剧情场景 | +100 | 场景自带冷却 |
| 生日/相识纪念日 | +50 | 自动，读 profile |
| 用户上传图片/语音 | +3 | 每日封顶（防刷） |

日均满效率 ≈250 EXP → Lv10 约 32 天肝满、休闲玩家约 2 个月。**所有数值落配置文件，可调。**

### 4.4 特殊互动的三种形态

1. **被动演化**（零成本）：语气/称呼随等级变化——纯 prompt diff；升级瞬间角色发一条 in-character 庆祝消息（复用 300 字分段机制）；
2. **档案解锁**（方舟式）：等级到达 → 角色在合适时机「透露」背景故事，同时写入 bond_events 成为共同记忆；
3. **剧情场景**（Confidant 式）：用户触发或升级自动邀请 → 开 special_session 载入分幕场景脚本（card.md 内作者预写），**仅限私聊**；结束 +100 EXP，并用 `card_renderer.py` 生成纪念卡片（P4 后升级为生成式 CG + 配音）。

---

## 5. UGC 角色创作

### 5.1 创作流程（LLM 引导式访谈，不要求用户会写 prompt）

```
用户：「帮我创建一个角色」
→ 进入创作 special_session，Agent 逐项访谈：
  姓名/性别 → 性格关键词 → 世界观背景 → 与用户的关系设定 →
  对用户的初始称呼 → 说话风格示例 → 礼物偏好(喜爱/普通/讨厌各3) →
  外观锚点描述 → 各等级想解锁的档案与剧情
→ LLM 生成结构化 card.md + meta.json → 敏感内容审查 → 落盘注册
```

### 5.2 配额与可见性

- 配额沿用 special_session 模式：**regular 1 个 / vip 3 个 / admin 10 个**（`permissions.py`）；
- 默认私有；「发布到画廊」供他人缔结（每人独立好感线）延后至 P2 之后评估（涉及内容审核责任）；
- 内置人格（rubi/roxy_character）自动生成「官方角色卡」接入好感系统（rubi 的「团长」设定天然为高等级剧本）。

### 5.3 card.md 结构约定

```markdown
# 显示名

mode: split
task_voice: character

## Core Personality / Communication Style / Event & Facts
（与现有 config/personalities/*.md 兼容）

## Appearance Anchor
（外观 tag 串，图像生成强制前缀，一致性锚点）

## Bond Levels
（可选：自定义各等级称号，覆盖默认表）

## Bond Tiers
### Lv3 · 档案Ⅰ
（解锁后注入动态层的背景内容）
### Lv4 · 场景 · 雨夜谈心
（分幕场景脚本 prompt）
### Lv6 · 语气解禁
（prompt diff 指令）
...

## Gift Preferences
喜爱: [...] / 普通: [...] / 讨厌: [...]
```

---

## 6. 提示词注入与缓存策略（Cache-Hit-Rate-Plan 对齐）

```
L0/L1  系统共享块                      ← 不动
L2     角色卡基础人设（静态）            ← key = (user, char)，仅人格切换时变
       任务面 L2 = Roxy 卡              ← 全局共享前缀，所有用户命中同一段
动态层  ── 好感状态块（每轮拼装，放 profile 附近）──
       当前等级/称号、双方称呼、心情一句话、
       已解锁 Bond Tiers 内容（档案 + 语气指令 diff）、
       最近 3 条共同经历、
       影音记忆 top-k（见 §8.3，预算 ~5 条）
```

已解锁内容放**动态层而非 L2**：升级低频但用户间各异，塞 L2 会打碎缓存前缀；动态层每轮多 ~200 token，换 L2 全量命中。

---

## 7. 多模态创作子代理

### 7.1 挂点（双平面中的位置）

| 形态 | 挂点 | 理由 |
|---|---|---|
| 一次性生成（发语音、画自拍/立绘） | **角色面社交工具** `send_voice` / `create_image` | 单次 provider 调用无需多轮循环；先例：`play_gacha_animation` 已通过 contextvars 回调直发 `MessageSegment.image` |
| 复合制作（剧情 CG+配音、多格漫画、有声短剧） | **任务面子代理**（delegate 同款模式） | 需要多步编排，走任务面完整循环 |
| 主动调用 | 角色面决策，三重门控 | 见 7.2 |

### 7.2 主动调用门控（防骚扰）

1. **好感等级门**：图片 Lv4 / 语音 Lv5 / 主动「想你了」语音 Lv7 —— 多模态即养成奖励，与 Bond Tiers 咬合；
2. **心情/场景概率门**：高心情 + 私聊 + 剧情节点 → 概率触发，而非每轮判定；低频主动挂现有 HEARTBEAT；
3. **配额门**：按角色 regular/vip/admin 每日生成限额；主动语音硬上限 ≤1 次/天/用户（QQ 风控）。

### 7.3 Provider 层（新增 `lib/media_gen.py`）

仿照 `lib/browser_fetch.py` 共享运行器模式（并发闸 + 超时 + 重试）：

```
TTS:    edge-tts（免费基线）→ GPT-SoVITS / CosyVoice（角色音色克隆，per-char voice_id）→ fish-audio API
IMAGE:  SiliconFlow / 即梦 API → 本地 SD-WebUI / ComfyUI（若有 GPU）
配置:   .env: TTS_PROVIDER / IMAGE_PROVIDER / 各自 API key；走 sing-box 代理(127.0.0.1:1081)同现有惯例
输出:   mp3（NapCat record 段）/ png（image 段），发送前大小预检
```

### 7.4 一致性锚点（MemVerse「快速通路」的廉价对应物）

角色卡 `## Appearance Anchor`（外观 tag 串）+ `meta.json` 固定 `voice_id` / 参考立绘（存 `data/characters/{char_id}/assets/`，不参与淘汰）。所有生成请求强制前缀锚点，保证角色「每次同一张脸、同一声线」。锚点常驻 prompt，不走检索。

### 7.5 延迟与发送

图像生成 5~30s：角色先回「等一下哦，露比去画～♡」，完成后经 contextvars send 回调补发（复用现有分段发送机制）。`context.py` 新增 `send_record` 回调（语音段）。

---

## 8. 多模态文本化记忆（MemVerse 方案的轻量化落地）

### 8.1 核心原则

> **文本化是检索层，不是存储层。原始二进制保留为「可淘汰缓存」，文本记忆才是本体。**

依据：

- **生成侧文本化免费**：图片/语音是系统自己生成的，生成 prompt、TTS 台词、场景上下文在生成时刻已是文本，无需蒸馏管线；仅**用户上传**内容需要 VLM caption / ASR 转写；
- **吸收 MemCanvas 对 MemVerse 的批评**（文本化丢失视觉结构）：原文件被淘汰后角色仍能谈论记忆；需重发时按 asset_id 找回原文件，找不到则重新生成；
- **不引入向量库**：单用户资产量级为百~千而非百万，关键词检索 + 歧义时一次 LLM 重排足够；现有记忆栈全是 markdown+frontmatter（人与 agent 均可读写审计）；向量库引入 embedding 模型/存储/漂移管理，收益为负；
- **双通路对应**：锚点（§7.4）= 快速参数化通路（常驻 prompt）；事件/资产卡 = 分层检索通路（按需注入）。

### 8.2 三层结构

```
① 资产层（二进制，可淘汰）
   data/assets/{user_id}/{char_id}/{asset_id}.{png|mp3}
   data/characters/{char_id}/assets/          ← 锚点资产（不淘汰）
   配额沿用 UserWorkspaceManager 模式；quota_cleanup.py 扩展 LRU 淘汰：
   按 recall_count × 时间衰减打分；淘汰只删二进制、保留文本卡（path 置 null）

② 文本索引层（记忆本体，MemorySystem 兼容，mem_type="asset" / "episode"）
   A. 资产卡（每个生成/接收资产一张）：
      ---
      id: ast_7f3k2
      type: image | voice | user_upload
      char_id: rubi
      user_id: "12345"
      path: data/assets/.../ast_7f3k2.png     ← 淘汰后置 null
      model: sd-xl / gpt-sovits / edge-tts
      prompt: "..."                            ← 生成侧免费文本
      transcript: "团长晚安，才不是..."          ← TTS 台词免费文本
      scene_ref: ep_20261018_雨夜谈心
      tags: [雨夜, 立绘, 剧情CG]
      created_at: 2026-10-18T22:31:00
      recall_count: 2
      ---
      （正文 = VLM 一句话描述；user_upload 类型由 VLM caption / ASR 转写生成）
   B. 事件卡（episode，MemVerse「记忆蒸馏」轻量版）：
      一次剧情/约会把 N 张资产卡 + 对话高光蒸馏为一条事件记忆，
      直接进 TieredMemory 的 short→medium→long 晋升机制（现成）

③ 召回注入层（动态 prompt 层，token 预算 top-k≈5）
   ## 你与团长的影音记忆
   - [雨夜立绘] 10-18 剧情·雨夜谈心：撑伞回眸…（ast_7f3k2，可重发）
   - [语音·晚安] 10-19：「才不是担心你熬夜呢！」（ast_9x2m1）
   检索 = MemorySystem.search 关键词 + 歧义时 LLM 重排一次；
   配套角色面工具 recall_asset(描述|id)：找回原文件重发；已淘汰 → 告知可重新生成
```

### 8.3 用户上传的对称通路

NapCat 收到图片/语音 → 现有 `_download_and_save_file` 落 `uploads/` → VLM caption / ASR 转写 → 同款资产卡（`type: user_upload`）。效果：「用户发来的自拍」与「角色画的立绘」进同一套记忆，角色能回忆用户发过什么——好感系统的强沉浸点。EXP 小额封顶（§4.3）。

### 8.4 去重与审计

- `prompt_hash` 去重，避免重复生成同图；
- 每条生成资产写入 Agent 现有审计日志：prompt + 输出路径 + 触发来源（用户请求/主动）；
- 生成侧内容过滤前置（UGC 角色卡可能诱导擦边内容，provider 层统一过滤，与角色卡内容解耦）。

---

## 9. 数据模型总览

```
QQBot/
├── agent/
│   ├── bond.py                     # 新增：BondManager（EXP/等级/心情/结算，JSON 持久化 + mtime 校验）
│   ├── character_registry.py       # 新增：叠加于 PersonalityManager，统一内置人格与 UGC 角色
│   └── asset_memory.py             # 新增：资产卡/事件卡管理（或扩展 memory.py 增加 mem_type）
├── lib/
│   └── media_gen.py                # 新增：TTS/图像 provider 共享运行器
├── tools/
│   └── creation_tools.py           # 新增：send_voice / create_image / recall_asset / gift / create_character
└── data/
    ├── characters/{char_id}/
    │   ├── card.md                 # UGC 角色卡（§5.3 结构）
    │   ├── meta.json               # 作者/可见性/voice_id/礼物偏好/创建时间
    │   └── assets/                 # 锚点资产（立绘/音色样本，不淘汰）
    ├── bonds/{user_id}.json        # {char_id: {exp, level, mood, mood_updated,
    │                               #  stats:{days,msgs,gifts}, unlocked:[], milestones:[]}}
    ├── bond_events/{user_id}/{char_id}.md   # 共同经历日志（喂记忆注入）
    ├── assets/{user_id}/{char_id}/          # 生成/接收的二进制资产（可淘汰）
    └── memory/…/asset|episode 条目           # 资产卡/事件卡（MemorySystem 既有目录结构）
```

**持久化纪律**（吸取 panel↔bot 缓存一致性教训）：所有 JSON 读写带 mtime 校验，不做无校验的进程内长缓存。

---

## 10. 与现有系统的集成点

| 文件 | 改动 |
|---|---|
| `lib/model_router.py` | 分类输出扩展 `plane: social\|task` 字段（复用既有 LLM 分类调用，零额外成本） |
| `plugins/agent_router.py` | 平面路由；回合结束好感结算钩子；注册新工具；戳一戳 notice 事件接入 |
| `agent/agent.py` | 动态层注入好感状态块 + 影音记忆；转述层约束（正文原样透传） |
| `agent/personality.py` | 叠加 CharacterRegistry；人格卡元数据 `mode` / `task_voice` |
| `agent/permissions.py` | 创角配额、生成配额（regular/vip/admin）；角色面社交工具集定义 |
| `agent/context.py` | 新增 bond 状态、send_record 回调等 contextvars |
| `agent/profile.py` | 读取称呼/生日供好感上下文与纪念日使用（只读，不改） |
| `agent/memory.py` | 新增 mem_type: asset / episode（事件卡走 TieredMemory 晋升） |
| `agent/special_session.py` | 剧情场景与创角访谈的承载（模板化场景脚本载入） |
| `agent/quota_cleanup.py` | 资产 LRU 淘汰（只删二进制保留文本卡） |
| `tools/card_renderer.py` | 好感卡片 / 剧情纪念卡渲染升级 |
| `agent/config/TOOLS.md` 等 | 新工具文档；AGENTS.md 增加 delegate 规则（「涉及实时信息/代码/文件必须 delegate」） |

---

## 11. 风险与对策

| 风险 | 对策 |
|---|---|
| 分类器把 task 误判为 social → 角色瞎编事实 | 角色卡固定注入 AGENTS 级规则「涉及实时信息/代码/文件操作必须 delegate」+ delegate 兜底工具；上线前用现有消息日志跑分类准确率基线 |
| task 回合延迟增加 | 路由直达任务面的主路径无额外调用；仅 delegate 兜底路径多一次 flash 调用（~1s），可接受 |
| 转述层篡改事实 | 只允许包装开场白，任务结果正文强制原样透传（prompt + 代码双层约束） |
| 角色面被剧情越狱 | 危险工具物理隔离在任务面；任务面为中立人格 + 权限双层防御，RP 叙事不进入任务面上下文 |
| UGC 角色卡内容违规 | 创作访谈产出后统一敏感审查；provider 层生成过滤与角色卡解耦 |
| 主动推送打扰/风控 | 等级门 + 概率门 + 配额门三重限制；主动语音 ≤1 次/天/用户 |
| 好感刷分 | 每日软上限 + 递减收益 + 各行为独立限额 |
| 资产撑爆磁盘 | 配额 + LRU 淘汰（文本卡永生，二进制可弃） |
| 生成模型服务不稳定 | media_gen.py 超时/重试/provider 降级链（如 GPT-SoVITS 失败降 edge-tts） |

---

## 12. 分期路线图

| 阶段 | 内容 | 主要交付 |
|---|---|---|
| **P0** | Bond 核心 + 双平面路由 | `agent/bond.py`；ModelRouter plane 字段；`delegate_task`；转述层；人格卡 `mode: unified/split`；好感结算钩子；`好感度` 查询指令；动态层注入 |
| **P1** | UGC 角色 | 访谈式创作 special_session；`character_registry.py`；创角配额；与人格切换/群绑定打通；内置人格官方角色卡 |
| **P2** | 互动动作 | 礼物 + 偏好表；戳一戳接入；称呼演化；档案解锁；升级庆祝消息；画廊共享评估 |
| **P3** | 剧情场景 | 场景脚本引擎（special_session 承载）；纪念卡片渲染；重逢/纪念日事件；HEARTBEAT 低频主动问候 |
| **P4** | 多模态生成 | `lib/media_gen.py`；`send_voice` / `create_image` 工具；资产层 + 配额；资产卡；一致性锚点；`send_record` 回调 |
| **P5** | 多模态记忆闭环 | 事件卡蒸馏进 TieredMemory；召回注入；`recall_asset` 重发；用户上传文本化（VLM/ASR）；LRU 淘汰 |

**分期依据：**

- 每期独立可上线；P0 对现有人格即刻生效（rubi 的「团长」设定天然是高等级剧本）；
- P4/P5 拆开：生成工具无记忆也能独立上线（一次性发图发语音）；记忆闭环依赖 P0–P3 的 bond/episode 结构先就位；
- 数值参数（等级曲线、涨点、配额）全部配置化，上线后按真实数据调平衡。

---

## 附：参考

- MemVerse: Multimodal Memory for Lifelong Learning Agents — arXiv:2512.03627（上海 AI Lab；CLS 双通路：快速参数化回忆 + 分层检索记忆；多模态经验蒸馏为结构化记忆）
- MemCanvas: Visual Long-Term Memory — OpenReview xVloKUKee8（对纯文本化路线的批评：丢失视觉结构 → 本方案以「二进制可淘汰缓存 + 文本本体」对策）

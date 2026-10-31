# QQBot Agent

基于 **NoneBot2** 构建的智能 QQ 机器人，采用 **LLM Agent 架构**（Think → Act → Observe → Respond），支持工具调用、用户画像、三层长期记忆与角色养成。

**双协议渠道**：一条内核（`agent/turn.py`）+ 两套入口适配器——`NapCat / OneBot V11`（QQ 号身份）与 `QQ 官方机器人 API`（openid 身份，单聊与群 @）。两条渠道共用全部工具、记忆与权限逻辑，互不干扰。

## 特性

- **智能体架构** — Markdown 配置驱动，OpenAI 兼容 Function Calling，最多 20 轮工具调用
- **多模型路由** — 轻量 FLASH 模型处理简单任务，强力 REASONING 模型处理复杂推理，MULTIMODAL 模型理解图片，AUDIO 模型分析语音，OCR 模型识别战斗截图，IMAGE_GEN / TTS 模型负责画图与语音合成
- **直连识图（Direct Vision）** — 模型段开启 `supports_vision` 后，本轮上传/引用的图片以 `image_url` content part 直接进主模型（最长边 1568px、GIF 取首帧、≤4 张/轮、编码后 ≤5MB），不再绕道视觉模型
- **双平面路由** — 角色人格下，闲聊由角色本人回应；搜索/计算/文件等任务委派给任务面（agent-as-tool：隔离上下文、按角色过滤工具、单层嵌套防递归），结果再由角色口吻转述
- **角色养成（羁绊）** — 好感度 10 级（只涨不掉）+ 心情值（随时间回归基线）+ 陪伴统计；送礼、戳一戳、称呼演化、随等级解锁角色档案；剧情场景分幕演绎与纪念卡；久别重逢与相识 30/100/365 天里程碑
- **自创角色（UGC）** — `/创建角色` 访谈式创作（姓名/性格/语气/外观/初始羁绊），可设参考立绘与声纹，保存为仅自己可见的角色卡，`/人格切换` 后即可养成
- **影音生成与记忆** — Lv4 起可要图、Lv5 起可要语音，形象与声线保持一致；生成的图/语音落「资产卡」（文本卡是本体、二进制是可淘汰缓存），支持按记忆召回、事件蒸馏与 LRU 淘汰
- **原作剧情知识库** — 本地 SQLite/FTS5 语料（约 5 万句对白，中英 bigram 索引 + RRF 重排），角色扮演时按人格白名单注入相关剧情，也可由 `story_search` 工具主动检索
- **官方渠道主动互动** — 仅在官方单聊：**角色信箱**每日 10:00–22:00 发一封「信封」摘要（沉默 ≥24h 且羁绊 Lv≥5 的角色各写一句 ≤60 字留言，markdown + 展开按钮，失败逐档降级纯文本；`/邮件` 回看，信件保留 28 天）、Lv7+ 偶发主动发图/发语音；两类主动消息同日互斥不双发，发送经令牌桶限速（c2c 10 条/秒），用户点「拒收」自动停发（`C2C_MSG_REJECT`/`RECEIVE` 事件驱动）；管理员可在面板邮件管理页勾选用户发送**系统邮件**或撤回信件（拒收用户自动发送失败不入账）
- **官方渠道原生渲染** — 仅在 QQ 官方路由（OneBot 恒纯文本）：unified 人格回复与 `/帮助`/`/兑换码` 走原生 markdown（msg_type=2）；消息按钮（指令按钮替用户打字 + 回调按钮毫秒级本地操作，如会话列表/删除），不支持时自动降级为纯文本命令清单；**流式消息**（仅单聊）让工具进度「⏳ …」与回答逐字上屏（替代「输入中状态」），开流失败/中途断裂自动回退缓冲全文，内容不丢。三者各有熔断器 + 热读开关（`QQ_OFFICIAL_MARKDOWN` / `QQ_OFFICIAL_BUTTONS` / `QQ_OFFICIAL_STREAM`）
- **特殊会话** — 每用户至多 10/3/1 个（按角色），快照+增量双层存储，读时分层截断适配模型上下文窗口；`/压缩会话` 可将长会话早期历史浓缩为摘要（≥85% 窗口时人格化主动建议，≥95% 紧急截断保护）；**剧情/角色创作会话豁免此配额**（剧情完结自动封档、可回看，总量上限 `MAX_SCENE_ARCHIVES`，默认 20）
- **用户工作区** — 每用户独立文件空间，配额管理（3 级策略），跨会话隔离
- **群聊连续对话** — @一次后 90 秒内免 @，消息自动续期
- **自托管搜索** — SearXNG 聚合搜索 + `web_fetch` 直接抓取网页（搜索无结果时的 fallback）；完整结果自动存档（7 天/每用户 50 条），上下文折叠后可经 `recall_search_result` 按 id 取回，无需重搜
- **代码执行** — 三层安全隔离（模式匹配 + `python3 -I` 隔离 + 资源限制，分级限制：管理员 60s，会员 15s；输出上限统一 16KB）
- **文件阅读** — 支持文本 / PDF / 图片 / 音频（多模态 AI 分析，语音转文字+情绪识别）
- **用户系统** — 三层记忆引擎（SHORT / MEDIUM / LONG，`TieredMemory`）+ LLM 驱动用户画像提取（含确定性事实过滤 `fact_filter`）
- **Web 管理面板** — FastAPI 后台（`webui/`）：进程管理、配置热编辑（提示词/人格/.env/模型结构化表单+API 可用性探测）、记忆/画像编辑（与 bot 内存缓存 mtime 一致性校验）、**角色管理（用户自创角色的 card.md / meta.json 分区编辑 + 静态体检 + 版本回滚 + 立绘上传 + 注入预览）**、Wiki 别名管理（详情列表合并解包数据中 wiki 未收录的新角色，待补录含官方名占用冲突一键拆分）、Token 用量看板、反馈处理、更新记录/公告编辑与双渠道群发、**邮件管理（用户信箱只读查看 + 勾选发送系统邮件 + 信件撤回，经文件队列由 bot 执行回写状态）**、Playground
- **更新记录与公告** — 结构化更新日志（版本/日期/分类变更条目），用户经 `/更新日志 [d]`、`/update record [d]` 或自然语言查询；管理员在面板编辑**更新记录**与**自由公告**，并可双渠道群发（OneBot 群 + QQ 官方群，图片卡片/markdown 呈现，官方群按登记表预检、退群自动标记）；更新内容自动同步到公开使用站点 `bot.oneweblog.cn`（使用说明+演示）
- **游戏工具** — 抽卡模拟（Wiki 自动爬取卡池数据）、团战截图测速（OCR 解析战斗截图，轻量/全量两种模式）、乱速概率计算、角色/羁绊查询（面板/技能/倍率）、兑换码查询（自动爬取+手动维护）
- **地图服务** — 地址↔坐标转换、实时天气、POI搜索、路线规划（高德地图）
- **群聊管理** — 按群开关功能（抽卡/图片/语音），超级用户可远程管理
- **人格切换** — 内置人格（助手 Roxy / 角色 Roxy / 露比）+ 用户自创角色；人格绑定在**会话**上（特殊会话各自记住自己的人格，切换会话自动切换人格），并支持用户级与群级默认（`/toggle personality`）；切换后可选择保留历史或清空上下文
- **安全设计** — 工作区隔离、路径验证防穿越、Git URL 注入防护；两条渠道各自的身份表（QQ 号 / openid）与私密数据隔离

## 技术栈

| 层 | 技术 |
|---|---|
| QQ 协议 | NapCat (NT QQ, OneBot V11) + QQ 官方机器人 API（`nonebot-adapter-qq`） |
| 机器人框架 | NoneBot2 + FastAPI |
| 协议适配 | OneBot V11（反向 WebSocket，QQ 号身份）/ QQ 官方（WebSocket 网关，openid 身份） |
| AI 后端 | OpenAI 兼容接口：REASONING / FLASH / MULTIMODAL / AUDIO / OCR 五段路由；IMAGE_GEN / TTS 走 DashScope 原生接口 |
| 图像与语音生成 | DashScope `qwen-image`（文生图/图生图）+ `qwen-audio-tts`（TTS，支持声纹） |
| 剧情检索 | SQLite + FTS5（bigram 中文索引 + BM25/RRF），离线构建于 `scripts/build_story_kb.py` |
| 搜索引擎 | SearXNG (Docker 自托管) |
| 管理面板 | FastAPI + Jinja2 模板（`webui/`，独立进程，scrypt 登录） |
| 运行时 | Python 3.12+ |

## 快速开始

### 前置条件

- Ubuntu 22.04+ / Debian 12+（其他 Linux 发行版需手动安装依赖）
- Python 3.12+
- Docker（用于 SearXNG 搜索服务）
- QQ 账号

### 1. 克隆项目

```bash
git clone <repo-url>
cd QQBotAgent
```

### 2. 一键安装

```bash
bash setup.sh
```

脚本会自动完成：系统依赖安装 → Python 虚拟环境创建 → pip 包安装 → `.env` 模板创建 → SearXNG 容器启动。

### 3. 配置密钥

`QQBot/.env.example` 是 `.env` 的**说明文档 + 模板**（逐行注释每个变量）。首次配置直接复制并填写：

```bash
cp QQBot/.env.example QQBot/.env
```

核心配置项：

```ini
# ── NoneBot 服务 ──
DRIVER=~fastapi+~httpx+~websockets   # 官方渠道需要 httpx + websockets
HOST=0.0.0.0
PORT=8081

# ── OneBot / NapCat 渠道 ──
ONEBOT_ACCESS_TOKEN=你的Token        # 与 NapCat WebUI 中的 Token 一致
COMMAND_START=["/", ""]
COMMAND_SEP=[" "]

# ── QQ 官方机器人渠道（可选，与 OneBot 并存）──
# QQ_BOTS=[{"id":"appid","secret":"secret","use_websocket":true,
#           "intent":{"c2c_group_at_messages":true}}]

# ── 权限（两条渠道各自独立名单）──
SUPERUSERS=["管理员的QQ号1","管理员的QQ号2"]
VIP_USERS=["VIP用户的QQ号1"]
# QQ_OFFICIAL_SUPERUSERS=["openid1"]   # 官方渠道管理员（openid）
# QQ_OFFICIAL_VIP_USERS=["openid2"]

# ── 模型凭据（models_settings.json 留空时回退）──
DEEPSEEK_API_KEY=sk-xxxxxxxx
DEEPSEEK_API_BASE=https://api.deepseek.com
AMAP_API_KEY=你的高德Key              # 可选，地图/天气工具

# ── 数据与工作区 ──
USER_DATA_ROOT=/path/to/store/users/data           # 用户数据根目录
# STORY_KB_DIR=...        # 原作剧情库（默认 $USER_DATA_ROOT/story_kb）
# CHARACTERS_DIR=...      # UGC 角色卡（生产建议与 USER_DATA_ROOT 同盘）
# BONDS_DIR=... / MEMORY_DIR=... / ASSETS_DIR=...
MAX_SPECIAL_SESSIONS=3                 # 默认值，实际按角色（10/3/1）
USER_WORKSPACE_QUOTA_MB=500            # 默认值，实际按角色（2GB/500MB/100MB）

# ── 性能 / 可选服务 ──
BUFF_DETECTOR_WORKERS=2                # 团战测速 buff 图标匹配并行线程数
# SEARXNG_ENDPOINT=http://localhost:8082
# NAPCAT_HTTP_BASE=http://127.0.0.1:6099
# BOT_PUBLIC_SITE_URL=https://bot.oneweblog.cn   # 更新公告里的指路短链
```

> 完整清单（含兑换码抓取代理、`web_fetch` 反爬浏览器、`WIKI_PROXY` 等可选段）见 `QQBot/.env.example` 的逐行注释。

**多模型配置（可选）**：编辑 `QQBot/config/models_settings.json` 配置各模型（REASONING/FLASH/MULTIMODAL/AUDIO/OCR），留空则回退到 `.env` 的 DeepSeek 默认配置。参考 `QQBot/config/models_settings_example.json` 格式。也可在 Web 管理面板「配置热管理 → 模型」中用结构化表单逐段编辑：保存前会以最小真实调用探测 API 可用性（仅验证通过的配置才落盘），保存后 bot 约 10 秒内自动热重载，无需重启。

### 4. 安装 NapCat（QQ 协议适配）

```bash
bash napcat.sh --docker n
```

或参考 [NapCat 官方文档](https://github.com/NapNeko/NapCatQQ) 手动安装。

**关键配置**：在 NapCat WebUI（`http://localhost:6099`）中设置：
- 反向 WebSocket 地址：`ws://127.0.0.1:8081/onebot/v11/ws`
- Access Token：与 `.env` 中的 `ONEBOT_ACCESS_TOKEN` 一致

### 5. 启动

```bash
bash start.sh
```

启动后：
- NoneBot API：`http://localhost:8081`
- SearXNG 搜索：`http://localhost:8082`
- NapCat WebUI：`http://localhost:6099`

### 6. 验证

在 QQ 群聊或私聊中发送：

```
@Roxy /status
```

应返回已注册的工具列表（40 个）与机器人运行状态。

**官方渠道**（如已配置 `QQ_BOTS`）：把机器人添加到群或发起单聊后发 `/status`，并观察日志里的 `[qq_official] c2c message / group message` 行；WebUI 仪表盘的「QQ 官方路由」卡片会显示连接状态与消息计数。

### QQ 命令一览

在 QQ 聊天中 @机器人 或直接发送以下命令：

**会话管理**

| 命令 | 说明 |
|------|------|
| `/新会话 [名称]` | 创建特殊会话 (留空由 LLM 自动命名)；创建时快照当前生效人格，绑定到该会话 |
| `/切换会话 <名称>` | 切换到指定会话（自动切换到该会话绑定的人格，系统发提示消息） |
| `/会话列表` 或 `/会话` | 列出所有特殊会话（普通会话 / 剧情归档 / 角色创作分组，含消息数、绑定人格与封档状态；绑定失效显示「已失效→跟随默认」） |
| `/重命名会话 <旧名> <新名>` | 重命名会话 |
| `/删除会话 <名称>` | 删除指定会话（回复「确认删除 <名称>」二次确认，60 秒内有效；删除前会列出将被清理的关联文件） |
| `/压缩会话` | 将当前特殊会话早期历史浓缩为结构化摘要，仅保留最近 10 条原文（回复「确认压缩会话」二次确认，60 秒内有效；仅私聊；剧情归档/角色创作会话不支持；上下文达模型窗口 85% 时机器人会以人格口吻主动建议，95% 触发紧急截断保护） |
| `/保存为会话 [名称]` | 将当前临时对话保存为特殊会话（同样快照当前人格绑定到新会话） |
| `/结束会话` 或 `/退出特殊会话` 或 `/退出会话` 或 `/临时会话` | 退出特殊会话，回到临时模式（提示回落后的人格）；剧情中途退出保留进度、可 `/剧情 <同标题>` 恢复，已完结剧情退出即封档 |
| `/帮助` 或 `/help` 或 `/命令` | 显示完整系统命令列表 |

**人格切换**

| 命令 | 说明 |
|------|------|
| `/personality` 或 `/人格切换` | 查看当前人格、生效来源（剧情钉住/剧情归档/会话绑定/个人设置/群默认/全局默认）与可用人格列表 |
| `/personality <名称>` 或 `/人格切换 <名称>` | 切换人格（支持模糊匹配，如「露比」「Rubi」）；切换后会追问**保留历史**还是**清空上下文**。**特殊会话内只改本会话的绑定**（你的默认人格不变）；临时会话内则修改用户默认；剧情/角色创作会话内拒绝切换 |
| `/人格切换 <名称> --全局` | 逃生门：在特殊会话内修改**用户默认人格**（会话绑定不动，退出本会话后生效） |
| `/toggle personality <名称>` | **仅超级用户 + 群聊**：把该人格设为「本群默认人格」 |
| `/toggle personality 默认` | 清除本群默认人格，回落到全局默认 |

**角色养成（羁绊）**（仅 `split` 模式角色人格下生效；数据按 用户 × 角色 隔离）

| 命令 | 说明 |
|------|------|
| `/好感度`、`#好感度` 或 `/bond` | 查看等级/称号/心情/陪伴统计（零 token） |
| `/剧情` | 列出当前角色的剧情场景（Lv 解锁与完成状态） |
| `/剧情 <标题>` | 进入该剧情场景（**仅私聊**；**不占特殊会话配额**，`/结束会话` 中途退出保留进度、再发 `/剧情 <同标题>` 可恢复；走到结局自动封档、`/切换会话` 回看） |
| `/设好感 <1-10>` | 把**你与当前角色**的羁绊等级直接设为指定值（可升可降，只作用于本人） |
| `/创建角色` | 访谈式自创角色（姓名/性格/语气/外观/初始羁绊 Lv1–5）；**不占特殊会话配额**，中途退出后再发 `/创建角色` 可恢复访谈，创建成功后该会话自动删除 |
| `/删除角色 [名称]` | 不带名称列出你的自创角色；带名称删除（回复「确认删除角色 <名称>」二次确认）；删除时解除用户默认/群默认对该角色的引用（提示「已解除 N 处」），会话内绑定失效自动回落，好感数据保留 |
| `/设形象 [角色名]` | 把图片设为自创角色的参考立绘（`/形象` 查看、`/清除形象` 撤销；内置人格仅管理员可改） |
| `/邮件` | 查看角色信箱（**仅官方单聊**）：不带参数列出信件（分页，`●` 为未读，系统邮件带【系统】前缀）、`下一页`/`p2` 翻页、`MM-DD` 按日期查看、信件编号看全文并标记已读；面板删除过的信件显示「已被撤回」（零 token，不带免责尾） |

**群聊功能管理**（仅超级用户，仅群聊）

| 命令 | 说明 |
|------|------|
| `/toggle` | 查看当前群聊功能开关状态 |
| `/toggle <功能> <on/off>` | 开关功能（gacha / image / voice） |

**游戏工具**（直接返回，零 token，不经智能体）

| 命令 | 说明 |
|------|------|
| `/兑换码` 或 `/redeem-code` | 查询当前有效游戏兑换码（含 7 天内刚过期；自动刷新，逐条单发方便复制） |
| `/长期兑换码` | 查询长期有效（无过期时间）的兑换码 |
| `/角色详情 <名称>` | 查询角色详情，渲染卡片图片返回（如 `/角色详情 夏妮`） |
| `/羁绊详情 <名称>` | 查询羁绊详情，渲染卡片图片返回（如 `/羁绊详情 驰骋的快感`） |
| `/刷新角色数据` | **仅管理员**：强制后台刷新角色/羁绊数据库（抓取+翻译+图片下载+卡片重渲染） |

**连续对话**

| 命令 | 说明 |
|------|------|
| `/取消` 或 `#取消` 或 `/结束` 或 `#结束` | 退出群聊连续对话模式（四个写法等价） |

**其他**

| 命令 | 说明 |
|------|------|
| `/clear` 或 `清除上下文` 或 `新对话` | 清除临时会话上下文（三个写法等价） |
| `/status` | 查看 Agent 运行状态（活跃会话、已注册工具列表） |
| `/功能` 或 `/features` | 渲染 `FEATURES.md` 为功能卡片图片返回（零 token） |
| `/更新日志 [d]` 或 `/update record [d]` | 查看机器人最近 d 条更新记录（默认 3，最多 20）；也可自然语言询问「最近有什么更新」（落入 `get_changelog` 工具） |
| `/管理工作区` | 触发智能体调用 `get_user_info` 展示工作区快照并引导清理（落入 Agent 处理，非拦截命令） |
| `/免责声明` | 查看免责尾状态；`/免责声明 关` 关闭对话回复末尾的免责声明追加（所有用户可用，按渠道独立记录；主动信件本就不带免责尾） |

**反馈 & Bug 报告**（零 token 消耗，直达开发者）

| 命令 | 说明 |
|------|------|
| `#反馈 <内容>` | 提交使用反馈，自动附用户上下文 |
| `#bug <描述>` | 提交 Bug 报告，自动附用户上下文 |
| `#建议 <内容>` | 提交改进建议，自动附用户上下文 |

## 启动脚本

| 脚本 | 用途 |
|------|------|
| `setup.sh` | 一键安装所有依赖（系统库 + Python 环境 + SearXNG） |
| `start.sh` | 启动所有服务（SearXNG + NoneBot + NapCat 检查） |
| `stop.sh` | 停止所有服务（NoneBot + SearXNG），并同步 WebUI 看门狗状态 |
| `start_bot.sh` | 仅启动 NoneBot（SearXNG & NapCat 已运行时使用） |
| `start_webui.sh` | Web 管理面板 启动/停止/重启/状态（`start\|stop\|restart\|status`，默认绑定 `127.0.0.1:8090`，经 SSH 隧道访问） |
| `test.sh` | 运行测试套件：`test_agent.py` + `test_workspace.py` + `test_story_kb.py` + 记忆 benchmark + `test/` 下 11 个离线测试脚本 |

> ⚠️ **测试脚本说明**：`test.sh` 调用的 11 个 `test/*.py` 离线测试位于仓库根 `test/` 目录，该目录被 `.gitignore` 忽略（`.gitignore` 中 `/test/`），仅存在于开发机、不随仓库分发。clone 后直接 `bash test.sh` 会因缺少这些文件而报错；CI/新环境请先确认 `test/` 是否存在，或仅运行 `cd QQBot && python test_agent.py`。

## 目录结构

```
QQBotAgent/
├── setup.sh / start.sh / stop.sh / start_bot.sh   # 安装 / 启动 / 停止 / 仅启动 NoneBot
├── start_webui.sh              # Web 管理面板 启动|停止|重启|状态（127.0.0.1:8090）
├── test.sh                     # 测试调度：3 个主测试 + 记忆 benchmark + 11 个离线脚本
├── bot.py                      # NoneBot 备用入口（仅注册 OneBot 适配器；生产用 `cd QQBot && nb run`）
├── main.py                     # PyCharm 占位入口（非实际入口）
├── docker-compose.yml          # Docker 编排（searxng + qqbot 两服务）
├── Dockerfile / docker-entrypoint.sh
├── vllm-start.sh               # 手动拉起 vLLM 推理服务（可选，非 compose 服务）
├── napcat.sh                   # NapCat 安装脚本（Rootless / Docker）
├── deploy_public_site.sh       # 公开站点发布脚本
├── searxng/settings.yml        # SearXNG 搜索引擎配置（国内可用引擎）
│
├── webui/                      # ★ Web 管理面板（FastAPI 独立进程）
│   ├── main.py                 #   入口（74 个 /api/ 端点 + 16 个页面路由 + 2 个 WebSocket）
│   ├── auth.py                 #   登录鉴权（scrypt 口令哈希 + 内存会话）
│   ├── process_manager.py      #   启停 bot / 看门狗状态持久化
│   ├── config_editor.py        #   提示词 / 人格 / .env / JSON 配置热编辑（密钥脱敏 + 原子写）
│   ├── model_probe.py          #   模型 API 可用性探测（最小真实调用 + 分级判定）
│   ├── data_reader.py          #   记忆/画像/会话/Wiki 别名/兑换码/更新记录 数据读写
│   ├── log_viewer.py / audit.py / hardware_monitor.py / playground.py / napcat_login.py
│   ├── official_status.py      #   QQ 官方路由状态（只读 official_status.json，mtime 判新鲜度）
│   ├── config.py               #   面板配置（从 QQBot/.env 读 USER_DATA_ROOT 等）
│   ├── templates/ static/      #   19 个页面模板与静态资源
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
    ├── requirements.txt / pyproject.toml
    ├── config.yml              # 历史遗留的 go-cqhttp 配置（与 NoneBot 无关，可忽略）
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
    │   │                       #   硬件检测（bootstrap 时注入约束）/ 工作区配额 / 目录快照 / 清理协议
    │   ├── task_record.py / search_archive.py / group_features.py
    │   └── config/             #   SOUL/IDENTITY/AGENTS/MEMORY（注入）+ TOOLS/BOOTSTRAP/SESSION（加载）
    │                           #   + HELP/FEATURES（命令时读取）+ personalities/ + portraits/
    │
    ├── adapters/               # 平台入口适配器
    │   ├── onebot.py           #   OneBot V11：事件→InboundMessage、附件下载、OneBotSender
    │   ├── qq_official.py      #   QQ 官方：build_view、附件解析、QQOfficialSender（缓冲+合并+配额+流式能力位）、主动消息（含 markdown+按钮信封阶梯降级）
    │   ├── qq_official_buttons.py  # 官方键盘 wire 格式 + 回调编解码 + 交互回执发送器 + 点击遥测
    │   └── qq_official_stream.py   # 官方 c2c 流式消息：MessageStreamer 双流传输 + 熔断器 + stream_snapshot
    │
    ├── plugins/                # NoneBot 插件
    │   ├── agent_router.py     #   OneBot 入口 + 配置看门狗 + 更新记录/公告双渠道群发 + 群列表导出（OneBot+官方）
    │   ├── qq_official.py      #   官方入口 + 每日主动循环（角色信箱信封 + 主动发媒体，双向互斥）+ 拒收/恢复事件 + 状态心跳 + 邮件管理队列 worker（面板删信/系统邮件）
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
        ├── personality_config.json / personality_settings.json / group_personality.json
        ├── official_status.json #  官方路由状态（面板仪表盘读取；按需生成）
        ├── task_log/           #   子任务结构化日志（按需创建）
        ├── search_cache/       #   搜索存档（按需创建，7 天 TTL + 每用户 50 份）
        ├── mailboxes/          #   角色信箱（每用户一 JSON；bot 单写、面板只读）
        ├── mail_admin/         #   面板邮件管理队列（queue/processing/status；面板写、bot 认领执行回写）
        ├── bond_greet_targets.json  # c2c 联系人登记表（每个私聊回合落库；面板用户列表并集来源）
        └── changelog/          #   更新记录 + 公告 + 群发/群列表文件队列（按需创建）
```

> 说明：`data/` 下多数目录在首次使用时才创建；生产环境建议把 `USER_DATA_ROOT` 与 `CHARACTERS_DIR`/`BONDS_DIR`/`MAILBOX_DIR`/`MEMORY_DIR`/`ASSETS_DIR`/`STORY_KB_DIR` 一起指向数据盘（见 `.env.example`），便于整盘备份与迁移。

## 核心架构

### Agent 主循环

```
QQ 消息
  ├─ OneBot 渠道（QQ 号）      adapters/onebot.py      ─┐
  └─ 官方渠道（openid）        adapters/qq_official.py  ─┤
                                                         ▼
                                   agent/turn.py（平台无关内核）
   ├── 平台化身份：_current_platform → PermissionManager（SUPERUSERS/VIP_USERS 或 QQ_OFFICIAL_*）
   ├── 直连命令（零 token）：会话 / 人格 / 养成 / 游戏 / 群管理 / 反馈 …
   └── 自然语言 → run_agent_turn
         ├── ModelRouter.classify_complexity() → "simple"(FLASH) / "complex"(REASONING)
         ├── 构建消息：L1–L3 系统提示词 + 历史 + L4 易变尾（时间/画像/MEDIUM 记忆/剧情块）
         └── Think→Act→Observe→Respond 循环（最多 20 轮）
               ├── think：LLM 决定是否调用工具
               ├── act：ToolRegistry.execute()（权限已在 schema 与执行层双层过滤）
               ├── observe：工具结果回灌对话
               └── respond：无工具调用时产出最终回复
                     └── Sender 出站（文本/图片/语音/进度；官方渠道缓冲合并 + 被动回复配额）
```

**关键参数**：`max_tool_iterations=20`、`thinking_timeout=180s`、单次处理总超时 `300s`、临时会话 `30min` 过期、群聊连续对话窗口 `90s`。

### 双协议入口与出站契约

| 层 | 入口适配器 | 内核 |
|---|---|---|
| 入站 | `adapters/onebot.py` / `adapters/qq_official.py`：把平台事件转成 `agent/inbound.py::InboundMessage`（统一 `user_id / is_group / group_id / text / message_id / reply_context / resolver`） | `agent/turn.py` 只读视图字段，不碰平台对象 |
| 出站 | `OneBotSender` / `QQOfficialSender` 实现 `agent/outbound.py::Sender`（`send_text / send_image / send_audio / send_progress`） | 内核用 `_safe_send()` 表达语义，仅对 `SendFailed` 重试 |
| 差异 | 官方渠道无「读本地文件」能力（图片/语音走 base64 上传）；被动回复配额 单聊 4 次/60min、群 5 次/5min，超出直接丢弃并告警；「正在思考…」是唯一值得即时占用配额的进度行 | — |

### 群聊连续对话

```
@Roxy "帮我分析数据"   → agent_router (to_me) → 自动开启 90 秒窗口
"这个字段是什么" (无@)  → continuous_router       → Agent 处理 + 续期
"/取消" (无@)          → continuous_router       → 关闭窗口
90 秒无消息            → 自动过期清理
```

### 多模型路由

参考 Claude Code 的模型分层策略：

```
用户消息
  │
  ├── FLASH_MODEL 分类: "simple" 或 "complex"
  │
  ├── simple → FLASH_MODEL 直接回复（低成本）
  └── complex → REASONING_MODEL + 工具调用（强力推理）
  │
  ├── 图片 → MULTIMODAL_MODEL（read_file 懒分析）或主模型直连识图（supports_vision）
  ├── 音频 → AUDIO_MODEL（转写 + 情绪 + 声线 + 背景）
  ├── 战斗截图 → OCR_MODEL
  └── 画图 / 语音 → IMAGE_GEN_MODEL / TTS_MODEL（DashScope 原生接口）
```

配置在 `QQBot/config/models_settings.json`（7 段模型 + `task_routing`），留空自动回退到 `.env` 默认。也可在 Web 面板「配置热管理 → 模型」用结构化表单编辑：保存前先对改动过的段做最小真实调用探测（`POST {api_base}/chat/completions`，max_tokens=8；401/403→密钥无效，连接失败→不可达，AUDIO/OCR 段对 400/422 宽判为「凭据有效但模型不接受纯文本探测」），仅验证通过的配置才写盘；写盘后 bot 侧 watcher（5s 轮询 mtime）触发 `ModelRouter.reload()` / `MultimodalClient.reload()` 并重挂固化引用，约 10 秒生效，无需重启。

### 双平面路由（角色人格）

角色人格（`split` 模式：露比、自创角色等）下，一次对话会被分到两个平面：

```
用户消息
  ├── 角色面（social）   —— 只有社交工具（delegate_task / give_gift / create_image / send_voice / recall_asset）
  │     └── 角色自己以人设回应；要搜索/算数/读文件时调用 delegate_task
  └── 任务面（task）     —— delegate_task 以 Roxy 中立人格、隔离上下文（persist=False）运行，
                            按用户角色过滤工具、单层嵌套防递归；结果由 revoice_task_result 用角色口吻转述
```

- 目的：把「人设表达」与「工具执行」解耦，避免角色卡的安全约束与工具输出要求互相打架；
- `unified` 人格（助手 Roxy）不拆分，直接走任务面；
- 委派深度由 `_delegate_depth` 守卫，禁止递归委派。

### 角色养成与影音

- **好感度（Bond）**：10 级（陌生 → 灵魂伴侣），只涨不掉；心情值 0–100 围绕基线回落，影响加经验系数与提示词语气；陪伴统计含相识天数/消息数/礼物数。
- **互动动作**：送礼（喜爱/普通/讨厌三档，每日上限）、戳一戳（QQ 双击头像，每日上限）、称呼演化、按等级解锁角色档案。
- **剧情场景**：`/剧情 <标题>` 进入一对一演绎（仅私聊，**不占特殊会话配额**），分幕推进，收尾结算经验/心情、发「羁绊纪念卡」并**自动封档**（会话保留、可 `/切换会话` 回看；同标题再进为恢复而非新建）。
- **久别重逢 / 纪念日**：≥14 天未聊触发重逢语气；相识 30/100/365 天等里程碑提一句。
- **画图与语音**：Lv4 起可要图（`create_image`，支持参考立绘做图生图）、Lv5 起可要语音（`send_voice`，角色声纹）；每日配额按角色：admin 50 / vip 10 / regular 3；资产磁盘配额 1GB / 300MB / 100MB。
- **影音记忆**：每次生成/上传落「资产卡」（markdown + frontmatter 文本化本体），二进制可被 LRU 淘汰；`recall_asset` 支持「再发一次那张图」式召回，久远资产保留文字记忆。

### 原作剧情知识库（Story KB）

- 离线构建：`python scripts/build_story_kb.py` → `data/story_kb/story.db`（当前约 11.7 MB，约 5 万句对白）；
- 检索：SQLite FTS5（中文 bigram + 英文 unicode61）+ BM25 → RRF 融合 → 场景/角色加权重排，无需 embedding 依赖；
- 注入：角色扮演时按**人格白名单**（`config/story_personas.json`）把相关剧情块注入 L4 尾层（不进缓存前缀）；非白名单人格不注入；
- 主动检索：`story_search` 工具（schema 层按人格裁剪），供「第一次方舟战争发生了什么」这类问题；
- 测试：`test_story_kb.py`（37 个用例，无网络）。

## 核心类

### Agent (`agent/agent.py`)

```python
class Agent:
    # 构造（除前三项外均可选）
    def __init__(self, deepseek_client, tool_registry, config_dir,
                 session_manager=None, memory_system=None, tiered_memory=None,
                 profile_manager=None, hardware_detector=None,
                 workspace_manager=None, special_session_manager=None,
                 asset_memory=None,
                 max_tool_iterations=20, thinking_timeout=180.0)

    # 核心方法
    async def run(self, user_message, user_id, client=None,
                  progress_callback=None, session_type="temporary",
                  allowed_tools=None, user_role=None,
                  images=None) -> str
    def build_system_prompt(self) -> str    # SOUL + IDENTITY + AGENTS + MEMORY + 硬件信息
    def reload_configs(self)                # 清缓存并重读配置（配置看门狗调用）
    def get_status(self) -> dict            # 活跃会话数 + 全部已注册工具名
    def clear_user_session(self, user_id)   # 清除临时会话
```

- `client` 支持运行时模型切换（ModelRouter 传入不同 `DeepSeekClient`）
- `images` 仅在所选模型段开启 `supports_vision` 时由 `turn.py` 传入（Direct Vision：本轮图片以 `image_url` part 直接进主模型）
- `run()` 返回最终回复文本，内部管理 Session / Memory / Profile / 资产记忆注入；异常统一走 `lib/errors.py` 诊断编号

### DeepSeekClient (`lib/deepseek_client.py`)

```python
class DeepSeekClient:
    def __init__(self, api_key=None, api_base=None, model=None)
    async def chat_completion(self, message, history, timeout=180.0) -> str
    async def chat_completion_with_tools(self, messages, tools, timeout=180.0) -> dict
```

可选参数支持多实例（不同模型不同端点），用于 ModelRouter；`reasoning_content` 全链路保留（thinking mode 模型要求原样回传）。

### ModelRouter (`lib/model_router.py`)

```python
class ModelRouter:
    async def classify_complexity(self, user_message) -> str   # "simple" | "complex"
    def get_client(self, task_type) -> DeepSeekClient           # triage/simple/complex/multimodal
    @property reasoning_client / flash_client / multimodal_client
    def reload(self)                                            # 重读 models_settings.json 并原地重建 client（配置看门狗调用）
```

> 图像生成与 TTS 不走 ModelRouter：`lib/media_gen.py` 每次调用现读 `models_settings.json` 的 `IMAGE_GEN_MODEL` / `TTS_MODEL` 段，天然跟随面板热重载。

### ToolRegistry (`agent/tool_registry.py`)

```python
class ToolRegistry:
    def register(self, name, func, description, parameters)    # 注册工具
    def get_schemas_for(allowed_names) -> list                 # 按权限过滤的 schema
    async def execute(self, name, arguments) -> str            # 执行工具
```

### ContinuousSessionManager (`agent/continuous_session.py`)

```python
class ContinuousSessionManager:
    def __init__(self, timeout_minutes=1.5)   # 90 秒；turn.py 以 1.5 实例化
    def start(self, group_id, user_id)                         # 开启窗口
    def is_active(self, group_id, user_id) -> bool             # 检查 + 自动清理
    def touch(self, group_id, user_id)                         # 续期
    def end(self, group_id, user_id)                           # 手动关闭
```

### Session / Memory / Profile

| 类 | 文件 | 功能 |
|---|---|---|
| `Session` | `session.py` | per-user 对话上下文（最多 20 条 + 4 条迟滞缓冲，减少修剪对前缀缓存的破坏），持久化到 `data/sessions/`；缓存命中时按文件 `st_mtime_ns` 重校验——面板「清除临时会话」等外部删改优先于内存缓存，不会被陈旧写回复活 |
| `MemorySystem` | `memory.py` | 旧版长期记忆（Markdown + frontmatter），`search()` 按关键词子串匹配返回**全部**命中；user 类记忆自 P1 起由 `TieredMemory` 接管，本类仍服务 knowledge/system 类与面板编辑（无内存缓存，每次直读盘） |
| `TieredMemory` | `memory.py` | 三层记忆引擎：SHORT（容量 50，不注入，count≥3 晋升）/ MEDIUM（容量 60，注入 top 15 条 ≤600 字符，count>10 晋升 LONG，age>30 降级回 SHORT）/ LONG（长期对象，索引行注入 L4 尾部，`recall_memory` 按 id 取回快照）；schema v2 引入 `category` 与 `last_confirmed_ts`（写入时类型化 + 时效标注）；每用户 `extraction_count` 逻辑时钟驱动 age；存储 `data/memory/tiers/{uid}.json`（原子写）+ `tiers/long/{uid}/{id}.md` |
| `ProfileManager` | `profile.py` | 用户画像（昵称/兴趣/偏好槽位；`facts` P0 起休眠）。Layer 1 LLM 提取（石蕊测试 + few-shot 反例）+ Layer 2 `fact_filter` 确定性过滤 + Layer 3 批量调度（`observe_turn` 每 5 轮一次合并 extract+judge）；持久化 `data/users/`；`get()` 命中缓存按 mtime 重校验 |

### 其他核心类（养成 / 多模态 / 官方渠道）

| 类 | 文件 | 功能 |
|---|---|---|
| `BondManager` | `agent/bond.py` | 好感度（EXP→10 级，只涨不掉）+ 心情（0–100，向基线回归）+ 陪伴统计；送礼/戳一戳/称呼演化/档案解锁/剧情场景/重逢/纪念日；每日媒体配额与资产配额；每用户一 JSON（`data/bonds/`），缓存按 mtime 校验 |
| `BondContent` | `agent/bond_content.py` | 角色卡解析：外观锚点、称呼演化、档案分层、剧情场景定义 |
| `CharacterRegistry` | `agent/character_registry.py` | 内置人格 + UGC 角色叠加层：UGC 落 `data/characters/{id}/card.md + meta.json`，按作者可见性隔离；解析 `split`/`unified` 模式、显示名、声纹。单点人格解析链 **剧情钉住 > 会话绑定 > 个人设置 > 群默认 > 全局默认**（活跃特殊会话经 `set_session_provider` 注入，绑定 key 失效懒校验穿透），`resolve_effective_source` 返回来源标签（含已封档剧情的「剧情归档」） |
| `PersonalityManager` | `agent/personality.py` | 内置人格目录 + 用户级/群级绑定 + 模糊匹配 + 切换标记（CharacterRegistry 的基类） |
| `AssetMemory` | `agent/asset_memory.py` | 资产卡（markdown+frontmatter 为本体，二进制可淘汰）：建卡/检索/召回计数/prompt_hash 去重/事件蒸馏/LRU 淘汰/审计 |
| `Delegation` | `agent/delegation.py` | 双平面路由执行核心：`SOCIAL_TOOLS` 白名单、`run_delegated_task`（隔离上下文 + 权限过滤 + 单层嵌套）、`revoice_task_result`（角色口吻转述） |
| `GreetingState` | `agent/greeting.py` | 官方渠道主动消息簿记：候选索引（仅 c2c 入站）+ 日/月配额 + 冷却 + 概率 + 批量上限（纯函数可单测） |
| `MailboxManager` | `agent/mailbox.py` | 角色信箱：每用户一 JSON（`data/mailboxes/`），信件/摘要落库、幂等 digest、28 天 + 200 封裁剪、预览/全角转义纯函数 |
| `GroupRegistry` | `agent/group_registry.py` | 官方群登记表（`data/group_registry.json`）：GROUP_AT 入站 upsert（60s 节流）、left/unknown 状态机、active_enabled 开关（v1 纯记账） |
| `StoryKB` | `lib/story_kb.py` | 原作剧情检索：SQLite FTS5（中文 bigram / 英文 unicode61）+ BM25 → RRF → 场景/角色加权重排 |
| `QQOfficialSender` | `adapters/qq_official.py` | 官方渠道 Sender：缓冲 + 合并 + 配额控制、图片/语音 base64 上传、语音被拒降级文件卡、`/正在思考/` 即时进度 |
| `TokenLedger` | `lib/token_ledger.py` | Token 用量记账（按日聚合，WebUI 看板数据源） |

## 已注册工具（40 个）

> 注册位置：`agent/turn.py`（`_build_tool_registry()` 33 个 + 模块级 7 个）；权限集合定义在 `agent/permissions.py`。

**信息与通用（public）**

| 工具 | 说明 |
|------|------|
| `search_web` | SearXNG 聚合搜索（结果自动存档，返回 `[存档] id`） |
| `get_time` | 当前日期时间 |
| `read_file` | 读取工作区文件（文本 / PDF / 图片分析 / 音频分析，支持分段 offset） |
| `summarize_pdf` | PDF 提取 + 总结 |
| `delete_workspace_file` | 删除工作区文件/空目录（释放磁盘） |
| `get_user_info` | 用户信息快照（权限/会话/工作区/工具范围） |
| `recall_search_result` | 按 id 取回本人数日内的搜索/抓取存档 |
| `get_changelog` | 查询机器人更新记录（与 `/更新日志` 共用 `tools/changelog.py`） |

**地图与天气（public）**：`geocode`、`reverse_geocode`、`get_weather`、`search_poi`、`plan_route`

**游戏工具（public）**：`gacha_pull`、`play_gacha_animation`、`calculate_speed`、`compare_speed_probability`、`parse_battle_screenshots`、`character_detail`、`bond_detail`、`redeem_code`

**文本工具（public）**：`explain_code`、`translate_text`

**会员工具（vip，4 个）**：`web_fetch`、`download_repo`、`get_system_load`、`execute_code`

**管理员工具（admin，1 个）**：`shell_exec`

**记忆与编排（public）**：`begin_task`、`finalize_subtask`、`recall_memory`、`story_search`

**养成 / 多模态（动态下发，8 个）**：`delegate_task`、`give_gift`、`create_image`、`send_voice`、`recall_asset`、`complete_scene`、`create_character`、`end_continuous_mode`

> 📝 **权限分布**：静态工具 32 个 = 公共 27（`_PUBLIC_TOOLS`）+ 会员 4（`_VIP_TOOLS`）+ 管理员 1（`_ADMIN_TOOLS`）；角色可用数 admin 32 / vip 31 / regular 27。
> 另外 8 个工具**不在任何权限集合**中，只经路由层的 `allowed_tools` 动态下发：`SOCIAL_TOOLS`（角色面：`delegate_task`/`give_gift`/`create_image`/`send_voice`/`recall_asset`）、仅创作会话的 `create_character`、仅剧情会话的 `complete_scene`、仅连续对话的 `end_continuous_mode`；越权调用由执行层硬拒绝。
> `story_search` 虽属 public，但只对剧情白名单人格出现在 schema 中（非白名单人格看不到、也用不了）。

## 智能体配置

所有配置文件在 `QQBot/agent/config/`。按代码实际用途分为四类：

**① 注入系统提示词（`build_system_prompt()` 拼接，共 4 个）**

| 文件 | 用途 |
|------|------|
| `SOUL.md` | 人格定义（Roxy）& 行为规则 |
| `IDENTITY.md` | 身份声明 & 技术栈 & 能力 |
| `AGENTS.md` | 编排规则 & 工具选择 & 连续对话模式 |
| `MEMORY.md` | 三层记忆引擎规则（P1 起接入提示词，`agent.py:156-158`） |

**② 加载但不注入提示词（`_load_configs()` 读入 `_configs`，仅作引用/文档，共 3 个）**

| 文件 | 用途 |
|------|------|
| `TOOLS.md` | 全部工具的参数文档（供查阅，不进提示词） |
| `BOOTSTRAP.md` | 启动序列说明 |
| `SESSION.md` | 会话参数说明 |

**③ 由 `agent_router` 在命令处理时读取（共 2 个）**

| 文件 | 用途 |
|------|------|
| `HELP.md` | `/帮助` 命令输出内容 |
| `FEATURES.md` | `/功能` 命令输出内容 |

**④ 未被代码加载（文档遗留 / 已失效）**

| 文件 | 说明 |
|------|------|
| `WORKSPACE.md` | 早期安全/资源约束文档；其硬件约束已由 `HardwareDetector` 取代，文件本身不再加载 |
| `USER.md` / `HEARTBEAT.md` | 代码中无引用，为历史遗留文件 |
| `personalities/` | 人格定义目录（assistant / roxy_character / rubi），由 `personality.py` 按需加载；`mode: split` 的角色另有任务面语音等元数据 |
| `portraits/` | 内置人格的出厂参考立绘（`roxy_character.png` / `rubi.png` + README），供图生图与 `/形象` 使用 |

> 📝 **热重载实况**：`plugins/agent_router.py` 的配置看门狗每 5 秒轮询三组文件的 mtime（各自独立 5 秒冷却）：
> 1. **`agent/config/*.md`（含 `personalities/`）** → `reload_configs()`。它只重新读取 ① ② 类共 7 个文件并重建提示词，因此**只有修改 ① 类（SOUL/IDENTITY/AGENTS/MEMORY）才会即时改变 Agent 行为**；修改 ③ 类（HELP/FEATURES）在下次触发对应命令时生效；修改 ④ 类无效。
> 2. **Wiki 别名字典（`config/characters/character_dic.json` / `bonds_search_dic.json`）** → `NameResolver.reload()` + 详情缓存失效。面板「Wiki 缓存」页可直接编辑别名，保存后 bot 侧自动跟随。
> 3. **`config/models_settings.json`** → `model_router.reload()`（含模块级单例与 `MultimodalClient`）并重挂固化 client 引用（`agent.client` / `_profile_manager` / `_special_sessions`）。面板保存模型配置后约 10 秒内生效。
>
> 配合 WebUI 面板的配置热编辑，以上修改均无需重启 bot。

> 📝 **硬件上下文说明**：`agent/hardware.py` 的检测在 `Agent.bootstrap()` 中触发并把硬件约束并入 L1；当前入口（`turn.py`）未调用 `bootstrap()`，所以该段默认不注入。需要按机型给出「高负载任务拒绝」话术时，在启动处调用一次 `agent.bootstrap()` 即可。

## 添加新工具

在 `QQBot/tools/` 下创建工具函数，然后在 `agent/turn.py` 的 `_build_tool_registry()` 中注册：

```python
async def my_tool(param: str) -> str:
    # 工具逻辑
    return result

# 在 agent/turn.py 的 _build_tool_registry() 中:
registry.register(
    "my_tool", my_tool,
    "工具描述（中文）",
    {
        "type": "object",
        "properties": {
            "param": {"type": "string", "description": "参数描述"},
        },
        "required": ["param"],
    },
)
```

同时：
1. 到 `agent/permissions.py` 把工具名加入 `_PUBLIC_TOOLS` / `_VIP_TOOLS` / `_ADMIN_TOOLS`（否则 schema 过滤阶段就看不到它）；
2. 更新 `agent/config/TOOLS.md` 补充工具文档；
3. 若工具只在特定上下文可用（角色面/创作/剧情/连续对话），不要加权限集合，改为在 `turn.py` 的 `allowed_tools` 里按场景下发（参考 `SOCIAL_TOOLS`）。

## 测试

```bash
bash test.sh
# 或
cd QQBot && python test_agent.py
```

`test_agent.py` 共 **53 个测试套件 / 316 个用例**（全部离线，mock client，无网络），`test_workspace.py` 另有 11 个类，`test_story_kb.py` 6 个类 / 37 个用例：

| 分组 | 套件 |
|---|---|
| 基础架构（15） | `TestToolRegistry`、`TestSessionManager`、`TestMemorySystem`、`TestAgentCore`、`TestUserProfile`、`TestProfileExtraction`、`TestDeepSeekClientParsing`、`TestBuiltinTools`、`TestSearchArchive`、`TestPersonality`、`TestTokenLedger`、`TestCacheStability`、`TestTieredMemory`、`TestMergedExtraction`、`TestMemoryMigration` |
| 养成与角色 / 会话人格（15） | `TestBondManager`、`TestBondContent`、`TestBondGiftPoke`、`TestBondEvents`、`TestBondScene`、`TestSceneSession`、`TestSceneArchive`、`TestBondReunionAnniversary`、`TestInitialBond`、`TestCharacterRegistry`、`TestSessionPersonaBinding`、`TestCreateCharacterTool`、`TestCharCreationInterview`、`TestPersonalityMeta`、`TestReferencePortrait` |
| 渠道 / 多模态 / 面板（16） | `TestQQOfficialQuoteParsing`、`TestOfficialMarkdown`、`TestOfficialStatusHeartbeat`、`TestWebuiOfficialStatus`、`TestMediaGen`、`TestProactiveMedia`、`TestMediaQuota`、`TestAnchorVoiceId`、`TestDirectVision`、`TestDelegation`、`TestAgentPersist`、`TestActiveGreeting`、`TestModelRouterClassify`、`TestPersonalityMarkerPersistence`、`TestWebuiPermissionsEditor`、`TestWebuiModelSections` |
| 资产记忆 / 剧情（7） | `TestAssetCardsP4`、`TestAssetSearch`、`TestAssetRecall`、`TestUploadTextualization`、`TestEpisodeDistill`、`TestAssetEvictionInjection`、`TestStoryKbInjectionGate` |

`test.sh` 还会跑 `benchmarks/memory/bench_engine.py`（确定性场景流的记忆引擎 benchmark）与 `test/` 下 11 个离线脚本（测速管线、别名、卡片渲染、兑换码、web_fetch 等）。

## 安全模型

| 边界 | 规则 |
|------|------|
| 权限系统 | 三层角色（管理员/会员/普通），按平台选身份表（`SUPERUSERS`/`VIP_USERS` 或 `QQ_OFFICIAL_SUPERUSERS`/`QQ_OFFICIAL_VIP_USERS`），schema 过滤 + 执行层硬拦截纵深防御 |
| 代码执行 | `python3 -I` 隔离 + 正则匹配 + AST 白名单三层过滤，分级限制（管理员 60s/16KB/256MB，会员 15s/16KB/128MB） |
| 文件访问 | 只在用户工作区内，拒绝路径遍历（`..`, `~`, `/etc/`）；配额 80% 预警、超限进入清理协议 |
| 网络 | 仅通过预定义工具（SearXNG、`web_fetch` 内置 SSRF 防护 + 可选浏览器兜底、`download_repo` 仅 HTTPS） |
| 生成内容 | 画图/语音台词先过 LLM 内容审查；TTS 有字符硬顶与计费上限；资产按角色配额与 LRU 淘汰 |
| 隐私 | per-user 隔离，本地存储，不上传第三方（除模型 API 调用），工具调用审计 JSONL |
| 双渠道身份 | 官方渠道用不透明 openid 与独立权限表，不会与 QQ 号身份串号；私密数据（会话/好感/角色）只在私聊渲染 |

### 权限系统

通过环境变量配置用户层级；官方渠道读另一套 openid 名单：

| 角色 | 配置方式 | 权限范围 |
|------|---------|---------|
| **管理员** | `SUPERUSERS` / `QQ_OFFICIAL_SUPERUSERS` | 32 个静态工具（含 `shell_exec`），`execute_code` 60s/16KB/256MB，2GB 工作区，10 个特殊会话，10 个自创角色，每日画图/语音各 50，影音资产 1GB |
| **会员** | `VIP_USERS` / `QQ_OFFICIAL_VIP_USERS` | 31 个（含 `web_fetch`、`download_repo`、受限 `execute_code`），500MB 工作区，3 个特殊会话，3 个自创角色，每日画图/语音各 10，影音资产 300MB |
| **普通用户** | 默认 | 27 个基础工具（搜索/天气/地图/文件/娱乐/养成查询），100MB 工作区，1 个特殊会话，1 个自创角色，每日画图/语音各 3，影音资产 100MB |

工具权限通过请求时 **schema 过滤** 控制（LLM 只看到允许调用的工具），`_execute_tool_calls()` 中的硬拦截作为纵深防御；角色面/创作/剧情/连续对话等场景的工具另经 `allowed_tools` 动态下发（不进入上面的静态集合）。

详见 `QQBot/agent/config/WORKSPACE.md`、`AGENTS.md` 与 `QQBot/agent/permissions.py`。

## 安装 NapCat（详细）

```bash
bash napcat.sh --docker n     # Rootless Shell 安装（推荐）
bash napcat.sh --docker y     # Docker 安装
```

安装后：
1. 在 NapCat WebUI 登录 QQ
2. 配置反向 WebSocket：`ws://127.0.0.1:8081/onebot/v11/ws`
3. 设置 Access Token 与 `.env` 一致

## Docker 部署

```bash
# 启动 SearXNG + QQBot 两个服务
docker compose up -d

# 仅 SearXNG
docker compose up -d searxng
```

Docker Compose 包含两个服务：
- `searxng` — 搜索引擎（容器 8080 → 宿主 8082）
- `qqbot` — NapCat + NoneBot 主服务（8080/8081/6099 等端口）

> vLLM 不是 compose 服务：`vllm-start.sh` 只是手动拉起 vLLM 推理服务的脚本（默认 8000 端口），需要本地推理时再单独执行。

## 常见问题

### Docker 安装失败（国内云服务器）

**现象**：在腾讯云、阿里云等国内云服务器上执行 `bash napcat.sh --docker y` 或 `get-docker.sh` 时，出现 `Connection reset by peer` 或下载超时。

**原因**：Docker 官方安装脚本从 `download.docker.com`（AWS/Akamai 海外 CDN）下载 GPG 密钥和软件包，国内云服务器的网络出口受 GFW 限制，TCP 连接会被重置。

**解决方法（任选一种）**：

方法一：直接使用系统包管理器安装（推荐）
```bash
# Ubuntu/Debian（腾讯云 apt 镜像已包含 Docker）
sudo apt-get install -y docker.io docker-compose-v2
sudo systemctl enable docker --now
```

方法二：使用 Aliyun 镜像安装
```bash
sudo curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh --mirror Aliyun
```

方法三：手动配置 Docker 镜像源
```bash
# 先通过 apt 安装 docker，再配置国内镜像加速
sudo apt-get install -y docker.io
sudo tee /etc/docker/daemon.json <<EOF
{
  "registry-mirrors": [
    "https://docker.1ms.run",
    "https://docker.xuanyuan.me"
  ]
}
EOF
sudo systemctl restart docker
```

> **注意**：`napcat.sh` 对 Docker Hub 镜像拉取（`docker pull`）已内置代理测速（`docker.1ms.run` 等），但 Docker 本身的安装步骤（`get-docker.sh`）未走代理。如果遇到安装失败，先用上述方法手动安装 Docker，再运行 `napcat.sh`。

---

### NapCat 反向 WebSocket 403 错误

**现象**：
- NoneBot 日志：`WebSocket /` 返回 403
- NapCat 报错：`ws://localhost:8081` 连接失败 (403)

**原因分析**：

1. **路径不匹配（首要原因）**：NoneBot 的 OneBot 适配器将反向 WebSocket 端点注册在 `/onebot/v11/ws/`，而非根路径 `/`。NapCat 如果配置为 `ws://localhost:8081`（即请求 `/`），FastAPI 找不到对应路由，返回 403。

2. **Access Token 未传递或错误**：即使路径正确，如果 NapCat 没有携带 `ONEBOT_ACCESS_TOKEN`（通过 `Authorization: Bearer <token>` 请求头或 `?access_token=<token>` 查询参数），NoneBot 也会因鉴权失败返回 403。

**解决方法**：

**步骤 1**：修改 NapCat 的反向 WebSocket 地址为正确路径
```
ws://127.0.0.1:8081/onebot/v11/ws/
```

**步骤 2**：在 URL 中携带 access token（两种方式任选一种）

方式 A — 查询参数：
```
ws://127.0.0.1:8081/onebot/v11/ws/?access_token=你的ONEBOT_ACCESS_TOKEN
```

方式 B — 单独配置 token 字段（如果 NapCat WebUI 支持）：
- WebSocket 地址：`ws://127.0.0.1:8081/onebot/v11/ws/`
- Access Token：与 `.env` 中 `ONEBOT_ACCESS_TOKEN` 一致

**步骤 3**：确认 NoneBot 已加载 OneBot 适配器
```bash
nb plugin list
```
应能看到 `nonebot_adapter_onebot`。检查 `bot.py` 确保已注册适配器：
```python
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter
driver.register_adapter(OneBotV11Adapter)
```

**步骤 4**：检查 NoneBot 启动日志
成功加载适配器后，启动日志应打印：
```
OneBot V11 | WebSocket Server listening on ws://0.0.0.0:8081/onebot/v11/ws/
```
确保 NapCat 的地址与此日志中的路径完全一致。

**步骤 5**：验证 Token 一致性
确保 `QQBot/.env` 中的 `ONEBOT_ACCESS_TOKEN` 与 NapCat 配置的 token **完全一致**（注意无多余空格、换行）。Token 中的 `~` 等特殊字符在 URL 中是安全的，直接使用即可。

---

### SearXNG 容器启动 / 搜索失败

**现象一：`docker pull searxng/searxng:latest` 超时或 `Connection reset by peer`**

**原因**：与 Docker 安装类似，GFW 阻断了对 Docker Hub（海外 CDN）的访问。

**解决**：给 Docker 配置镜像加速器（任选一种）：

方案 A — 全局配置 registry-mirrors（推荐，一劳永逸）：
```bash
sudo tee /etc/docker/daemon.json <<'EOF'
{
  "registry-mirrors": [
    "https://docker.1ms.run",
    "https://docker.xuanyuan.me",
    "https://docker.mybacc.com",
    "https://dytt.online"
  ]
}
EOF
sudo systemctl restart docker
```
配置后直接 `docker pull searxng/searxng:latest` 即可，Docker 会自动按顺序重试各个镜像。

方案 B — 手动通过代理拉取：
```bash
docker pull docker.xuanyuan.me/searxng/searxng:latest && \
docker tag docker.xuanyuan.me/searxng/searxng:latest searxng/searxng:latest
```

> **注意**：单个代理（如 `docker.1ms.run`）可能没缓存特定镜像，换一个试试。如果都不行，用方案 A 让 Docker 自动重试。

**现象二：容器运行中但搜索无结果 / 超时**

```bash
# 检查容器状态
docker logs searxng --tail 50

# 测试 API
curl "http://localhost:8082/search?format=json&q=test"
```

**原因**：SearXNG 默认引擎（Google / DuckDuckGo / Wikipedia 等）在国内全部不可用，`settings.yml` 中已只启用 Bing。但 Bing 在国内有时也会间歇性超时。

**解决**：等几分钟重试；或者编辑 `searxng/settings.yml` 添加国内可用的搜索引擎（如百度），然后 `docker compose restart searxng`。

**现象三：容器退出 / 反复重启**

```bash
docker compose up searxng   # 前台运行看报错
docker compose run --rm searxng cat /etc/searxng/settings.yml  # 验证挂载
```

常见原因：`settings.yml` YAML 格式错误（缩进必须用空格不能用 tab）、端口 8082 被占用。

**QQ 消息发送超时 (retcode 1200)？**
- 已内置重试机制 + 智能拆分（300 字符 / 块 + 1s 间隔）
- 如仍出现，增大 `.env` 中的间隔时间或减小 chunk 大小

**模型路由不生效？**
- 检查 `QQBot/config/models_settings.json` 中 model 字段非空
- 留空则所有模型回退到 `.env` 的 DeepSeek 默认配置

**图片分析不可用？**
- 编辑 `models_settings.json` 的 `MULTIMODAL_MODEL` 部分
- 填入支持 OpenAI 兼容 vision API 的服务（如 GPT-4V, Claude Vision）
- 未配置时图片仅返回尺寸 / 格式等基本信息

**音频/语音分析不可用？**
- 编辑 `models_settings.json` 的 `AUDIO_MODEL` 部分
- 配置支持音频的多模态模型（如 qwen3-omni-flash，通过 DashScope 原生 API）
- 需要 `ffmpeg` 和 `pilk`（SILK 解码）已安装
- 未配置时音频仅返回元数据（格式/时长/采样率）

**画图 / 语音生成不可用？**
- 编辑 `models_settings.json` 的 `IMAGE_GEN_MODEL` / `TTS_MODEL` 段（DashScope 原生接口）
- 需要先在阿里云百炼开通 `qwen-image` 与 `qwen-audio-tts` 权限
- 生成受好感等级门（图 Lv4 / 语音 Lv5）与每日配额（3/10/50）约束

**官方渠道收不到消息 / 发不出消息？**
- 检查 `.env` 的 `QQ_BOTS` 是否配置，且 `intent` 里 `c2c_group_at_messages` 已开启（官方渠道只投递单聊与群 @ 消息）
- 官方渠道被动回复有配额（单聊 4 次/60 分钟、群 5 次/5 分钟），超出的消息会被丢弃并打 WARNING；「正在思考…」会独立占用一次
- 面板仪表盘「QQ 官方路由」卡片可看连接状态 / 消息计数；状态文件是 `QQBot/data/official_status.json`

## 📖 扩展阅读

**设计与实施文档（仓库根目录）**

- [Bond-System-Design.md](Bond-System-Design.md) — 角色养成 / 双平面路由 / 影音与剧情（P0–P5 全阶段已实施）
- [Story-Knowledge-Base-Design.md](Story-Knowledge-Base-Design.md) — 原作剧情知识库（构建、检索、注入门控）
- [Voice-System-Design.md](Voice-System-Design.md) — 三层音色架构（设计稿）
- [Cache-Hit-Rate-Plan.md](Cache-Hit-Rate-Plan.md) — 提示词分层与缓存命中率优化
- [Memory-Optimization-Plan.md](Memory-Optimization-Plan.md) / [Profile-Fact-Extraction-Plan.md](Profile-Fact-Extraction-Plan.md) — 记忆与画像抽取
- [Web-UI-Plan.md](Web-UI-Plan.md) — 管理面板设计

**开发文档**

- [DOCUMENTATION.md](QQBot/DOCUMENTATION.md) — 完整项目文档（架构 / 内核分层 / 模块详解 / 数据流 / 多模态 / 模型 / 面板 / 演进历史）
- [PLAN.md](QQBot/PLAN.md) — Agent 架构设计决策记录
- [docs/qqbot-official-buttons-design.md](docs/qqbot-official-buttons-design.md) — QQ 官方渠道消息按钮：功能盘点 + 接入设计（**设计稿，尚未实施**）
- [docs/qqbot-api-v2-full-docs.md](docs/qqbot-api-v2-full-docs.md) — QQ 官方 API v2 离线全量文档（192 章）
- [docs/更新日志.md](docs/更新日志.md) — 按提交日期汇总的开发者更新日志
- [WORKSPACE.md](QQBot/agent/config/WORKSPACE.md) — 安全模型与能力边界
- [AGENTS.md](QQBot/agent/config/AGENTS.md) — Agent 编排规则

# Assignment 4 QQ Agent — Implementation Plan (PLAN.md)

## 1. Solution Overview

| 选择项 | 说明 |
|--------|------|
| **方案** | 自行开发 (Self-developed) |
| **框架** | NoneBot2 + OneBot V11 适配器 |
| **QQ 连接** | NapCat (反向 WebSocket) |
| **AI 后端** | DeepSeek API (OpenAI 兼容 Function Calling) |
| **智能体架构** | Think→Act→Observe→Respond 循环, Markdown 配置文件驱动 |
| **运行环境** | WSL2 (Ubuntu 22.04), Python 3.12, NapCat Shell |

---

## 2. Selected QQ Interaction Tasks

### Task A: 查询天气 (Check Weather via Search)

**工具**: `search_web` (SearXNG — 天气查询通过搜索 + LLM 合成)

**交互流程**:
```
User: @Roxy 今天深圳天气怎么样？
Agent: [THINK] 用户要查天气 → call search_web("深圳 2026-05-26 天气")
       [OBSERVE] SearXNG 返回 5 条天气相关搜索结果
       [RESPOND] 根据搜索结果整合: 深圳今天多云，28-33°C，南风3级...
```

**验收标准**:
- [x] Agent 正确理解天气查询意图
- [x] 自动调用 search_web 工具（query 包含城市+天气）
- [x] 从搜索结果中提取整合天气信息
- [x] 支持中文城市名

### Task B: 搜索信息 (Search Web)

**工具**: `search_web` (SearXNG JSON API)

**交互流程**:
```
User: @Roxy 帮我查一下 DeepSeek-V3 的最新消息
Agent: [THINK] 用户要搜索 → call search_web("DeepSeek-V3 最新消息")
       [OBSERVE] SearXNG 返回结果（聚合 Google/Bing/DDG/Wikipedia）
       [RESPOND] 根据搜索结果整理回复...
```

**验收标准**:
- [x] Agent 正确触发搜索工具
- [x] 返回结构化结果（标题、摘要、URL、来源引擎）
- [x] 基于搜索结果组织回复

### Task C (备选): 执行代码 (Execute Code)

**工具**: `execute_code` (subprocess)

**交互流程**:
```
User: @Roxy 写一段 Python 代码计算斐波那契数列前 20 项
Agent: [THINK] 用户要代码执行 → call execute_code("def fib...")
       [OBSERVE] 获取执行结果
       [RESPOND] 代码输出是...
```

### Task D (备选): 多轮辩论 (Multi-turn Debate)

**纯 LLM 对话** (不需要工具)

**交互流程**:
```
User: @Roxy 我觉得 AI 会取代所有人类工作
Agent: [RESPOND] 这个观点很有意思，但我觉得...
User: @Roxy 但是最近很多公司都在裁员
Agent: [RESPOND] (基于记忆中的上下文继续讨论)
```

---

## 3. Recommended Execution Plan

**推荐演示 Task A (天气) + Task B (搜索)**，原因：
1. 两者都使用统一的 `search_web` 工具（SearXNG 聚合搜索），体现 Agent 的工具调用循环
2. 天气查询展示 Agent 通过搜索 + LLM 合成结果的能力（而非独立天气 API）
3. 不需要准备额外材料（如 PDF 文件、代码仓库）
4. 执行速度快，交互流畅
5. 截图清晰，证据链完整

如果时间充裕，可额外演示 Task C (代码执行) 或 Task D (多轮辩论，体现 Session + Memory + Profile 的联动)。

---

## 4. Evidence Capture Checklist

### 4.1 运行环境证据
- [ ] 终端截图：显示 NoneBot 启动日志 (含 `Running on 0.0.0.0:8081`)
- [ ] 终端截图：显示 Napcat 运行进程 (`ps aux | grep napcat`)
- [ ] 可选：任务管理器截图 (Windows 侧)

### 4.2 QQ 连接证据
- [ ] NapCat WebUI 截图：显示 WebSocket 连接状态 (已连接)
- [ ] NoneBot 日志截图：显示 `Connected to OneBot V11` 或收到消息事件

### 4.3 Task A 对话截图
- [ ] 完整截图：用户发送 "@Roxy 今天深圳天气怎么样？"
- [ ] 完整截图：Agent 回复天气信息
- [ ] 确保截图包含：时间、机器人名称、用户消息、Agent 回复

### 4.4 Task B 对话截图
- [ ] 完整截图：用户发送搜索请求
- [ ] 完整截图：Agent 返回搜索结果
- [ ] 确保截图包含完整上下文

### 4.5 额外证据
- [ ] bot.py 启动命令截图
- [ ] `.env` 配置文件截图 (敏感 Token 打码)
- [ ] Agent 状态截图 (`/status` 命令)

---

## 5. Report Structure

```
Assignment4_Report.md

1. 方案说明
   - 自行开发的 Agent 系统
   - 基于 NoneBot2 + NapCat
   - Markdown 驱动的智能体架构

2. 运行环境
   - OS: WSL2 Ubuntu 22.04
   - Python: 3.12
   - 关键依赖: nonebot2, nonebot-adapter-onebot, httpx, PyPDF2
   - QQ 连接: NapCat (OneBot V11 反向 WebSocket)

3. QQ 连接方式
   - NapCat 作为 QQ 协议客户端
   - 反向 WebSocket 连接到 NoneBot
   - 端口: 8081, 路径: /onebot/v11/ws
   - Access Token 鉴权

4. Agent 基本能力
   - 天气查询 (check_weather)
   - 网页搜索 (search_web)
   - Python 代码执行 (execute_code)
   - PDF 摘要 (summarize_pdf)
   - Git 仓库下载 (download_repo)
   - 翻译、代码解释、抽卡模拟等娱乐功能
   - 用户画像 (自动提取用户事实/偏好)
   - 长期记忆 (关键词搜索注入)

5. 安装、配置、启动、测试步骤
   (分步说明 + 命令)

6. Task A 结果: 天气查询
   - 测试说明
   - 截图
   - 通过理由

7. Task B 结果: 信息搜索
   - 测试说明
   - 截图
   - 通过理由

8. 自行开发工作说明
   - Agent 核心循环 (agent/agent.py)
   - 工具注册表 (agent/tool_registry.py)
   - 会话管理 (agent/session.py)
   - 长期记忆 (agent/memory.py)
   - 用户画像 (agent/profile.py)
   - DeepSeek 客户端升级 (Function Calling)
   - 统一消息路由 (plugins/agent_router.py)
   - 11 个工具实现 (tools/)
   - Markdown 配置文件 (agent/config/)
```

---

## 6. Submission Package Structure

```
Assignment4_QQ_Agent_<学号>/
├── Assignment4_Report.md          # 主报告
├── evidence/                      # 证据文件夹
│   ├── 01_environment.png         # 运行环境截图
│   ├── 02_nonebot_startup.png     # NoneBot 启动日志
│   ├── 03_napcat_connection.png   # NapCat WebSocket 连接
│   ├── 04_taskA_weather.png       # Task A 对话截图
│   ├── 05_taskB_search.png        # Task B 对话截图
│   └── 06_agent_status.png        # Agent 状态截图
├── code/                          # 代码文件
│   ├── agent/                     # 智能体核心
│   │   ├── agent.py
│   │   ├── tool_registry.py
│   │   ├── session.py
│   │   ├── memory.py
│   │   ├── profile.py
│   │   └── config/                # 配置文件
│   │       ├── SOUL.md
│   │       ├── IDENTITY.md
│   │       ├── AGENTS.md
│   │       ├── TOOLS.md
│   │       ├── BOOTSTRAP.md
│   │       └── SESSION.md
│   ├── plugins/
│   │   └── agent_router.py
│   ├── tools/
│   │   ├── builtin_tools.py
│   │   └── legacy_tools.py
│   ├── lib/
│   │   └── deepseek_client.py
│   ├── test_agent.py
│   ├── requirements.txt
│   └── .env.example
└── PLAN.md                        # 本文件
```

---

## 7. Timeline

| 步骤 | 内容 | 预计时间 |
|------|------|----------|
| 1 | 确认 Agent 运行正常 | ✓ 已完成 |
| 2 | 文档更新 (DOCUMENTATION.md) | ✓ 已完成 |
| 3 | Task A 测试执行 + 截图 | 10 min |
| 4 | Task B 测试执行 + 截图 | 10 min |
| 5 | 环境证据截图 | 5 min |
| 6 | 撰写报告 (Assignment4_Report.md) | 30 min |
| 7 | 打包提交 | 5 min |

**截止日期**: 2026.05.30

---

## 8. Prerequisites Checklist

在执行 QQ 交互任务之前，确保：

- [ ] NapCat 正在运行 (QQ 已登录)
- [ ] NoneBot 正在运行 (`nb run`)
- [ ] WebSocket 连接正常 (NoneBot 日志有 `Connected` 消息)
- [ ] DeepSeek API 可用 (发一条测试消息确认)
- [ ] QQ 客户端可以正常收发消息

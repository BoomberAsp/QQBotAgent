# DeepScientist 集成方案

> 2026-05-26 — 待决策

DeepScientist (v1.6.0) 是一个 daemon-based AI 研究工作室，运行在 HTTP 20999 端口。它驱动外部 AI coding agent（Claude Code、Codex、Kimi、OpenCode）作为 "runners" 执行研究任务，具备 MCP servers（memory、artifact、bash_exec）、git-based quest 管理，以及自带的 QQRelayChannel。

## 方案 A：工具调用（Tool-based）

```
用户 → QQBot Agent → Tool: deepscientist_start_research(topic)
                   → Tool: deepscientist_check_status(quest_id)
                   → Tool: deepscientist_get_results(quest_id)
```

- 在 QQBot `tools/` 下新增 `deepscientist_tools.py`，封装 DeepScientist REST API 为 Agent 工具
- DeepScientist daemon 在 20999 端口提供 HTTP API，QQBot 通过 HTTP 调用提交任务、查询状态、获取结果

| 优点 | 缺点 |
|------|------|
| 改动最小，QQBot 全部现有功能不受影响 | 两套系统独立运行，用户画像/记忆不互通 |
| DeepScientist 作为可选能力按需调用 | Agent 只能通过轮询获取研究结果 |
| 实现最快 | 大文件（论文 PDF、实验产物）传递困难 |

---

## 方案 B：Channel 替换（Channel-based）

```
用户 → DeepScientist QQRelayChannel → QuestService → Runners → 回复
```

- 用 DeepScientist 自带的 `QQRelayChannel`（`src/deepscientist/channels/qq.py`, 1106 行）完全替代 QQBot
- QQRelayChannel 已具备：群聊感知、@检测、文件收发、connector bridge、命令系统
- QQBot 项目降级为仅提供 NoneBot WebSocket 桥接

| 优点 | 缺点 |
|------|------|
| DeepScientist 原生 QQ 支持，mentions/groups/commands 全内置 | 完全放弃 QQBot：模型路由、连续对话、SearXNG 搜索、代码沙盒、游戏工具、用户画像全部丢失 |
| Quest 模型天然支持长任务跟踪 | file-based relay 比 NoneBot WebSocket 多一层延迟 |
| 长期来看架构更统一 | 迁移成本最高 |

---

## 方案 C：混合架构（Hybrid，推荐）

```
用户 → QQBot Agent
         ├── 简单任务 → FLASH_MODEL 直接回复
         └── 深度研究 → 提交到 DeepScientist
                        → Runners (Claude Code/Codex) 执行
                        → QQBot 读取结果并回复用户
```

- QQBot 保持为聊天前端，DeepScientist 作为后端研究引擎
- 通过 DeepScientist 的 inbox/outbox relay 或 HTTP API 提交任务
- 研究结果异步返回，QQBot 轮询 outbox 或接收 webhook 后推送给用户

| 优点 | 缺点 |
|------|------|
| QQBot 全部现有功能保留不损 | 两套系统需要同时运行 |
| 深度研究交给专业系统，职责清晰 | 异步返回机制需要额外开发（outbox 轮询或 webhook） |
| 两套系统解耦，各自独立进化 | 两套系统间需要定义清晰的任务协议 |
| 失败隔离（研究挂了不影响基础聊天） | |
| 渐进式：先 HTTP API，后续可深化到 relay | |

---

## 方案 D：MCP 集成（MCP-based）

```
QQBot Agent → MCP Client → DeepScientist MCP Servers
                              ├── memory server
                              ├── artifact server
                              └── bash_exec server
```

- QQBot 作为 MCP Client 连接到 DeepScientist 的 MCP Servers
- 通过 MCP 协议直接访问 DeepScientist 的记忆、产物、代码执行能力
- DeepScientist 已有 3 个 MCP server（`src/deepscientist/mcp/`）

| 优点 | 缺点 |
|------|------|
| 标准化协议（MCP），记忆和产物可共享 | QQBot 现有架构不包含 MCP client，需引入 mcp SDK |
| QQBot Agent 获得更强的执行能力 | DeepScientist MCP server 为内部 runners 设计，直接暴露可能需要适配 |
| 可逐步接入（先 memory，再 artifact） | 需要额外开发连接层和协议适配 |

---

## 综合对比

| 维度 | A 工具调用 | B Channel 替换 | C 混合架构 | D MCP 集成 |
|------|-----------|---------------|-----------|-----------|
| 实现工作量 | 低 | 高 | 中 | 中-高 |
| QQBot 破坏性 | 无 | 完全替换 | 无 | 低 |
| DeepScientist 修改 | 无 | 无 | 无/低 | 中 |
| 记忆/画像共享 | 不共享 | DeepScientist 主导 | 各自维护 | 可共享 |
| 长任务支持 | 差（轮询） | 原生支持 | 好（异步） | 好 |
| 风险 | 低 | 高 | 低 | 中 |

## 推荐：方案 C（混合架构）

- 改动最小、风险最低 — QQBot 零破坏，DeepScientist 零修改
- 职责清晰 — QQBot = 聊天前端 + 轻量任务，DeepScientist = 重量级研究
- 利用已有基建 — DeepScientist 的 inbox/outbox relay 机制为此场景设计
- 渐进式 — 先接入最简单的 HTTP API 调用，后续可深化到 relay

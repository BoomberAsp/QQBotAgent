# 特殊会话 & 工作区问题排查报告

> SSH 登录服务器 `106.52.32.91` 实际排查后撰写。服务器环境: Ubuntu, 数据盘挂载于 `/home/ubuntu/datadisk`, bot 运行于 screen session `nonebot`。

---

## 服务器实际状态

```
USER_DATA_ROOT=/home/ubuntu/datadisk/QQBotData   ← .env 中的值
/home/ubuntu/datadisk/QQBotData/                  ← 空目录，owned by root (bot 进程无法写入)
/home/ubuntu/QQBotAgent/QQBot/data/
├── sessions/2578260985.json                       ← 临时会话持久化 (7.5KB, 8 条消息, 84 次工具调用)
├── users_store/2578260985/
│   ├── sessions/
│   │   ├── _index.json                            ← active_session: null, sessions: [{total_messages: 0}]
│   │   └── napcat语音bug调试/                     ← 空目录
│   └── workspace/{code,output,projects,uploads}/  ← 全部为空
├── memory/
└── workspace/                                      ← 共享工作区
```

---

## Bug A（致命）：`create()` 创建特殊会话但不激活，消息全部丢失

### 现象

用户创建特殊会话 "napcat语音bug调试"，进行了"非常长的多轮对话"。但服务器上该会话目录为空，`_index.json` 显示 `total_messages: 0`。Agent 报告该会话"从未真正使用过"。

### 根因

`agent/special_session.py` 的 `create()` 方法：

```python
def create(self, user_id, name=None):
    ...
    session = SpecialSession(...)
    self._save(session)                    # ← 只创建了目录
    self._update_index(user_id, session)   # ← 只添加到 sessions 列表
    return session                         # ← 没有设置 active_session!
```

而 `_update_index()` 仅仅将 session 加入 `sessions[]` 列表，**不设置 `active_session`**。

`switch_to()` 是唯一设置 `active_session` 的方法：

```python
def switch_to(self, user_id, name):
    ...
    index["active_session"] = name         # ← 只有这里设置了 active_session
    self._save_index(user_id, index)
```

但 `plugins/agent_router.py` 的 `/新会话` handler 中：

```python
if cmd in ("/新会话", "#新会话"):
    session = _special_sessions.create(user_id, name)
    # ← 缺少 _special_sessions.switch_to(user_id, session.name) !!
    await _safe_send("已创建特殊会话...当前处于特殊会话模式...")
    return True
```

**没有调用 `switch_to()`。** 创建后 `_index.json` 中 `active_session` 仍为 `null`。

### 消息丢失的完整链路

```
用户发送 /新会话 napcat语音bug调试
  → create() 成功，active_session = null
  → 回复: "已创建特殊会话...当前处于特殊会话模式"  ← 谎报

用户发送消息 (在"特殊会话"中)
  → handle_agent_message()
  → active_special = _special_sessions.get_active(user_id)
  → get_active() → _load_index() → active_session = null → return None
  → session_type = "temporary"  ← 实际走了临时会话!
  → agent.run() with session_type="temporary"
  → special_session = None
  → 消息存入 SessionManager (data/sessions/2578260985.json) ← 丢错地方
  → special_session 目录永远为空
```

**用户以为在特殊会话中对话，实际所有消息都进了临时会话。**

### 修复方向

在 `/新会话` handler 的 `create()` 之后添加 `_special_sessions.switch_to(user_id, session.name)`。

---

## Bug B（严重）：USER_DATA_ROOT 配置无效，目录权限导致数据错位

### 现象

`.env` 配置 `USER_DATA_ROOT=/home/ubuntu/datadisk/QQBotData`，但实际用户数据全部写入了 `QQBot/data/users_store/`。

### 根因

```
$ ls -la /home/ubuntu/datadisk/QQBotData/
drwxr-xr-x 2 root root 4096 May 28 17:24 .    ← 所有者是 root
```

Bot 进程以 `ubuntu` 用户运行，没有权限在此目录创建子目录和文件。`UserWorkspaceManager`、`SpecialSessionManager`、`ProfileManager`、`HardwareDetector` 全部被传入这个无写入权限的路径。

根据代码中 `os.makedirs(path, exist_ok=True)` 的行为：对已存在但权限不足的目录会抛出 `PermissionError`。但各处代码的异常处理都是 `except Exception: pass`（静默吞错），导致 bot 部分功能降级但没有任何日志。

然而实际数据落到了 `QQBot/data/` 下，说明这些模块在某处有 fallback 逻辑或者初始化时的默认值覆盖了 `.env` 配置。需要检查 `_USER_DATA_ROOT` 的解析链。

### 影响

- 用户数据写入到项目目录而非数据盘，违背了部署意图
- 如果项目目录被清理（如重新部署），所有用户数据将丢失
- 实际路径与 Agent 通过 `shell_exec` 看到的路径不一致

---

## Bug C（严重）：Agent 将临时会话文件误判为特殊会话的"快照层"

### 现象

用户问"特殊会话的文件存放在哪？"，Agent 描述了这样的架构：

```
sessions/2578260985.json          ← "全局快照层"
users_store/2578260985/sessions/   ← "增量层"
```

### 根因

这是**两套完全独立**的存储系统被 Agent 自行关联：

| 路径 | 管理者 | 实际用途 |
|------|--------|---------|
| `data/sessions/{uid}.json` | `SessionManager` | **临时会话**持久化 |
| `users_store/{uid}/sessions/{name}/` | `SpecialSessionManager` | **特殊会话**，内部才有 snapshots + delta |

真正的快照 + 增量架构在 `SpecialSessionManager` 内部：

```
users_store/{uid}/sessions/{name}/
├── snapshot_00050.json   ← 这才是快照
└── delta.jsonl           ← 这才是增量
```

Agent 通过 `shell_exec` 看到了两个带 "session" 的路径，自行将临时会话 JSON 拼凑为特殊会话的"快照层"——纯属 LLM 幻觉。**没有一份配置文档解释三者（临时会话 / 特殊会话 / 用户工作区）的关系。**

---

## Bug D（中等）：工作区路径仅在特殊会话模式下注入系统提示词

### 根因

`agent/agent.py:264-280` `_build_messages()`：

```python
if special_session:             # ← 临时模式下整个块被跳过
    system_content += (
        f"工作区: {self.workspaces.get_workspace(user_id)}"
        ...
    )
    if self.workspaces:
        quota_ctx = self.workspaces.get_quota_context(user_id)
```

`special_session` 为 None 时（临时模式 / 已退出特殊会话 / Bug A 导致的实际临时模式），Agent 对用户独立工作区路径、配额、与共享工作区的区别**完全无知**，被迫每次 `shell_exec` 探索。

---

## Bug E（轻微）：所有 10 个 Markdown 配置文件缺乏用户工作区文档

- `WORKSPACE.md` — 只描述共享工作区 `data/workspace/` 的工具约束
- `SESSION.md` — 只描述临时会话持久化
- `AGENTS.md` — 有特殊会话命令列表，无工作区说明
- 其他 7 个文件 — 无工作区相关内容

**没有任何配置文件解释以下概念及其关系**：共享工作区（工具执行）、用户独立工作区（持久隔离存储）、特殊会话目录（长对话持久化）。

---

## 总结

| # | 严重程度 | 问题 | 根因 | 类型 |
|---|---------|------|------|------|
| A | 🔴 致命 | 特殊会话创建后消息全部丢失 | `create()` 未调用 `switch_to()`，`active_session` 为 null | 代码 bug |
| B | 🔴 严重 | USER_DATA_ROOT 无写入权限 | `QQBotData/` 目录为 root 所有，ubuntu 用户无写权限 | 部署问题 |
| C | 🔴 严重 | Agent 编造双层存储架构 | 无文档描述存储架构 + Agent 文件系统探索后自行推理 | 配置缺口 + LLM 幻觉 |
| D | 🟡 中等 | 临时模式下 Agent 不知道用户工作区 | `_build_messages` 工作区注入被 `if special_session:` 包裹 | 设计缺陷 |
| E | 🟡 中等 | Agent 不知道用户工作区的存在 | 10 个 Markdown 配置文件零提及用户工作区概念 | 配置缺口 |
| F | 🟢 低 | 服务器 `.env` 中 `SUPERUSERS=["你的QQ号"]` 未填写 | 部署时未修改模板值 | 部署遗漏 |

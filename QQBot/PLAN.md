# Workspace File Management — Design Plan

## Problem

用户工作区文件只能通过上传增加，无法删除。磁盘配额有限（普通用户 100MB / 会员 500MB / 管理员 2GB），用完后就无法上传新文件。用户需要手段查看和管理自己的工作区文件。

## Design Principles

- **增（upload）**：已有，通过 QQ 文件上传 → `_download_and_save_file()`
- **查（list/view）**：新增快照工具，只看目录结构和文件大小，不给文件内容查看权限
- **删（delete）**：新增删除工具/命令，允许用户清理不需要的文件
- **改（modify）**：不给，用户不能修改工作区文件内容

> 注：`read_file` 工具已允许用户读取文本/PDF，这属于智能体辅助分析场景，不在本次讨论范围。

---

## Feature 1: `get_workspace_snapshot` Tool

### Purpose

快速返回当前用户工作区完整快照：目录树 + 每个文件的大小 + 磁盘使用量汇总。

### Behavior

- 遍历 `{workspace}/` 下所有子目录（`uploads/`, `code/`, `repos/`, `output/`）
- 返回人类可读的树形结构，每个节点标注文件大小
- 顶部给出总计使用量 vs 配额
- **当使用量 ≥ 80% 配额时**，追加醒目的容量警告并提示清理办法
- 纯本地操作，无网络开销，秒级返回

### Output Example (Normal)

```
📁 工作区 /home/ubuntu/datadisk/QQBotData/2578260985/workspace/
├── 📁 uploads/                                    (2 个文件, 1.2 MB)
│   ├── 📄 a1b2c3d4-Assignment_3.pdf               (523 KB)
│   └── 📄 e5f6g7h8-作业1评分标准.pdf               (658 KB)
├── 📁 code/                                       (1 个文件, 4 KB)
│   └── 📄 sort_demo.py                            (4 KB)
├── 📁 repos/                                      (空)
└── 📁 output/                                     (1 个文件, 32 KB)
    └── 📄 chart_20260530.png                       (32 KB)
─────────────────────────────────────────────────
总计: 1.24 MB / 500 MB (0.2%)  |  剩余: 498.76 MB
```

### Output Example (≥80% Quota Warning)

```
📁 工作区 ...
...（同上树形结构）...
─────────────────────────────────────────────────
⚠️  总计: 420.31 MB / 500 MB (84.1%)  |  剩余: 79.69 MB

容量警告：工作区使用量已超过 80%！
继续上传可能导致空间不足。建议：
  - 发送 /管理工作区 查看并清理不需要的文件
  - 或直接告诉我 "帮我清理工作区"
```

### Registration

| 属性 | 值 |
|------|-----|
| 工具名 | `get_workspace_snapshot` |
| 描述 | 获取当前用户工作区的完整目录结构和磁盘使用情况 |
| 参数 | 无（自动限定当前用户工作区） |
| 权限 | 所有用户（普通/会员/管理员） |
| 实现位置 | `QQBot/tools/builtin_tools.py` |

### Implementation Notes

- 使用 `_current_user_workspace` contextvar 获取工作区根目录
- `os.scandir()` 递归遍历，只读操作（`stat` 获取文件大小）
- 跳过 `.git` 目录和 `.pyc` 等缓存文件
- 排序：目录优先，然后按名称字母序
- 通过 `PermissionManager.get_workspace_quota_mb()` 获取当前用户配额，计算使用百分比

---

## Feature 2: `delete_workspace_file` Tool

### Purpose

允许用户删除工作区中的文件或空目录，释放磁盘空间。

### Behavior

- 接收相对路径（相对于工作区根目录），如 `uploads/old_file.pdf`
- 只能删除工作区内的文件/目录，禁止穿越到工作区外
- 如果目标是文件 → 直接删除
- 如果目标是目录 → 仅当目录为空时删除；非空拒绝并提示
- 删除前返回确认信息（文件名 + 大小），智能体可以直接执行无需二次确认
- 返回删除结果（成功/失败 + 释放了多少空间）

### Safety Constraints

1. **路径安全**：复用 `_validate_path()` 的 workspace 边界检查逻辑
2. **禁止删除系统文件**：拒绝以 `.` 开头的隐藏文件/目录
3. **禁止删除会话数据**：拒绝 `sessions/` 目录下的任何操作（那是特殊会话存储）
4. **禁止删除非空目录**：防止误删整个 `repos/` 或 `code/` 目录

### Registration

| 属性 | 值 |
|------|-----|
| 工具名 | `delete_workspace_file` |
| 描述 | 删除工作区中的指定文件或空目录，释放磁盘空间 |
| 参数 | `path` (string, required) — 相对于工作区根目录的路径 |
| 权限 | 所有用户 |
| 实现位置 | `QQBot/tools/builtin_tools.py` |

---

## Feature 3: Session File Provenance Tracking

### Purpose

跟踪每个特殊会话期间上传/生成的文件，实现"删除会话时同步清理其文件"的能力。

### Design

在每个特殊会话的元数据中维护一个 `files` 列表，记录该会话期间上传的文件路径（相对于工作区根目录）。

```json
// sessions/{session_name}/_meta.json 新增字段
{
  "name": "STA404 答疑",
  "created_at": "2026-05-30T01:00:00",
  "files": [
    "uploads/a1b2c3d4-Assignment_3.pdf",
    "uploads/e5f6g7h8-作业1评分标准.pdf",
    "output/chart_20260530.png",
    "code/sort_demo.py"
  ]
}
```

### File Recording Logic

在 `_download_and_save_file()` 和 `execute_code()` / `download_repo()` 的调用处，当特殊会话处于活动状态时，将保存的文件路径追加到当前会话的 `files` 列表。

```python
# 伪代码 — agent_router.py 中文件下载后
if saved_path:
    # 如果当前处于特殊会话，记录文件归属
    active_session = _special_sessions.get_active(user_id)
    if active_session:
        rel_path = os.path.relpath(saved_path, workspace_root)
        _special_sessions.add_file(active_session, rel_path)
```

### SpecialSessionManager API 新增

```python
class SpecialSessionManager:
    def add_file(self, user_id: str, name: str, file_path: str):
        """Record a file against a special session."""

    def get_files(self, user_id: str, name: str) -> list[str]:
        """Get list of files (relative paths) belonging to a session."""

    def remove_file(self, user_id: str, name: str, file_path: str):
        """Remove a file record from a session (when file is deleted individually)."""
```

### Implementation Notes

- `files` 列表存储在 `_meta.json` 中，随会话持久化
- 文件路径为相对于工作区根目录的路径（便于跨环境移植）
- 文件被单独删除时（通过 `delete_workspace_file`），同步从会话的 `files` 列表中移除
- 同一文件可能被多个会话引用（例如通过 `/创建会话` 复制而来）——删除一个会话不影响其他会话对该文件的引用

---

## Feature 4: Session Deletion with File Cleanup Prompt

### Purpose

用户删除特殊会话时，智能体主动提示是否同步清理该会话上传的文件。

### Behavior

当用户发送 `/删除会话 <名称>` 时，现有流程要求用户通过二次确认（"确认删除 {名称}"）来执行。修改后：

1. 用户发送 `/删除会话 <名称>`
2. 系统查询该会话的 `files` 列表
3. 如果有文件，回复：

```
确认删除特殊会话「STA404 答疑」？

该会话上传了 3 个文件，共占用 8.42 MB：
  - uploads/Assignment_3.pdf (3.2 MB)
  - uploads/作业1评分标准.pdf (5.1 MB)
  - code/sort_demo.py (0.1 MB)

是否同步清理这些文件？
  回复「确认删除 STA404 答疑」→ 仅删除会话，保留文件
  回复「确认删除 STA404 答疑 --with-files」→ 删除会话 + 所有文件

⚠️ 注意：如果不清理，后续工作区容量不足时，可随时通过 /管理工作区 清理。
（60秒内有效）
```

4. 如果无文件，直接回复现有的确认提示（无额外选项）

### Implementation

修改 `QQBot/plugins/agent_router.py` 中 `/删除会话` 的处理逻辑（`_handle_session_command()`），在生成确认提示前查询会话文件列表。

### Edge Cases

- 用户仅确认删除会话（不带 `--with-files`）：文件保留在工作区，`_recent_files` 中对应的记录继续有效
- 用户确认带 `--with-files`：遍历 `files` 列表逐一删除，最后 `shutil.rmtree` 会话目录
- 文件已被手动删除（通过 `delete_workspace_file`）但仍在 session 的 files 列表中：删除时检查文件是否存在，不存在则跳过

---

## Feature 5: Quota Threshold Auto-Reminder

### Purpose

当工作区使用量达到配额的 80% 时，主动提醒用户清理。

### Trigger Points

| 触发场景 | 行为 |
|----------|------|
| **文件上传后** | `_download_and_save_file()` 成功后检查配额，≥80% 时在回复中追加容量警告 |
| **`get_workspace_snapshot` 调用** | 输出中自动包含警告横幅（见 Feature 1） |
| **特殊会话创建时** | `create()` 时检查配额，≥80% 时提示用户先清理再创建 |

### Warning Message Template

```
⚠️ 工作区容量已使用 {percent}%（{used} / {quota}）。
建议发送 /管理工作区 查看详情并清理不需要的文件。
也可直接告诉我 "帮我清理工作区"。
```

### Implementation

在 `QQBot/tools/builtin_tools.py` 中添加一个辅助函数：

```python
def _check_quota_warning() -> str:
    """Return a quota warning string if usage >= 80%, else ''."""
    workspace = _get_workspace_root()
    quota = _current_quota_bytes.get()  # set via contextvar
    used = _get_dir_size(workspace)
    if quota and used >= quota * 0.8:
        pct = used / quota * 100
        return f"⚠️ 工作区容量已使用 {pct:.0f}%..."
    return ""
```

在以下位置调用：
- `_download_and_save_file()` 成功后
- `_handle_session_command()` 中 `/创建会话` 前
- `get_workspace_snapshot()` 输出末尾

---

## Feature 6: `/管理工作区` Router Command

### Purpose

用户发送 `/管理工作区` 后，智能体自动调用 `get_workspace_snapshot` 并引导用户管理文件。

### Behavior

这条命令本身只是一个触发词——收到后智能体会：

1. 调用 `get_workspace_snapshot` 获取工作区快照
2. 向用户展示目录结构和容量使用情况
3. 询问用户要删除哪些文件（或让用户自行决定）
4. 根据用户指示调用 `delete_workspace_file` 执行删除

### Registration

在 `_handle_session_command()` 中添加：

```python
if cmd in ("/管理工作区", "#管理工作区"):
    # 触发智能体调用 get_workspace_snapshot + 引导清理
    return False  # 不拦截，交给智能体处理
```

---

## Files to Modify

| 文件 | 变更 |
|------|------|
| `QQBot/tools/builtin_tools.py` | 新增 `get_workspace_snapshot()`, `delete_workspace_file()`, `_check_quota_warning()` |
| `QQBot/plugins/agent_router.py` | 修改 `/删除会话` 确认流程（Feature 4）；增加文件上传后的配额检查（Feature 5）；添加 `/管理工作区` 路由（Feature 6）；在文件下载后记录会话归属（Feature 3） |
| `QQBot/agent/special_session.py` | 新增 `add_file()`, `get_files()`, `remove_file()` 方法；`_meta.json` 新增 `files` 字段 |
| `QQBot/agent/permissions.py` | `get_workspace_snapshot` 和 `delete_workspace_file` 加入 `_ALL_USER_TOOLS` 集合 |
| `QQBot/agent/context.py` | 新增 `_current_quota_bytes` contextvar，供配额检查使用 |

## Files to Create

无。所有变更在现有文件中完成。

---

## Verification

1. 上传文件 → `get_workspace_snapshot` → 确认文件出现在树中且大小正确
2. 删除文件 → 再次快照 → 确认已消失
3. 路径穿越测试 → 被拒绝
4. 非空目录删除 → 被拒绝
5. 上传文件达到 80% 配额 → 确认收到容量警告
6. 特殊会话中上传 3 个文件 → `/删除会话` → 确认显示文件列表和 `--with-files` 选项
7. `--with-files` 删除 → 确认会话目录和文件均已删除
8. 不带 `--with-files` 删除 → 确认会话目录删除但文件保留
9. 配额已满上传 → 收到配额提示 → 删除文件 → 再次上传成功

---

# Session Lifecycle Enhancements — Audit & Design Plan

以下四项来源于用户反馈，逐一审计当前实现状态，未实现的给出设计方案。

---

## Idea 1: 临时会话文件迁移到用户专属工作区

### 当前状态：**已架构上消除（无需迁移）**

所有文件从上传那一刻起就写入用户专属工作区 `{USER_DATA_ROOT}/{user_id}/workspace/`，无论是临时会话还是特殊会话。关键代码：

- `agent_router.py:1068-1070` — 每条消息处理前设置 `_current_user_workspace` contextvar：
  ```python
  _workspace_manager.ensure_dirs(user_id)
  _current_user_workspace.set(_workspace_manager.get_workspace(user_id))
  ```
- `agent_router.py:98-109` — `_get_uploads_dir()` 运行时读取 contextvar，始终返回用户工作区路径
- `builtin_tools.py:37-59` — `_get_workspace_root()` 同样读取 contextvar

**结论**：不存在「共享工作区」，所有用户的文件从一开始就隔离。无需迁移。但 `/保存为会话` 只复制了对话上下文（最近 20 条消息），没有额外操作——这已经足够，因为文件已经在正确位置。

---

## Idea 2: 特殊会话的两种创建方式

### 当前状态：**已实现**

| 创建方式 | 命令 | 行为 | 代码位置 |
|----------|------|------|----------|
| 从零创建 | `/新会话 <名称>` | 创建空上下文的特殊会话 | `agent_router.py:1432-1455` |
| 临时升级 | `/保存为会话 <名称>` | 将当前临时会话最近 20 条消息复制到新特殊会话 | `agent_router.py:1563-1605` |

`SpecialSessionManager.create()` (`special_session.py:82-125`) 创建 `SpecialSession` dataclass，其 `context` 字段默认为空列表。

`/保存为会话` 的核心逻辑 (`agent_router.py:1585`)：
```python
for msg in temp_session.context[-20:]:  # 最多 20 条
    _special_sessions.add_message(...)
```

AGENTS.md 中也有文档记录 (`config/AGENTS.md:95`)：
```
| `/保存为会话 <名称>` | 将当前临时会话最近 20 条消息保存为新特殊会话 |
```

**结论**：已完整实现，无需改动。

---

## Idea 3: 15+ 条消息自动建议升级为特殊会话

### 当前状态：**未实现**

### 问题

用户在临时会话中连续讨论同一话题，发送 15 条以上消息时，智能体不会主动建议升级。用户可能不知道 `/保存为会话` 功能，导致上下文积累在临时会话中、无法持久化。

### 设计

**触发条件**：临时会话上下文中累计消息数 ≥ 15 条，且智能体即将回复时。

**行为**：智能体在回复末尾追加升级建议：

```
💡 提示：当前话题已持续 {n} 条消息。考虑发送 /保存为会话 <名称> 来保存这段对话，方便后续继续讨论。临时会话有消息数限制，超出后会丢失早期上下文。
```

**关键问题：谁来触发？**

- **方案 A（推荐）**：在 `agent_router.py` 的 `_handle_agent_message_impl()` 中，调用 agent 前检查 `len(temp_session.context) >= 15`，如果满足且未在近 5 条消息内提示过，在 augmented message 末尾追加提示指令（低 token 成本方式）。智能体在回复时自然提及。
- **方案 B**：agent 回复后，`agent_router.py` 在发送前检查并追加提示文本（零 token 成本，但可能打断智能体输出）。

选择方案 A：让智能体在回复中自然融入提示，更符合对话体验。

### 实现要点

- `SessionManager` 暴露 `message_count(user_id)` 方法
- `agent_router.py` 在构建 augmented message 时检查
- 限流：同一用户每 10 条消息最多提示一次（防止骚扰）
- 提示文本加在 augmented message 中（非回复后追加），智能体可选择自然融入或忽略

---

## Idea 4: 上下文长度达到 85% 时提示压缩

### 当前状态：**未实现**

### 问题

特殊会话的 `context` 列表无限增长（`special_session.py:244-272` `add_message()` 无长度限制）。当消息累积到接近模型上下文窗口上限时，API 调用会失败或返回截断结果。目前没有主动的上下文长度监控或压缩提示。

### 现有相关代码

| 代码 | 作用 | 局限性 |
|------|------|--------|
| `agent.py:348-382` `_compress_context()` | 压缩旧 tool 结果（保留前 20 条完整，截断旧输出到第一行） | 仅按消息数量截断，不感知 token 数 |
| `config/models_settings.json` | 定义 `max_tokens`（REASONING_MODEL: 409600, FLASH_MODEL: 102400） | 这是模型输出上限，非上下文窗口上限；代码中未读取用于上下文管理 |

### 设计

**上下文窗口上限获取**：
- reasoning model（如 DeepSeek V3.2）：从 `models_settings.json` 或 API `/models` 端点获取 `max_input_tokens`
- flash model（如 DeepSeek V3.2 Flash）：同上
- 取 `min(reasoning_max_input, flash_max_input)` 作为有效上限

**触发条件**：
- 在 `SpecialSessionManager.add_message()` 后，计算当前会话的 token 估算值
- 若 `estimated_tokens >= effective_context_limit * 0.85`，设置会话的 `_needs_compression` 标志
- 智能体在下一轮对话中收到压缩提示

**Token 估算**（简化方案，避免引入 tiktoken 等重量依赖）：
- 中英文混合场景下，1 token ≈ 1.5~2 字符
- 保守估计：`estimated_tokens = len(json.dumps(context)) * 0.5`
- 或集成 `deepseek_tokenizer`（如有）

**智能体收到的提示**（注入到 augmented message）：
```
⚠️ [系统] 当前特殊会话上下文已达模型限制的 {percent}%，建议压缩。
压缩方式：回复中自然总结已完成的关键结论，然后建议用户发送 /压缩会话。
```

**`/压缩会话` 命令**（新增）：
1. 让智能体生成一份对话摘要（保留关键任务、结论、待办事项）
2. 用摘要替换当前上下文，保留最近 5 条完整消息
3. 告知用户压缩完成，展示保留的信息概要

### 实现要点

| 步骤 | 文件 | 变更 |
|------|------|------|
| 1. 读取模型上下文上限 | `model_router.py` 或新建 config | 从 models_settings.json 读取 `context_window`，或从 API 动态获取 |
| 2. Token 估算 | `special_session.py` | `add_message()` 后调用 `_estimate_tokens()` |
| 3. 阈值判断 | `special_session.py` | `add_message()` 中用 `add_message()` 后检查 |
| 4. 提示注入 | `agent_router.py` | 构建 augmented message 时检查 `_needs_compression` 标志 |
| 5. `/压缩会话` 命令 | `agent_router.py` | 触发智能体生成摘要 + 替换上下文 |

---

## Summary

| # | Idea | Status | Action |
|:-:|------|:------:|--------|
| 1 | 临时会话文件迁移 | 架构上已消除 | 无需改动 |
| 2 | 两种创建方式 | 已实现 | 无需改动 |
| 3 | 15+ 条消息升级建议 | **未实现** | 待实现（方案 A：augmented message 注入） |
| 4 | 85% 上下文压缩提示 | **未实现** | 待实现（token 估算 + `/压缩会话` 命令） |
| 5 | 群聊文件延迟下载 | **未实现** | 待实现（元数据记录 + 按需下载 + 进度反馈） |

---

## Idea 5: 群聊文件延迟下载（Lazy Download）

### 当前状态：**未实现**

### 问题

群聊文件通常较大（几十 MB），如果一上传就下载：
- 服务器带宽和磁盘吃不消
- 用户工作区可能在不知不觉中被占满
- 大多数群聊文件并非发给机器人的，下载了也用不上

当前行为：
- 群聊消息无 @ → 直接 return，文件不处理也不记录
- 群聊消息有 @ + 文件 → 立即下载到上传者的工作区
- 后续有人引用并 @ 机器人时，原始文件元数据已丢失，无法回溯

### 设计原则

**群聊中任何文件都不在收到时立即下载**。仅当用户明确引用文件消息并 @ 机器人时，才按需下载到**引用者**的工作区。

### 新流程

```
┌─ 用户 A 上传文件（无 @）─────────────────────────────┐
│  1. 提取文件元数据（msg_id, 文件名, file_id, 文件大小） │
│  2. 存入 _pending_group_files[msg_id]                  │
│  3. 不下载，不调用 agent，静默 return                    │
└──────────────────────────────────────────────────────┘

┌─ 用户 A 上传文件 + @Roxy ─────────────────────────────┐
│  1. 提取文件元数据（同上）                               │
│  2. 存入 _pending_group_files[msg_id]                  │
│  3. 不下载，但注入文件引用到 augmented message          │
│  4. Agent 处理时若需要，调用 read_file → 触发按需下载    │
└──────────────────────────────────────────────────────┘

┌─ 用户 B 引用文件 + @Roxy ─────────────────────────────┐
│  1. _build_reply_context 从 _pending_group_files       │
│     解析出文件元数据                                     │
│  2. 注入文件引用到 augmented message                    │
│  3. Agent 调用 read_file → 触发按需下载到 B 的工作区    │
└──────────────────────────────────────────────────────┘
```

### _pending_group_files 结构

```python
# 替代 _recent_files 在群聊场景的作用
# key: str(message_id) → file metadata (未下载)
_pending_group_files: dict[str, dict] = {}

# 条目示例
{
    "1107550660": {
        "msg_id": "1107550660",
        "name": "Assignment_3.pdf",       # 原始文件名
        "file_id": "f923ad99f95b09df...",  # OneBot file_id 用于 API 下载
        "file_size": 258414,               # 字节
        "uploader_qq": "2578260985",       # 上传者
        "group_id": "123456789",           # 来源群
    }
}
```

### 按需下载触发点

`read_file` 工具内部：

```python
async def read_file(path: str) -> str:
    workspace = _get_workspace_root()
    full_path = _resolve_path(path, workspace)
    
    if not os.path.exists(full_path):
        # 文件尚未下载 → 查找 _pending_group_files 中的元数据
        # → 调用 OneBot get_file API 下载
        # → 通过 progress_callback 返回 "⏳ 正在下载 {filename}..."
        # → 下载完成后返回 "✅ 下载完成"
        ...
        with open(full_path, "wb") as f:
            f.write(data)
    
    # 正常读取文件内容
    return _read_file_content(full_path)
```

### 下载进度反馈

```
⏳ 正在下载 Assignment_3.pdf (252 KB)...
✅ Assignment_3.pdf 下载完成，正在分析...
```

进度反馈通过 `progress_callback` 发送，与现有的 `⏳ 正在read_file...` 机制一致。

### _recent_files 与 _pending_group_files 分工

| 缓存 | 适用场景 | 存储内容 | 何时写入 | 何时下载 |
|------|----------|----------|----------|----------|
| `_recent_files` | 私聊 | 已下载文件的路径 | 文件下载后 | 立即下载 |
| `_pending_group_files` | 群聊 | 文件元数据（未下载） | 文件消息到达时 | `read_file` 调用时 |
| `_recent_files`（扩展） | 群聊按需下载后 | 已下载文件的路径 | `read_file` 下载后 | 延迟下载 |

> 群聊文件下载后同时写入 `_recent_files`，后续引用命中时直接使用已下载路径，避免重复下载。

### 安全约束

- 按需下载的文件大小仍需检查配额（下载前检查 `_get_dir_size() + file_size <= quota`）
- 超配额时拒绝下载并提示用户清理工作区
- 下载超时 120 秒（群聊文件可能较大）
- 同一文件被多人引用时分别下载到各自工作区（不共享，保持隔离）

### 清理策略

- `_pending_group_files` 上限 500 条，超出时删除最旧条目
- 已下载的文件元数据从 `_pending_group_files` 移除（转移到 `_recent_files`）
- 机器人重启后 `_pending_group_files` 清空（内存缓存），但不影响已下载的文件

### Files to Modify

| 文件 | 变更 |
|------|------|
| `QQBot/plugins/agent_router.py` | 新增 `_pending_group_files` 缓存；在 `handle_agent_message()` 的群聊 @ 判断前提取文件元数据并存入缓存；`_build_reply_context()` 适配 `_pending_group_files` 查找；修改群聊文件立即下载逻辑 |
| `QQBot/tools/builtin_tools.py` | `read_file` 添加按需下载逻辑；新增 `_download_pending_file()` 辅助函数 |

---

---

## Idea 6: 分层上下文 Layer 3 — 渐进式摘要

> 来源：`next_step.md` §8.2

### 当前状态：**Layer 1+2 已实现，Layer 3 未实现**

当前 `agent.py:348-382` `_compress_context()` 已实现：
- **Layer 1**：最近 20 条消息保留完整原文
- **Layer 2**：20 条之前的消息压缩 tool result 到首行
- **Layer 3**：**未实现**。代码注释 (`agent.py:354`): `"Layer 3: Progressive summary not yet implemented"`

### 问题

百万 token 特殊会话中，即使 Layer 2 压缩了 tool result，消息 100+ 的历史仍然占大量 token。LLM 存在 "lost-in-the-middle" 问题——超长上下文中的中间信息容易被模型忽略。

### 设计

```
┌─────────────────────────────────────┐
│ Layer 1: 最近 20 条消息 — 完整原文    │  ← 最高保真度
├─────────────────────────────────────┤
│ Layer 2: 20-100 条 — 压缩版          │  ← 去除 tool result 细节
│          保留 user 消息 + assistant   │     仅保留首行摘要
│          最终回复 + tool 调用名称      │
├─────────────────────────────────────┤
│ Layer 3: 100+ 条 — 渐进式摘要 (NEW)   │  ← 每 30 条生成 200 字摘要
│          追加到 system prompt 末尾     │     由 LLM 异步维护
└─────────────────────────────────────┘
```

### Layer 3 摘要内容

- 关键决策和结论
- 已解决的问题
- 未完成的任务
- 用户偏好变化

### 实现策略

**异步摘要生成 + 同步读取**

```
触发: 特殊会话消息数达到 100 + 每增加 30 条消息
  ↓
异步: LLM 生成/更新渐进式摘要（不阻塞用户交互）
  ↓
持久化: 摘要写入 {session}/summary.json
  ↓
读取: 下次对话时注入 to system prompt（不消耗 context 位置）
```

**摘要 Prompt**:
```
Based on the conversation below, generate a 200-word summary covering:
1. Key decisions made
2. Tasks completed  
3. Tasks not yet done
4. User preferences observed

Keep it concise. This summary will replace earlier messages in the context window.

Previous summary: {existing_summary}
New messages (30): {recent_30_messages}
```

### 与 `/压缩会话` 的关系

这是两个不同的机制：

| | Layer 3 渐进式摘要 | `/压缩会话` 命令 |
|------|------|------|
| 触发 | 自动（每 30 条） | 用户手动 |
| 粒度 | 追加式（保留旧摘要） | 全量替换 |
| 目的 | 维持长期记忆 | 紧急释放上下文 |

两者互补：Layer 3 自动维护日常记忆，`/压缩会话` 在上下文接近模型上限时由用户或系统触发一次性深度压缩。

---

## Idea 7: 配额柔性处理 — 三级策略

> 来源：`next_step.md` §8.3，与 PLAN.md Feature 5（80% 提醒）互补

### 当前状态：**仅硬拦截 100%**

当前 `UserWorkspaceManager.check_quota()` 在达到 100% 配额时硬拒绝。PLAN.md Feature 5 补充了 80% 提醒，但 >100% 的柔性处理未涉及。

### 问题

`execute_code` 生成的图表、`download_repo` 克隆的仓库大小不可预知。写入前无法精确检查，但超额后又不应粗暴中断用户操作（尤其是正在执行中的重要任务）。

### 设计

```
写入前: 快速估算当前使用量
  ├─ < 80% 配额  → 正常写入，无提示
  ├─ 80-100%     → 正常写入，回复末尾追加提醒（见 Feature 5）
  └─ > 100%      → 仍允许写入（不中断操作），标记 _over_quota

下次对话开始: 检查 _over_quota
  └─ 注入提醒到 system prompt → Agent 主动告知用户清理
```

### 防止滥用的措施

- 连续超额：连续 3 次超额后，拒绝新写入直到用户清理
- 超额上限：最多允许超额到 150% 配额（绝对硬限制）
- `download_repo` 工具增加 `--depth=1` 浅克隆
- `execute_code` 输出目录设 100MB 软限制

### 与现有 PLAN.md 功能的重叠

| PLAN.md Feature | 对应 | 新增内容 |
|------|------|------|
| Feature 5 80% 提醒 | 三级策略的前两级 | 柔性超额 + 下次对话提醒 |
| Feature 2 `delete_workspace_file` | 用户清理手段 | 不变 |

> 建议将 Feature 5 和 Idea 7 合并实现：80% 提醒 + 100% 柔性超额 + 150% 硬限制。

---

## Summary (Updated)

| # | Idea | Status | Action |
|:-:|------|:------:|--------|
| 1 | 临时会话文件迁移 | 架构上已消除 | 无需改动 |
| 2 | 两种创建方式 | 已实现 | 无需改动 |
| 3 | 15+ 条消息升级建议 | **未实现** | 待实现（方案 A：augmented message 注入） |
| 4 | 85% 上下文压缩提示 | **未实现** | 待实现（token 估算 + `/压缩会话` 命令） |
| 5 | 群聊文件延迟下载 | **未实现** | 待实现（元数据记录 + 按需下载 + 进度反馈） |
| 6 | 分层上下文 Layer 3 | **未实现** | 待实现（每 30 条异步生成渐进式摘要） |
| 7 | 配额柔性处理 | **未实现** | 待实现（三级策略：80%提醒 / 100%柔性 / 150%硬限） |

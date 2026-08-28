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

### 当前状态：**已实现（方案 A）**

不新建独立工具，改为扩展 `get_user_info` 的目录快照（`agent_router.py`）：

- ✅ 树形结构 + 每节点文件大小，目录优先、按名称字母序（`_build_workspace_tree()`）
- ✅ 跳过 `.git`/缓存目录/`.pyc` 文件（`_ws_skip()`）
- ✅ 80% 配额警告横幅
- ✅ 路径脱敏：以 `USER_DATA_ROOT` 为根返回相对路径（如 `2578260985/workspace/`），不暴露绝对路径

> 备注：`get_user_info`（注册于 `permissions.py:49`）原本已含「工作区目录快照」，与本 Feature 高度重合，故采用方案 A 合并实现，避免工具语义重复。

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

### 当前状态：**已实现**

`delete_workspace_file` 已在 `tools/builtin_tools.py` 定义并注册（`_build_tool_registry()`），加入 `_PUBLIC_TOOLS`（所有用户）。配套：
- `agent/quota_cleanup.py`（纯函数）：候选枚举 `list_candidates`、按 mtime 删除 `execute_cleanup`、清理响应解析 `resolve_targets`。
- `agent/workspace.py` 新增 `list_files_by_mtime()`。
- `agent_router.py` 配额清理协议（弹性超限 → 列出最早 mtime 文件 → 用户可自定义但不可跳过 → 10 分钟超时自主删除）。
- 测试：`test_workspace.py` 新增 4 套（删除行为、候选枚举、删除至配额内、响应解析）。

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

### 当前状态：**已实现**

实现见 `docs/feature3-session-file-provenance.md`。`SpecialSessionManager` 新增 `add_file()`；`files` 存于 `_index.json` 的 `metadata.files`（非 `_meta.json`）；`delete()` 删会话时直接删除会话级文件（`uploads`/`output`），保留 `repos/` 仓库并返回删除摘要供上层通知用户。

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

### 当前状态：**未实现**

`/删除会话` 已有 60 秒二次确认（`agent_router.py:1915-1943`），但无文件列表展示与 `--with-files` 选项。

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

### 当前状态：**已实现**

实现见 `docs/feature5-6-7-workspace-management.md`。

- ✅ `check_quota()` / `get_quota_context()` 改用每角色配额（`_effective_quota_bytes()` 读 `_current_quota_bytes` contextvar，回退固定默认值）
- ✅ `agent_router.py` 新增 `_quota_warning()`；上传后（纯文件 ack + agent 回复两路径）注入 80% 容量警告
- ✅ `/新会话` 与 `/保存为会话` 创建后追加容量警告（仅提醒，不阻止）
- ✅ `get_user_info` 快照 ≥80% 横幅（Feature 1 已有，无需改动）

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

### 当前状态：**已实现**

`/管理工作区` 经 `_handle_session_command()` 显式 `return False` 落入 agent（`agent_router.py`），AGENTS.md 引导其调用 `get_user_info` 展示快照并引导清理；`/帮助` 列表已补条目。

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

## Feature 7: 自然语言驱动的自主工作区管理

### 当前状态：**已实现**

底层 CRUD 已齐备（`get_user_info` 快照 + `delete_workspace_file`）。`AGENTS.md` 已扩充「工作区删除与配额清理」：`/管理工作区` 引导 + 批量删除确认 / 模糊条件询问 / 拒绝解释规则。

### Purpose

让智能体把用户的一句自然语言（如「帮我把 uploads 里超过 50MB 的旧文件删掉」「清理工作区」「我有哪些 PDF」）自主拆解为对底层增删改查工具的调用序列，无需用户手动记住 `/管理工作区` 等命令。

### 依赖

| 能力 | 对应工具 | 状态 |
|------|---------|------|
| 增 | QQ 文件上传 → `_download_and_save_file()` | ✅ 已有 |
| 查 | `get_workspace_snapshot`（或 `get_user_info` 目录快照） | ⚠️ 见 Feature 1 |
| 删 | `delete_workspace_file` | ❌ Feature 2 未实现 |
| 改 | （不提供，与设计原则一致） | — |

### Behavior

智能体（Agent）在收到工作区管理意图时，自主编排工具：

1. **理解意图**：解析自然语言，提取目标（哪些文件 / 哪个目录 / 什么条件）
2. **查**：调用快照工具获取当前状态
3. **决策**：根据条件（大小、后缀、时间、目录）筛选出候选文件
4. **确认**：对批量删除等不可逆操作，先列出清单请用户确认（除非用户已明确「直接删」）
5. **删**：调用 `delete_workspace_file` 逐个执行
6. **反馈**：汇总删除结果 + 释放空间

### Agent 侧交互引导（AGENTS.md 新增）

- 批量删除前必须展示清单 + 请求确认（不可逆操作）
- 用户条件模糊（如「旧文件」）时，先展示候选再询问，不擅自删除
- 删除后主动汇报释放了多少空间、剩余配额
- 遇到路径穿越/非空目录等被拒绝的操作，向用户解释原因

### 与 Feature 6 的关系

Feature 6 的 `/管理工作区` 命令是「显式入口」，本 Feature 是「隐式能力」——两者殊途同归。用户既可以直接发 `/管理工作区`，也可以用自然语言描述意图；智能体都应落到同一套 CRUD 工具上。

---

## Files to Modify

| 文件 | 变更 |
|------|------|
| `QQBot/tools/builtin_tools.py` | 新增 `delete_workspace_file()`, `_check_quota_warning()`（`get_workspace_snapshot` 已并入 `get_user_info`，见 Feature 1） |
| `QQBot/plugins/agent_router.py` | 修改 `/删除会话` 确认流程（Feature 4）；增加文件上传后的配额检查（Feature 5）；添加 `/管理工作区` 路由（Feature 6）；在文件下载后记录会话归属（Feature 3） |
| `QQBot/agent/special_session.py` | 新增 `add_file()`, `get_files()`, `remove_file()` 方法；`_meta.json` 新增 `files` 字段 |
| `QQBot/agent/permissions.py` | `get_workspace_snapshot` 和 `delete_workspace_file` 加入 `_ALL_USER_TOOLS` 集合 |
| `QQBot/agent/context.py` | 新增 `_current_quota_bytes` contextvar，供配额检查使用 |
| `QQBot/agent/config/AGENTS.md` | 新增工作区自主管理交互引导规则（Feature 7：批量删除确认、模糊条件询问等） |

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

### 当前状态：**已实现（方案 B）**

**文件系统层面已隔离（无需迁移）**：所有文件从上传那一刻起就写入用户专属工作区 `{USER_DATA_ROOT}/{user_id}/workspace/`，无论是临时会话还是特殊会话。关键代码：

- `agent_router.py:1301-1303` — 每条消息处理前设置 `_current_user_workspace` contextvar
- `agent_router.py:887-903` — `_record_session_file()` 将新写入文件归属到活动特殊会话

**被掩盖的缺口**：`/保存为会话`（`agent_router.py:2202-2261`）只复制最近 20 条**消息**，不迁移临时会话期间上传的文件归属。而 `_record_session_file()` 在无活动特殊会话时直接 no-op（`active is None → return`），导致临时会话上传的文件无归属记录；`/保存为会话` 后新会话 `metadata.files` 为空，日后 `/删除会话` 时这些文件变成孤儿、无法随会话清理。

### 方案（选定：方案 B）

**方案 B — 显式跟踪临时会话文件（推荐）**

- 新增模块级缓存 `_temp_session_files: dict[str, set[str]]`（`user_id` → workspace 相对路径集合）
- 改造 `_record_session_file()`：无活动特殊会话时，把相对路径记入 `_temp_session_files[user_id]`（而非直接丢弃）
- `/保存为会话` 复制消息后：`for rel in _temp_session_files.pop(user_id, set()): _special_sessions.add_file(user_id, session.name, rel)`
- 清理点：`/clear`（`_handle_special_command`）、`/新会话`（`_handle_session_command`）时 `_temp_session_files.pop(user_id, None)`

**方案 A — 从消息文本解析（备选）**：扫描 `/保存为会话` 复制的 `[用户上传了文件 X，已保存至: <abs_path>]` 文本，正则提取路径后 `add_file`。无新状态，但依赖消息格式、脆弱，故弃用。

### 实现要点

| 文件 | 变更 |
|------|------|
| `QQBot/plugins/agent_router.py` | 新增 `_temp_session_files`；`_record_session_file()` 无活动会话时记录；`/保存为会话` 迁移归属；`/clear`、`/新会话` 清理 |

> 边界：`add_file` 幂等，同一文件多会话引用互不影响；临时会话 30 分钟超时未保存，文件仍保留在工作区（本就是用户持久资产），`_temp_session_files` 残留引用由下次 `/clear`/`/新会话` 或 `/保存为会话` 的 `pop` 兜底清理。

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

### 当前状态：**已实现（方案 A）**

### 问题

用户在临时会话中连续讨论同一话题，发送 15 条以上消息时，智能体不会主动建议升级。用户可能不知道 `/保存为会话` 功能，导致上下文积累在临时会话中、无法持久化。

临时会话 `Session.context` 上限为 20 条（`session.py:85` `max_context_messages=20`，`trim()` 在 `session.py:49`），每轮追加 user + assistant 两条（`agent.py:281-282`），tool 消息不落库。故「15 条」≈ 7~8 轮，为上限的 75%，阈值合理。

### 设计（选定：方案 A）

**触发条件**：`session_type == "temporary"` 且 `message_count(user_id) >= 15`，在调用 `agent.run()` 前注入提示指令。

**注入点**：`agent_router.py:1536`（`agent.run()` 调用前）；`session_type` 已在 `1489-1490` 判定。

**注入内容**（追加到 `augmented_message` 前缀）：

```
[系统提示] 当前临时会话已累积 {n} 条消息，接近 20 条上限，超出后早期上下文会丢失。若用户当前话题明确且可能继续，请在回复末尾用一句话自然建议其发送「/保存为会话 <名称>」持久化这段对话。
```

**限流**：`_upgrade_hint_last: dict[str, int]`（user_id → 上次提示时的消息数），仅当 `n - last >= 10` 时再次提示。

**方案 B（备选，弃用）**：agent 回复后、发送前追加提示文本——零 token、零污染，但硬插在回复末尾，体验不如 A 自然。

### 实现要点

| 步骤 | 文件 | 变更 |
|------|------|------|
| 1. 消息计数 | `agent/session.py` | 新增 `message_count(user_id)`，返回 `len(context)` 或 0 |
| 2. 常量与限流状态 | `plugins/agent_router.py` | 新增 `UPGRADE_HINT_THRESHOLD=15`、`UPGRADE_HINT_INTERVAL=10`、`_upgrade_hint_last` |
| 3. 注入逻辑 | `plugins/agent_router.py` | `agent.run()` 前，`session_type=="temporary"` 且满足阈值+限流时，前缀注入提示 |

> 边界：仅临时会话触发（特殊会话已持久化；群聊连续模式走独立代码路径 `1714+`，不受影响）；提示作为 user 消息落入 20 条 `context` 会被自然 trim，且限流控制频率，可接受。

---

## Idea 4: 上下文长度达到 85% 时提示压缩

### 当前状态：**部分实现（仅 Layer 1+2 截断）— 待严密设计，暂不实现**

> ⚠️ 智能体的记忆系统（上下文压缩 / 摘要 / token 管理）需要严密设计后再动手。本 Idea 当前只做现状核查与设计预留，**不进入实现**。

### 问题

特殊会话的 `context` 列表无限增长（`special_session.py:260-288` `add_message()` 无长度限制）。当消息累积到接近模型上下文窗口上限时，API 调用会失败或返回截断结果。目前没有主动的上下文长度监控或压缩提示。

### 现状核查（2026-08-15）

**已存在：`_compress_context()`（`agent/agent.py:409-443`）** —— 一个 `@staticmethod`，仅在 `_build_messages()`（`agent.py:396`）对特殊会话 context 做请求前预处理：

- **Layer 1**：最近 20 条消息保留完整原文
- **Layer 2**：20 条之前的 `tool` 结果截断到首行（前 200 字符）
- **Layer 3**：渐进式摘要 —— **未实现**（代码注释 `agent.py:415`：`"Layer 3: Progressive summary not yet implemented"`）

其关键性质（对 Idea 4 的定位很重要）：

1. **无 token 感知**：只数消息条数（固定 20 条），不估算 token，不知道上下文窗口多大
2. **不可调用**：不是注册工具，也无 `/压缩会话` 命令，用户无法触发
3. **不落盘、不写回**：只对 context 副本操作并返回新列表，持久化 context 仍无限增长
4. **只在读时压缩**：每次请求临时压一下，存储层无任何压缩状态

**不存在的部分（Idea 4 需从零实现）**：

| 能力 | 状态 |
|------|------|
| `/压缩会话` 命令 | ❌ 无（AGENTS.md 命令表无此项） |
| token 估算函数 | ❌ 无任何实现 |
| 85% 阈值 / `_needs_compression` 标志 | ❌ 无 |
| 摘要生成逻辑（除 PDF 摘要工具外） | ❌ 无 |
| 上下文窗口上限读取 | ❌ 无 |

**模型配置现状**（`config/models_settings.json`）：

| 模型 | model | `max_tokens` |
|------|-------|-------------|
| REASONING | `deepseek-v4-pro` | 409600 |
| FLASH | `deepseek-v4-flash` | 102400 |
| MULTIMODAL | `qwen3.6-plus` | 20480 |

⚠️ 上述 `max_tokens` 是**输出上限**，非上下文窗口（输入上限）；`model_router.py` / `deepseek_client.py` 均未读取该字段，配置中亦无 `context_window` 字段。

**「无限记忆」（特殊会话）的真实状态**：

- 存储：快照 + 增量双层（`special_session.py` `SNAPSHOT_INTERVAL=50`），`add_message()` 无限追加，无长度限制、无 token 计数、无压缩标志
- 读取：仅 `_compress_context()` Layer 1+2 截断，且不写回
- 结论：「百万 token」（DOCUMENTATION.md:256）目前只是乐观描述，无任何机制保证 context 不超窗口

### 设计（初稿，待严密设计）

**上下文窗口上限获取**：
- reasoning model（`deepseek-v4-pro`）：从 `models_settings.json` 新增 `context_window` 字段，或 API `/models` 端点获取 `max_input_tokens`
- flash model（`deepseek-v4-flash`）：同上
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
| 1 | 临时会话文件迁移 | 已实现（方案 B） | 文件系统隔离 + `/保存为会话` 归属迁移 |
| 2 | 两种创建方式 | 已实现 | 无需改动 |
| 3 | 15+ 条消息升级建议 | 已实现（方案 A） | `message_count` + augmented message 注入 + 限流 |
| 4 | 85% 上下文压缩提示 | 部分实现（Layer 1+2 截断） | 待严密设计，暂不实现（token 估算 + `/压缩会话`） |
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

### 当前状态：**部分实现（80% 提醒已落地，柔性超额未实现）**

- ✅ **80% 提醒**已实现：`check_quota()` (`workspace.py:93-121`) 在 80-100% 返回警告；`get_quota_context()` (`workspace.py:131-147`) 在 80%/100% 注入系统提示（`agent.py:354`）
- ❌ **>100% 柔性超额**未实现：`check_quota()` 仍硬拒绝（返回 `False`），未采用「仍允许写入 + 标记 `_over_quota`」的柔性策略
- ❌ `check_quota()` 目前**未被任何工具调用**（见 `docs/security-boundary-remaining.md`），实时配额检查未接入上传/代码执行等写入路径
- ❌ 150% 绝对硬限、连续 3 次超额拒绝、`download_repo --depth=1` 浅克隆、`execute_code` 输出 100MB 软限制均未实现

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

## Idea 8: 基于截图多模态的测速功能

> 来源：用户需求 2026-05-30

### 当前状态：**已实现（OCR 层为 qwen3.5-ocr，2026-08-21 全量验证通过）**

完整实现记录见 `docs/implements-for-idea-8.md`。落地要点：

- ✅ `tools/battle_parser.py::parse_battle_screenshots(paths)` — 每张截图**一次** qwen3.5-ocr 调用（左半屏 0–50% 裁剪，名字+行动值按行交错返回），行配对（y 聚类 + 孤儿值按行序回填）、横幅颜色带扫描判定阵营（红=敌/蓝=我，横向计数窗口 13–45% 帧宽）、`(name, side)` 去重（镜像同名行保留）、每侧上限 3。阶段自动判定（`_detect_phase`）：全员行动值 ≤5% → `pre`（乱速后），否则 `post`；双图顺序异常（如疑似颠倒）写入 warnings。
- ✅ `tools/ocr_name_matcher.py` 字形纠偏层保留（Stroke-IoU + 分长度阈值 + 长名截断救援），uncertain 名称走 MULTIMODAL_MODEL 视觉兜底；名称索引动态取自 wiki 缓存 + vendored 别名词典。
- ✅ 跑条前截图三规则校验（进战 buff / 进战战意集中力 / 免疫套装 → `pre_valid`）+ 拉条推条技能提示（`action_gauge_skills`），技能索引来自 `character_details.json`；`BuffDetector` 28 图标模板匹配。
- ✅ 输出单图/双图 JSON + `calculate_speed` 兼容 `raw_format`；工具注册于 `agent_router.py`，`_PUBLIC_TOOLS` 全员可用；`AGENTS.md` 截图测速交互流程（pre_valid 检查、行动值修正确认、询问我方速度 → `calculate_speed`）。
- ✅ 验证（`test/test_region_ocr.py` → `test/swap_validation.json`，49 截图 / 285 真值行）：**名称 285/285、行动值 285/285、漏行 0、阵营 unknown 0**；`bash test.sh` 通过。报告中 6 个「额外」均为真实镜像行（GT 构建时继承旧管线去重 bug 未录入，非误报）。
- 演进：初版 easyOCR 四区域 + 逐数字模板（名称 87.7% / 值 93.7%）→ qwen3.5-ocr 对比评测胜出（`test/qwen_ocr_comparison.md`）→ 残余错误根因探针（`test/qwen_fullimg_probe.md`：截断=裁剪、漏行=窄裁剪跳行、茱/茉=字形相似由 matcher 纠偏、2%→3%=列表同化、98% 漏值=坐标回归）→ 宽裁剪单调用替换，easyOCR 提取 / `_parse_action_value` / 逐数字模板退役。

### 问题

当前测速功能 (`calculate_speed`) 要求用户手动键入完整的战斗数据文本（角色名 + 初始行动值 + 当前行动值 + 速度），格式严格、输入繁琐。用户已经能上传游戏截图，如果能让多模态模型从截图中直接提取行动值，则只需再提供一两个我方速度值即可完成测速。

### 设计

**核心思路**：新增独立工具 `parse_battle_screenshots`，接收 1~2 张截图路径，调用多模态模型提取结构化的角色行动值数据，输出 `calculate_speed` 兼容格式。

```
用户上传 1~2 张截图 + "@Roxy 测速"
  │
  ▼
agent_router 下载图片到 workspace/uploads/
  │
  ▼
Agent 调用 parse_battle_screenshots(["…/screen1.png", "…/screen2.png"])
  │
  ├── 对每张截图调用 multimodal_client.analyze_image(path, prompt=ONE_SHOT_PROMPT)
  │     └── One-shot 提示词让模型输出严格 JSON: {characters: [{name, side, action_value}]}
  ├── 解析多模态模型返回的 JSON → 我方/敌方 × 初始/当前行动值
  ├── 两图合并: 图1=初始值, 图2=结束值（单图则只有当前状态）
  └── 返回 calculate_speed 兼容的 battle_data 文本
  │
  ▼
Agent 展示提取结果给用户 → 用户确认 + 提供我方角色速度
  │
  ▼
Agent 调用 calculate_speed(battle_data) → 返回测速结果
```

### 两层降级方案：OCR 提取 + LLM 结构化（推荐）

游戏 UI 截图是固定的——角色排列在单一行动轴上，仅靠背景颜色区分阵营（红=敌方，蓝=我方），没有复杂的自然场景。直接调用多模态大模型处理这类格式化截图存在两个问题：
1. 多模态模型处理高分辨率截图时 token 消耗巨大（一张 1080p 截图可达数千 tokens），对普通/会员用户的 LLM 额度不友好
2. 格式化 UI 截图用 OCR 提取文字 + 颜色采样判定阵营更精准、更便宜

因此引入两层降级方案：

```
截图 → OCR 提取文字 + 像素颜色采样 → 结构化数据
         │
         ├── 成功 → 返回结构化 JSON（无需调用 LLM）
         │
         └── 失败/置信度低 → 降级调用 multimodal LLM
```

**第一层：OCR + 像素颜色采样**

1. **OCR 提取**：用 OCR 引擎识别截图中的所有文字块，返回 `[(text, bbox), ...]`（文字内容 + 包围框坐标）
2. **行分组**：根据包围框的 Y 坐标对文字块进行行聚类（同一行 Y 坐标相近）
3. **阵营判定（颜色采样）**：对每行文字的包围框区域采样背景像素颜色：
   - 红色系背景 → 敌方
   - 蓝色系背景 → 我方
   - 采样策略：取文字包围框左右两侧各 10px 的像素中位数，避免文字本身的干扰
4. **角色名-行动值配对**：同一行内，颜色判定阵营后，角色名与行动值按 X 坐标从左到右排列
5. **结构化输出**：生成与多模态方案相同的 JSON 格式，无缝对接 `calculate_speed`

**第二层：多模态 LLM 降级**

OCR 失败时（截图模糊、文字重叠、非标准 UI），降级调用 `multimodal_client.analyze_image()` 用 one-shot prompt 提取。

### 服务器可行性分析：OCR 在 4核4GB 上的运行

**结论：可行，推荐 EasyOCR 轻量模式或 PaddleOCR 移动版。**

| OCR 引擎 | 内存占用 | CPU 推理速度 | 安装复杂度 | 推荐 |
|---------|:------:|:----------:|:--------:|:---:|
| **EasyOCR** (轻量) | ~500MB | 单张 2~5s | `pip install easyocr` | ✅ 推荐 |
| **PaddleOCR** (mobile 模型) | ~200MB | 单张 1~3s | 需要 PaddlePaddle | ⚠️ |
| **Tesseract** | ~50MB | 单张 0.5~1s | `apt install tesseract-ocr` + 中文语言包 | ⚠️ 中文准确率低 |
| **多模态 LLM** | 0 (API 调用) | 取决于网络 | 无需安装 | ✅ 降级方案 |

**推荐策略**：优先使用 EasyOCR（`easyocr.Reader(['ch_sim', 'en'], gpu=False)`），纯 CPU 推理，首次加载模型后常驻 ~500MB 内存。4核 CPU 足够处理每次测速的 1~2 张截图（2~10s），不会影响 bot 响应其他消息。

**内存管理**：OCR 模型采用懒加载 + 引用计数：
- 只在首次调用 `parse_battle_screenshots` 时加载模型
- 加载后常驻（bot 运行期间测速是高频功能，反复加载反而浪费 CPU）
- 如果内存紧张，可提供 `/卸载ocr` 命令手动释放，下次调用时自动重新加载

### 为什么是独立工具而非扩展 read_file


- `read_file` 使用通用描述 prompt（"请描述这张图片"），不适合提取结构化数据
- 独立工具的 one-shot prompt 可以针对游戏 UI 精细调优
- 需要调 1~2 次模型并合并两张截图的结果，`read_file` 不支持
- 避免污染 `read_file` 的通用语义

### One-Shot 提示词策略

提示词需要包含：
1. 任务描述（"提取每个角色的行动值"）
2. 严格的 JSON 输出格式
3. 我方/敌方判断规则（如左侧/右侧、颜色等视觉特征）
4. 示例（one-shot example）
5. 防幻觉规则（"只提取可见的，不编造"）

提示词需根据目标游戏的实际 UI 布局调整。需要先用几组不同游戏截图测试多模态模型的提取准确率，迭代优化提示词。

### Agent 侧交互引导

在 `AGENTS.md` 中新增：
- 调用 `parse_battle_screenshots` → 表格展示提取结果 → 请求确认 → 询问速度 → 调用 `calculate_speed`
- 单截图缺少初始值时提示用户补充
- 识别到数据异常（角色数量不符、数值明显错误）时主动询问

### 容错设计

| 场景 | 处理 |
|------|------|
| 多模态未配置 | 返回配置指引，引导用户使用文字格式 |
| JSON 解析失败 | 返回原始输出，让 Agent 尝试自行解析 |
| 截图模糊 | 模型在 `notes` 字段说明困难，Agent 请求更清晰截图 |
| 两图角色名不匹配 | Agent 列出差异，请用户手动对应 |
| 单截图（仅当前值） | 标记缺少初始值，Agent 提示用户补充 |

### Files to Modify

| 文件 | 变更 |
|------|------|
| `QQBot/tools/builtin_tools.py` | 新增 `parse_battle_screenshots(paths)`，实现两层降级逻辑 |
| `QQBot/lib/ocr_engine.py` | 新增 OCR 引擎封装（懒加载 EasyOCR + 像素颜色采样阵营判定） |
| `QQBot/plugins/agent_router.py` | `_build_tool_registry()` 注册新工具 |
| `QQBot/agent/permissions.py` | 加入 `_PUBLIC_TOOLS`（基础功能，所有用户可用） |
| `QQBot/agent/config/AGENTS.md` | 新增截图测速交互引导规则 |
| `QQBot/agent/config/TOOLS.md` | 新增工具文档 |
| `requirements.txt` | 新增 `easyocr` 依赖 |

### 像素颜色采样阵营判定 — 详细设计

游戏截图中的行动轴背景颜色是唯一的敌我区分特征（红=敌方，蓝=我方）。OCR 只提取文字，无法感知颜色。因此需要独立的颜色采样步骤：

```
OCR 文字块: (text="Boss名称", bbox=[x1, y1, x2, y2])
                   │
                   ▼
    采样 bbox 左侧 10px、右侧 10px 区域像素
    （left_region: [x1-10, y1, x1, y2], right_region: [x2, y1, x2+10, y2]）
                   │
                   ▼
    计算左右区域像素中位数的 RGB 值
                   │
                   ▼
    RGB 色相判定:
      H ∈ [0°, 20°] ∪ [340°, 360°] → RED   → 敌方
      H ∈ [200°, 260°]               → BLUE  → 我方
      else                           → 无法判定 → 标记 unknown，后续 LLM 降级
```

**为什么采样左右两侧而非整行背景**：
- 文字块内部像素被文字颜色干扰
- 左右侧紧邻文字块，高度匹配，且游戏 UI 中角色行的背景色会延伸到文字区域之外
- 取左右两侧中位数而非均值，避免个别噪点影响

**为什么用 HSV 色相而非 RGB 阈值**：
- 游戏可能使用不同的红/蓝色调（深红/浅红、深蓝/浅蓝），但色相角基本稳定
- 红色色相角固定：H ∈ [0°, 20°] ∪ [340°, 360°]
- 蓝色色相角固定：H ∈ [200°, 260°]
- 对 UI 主题变化（亮色/暗色模式）更鲁棒

### 多尺度输入适配

不同设备产生的截图分辨率差异巨大（720p 手机 ~ 4K 平板），游戏 UI 布局固定但像素尺寸随设备缩放。需要统一的尺度归一化策略。

**核心问题**：OCR 引擎对文字像素高度敏感。EasyOCR 的检测模型在文字高度 20~40px 时表现最佳；低于 15px 时漏检率急剧上升，高于 60px 时推理变慢且无收益。

**解决方案：基于文字高度的自适应缩放**

```
原始截图 (任意分辨率)
  │
  ▼
快速文字检测 (EasyOCR 仅检测，不识别)
  → 获取所有候选文字框的中位数高度 H_med
  │
  ├── H_med < 18px → 放大至 H_med ≈ 24px → 再执行完整 OCR
  ├── 18px ≤ H_med ≤ 60px → 无需缩放，直接 OCR
  └── H_med > 60px → 缩小至 H_med ≈ 40px → 再执行完整 OCR（节省推理时间）
```

**为什么用中位数而非平均值**：中位数不受个别异常检测框（如 UI 图标被误检为文字、超大字号的标题行）影响。

**缩放实现细节**：

| 步骤 | 操作 |
|------|------|
| 1. 轻量检测 | `reader.detect(img)` 仅运行检测头，速度约完整 OCR 的 30%，返回所有候选文字框 |
| 2. 计算目标比例 | `scale = 24 / H_med`（或 `40 / H_med` 对于大图） |
| 3. 高质量缩放 | `cv2.resize(img, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)` — 放大用三次插值保留细节 |
| 4. 完整 OCR | 在归一化后的图像上运行 `reader.readtext(img)` |
| 5. 坐标还原 | 将 OCR 返回的 bbox 坐标除以 scale，映射回原始截图坐标（用于颜色采样） |

**颜色采样坐标的处理**：像素颜色采样必须在**原始截图**上进行，而非缩放后的图像。流程调整为：

```
原始截图 ──┬── 缩放 → OCR 提取文字块 + bbox
           │              │
           │              └── bbox 坐标 ÷ scale 还原
           │                         │
           └── 原始截图 + 还原后的 bbox ──→ 像素颜色采样 → 阵营判定
```

**极端情况处理**：

| 情况 | 判断条件 | 处理 |
|------|---------|------|
| 极低分辨率（文字 < 10px） | `H_med < 10` | 放大后仍不可靠 → 跳过 OCR，直接降级多模态 LLM |
| 极高分辨率（> 4K） | `max(img.shape) > 4096` | 先缩小至 4K 再做检测，避免 OOM |
| 窗口模式/局部截图 | 检测到的文字块 < 4 个 | 可能是非标准 UI → 降级多模态 LLM |
| 缩放后与原始比例差异大 | `scale > 3.0` | 警告用户截图质量过低，建议更换截图 |

**测速流程与注意事项：**

测速的方法可以见测速函数"calculate_speed"。本质需要从图片中提取敌我双方角色的名字及两组行动值（跑条前与跑条后），锁定每个角色，计算跑条前后行动值的差值 $\alpha_1、 \alpha_2、 \alpha_3 $（我方）与 $\beta_1、 \beta_2、 \beta_3$（敌方）。
随后根据角色特殊的技能，如己方拉条、对敌推条等，对该差值进行修正，得到仅由速度引起的行动值差（即角色在本次跑条中纯靠速度跑了多少行动值）。

得到仅由速度属性引起的行动值差序列后，可以根据其比例关系，在给定至少一名我方角色速度 $v_1$ 时，根据花费时间相等这一条件： $\frac{\alpha_2}{v_2} = \frac{\alpha_1}{v_1} = \frac{\alpha_3}{v_3} =\frac{\beta_1}{w_1} = \frac{\beta_2}{w_2} = \frac{\beta_3}{w_3}$，推导出敌方角色的速度 $w_1、w_2、w_3$。

该问题的难点在于——如何修正行动值差？对于游戏玩家而言，哪些角色触发了拉条效果或推条效果是可由观察与经验得到的，而对于机器而言，行动值截图中并不会存在这一信息。
因此，可以想到的一个解决方案是：从官方wiki中爬取角色信息，获取角色技能描述储存于本地，从而判断提供的行动值截图中所示的角色是否触发了有关行动值的特殊效果。但该方案依赖于LLM的推理能力，且有可能出错。
官方wiki及其网页schema可以参照抽卡功能使用到的wiki_scraper.py。

另一个解决方案是：从官方wiki中爬取角色信息，获取角色技能描述储存于本地，将角色分为技能与行动值有关与无关两类，计算时只挑选技能与行动值无关的角色。但该方案的缺点很明显，无法保证计算的可行性。

**关于图像信息提取：**

行动值截图一般由一个中央框与背景构成，背景可能千变万化、中央框有一定的透明度（很小）。最重要的信息展示在中央框中，包括：角色头像、血条、状态、buff以及最重要的角色名与行动值。

行动值截图分为3种：无效的跑条前截图、有效的跑条前截图、跑条后截图（跑条后截图几乎总是有效）。无效的跑条前截图都是截图时间过早，还未完成乱速（在进入战斗的最开始阶段，会给所有角色随机分配一个“起跑线”，不同角色会被随机分配0.0%~5.0%的初始行动值，这个过程叫做乱速）的时候截下的图。我们的目标是识别有效的截图并提取信息。

跑条前截图的有效无效判断依赖于跑条后截图与角色技能信息。"QQBot/images/cal-speed-data"目录下存放了命名好的一些截图对，命名规则如下：
- “xx-1”表示第xx对跑条前截图；
- “xx-2”表示第xx对跑条后截图；
- 跑条前截图的括号中注明了其是否无效；
- 跑条后截图的括号中注明了其是否受到行动值相关技能的影响。 

跑条前截图的无效判断依据有以下几点： 
- 当乱速已经正常完成（截图有效），拥有进入战斗立即获得buff的技能的角色（以下简称进战buff角色）会在截图中看到其对应的buff。若观察到对应buff或角色不存在相应技能，返回True，否则False。
- 当乱速已经正常完成，拥有进入战斗立即获得战意/集中力状态的角色会在截图中看到对应的战意/集中力。若观察到战意/集中力或角色不存在相应技能，返回True，否则返回False。
- 当乱速已经正常完成，会在截图中看到穿戴免疫套装的角色的免疫buff的标识。判断角色是否穿戴免疫套装可以根据跑条后的截图是否显示了对应于该角色的免疫buff，如果跑条后截图显示该角色有免疫buff，且该角色不存在获取免疫buff的技能，则可以确定免疫buff来源于角色穿戴的免疫套装，此时跑条前截图该角色有免疫buff则返回True，否则返回False。

当且仅当以上三条判断返回全为True，跑条前截图有效，否则无效。

跑条后，正在行动的角色行动值会归零，且其代表的行动条位于中间框的最上方，行动值最大的角色代表的行动条将位于中间框的底部，且被Next On条与其它角色行动条上下分开。注：角色行动值大于等于100%时，将会行动，且所有角色不再因速度属性跑条（任可受到拉条），直到行动完成后。若同时有多名角色行动值大于等于100%，按行动值大小决定行动次序。

---

## Summary (Updated)

| # | Idea | Status | Action |
|:-:|------|:------:|--------|
| 1 | 临时会话文件迁移 | 已实现（方案 B） | 文件系统隔离 + `/保存为会话` 归属迁移 |
| 2 | 两种创建方式 | 已实现 | 无需改动 |
| 3 | 15+ 条消息升级建议 | 已实现（方案 A） | `message_count` + augmented message 注入 + 限流 |
| 4 | 85% 上下文压缩提示 | 部分实现（Layer 1+2 截断） | 待严密设计，暂不实现（token 估算 + `/压缩会话`） |
| 5 | 群聊文件延迟下载 | **未实现** | 待实现（元数据记录 + 按需下载 + 进度反馈） |
| 6 | 分层上下文 Layer 3 | **未实现** | 待实现（每 30 条异步生成渐进式摘要） |
| 7 | 配额柔性处理 | **部分实现** | 80% 提醒已实现（`check_quota`/`get_quota_context`）；>100% 柔性超额与 150% 硬限待实现 |
| 8 | 截图测速（OCR+LLM 两层降级） | **已实现** | qwen3.5-ocr 单调用提取 + 字形名称纠偏 + 颜色带阵营判定 + 三规则校验；名称/值 285/285 全对（见 `docs/implements-for-idea-8.md`） |

# Special Sessions & Per-User Workspace — Implementation Plan

## 1. Overview

为每个 QQ 用户提供至多 3 个**特殊会话**（持久化、百万 token 上下文），以及独立的**用户工作区**（文件隔离）。同时，将当前的会话机制规范为**临时会话**（短生命周期）。新增**硬件自动检测**替代 WORKSPACE.md 中的硬编码硬件信息。

### Core Concepts

```
┌─────────────────────────────────────────────────────────┐
│                      QQ 消息流入                          │
├─────────────────────────────────────────────────────────┤
│  Router: 特殊命令? → SessionCommandRouter                 │
│         特殊会话中? → Agent (完整上下文 + 用户工作区)        │
│         普通消息 → Agent (临时会话 + 受限上下文)             │
└─────────────────────────────────────────────────────────┘
```

| 会话类型 | 生命周期 | 上下文限制 | 持久化 | 触发方式 |
|---------|---------|-----------|--------|---------|
| **临时会话** | 30 分钟超时 | 20 条消息 | 仅内存 | 默认（所有消息） |
| **特殊会话** | 手动结束/无超时 | 不裁剪 (~百万 token) | 磁盘 JSONL | `/新会话` 命令 |
| **连续对话** (已规划) | 5 分钟窗口 | 正常消息 | 仅内存 | 群聊 @后自动激活 |

---

## 2. .env 新增配置

```env
# 用户数据根目录（存放所有用户工作区、特殊会话、硬件信息）
# 建议放在项目目录之外，独立数据盘
USER_DATA_ROOT=/data/qqbot/users

# 每用户最大特殊会话数 (默认 3)
MAX_SPECIAL_SESSIONS=3

# 每用户工作区磁盘配额 (默认 500MB)
USER_WORKSPACE_QUOTA_MB=500
```

---

## 3. 新增文件

### 3.1 `QQBot/agent/hardware.py` — HardwareProfile

**职责**: 首次启动时自动检测物理机性能，缓存到 `{USER_DATA_ROOT}/.hardware.json`。后续启动直接读取缓存。

```python
@dataclass
class HardwareProfile:
    cpu_cores: int
    memory_gb: float
    disk_system_gb: float      # 系统盘
    disk_data_gb: float         # 数据盘
    has_gpu: bool
    gpu_info: Optional[str]
    os_info: str
    detected_at: str            # ISO timestamp
    cpu_model: str

class HardwareDetector:
    def __init__(self, cache_path: str):
        ...

    def detect(self) -> HardwareProfile:
        """运行 shell 命令检测硬件（nproc, free, df, uname, lscpu）"""

    def load_or_detect(self) -> HardwareProfile:
        """优先读缓存，缓存不存在则检测并写入"""

    def get_prompt_context(self) -> str:
        """生成注入 system prompt 的硬件信息块"""
```

**检测命令**（使用现有 `shell_exec` 白名单中的命令）:
- `nproc` → CPU 核数
- `free -h` → 内存
- `df -h` → 磁盘（筛选 `/` 和 `/data`）
- `uname -a` → 操作系统
- `lscpu | grep "Model name"` → CPU 型号（如果 lscpu 可用）
- GPU 检测: `ls /dev/nvidia* 2>/dev/null` 或检查 `lspci | grep -i vga`

**缓存文件** `{USER_DATA_ROOT}/.hardware.json`:
```json
{
  "cpu_cores": 2,
  "cpu_model": "Intel Xeon Platinum 8255C",
  "memory_gb": 4.0,
  "disk_system_gb": 50.0,
  "disk_data_gb": 50.0,
  "has_gpu": false,
  "gpu_info": null,
  "os_info": "Linux 6.6.114.1-microsoft-standard-WSL2",
  "detected_at": "2026-05-27T12:00:00"
}
```

### 3.2 `QQBot/agent/special_session.py` — SpecialSessionManager

**职责**: 管理特殊会话的创建、切换、重命名、删除、持久化。

```python
@dataclass
class SpecialSession:
    session_id: str            # UUID
    user_id: str               # QQ 号
    name: str                  # 用户自定义名称 / LLM 自动生成
    context: List[dict]        # 完整对话历史（不裁剪）
    created_at: float
    last_active: float
    total_messages: int
    metadata: dict             # {first_topic, tags, summary}

class SpecialSessionManager:
    def __init__(self, user_data_root: str, max_per_user: int = 3):
        ...

    # ── CRUD ──
    def create(self, user_id: str, name: str = None) -> SpecialSession:
        """创建特殊会话。如果 name 为空，先用占位名，后续由 LLM 总结命名。"""

    def list_sessions(self, user_id: str) -> List[SpecialSession]:
        """列出用户的所有特殊会话（名称、创建时间、消息数）。"""

    def get_active(self, user_id: str) -> Optional[SpecialSession]:
        """获取用户当前活跃的特殊会话。"""

    def switch_to(self, user_id: str, session_name: str) -> SpecialSession:
        """切换到指定特殊会话。自动存档当前会话。"""

    def rename(self, user_id: str, old_name: str, new_name: str):
        """重命名特殊会话。"""

    def delete(self, user_id: str, session_name: str):
        """删除特殊会话及其持久化文件。"""

    def add_message(self, user_id: str, role: str, content: str, reasoning: str = None):
        """追加消息到当前活跃的特殊会话。"""

    # ── Persistence ──
    def _save(self, session: SpecialSession):
        """保存到 {USER_DATA_ROOT}/{user_id}/sessions/{name}.jsonl"""

    def _load(self, user_id: str, session_name: str) -> SpecialSession:
        """从 JSONL 文件加载"""

    def _auto_name(self, user_id: str, first_message: str, first_response: str) -> str:
        """使用 LLM 总结首次交互，自动生成会话名称（≤12 字）"""
```

**持久化格式 — JSONL**（每行一条消息，适合百万 token 场景的增量写入）:

```jsonl
{"role":"system","content":"..."}
{"role":"user","content":"帮我分析这个项目的架构"}
{"role":"assistant","content":"好的，让我来分析...","reasoning_content":"..."}
{"role":"tool","tool_call_id":"call_xxx","content":"..."}
{"role":"assistant","content":"分析结果如下..."}
```

**索引文件** `{USER_DATA_ROOT}/{user_id}/sessions/_index.json`:
```json
{
  "active_session": "项目架构讨论",
  "sessions": [
    {
      "name": "项目架构讨论",
      "session_id": "uuid-1",
      "created_at": 1716796800.0,
      "last_active": 1716800400.0,
      "total_messages": 42,
      "metadata": {"first_topic": "QQBot 架构分析"}
    }
  ]
}
```

### 3.3 `QQBot/agent/workspace.py` — UserWorkspaceManager

**职责**: 管理每个用户的独立工作区，提供路径隔离和安全验证。

```python
class UserWorkspaceManager:
    def __init__(self, user_data_root: str, quota_mb: int = 500):
        ...

    def get_workspace(self, user_id: str) -> str:
        """返回用户工作区路径: {USER_DATA_ROOT}/{user_id}/workspace/"""

    def get_size(self, user_id: str) -> int:
        """返回用户工作区当前占用字节数"""

    def check_quota(self, user_id: str, additional_bytes: int = 0) -> bool:
        """检查是否超过配额"""

    def ensure_dirs(self, user_id: str):
        """创建用户目录结构"""
```

**用户目录结构**:
```
{USER_DATA_ROOT}/
  .hardware.json                    # 硬件信息缓存（全局）
  {qq_id}/
    profile.json                    # 用户画像（从 data/users/ 迁移过来）
    sessions/
      _index.json                   # 会话索引
      项目架构讨论.jsonl             # 特殊会话 1
      游戏脚本开发.jsonl             # 特殊会话 2
    workspace/                      # 用户文件工作区
      code/                         # 代码执行临时文件
      uploads/                      # QQ 上传的文件
      output/                       # 生成的文件（图表等）
      projects/                     # 用户项目文件
```

### 3.4 `QQBot/agent/config/SESSION.md` — 更新

新增特殊会话的使用说明：
```markdown
## Session Types

### Temporary Session (默认)
- 每次发送消息自动创建/续期
- 30 分钟无活动自动清除
- 上下文限制: 最近 20 条消息

### Special Session (特殊会话)
- 通过 `/新会话 [名称]` 创建
- 最多 3 个，无超时限制
- 上下文不裁剪，支持百万 token 级别对话
- 自动持久化到用户工作区
- 首次交互自动总结命名（如未指定名称）
```

---

## 4. 修改文件

### 4.1 `QQBot/plugins/agent_router.py` — 会话命令路由

新增 `SessionCommandRouter` 处理所有会话管理命令：

| 命令 | 功能 | 示例 |
|------|------|------|
| `/新会话 [名称]` | 创建特殊会话 | `/新会话 项目讨论` |
| `/会话列表` | 列出所有特殊会话 | `/会话列表` |
| `/切换会话 <名称>` | 切换到指定会话 | `/切换会话 项目讨论` |
| `/重命名会话 <旧名> <新名>` | 重命名 | `/重命名会话 项目讨论 架构分析` |
| `/删除会话 <名称>` | 删除（需确认） | `/删除会话 项目讨论` |
| `/结束会话` | 退出特殊会话，回到临时会话 | `/结束会话` |
| `/临时会话` | 同上 | `/临时会话` |

处理流程：
```python
# 在 handle_agent_message() 之前执行
async def _handle_session_command(event, text: str) -> bool:
    """处理会话管理命令。返回 True 表示已处理，不用进入 Agent。"""
    cmd, *args = text.split(maxsplit=1)
    
    if cmd == "/新会话":
        name = args[0] if args else None
        _special_sessions.create(user_id, name)
        if name is None:
            # 等待首次交互后自动命名
            _pending_naming[user_id] = True
        return True
    
    if cmd == "/会话列表":
        sessions = _special_sessions.list(user_id)
        # 格式化返回
        return True
    
    # ... 其他命令类似
```

需要特别处理的逻辑：
- `/新会话` 不带名称 → 标记 `_pending_naming`，在首次 Agent 回复后异步调用 LLM 生成名称
- `/切换会话` → 保存当前会话上下文，加载目标会话，更新 system prompt
- `/删除会话` → 先发送确认提示，用户回复 "确认" 后执行删除

### 4.2 `QQBot/agent/agent.py` — Agent 核心改造

**A. 新增构造参数**:
```python
def __init__(
    self,
    ...,
    special_session_manager: Optional[SpecialSessionManager] = None,
    user_workspace_manager: Optional[UserWorkspaceManager] = None,
    hardware_profile: Optional[HardwareProfile] = None,
):
    self.special_sessions = special_session_manager
    self.workspaces = user_workspace_manager
    self.hardware = hardware_profile
```

**B. `run()` 方法改造** — 接受额外的 `session_type` 参数:
```python
async def run(
    self,
    user_message: str,
    user_id: str,
    session_type: str = "temporary",  # "temporary" | "special" | "continuous"
    ...
) -> str:
```

**C. `_build_messages()` 改造**:
```python
def _build_messages(self, session, user_message, session_type="temporary"):
    messages = []
    
    # 1. System prompt (包含硬件信息 + 会话类型标记)
    system_content = self.build_system_prompt()
    
    # 2. 会话类型上下文
    if session_type == "special":
        ss = self.special_sessions.get_active(user_id)
        system_content += f"\n\n## 当前特殊会话\n名称: {ss.name}\n工作区: {self.workspaces.get_workspace(user_id)}\n会话消息数: {ss.total_messages}\n\n你处于特殊会话模式，拥有长期上下文。可以使用用户工作区存储文件。如果任务已完成，可以建议用户使用 /结束会话 退出。"
    
    # 3. Hardware context (替换硬编码的 WORKSPACE.md §4)
    if self.hardware:
        system_content += "\n\n" + self.hardware.get_prompt_context()
    
    messages.append({"role": "system", "content": system_content})
    
    # 4. User profile
    ...
    
    # 5. History
    if session_type == "special":
        # 加载完整特殊会话上下文（不裁剪）
        special_session = self.special_sessions.get_active(user_id)
        messages.extend(special_session.context)
    else:
        # 临时会话：裁剪到最近 N 条
        messages.extend(session.context)
    
    # 6. Current message
    messages.append({"role": "user", "content": user_message})
    
    return messages
```

**D. 回复后持久化**:
```python
# 在 agent.run() 返回前
if session_type == "special":
    self.special_sessions.add_message(user_id, "user", user_message)
    self.special_sessions.add_message(user_id, "assistant", final_content, reasoning)
    self.special_sessions._save(user_id)  # 增量追加到 JSONL
elif session_type == "temporary":
    session.add_message("user", user_message)
    session.add_message("assistant", final_content, reasoning)
    session.trim(self.sessions.max_context_messages)
    self.sessions.update(user_id, session)

# Profile update (common to both types)
self._schedule_profile_update(user_id, user_message, final_content)
```

### 4.3 `QQBot/agent/session.py` — 会话系统区分

`SessionManager` 明确更名为 "临时会话管理器"，增加注释说明与 `SpecialSessionManager` 的关系：

- `SessionManager` → 管理临时会话（30 分钟超时，20 条消息限制）
- `SpecialSessionManager` → 管理特殊会话（持久化，百万 token）
- 两者的 `user_id` 命名空间共享，但存储位置不同

### 4.4 `QQBot/tools/builtin_tools.py` — 工具作用域适配

**问题**: `execute_code`, `shell_exec`, `read_file` 等工具的当前工作区是全局共享的 `WORKSPACE_ROOT`。需要改为：根据当前用户，自动限定到用户专属工作区。

**方案**: 通过 `contextvars.ContextVar` 传递当前用户的工作区路径（类似现有的 `_send_msg`）:

```python
# agent/context.py 新增
_current_user_workspace: contextvars.ContextVar[Optional[str]] = (
    contextvars.ContextVar("_current_user_workspace", default=None)
)
```

`agent_router.py` 在处理消息时设置:
```python
_current_user_workspace.set(workspaces.get_workspace(user_id))
```

`builtin_tools.py` 的工具读取:
```python
def _get_workspace_root() -> str:
    from agent.context import _current_user_workspace
    user_ws = _current_user_workspace.get()
    if user_ws:
        return user_ws
    return _default_workspace_root()
```

**注意**: `shell_exec` 的 `cwd` 也需要设定为用户工作区。`_validate_path()` 的安全边界更新为用户工作区。

### 4.5 `QQBot/agent/config/WORKSPACE.md` — 动态硬件信息

将 §4 "Server Hardware" 从静态文本改为**动态注入**：

- `WORKSPACE.md` 保留任务分类规则（必须拒绝 / 可执行但有警告 / 安全执行）
- 具体的硬件规格数字不再硬编码在 WORKSPACE.md 中
- 改为由 `HardwareProfile.get_prompt_context()` 在 bootstrap 时动态生成，追加到 system prompt

### 4.6 `QQBot/agent/profile.py` — 画像存储迁移

`ProfileManager` 的存储路径从 `data/users/` 迁移到 `{USER_DATA_ROOT}/{user_id}/profile.json`。

向后兼容：如果旧路径存在画像文件，自动迁移。

---

## 5. Bootstrap 序列更新

`agent.py` 的 `bootstrap()` 方法新增步骤：

```python
async def bootstrap(self) -> Dict[str, Any]:
    status = {...}  # existing checks

    # 5. Hardware detection (NEW)
    try:
        if self.hardware_detector:
            self.hardware = self.hardware_detector.load_or_detect()
            status["hardware"] = {
                "cpu_cores": self.hardware.cpu_cores,
                "memory_gb": self.hardware.memory_gb,
                "detected_at": self.hardware.detected_at,
            }
    except Exception as e:
        status["errors"].append(f"Hardware detection: {e}")

    # 6. User workspace initialization (NEW)
    try:
        if self.workspaces:
            self.workspaces.ensure_root_dirs()
            status["user_data_root"] = self.workspaces.user_data_root
    except Exception as e:
        status["errors"].append(f"Workspace init: {e}")

    return status
```

---

## 6. 数据流

```
QQ Message
  │
  ├─ CommandRouter (priority=0)
  │   检查 /新会话, /切换会话, /会话列表, /删除会话, /结束会话 等
  │   命中 → 直接处理并回复，不进入 Agent
  │
  ├─ AgentRouter (priority=1, to_me())
  │   │
  │   ├─ 特殊会话模式?
  │   │   ├─ 用户消息追加到 SpecialSession.context (JSONL)
  │   │   ├─ Agent.run(session_type="special")
  │   │   │   ├─ System prompt + Hardware context + Special session marker
  │   │   │   ├─ Full context (no trim, from JSONL)
  │   │   │   ├─ Tools scoped to user workspace
  │   │   │   └─ Return response
  │   │   ├─ 持久化 assistant 回复到 JSONL
  │   │   ├─ 后台: _maybe_remember() + _schedule_profile_update()
  │   │   └─ Send response
  │   │
  │   └─ 临时会话模式 (默认)
  │       ├─ Agent.run(session_type="temporary")
  │       │   ├─ Standard system prompt + Hardware context
  │       │   ├─ Trimmed context (20 messages)
  │       │   ├─ Tools scoped to shared workspace
  │       │   └─ Return response
  │       ├─ Session 更新 + 裁剪 + 持久化
  │       ├─ 后台: _maybe_remember() + _schedule_profile_update()
  │       └─ Send response
  │
  └─ ContinuousRouter (priority=2, no to_me()) [已规划]
      群聊连续对话处理
```

---

## 7. 安全与隔离

| 维度 | 机制 |
|------|------|
| **工作区隔离** | `contextvars` 传递当前用户 workspace 路径，`_validate_path()` 强制边界检查 |
| **路径穿越防护** | 同现有逻辑（拒绝 `..`、`~`、绝对路径越界） |
| **磁盘配额** | `UserWorkspaceManager.check_quota()` 在文件写入前检查 |
| **会话数量限制** | `SpecialSessionManager.create()` 检查 `len(sessions) < MAX_SPECIAL_SESSIONS` |
| **命令注入防护** | 会话管理命令在 Router 层硬编码匹配，不传递给 Agent 做 NLP 理解 |
| **硬件感知拒绝** | System prompt 注入实时硬件信息，Agent 据此拒绝高负载任务 |

---

## 8. 设计深化 — 七项改进

以上为基础设计方案。以下七项改进解决性能、体验和可靠性方面的深层问题。

### 8.1 快照 + 增量双层存储（替代纯 JSONL）

**问题**: 纯 JSONL 追加快，但每次加载特殊会话都需要完整解析全部行来重建 `context` 列表。百万 token 级别（约 3000-5000 条消息）时，加载延迟不可接受。

**方案**: 双层存储结构：

```
{user_id}/sessions/
  项目讨论/
    snapshot_00050.json     ← 第 1-50 条消息的完整快照
    snapshot_00100.json     ← 第 1-100 条消息的完整快照
    delta.jsonl             ← 快照之后的增量消息
```

- 每 50 条消息生成一份完整 JSON 快照
- 快照之后的新消息追加到 `delta.jsonl`
- 加载流程: 读最新快照 (一次 `json.load`) + 读 `delta.jsonl` (少量行) + 合并
- 新快照生成后，旧的快照和 delta 均可删除
- `_index.json` 记录 `latest_snapshot_seq` 和 `delta_count`

**性能对比**:

| 场景 | 纯 JSONL | 快照 + 增量 |
|------|---------|------------|
| 加载 5000 条消息 | 解析 5000 行 JSONL (~2s) | 解析 1 个快照 + ~50 行 JSONL (~100ms) |
| 追加 1 条消息 | 1 行追加 (~1ms) | 1 行追加 (~1ms) |
| 生成快照 | N/A | 后台异步，不阻塞 (~500ms) |

### 8.2 分层上下文（软裁剪）

**问题**: 百万 token 上下文虽好，但 LLM 存在 "lost-in-the-middle" 问题——超长上下文中的中间信息容易被模型忽略。无条件保留所有原始消息并非最优策略。

**方案**: 特殊会话内部采用三层上下文结构：

```
┌─────────────────────────────────────┐
│ Layer 1: 最近 20 条消息 — 完整原文    │  ← 最高保真度
├─────────────────────────────────────┤
│ Layer 2: 20-100 条 — 压缩版          │  ← 去除 tool result 细节
│          保留 user 消息 + assistant   │     仅保留首行摘要
│          最终回复 + tool 调用名称      │
├─────────────────────────────────────┤
│ Layer 3: 100+ 条 — 渐进式摘要         │  ← 每 30 条生成 200 字摘要
│          追加到 system prompt 末尾     │     由 LLM 自动维护
└─────────────────────────────────────┘
```

- Layer 3 摘要由 Agent 在回复用户后**异步生成**，不阻塞交互
- 摘要内容: 关键决策、已解决的问题、未完成的任务、用户偏好变化
- 用户感知到 "无限记忆"，但实际 token 消耗保持可控
- 摘要作为 system prompt 的一部分注入，不消耗 context 位置

### 8.3 工作区配额的柔性处理

**问题**: `execute_code` 生成的图表、`download_repo` 克隆的仓库大小不可预知。在写入前精确检查配额不现实，但超额后又不应粗暴中断用户操作。

**方案**: 三级策略：

```
写入前: du -sb 快速估算当前使用量（du 已在 shell_exec 白名单）
        ├─ 当前用量 < 80% 配额 → 正常写入，无提示
        ├─ 当前用量 80-100% 配额 → 正常写入，回复末尾追加提醒
        └─ 当前用量 > 100% 配额 → 正常写入（允许小幅超额），
           下次对话开头主动提醒用户清理

写入后: 异步检查是否超额
        └─ 超额 → 标记 _over_quota 标志，下次 Agent.run() 开始时注入提醒
```

- 对 `download_repo` 工具增加 `--depth=1` 浅克隆
- 对 `execute_code` 输出目录设 100MB 软限制

### 8.4 自动命名的即时反馈

**问题**: `/新会话` 无名称时，仅靠后台 LLM 异步命名。用户在收到第一条回复时看到的仍是 "未命名会话"，体验有延迟。

**方案**: 两步命名法：

```
Step 1 (同步, ~0ms):
  规则化临时名称 = "{日期} {用户首条消息截取前6字}"
  示例: "0527 帮我分析一下这个"
  → 立即写入 _index.json，用户马上可见

Step 2 (异步, 首次回复后):
  LLM 总结首次交互 → 提炼 ≤12 字精炼名称
  示例: "QQBot 架构分析"
  → 后台重命名，更新 _index.json 和目录名
  → 下次 /会话列表 显示精炼名称
```

- 用户始终能看到一个可辨识的名称，即使 LLM 命名延迟或失败
- 如果 LLM 命名失败（超时、API 错误），保留规则化名称作为永久名称

### 8.5 实时系统负载工具

**问题**: 静态硬件信息（CPU 核数、内存总量）不足以为实时决策提供依据。服务器可能此刻正在执行其他用户的重任务，即使硬件规格允许也应拒绝。

**方案**: 新增 `get_system_load` 工具，Agent 可在决策前调用：

```python
# tools/builtin_tools.py 新增
async def get_system_load() -> str:
    """返回实时系统负载信息。
    数据来源: /proc/loadavg, free, df (均在 shell_exec 白名单)。
    """
```

**返回格式**:
```
系统实时负载:
  CPU: 1分钟负载 0.45 / 5分钟 0.32 / 15分钟 0.28 (2核)
  内存: 已用 2.1 GB / 总计 3.8 GB (55%)
  数据盘: 已用 23 GB / 总计 50 GB (46%)
  活跃会话: 3 个特殊会话, 12 个临时会话
  评估: 负载较轻，可正常执行任务
```

**Agent 使用场景**:
- 用户要求处理大数据集 → Agent 先调用 `get_system_load` 检查可用内存
- 用户要求批量生成图表 → Agent 先检查磁盘空间
- 多个用户同时请求重任务 → Agent 根据负载自主排队或拒绝

此工具与静态 `HardwareProfile` 互补：静态信息定义能力上限，实时信息决定当前是否有余力。

### 8.6 删除会话的安全确认

**问题**: `/删除会话` 需要确认机制防止误操作，但异步聊天中简单的 "回复确认" 可能被其他消息打断或误触发。

**方案**: 一次性确认码：

```
用户: /删除会话 项目讨论
Bot:  ⚠️ 确认删除特殊会话「项目讨论」？
      此操作不可撤销，会话中的所有上下文将被永久删除。
      请回复「确认删除 项目讨论」来执行。

用户: 确认删除 项目讨论
Bot:  ✅ 已删除特殊会话「项目讨论」。
      会话文件: {path} 已移除。
      当前特殊会话: 1/3
```

**安全特性**:
- 确认语句必须与目标会话名称**完全匹配**
- 确认码有效期为 **60 秒**，超时自动取消
- 聊天中的 "确认" 等泛词不会触发删除
- 不同会话的确认码互不干扰

### 8.7 临时会话升级为特殊会话

**问题**: 用户在临时会话中讨论了很多有价值的内容后，可能决定 "这段对话值得保存"。当前设计无法将临时会话的上下文迁移到特殊会话。

**方案**: 新增 `/保存为会话 <名称>` 命令：

```
用户: /保存为会话 架构设计讨论
Bot:  ✅ 已将当前临时会话（最近 15 条消息）保存为特殊会话「架构设计讨论」。
      现在处于特殊会话模式，后续对话将持续保存。
      当前特殊会话: 2/3
```

**实现细节**:
- 复制当前临时会话的 `context` 列表到新的 `SpecialSession`
- 消息数 ≤ 20（临时会话的裁剪上限），所以不会超过快照阈值
- 如果用户已在特殊会话中，此命令无效（提示先 `/结束会话`）
- 可选: 复制时附带用户画像上下文作为第一条 system 消息，让新会话继承用户背景

---

## 9. 实现顺序（更新）

| Phase | 内容 | 依赖 |
|-------|------|------|
| **Phase 1** | `HardwareProfile` — 硬件检测 + 缓存 | 无 |
| **Phase 2** | `UserWorkspaceManager` — 用户工作区 + `contextvars` 适配 | Phase 1 (目录结构) |
| **Phase 3** | `SpecialSessionManager` — 特殊会话 CRUD + 快照/增量存储 | Phase 2 |
| **Phase 4** | Router 命令 — `/新会话`、`/切换会话`、`/保存为会话` 等 | Phase 3 |
| **Phase 5** | `Agent.run()` 改造 — `session_type` 参数 + 分层上下文 | Phase 3, 4 |
| **Phase 6** | 工具作用域适配 — `execute_code`/`shell_exec`/`read_file` 限定用户工作区 | Phase 2 |
| **Phase 7** | `get_system_load` 工具 + `HardwareProfile` 动态注入 | Phase 1 |
| **Phase 8** | `ProfileManager` 迁移 — 画像存储移到新的用户目录 | Phase 2 |
| **Phase 9** | `WORKSPACE.md` 重构 — 硬件规格动态化 + 分层上下文规则 | Phase 1, 5 |
| **Phase 10** | 测试 + 文档更新 | 全部 |

---

## 10. 验证清单

- [ ] 硬件检测: 首次启动生成 `.hardware.json`，再次启动读缓存
- [ ] 创建特殊会话: `/新会话 项目讨论` → 创建成功 → `/会话列表` 可见
- [ ] 自动命名: `/新会话` (无名称) → 规则化临时名称立即可见 → 后台 LLM 精炼
- [ ] 会话切换: `/切换会话 <名称>` → 上下文正确加载
- [ ] 百万 token 上下文: 在特殊会话中连续对话 100+ 轮，快照 + 增量加载 <200ms
- [ ] 分层上下文: 100+ 条历史消息自动触发渐进式摘要
- [ ] 临时会话: 正常对话 → 30 分钟无活动 → 上下文清除
- [ ] 临时升级: `/保存为会话 <名称>` → 当前临时上下文复制到新特殊会话
- [ ] 工作区隔离: 用户 A 无法访问用户 B 的 workspace
- [ ] 磁盘配额: 三级策略（80%提醒/100%限制写入/超额异步提醒）
- [ ] 实时负载: `get_system_load` 返回 CPU/内存/磁盘实时数据
- [ ] 删除确认: `/删除会话 <名称>` → 一次性确认码 → 60 秒超时
- [ ] 画像联动: 特殊会话和临时会话的消息都能更新用户画像
- [ ] 硬件感知: Agent 基于实时硬件信息 + 静态上限共同判断任务拒绝
- [ ] 向后兼容: 现有测试通过
- [ ] 群聊连续对话: 与特殊会话独立运作（特殊会话是用户全局的）

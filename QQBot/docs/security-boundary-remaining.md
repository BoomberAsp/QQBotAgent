# 安全边界待优化条目

已实施的 6 项代码层修复（详见 `bug-report-special-session-workspace.md`）：

- [x] 移除 shell_exec 白名单中的 `python3 -c`
- [x] web_fetch 增加 SSRF DNS 解析检查
- [x] 记忆系统按 user_id 隔离存储
- [x] execute_code 增加 AST 白名单第二层校验
- [x] check_quota 超额硬拒绝
- [x] 全部工具调用审计日志（JSONL）

---

## 一、代码执行沙箱加固

### 1.1 execute_code 进程级资源限制

**现状**：仅通过 `timeout` 限制执行时间，对内存和 CPU 无约束。恶意或意外代码可以耗尽服务器 4GB 内存。

**方案**：使用 `resource` 模块或 `prlimit` 在 subprocess 启动时设置内存上限（如 256MB）和 CPU 时间硬限制。

```python
# resource.setrlimit 示例
import resource
resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024, -1))  # 256MB 虚拟内存
```

**优先级**：高。服务器只有 4GB 内存，OOM 会导致整个 bot 进程被 kill。

---

### 1.2 AST 检查器：getattr 绕过

**现状**：`getattr` 在安全内置函数白名单中，但可用于构造动态属性访问绕过 AST 检查：

```python
getattr(__builtins__, 'ev' + 'al')('1+1')      # 绕过 eval 禁止
getattr(__builtins__, '__im' + 'port__')('os')  # 绕过 __import__ 禁止
```

**方案**：将 `getattr` 从 `_AST_SAFE_BUILTINS` 移入 `_AST_BLOCKED_BUILTINS`，或增加对 `getattr(__builtins__, ...)` 模式的专项检测。

**优先级**：中。需要较强的 Python 知识才能利用，但一旦利用即可完全逃逸沙箱。

---

### 1.3 临时目录清理不保证

**现状**：`execute_code` 在 `finally` 块中清理临时工作目录，但如果进程被 SIGKILL 或系统崩溃，临时文件会残留。

**方案**：
- 定时任务清理超过 1 小时的 `exec_*` 临时目录
- 或使用 `tempfile.TemporaryDirectory` 配合 `atexit` 注册清理

**优先级**：低。磁盘空间充裕（50GB+50GB），且正常情况下 finally 会执行。

---

## 二、网络安全

### 2.1 web_fetch：DNS 重绑定攻击

**现状**：`_check_ssrf()` 在请求前做一次 DNS 解析并检查 IP。攻击者可以设置 TTL=0 的 DNS 记录，在检查时返回公网 IP，在 httpx 实际连接时返回内网 IP（DNS rebinding）。

**方案**：
- 方案 A：使用 `httpx.AsyncHTTPTransport` 的子类，在建立连接后再次校验 socket 的对端 IP
- 方案 B：使用 `socket.create_connection` 自定义连接逻辑，连接前先解析并校验 IP，然后用该 IP 直接连接（跳过 httpx 的 DNS）

**优先级**：中。需要攻击者控制 DNS 解析，在多数场景下较难触发，但一旦触发即可完全绕过 SSRF 防护。

---

### 2.2 web_fetch：HTTP 重定向到内网

**现状**：`web_fetch` 使用 `follow_redirects=True`。如果公网 URL 返回 302 重定向到 `http://127.0.0.1/`，httpx 会跟随重定向，绕过 SSRF 检查。

**方案**：
- 使用自定义 `httpx.AsyncHTTPTransport`，在每次重定向前对目标 URL 调用 `_check_ssrf()`
- 或设置 `follow_redirects=False`，手动处理重定向并逐个校验

**优先级**：高。这是比 DNS rebinding 更简单直接的 SSRF 绕过方式。

---

### 2.3 web_fetch：响应内容类型校验

**现状**：对 `content-type` 仅用于判断文本/HTML/JSON 分支，不校验是否匹配预期。服务端可能返回 `Content-Type: text/plain` 但实际内容是可执行文件。

**方案**：无直接安全风险（内容仅被读取和展示，不执行），但可增加响应体大小上限的精细控制（按 content-type 区分上限）。

**优先级**：低。

---

## 三、访问控制

### 3.1 缺少请求频率限制

**现状**：同一用户可在短时间内发起大量工具调用（搜索、代码执行、网页抓取），无任何速率限制。可能导致：
- SearXNG 上游被 IP 封禁
- 服务器资源被单个用户耗尽
- 审计日志爆炸增长

**方案**：在 `_execute_tool_calls()` 或 agent router 层增加基于 `(user_id, tool_name)` 的滑动窗口限流器，如每分钟最多 10 次搜索、每分钟最多 5 次代码执行。

**优先级**：高。直接关系到服务可用性。

---

### 3.2 工具权限未分级

**现状**：所有用户（包括群聊中任意成员）均可使用全部工具。`SUPERUSERS` 配置项存在但未用于工具权限控制。`shell_exec`、`execute_code`、`web_fetch` 等高风险工具对所有用户平等开放。

**方案**：
- `shell_exec`、`execute_code` 仅 SUPERUSERS 可用
- `web_fetch`、`download_repo` 登录用户可用（通过 profile 是否有记录判断）
- `search_web`、`get_time`、`read_file`、`summarize_pdf` 所有人可用

**优先级**：高。当前 Q 号就是唯一的身份凭证，群聊里任何人都能 @bot 执行代码。

---

### 3.3 用户消息大小无限制

**现状**：用户可发送任意长度的消息。超长消息可能导致 LLM token 消耗暴增、内存占用飙升。

**方案**：在 `agent_router.py` 的消息入口处截断超过 N 字符（如 8000 字符）的用户消息，并警告用户。

**优先级**：低。QQ 消息本身有长度限制，但通过文件上传可以绕过。

---

## 四、基础设施

### 4.1 Docker 容器以 root 运行

**现状**：bot 在 Docker 容器中以 root(sudo) 身份运行（见 `docker-compose.yml`）。一旦 execute_code 或 shell_exec 沙箱被绕过，攻击者拥有容器内完整 root 权限，可以修改容器文件系统、安装软件、发起对外攻击。

**方案**：
- 创建非 root 用户运行 bot（`USER 1000:1000`）
- 挂载工作区目录时确保权限正确
- 添加 `--read-only` 挂载除工作区外的所有路径
- 添加 `--security-opt no-new-privileges` 和 `--cap-drop=ALL`

**优先级**：中。需要配合宿主机部署调整，不能仅靠代码修改解决。

---

### 4.2 磁盘配额检查未接入工具调用链

**现状**：`UserWorkspaceManager.check_quota()` 已实现并可硬拒绝，但未被任何工具调用。`read_file` 读取文件前不检查配额，`execute_code` 生成图表写入 output 目录前也不检查。

**方案**：在以下路径接入配额检查：
- `execute_code`：检测到图表生成（`saved_images` 非空）时，调用 `check_quota(user_id, estimated_size)`
- `read_file`（图片分析场景）：不会写入文件，无需检查
- 后续如有文件上传/写入工具，一律接入

**优先级**：中。当前仅 `read_file` 和 `execute_code` 涉及文件操作，且 execute_code 生成的图表通常很小。

---

### 4.3 审计日志无轮转/保留策略

**现状**：审计日志写入 `data/audit/tool_calls_{date}.jsonl`，每日一个文件，永不删除。在高频使用下会持续占用磁盘空间。

**方案**：
- 定时清理超过 30 天的审计日志文件
- 或按总大小限制（如 500MB），超出时删除最旧文件
- 可通过 agent_router 启动时注册一个 asyncio 定时任务

**优先级**：低。每条日志约 300 字节，10 万次调用才 ~30MB。

---

### 4.4 HTTPS 证书校验未显式确认

**现状**：`web_fetch` 使用 httpx 的默认行为（`verify=True`），证书校验是开启的。`search_web` 使用 `urllib.request`，在 HTTPS URL 下证书校验也是默认开启的。但 `SEARXNG_ENDPOINT` 默认值是 `http://localhost:8082`（HTTP），部署在本地所以无中间人风险。

**方案**：无需改动。仅记录为已确认的安全属性。

---

## 优先级汇总

| 优先级 | 条目 |
|--------|------|
| **高** | 2.2 HTTP 重定向 SSRF 绕过 |
| **高** | 3.1 请求频率限制 |
| **高** | 3.2 工具权限分级 |
| **中** | 1.1 execute_code 进程内存限制 |
| **中** | 1.2 AST getattr 绕过 |
| **中** | 2.1 DNS 重绑定攻击 |
| **中** | 4.1 Docker 非 root 运行 |
| **中** | 4.2 配额检查接入工具调用链 |
| **低** | 1.3 临时目录清理不保证 |
| **低** | 2.3 响应内容类型校验 |
| **低** | 3.3 用户消息大小限制 |
| **低** | 4.3 审计日志轮转 |
| — | 4.4 HTTPS 证书校验（已确认安全） |

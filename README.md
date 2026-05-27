# QQBot Agent

基于 **NapCat + NoneBot2** 构建的智能 QQ 机器人，采用 **LLM Agent 架构**（Think → Act → Observe → Respond），支持工具调用、用户画像与长期记忆。

## 特性

- **智能体架构** — Markdown 配置驱动，OpenAI 兼容 Function Calling，最多 5 轮工具调用
- **多模型路由** — 轻量 FLASH 模型处理简单任务，强力 REASONING 模型处理复杂推理，MULTIMODAL 模型理解图片
- **流式交互** — 群聊连续对话模式：@一次后 5 分钟内免 @，消息自动续期
- **自托管搜索** — SearXNG 聚合搜索（Bing + 多引擎），无需外部搜索 API Key
- **代码执行** — 三层安全隔离（模式匹配 + `python3 -I` 隔离 + 资源限制）
- **文件阅读** — 支持文本 / PDF / 图片（多模态 AI 分析）
- **用户系统** — 长期记忆（Markdown 存储）+ LLM 驱动用户画像提取
- **游戏工具** — 抽卡模拟、战斗测速、乱速概率计算
- **地图服务** — 地址↔坐标转换、实时天气、POI搜索、路线规划（高德地图）
- **安全设计** — 工作区隔离、路径验证防穿越、Git URL 注入防护

## 技术栈

| 层 | 技术 |
|---|---|
| QQ 协议 | NapCat (NT QQ) |
| 机器人框架 | NoneBot2 + FastAPI |
| 协议适配 | OneBot V11 (反向 WebSocket) |
| AI 后端 | DeepSeek API + 多模型路由 |
| 搜索引擎 | SearXNG (Docker 自托管) |
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

编辑 `QQBot/.env`，填入你的配置：

```ini
DRIVER=~fastapi
HOST=0.0.0.0
PORT=8081
ONEBOT_ACCESS_TOKEN=你的Token
SUPERUSERS=["你的QQ号"]
DEEPSEEK_API_KEY=sk-xxxxxxxx
DEEPSEEK_API_BASE=https://api.deepseek.com
SEARXNG_ENDPOINT=http://localhost:8082
AMAP_API_KEY=你的高德Key  # 可选，用于地图工具
```

**多模型配置（可选）**：编辑 `QQBot/config/models_settings.json` 配置三种模型，留空则回退到 `.env` 默认配置。参考 `QQBot/config/models_settings_example.json` 格式。

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

应返回已注册的工具列表和机器人状态。

## 启动脚本

| 脚本 | 用途 |
|------|------|
| `setup.sh` | 一键安装所有依赖（系统库 + Python 环境 + SearXNG） |
| `start.sh` | 启动所有服务（SearXNG + NoneBot + NapCat 检查） |
| `stop.sh` | 停止所有服务（NoneBot + SearXNG） |
| `start_bot.sh` | 仅启动 NoneBot（SearXNG & NapCat 已运行时使用） |
| `test.sh` | 运行 38 项单元测试 |

## 目录结构

```
QQBotAgent/
├── setup.sh                # 一键安装脚本
├── start.sh / stop.sh      # 启动 / 停止脚本
├── test.sh                 # 测试脚本
├── bot.py                  # NoneBot 入口（备用）
├── docker-compose.yml      # Docker 编排 (SearXNG + QQBot + vLLM)
├── Dockerfile              # Docker 镜像构建
├── docker-entrypoint.sh    # Docker 容器入口
├── napcat.sh               # NapCat 安装脚本
├── vllm-start.sh           # vLLM 推理服务启动
├── searxng/                # SearXNG 搜索配置
│   └── settings.yml        #   搜索引擎配置 (Bing / 国内优化)
│
└── QQBot/                  # NoneBot 机器人主体
    ├── .env                # 环境变量（密钥 / 服务配置）⚠ git-ignored
    ├── requirements.txt    # Python 依赖
    ├── pyproject.toml      # NoneBot 项目配置
    ├── test_agent.py       # 测试套件（38 项测试）
    │
    ├── agent/              # 智能体核心
    │   ├── agent.py        #   主循环: Think→Act→Observe→Respond
    │   ├── tool_registry.py#   工具注册（OpenAI JSON Schema）
    │   ├── session.py      #   会话管理（per-user, 持久化）
    │   ├── continuous_session.py  # 群聊连续对话窗口（5分钟免@）
    │   ├── memory.py       #   长期记忆（Markdown 文件）
    │   ├── profile.py      #   用户画像（LLM 自动提取）
    │   └── config/         #   智能体配置（10 个 Markdown）
    │
    ├── plugins/            # NoneBot 插件
    │   └── agent_router.py #   ★ 统一消息入口（所有交互的唯一处理器）
    │
    ├── tools/              # Agent 工具实现
    │   ├── builtin_tools.py#   搜索 / 代码执行 / Git / PDF / 时间
    │   ├── file_tools.py   #   文件读取（文本 / PDF / 图片分析）
    │   └── legacy_tools.py #   游戏工具（抽卡 / 测速 / 翻译）
    │
    ├── lib/                # 库
    │   ├── deepseek_client.py   # DeepSeek API 客户端
    │   ├── model_router.py      # 多模型路由器
    │   └── multimodal_client.py # 多模态客户端（图片理解）
    │
    ├── config/             # 敏感配置 ⚠ git-ignored
    │   ├── models_settings.json         # 多模型配置
    │   └── models_settings_example.json # 配置模板
    │
    └── data/               # 运行时数据
        ├── sessions/       #   会话持久化
        ├── memory/         #   长期记忆
        ├── users/          #   用户画像
        └── workspace/      #   工作区（代码执行 / 仓库 / 上传 / 输出）
```

## 核心架构

### Agent 主循环

```
User Message → agent_router
  ├── 特殊命令 (/clear, /status) → 直接处理
  └── 自然语言
       ├── ModelRouter.classify_complexity(message)
       │     FLASH_MODEL → "simple" 或 "complex"
       ├── Build Messages (System + Profile + Memory + History)
       └── Think→Act→Observe→Respond 循环（最多 5 轮）
            ├── think: LLM 分析，决定是否调用工具
            ├── act: 执行工具（搜索 / 代码 / 文件等）
            ├── observe: 工具结果注入对话
            └── respond: 无工具调用时 → 回复用户
```

**关键参数**：`max_tool_iterations=5`, `thinking_timeout=180s`, `session_timeout=30min`

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
  └── 图片 → MULTIMODAL_MODEL（视觉理解）
```

配置在 `QQBot/config/models_settings.json`，留空自动回退到 `.env` 默认。

### 群聊连续对话

```
@Roxy "帮我分析数据"   → agent_router (to_me) → 自动开启 5 分钟窗口
"这个字段是什么" (无@)  → continuous_router       → Agent 处理 + 续期
"/取消" (无@)          → continuous_router       → 关闭窗口
5 分钟无消息            → 自动过期清理
```

## 核心类

### Agent (`agent/agent.py`)

```python
class Agent:
    # 构造
    def __init__(self, deepseek_client, tool_registry, config_dir,
                 session_manager=None, memory_system=None, profile_manager=None,
                 max_tool_iterations=5, thinking_timeout=180.0)

    # 核心方法
    async def run(self, user_message, user_id, client=None) -> str
    def build_system_prompt(self) -> str                      # SOUL + IDENTITY + AGENTS
    def get_status(self) -> dict                              # 运行状态
    def clear_user_session(self, user_id)                     # 清除会话
```

- `client` 参数支持运行时模型切换（ModelRouter 传入不同 `DeepSeekClient`）
- `run()` 返回最终回复文本，内部自动管理 Session / Memory / Profile

### DeepSeekClient (`lib/deepseek_client.py`)

```python
class DeepSeekClient:
    def __init__(self, api_key=None, api_base=None, model=None)
    async def chat_completion(self, message, history, timeout=180.0) -> str
    async def chat_completion_with_tools(self, messages, tools, timeout=180.0) -> dict
```

可选参数支持多实例（不同模型不同端点），用于 ModelRouter。

### ModelRouter (`lib/model_router.py`)

```python
class ModelRouter:
    async def classify_complexity(self, user_message) -> str   # "simple" | "complex"
    def get_client(self, task_type) -> DeepSeekClient           # triage/simple/complex/multimodal
    @property reasoning_client / flash_client / multimodal_client
```

### ToolRegistry (`agent/tool_registry.py`)

```python
class ToolRegistry:
    def register(self, name, func, description, parameters)    # 注册工具
    def get_schemas(self) -> list                              # OpenAI Function Calling JSON
    async def execute(self, name, arguments) -> str            # 执行工具
```

### ContinuousSessionManager (`agent/continuous_session.py`)

```python
class ContinuousSessionManager:
    def __init__(self, timeout_minutes=5.0)
    def start(self, group_id, user_id)                         # 开启窗口
    def is_active(self, group_id, user_id) -> bool             # 检查 + 自动清理
    def touch(self, group_id, user_id)                         # 续期
    def end(self, group_id, user_id)                           # 手动关闭
```

### Session / Memory / Profile

| 类 | 文件 | 功能 |
|---|---|---|
| `Session` | `session.py` | per-user 对话上下文（最多 20 条），持久化到 `data/sessions/` |
| `MemorySystem` | `memory.py` | 长期记忆（Markdown 文件），关键词搜索，最多返回 3 条 |
| `ProfileManager` | `profile.py` | 用户画像，LLM 自动提取事实/兴趣，持久化到 `data/users/` |

## 已注册工具（16 个）

| 工具 | 说明 |
|------|------|
| `search_web` | SearXNG 聚合搜索（天气 / 新闻 / 百科） |
| `execute_code` | Python 沙盒执行（三层隔离） |
| `get_time` | 当前日期时间 |
| `read_file` | 读取文件（文本 / PDF / 图片 AI 分析） |
| `summarize_pdf` | PDF 提取 + 总结 |
| `download_repo` | Git clone 仓库（HTTPS only） |
| `translate_text` | 多语言翻译 |
| `explain_code` | 代码解释 |
| `gacha_pull` | 抽卡模拟（4 种卡池） |
| `calculate_speed` | 游戏战斗测速 |
| `compare_speed_probability` | 乱速概率计算 |
| `geocode` | 地址 → 经纬度坐标查询 |
| `reverse_geocode` | 经纬度 → 详细地址反查 |
| `get_weather` | 实时天气 / 4天预报（高德） |
| `search_poi` | 周边POI搜索（餐厅、地铁等） |
| `plan_route` | 驾车/步行/公交路线规划 |

## 智能体配置

所有配置文件在 `QQBot/agent/config/`：

| 文件 | 用途 |
|------|------|
| `SOUL.md` | 人格定义（Roxy）& 行为规则 |
| `IDENTITY.md` | 身份声明 & 技术栈 & 能力 |
| `AGENTS.md` | 编排规则 & 工具选择 & 连续对话模式 |
| `WORKSPACE.md` | 工作区约束 & 安全边界 |
| `TOOLS.md` | 全部工具的参数文档 |
| `BOOTSTRAP.md` | 启动健康检查 |
| `SESSION.md` | 会话参数 |

修改这些文件会**即时影响 Agent 行为**，无需重启即可生效（`reload_configs()`）。

## 添加新工具

在 `QQBot/tools/` 下创建新的工具函数，然后在 `agent_router.py` 的 `_build_tool_registry()` 中注册：

```python
async def my_tool(param: str) -> str:
    # 工具逻辑
    return result

# 在 _build_tool_registry() 中:
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

同时更新 `TOOLS.md` 添加工具文档。

## 测试

```bash
bash test.sh
# 或
cd QQBot && python test_agent.py
```

7 个测试套件，38 项测试：
1. **ToolRegistry** — 注册 / Schema / 同步异步执行 / 错误
2. **SessionManager** — CRUD / 超时 / 裁剪 / 持久化
3. **MemorySystem** — 保存 / 搜索 / 遗忘 / 列出
4. **AgentCore** — 启动 / 提示词 / 工具循环 / 迭代上限 / 画像注入
5. **UserProfile** — 创建 / 事实去重 / 持久化
6. **DeepSeekClient** — 响应解析（纯文本 / 工具调用 / 混合）
7. **BuiltinTools** — get_time / execute_code / search_web

## 安全模型

| 边界 | 规则 |
|------|------|
| 代码执行 | `python3 -I` 隔离 + 模式匹配（15 个禁止模式）+ 60s / 100KB 限制 |
| 文件访问 | 仅在 `/data/workspace/`，拒绝路径遍历（`..`, `~`, `/etc/`） |
| 网络 | 仅通过预定义工具（search, download_repo HTTPS only） |
| 隐私 | per-user 隔离，本地存储，不上传第三方（除 API 调用外） |

详见 `QQBot/agent/config/WORKSPACE.md`。

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
# 完整栈（SearXNG + QQBot + vLLM）
docker compose up -d

# 仅 SearXNG
docker compose up -d searxng
```

Docker Compose 包含：
- `searxng` — 搜索引擎 (port 8082)
- `qqbot` — 主服务，含 NapCat + NoneBot + vLLM（port 8080/8081/6099/8000）

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

## 📖 扩展阅读

- [DOCUMENTATION.md](QQBot/DOCUMENTATION.md) — 完整的项目文档（架构 / 数据流 / SearXNG / 多模态 / 多模型 / 演进历史）
- [PLAN.md](QQBot/PLAN.md) — Agent 架构设计决策记录
- [WORKSPACE.md](QQBot/agent/config/WORKSPACE.md) — 安全模型与能力边界
- [AGENTS.md](QQBot/agent/config/AGENTS.md) — Agent 编排规则

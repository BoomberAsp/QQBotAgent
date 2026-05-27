# QQBot — LLM Agent 智能体

基于 [NapCat](https://github.com/NapNeko/NapCatQQ) + [NoneBot2](https://nonebot.dev/) 构建的 **Markdown 驱动 LLM Agent**，支持工具调用、用户画像、长期记忆、特殊会话（百万 token 上下文）与用户工作区隔离。

## 特性

- **Agent 架构**: Think → Act → Observe → Respond 循环，Markdown 配置文件驱动
- **工具调用**: 19 个已注册工具 — 搜索、代码执行、Shell、地图、天气、文件分析、抽卡等
- **多模型路由**: FLASH (简单对话) / REASONING (复杂推理) / MULTIMODAL (图片理解)
- **特殊会话**: 每用户至多 3 个持久化会话，百万 token 上下文窗口，快照+增量双层存储
- **用户画像**: LLM 驱动的背景事实提取，自动更新，跨会话持久化
- **长期记忆**: Markdown 文件系统，关键词搜索，自动保存重要对话
- **用户工作区**: 每用户独立文件空间，配额管理 (3 级策略)，contextvars 隔离
- **硬件自适应**: 启动时自动检测硬件规格，动态调整任务拒绝策略
- **安全沙箱**: Python 代码执行 (三层防护)、Shell 命令白名单 (40+ 命令)、路径验证
- **群聊连续对话**: 5 分钟免 @ 窗口，自动续期，随时可退出
- **地图服务**: 高德 API 集成 — 地理编码、天气、POI 搜索、路径规划

## 快速开始

### 环境要求

- Python 3.10+
- Docker (用于 SearXNG 搜索引擎)
- NapCatQQ (QQ 协议适配)

### 安装与启动

```bash
# 1. 安装系统依赖
bash napcat.sh --docker n

# 2. 启动 SearXNG
docker compose up -d searxng

# 3. 配置 Python 虚拟环境
python3 -m venv ~/.virtualenvs/QQBotAgent
source ~/.virtualenvs/QQBotAgent/bin/activate
pip install -r QQBot/requirements.txt

# 4. 配置 .env (复制模板并填入密钥)
cp QQBot/.env.example QQBot/.env
# 编辑 QQBot/.env: 设置 DEEPSEEK_API_KEY, ONEBOT_ACCESS_TOKEN 等

# 5. (可选) 配置多模型路由
cp QQBot/config/models_settings_example.json QQBot/config/models_settings.json
# 编辑填入各模型的 API 信息

# 6. 启动 NoneBot
cd QQBot && nb run

# 7. 启动 Napcat (另开终端)
xvfb-run -a /path/to/qq --no-sandbox
```

## 项目结构

```
QQBotAgent/
├── bot.py                   # NoneBot 启动入口
├── docker-compose.yml       # Docker Compose 编排
├── napcat.sh                # Napcat 安装脚本
├── searxng/                 # SearXNG 配置
│
└── QQBot/                   # 机器人主体
    ├── .env                 # 环境配置
    ├── requirements.txt     # Python 依赖
    ├── test_agent.py        # 测试套件 (7 套件, 38 测试)
    │
    ├── agent/               # 智能体核心
    │   ├── agent.py         #   主循环
    │   ├── tool_registry.py #   工具注册表
    │   ├── session.py       #   会话管理 (临时会话)
    │   ├── special_session.py # 特殊会话 (百万 token, 快照+增量)
    │   ├── continuous_session.py # 群聊连续对话
    │   ├── hardware.py      #   硬件自动检测
    │   ├── workspace.py     #   用户工作区隔离
    │   ├── context.py       #   执行上下文
    │   ├── memory.py        #   长期记忆
    │   ├── profile.py       #   用户画像
    │   └── config/          #   智能体配置 (12 个 Markdown 文件)
    │
    ├── plugins/             # NoneBot 插件
    │   └── agent_router.py  #   统一消息入口 (19 个工具, 8 个会话命令)
    │
    ├── tools/               # 工具实现
    │   ├── builtin_tools.py #   6 个内置工具
    │   ├── file_tools.py    #   文件读取 (文本/PDF/图片)
    │   ├── map_tools.py     #   地图工具 (5 个)
    │   └── legacy_tools.py  #   游戏/娱乐工具 (6 个)
    │
    ├── lib/                 # 自定义库
    │   ├── deepseek_client.py  # DeepSeek API 客户端
    │   ├── multimodal_client.py # 多模态 LLM 客户端
    │   ├── model_router.py  #   多模型路由器
    │   └── amap_client.py   #   高德地图 API 客户端
    │
    └── config/              # JSON 配置
        ├── models_settings.json      # 多模型配置 (git-ignored)
        ├── models_settings_example.json # 配置模板
        └── gacha_data.json   # 抽卡数据
```

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `DEEPSEEK_API_KEY` | (必填) | DeepSeek API 密钥 |
| `DEEPSEEK_API_BASE` | `https://api.deepseek.com` | API 端点 |
| `ONEBOT_ACCESS_TOKEN` | (必填) | OneBot V11 WebSocket 密钥 |
| `SUPERUSERS` | — | 管理员 QQ 号 (JSON 数组) |
| `SEARXNG_ENDPOINT` | `http://localhost:8082` | SearXNG JSON API 地址 |
| `AMAP_API_KEY` | — | 高德地图 API Key (可选) |
| `QQBOT_WORKSPACE` | `data/workspace/` | 全局工作区根目录 |
| `USER_DATA_ROOT` | `data/users_store/` | 用户数据根目录 (画像/会话/工作区) |
| `MAX_SPECIAL_SESSIONS` | `3` | 每用户最大特殊会话数 |
| `USER_WORKSPACE_QUOTA_MB` | `500` | 每用户工作区配额 (MB) |

## 特殊会话命令

在 QQ 聊天中 @机器人 使用以下命令:

| 命令 | 说明 |
|------|------|
| `/新会话 [名称]` | 创建特殊会话 (留空由 LLM 自动命名) |
| `/切换会话 <编号>` | 切换到指定会话 |
| `/我的会话` | 列出所有特殊会话 |
| `/重命名会话 <编号> <名称>` | 重命名会话 |
| `/删除会话 <编号>` | 删除会话 (确认码保护) |
| `/激活会话` | 查看当前会话状态 |
| `/退出会话` | 退出特殊会话，回到临时模式 |
| `/clear` / `清除上下文` | 清除当前会话上下文 |
| `/status` | 查看 Agent 状态 |

## 运行测试

```bash
cd QQBot
python test_agent.py          # 运行全部 38 个测试
python -m pytest test_agent.py -v  # 详细输出
```

## 文档

完整项目文档见 [DOCUMENTATION.md](DOCUMENTATION.md)，涵盖:
- 智能体架构 (Agent 主循环、系统提示词结构)
- 核心模块详解 (Agent、工具注册表、会话管理、用户画像、记忆系统)
- 工作区隔离 & 安全模型 (三层代码执行防护)
- 多模型架构 (三模型分层 + 复杂度分类)
- 架构演进历史 (v1.x → v2.13)
- 工具实现参考 (全部 19 个工具)

## 架构演进

当前版本: **v2.13**

| 版本 | 内容 |
|------|------|
| v2.0 | Agent 统一入口，替代分布式命令 |
| v2.1–2.4 | SearXNG 搜索、工作区隔离、文件阅读、多模型路由 |
| v2.5 | 群聊连续对话 (5 分钟免 @) |
| v2.6–2.8 | reasoning_content 保留、实时进度推送、地图服务 |
| v2.9–2.10 | 抽卡动画工具、抽卡数据外部化 |
| v2.11–2.12 | 代码执行图表输出、Shell 命令执行工具 |
| v2.13 | 特殊会话 + 用户工作区 + 硬件检测 |

## License

MIT

# QQBotAgent 管理 Web 面板（Web UI）实施规划

> **实施状态（2026-09-19 按代码实况更新）**：✅ **已全部实现并合入 dev**（commit `924fb62`
> "新增 Web UI 管理面板与 Token 计量总表"，Phase 0–5 完成）。`webui/` 11 个 Python 模块、
> 17 个模板（15 功能页 + login + placeholder）、17 个 JS、`start_webui.sh` 均在仓库中；
> start.sh/stop.sh/.gitignore 改动已落地。实际实现与本规划的差异已在下文逐节用 ✅/⚠️ 注记标出。
> 生产部署注意事项（venv 缺 psutil、面板 adopt screen 进程、SSH 隧道访问）见 `.claude/deploy-server.md`。

## Context

QQBotAgent 是一个 NoneBot2 QQ 机器人（LLM Agent 架构）。目前运维全靠 SSH + shell 脚本 + 翻 JSONL 日志文件：服务挂了没人知道、用户反馈（`data/feedback/`）只写不读、改一句系统提示词要编辑文件再重启。本规划新增一个**独立进程**的 Web 管理面板，覆盖：进程启停/看门狗、资源监控、日志查看、Web 终端、Token 计量看板、用户反馈查看、工具审计、会话/任务/记忆/画像管理、配置热编辑（提示词改完 5 秒内热重载）、Agent Playground。

**用户已确认的决策**：Jinja2 SSR + 原生 JS（ECharts/xterm.js 走 CDN，无 Node 构建）；仅绑 127.0.0.1（SSH 隧道访问）；单一管理员密码（哈希存储）。

**交付物要求**：用户要求规划文档保存为仓库根目录 `Web-UI-Plan.md` —— 实施第 0 步即把本规划写入该文件。

## 已核实的现状（影响设计的事实）

- **真正的启动方式**（⚠️ 本节初稿判断**错误**，Phase 2 已在服务器 `ps aux` 核实并修正，见 `.claude/deploy-server.md`）：
  生产实际是 **`cd QQBot && nb run`**（screen 会话托管；`nb run` 读 `QQBot/pyproject.toml`，端口 8081 来自 `QQBot/.env`；
  真正 listen 的是其派生 worker `python3 -c "…nonebot.load_from_toml…"`）。**`python bot.py` 会失败**
  （`ModuleNotFoundError: No module named 'agent'`——bot.py 不把 QQBot/ 加入 sys.path），只是历史/备用入口。
  ProcessManager 的 nonebot 命令已按实况实现为 `[VENV_NB, "run"]`、cwd=`QQBot/`（config.py:77-109）。
- **文件日志**（初稿时"无任何文件日志"，现已实现）：loguru file sink 落在 **`agent_router.py:111-128`
  `_setup_file_logging()`**（模块导入时安装，写 `QQBot/logs/bot_{time:YYYY-MM-DD}.log`，rotation 10MB /
  retention 7 天）——**不在 bot.py**，因为生产用 `nb run` 启动、根本不执行 bot.py。NapCat 为本地
  `xvfb-run -a ~/Napcat/opt/QQ/qq --no-sandbox` 进程（自带 6099 WebUI）；SearXNG 是 docker 容器 `searxng`
  （8082→8080，走 `docker logs`）。
- stop.sh ✅ 已修正：pgrep 模式覆盖 `bin/nb run`、`nb run`、`nonebot\.load_from_toml`、legacy `python.*bot\.py$`，
  并显式豁免面板（只匹配 `uvicorn.*8081`，绝不碰 8090）；还会写 `watchdog_state.json` 把 nonebot 的
  `should_run` 置 False，防止看门狗 30 秒后又拉起（stop.sh:52-66）。
- 依赖：venv `~/.virtualenvs/QQBotAgent/`（Python 3.12）已有 fastapi/uvicorn/jinja2/loguru/httpx；**缺 psutil**
  → ✅ 已加入 `webui/requirements.txt`（⚠️ 未按初稿加进 `QQBot/requirements.txt`——webui 是独立进程带自己的
  requirements，功能上无影响；**生产服务器 venv 部署前须先 pip install**，见 deploy-server.md）。
- 可复用代码：`QQBot/lib/token_ledger.py`（read_summary/aggregate_day/format_summary）、`Agent.reload_configs()`（agent.py:167）、`PermissionManager` 每次调用重读 .env（改权限即生效）、`GroupFeatures.refresh()` 每条消息自动调用（群开关改完即生效）、`SpecialSessionManager.delete()`（special_session.py:204）、`MemorySystem`（save/recall/forget/search/list_all）、`ProfileManager.get/save`、`HardwareDetector`（{USER_DATA_ROOT}/.hardware.json）。
- 数据格式：反馈 `data/feedback/feedback_YYYY-MM.jsonl`（timestamp/user_id/type/content/context）；会话 `data/sessions/{uid}.json`；任务 `data/task_log/{uid}.jsonl`；审计 `data/audit/tool_calls_YYYY-MM-DD.jsonl`；画像 `data/users/{uid}/profile.json`；记忆 `data/memory/{user|knowledge|system}/*.md`（YAML frontmatter）；人格 `agent/config/personalities/*.md` + `data/personality_config.json` + `data/group_personality.json`；USER_DATA_ROOT（工作区、特殊会话、画像）——⚠️ 初稿写的 `/mnt/datadisk0/QQBotUserData` 在生产服务器上**不存在**，
真实值来自 `QQBot/.env`：**`/home/ubuntu/datadisk/QQBotData`**（见 deploy-server.md §生产数据根目录）。

## 架构总览

```
┌──────────────┐     读/写      ┌────────────────┐     写      ┌──────────────┐
│ WebUI 面板    │◀─────────────▶│ QQBot/data/    │◀───────────▶│ NoneBot bot  │
│ 127.0.0.1:   │               │ + USER_DATA_   │             │ 0.0.0.0:8081 │
│ 8090(独立进程)│   配置热编辑──▶│ ROOT 共享数据   │──mtime监听─▶│ reload_configs│
└──────┬───────┘               └────────────────┘             └──────────────┘
       │ 进程管理(start/stop/看门狗) ──▶ nonebot / napcat / searxng
       │ 日志读取 ──▶ QQBot/logs/*.log (新增) / napcat捕获 / docker logs
```

面板进程绝不 import `QQBot/plugins/*`（模块级 `nonebot.on_message()` 需要 driver 初始化，会在面板进程炸）。面板读 QQBot 模块时在入口显式 `sys.path.insert(0, ROOT/"QQBot")`。

## 目录结构

### 新增 `webui/`（与 QQBot/ 平级；独立进程、无 import 耦合、不侵入 bot 代码）

```
webui/
├── main.py                 # FastAPI 入口：路由注册、中间件、uvicorn 启动
├── config.py               # 面板配置：端口、路径常量、进程启动命令表（可覆盖）
├── auth.py                 # scrypt 密码哈希、登录、session cookie 中间件
├── audit.py                # 面板操作审计（写 webui/data/audit.jsonl）
├── process_manager.py      # ProcessManager + 看门狗后台任务
├── log_viewer.py           # 日志分页/tail/正则脱敏
├── data_reader.py          # token/审计/反馈/会话/任务/画像/记忆的只读解析（分页）
├── config_editor.py        # markdown/JSON/.env 读写（写前自动备份）
├── playground.py           # 隔离 Agent 构造与运行
├── hardware_monitor.py     # psutil 采集（结果缓存 5 秒）
├── requirements.txt        # psutil
├── static/{css/app.css, js/*.js, img/favicon.svg}   # 每页一个同名 js
├── templates/              # base.html + login + 15 个页面模板
└── data/                   # 运行时数据（加入 .gitignore）
    ├── auth.json           # 密码哈希 + session 表（运行时按需创建）
    ├── audit.jsonl         # 面板操作审计
    ├── *.pid               # 受管进程 PID 文件
    └── logs/               # napcat stdout 捕获 + webui 自身日志
```

> ✅ 实况补充：另有 `webui/__init__.py`（包声明）；`data/` 下还有规划未列的
> `watchdog_state.json`（看门狗开关持久化，stop.sh 也会写它）与 `feedback_seen.json`
> （未读反馈计数，data_reader.py:39）。session 表实际存内存（auth.py:135），不落 auth.json。

### 现有文件改动

| 文件 | 改动 | 落地实况 |
|---|---|---|
| ~~`bot.py`（根）~~ → **`QQBot/plugins/agent_router.py`** | loguru file sink：`QQBot/logs/bot_{date}.log`，rotation 10MB / retention 7 天 | ✅ sink 在 `agent_router.py:111-128` `_setup_file_logging()` 模块级安装（生产走 `nb run`、不执行 bot.py，放 bot.py 会永远不生效）；bot.py:43-45 仅留注释说明 |
| `QQBot/plugins/agent_router.py` | `@on_startup` 启动配置 watcher 后台任务，每 5 秒 stat 配置 md 的 mtime，变化则 `agent.reload_configs()`（带 5 秒冷却） | ✅ `:999-1025` `_start_config_watcher()`（约 26 行）；⚠️ 监视范围比规划大：`os.walk` 递归 `agent/config/` 下**全部** `*.md`（含 personalities/），非"六个" |
| `QQBot/lib/deepseek_client.py` | `__init__` 开头加 early-return：api_key 与 api_base 均显式传入时跳过 `get_driver()` | ✅ `:27-35`，条件 `if api_key and api_base:` |
| ~~`QQBot/requirements.txt`~~ | ~~+ `psutil>=5.9.0`~~ | ⚠️ **未加入**——psutil 只在 `webui/requirements.txt`（webui 独立进程自带依赖清单，功能无影响） |
| `start.sh` / `stop.sh` | start.sh 追加启动面板；stop.sh 修正 bot 匹配模式、面板进程豁免（绝不匹配 8090） | ✅ start.sh 第 5 节 `cd QQBot && nb run`、第 5.5 节 `bash start_webui.sh start`；stop.sh pgrep 覆盖 `bin/nb run`/`nb run`/`nonebot\.load_from_toml`/legacy `bot.py`，另同步看门狗状态（规划外增强） |
| `.gitignore` | + `webui/data/`、`QQBot/logs/` | ✅ `.gitignore:8`、`:26` |
| 新增 `start_webui.sh` | `nohup {venv}/bin/python -m uvicorn webui.main:app --host 127.0.0.1 --port 8090`（cwd=仓库根） | ✅ 并额外支持 `start/stop/restart/status` 子命令 + PID 文件 `webui/data/webui.pid` |

## API 设计（规划 ~42 端点；✅ 实际落地 **48 个 `/api/` 端点 + 15 个页面路由 + 2 个 WS**，共 65 条路由）

> 与规划的差异（webui/main.py 行号为证）：
> - **规划外新增**：`POST /api/auth/change-password`(:201) · `POST /api/feedback/mark-read`(:467) ·
>   `GET /api/playground/options`(:738，可用模型/人格列表)
> - **合并实现**：进程 start/stop/restart 三个独立端点 → 1 个参数化 `POST /api/processes/{name}/{action}`(:335)
> - **路径差异**：记忆 API 实际为 `GET|PUT|DELETE /api/memory`（query 参数 `mem_type`/`user_id`/`q`），
>   非规划的 `/api/memory/{uid}[/{entry}]`

**认证**：`GET /api/auth/status` · `POST /api/auth/setup`（仅未配置时）· `POST /api/auth/login` · `POST /api/auth/logout` · `POST /api/auth/change-password`（新增）

**仪表盘**：`GET /api/dashboard`（进程状态+资源+今日Token+未读反馈数+看门狗状态）· `WS /ws/dashboard`（5 秒推送）

**进程**：`GET /api/processes` · `POST /api/processes/{name}/{action}`（action ∈ start|stop|restart，合并实现）· `POST /api/processes/watchdog`（开关）

**日志**：`GET /api/logs/{source}?lines=&offset=` · `WS /ws/logs/{source}` · `POST /api/logs/{source}/clear`。source ∈ nonebot|napcat|searxng|webui（searxng 走 `docker logs` 子进程）

**终端**：`WS /ws/terminal`（pty 双向流，标准库 `pty` 模块）

**Token**：`GET /api/tokens/summary?days=`（复用 `TokenLedger.read_summary()`）· `GET /api/tokens/daily?date=` · `GET /api/tokens/users?days=&limit=`

**审计**：`GET /api/audit/tool-calls?date=&tool=&user=&page=` · `GET /api/audit/tool-stats?days=`

**反馈**：`GET /api/feedback?month=&type=&page=` · `POST /api/feedback/mark-read`（新增，未读计数存 `webui/data/feedback_seen.json`）· `POST /api/feedback/{month}/{seq}/tag`（行级追加标签）

**会话**：`GET /api/sessions` · `GET /api/sessions/{uid}/temp`（临时会话上下文）· `POST /api/sessions/{uid}/clear-temp` · `GET /api/sessions/{uid}/special` · `DELETE /api/sessions/{uid}/special/{name}`（直调 `SpecialSessionManager.delete`）

**任务日志**：`GET /api/tasks?user_id=&page=` · `GET /api/tasks/{uid}/{task_id}`

**记忆与画像**：`GET|PUT|DELETE /api/memory`（query 参数定位条目，实际实现）· `GET|PUT /api/profiles/{uid}`

**工作区**：`GET /api/workspace/stats`（用量 vs 配额）· `GET /api/workspace/{uid}`（文件树）

**配置热管理**：`GET /api/config/prompts` · `GET|PUT /api/config/prompts/{name}`（含六个人格文件路径名）· `GET|PUT /api/config/permissions`（SUPERUSERS/VIP_USERS，返回脱敏）· `GET|PUT /api/config/models`（models_settings.json，响应注明需重启生效）· `GET /api/config/group-features` + `PUT /api/config/group-features`（改完即生效，无需重载）· `GET|PUT /api/config/personality`（personality_config.json + group_personality.json）

**Playground/预览/Wiki**：`GET /api/playground/options`（新增，模型 tier/人格列表）· `POST /api/playground/run`（支持多轮 history，最多 20 轮）· `GET /api/config/system-prompt-preview?personality=` · `GET /api/wiki/cache-status`（wiki_cache + redeem_code 的 mtime/大小/条目数）

## 页面清单（16 页）

登录 · 仪表盘（状态灯+实时曲线+今日Token+未读反馈+看门狗开关） · 进程管理 · 日志查看（4 源 Tab + 搜索） · Web 终端 · Token 消耗（折线/饼图/柱状/用户排行） · 工具审计 · **用户反馈（时间线+类型筛选+上下文展示+标签）** · 会话管理 · 任务日志 · 记忆与画像 · 工作区磁盘 · 配置热管理（编辑器+备份+重载状态） · Playground · Wiki 缓存状态

> ✅ 全部实现：`webui/templates/` 共 17 个模板 = 上述 16 页（base.html 为布局框架）+ `placeholder.html`
> （规划外的未实现模块占位页）。main.py:66-69 `_IMPLEMENTED` 集合含全部 14 个功能页路由。

## 关键设计点

1. **日志文件化**：~~bot.py 加 loguru sink~~ → ✅ 实际 sink 装在 **`agent_router.py:111-128`**（生产走 `nb run`
   不执行 bot.py，装 bot.py 会不生效）；nonebot 日志源优先读 `QQBot/logs/bot_*.log`，fallback
   `webui/data/logs/nonebot.log`（config.py:121-136）。NapCat 由 ProcessManager 启动时重定向 stdout 到
   `webui/data/logs/napcat.log`；SearXNG 走 `docker logs`（log_viewer.py:70-84）。**脱敏正则**统一在
   log_viewer（:23-28）：`sk-…`、`Bearer …`、`*api_key|token|secret=…`（比规划更宽）。
2. **ProcessManager**：命令白名单表（config.py 常量，可覆盖）；`Popen(start_new_session=True)` 脱离进程组（面板重启不杀子进程，process_manager.py:252）；PID 文件 `webui/data/{name}.pid`（启动时扫描接管/adopt 存活进程，:113-133——线上会接管 screen 起的 `nb run`+worker 双进程，见 deploy-server.md）；stdout/stderr 重定向到日志文件。默认命令：bot=**`{venv}/bin/nb run`（cwd=`QQBot/`**，config.py:81，按服务器实况修正，非初稿的 `python bot.py`）、napcat=`xvfb-run -a ~/Napcat/opt/QQ/qq --no-sandbox`、searxng=docker 容器方式（container=`searxng`, compose_cwd=仓库根）。停止走 SIGTERM→8 秒→SIGKILL 升级（:288-313）。
3. **看门狗** ✅：asyncio 后台任务 30 秒轮询（process_manager.py:33/:379-392）；崩溃自动重启，5 分钟内最多 3 次（:34-35/:343-349）；默认关闭（:49）、API 可开关且**持久化到 `watchdog_state.json`**（规划外增强，stop.sh 借此防止停掉的 bot 被看门狗复活）；面板展示崩溃计数与最近崩溃时间（:368-369）。
4. **热重载** ✅：面板写 `agent/config/*.md` → bot 端 watcher（agent_router.py:999-1025）5 秒内 `reload_configs()`；⚠️ 监视范围为 config 目录下**全部** `.md`（含 personalities/，递归 os.walk），比规划的"六个 md"更宽。`.env` 权限改完即生效（PermissionManager 重读）；`group_features.json` 改完下条消息生效（已有 refresh()）；`models_settings.json` 无 reload，UI 明确标注"保存后需重启 NoneBot"。注意：热重载只对**部署了新 agent_router.py 并重启过的 bot** 生效（见 deploy-server.md §当前线上 bot 是旧代码）。
5. **Playground 隔离**（分两档）：
   - v1 ✅（已实现）：**无工具**纯对话（playground.py:86 空 `ToolRegistry()`）—— 显式参数构造 `DeepSeekClient(api_key, api_base, model)`（读 models_settings.json，reasoning/flash 双 tier 可选，:44-56）+ `Agent(…)` 全隔离（memory/profile/hardware/workspace/special 均 None，:89-93）+ contextvars user_id 固定 `"playground"`（:100）、token 归属 `set_usage_context("playground", …)`（:167）→ 绝不污染线上数据。**规划外增强**：多轮对话（history 最多 20 轮，:172-181）、`asyncio.Lock` 并发保护（:39）、系统提示词预览无网络调用（:112-134）。
   - v2（后续可选，❌ 未实现）：白名单只读工具（get_time/角色/羁绊查询等），在面板进程手工注册（绝不 import agent_router）。
6. **安全** ✅：`hashlib.scrypt` 存哈希（N=16384/r=8/p=1，auth.py:27-33，无新依赖）；cookie `HttpOnly+SameSite=Strict+24h`（:149-153；未设 `secure` 标志——面板仅绑 127.0.0.1 走 HTTP，属正确取舍）；5 次失败锁 5 分钟（config.py:115-116）；密码比较用 `hmac.compare_digest`；所有非 GET 操作写 `webui/data/audit.jsonl`（audit_middleware，main.py:108-120）；WS 连接内联校验 cookie（main.py:385-386，规划外增强）；密钥掩码首 6 尾 4（config_editor.py:170-171）；Jinja2 默认转义。

## 分阶段实施计划

> ✅ **全部 Phase 已完成**（commit `924fb62`，一次性合入）。以下保留原计划文本，仅修正与落地实况不符处。

**Phase 0 — 规划落档** ✅：写 `Web-UI-Plan.md`（本文件内容）到仓库根。

**Phase 1 — 骨架与认证** ✅：webui/ 目录、config.py、auth.py（scrypt+cookie+锁定）、audit.py、main.py（Jinja2+静态+中间件）、base/login/dashboard 模板、app.css、app.js、start_webui.sh、.gitignore。
验证项：启动面板 → 首次设密 → 登录 → 仪表盘空壳加载；错误密码 5 次被锁。

**Phase 2 — 进程/日志/终端/资源** ✅：已在服务器核实线上真实启动命令（`ps aux` → **`cd QQBot && nb run`**，推翻初稿的 `python bot.py` 判断）；loguru sink 装在 **agent_router.py**（非 bot.py，理由见"现有文件改动"表）；process_manager.py（含看门狗）；log_viewer.py；`/ws/terminal`（pty）；hardware_monitor.py（psutil，5 秒缓存）；三个页面 + 仪表盘集成；改 stop.sh/start.sh。
验证项：面板启停 bot 全流程；看门狗杀进程后 30 秒内自动拉起；日志搜索与脱敏生效；终端可执行命令；`bash stop.sh` 不杀面板。

**Phase 3 — 数据看板** ✅：data_reader.py（统一分页+尾读优化；⚠️ 实际非纯只读——含 `memory_save/memory_delete/profile_save` 写入函数，服务 PUT/DELETE 端点）；Token/审计/反馈/会话/任务/工作区六页 + 仪表盘卡片。
验证项：每页数据与手工 `cat` JSONL 一致；大会话文件（>1MB）读取 <1 秒。

**Phase 4 — 配置热管理与记忆画像** ✅：config_editor.py（写前备份到 `webui/data/backups/configs/`）；agent_router.py config watcher（实际 ~26 行，:999-1025）；配置/记忆与画像/Wiki 三页。
验证项：改 SOUL.md 加一句暗号 → 5 秒内 QQ 回复带暗号（或 Playground 验证）；面板删除一条记忆 → `data/memory/` 文件消失；误写坏 JSON 可从备份恢复。⚠️ 热重载生效前提是线上 bot 已部署含 watcher 的新代码并重启。

**Phase 5 — Playground 与打磨** ✅：deepseek_client.py early-return（实际 ~5 行）；playground.py（v1 无工具 + 多轮 + 模型 tier 选择）；Playground 页；系统提示词预览；整体 UI 打磨；bot 侧回归验证。
验证项：Playground 跑一轮对话 → `QQBot/data/sessions/` 无 playground 残留、`token_usage` 出现 purpose 记录。
⚠️ 原"`bash test.sh` 9 套件全绿"口径已过时：test.sh 现列 **10 个脚本**，其中 8 个位于被 .gitignore 排除的
`test/` 目录（仅开发机存在）；bot 侧回归以 `test_agent.py`（13 套件）+ `test_workspace.py`（11 类）为准。

## 风险与回滚

| 风险 | 缓解 / 回滚 |
|---|---|
| 线上启动命令与预期不符 | Phase 2 第一步 ps 核实；命令表可配置 |
| 看门狗雪崩 | 5 分钟 3 次限流；默认关闭 |
| stop.sh 误杀面板 / 漏杀 bot | PID 文件优先，pgrep 模式显式排除 8090 |
| watcher 频繁 reload | 5 秒冷却 + 仅比较 mtime |
| 配置写坏 | 每次保存前自动备份；.env 写入前语法校验 |
| Playground 隔离泄漏 | memory/profile=None、临时工作区、独立 SessionManager；v1 干脆无工具 |
| 大文件读取慢 | 分页 + 尾读（deque/seek 从文件尾） |

## 端到端验证（全部完成后）

> ⚠️ **生产部署状态**：面板代码已合入 dev，但生产服务器**尚未完成面板部署前置**——服务器 venv 缺 psutil
> （须先 `~/.virtualenvs/QQBotAgent/bin/pip install -r webui/requirements.txt`）、访问需
> `ssh -L 8090:127.0.0.1:8090` 隧道、且要避免与 screen 托管的 bot 形成"双份管理"（面板会 adopt screen
> 进程；一旦用面板 stop/start 过，之后应统一走面板）。详见 `.claude/deploy-server.md` §部署面板前的前置条件。

1. `bash test.sh` 全绿（bot 侧零回归；⚠️ test.sh 依赖被 gitignore 的 `test/` 目录，服务器上以
   `test_agent.py` 13/13 + `test_workspace.py` 为准）
2. 面板：登录 → 重启 NoneBot → 日志窗口看到启动输出 → 资源曲线在动
3. 改 SOUL.md 追加测试句 → QQ 发消息验证 5 秒内生效 → 面板回滚备份
4. 反馈页能看到存量 `data/feedback/` 全部记录；会话页能清掉一个测试会话
5. Playground 跑通一轮对话且无数据污染
6. 断开 SSH 隧道后面板不可达（确认仅本机绑定）

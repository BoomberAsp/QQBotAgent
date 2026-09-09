# QQBotAgent 管理 Web 面板（Web UI）实施规划

## Context

QQBotAgent 是一个 NoneBot2 QQ 机器人（LLM Agent 架构）。目前运维全靠 SSH + shell 脚本 + 翻 JSONL 日志文件：服务挂了没人知道、用户反馈（`data/feedback/`）只写不读、改一句系统提示词要编辑文件再重启。本规划新增一个**独立进程**的 Web 管理面板，覆盖：进程启停/看门狗、资源监控、日志查看、Web 终端、Token 计量看板、用户反馈查看、工具审计、会话/任务/记忆/画像管理、配置热编辑（提示词改完 5 秒内热重载）、Agent Playground。

**用户已确认的决策**：Jinja2 SSR + 原生 JS（ECharts/xterm.js 走 CDN，无 Node 构建）；仅绑 127.0.0.1（SSH 隧道访问）；单一管理员密码（哈希存储）。

**交付物要求**：用户要求规划文档保存为仓库根目录 `Web-UI-Plan.md` —— 实施第 0 步即把本规划写入该文件。

## 已核实的现状（影响设计的事实）

- **真正的启动方式**：`python bot.py` 于仓库根目录运行（bot.py:54 用相对路径 `nonebot.load_plugins("QQBot/plugins")`，:58 `nonebot.run(port=8081)`，:33 按自身位置解析 `QQBot/.env`）。⚠️ start.sh/start_bot.sh 里的 `cd QQBot && nb run` 实际找不到入口（QQBot/ 下无 bot.py）——Phase 2 需在服务器上 `ps aux` 核实线上真实启动命令，ProcessManager 的命令做成可配置常量。
- **无任何文件日志**：NoneBot 仅 stdout（loguru，无 sink）；NapCat 为本地 `xvfb-run -a ~/Napcat/opt/QQ/qq --no-sandbox` 进程（自带 6099 WebUI）；SearXNG 是 docker 容器 `searxng`（8082→8080，可 `docker logs`）。
- stop.sh 用 `pgrep -f "nb run"` + `pgrep -f "uvicorn.*8081"` —— 都匹配不到 `python bot.py` 进程，需修正。
- 依赖：venv `~/.virtualenvs/QQBotAgent/`（Python 3.12）已有 fastapi/uvicorn/jinja2/loguru/httpx；**缺 psutil**（加入 requirements）。
- 可复用代码：`QQBot/lib/token_ledger.py`（read_summary/aggregate_day/format_summary）、`Agent.reload_configs()`（agent.py:167）、`PermissionManager` 每次调用重读 .env（改权限即生效）、`GroupFeatures.refresh()` 每条消息自动调用（群开关改完即生效）、`SpecialSessionManager.delete()`（special_session.py:204）、`MemorySystem`（save/recall/forget/search/list_all）、`ProfileManager.get/save`、`HardwareDetector`（{USER_DATA_ROOT}/.hardware.json）。
- 数据格式：反馈 `data/feedback/feedback_YYYY-MM.jsonl`（timestamp/user_id/type/content/context）；会话 `data/sessions/{uid}.json`；任务 `data/task_log/{uid}.jsonl`；审计 `data/audit/tool_calls_YYYY-MM-DD.jsonl`；画像 `data/users/{uid}/profile.json`；记忆 `data/memory/{user|knowledge|system}/*.md`（YAML frontmatter）；人格 `agent/config/personalities/*.md` + `data/personality_config.json` + `data/group_personality.json`；USER_DATA_ROOT=/mnt/datadisk0/QQBotUserData（工作区、特殊会话）。

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
    ├── auth.json           # 密码哈希 + session 表
    ├── audit.jsonl         # 面板操作审计
    ├── *.pid               # 受管进程 PID 文件
    └── logs/               # napcat stdout 捕获 + webui 自身日志
```

### 现有文件改动

| 文件 | 改动 |
|---|---|
| `bot.py`（根） | `init()` 里加 loguru file sink：`QQBot/logs/bot_{date}.log`，rotation 10MB / retention 7 天 |
| `QQBot/plugins/agent_router.py` | +25 行：`@on_startup` 启动 `_config_watcher()` 后台任务，每 5 秒 stat 六个配置 md 的 mtime，变化则 `agent.reload_configs()`（带 5 秒冷却） |
| `QQBot/lib/deepseek_client.py` | `__init__` 开头加 early-return：api_key 与 api_base 均显式传入时跳过 `get_driver()`（Playground 需要；原有调用路径不受影响，`ModelRouter._create_client` 已是这种用法） |
| `QQBot/requirements.txt` | + `psutil>=5.9.0` |
| `start.sh` / `stop.sh` | start.sh 追加启动面板；stop.sh 修正 bot 匹配模式（加 `pgrep -f "bot.py"`）、面板进程豁免（绝不匹配 8090）；两者都调用 `bash start_webui.sh` / 读 PID 文件 |
| `.gitignore` | + `webui/data/`、`QQBot/logs/` |
| 新增 `start_webui.sh` | `nohup {venv}/bin/python -m uvicorn webui.main:app --host 127.0.0.1 --port 8090`（cwd=仓库根） |

## API 设计（~42 端点）

**认证**：`GET /api/auth/status` · `POST /api/auth/setup`（仅未配置时）· `POST /api/auth/login` · `POST /api/auth/logout`

**仪表盘**：`GET /api/dashboard`（进程状态+资源+今日Token+未读反馈数+看门狗状态）· `WS /ws/dashboard`（5 秒推送）

**进程**：`GET /api/processes` · `POST /api/processes/{name}/start|stop|restart` · `POST /api/processes/watchdog`（开关）

**日志**：`GET /api/logs/{source}?lines=&offset=` · `WS /ws/logs/{source}` · `POST /api/logs/{source}/clear`。source ∈ nonebot|napcat|searxng|webui（searxng 走 `docker logs` 子进程）

**终端**：`WS /ws/terminal`（pty 双向流，标准库 `pty` 模块）

**Token**：`GET /api/tokens/summary?days=`（复用 `TokenLedger.read_summary()`）· `GET /api/tokens/daily?date=` · `GET /api/tokens/users?days=&limit=`

**审计**：`GET /api/audit/tool-calls?date=&tool=&user=&page=` · `GET /api/audit/tool-stats?days=`

**反馈**：`GET /api/feedback?month=&type=&page=` · `POST /api/feedback/{month}/{seq}/tag`（行级追加标签）

**会话**：`GET /api/sessions` · `GET /api/sessions/{uid}/temp`（临时会话上下文）· `POST /api/sessions/{uid}/clear-temp` · `GET /api/sessions/{uid}/special` · `DELETE /api/sessions/{uid}/special/{name}`（直调 `SpecialSessionManager.delete`）

**任务日志**：`GET /api/tasks?user_id=&page=` · `GET /api/tasks/{uid}/{task_id}`

**记忆与画像**：`GET|PUT|DELETE /api/memory/{uid}[/{entry}]` · `GET|PUT /api/profiles/{uid}`

**工作区**：`GET /api/workspace/stats`（用量 vs 配额）· `GET /api/workspace/{uid}`（文件树）

**配置热管理**：`GET /api/config/prompts` · `GET|PUT /api/config/prompts/{name}`（含六个人格文件路径名）· `GET|PUT /api/config/permissions`（SUPERUSERS/VIP_USERS，返回脱敏）· `GET|PUT /api/config/models`（models_settings.json，响应注明需重启生效）· `GET /api/config/group-features` + `PUT /api/config/group-features`（改完即生效，无需重载）· `GET|PUT /api/config/personality`（personality_config.json + group_personality.json）

**Playground/预览/Wiki**：`POST /api/playground/run` · `GET /api/config/system-prompt-preview?personality=` · `GET /api/wiki/cache-status`（wiki_cache + redeem_code 的 mtime/大小/条目数）

## 页面清单（16 页）

登录 · 仪表盘（状态灯+实时曲线+今日Token+未读反馈+看门狗开关） · 进程管理 · 日志查看（4 源 Tab + 搜索） · Web 终端 · Token 消耗（折线/饼图/柱状/用户排行） · 工具审计 · **用户反馈（时间线+类型筛选+上下文展示+标签）** · 会话管理 · 任务日志 · 记忆与画像 · 工作区磁盘 · 配置热管理（编辑器+备份+重载状态） · Playground · Wiki 缓存状态

## 关键设计点

1. **日志文件化**：bot.py 加 loguru sink（与启动方式无关）；NapCat 由 ProcessManager 启动时重定向 stdout 到 `webui/data/logs/napcat.log`（若发现 `~/Napcat` 下自带日志目录则优先读）；SearXNG 走 `docker logs`。**脱敏正则**统一在 log_viewer：`sk-[A-Za-z0-9]{20,}`、`Bearer …`、`*_API_KEY=…`。
2. **ProcessManager**：命令白名单表（config.py 常量，可覆盖）；`Popen(start_new_session=True)` 脱离进程组（面板重启不杀子进程）；PID 文件 `webui/data/{name}.pid`（启动时扫描接管存活进程）；stdout/stderr 重定向到日志文件。默认命令：bot=`{venv}/bin/python bot.py`（cwd=仓库根）、napcat=`xvfb-run -a ~/Napcat/opt/QQ/qq --no-sandbox`、searxng=`docker compose up -d / stop searxng`。
3. **看门狗**：asyncio 后台任务 30 秒轮询；崩溃自动重启，5 分钟内最多 3 次（防雪崩）；默认关闭、API 可开关；面板展示崩溃计数与最近崩溃时间。
4. **热重载**：面板写 `agent/config/*.md` → bot 端 watcher 5 秒内 `reload_configs()`。`.env` 权限改完即生效（PermissionManager 重读）；`group_features.json` 改完下条消息生效（已有 refresh()）；`models_settings.json` 无 reload，UI 明确标注"保存后需重启 NoneBot"。
5. **Playground 隔离**（分两档）：
   - v1（本期）：**无工具**纯对话 —— 显式参数构造 `DeepSeekClient(api_key, api_base, model)`（读 models_settings.json）+ `Agent(…, session_manager=内存, memory_system=None, profile_manager=None)` + 手动设置 `agent/context.py` contextvars（user_id="playground"、role=admin、personality、workspace=临时目录）→ 用于验证提示词/人格/模型效果，绝不污染线上数据。
   - v2（后续可选）：白名单只读工具（get_time/角色/羁绊查询等），在面板进程手工注册（绝不 import agent_router）。
6. **安全**：`hashlib.scrypt` 存哈希（无新依赖）；cookie `HttpOnly+SameSite=Strict+24h`；5 次失败锁 5 分钟；所有非 GET 操作写 `webui/data/audit.jsonl`；.env/密钥展示一律掩码（首 6 尾 4）；Jinja2 默认转义，富文本输出用 `|e`。

## 分阶段实施计划

**Phase 0 — 规划落档**：写 `Web-UI-Plan.md`（本文件内容）到仓库根。

**Phase 1 — 骨架与认证**（~2 天）：webui/ 目录、config.py、auth.py（scrypt+cookie+锁定）、audit.py、main.py（Jinja2+静态+中间件）、base/login/dashboard 模板、app.css、app.js、start_webui.sh、.gitignore。
✅ 验证：启动面板 → 首次设密 → 登录 → 仪表盘空壳加载；错误密码 5 次被锁。

**Phase 2 — 进程/日志/终端/资源**（~3 天）：先在服务器核实线上真实启动命令（`ps aux`）；bot.py 加 loguru sink；process_manager.py（含看门狗）；log_viewer.py；`/ws/terminal`（pty）；hardware_monitor.py（装 psutil）；三个页面 + 仪表盘集成；改 stop.sh/start.sh。
✅ 验证：面板启停 bot 全流程（启→日志出现→停→进程消失）；看门狗杀进程后 30 秒内自动拉起；日志搜索与脱敏生效；终端可执行命令；`bash stop.sh` 不杀面板。

**Phase 3 — 数据看板**（~3 天）：data_reader.py（统一分页+尾读优化）；Token/审计/反馈/会话/任务/工作区六页 + 仪表盘卡片。
✅ 验证：每页数据与手工 `cat` JSONL 一致（用现有真实数据：8 月 token_usage、audit、feedback 均有存量）；大会话文件（>1MB）读取 <1 秒。

**Phase 4 — 配置热管理与记忆画像**（~3 天）：config_editor.py（写前备份到 `webui/data/backups/`）；agent_router.py config watcher（+25 行）；配置/记忆与画像/Wiki 三页。
✅ 验证：改 SOUL.md 加一句暗号 → 5 秒内 QQ 回复带暗号（或 Playground 验证）；面板删除一条记忆 → `data/memory/` 文件消失；误写坏 JSON 可从备份恢复。

**Phase 5 — Playground 与打磨**（~2 天）：deepseek_client.py early-return（3 行）；playground.py（v1 无工具）；Playground 页；系统提示词预览；整体 UI 打磨；跑 `bash test.sh` 确认 bot 侧改动零回归。
✅ 验证：Playground 跑一轮对话 → `QQBot/data/sessions/` 无 playground 残留、`token_usage` 出现 purpose 记录；`bash test.sh` 9 套件全绿。

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

1. `bash test.sh` 全绿（bot 侧零回归）
2. 面板：登录 → 重启 NoneBot → 日志窗口看到启动输出 → 资源曲线在动
3. 改 SOUL.md 追加测试句 → QQ 发消息验证 5 秒内生效 → 面板回滚备份
4. 反馈页能看到存量 `data/feedback/` 全部记录；会话页能清掉一个测试会话
5. Playground 跑通一轮对话且无数据污染
6. 断开 SSH 隧道后面板不可达（确认仅本机绑定）

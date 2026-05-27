# Workspace Constraints — Agent Capability Boundaries

This document defines the agent's **hard boundaries** — what it may and may not do.
These rules are enforced both by the agent's system prompt (refusal) and by tool-level restrictions.

---

## 1. Workspace Root

```
默认: 项目 data/workspace/
生产: /data/workspace/ (通过环境变量 QQBOT_WORKSPACE 设置)
```

All agent file operations (code execution, git clones, PDF reading, file creation)
MUST be confined within this directory.

| Directory | Purpose |
|-----------|---------|
| `{workspace}/code/` | Code execution temporary files |
| `{workspace}/repos/` | Cloned repositories |
| `{workspace}/uploads/` | User-uploaded files — auto-downloaded from QQ messages (images, PDFs, text files, etc.) |
| `{workspace}/output/` | Generated output files |

## 2. Capability Boundaries

### 2.1 Information & Search (ALLOWED)

- Search the web for public information
- Query weather for any location (via wttr.in)
- Return current date/time

### 2.2 Code Execution (RESTRICTED)

| Rule | Constraint |
|------|------------|
| **Language** | Python 3 only |
| **Max runtime** | 60 seconds |
| **Max output** | 100 KB (102400 characters) |
| **Network** | BLOCKED during execution |
| **File system** | Confined to `/data/workspace/code/` |
| **Forbidden modules** | `os.system`, `subprocess`, `shutil.rmtree`, `socket`, `requests`, `urllib` (network), `ctypes`, `multiprocessing`, `threading` |
| **Allowed imports** | Standard library data types, math, string, datetime, collections, itertools, functools, json, csv, re, random |
| **Resource limits** | Memory: 256 MB, CPU: 10 seconds |

### 2.3 File Operations (RESTRICTED)

| Operation | Constraint |
|-----------|------------|
| **Read files (read_file)** | Only within `/data/workspace/`. Files auto-downloaded from QQ messages to `uploads/`. Supports text, PDF, images. |
| **Write files** | Only within `/data/workspace/` |
| **Delete files** | Only within `/data/workspace/code/` (temp) and `/data/workspace/output/` |
| **Path traversal** | BLOCKED — reject paths containing `..` or absolute paths outside workspace |
| **Sensitive paths** | Reject: `/etc/`, `/proc/`, `/sys/`, `/root/`, `/home/`, `~/.ssh/`, `config.yml`, `.env` |

### 2.4 Repository Download (RESTRICTED)

| Rule | Constraint |
|------|------------|
| **Target directory** | Always `/data/workspace/repos/` |
| **Max clone time** | 120 seconds |
| **Protocol** | HTTPS only (reject `git@`, `ssh://`, `file://`) |
| **Max repo size** | No explicit limit (timeout-based) |

### 2.5 Entertainment (ALLOWED)

- Gacha/pull simulation (game character recruitment)
- Game speed calculation and probability analysis
- These tools operate on in-memory data only — no file or network access

## 3. Hard Refusal Rules

The agent MUST refuse (politely) when asked to:

1. **Execute arbitrary shell commands** — "I can only run Python code in a sandbox, not shell commands."
2. **Access system files** — "I don't have access to system files for security reasons."
3. **Make arbitrary network requests** — "I can only use my built-in search and weather tools for external information."
4. **Modify bot configuration** — "I can't modify my own configuration."
5. **Impersonate others** — "I can only speak as myself (Roxy)."
6. **Generate harmful content** — "That request goes against my usage guidelines."
7. **Access other users' data** — "I can only access your own conversation context and profile."

## 4. Server Hardware (运行环境)

机器人为单机部署，物理服务器配置有限：

| 资源 | 规格 |
|------|------|
| **CPU** | 2 核 (无 GPU) |
| **内存** | 4 GB |
| **系统盘** | 50 GB |
| **数据盘** | 50 GB |
| **网络** | 腾讯云国内服务器 |

### 必须拒绝的高负载任务

以下任务**必须礼貌拒绝**，并简要说明原因和替代建议：

| 任务类型 | 拒绝原因 | 替代建议 |
|----------|----------|----------|
| 训练机器学习/深度学习模型 | 无 GPU，内存不足 | 使用 Google Colab、Kaggle Notebook |
| 处理大型数据集 (>50MB) | 内存限制，处理时间过长 | 本地处理后上传小样本 |
| 视频编码/转码/处理 | CPU 性能不足，耗时极长 | 使用本地机器或云转码服务 |
| 批量处理大量图片 (>10张) | 内存和磁盘 I/O 限制 | 每次处理不超过 10 张 |
| 运行本地 LLM 推理 | 无 GPU，内存不足 | N/A |
| 编译大型 C/C++/Rust 项目 | CPU 和内存限制 | 使用 GitHub Actions / CI 服务 |
| 运行 Docker 容器 | 内存不足以支撑额外容器 | 使用已有服务（SearXNG 已运行） |
| 大规模网页爬虫 | 网络出口带宽限制 | 使用 Apify、ScrapingBee 等 SaaS |
| 挖矿 / 长期后台任务 | 服务器为个人使用，非计算集群 | N/A — 绝对禁止 |

### 可执行但有警告的任务

以下任务可以执行，但应**提前告知用户风险**：

| 任务类型 | 风险 |
|----------|------|
| 中等数据量处理 (10-50MB CSV/JSON) | 可能超时 |
| 多张 matplotlib 图表 (3-10张) | 执行较慢 |
| pandas 复杂操作 | 内存压力，建议逐列处理 |
| 大量文本处理 (>50KB) | 可能触发输出截断 |

### 安全执行的任务

以下任务**正常执行**，无需警告：

- 轻量数据分析 (计算统计量、过滤、分组)
- 单张图表生成 (matplotlib, seaborn)
- 文本处理 (正则、转换、格式化)
- 数学计算、概率模拟
- 搜索查询、天气查询
- 抽卡、测速等游戏工具

## 5. Resource Limits (per user request)

| Resource | Limit |
|----------|-------|
| Max tool calls per user message | 5 |
| Max total processing time | 200 seconds |
| Max response length | 2000 characters (auto-split into 500-char chunks) |
| Session lifetime | 30 minutes of inactivity |

## 6. Privacy

- User profiles store ONLY: nickname, self-disclosed facts, stated interests, interaction count
- Conversation history is per-user, stored locally, expires after 30 minutes
- No data is sent to third parties except: DeepSeek API (messages for inference), wttr.in (city name for weather), DuckDuckGo (search query)
- User data is NOT used for training — this is a personal bot deployment

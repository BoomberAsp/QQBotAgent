"""
Built-in Agent Tools — Core tools for information, code, and utility tasks.

These tools are registered with the agent on bootstrap:
1. Search for information (search_web) — SearXNG JSON API (covers weather/news/knowledge)
2. Write and return executable code (execute_code)
3. Summarize PDF (summarize_pdf)
4. Download a repository (download_repo)
5. Get current time (get_time)

Note: check_weather has been removed. Weather queries are handled by
search_web — the Agent searches for weather info and synthesizes it via LLM.

All file operations are confined to WORKSPACE_ROOT.
Security constraints are defined in agent/config/WORKSPACE.md.
"""

import asyncio
import glob
import os
import re
import shutil
import subprocess
import tempfile
import time
from datetime import datetime
from urllib.parse import quote

# ── Workspace Configuration ────────────────────────────────────────

# Production workspace root. Override via environment variable.
# Default uses the project's data directory for dev/portability.
def _get_workspace_root() -> str:
    env_ws = os.environ.get("QQBOT_WORKSPACE", "")
    if env_ws:
        return env_ws
    # Default: project-relative data/workspace/
    project_data = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data", "workspace",
    )
    return project_data

WORKSPACE_ROOT = _get_workspace_root()
WORKSPACE_CODE = os.path.join(WORKSPACE_ROOT, "code")
WORKSPACE_REPOS = os.path.join(WORKSPACE_ROOT, "repos")
WORKSPACE_UPLOADS = os.path.join(WORKSPACE_ROOT, "uploads")
WORKSPACE_OUTPUT = os.path.join(WORKSPACE_ROOT, "output")

# Forbidden patterns in code execution
FORBIDDEN_PATTERNS = [
    r'os\.system\s*\(',
    r'subprocess\.',
    r'shutil\.rmtree',
    r'shutil\.rmove',
    r'socket\.',
    r'requests\.',
    r'urllib\.',
    r'ctypes\.',
    r'multiprocessing\.',
    r'threading\.Thread',
    r'__import__\s*\(\s*[\'"]os[\'"]',
    r'__import__\s*\(\s*[\'"]subprocess[\'"]',
    r'__import__\s*\(\s*[\'"]shutil[\'"]',
    r'__import__\s*\(\s*[\'"]socket[\'"]',
    r'eval\s*\(',
    r'exec\s*\(',
    r'compile\s*\(',
    r'open\s*\([^)]*[\'"]w',  # File write (we allow file read)
    r'os\.remove\s*\(',
    r'os\.rmdir\s*\(',
    r'os\.unlink\s*\(',
    r'os\.chmod\s*\(',
    r'os\.chown\s*\(',
]
FORBIDDEN_PATTERNS_COMPILED = [re.compile(p) for p in FORBIDDEN_PATTERNS]

# Allowed import whitelist display (for error messages)
_ALLOWED_IMPORTS_HINT = (
    "Allowed modules: math, random, datetime, collections, itertools, "
    "functools, json, csv, re, string, statistics, dataclasses, typing, "
    "decimal, fractions, hashlib, base64, textwrap, heapq, bisect, copy"
)

MAX_OUTPUT_SIZE = 102400  # 100 KB
MAX_RUNTIME = 60  # seconds


# ── Helpers ─────────────────────────────────────────────────────────

def _ensure_workspace_dirs() -> None:
    """Create workspace directories if they don't exist."""
    for d in [WORKSPACE_CODE, WORKSPACE_REPOS, WORKSPACE_UPLOADS, WORKSPACE_OUTPUT]:
        os.makedirs(d, exist_ok=True)


def _validate_path(file_path: str, must_exist: bool = True) -> tuple[str | None, str | None]:
    """Validate a file path is safe and within workspace.

    Returns (safe_abs_path, error_message).
    """
    if not file_path:
        return None, "路径为空。"

    # Reject path traversal
    if ".." in file_path:
        return None, f"路径包含非法字符 '..' : {file_path}"

    # Reject home directory shortcuts
    if file_path.startswith("~"):
        return None, f"不允许使用 ~ 路径: {file_path}"

    # Resolve to absolute path
    abs_path = os.path.abspath(file_path)

    # If relative, assume under workspace
    if not os.path.isabs(file_path) or not file_path.startswith("/"):
        abs_path = os.path.join(WORKSPACE_ROOT, file_path)
        abs_path = os.path.abspath(abs_path)

    # Check within workspace
    workspace_real = os.path.realpath(WORKSPACE_ROOT)
    path_real = os.path.realpath(abs_path)
    if not path_real.startswith(workspace_real + os.sep) and path_real != workspace_real:
        return None, (
            f"路径超出工作区范围。所有文件操作必须限于 {WORKSPACE_ROOT}/ 目录下。\n"
            f"请求路径: {file_path}\n"
            f"解析后: {abs_path}"
        )

    # Reject sensitive system paths (only if NOT already within workspace)
    forbidden_prefixes = ["/etc/", "/proc/", "/sys/", "/root/"]
    for prefix in forbidden_prefixes:
        if path_real.startswith(prefix):
            return None, f"不允许访问系统目录: {prefix}..."

    if must_exist and not os.path.exists(abs_path):
        return None, f"文件不存在: {file_path}"

    return abs_path, None


def _validate_repo_url(url: str) -> tuple[str | None, str | None]:
    """Validate a git repository URL is safe (HTTPS only).

    Returns (safe_url, error_message).
    """
    if not url:
        return None, "仓库 URL 为空。"

    # Only allow HTTPS
    if not url.startswith("https://"):
        return None, f"只支持 HTTPS 协议的仓库地址。不支持的协议: {url.split('://')[0] if '://' in url else 'unknown'}"

    # Reject obviously malicious patterns
    dangerous = [";", "|", "`", "$(", "${", "&&", "||", ">", "<"]
    for char in dangerous:
        if char in url:
            return None, f"仓库 URL 包含非法字符: '{char}'"

    # Basic URL format check
    if not re.match(r'^https://[^\s]+\.git$', url) and not re.match(r'^https://[^\s]+$', url):
        # Allow both .git and non-.git URLs
        pass

    return url, None


# ── Time ─────────────────────────────────────────────────────────

def get_time() -> str:
    """Return the current date and time."""
    now = datetime.now()
    weekday = ['一', '二', '三', '四', '五', '六', '日'][now.weekday()]
    return f"当前时间: {now.strftime('%Y年%m月%d日 %H:%M:%S')} (星期{weekday})"


# ── Web Search (SearXNG JSON API) ────────────────────────────────

# SearXNG endpoint — configurable via environment variable
_SEARXNG_ENDPOINT = os.environ.get("SEARXNG_ENDPOINT", "http://localhost:8082")

# Fallback engines for when SearXNG is unreachable (read-only HTTP APIs)
_FALLBACK_ENGINES = {
    "weather": "https://wttr.in/{query}?format=%C+%t+%h+%w&lang=zh",
}


def search_web(query: str, num_results: int = 5) -> str:
    """Search the web using SearXNG JSON API.

    SearXNG is a self-hosted metasearch engine that aggregates results
    from Google, DuckDuckGo, Bing, Wikipedia, and more.

    This single tool handles ALL information retrieval: web search,
    weather, news, facts, encyclopedia lookups, etc.

    Args:
        query: Search query string (e.g. "深圳天气", "Python async tutorial").
        num_results: Number of results to return (max 10, default 5).
    """
    import urllib.request
    import json as _json

    query = query.strip()
    if not query:
        return "[Search] 请提供搜索关键词。"

    num_results = min(max(num_results, 1), 10)

    try:
        # Build SearXNG JSON API request
        params = {
            "q": query,
            "format": "json",
            "language": "zh-CN",
            "safesearch": "1",
            "categories": "general",
        }
        param_str = "&".join(f"{k}={quote(str(v))}" for k, v in params.items())
        url = f"{_SEARXNG_ENDPOINT}/search?{param_str}"

        req = urllib.request.Request(url, headers={
            "User-Agent": "QQBot-Agent/2.0",
            "Accept": "application/json",
        })

        with urllib.request.urlopen(req, timeout=15) as resp:
            data = _json.loads(resp.read().decode("utf-8"))

        results = data.get("results", [])
        if not results:
            return f"搜索 '{query}' 未找到相关结果。\n\n建议:\n1. 尝试更具体的关键词\n2. 使用英文关键词重试\n3. 检查 SearXNG 服务是否正常运行"

        # Format results
        lines = [f"搜索 '{query}' 的结果 ({len(results)} 条):\n"]
        for i, r in enumerate(results[:num_results]):
            title = r.get("title", "无标题").strip()
            snippet = r.get("content", "").strip()
            url_str = r.get("url", "")
            engine = r.get("engine", "unknown")

            # Clean up HTML entities and excess whitespace
            snippet = re.sub(r'<[^>]+>', '', snippet)
            snippet = re.sub(r'\s+', ' ', snippet)
            snippet = snippet[:300] + "..." if len(snippet) > 300 else snippet

            lines.append(
                f"{i + 1}. **{title}**\n"
                f"   {snippet}\n"
                f"   来源: {engine} | URL: {url_str}"
            )

        return "\n\n".join(lines)

    except urllib.request.HTTPError as e:
        return (
            f"[Search] SearXNG 请求失败 (HTTP {e.code}): {e.reason}\n\n"
            f"查询: {query}\n"
            f"请检查 SearXNG 服务状态。"
        )
    except urllib.request.URLError as e:
        return (
            f"[Search] 无法连接到 SearXNG ({_SEARXNG_ENDPOINT}): {e.reason}\n\n"
            f"查询: {query}\n\n"
            f"建议:\n"
            f"1. Docker 部署: 确保 searxng 容器已启动 (docker compose up -d searxng)\n"
            f"2. 手动部署: 设置环境变量 SEARXNG_ENDPOINT 指向可用的 SearXNG 实例\n"
            f"3. 备用: 直接搜索 https://www.google.com/search?q={quote(query)}"
        )
    except _json.JSONDecodeError as e:
        return f"[Search] SearXNG 返回了无效的 JSON: {e}"
    except Exception as e:
        return (
            f"[Search] 搜索 '{query}' 时出现意外错误: {e}\n\n"
            f"建议: 自行搜索 https://www.google.com/search?q={quote(query)}"
        )


# ── Code Execution ───────────────────────────────────────────────

def _check_code_safety(code: str) -> str | None:
    """Check code for dangerous patterns. Returns error message or None if safe."""
    for i, pattern in enumerate(FORBIDDEN_PATTERNS_COMPILED):
        if pattern.search(code):
            forbidden = FORBIDDEN_PATTERNS[i]
            return (
                f"[Security] 代码包含不安全的操作: 匹配规则 `{forbidden}`\n\n"
                f"代码执行安全限制:\n"
                f"- 禁止执行 shell 命令 (os.system, subprocess)\n"
                f"- 禁止网络访问 (socket, requests, urllib)\n"
                f"- 禁止文件写入/删除操作\n"
                f"- 禁止动态代码执行 (eval, exec, compile)\n"
                f"- 仅允许纯计算和数据处理\n\n"
                f"{_ALLOWED_IMPORTS_HINT}"
            )
    return None


async def execute_code(code: str, timeout: int = 30) -> str:
    """Execute Python code in an isolated workspace and return output.

    Generated image files (charts, plots) are detected, saved to the output
    directory, and sent to the QQ chat automatically.

    Security:
    - Runs in a temporary directory under /data/workspace/code/
    - Blocks dangerous patterns (shell, network, file write, dynamic exec)
    - Clean environment variables
    - Strict timeout (max 60s)
    - Output size limit (100KB)

    Args:
        code: Python source code to execute.
        timeout: Max execution time in seconds (max 60).
    """
    # Validate code
    code = code.strip()
    if not code:
        return "[Code Error] 代码为空，请提供要执行的 Python 代码。"

    # Security check
    safety_error = _check_code_safety(code)
    if safety_error:
        return safety_error

    # Clamp timeout
    timeout = min(max(timeout, 1), MAX_RUNTIME)

    # Ensure workspace exists
    _ensure_workspace_dirs()

    # Create isolated temp directory
    try:
        work_dir = tempfile.mkdtemp(dir=WORKSPACE_CODE, prefix="exec_")
    except Exception as e:
        return f"[Code Error] 无法创建工作目录: {e}"

    # Write code to file (avoids shell escaping issues)
    code_file = os.path.join(work_dir, "script.py")
    try:
        with open(code_file, "w", encoding="utf-8") as f:
            f.write("# -*- coding: utf-8 -*-\n")
            f.write(code)
    except Exception as e:
        shutil.rmtree(work_dir, ignore_errors=True)
        return f"[Code Error] 无法写入代码文件: {e}"

    # Build restricted environment
    clean_env = {
        "PATH": "/usr/bin:/usr/local/bin",
        "HOME": work_dir,
        "TMPDIR": work_dir,
        "TEMP": work_dir,
        "TMP": work_dir,
        "PYTHONPATH": work_dir,
        "PYTHONDONTWRITEBYTECODE": "1",  # Don't generate .pyc files
        "LANG": "en_US.UTF-8",
    }

    # Image extensions to detect from generated output
    _IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".svg", ".gif", ".webp", ".pdf"}

    saved_images = []
    try:
        result = subprocess.run(
            ["python3", "-I", code_file],  # -I = isolated mode (ignore PYTHON* env vars, no site-packages)
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=work_dir,
            env=clean_env,
        )
        output = ""
        if result.stdout:
            stdout = result.stdout[:MAX_OUTPUT_SIZE]
            if len(result.stdout) > MAX_OUTPUT_SIZE:
                stdout += "\n... (输出过长，已截断)"
            output += f"标准输出:\n{stdout}\n"
        if result.stderr:
            stderr = result.stderr[:MAX_OUTPUT_SIZE]
            if len(result.stderr) > MAX_OUTPUT_SIZE:
                stderr += "\n... (错误输出过长，已截断)"
            output += f"标准错误:\n{stderr}\n"
        if not output:
            output = "(代码执行完成，无输出)"

        # Include exit code if non-zero
        if result.returncode != 0:
            output += f"\n(退出码: {result.returncode})"

        # ── Detect generated image files ──────────────────────────
        for filename in sorted(os.listdir(work_dir)):
            ext = os.path.splitext(filename)[1].lower()
            if ext in _IMAGE_EXTS and filename != "script.py":
                src = os.path.join(work_dir, filename)
                timestamp = time.strftime("%Y%m%d_%H%M%S")
                dest_name = f"chart_{timestamp}_{filename}"
                dest = os.path.join(WORKSPACE_OUTPUT, dest_name)
                shutil.copy2(src, dest)
                saved_images.append(dest)

        # ── Send images to QQ chat if context variable is set ─────
        if saved_images:
            try:
                from agent.context import _send_msg
                from nonebot.adapters.onebot.v11 import MessageSegment

                send = _send_msg.get()
                if send is not None:
                    for img_path in saved_images:
                        try:
                            await send(MessageSegment.image(f"file://{img_path}"))
                            await asyncio.sleep(0.3)  # Small delay between images
                        except Exception:
                            pass  # Image send failed, continue with other images
            except ImportError:
                pass  # Not in QQ context, skip image sending

            # Append image paths to output
            img_list = "\n".join(f"  → {os.path.basename(p)}" for p in saved_images)
            output += f"\n\n📊 生成的图表 ({len(saved_images)} 个):\n{img_list}"

        return output.strip()

    except subprocess.TimeoutExpired:
        return (
            f"[Code Error] 代码执行超时 ({timeout}秒)。\n"
            f"请检查:\n"
            f"1. 是否存在死循环?\n"
            f"2. 计算量是否过大?\n"
            f"3. 超时限制可通过 timeout 参数调整 (最大 {MAX_RUNTIME}秒)"
        )
    except Exception as e:
        return f"[Code Error] 代码执行失败: {e}"
    finally:
        # Always clean up the temp workspace
        try:
            shutil.rmtree(work_dir, ignore_errors=True)
        except Exception:
            pass


# ── PDF Summarization ────────────────────────────────────────────

def summarize_pdf(file_path: str) -> str:
    """Extract and summarize text from a PDF file.

    The file must be within /data/workspace/.

    Args:
        file_path: Path to the PDF file (relative to workspace or absolute within workspace).
    """
    # Validate path
    safe_path, error = _validate_path(file_path)
    if error:
        return f"[PDF Error] {error}"

    try:
        try:
            from PyPDF2 import PdfReader
        except ImportError:
            return (
                "[PDF Error] PyPDF2 未安装。请先在服务器上安装:\n"
                "pip install PyPDF2\n\n"
                "安装后重新发送 PDF 文件即可。"
            )

        reader = PdfReader(safe_path)
        num_pages = len(reader.pages)
        text_parts = []
        for i, page in enumerate(reader.pages):
            page_text = page.extract_text()
            if page_text:
                text_parts.append(f"--- 第{i + 1}页 ---\n{page_text}")

        full_text = "\n".join(text_parts)

        if not full_text.strip():
            return f"[PDF] PDF 文件共 {num_pages} 页，但无法提取文本内容（可能是扫描版 PDF）。"

        max_chars = 8000
        if len(full_text) > max_chars:
            full_text = (
                full_text[:max_chars]
                + f"\n\n... (文本过长，已截断至前{max_chars}字符，共{num_pages}页)"
            )

        return f"PDF 摘要 ({num_pages} 页):\n\n{full_text}"

    except Exception as e:
        return f"[PDF Error] 处理 PDF 时出错: {e}"


# ── Repository Download ──────────────────────────────────────────

def download_repo(repo_url: str, target_dir: str = None) -> str:
    """Clone a git repository to /data/workspace/repos/.

    Only HTTPS URLs are accepted. Target directory is always forced to workspace.

    Args:
        repo_url: Git repository URL (HTTPS only).
        target_dir: Ignored — always uses /data/workspace/repos/.
    """
    # Validate URL
    safe_url, error = _validate_repo_url(repo_url)
    if error:
        return f"[Git Error] {error}"

    # Always force target to workspace
    _ensure_workspace_dirs()
    target_dir = WORKSPACE_REPOS

    repo_name = safe_url.rstrip("/").split("/")[-1].replace(".git", "")
    # Sanitize repo name (prevent path tricks)
    repo_name = re.sub(r'[^\w\-_.]', '_', repo_name)
    if not repo_name:
        repo_name = "repo"
    clone_path = os.path.join(target_dir, repo_name)

    # Double-check resolved path is within workspace
    resolved, path_error = _validate_path(clone_path, must_exist=False)
    if path_error and "文件不存在" not in path_error:
        # Only reject if the error is about path traversal, not file-not-found
        return f"[Git Error] {path_error}"

    try:
        if os.path.exists(clone_path):
            result = subprocess.run(
                ["git", "-C", clone_path, "pull"],
                capture_output=True, text=True, timeout=60,
            )
            return f"仓库已存在，已更新:\n{result.stdout}\n路径: {clone_path}"
        else:
            result = subprocess.run(
                ["git", "clone", safe_url, clone_path],
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode == 0:
                files = sorted(os.listdir(clone_path))[:20]
                file_list = "\n".join(f"  - {f}" for f in files)
                total_files = len(os.listdir(clone_path))
                more = f"\n  ... 及其他 {total_files - 20} 个文件" if total_files > 20 else ""
                return (
                    f"仓库克隆成功!\n"
                    f"URL: {safe_url}\n"
                    f"路径: {clone_path}\n"
                    f"文件列表:\n{file_list}{more}"
                )
            else:
                return f"[Git Error] 克隆失败:\n{result.stderr}"
    except subprocess.TimeoutExpired:
        return "[Git Error] 仓库下载超时 (120秒)。仓库可能过大，请手动下载。"
    except FileNotFoundError:
        return "[Git Error] 服务器未安装 git。请先安装: apt-get install git"
    except Exception as e:
        return f"[Git Error] 下载仓库时出错: {e}"

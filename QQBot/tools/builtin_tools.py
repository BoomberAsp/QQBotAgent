"""
Built-in Agent Tools — Core tools for information, code, and utility tasks.

These tools are registered with the agent on bootstrap:
1. Search for information (search_web) — SearXNG JSON API (covers weather/news/knowledge)
2. Fetch web page content (web_fetch) — HTTPS URL fetcher with HTML-to-text extraction
3. Write and return executable code (execute_code)
4. Summarize PDF (summarize_pdf)
5. Download a repository (download_repo)
6. Get current time (get_time)

Note: check_weather has been removed. Weather queries are handled by
the get_weather tool (Amap API) or search_web as a fallback.

All file operations are confined to WORKSPACE_ROOT.
Security constraints are defined in agent/config/WORKSPACE.md.
"""

import asyncio
import glob
import ipaddress
import os
import re
import shlex
import shutil
import socket
import subprocess
import tempfile
import time
from datetime import datetime
from urllib.parse import quote, urlparse

# ── Workspace Configuration ────────────────────────────────────────

# Production workspace root. Override via environment variable.
# Default uses the project's data directory for dev/portability.
def _get_workspace_root() -> str:
    # Check for per-user workspace override (set by agent_router via contextvar)
    try:
        from agent.context import _current_user_workspace
    except ImportError:
        try:
            from QQBot.agent.context import _current_user_workspace
        except ImportError:
            _current_user_workspace = None
    if _current_user_workspace:
        user_ws = _current_user_workspace.get()
        if user_ws:
            return user_ws

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
    """Create workspace directories if they don't exist.

    Uses _get_workspace_root() at runtime so that user workspace
    contextvar overrides are respected (not frozen at import time).
    """
    root = _get_workspace_root()
    for sub in ["code", "repos", "uploads", "output"]:
        os.makedirs(os.path.join(root, sub), exist_ok=True)


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

    # Use runtime workspace root (respects per-user contextvar), not frozen import-time constant
    workspace_root = _get_workspace_root()

    # If relative, assume under workspace
    if not os.path.isabs(file_path) or not file_path.startswith("/"):
        abs_path = os.path.join(workspace_root, file_path)
        abs_path = os.path.abspath(abs_path)

    # Check within workspace
    workspace_real = os.path.realpath(workspace_root)
    path_real = os.path.realpath(abs_path)
    if not path_real.startswith(workspace_real + os.sep) and path_real != workspace_real:
        return None, (
            f"路径超出工作区范围。所有文件操作必须限于 {workspace_root}/ 目录下。\n"
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


# ── Web Fetch ────────────────────────────────────────────────────

# Maximum response size for web_fetch (2 MB)
_MAX_FETCH_SIZE = 2 * 1024 * 1024
# Maximum text output for web_fetch (characters)
_MAX_FETCH_OUTPUT = 8000
_FETCH_TIMEOUT = 30.0

# Private/special-use networks blocked for SSRF prevention
_SSRF_BLOCKED_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),       # Loopback
    ipaddress.ip_network("10.0.0.0/8"),        # Private (Class A)
    ipaddress.ip_network("172.16.0.0/12"),     # Private (Class B)
    ipaddress.ip_network("192.168.0.0/16"),    # Private (Class C)
    ipaddress.ip_network("169.254.0.0/16"),    # Link-local (APIPA)
    ipaddress.ip_network("0.0.0.0/8"),         # "This" network
    ipaddress.ip_network("100.64.0.0/10"),     # CGNAT (RFC 6598)
    ipaddress.ip_network("198.18.0.0/15"),     # Benchmarking (RFC 2544)
    ipaddress.ip_network("224.0.0.0/4"),       # Multicast
    ipaddress.ip_network("240.0.0.0/4"),       # Reserved
    ipaddress.ip_network("::1/128"),           # IPv6 loopback
    ipaddress.ip_network("fc00::/7"),          # IPv6 unique local
    ipaddress.ip_network("fe80::/10"),         # IPv6 link-local
]


def _check_ssrf(url: str) -> str | None:
    """Check if a URL targets a private/internal IP (SSRF prevention).

    Resolves the hostname and checks all returned IPs against blocked
    network ranges. Returns an error message string if any IP is private,
    or None if the URL is safe.
    """
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
        if not hostname:
            return f"[SSRF] 无法从 URL 中提取主机名: {url}"

        # Resolve hostname to IPs
        try:
            addrinfo = socket.getaddrinfo(hostname, None)
        except socket.gaierror:
            return f"[SSRF] 无法解析主机名: {hostname}"

        # Check every resolved IP
        for info in addrinfo:
            ip_str = info[4][0]  # (family, type, proto, canonname, sockaddr)
            try:
                ip_addr = ipaddress.ip_address(ip_str)
            except ValueError:
                continue

            for network in _SSRF_BLOCKED_NETWORKS:
                if ip_addr in network:
                    return (
                        f"[SSRF] 拒绝访问内部/私有地址: {ip_str} "
                        f"(网段: {network}, 主机: {hostname})。"
                        f"仅允许访问公网地址。"
                    )

        return None  # Safe

    except Exception as e:
        return f"[SSRF] 安全检查失败: {type(e).__name__}: {e}"


def _html_to_text(html: str) -> str:
    """Strip HTML tags and extract readable text."""
    from html.parser import HTMLParser

    class TextExtractor(HTMLParser):
        def __init__(self):
            super().__init__()
            self.parts = []
            self.skip_tags = {"script", "style", "noscript", "head"}

        def handle_starttag(self, tag, attrs):
            if tag in ("br", "p", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "div", "section", "article"):
                self.parts.append("\n")

        def handle_endtag(self, tag):
            if tag in ("br", "p", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "div", "section", "article"):
                self.parts.append("\n")

        def handle_data(self, data):
            self.parts.append(data)

    extractor = TextExtractor()
    extractor.feed(html)
    text = "".join(extractor.parts)
    # Collapse whitespace
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n +", "\n", text)
    return text.strip()


async def web_fetch(url: str) -> str:
    """Fetch content from a URL and return the extracted text.

    Only HTTPS URLs are accepted. HTML pages are parsed and converted
    to plain text. Non-HTML content is returned as-is (truncated).

    Args:
        url: The URL to fetch (HTTPS only).
    """
    import httpx

    url = url.strip()
    if not url:
        return "[WebFetch] 请提供 URL。"

    # HTTPS only
    if not url.startswith("https://"):
        return f"[WebFetch] 仅支持 HTTPS 协议。不支持的协议: {url.split('://')[0] if '://' in url else 'unknown'}"

    # Reject dangerous characters
    dangerous = [";", "|", "`", "$(", "${", "&&", "||"]
    for char in dangerous:
        if char in url:
            return f"[WebFetch] URL 包含非法字符: '{char}'"

    # SSRF check: resolve DNS and verify target is not a private IP
    ssrf_error = _check_ssrf(url)
    if ssrf_error:
        return ssrf_error

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                url,
                headers={
                    "User-Agent": "QQBot-Agent/2.0",
                    "Accept": "text/html,text/plain,application/json,*/*",
                    "Accept-Language": "zh-CN,en;q=0.9",
                },
                timeout=_FETCH_TIMEOUT,
                follow_redirects=True,
            )
            response.raise_for_status()

            content_type = response.headers.get("content-type", "").lower()
            content = response.content[:_MAX_FETCH_SIZE]
            truncated = len(response.content) > _MAX_FETCH_SIZE

            if "text/html" in content_type:
                text = _html_to_text(content.decode("utf-8", errors="replace"))
            elif "text/plain" in content_type or "application/json" in content_type:
                text = content.decode("utf-8", errors="replace")
            else:
                text = f"(非文本内容: {content_type}, 大小: {len(content)} 字节)"

            if truncated:
                text += f"\n\n... (内容已截断，原大小 {len(response.content)} 字节，限制 {_MAX_FETCH_SIZE} 字节)"

            if len(text) > _MAX_FETCH_OUTPUT:
                text = text[:_MAX_FETCH_OUTPUT] + f"\n\n... (输出已截断至 {_MAX_FETCH_OUTPUT} 字符)"

            return text or "(页面内容为空)"

    except httpx.ConnectTimeout:
        return f"[WebFetch] 连接超时 ({_FETCH_TIMEOUT}秒): {url}"
    except httpx.ReadTimeout:
        return f"[WebFetch] 读取超时 ({_FETCH_TIMEOUT}秒): {url}"
    except httpx.HTTPStatusError as e:
        return f"[WebFetch] HTTP 错误 ({e.response.status_code}): {url}"
    except httpx.InvalidURL:
        return f"[WebFetch] 无效的 URL: {url}"
    except Exception as e:
        return f"[WebFetch] 抓取失败: {type(e).__name__}: {e}"


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


# ── AST-Level Code Safety ──────────────────────────────────────────

# Allowed import modules (same as _ALLOWED_IMPORTS_HINT)
_AST_ALLOWED_IMPORTS = {
    "math", "random", "datetime", "collections", "itertools",
    "functools", "json", "csv", "re", "string", "statistics",
    "dataclasses", "typing", "decimal", "fractions", "hashlib",
    "base64", "textwrap", "heapq", "bisect", "copy",
    # Sub-modules of allowed packages
    "collections.abc", "typing.re",
}

# Built-in functions that are always safe to call
_AST_SAFE_BUILTINS = {
    "abs", "all", "any", "ascii", "bin", "bool", "bytearray", "bytes",
    "callable", "chr", "classmethod", "complex", "copyright", "credits",
    "dict", "dir", "divmod", "enumerate", "filter", "float", "format",
    "frozenset", "getattr", "hasattr", "hash", "help", "hex", "id",
    "input", "int", "isinstance", "issubclass", "iter", "len", "license",
    "list", "locals", "map", "max", "memoryview", "min", "next",
    "object", "oct", "ord", "pow", "print", "property", "range",
    "repr", "reversed", "round", "set", "setattr", "slice", "sorted",
    "staticmethod", "str", "sum", "super", "tuple", "type", "vars",
    "zip", "__build_class__", "__import__",
}

# Dangerous builtin calls that must be blocked (even though they're builtins)
_AST_BLOCKED_BUILTINS = {
    "__import__",  # Dynamic import bypass
    "eval",
    "exec",
    "compile",
    "open",
    "breakpoint",
}

# Dangerous attribute roots (modules/objects that shouldn't be accessed)
_AST_BLOCKED_ATTRIBUTE_ROOTS = {
    "os", "subprocess", "socket", "shutil", "ctypes",
    "sys", "importlib", "builtins", "code", "codeop",
    "ptrace", "multiprocessing", "threading", "signal",
    "requests", "urllib", "http", "ftplib", "telnetlib",
    "smtplib", "imaplib", "poplib",
}


def _check_code_safety_ast(code: str) -> str | None:
    """AST-level security check for Python code.

    Parses the code and walks the AST to validate:
    - Imports are from the allowed whitelist
    - No dangerous builtin calls (eval, exec, compile, open, __import__)
    - No access to dangerous module attributes (os, subprocess, etc.)

    This is the second layer of defense, after regex pattern matching.
    AST parsing cannot be bypassed by whitespace, comments, or string
    obfuscation tricks that would fool regex.

    Returns an error message string, or None if the code passes.
    """
    import ast as _ast

    try:
        tree = _ast.parse(code)
    except SyntaxError as e:
        return f"[Security] 代码语法错误，无法进行安全检查: {e}"

    # Collect all errors before reporting
    errors: list[str] = []

    for node in _ast.walk(tree):
        # ── Check imports ────────────────────────────────────
        if isinstance(node, _ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] not in _AST_ALLOWED_IMPORTS:
                    errors.append(
                        f"不允许的导入: `import {alias.name}`。{_ALLOWED_IMPORTS_HINT}"
                    )

        elif isinstance(node, _ast.ImportFrom):
            if node.module is None:
                # Relative import without module: "from . import foo"
                errors.append("不允许相对导入。")
            elif node.module.split(".")[0] not in _AST_ALLOWED_IMPORTS:
                errors.append(
                    f"不允许的导入: `from {node.module} import ...`。{_ALLOWED_IMPORTS_HINT}"
                )

        # ── Check function calls ──────────────────────────────
        elif isinstance(node, _ast.Call):
            # Direct calls to dangerous builtins: eval(), exec(), etc.
            if isinstance(node.func, _ast.Name):
                if node.func.id in _AST_BLOCKED_BUILTINS:
                    errors.append(
                        f"禁止调用: `{node.func.id}()`。出于安全考虑，该函数已被禁用。"
                    )

            # Attribute access: os.system(), subprocess.run(), etc.
            elif isinstance(node.func, _ast.Attribute):
                root = _get_attr_root(node.func)
                if root in _AST_BLOCKED_ATTRIBUTE_ROOTS:
                    errors.append(
                        f"禁止访问模块: `{root}`。"
                        f"该模块不允许在沙箱中使用。"
                    )

    if errors:
        unique = list(dict.fromkeys(errors))  # deduplicate while preserving order
        return "[Security] AST 安全检查失败:\n- " + "\n- ".join(unique)

    return None


def _get_attr_root(node) -> str | None:
    """Extract the root of an attribute chain.

    Example: os.path.join → "os"
             subprocess.run → "subprocess"
             foo.bar.baz → "foo"
    """
    import ast as _ast

    if isinstance(node, _ast.Attribute):
        inner = node.value
        if isinstance(inner, _ast.Name):
            return inner.id
        elif isinstance(inner, _ast.Attribute):
            return _get_attr_root(inner)
    return None


def _get_code_limits() -> dict:
    """Read tiered code execution limits from permission context.

    Returns a dict with max_timeout (seconds), max_output (bytes),
    and max_memory_mb. If the contextvar is not set, returns empty
    dict and the callers use their defaults.
    """
    try:
        from agent.context import _current_code_limits
    except ImportError:
        try:
            from QQBot.agent.context import _current_code_limits
        except ImportError:
            return {}
    return _current_code_limits.get({})


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

    # Security check (layer 1: regex patterns)
    safety_error = _check_code_safety(code)
    if safety_error:
        return safety_error

    # Security check (layer 2: AST whitelist)
    ast_error = _check_code_safety_ast(code)
    if ast_error:
        return ast_error

    # Read tiered limits from permission context (default: full access)
    limits = _get_code_limits()
    max_timeout = limits.get("max_timeout", MAX_RUNTIME)
    max_output = limits.get("max_output", MAX_OUTPUT_SIZE)

    # Clamp timeout to role-specific max
    timeout = min(max(timeout, 1), max_timeout)

    # Ensure workspace exists
    _ensure_workspace_dirs()

    # Create isolated temp directory
    try:
        work_dir = tempfile.mkdtemp(dir=os.path.join(_get_workspace_root(), "code"), prefix="exec_")
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
            stdout = result.stdout[:max_output]
            if len(result.stdout) > max_output:
                stdout += "\n... (输出过长，已截断)"
            output += f"标准输出:\n{stdout}\n"
        if result.stderr:
            stderr = result.stderr[:max_output]
            if len(result.stderr) > max_output:
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
                dest = os.path.join(_get_workspace_root(), "output", dest_name)
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
    """Clone a git repository to the current user's workspace repos/ directory.

    Only HTTPS URLs are accepted. Target directory is always forced to the
    current user's workspace (resolved via _get_workspace_root() at runtime).

    Args:
        repo_url: Git repository URL (HTTPS only).
        target_dir: Ignored — always uses {user_workspace}/repos/.
    """
    # Validate URL
    safe_url, error = _validate_repo_url(repo_url)
    if error:
        return f"[Git Error] {error}"

    # Always force target to workspace
    _ensure_workspace_dirs()
    target_dir = os.path.join(_get_workspace_root(), "repos")

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


# ── Shell Command Execution ───────────────────────────────────────

# Whitelist: commands allowed in shell_exec
_SHELL_WHITELIST = {
    # File listing & metadata
    "ls", "find", "tree", "file", "stat", "realpath", "readlink",
    "basename", "dirname", "pwd",
    # Content viewing (read-only)
    "cat", "head", "tail", "zcat", "bzcat", "xzcat",
    # Text processing
    "grep", "wc", "sort", "uniq", "cut", "tr", "awk", "sed",
    "echo", "printf", "diff", "cmp", "paste", "join", "column",
    # System info (read-only)
    "date", "which", "df", "free", "uptime", "uname", "ps", "nproc",
    # Hashes
    "md5sum", "sha1sum", "sha256sum", "sha512sum",
    # Size & disk
    "du",
    # Binary inspection
    "xxd", "hexdump", "od", "strings",
}

# Commands with subcommand-level restrictions
_SHELL_SUBCOMMAND_WHITELIST = {
    "git": {
        "status", "log", "show", "diff", "branch", "tag",
        "rev-parse", "ls-files", "describe", "rev-list",
        "shortlog", "stash", "remote", "config", "ls-remote",
        "for-each-ref", "name-rev", "merge-base", "cherry",
    },
    "pip": {
        "list", "show", "freeze",
    },
}

# Sed flags blocked (can write files)
_SED_BLOCKED_FLAGS = {"-i", "--in-place"}

# Shell redirects — reject output/input redirects
_REDIRECT_RE = re.compile(
    r'(?<![=<>\-])\s*\d?>>?\s*\S|'    # > file, >> file, 2> file
    r'(?<![=<>\-])\s*<(?![<=])'        # < file (but not <<< heredoc or <=)
)

# Max output for shell commands
_MAX_SHELL_OUTPUT = 102400  # 100 KB
_MAX_SHELL_TIMEOUT = 30  # seconds


def _split_pipeline(cmd: str) -> list[str]:
    """Split a command on unquoted pipe characters only.

    Pipes inside single/double quotes are treated as literal data,
    not as pipeline separators. Consecutive pipes (||) are NOT split —
    they are shell logic operators, not pipeline separators.
    """
    segments = []
    current = []
    in_single = False
    in_double = False
    i = 0
    while i < len(cmd):
        ch = cmd[i]
        # Handle backslash escapes
        if ch == '\\' and i + 1 < len(cmd):
            current.append(ch)
            current.append(cmd[i + 1])
            i += 2
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == '|' and not in_single and not in_double:
            # Don't split on || (OR operator) or | at start of command
            if i + 1 < len(cmd) and cmd[i + 1] == '|':
                current.append('||')
                i += 2
                continue
            segments.append(''.join(current).strip())
            current = []
            i += 1
            continue
        current.append(ch)
        i += 1
    if current:
        segments.append(''.join(current).strip())
    return segments


def _validate_shell_command(command: str) -> str | None:
    """Validate a shell command against the whitelist and safety rules.

    Uses shlex.split() for proper shell quoting handling: text inside
    quotes (single, double) is treated as literal data, not syntax.

    Returns an error message string, or None if the command is safe.
    """
    if not command or not command.strip():
        return "命令为空。"

    cmd = command.strip()

    # ── Check for redirects (>/>>/<) ───────────────────────────
    if _REDIRECT_RE.search(cmd):
        return "[Shell] 不允许重定向 (> / >> / <)。请使用管道 (|) 代替。"

    # ── Split pipeline on unquoted | and validate each segment ─
    pipeline = _split_pipeline(cmd)
    for segment in pipeline:
        if not segment:
            return "[Shell] 管道语法错误: 空命令。"

        try:
            tokens = shlex.split(segment)
        except ValueError as e:
            return f"[Shell] 命令解析错误: {e}"

        if not tokens:
            return "[Shell] 管道语法错误: 空命令。"

        cmd_name = os.path.basename(tokens[0])  # Strip path if present (e.g. /usr/bin/ls)

        # ── Check for dangerous operators ────────────────────
        # Standalone operators (correctly spaced): ; && || &
        # Suffix-attached operators (no space before): /etc/passwd; ls
        # Embedded operators (no-space chaining): cat /etc/passwd;ls
        # Commands whose arguments may legitimately contain ; (scripts/patterns)
        _SEMICOLON_SAFE_COMMANDS = {"awk", "sed", "echo", "printf", "grep"}
        for i, tok in enumerate(tokens):
            if tok in (";", "&&", "||", "&"):
                return f"[Shell] 不允许命令链接符号: {tok}"
            # Catch ; or & attached to preceding argument: "/etc/passwd; ls"
            if tok.endswith(";") or (tok.endswith("&") and not tok.endswith("&&")):
                return f"[Shell] 不允许命令链接符号 (在 '{tok}' 中检测到 ';' 或 '&' 后缀)"
            # Catch ; embedded mid-token: "/etc/passwd;ls"
            if ";" in tok:
                if cmd_name not in _SEMICOLON_SAFE_COMMANDS:
                    return f"[Shell] 不允许命令链接符号 (在 '{tok}' 中检测到 ';')"
            # Catch && / || embedded anywhere (no-space chaining or inside args)
            if "&&" in tok or "||" in tok:
                return f"[Shell] 不允许命令链接符号 (在 '{tok}' 中检测到 '&&' 或 '||')"
            # Check for backtick command substitution
            if tok.startswith("`") and tok.endswith("`"):
                return "[Shell] 不允许反引号命令替换 (``)。"
            # Check for $(...) pattern
            if tok.startswith("$(") or "${" in tok:
                return "[Shell] 不允许命令替换 $(...) 或变量展开 ${...}。"

        # ── Check command whitelist ──────────────────────────
        if cmd_name in _SHELL_SUBCOMMAND_WHITELIST:
            # Validate subcommand
            allowed_subs = _SHELL_SUBCOMMAND_WHITELIST[cmd_name]
            if len(tokens) < 2:
                return f"[Shell] {cmd_name} 需要子命令。允许: {', '.join(sorted(allowed_subs))}"
            sub = tokens[1]
            # Skip flags before subcommand (e.g. git --no-pager log)
            if sub.startswith("-"):
                for t in tokens[1:]:
                    if not t.startswith("-"):
                        sub = t
                        break
            if sub not in allowed_subs:
                return (
                    f"[Shell] 不允许的 {cmd_name} 子命令: {sub}。\n"
                    f"允许: {', '.join(sorted(allowed_subs))}"
                )

        elif cmd_name not in _SHELL_WHITELIST:
            return (
                f"[Shell] 不允许的命令: {cmd_name}。\n"
                f"允许的命令 ({len(_SHELL_WHITELIST)} 个): "
                f"{', '.join(sorted(_SHELL_WHITELIST))}\n"
                f"带限制的命令: {', '.join(sorted(_SHELL_SUBCOMMAND_WHITELIST.keys()))}"
            )

        # ── Block sed -i (inline edit) ────────────────────────
        if cmd_name == "sed":
            if any(flag in tokens for flag in _SED_BLOCKED_FLAGS):
                return "[Shell] sed 不允许使用 -i (文件内编辑)。只能进行只读文本处理。"

    return None  # Safe


async def shell_exec(command: str, timeout: int = 15) -> str:
    """Execute a shell command in the workspace directory (read-only, whitelist-gated).

    Supports pipes (|) for chaining commands. Each command in the pipeline
    is individually validated against a whitelist.

    Blocked: redirects (> / >> / <), command substitution ($() / ``),
    background execution (&), command chaining (; / && / ||), sed -i, git push.

    Args:
        command: Shell command to execute (e.g. "ls -la | wc -l").
        timeout: Max execution time in seconds (max 30, default 15).
    """
    if not command or not command.strip():
        return "[Shell] 请提供要执行的命令。"

    # Validate
    error = _validate_shell_command(command)
    if error:
        return error

    timeout = min(max(timeout, 1), _MAX_SHELL_TIMEOUT)

    # Ensure workspace exists
    _ensure_workspace_dirs()

    try:
        workspace_root = _get_workspace_root()
        result = subprocess.run(
            ["bash", "-c", command],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=workspace_root,
            env={
                "PATH": "/usr/bin:/usr/local/bin:/bin",
                "HOME": workspace_root,
                "LANG": "en_US.UTF-8",
                "LC_ALL": "C",
            },
        )

        output = ""
        if result.stdout:
            stdout = result.stdout[:_MAX_SHELL_OUTPUT]
            if len(result.stdout) > _MAX_SHELL_OUTPUT:
                stdout += "\n... (输出过长，已截断)"
            output += stdout
        if result.stderr:
            stderr = result.stderr[:_MAX_SHELL_OUTPUT // 2]
            if len(result.stderr) > _MAX_SHELL_OUTPUT // 2:
                stderr += "\n... (错误输出过长，已截断)"
            if output:
                output += f"\n--- stderr ---\n{stderr}"
            else:
                output = stderr

        if result.returncode != 0:
            output += f"\n(退出码: {result.returncode})"

        return output.strip() or "(命令执行完成，无输出)"

    except subprocess.TimeoutExpired:
        return (
            f"[Shell] 命令执行超时 ({timeout}秒)。\n"
            f"命令: {command}\n"
            f"如需更长时间，请使用更大 timeout 参数 (最大 {_MAX_SHELL_TIMEOUT}秒)"
        )
    except FileNotFoundError:
        return (
            f"[Shell] 命令未找到。可能是工具未安装。\n"
            f"命令: {command}\n"
            f"允许的命令: {', '.join(sorted(_SHELL_WHITELIST))}"
        )
    except Exception as e:
        return f"[Shell] 命令执行失败: {e}"


# ── System Load ──────────────────────────────────────────────────

def get_system_load() -> str:
    """Get real-time system load information (CPU, memory, disk).

    Reads from /proc/loadavg, free, and df to provide current resource
    usage. Use this before executing resource-intensive tasks to check
    if the server has sufficient capacity.
    """
    lines = ["系统实时负载:"]

    # ── CPU load ──────────────────────────────────────────────
    try:
        with open("/proc/loadavg", "r") as f:
            parts = f.read().strip().split()
            load1, load5, load15 = float(parts[0]), float(parts[1]), float(parts[2])
            nproc = os.cpu_count() or 1
            lines.append(
                f"  CPU: 1分钟负载 {load1:.2f} / 5分钟 {load5:.2f} / 15分钟 {load15:.2f}"
                f" ({nproc}核)"
            )
    except Exception:
        lines.append("  CPU: 无法获取")

    # ── Memory ────────────────────────────────────────────────
    try:
        result = subprocess.run(
            ["free", "-b"], capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.strip().split("\n"):
            if line.startswith("Mem:"):
                parts = line.split()
                total_gb = int(parts[1]) / (1024 ** 3)
                used_gb = int(parts[2]) / (1024 ** 3)
                pct = (used_gb / total_gb * 100) if total_gb > 0 else 0
                lines.append(
                    f"  内存: 已用 {used_gb:.1f} GB / 总计 {total_gb:.1f} GB ({pct:.0f}%)"
                )
                break
    except Exception:
        lines.append("  内存: 无法获取")

    # ── Disk ──────────────────────────────────────────────────
    for mount_point, label in [("/", "系统盘"), ("/data", "数据盘")]:
        try:
            result = subprocess.run(
                ["df", "-B1", mount_point],
                capture_output=True, text=True, timeout=5,
            )
            for line in result.stdout.strip().split("\n")[1:]:
                parts = line.split()
                if len(parts) >= 4:
                    total_gb = int(parts[1]) / (1024 ** 3)
                    used_gb = int(parts[2]) / (1024 ** 3)
                    pct = (used_gb / total_gb * 100) if total_gb > 0 else 0
                    lines.append(
                        f"  {label}: 已用 {used_gb:.0f} GB / 总计 {total_gb:.0f} GB ({pct:.0f}%)"
                    )
                    break
        except Exception:
            pass

    # ── Assessment ─────────────────────────────────────────────
    try:
        with open("/proc/loadavg", "r") as f:
            load1 = float(f.read().strip().split()[0])
        nproc = os.cpu_count() or 1
        if load1 < nproc * 0.5:
            lines.append("\n  评估: 负载较轻，可正常执行任务")
        elif load1 < nproc * 0.8:
            lines.append("\n  评估: 负载适中，建议避免高负载任务")
        else:
            lines.append("\n  评估: 负载较高，建议拒绝计算密集型任务")
    except Exception:
        pass

    return "\n".join(lines)

"""
File Tools — Read and analyze uploaded files (text, PDF, images).

The `read_file` tool handles three file types:
1. Text files — read and return contents (UTF-8, capped at 50KB)
2. PDF files — extract text via PyPDF2
3. Image files — return metadata, plus AI analysis if multimodal LLM configured

All file access is validated by _validate_path (workspace boundary enforcement).
"""

import os

from ..lib.multimodal_client import multimodal_client

# Reuse security helpers and workspace paths from builtin_tools
from .builtin_tools import (
    _validate_path,
    _ensure_workspace_dirs,
    WORKSPACE_ROOT,
    WORKSPACE_UPLOADS,
)

# ── File Type Detection ──────────────────────────────────────────────

_TEXT_EXTENSIONS = {
    ".txt", ".md", ".py", ".json", ".csv", ".log",
    ".yml", ".yaml", ".toml", ".ini", ".cfg", ".conf",
    ".xml", ".html", ".htm", ".css", ".js", ".ts", ".jsx", ".tsx",
    ".sh", ".bat", ".ps1",
    ".sql", ".rb", ".go", ".rs", ".java", ".c", ".cpp", ".h",
    ".hpp", ".php", ".swift", ".kt", ".scala", ".lua", ".r",
    ".pl", ".tex", ".rst", ".org", ".dockerfile",
}

_IMAGE_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp",
}

# ── Helpers ───────────────────────────────────────────────────────────

def _get_image_metadata(file_path: str) -> dict:
    """Extract basic image metadata using PIL. Returns dict or error info."""
    try:
        from PIL import Image as PILImage
        img = PILImage.open(file_path)
        width, height = img.size
        fmt = img.format or "unknown"
        mode = img.mode
        file_size = os.path.getsize(file_path)
        return {
            "width": width,
            "height": height,
            "format": fmt,
            "mode": mode,
            "file_size": file_size,
            "error": None,
        }
    except ImportError:
        return {"error": "PIL(Pillow) 未安装，无法获取图片信息。请安装: pip install Pillow"}
    except Exception as e:
        return {"error": f"无法读取图片: {e}"}


def _format_size(size_bytes: int) -> str:
    """Format bytes as human-readable string."""
    for unit in ["B", "KB", "MB", "GB"]:
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"


# ── Main Tool ─────────────────────────────────────────────────────────

async def read_file(file_path: str) -> str:
    """Read and analyze a file from the workspace.

    Supports:
    - Text files (code, configs, logs, etc.): returns content
    - PDF files: extracts text from all pages
    - Image files: returns metadata + AI analysis (if multimodal configured)

    Args:
        file_path: Path to the file (relative to workspace or absolute within workspace).

    Returns:
        File contents, analysis, or error message.
    """
    # Security: validate path is within workspace
    safe_path, error = _validate_path(file_path)
    if error:
        return f"[ReadFile] {error}"

    # Detect file type by extension
    ext = os.path.splitext(safe_path)[1].lower()

    # ── Text Files ──────────────────────────────────────────────────
    if ext in _TEXT_EXTENSIONS:
        return _read_text_file(safe_path)

    # ── PDF Files ───────────────────────────────────────────────────
    if ext == ".pdf":
        return _read_pdf_file(safe_path)

    # ── Image Files ─────────────────────────────────────────────────
    if ext in _IMAGE_EXTENSIONS:
        return await _read_image_file(safe_path)

    # ── Unsupported ─────────────────────────────────────────────────
    supported = sorted(_TEXT_EXTENSIONS | _IMAGE_EXTENSIONS | {".pdf"})
    return (
        f"[ReadFile] 不支持的文件类型: {ext}\n\n"
        f"支持的文件类型:\n"
        f"  文本: {', '.join(ext for ext in sorted(_TEXT_EXTENSIONS)[:15])} ...\n"
        f"  图片: {', '.join(sorted(_IMAGE_EXTENSIONS))}\n"
        f"  PDF: .pdf"
    )


# ── Sub-readers ───────────────────────────────────────────────────────

def _read_text_file(path: str, max_chars: int = 50000) -> str:
    """Read a text file and return its contents."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read(max_chars + 1)

        truncated = len(content) > max_chars
        if truncated:
            content = content[:max_chars]

        # File info header
        file_size = os.path.getsize(path)
        lines = content.count("\n") + 1
        ext = os.path.splitext(path)[1].lower()
        lang_map = {
            ".py": "Python", ".js": "JavaScript", ".ts": "TypeScript",
            ".java": "Java", ".go": "Go", ".rs": "Rust", ".cpp": "C++",
            ".c": "C", ".h": "C/C++ Header", ".sh": "Shell", ".sql": "SQL",
            ".html": "HTML", ".css": "CSS", ".json": "JSON", ".md": "Markdown",
            ".yml": "YAML", ".yaml": "YAML", ".toml": "TOML", ".xml": "XML",
        }
        lang = lang_map.get(ext, "Text")

        header = (
            f"[文件信息] {os.path.basename(path)}\n"
            f"  类型: {lang}\n"
            f"  大小: {_format_size(file_size)} ({file_size:,} bytes)\n"
            f"  行数: {lines}\n"
        )
        if truncated:
            header += f"  (内容已截断，仅显示前 {max_chars:,} 字符)\n"
        header += f"\n{'─' * 40}\n\n"

        return header + content

    except Exception as e:
        return f"[ReadFile] 读取文本文件时出错: {e}"


def _read_pdf_file(path: str) -> str:
    """Extract text from a PDF file."""
    try:
        from PyPDF2 import PdfReader
    except ImportError:
        return (
            "[ReadFile] PyPDF2 未安装。请先在服务器上安装:\n"
            "pip install PyPDF2"
        )

    try:
        reader = PdfReader(path)
        num_pages = len(reader.pages)
        text_parts = []
        total_chars = 0
        max_chars = 8000

        for i, page in enumerate(reader.pages):
            page_text = page.extract_text()
            if page_text:
                text_parts.append(f"--- 第{i + 1}页 ---\n{page_text}")
                total_chars += len(page_text)
                if total_chars >= max_chars:
                    break

        full_text = "\n".join(text_parts)

        if not full_text.strip():
            return f"[文件信息] PDF 文件 ({os.path.basename(path)})\n  页数: {num_pages}\n  注意: 无法提取文本内容（可能是扫描版 PDF）。"

        if total_chars >= max_chars:
            full_text += (
                f"\n\n... (文本过长，已截断至前{max_chars}字符，"
                f"共{num_pages}页，已显示{i + 1}页)"
            )

        header = (
            f"[文件信息] {os.path.basename(path)}\n"
            f"  类型: PDF\n"
            f"  页数: {num_pages}\n"
            f"  文件大小: {_format_size(os.path.getsize(path))}\n"
            f"\n{'─' * 40}\n\n"
        )

        return header + full_text

    except Exception as e:
        return f"[ReadFile] 处理 PDF 时出错: {e}"


async def _read_image_file(path: str) -> str:
    """Analyze an image file — metadata + optional AI analysis."""
    metadata = _get_image_metadata(path)

    # Build metadata section
    lines = [
        f"[文件信息] {os.path.basename(path)}",
    ]

    if metadata.get("error"):
        lines.append(f"  错误: {metadata['error']}")
    else:
        lines.extend([
            f"  类型: {metadata.get('format', 'unknown')} 图片",
            f"  尺寸: {metadata.get('width', '?')} × {metadata.get('height', '?')} px",
            f"  色彩模式: {metadata.get('mode', '?')}",
            f"  文件大小: {_format_size(metadata.get('file_size', 0))}",
        ])

    lines.append("")

    # Try AI analysis
    if multimodal_client.is_available():
        lines.append(f"{'─' * 40}")
        lines.append("[AI 图片分析]")
        lines.append("")
        analysis = await multimodal_client.analyze_image(path)
        lines.append(analysis)
    else:
        lines.append(
            "提示: 配置多模态 LLM 后可以自动分析图片内容。\n"
            "请编辑 QQBot/config/multimodal.json 并重启机器人。"
        )

    return "\n".join(lines)

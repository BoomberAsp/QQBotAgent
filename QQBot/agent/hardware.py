"""
Hardware Profile — Detect physical server specs on first boot, cache to disk.

Runs detection commands at bootstrap time (using subprocess directly, not via
the agent's shell_exec tool, since this is infrastructure-level initialization).
Results are cached to {USER_DATA_ROOT}/.hardware.json and injected into the
agent's system prompt.

The cached file is reused on subsequent boots. To force re-detection, delete
the cache file or call HardwareDetector.detect(force=True).
"""

import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class HardwareProfile:
    """Detected physical server hardware specifications."""

    cpu_cores: int = 0
    cpu_model: str = ""
    memory_gb: float = 0.0
    disk_system_gb: float = 0.0
    disk_data_gb: float = 0.0
    has_gpu: bool = False
    gpu_info: Optional[str] = None
    os_info: str = ""
    detected_at: str = ""

    # ── Serialization ────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "cpu_cores": self.cpu_cores,
            "cpu_model": self.cpu_model,
            "memory_gb": self.memory_gb,
            "disk_system_gb": self.disk_system_gb,
            "disk_data_gb": self.disk_data_gb,
            "has_gpu": self.has_gpu,
            "gpu_info": self.gpu_info,
            "os_info": self.os_info,
            "detected_at": self.detected_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "HardwareProfile":
        return cls(
            cpu_cores=data.get("cpu_cores", 0),
            cpu_model=data.get("cpu_model", ""),
            memory_gb=data.get("memory_gb", 0.0),
            disk_system_gb=data.get("disk_system_gb", 0.0),
            disk_data_gb=data.get("disk_data_gb", 0.0),
            has_gpu=data.get("has_gpu", False),
            gpu_info=data.get("gpu_info"),
            os_info=data.get("os_info", ""),
            detected_at=data.get("detected_at", ""),
        )

    # ── Prompt Injection ─────────────────────────────────────────

    def get_prompt_context(self) -> str:
        """Generate the hardware info block to inject into the system prompt."""
        lines = [
            "## 服务器硬件 (自动检测)",
            "",
            "| 资源 | 规格 |",
            "|------|------|",
            f"| **CPU** | {self.cpu_cores} 核 ({self.cpu_model}) |",
            f"| **内存** | {self.memory_gb:.1f} GB |",
            f"| **系统盘** | {self.disk_system_gb:.0f} GB |",
            f"| **数据盘** | {self.disk_data_gb:.0f} GB |",
            f"| **GPU** | {'是 — ' + self.gpu_info if self.has_gpu else '无'} |",
            f"| **操作系统** | {self.os_info} |",
            f"| **检测时间** | {self.detected_at} |",
        ]
        return "\n".join(lines)

    def get_task_refusal_context(self) -> str:
        """Generate the task refusal rules based on detected hardware."""
        mem = self.memory_gb
        cores = self.cpu_cores

        return (
            "### 必须拒绝的高负载任务\n\n"
            "以下任务**必须礼貌拒绝**，并简要说明原因和替代建议：\n\n"
            "| 任务类型 | 拒绝原因 | 替代建议 |\n"
            "|----------|----------|----------|\n"
            "| 训练机器学习/深度学习模型 | 无 GPU，内存不足 | 使用 Google Colab、Kaggle Notebook |\n"
            f"| 处理大型数据集 (>{max(10, int(mem * 0.02))}MB) | 内存限制，处理时间过长 | 本地处理后上传小样本 |\n"
            "| 视频编码/转码/处理 | CPU 性能不足，耗时极长 | 使用本地机器或云转码服务 |\n"
            f"| 批量处理大量图片 (>{max(5, cores * 2)}张) | 内存和磁盘 I/O 限制 | 每次不超过 {max(5, cores * 2)} 张 |\n"
            "| 运行本地 LLM 推理 | 无 GPU，内存不足 | N/A |\n"
            f"| 编译大型项目 | CPU ({cores}核) 和内存 ({mem:.0f}GB) 限制 | 使用 GitHub Actions / CI |\n"
            "| 运行 Docker 容器 | 内存不足以支撑额外容器 | 使用已有服务（SearXNG 已运行） |\n"
            "| 大规模网页爬虫 | 网络出口带宽限制 | 使用 Apify、ScrapingBee 等 SaaS |\n"
            "| 挖矿 / 长期后台任务 | 服务器为个人使用，非计算集群 | N/A — 绝对禁止 |\n"
        )


class HardwareDetector:
    """Detect hardware specs and cache to disk."""

    def __init__(self, cache_dir: str):
        """
        Args:
            cache_dir: Directory to store .hardware.json (typically USER_DATA_ROOT).
        """
        self.cache_dir = cache_dir
        self.cache_path = os.path.join(cache_dir, ".hardware.json")

    # ── Detection ─────────────────────────────────────────────────

    def detect(self, force: bool = False) -> HardwareProfile:
        """Run detection commands and return a HardwareProfile.

        Args:
            force: If True, re-detect even if cache exists.
        """
        if not force and os.path.exists(self.cache_path):
            return self._load_cache()

        profile = HardwareProfile()
        profile.detected_at = time.strftime("%Y-%m-%d %H:%M:%S")

        # ── CPU cores ──────────────────────────────────────────
        try:
            result = subprocess.run(
                ["nproc"], capture_output=True, text=True, timeout=5
            )
            profile.cpu_cores = int(result.stdout.strip())
        except Exception:
            profile.cpu_cores = 1

        # ── CPU model ──────────────────────────────────────────
        try:
            result = subprocess.run(
                ["bash", "-c", "lscpu 2>/dev/null | grep 'Model name' | head -1"],
                capture_output=True, text=True, timeout=5,
            )
            if result.stdout.strip():
                profile.cpu_model = result.stdout.strip().split(":", 1)[-1].strip()
        except Exception:
            pass

        if not profile.cpu_model:
            try:
                result = subprocess.run(
                    ["bash", "-c", "cat /proc/cpuinfo 2>/dev/null | grep 'model name' | head -1"],
                    capture_output=True, text=True, timeout=5,
                )
                if result.stdout.strip():
                    profile.cpu_model = result.stdout.strip().split(":", 1)[-1].strip()
            except Exception:
                profile.cpu_model = "Unknown"

        # ── Memory ─────────────────────────────────────────────
        try:
            result = subprocess.run(
                ["free", "-b"], capture_output=True, text=True, timeout=5
            )
            for line in result.stdout.strip().split("\n"):
                if line.startswith("Mem:"):
                    parts = line.split()
                    total_bytes = int(parts[1])
                    profile.memory_gb = round(total_bytes / (1024**3), 1)
                    break
        except Exception:
            profile.memory_gb = 0.0

        # ── Disk (system + data) ───────────────────────────────
        try:
            result = subprocess.run(
                ["df", "-B1", "--output=target,size"],
                capture_output=True, text=True, timeout=5,
            )
            for line in result.stdout.strip().split("\n")[1:]:
                parts = line.split()
                if len(parts) < 2:
                    continue
                mount = parts[0]
                size_bytes = int(parts[1])
                size_gb = round(size_bytes / (1024**3), 1)
                if mount == "/":
                    profile.disk_system_gb = size_gb
                elif mount in ("/data", "/home", "/mnt/data"):
                    profile.disk_data_gb = size_gb
        except Exception:
            pass

        if profile.disk_data_gb == 0.0:
            profile.disk_data_gb = profile.disk_system_gb

        # ── GPU ────────────────────────────────────────────────
        try:
            result = subprocess.run(
                ["bash", "-c", "lspci 2>/dev/null | grep -iE 'vga|3d|display' | head -3"],
                capture_output=True, text=True, timeout=5,
            )
            gpu_lines = [l.strip() for l in result.stdout.strip().split("\n") if l.strip()]
            if gpu_lines:
                profile.has_gpu = True
                profile.gpu_info = "; ".join(gpu_lines)
        except Exception:
            pass

        if not profile.has_gpu:
            try:
                nvidia_check = subprocess.run(
                    ["bash", "-c", "ls /dev/nvidia* 2>/dev/null"],
                    capture_output=True, text=True, timeout=5,
                )
                if nvidia_check.stdout.strip():
                    profile.has_gpu = True
                    profile.gpu_info = "NVIDIA GPU (detected via /dev/nvidia*)"
            except Exception:
                pass

        # ── OS info ────────────────────────────────────────────
        try:
            result = subprocess.run(
                ["uname", "-a"], capture_output=True, text=True, timeout=5
            )
            profile.os_info = result.stdout.strip()
        except Exception:
            profile.os_info = "Unknown"

        # ── Save cache ─────────────────────────────────────────
        self._save_cache(profile)

        return profile

    # ── Cache ─────────────────────────────────────────────────────

    def load_or_detect(self) -> HardwareProfile:
        """Load cached profile or detect if cache doesn't exist."""
        return self.detect(force=False)

    def _load_cache(self) -> Optional[HardwareProfile]:
        try:
            with open(self.cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return HardwareProfile.from_dict(data)
        except Exception:
            return None

    def _save_cache(self, profile: HardwareProfile):
        os.makedirs(self.cache_dir, exist_ok=True)
        try:
            with open(self.cache_path, "w", encoding="utf-8") as f:
                json.dump(profile.to_dict(), f, ensure_ascii=False, indent=2)
        except Exception:
            pass

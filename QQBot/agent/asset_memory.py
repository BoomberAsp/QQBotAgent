"""
Asset Memory — 多模态资产的文本卡记忆层（Bond-System-Design.md §8，P4/P5）。

核心原则（§8.1）：**文本化是检索层不是存储层；二进制只是可淘汰缓存，
文本卡才是本体**。每次生成（图/语音）或用户上传的多模态内容都落一张
markdown+frontmatter 资产卡（与 MemorySystem 同款格式），二进制文件另存：

    data/assets/{user_id}/cards/{asset_id}.md          # 资产卡（本体，永不淘汰）
    data/assets/{user_id}/{char_id}/{asset_id}.png|mp3 # 二进制（P5 LRU 可淘汰）
    data/assets/_dedupe_index.json                     # prompt_hash 去重索引
    data/assets/audit.log                              # 生成审计（单行追加）

P4 范围：建卡 / 读卡 / 列表 / prompt_hash 去重 / 审计。
P5 追加：关键词检索 + 歧义 LLM 重排 / 召回计数 / 注入块 / 事件蒸馏 / LRU 淘汰。

无 nonebot / 网络依赖，可独立单测。JSON 一律原子写 + 损坏回退空值。
"""

import hashlib
import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional

# 资产类型
ASSET_IMAGE = "image"
ASSET_VOICE = "voice"
ASSET_UPLOAD = "user_upload"

_DEFAULT_BASE_DIR = os.path.join(
    os.path.dirname(__file__), "..", "data", "assets"
)


def _safe_id(id_str: str) -> str:
    """Filesystem-safe id (same convention as memory.py / bond.py)."""
    return re.sub(r"[^0-9A-Za-z_.-]", "_", id_str or "anonymous")


def make_asset_id() -> str:
    """``ast_`` + 8 hex（uuid4 前缀，碰撞概率对单用户量级足够低）。"""
    return f"ast_{uuid.uuid4().hex[:8]}"


def prompt_hash_of(prompt: str) -> str:
    """Stable hash for dedupe (blake2b-16; 跨进程稳定，不用内建 hash())。"""
    return hashlib.blake2b(
        (prompt or "").strip().encode("utf-8"), digest_size=16
    ).hexdigest()


@dataclass
class AssetCard:
    """一次多模态生成/上传的文本化记忆卡。"""

    id: str
    type: str                       # image | voice | user_upload
    char_id: str
    user_id: str
    path: Optional[str] = None      # 二进制路径；None = 已淘汰（P5），文本记忆仍在
    model: str = ""                 # 生成模型（qwen-image-3.0 / tts 模型名）
    prompt: str = ""                # 图像 prompt / 语音生成指令原文
    transcript: str = ""            # 语音文本（TTS 原文 / 上传语音转录）
    tags: List[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    recall_count: int = 0           # P5 召回重发计数（LRU 权重）
    prompt_hash: str = ""           # 去重键（图像生成侧）
    proactive: bool = False         # 主动发送的媒体（P4 §8 主动 pass 标记）

    # ── markdown+frontmatter 序列化（MemorySystem 兼容）──

    def to_markdown(self) -> str:
        tags = ",".join(self.tags)
        path = self.path or ""
        return (
            f"---\n"
            f"id: {self.id}\n"
            f"type: {self.type}\n"
            f"char_id: {self.char_id}\n"
            f"user_id: {self.user_id}\n"
            f"path: {path}\n"
            f"model: {self.model}\n"
            f"created_at: {self.created_at:.0f}\n"
            f"recall_count: {int(self.recall_count)}\n"
            f"prompt_hash: {self.prompt_hash}\n"
            f"proactive: {'true' if self.proactive else 'false'}\n"
            f"tags: {tags}\n"
            f"---\n\n"
            f"{self.prompt or self.transcript or ''}\n"
        )

    @classmethod
    def from_markdown(cls, content: str) -> Optional["AssetCard"]:
        if not content.startswith("---"):
            return None
        parts = content.split("---", 2)
        if len(parts) < 3:
            return None
        meta: Dict[str, str] = {}
        for line in parts[1].splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip()
        body = parts[2].strip()
        try:
            created = float(meta.get("created_at") or time.time())
        except ValueError:
            created = time.time()
        try:
            recall = int(meta.get("recall_count") or 0)
        except ValueError:
            recall = 0
        card_type = meta.get("type") or ASSET_IMAGE
        transcript = body if card_type == ASSET_VOICE else ""
        prompt = body if card_type != ASSET_VOICE else ""
        tags = [t for t in (meta.get("tags") or "").split(",") if t]
        return cls(
            id=meta.get("id") or "",
            type=card_type,
            char_id=meta.get("char_id") or "",
            user_id=meta.get("user_id") or "",
            path=meta.get("path") or None,
            model=meta.get("model") or "",
            prompt=prompt,
            transcript=transcript,
            tags=tags,
            created_at=created,
            recall_count=recall,
            prompt_hash=meta.get("prompt_hash") or "",
            proactive=(meta.get("proactive") or "").lower() == "true",
        )


class AssetMemory:
    """Per-user multimodal asset cards under ``data/assets/``."""

    def __init__(self, base_dir: str = None):
        self.base_dir = base_dir or _DEFAULT_BASE_DIR
        os.makedirs(self.base_dir, exist_ok=True)
        self._dedupe_path = os.path.join(self.base_dir, "_dedupe_index.json")
        self._audit_path = os.path.join(self.base_dir, "audit.log")
        # asset_id → (mtime, card) 的进程内缓存（mtime 校验，
        # panel↔bot 缓存一致性教训：外部写入优先于缓存）
        self._cache: Dict[str, tuple] = {}

    # ── 路径 ──────────────────────────────────────────────────────

    def _user_dir(self, user_id: str) -> str:
        return os.path.join(self.base_dir, _safe_id(user_id))

    def card_path(self, user_id: str, asset_id: str) -> str:
        return os.path.join(self._user_dir(user_id), "cards", f"{asset_id}.md")

    def binary_path(self, user_id: str, char_id: str, asset_id: str, ext: str) -> str:
        """二进制存放路径（目录顺带创建）。ext 如 "png" / "mp3"。"""
        d = os.path.join(self._user_dir(user_id), _safe_id(char_id))
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, f"{asset_id}.{ext.lstrip('.')}")

    # ── 卡的读写 ──────────────────────────────────────────────────

    def write_card(self, card: AssetCard) -> str:
        """原子写卡文件。返回卡路径。"""
        path = self.card_path(card.user_id, card.id)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(card.to_markdown())
        os.replace(tmp, path)
        self._cache[card.id] = (os.path.getmtime(path), card)
        return path

    def read_card(self, user_id: str, asset_id: str) -> Optional[AssetCard]:
        """读卡（mtime 校验缓存：外部写入优先，文件消失则失效）。"""
        path = self.card_path(user_id, asset_id)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            self._cache.pop(asset_id, None)
            return None
        cached = self._cache.get(asset_id)
        if cached is not None and cached[0] == mtime:
            return cached[1]
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
        except (FileNotFoundError, IOError):
            return None
        card = AssetCard.from_markdown(content)
        if card is not None:
            self._cache[card.id] = (mtime, card)
        return card

    def list_cards(self, user_id: str, char_id: Optional[str] = None) -> List[AssetCard]:
        """某用户全部资产卡（可按角色过滤），新的在前。"""
        cards_dir = os.path.join(self._user_dir(user_id), "cards")
        if not os.path.isdir(cards_dir):
            return []
        cards: List[AssetCard] = []
        for fname in os.listdir(cards_dir):
            if not fname.endswith(".md"):
                continue
            asset_id = fname[:-3]
            card = self.read_card(user_id, asset_id)
            if card is None:
                continue
            if char_id and card.char_id != char_id:
                continue
            cards.append(card)
        cards.sort(key=lambda c: c.created_at, reverse=True)
        return cards

    # ── prompt_hash 去重（P4：7 天窗口内同 prompt 复用旧文件）──────

    def _load_dedupe(self) -> Dict[str, dict]:
        try:
            with open(self._dedupe_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError, IOError):
            return {}

    def _save_dedupe(self, index: Dict[str, dict]):
        tmp = self._dedupe_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(index, f, ensure_ascii=False, indent=0)
        os.replace(tmp, self._dedupe_path)

    def find_by_prompt_hash(
        self, phash: str, ttl_hours: float = 168.0
    ) -> Optional[AssetCard]:
        """窗口内同 prompt 的旧卡（二进制仍在盘上才算命中）。

        命中 → 调用方直接复用旧文件，不再花一次生成计费。
        """
        if not phash:
            return None
        entry = self._load_dedupe().get(phash)
        if not isinstance(entry, dict):
            return None
        try:
            ts = float(entry.get("ts", 0))
        except (TypeError, ValueError):
            return None
        if ttl_hours > 0 and (time.time() - ts) > ttl_hours * 3600:
            return None
        path = str(entry.get("path") or "")
        if not path or not os.path.isfile(path):
            return None
        card = self.read_card(str(entry.get("user_id") or ""), str(entry.get("asset_id") or ""))
        return card

    def record_prompt_hash(self, phash: str, card: AssetCard):
        """登记去重索引（生成成功且卡已写盘后调用）。"""
        if not phash or not card.path:
            return
        index = self._load_dedupe()
        index[phash] = {
            "asset_id": card.id,
            "user_id": card.user_id,
            "path": card.path,
            "ts": time.time(),
        }
        # 简单容量护栏：索引超过 4096 条时丢掉最旧的一半
        if len(index) > 4096:
            ordered = sorted(index.items(), key=lambda kv: float(kv[1].get("ts", 0)))
            index = dict(ordered[len(ordered) // 2:])
            index[phash] = {
                "asset_id": card.id,
                "user_id": card.user_id,
                "path": card.path,
                "ts": time.time(),
            }
        self._save_dedupe(index)

    # ── 审计（单行追加，管理员排查/计费对账用）─────────────────────

    def append_audit(
        self,
        user_id: str,
        char_id: str,
        media_type: str,
        model: str,
        phash: str,
        result: str,
    ):
        """``ts|user|char|type|model|prompt_hash|result`` 单行；best-effort。"""
        line = (
            f"{time.time():.0f}|{_safe_id(user_id)}|{_safe_id(char_id)}"
            f"|{media_type}|{model}|{phash}|{result}\n"
        )
        try:
            with open(self._audit_path, "a", encoding="utf-8") as f:
                f.write(line)
        except IOError:
            pass

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
P5 范围：关键词检索 + 歧义 LLM 重排 / 召回计数 / L4 注入块 / 事件蒸馏 /
LRU 淘汰（score = recall_count × exp(-age_days/30)，只删二进制、卡保留）。

无 nonebot / 网络依赖，可独立单测。JSON 一律原子写 + 损坏回退空值。
"""

import hashlib
import json
import math
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# 资产类型
ASSET_IMAGE = "image"
ASSET_VOICE = "voice"
ASSET_UPLOAD = "user_upload"

# 注入块/observation 里的中文类型标签
_TYPE_LABEL = {ASSET_IMAGE: "图片", ASSET_VOICE: "语音", ASSET_UPLOAD: "上传"}

# LRU 半衰期（天）：score = recall_count × exp(-age_days / _LRU_HALF_LIFE_DAYS)
_LRU_HALF_LIFE_DAYS = 30.0

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

    # ── P5: 关键词检索 + 歧义 LLM 重排（§8.2，不引入向量库）──────────

    def _search_scored(
        self, user_id: str, query: str, char_id: Optional[str] = None
    ) -> List[Tuple[AssetCard, float]]:
        """打分后的命中列表（分数降序、同分新的在前）。"""
        terms = _search_terms(query)
        if not terms:
            return []
        scored: List[Tuple[AssetCard, float]] = []
        for card in self.list_cards(user_id, char_id):
            s = _score_asset(card, terms)
            if s > 0:
                scored.append((card, s))
        scored.sort(key=lambda cs: (-cs[1], -cs[0].created_at))
        return scored

    def search(
        self, user_id: str, query: str, top_k: int = 5,
        char_id: Optional[str] = None,
    ) -> List[AssetCard]:
        """关键词检索资产卡（prompt/transcript/tags 子串命中打分）。"""
        return [c for c, _ in self._search_scored(user_id, query, char_id)[:top_k]]

    async def search_with_rerank(
        self, user_id: str, query: str, rerank_client=None, top_k: int = 5,
        char_id: Optional[str] = None,
    ) -> List[AssetCard]:
        """关键词 top-N；仅当截断线两侧分差小（歧义）时花一次 flash 重排。

        重排失败/输出不可解析 → 静默回退关键词序（增益功能不阻断）。
        """
        scored = self._search_scored(user_id, query, char_id)
        if not scored:
            return []
        top_k = max(1, int(top_k))
        if len(scored) <= top_k or rerank_client is None:
            return [c for c, _ in scored[:top_k]]
        if scored[top_k - 1][1] - scored[top_k][1] >= 1.0:
            return [c for c, _ in scored[:top_k]]  # 分差足够大，无需重排
        window = scored[: max(top_k * 2, 6)]
        try:
            lines = [
                f"{i + 1}. [{_TYPE_LABEL.get(c.type, '资产')}] "
                f"{' '.join((c.transcript or c.prompt or '').split())[:40]}"
                for i, (c, _) in enumerate(window)
            ]
            prompt = (
                f"用户想找回一段影音记忆，TA 的描述是：「{(query or '')[:80]}」。\n"
                f"候选列表：\n" + "\n".join(lines) + "\n\n"
                f"请选出最匹配的至多 {top_k} 项，按相关度从高到低只输出序号，"
                f"用逗号分隔（例如「3,1」）。不要输出任何其他文字。"
            )
            raw = await rerank_client.chat_completion(
                message=prompt, timeout_set=10.0, purpose="asset_rerank")
            picked: List[AssetCard] = []
            seen = set()
            for n in re.findall(r"\d+", raw or ""):
                idx = int(n) - 1
                if 0 <= idx < len(window) and idx not in seen:
                    seen.add(idx)
                    picked.append(window[idx][0])
                if len(picked) >= top_k:
                    break
            if picked:
                return picked
        except Exception:  # noqa: BLE001
            pass
        return [c for c, _ in scored[:top_k]]

    # ── P5: 召回计数 / L4 注入块 / 事件蒸馏 ─────────────────────────

    def bump_recall(self, user_id: str, asset_id: str) -> bool:
        """召回重发成功后 +1（LRU 权重：常被想起的记忆最后淘汰）。"""
        card = self.read_card(user_id, asset_id)
        if card is None:
            return False
        card.recall_count = int(card.recall_count or 0) + 1
        self.write_card(card)
        return True

    @staticmethod
    def card_label(card: AssetCard) -> str:
        if card.type == ASSET_UPLOAD:
            return ("用户发来的语音" if "voice" in (card.tags or [])
                    else "用户发来的图片")
        return _TYPE_LABEL.get(card.type, "资产")

    def build_asset_injection(
        self, user_id: str, char_id: str, top_k: int = 5,
        display_name: str = "",
    ) -> str:
        """L4 易变尾部的「影音记忆」注入块（无资产 → 空串，零开销）。

        path=None 的已淘汰卡**照样注入**——二进制是缓存，文本记忆不褪色。
        """
        cards = self.list_cards(user_id, char_id)[: max(1, int(top_k))]
        if not cards:
            return ""
        name = display_name or char_id
        lines = []
        for c in cards:
            date = time.strftime("%Y-%m-%d", time.localtime(c.created_at))
            desc = " ".join((c.prompt or c.transcript or "").split())[:40]
            state = ("" if (c.path and os.path.isfile(c.path))
                     else "（原件已淡去，只剩文字记忆）")
            lines.append(f"- [{date} · {self.card_label(c)}] {desc or '（无描述）'}{state}")
        return (
            f"## 你与{name}的影音记忆\n"
            "以下是你们之间最近的图片与语音（新的在前）。可以自然提及这些共同记忆；"
            "当用户想找回某一张图/某一段语音（如「再发一次那张图」）时，"
            "调用 recall_asset 工具。\n" + "\n".join(lines)
        )

    def distill_episode(
        self, user_id: str, char_id: str,
        asset_ids: Optional[List[str]], highlights: Optional[List[str]],
        title: str,
    ) -> str:
        """里程碑时刻 → ≤300 字事件文本（TieredMemory category=event 用）。"""
        parts = [(title or "").strip()]
        hs = [str(h).strip() for h in (highlights or []) if h and str(h).strip()]
        if hs:
            parts.append("；".join(hs))
        descs = []
        for aid in (asset_ids or [])[:3]:
            card = self.read_card(user_id, str(aid))
            if card is None:
                continue
            d = " ".join((card.prompt or card.transcript or "").split())[:30]
            if d:
                descs.append(d)
        if descs:
            parts.append("相关影音：" + "、".join(descs))
        return "。".join(p for p in parts if p)[:300]

    # ── P5: LRU 淘汰（二进制 = 可淘汰缓存；卡 = 本体，永不删）────────

    def assets_size(self, user_id: str) -> int:
        """该用户资产目录总字节数（二进制 + 卡文件；卡极小可忽略）。"""
        total = 0
        for dirpath, _dirs, files in os.walk(self._user_dir(user_id)):
            for fn in files:
                try:
                    total += os.path.getsize(os.path.join(dirpath, fn))
                except OSError:
                    pass
        return total

    @staticmethod
    def eviction_score(card: AssetCard, now: Optional[float] = None) -> float:
        """recall_count × exp(-age_days/30)——低分先淘汰（平局老的先走）。"""
        now = now if now is not None else time.time()
        age_days = max(0.0, (now - float(card.created_at or now)) / 86400.0)
        return max(0, int(card.recall_count or 0)) * math.exp(
            -age_days / _LRU_HALF_LIFE_DAYS)

    def eviction_candidates(self, user_id: str) -> List[AssetCard]:
        """可淘汰二进制（升序：最低分在前）。

        只收资产目录内的文件——data/characters/ 锚点/参考立绘与工作区
        上传原件（路径在资产目录外）**硬保护**，永不进候选。
        """
        root = os.path.abspath(self._user_dir(user_id))
        out = []
        for card in self.list_cards(user_id):
            if not card.path:
                continue
            ap = os.path.abspath(card.path)
            if not ap.startswith(root + os.sep) or not os.path.isfile(ap):
                continue
            out.append(card)
        out.sort(key=lambda c: (self.eviction_score(c), c.created_at))
        return out

    def evict(self, user_id: str, asset_id: str) -> bool:
        """只删二进制、卡保留（path=None）；资产目录外的路径拒绝删除。"""
        card = self.read_card(user_id, asset_id)
        if card is None or not card.path:
            return False
        root = os.path.abspath(self._user_dir(user_id))
        ap = os.path.abspath(card.path)
        if not ap.startswith(root + os.sep):
            return False  # 硬保护：characters/ 锚点、工作区上传原件等
        try:
            os.remove(ap)
        except OSError:
            return False
        card.path = None
        self.write_card(card)
        return True

    def enforce_quota(self, user_id: str, quota_mb) -> List[str]:
        """超配额 → 从低分到高分淘汰二进制直到达标。返回被淘汰的 asset_id。"""
        try:
            limit = int(quota_mb) * 1024 * 1024
        except (TypeError, ValueError):
            return []
        if limit <= 0:
            return []
        size = self.assets_size(user_id)
        evicted: List[str] = []
        if size <= limit:
            return evicted
        for card in self.eviction_candidates(user_id):
            if size <= limit:
                break
            try:
                fsize = os.path.getsize(card.path)
            except OSError:
                fsize = 0
            if self.evict(user_id, card.id):
                size -= fsize
                evicted.append(card.id)
        return evicted


def _search_terms(query: str) -> List[str]:
    """检索词：完整 query + 标点/空白切分的 ≥2 字 token（CJK 友好）。"""
    q = (query or "").strip().lower()
    if not q:
        return []
    terms = {q}
    for tok in re.split(r"[\s,，。.!！?？、;；:：\"'「」『』()（）\[\]【】]+", q):
        if len(tok) >= 2:
            terms.add(tok)
    return sorted(terms)


def _score_asset(card: AssetCard, terms: List[str]) -> float:
    prompt = (card.prompt or "").lower()
    trans = (card.transcript or "").lower()
    tags = " ".join(card.tags or []).lower()
    score = 0.0
    for t in terms:
        if t in tags:
            score += 3.0
        if t in trans:
            score += 2.0
        if t in prompt:
            score += 2.0
    return score

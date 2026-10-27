"""
Character Registry — UGC 角色叠加层（Bond-System-Design.md §5 / P1）。

在 PersonalityManager（内置人格，config/personalities/*.md）之上叠加
用户自创角色（UGC）。UGC 角色落盘在::

    data/characters/{char_id}/card.md      # 角色卡（L2 注入，含 mode: split 元数据）
    data/characters/{char_id}/meta.json    # {char_id, display_name, author, created_at, visibility, voice_id, source}

设计要点：
- ``_discover()`` 保持 **仅内置**（不覆写）：get_default() 的兜底链、
  其他模块的内置视角都不受 UGC 影响。
- UGC 可见性按作者隔离（visibility=private）：resolve/list/用户级设置
  只暴露作者本人的角色；群绑定在设置时校验（owner 本人或内置），
  读取时只要求 key 仍存在。
- 卡片内容用 mtime 校验缓存（panel↔bot 缓存一致性教训），meta.json
  很小、每次直读保证新鲜。
"""

import hashlib
import json
import logging
import os
import re
import shutil
import uuid
from typing import Dict, List, Optional, Tuple

from agent.personality import PersonalityManager
from lib.media_gen import get_voice_pool


logger = logging.getLogger(__name__)

_DEFAULT_CHARACTERS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "data", "characters"
)

# char_id slug 部分的最大长度（CJK 也算 1 字符）
_SLUG_MAX_LEN = 16


def make_char_id(display_name: str) -> str:
    """Generate a filesystem-safe unique character id.

    ``"星野 爱"`` → ``"星野爱_a3f9c1"``（slug 保留 CJK/字母/数字，
    后缀 6 位随机 hex 保证唯一性）。
    """
    slug = re.sub(r"[^\w\u4e00-\u9fff]+", "", display_name or "")
    slug = slug[:_SLUG_MAX_LEN] or "char"
    return f"{slug}_{uuid.uuid4().hex[:6]}"


class CharacterRegistry(PersonalityManager):
    """PersonalityManager + UGC characters (data/characters/)."""

    def __init__(
        self,
        personalities_dir: str = None,
        characters_dir: str = None,
    ):
        super().__init__(personalities_dir=personalities_dir)
        # env 在 __init__ 惰性读取（而非模块级常量）：单例可能在 dotenv
        # 加载前被 import，且生产部署用 CHARACTERS_DIR 把 UGC 角色指向
        # 数据盘（与 turn.py 的 _CHARACTERS_DIR 同源约定）。
        self._char_dir = (
            characters_dir
            or os.environ.get("CHARACTERS_DIR")
            or _DEFAULT_CHARACTERS_DIR
        )
        # char_id → (card.md mtime, content)
        self._ugc_cache: Dict[str, Tuple[float, str]] = {}

    # ── UGC discovery ─────────────────────────────────────────────

    def _ugc_ids(self) -> List[str]:
        """All existing UGC character ids (any author)."""
        if not os.path.isdir(self._char_dir):
            return []
        ids = []
        for entry in sorted(os.listdir(self._char_dir)):
            if os.path.isfile(os.path.join(self._char_dir, entry, "card.md")):
                ids.append(entry)
        return ids

    def _ugc_ids_for(self, user_id: Optional[str]) -> List[str]:
        """UGC character ids authored by ``user_id`` (empty if None)."""
        if not user_id:
            return []
        return [c for c in self._ugc_ids() if (self.get_meta(c) or {}).get("author") == user_id]

    def is_ugc(self, name: str) -> bool:
        """True if ``name`` is an existing UGC character id."""
        return bool(name) and os.path.isfile(
            os.path.join(self._char_dir, name, "card.md")
        )

    def _card_path(self, char_id: str) -> str:
        return os.path.join(self._char_dir, char_id, "card.md")

    def reference_image_path(self, char_id: str) -> Optional[str]:
        """角色参考立绘（P4 视觉一致性 I2I 通路）——存在才返回。

        ``data/characters/{char_id}/assets/reference.png``。缺失时调用方
        优雅降级纯文生图（机制先就位，立绘后补）。此目录受资产 LRU
        **永不淘汰**保护（asset_memory 硬保护路径）。
        """
        for ext in ("png", "jpg", "jpeg", "webp"):
            path = os.path.join(
                self._char_dir, char_id, "assets", f"reference.{ext}"
            )
            if os.path.isfile(path):
                return path
        return None

    # ── 参考立绘写端（/设形象 命令；读端见 reference_image_path）────

    def reference_image_dir(self, char_id: str) -> str:
        """Directory that holds the character's reference portrait."""
        return os.path.join(self._char_dir, char_id, "assets")

    def set_reference_image(
        self, char_id: str, data: bytes, ext: str = "png"
    ) -> str:
        """Atomically store the reference portrait; return its path.

        Writes ``assets/reference.<ext>`` via tmp + ``os.replace`` (same
        directory, so the rename stays atomic even when CHARACTERS_DIR sits
        on another disk than the upload it came from), then removes stale
        ``reference.*`` with other extensions so exactly one portrait wins
        the probe order used by :meth:`reference_image_path`.
        """
        if not self.is_ugc(char_id):
            raise FileNotFoundError(f"不是自创角色: {char_id}")
        adir = self.reference_image_dir(char_id)
        os.makedirs(adir, exist_ok=True)
        target = os.path.join(adir, f"reference.{ext}")
        tmp = target + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
        for other in ("png", "jpg", "jpeg", "webp"):
            stale = os.path.join(adir, f"reference.{other}")
            if stale != target and os.path.isfile(stale):
                try:
                    os.remove(stale)
                except OSError:
                    pass
        return target

    def clear_reference_image(self, char_id: str) -> bool:
        """Remove every reference portrait variant; True if anything went."""
        adir = self.reference_image_dir(char_id)
        removed = False
        for ext in ("png", "jpg", "jpeg", "webp"):
            path = os.path.join(adir, f"reference.{ext}")
            if os.path.isfile(path):
                try:
                    os.remove(path)
                    removed = True
                except OSError:
                    pass
        try:
            if os.path.isdir(adir) and not os.listdir(adir):
                os.rmdir(adir)
        except OSError:
            pass
        return removed

    def _meta_path(self, char_id: str) -> str:
        return os.path.join(self._char_dir, char_id, "meta.json")

    def get_meta(self, char_id: str) -> Optional[dict]:
        """Read meta.json fresh (small file; no cache).

        损坏/非 UTF-8 编码的 meta.json（如 Windows 中转产生的 GBK）返回
        None 并 log 警告——一个坏文件既不应炸掉所有用户的
        list_personalities（_ugc_ids_for 遍历全量 id），也不应无声消失。
        """
        try:
            with open(self._meta_path(char_id), "r", encoding="utf-8") as f:
                meta = json.load(f)
            return meta if isinstance(meta, dict) else None
        except FileNotFoundError:
            return None
        except (json.JSONDecodeError, IOError, UnicodeError) as e:
            logger.warning(
                "UGC meta unreadable for %s (%s: %s) — character hidden",
                char_id, type(e).__name__, e,
            )
            return None

    # ── Load / display overrides ──────────────────────────────────

    def load(self, name: str) -> Optional[str]:
        """Builtin path unchanged; UGC ids read card.md with mtime cache."""
        if not self.is_ugc(name):
            return super().load(name)
        path = self._card_path(name)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return None
        cached = self._ugc_cache.get(name)
        if cached and cached[0] == mtime:
            return cached[1]
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read().strip()
        except (IOError, OSError) as e:
            logger.warning(
                "UGC card unreadable for %s (%s: %s)", name, type(e).__name__, e,
            )
            return None
        except UnicodeError as e:
            logger.warning(
                "UGC card %s is not valid UTF-8 (%s) — check file encoding",
                name, e,
            )
            return None
        if not content:
            return None
        self._ugc_cache[name] = (mtime, content)
        return content

    def get_voice_id(self, name: str) -> Optional[str]:
        """The character's fixed TTS voice (P4 声纹一致性锚点).

        解析链：UGC meta.json.voice_id → 卡片元数据注释 voice_id（内置卡
        主通路）→ voice_pool 确定性分配（blake2b 摘要取模，**不用内建
        hash()** —— 后者受 PYTHONHASHSEED 随机化，跨进程会漂移声纹）→
        TTS default_voice → None。同一角色永远解析到同一音色 ID。
        """
        if self.is_ugc(name):
            meta = self.get_meta(name) or {}
            vid = str(meta.get("voice_id") or "").strip()
            if vid:
                return vid
        vid = super().get_voice_id(name)
        if vid:
            return vid
        pool, default = get_voice_pool()
        if pool:
            digest = hashlib.blake2b(
                str(name).encode("utf-8"), digest_size=8
            ).hexdigest()
            return pool[int(digest, 16) % len(pool)]
        return default or None

    def get_display_name(self, name: str) -> str:
        """UGC: meta.json display_name, fallback to card heading, then id."""
        if self.is_ugc(name):
            meta = self.get_meta(name) or {}
            display = str(meta.get("display_name") or "").strip()
            if display:
                return display
            return super().get_display_name(name)
        return super().get_display_name(name)

    # ── Listing ───────────────────────────────────────────────────

    def list_personalities(self, user_id: Optional[str] = None) -> List[dict]:
        """Builtins + the caller's own UGC characters (marked source=ugc)."""
        result = []
        for entry in super().list_personalities():
            entry["source"] = "builtin"
            result.append(entry)
        for char_id in self._ugc_ids_for(user_id):
            result.append({
                "key": char_id,
                "display_name": self.get_display_name(char_id),
                "source": "ugc",
            })
        return result

    def list_ugc(self, user_id: str) -> List[dict]:
        """Meta dicts of ``user_id``'s own characters (for quota checks)."""
        metas = []
        for char_id in self._ugc_ids_for(user_id):
            meta = self.get_meta(char_id) or {}
            meta.setdefault("char_id", char_id)
            metas.append(meta)
        return metas

    # ── Name resolution (user-scoped) ─────────────────────────────

    def resolve_name(  # type: ignore[override]
        self, name: str, user_id: Optional[str] = None
    ) -> Optional[str]:
        """Resolve display/key to a personality key, own-UGC first.

        Order: own UGC exact id → own UGC exact display → builtin
        (super().resolve_name) → own UGC unique display substring.
        """
        own = self._ugc_ids_for(user_id)
        if own:
            name_stripped = (name or "").strip()
            name_lower = name_stripped.lower()
            # 1. Exact char_id
            if name_stripped in own:
                return name_stripped
            # 2. Exact display name (case-insensitive)
            exact = [
                c for c in own
                if self.get_display_name(c).lower() == name_lower
            ]
            if len(exact) == 1:
                return exact[0]
        # 3. Builtin resolution (unchanged semantics)
        resolved = super().resolve_name(name)
        if resolved is not None:
            return resolved
        # 4. Unique display substring among own UGC
        if own:
            name_lower = (name or "").strip().lower()
            sub = [
                c for c in own
                if name_lower and name_lower in self.get_display_name(c).lower()
            ]
            if len(sub) == 1:
                return sub[0]
        return None

    def _resolve_or_raise(  # type: ignore[override]
        self, name: str, user_id: Optional[str] = None
    ) -> str:
        """Scoped variant: error listing shows builtins + own UGC."""
        resolved = self.resolve_name(name, user_id=user_id)
        if resolved is not None:
            return resolved
        entries = self.list_personalities(user_id=user_id)
        if not entries:
            raise ValueError("没有可用的人格配置。")
        name_lower = (name or "").strip().lower()
        matches = [
            e for e in entries
            if name_lower in e["display_name"].lower()
        ]
        examples = "\n  ".join(
            f"{e['display_name']} ({e['key']})"
            + ("　[自创]" if e.get("source") == "ugc" else "")
            for e in entries
        )
        if len(matches) > 1:
            raise ValueError(
                f"「{name}」匹配到多个人格，请更具体。\n\n可用人格:\n  {examples}"
            )
        raise ValueError(
            f"未找到匹配「{name}」的人格。\n\n可用人格:\n  {examples}"
        )

    def _exists(self, key: str, user_id: Optional[str] = None) -> bool:
        """Key validity: builtin, or UGC authored by ``user_id``.

        ``user_id=None`` → any existing UGC also passes (used for the
        group layer, which was authorized at set-time).
        """
        if key in self._discover():
            return True
        if self.is_ugc(key):
            if user_id is None:
                return True
            meta = self.get_meta(key) or {}
            return meta.get("author") == user_id
        return False

    # ── Settings overrides (user scope) ───────────────────────────

    def get_user_personality(self, user_id: str) -> str:
        settings = self._load_settings()
        name = settings.get(user_id, self.get_default())
        if not self._exists(name, user_id=user_id):
            return self.get_default()
        return name

    def get_personal_personality(self, user_id: str) -> Optional[str]:
        settings = self._load_settings()
        name = settings.get(user_id)
        if name and self._exists(name, user_id=user_id):
            return name
        return None

    def set_user_personality(self, user_id: str, name: str) -> str:
        resolved = self._resolve_or_raise(name, user_id=user_id)
        settings = self._load_settings()
        settings[user_id] = resolved
        self._save_settings(settings)
        return resolved

    # ── Group binding overrides ───────────────────────────────────

    def get_group_personality(self, group_id: str) -> Optional[str]:
        if not group_id:
            return None
        settings = self._load_group_settings()
        name = settings.get(group_id)
        if name and self._exists(name):
            return name
        return None

    def set_group_personality(  # type: ignore[override]
        self, group_id: str, name: str, owner_id: Optional[str] = None
    ) -> str:
        """Bind a group default; ``owner_id`` exposes their UGC to matching."""
        resolved = self._resolve_or_raise(name, user_id=owner_id)
        settings = self._load_group_settings()
        settings[group_id] = resolved
        self._save_group_settings(settings)
        return resolved

    def resolve_effective_personality(
        self, user_id: str, group_id: str = None
    ) -> str:
        # 1. User override (builtin or own UGC)
        settings = self._load_settings()
        name = settings.get(user_id)
        if name and self._exists(name, user_id=user_id):
            return name
        # 2. Group default
        group_name = self.get_group_personality(group_id or "")
        if group_name:
            return group_name
        # 3. Global default (builtin-only, unaffected by UGC)
        return self.get_default()

    # ── UGC lifecycle (write side; used by tools/creation_tools.py) ──

    def register_character(
        self, char_id: str, card_content: str, meta: dict
    ) -> str:
        """Write card.md + meta.json under data/characters/{char_id}/.

        Returns the character directory path. Refuses to overwrite an
        existing character (ids are uuid-suffixed; collision = bug).
        """
        char_dir = os.path.join(self._char_dir, char_id)
        if os.path.exists(char_dir):
            raise FileExistsError(f"角色目录已存在: {char_id}")
        os.makedirs(char_dir, exist_ok=True)
        tmp_card = self._card_path(char_id) + ".tmp"
        with open(tmp_card, "w", encoding="utf-8") as f:
            f.write(card_content)
        os.replace(tmp_card, self._card_path(char_id))
        meta.setdefault("char_id", char_id)
        tmp_meta = self._meta_path(char_id) + ".tmp"
        with open(tmp_meta, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        os.replace(tmp_meta, self._meta_path(char_id))
        self._ugc_cache.pop(char_id, None)
        return char_dir

    def delete(self, char_id: str) -> bool:
        """Remove a UGC character directory. Returns True if deleted."""
        if not self.is_ugc(char_id):
            return False
        shutil.rmtree(os.path.join(self._char_dir, char_id), ignore_errors=True)
        self._ugc_cache.pop(char_id, None)
        self._cache.pop(char_id, None)
        return True

    def purge_references(self, char_id: str) -> int:
        """Drop the char from user settings + group bindings.

        Returns the number of references removed. Bond data
        (data/bonds/) is intentionally kept — 重逢时好感仍在。
        """
        removed = 0
        settings = self._load_settings()
        for uid in [u for u, v in settings.items() if v == char_id]:
            del settings[uid]
            removed += 1
        if removed:
            self._save_settings(settings)
        group_settings = self._load_group_settings()
        g_removed = [g for g, v in group_settings.items() if v == char_id]
        for gid in g_removed:
            del group_settings[gid]
        if g_removed:
            self._save_group_settings(group_settings)
        return removed + len(g_removed)

    def name_taken(self, display_name: str, user_id: str) -> bool:
        """True if ``user_id`` already has a character with this display name."""
        target = (display_name or "").strip().lower()
        for char_id in self._ugc_ids_for(user_id):
            if self.get_display_name(char_id).lower() == target:
                return True
        return False


# ── Module-level singleton ────────────────────────────────────────

_character_registry: Optional[CharacterRegistry] = None


def get_character_registry() -> CharacterRegistry:
    """Get or create the module-level CharacterRegistry singleton."""
    global _character_registry
    if _character_registry is None:
        _character_registry = CharacterRegistry()
    return _character_registry

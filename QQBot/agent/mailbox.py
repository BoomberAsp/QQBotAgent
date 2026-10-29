"""
Mailbox Manager — 主动消息「角色信箱」存储层（proactive-mail-design.md v0.5 §7）。

信(letter)/信封(digest)分离模型：
- letters：每封信一个角色人格产物，≤60 字，28 天保留，每用户上限 200 封；
- digests：一次主动发送 = 一个 digest（1 条 markdown 消息，占 1 次配额）；
- 每用户一个 JSON 文件 data/mailboxes/{safe_openid}.json（与 data/bonds/ 同款），
  锁竞争天然消失、损坏爆炸半径缩小到单用户。

写入纪律：原子写（.tmp + os.replace）、损坏降级视为空信箱、bot 单写者。
letter id = ``L-YYYYMMDD-<4hex>``、digest id = ``D-YYYYMMDD-<4hex>``；
随机后缀防「当日序号 +1」在 28 天裁剪后与历史 id 撞车。

同步纯函数模块：不依赖 nonebot / asyncio / 网络，可独立单测。
RMW（读-改-写）在单一事件循环内天然原子；record_digest 写盘前重读校验
last_digest_date 兜底并发 pass（§7.2 幂等）。

本模块同时提供信封/预览用的纯文本工具（§5.2）：
- make_preview()：首行去舞台指示，按 Unicode 字符截 20 加 ``…``；
- escape_md_text()：markdown 敏感字符全角转义 + 行首列表符补全角空格。
"""

import json
import os
import random
import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional


def _safe_id(id_str: str) -> str:
    """Filesystem-safe id (same convention as bond.py / memory.py)."""
    return re.sub(r"[^0-9A-Za-z_.-]", "_", id_str or "anonymous")


# ── §5.2 预览与转义（纯函数，无 I/O）───────────────────────────────

# markdown 敏感字符 → 全角（避免破坏加粗/列表/引用/标题）
_MD_ESCAPE_MAP = {
    "*": "＊",
    "_": "＿",
    "`": "｀",
    "[": "【",
    "]": "】",
    "#": "＃",
    ">": "＞",
    "+": "＋",
}

# 行首舞台指示：（...） / (...)（可连续多个）
_STAGE_DIR_RE = re.compile(r"^(?:\s*[（(][^）)]*[）)])+\s*")
# 行首 markdown 列表符：- / * / + / 1. 等有序列表
_LIST_PREFIX_RE = re.compile(r"^(\s*)([-*+]|\d+[.)])(\s)")


def escape_md_text(text: str) -> str:
    """把 markdown 敏感字符替换为全角，行首列表符前加全角空格（§5.2）。"""
    if not text:
        return text or ""
    out_lines = []
    for line in str(text).split("\n"):
        for ch, full in _MD_ESCAPE_MAP.items():
            line = line.replace(ch, full)
        m = _LIST_PREFIX_RE.match(line)
        if m:
            line = "\u3000" + line  # 全角空格，令其不再被解析为列表
        out_lines.append(line)
    return "\n".join(out_lines)


def make_preview(text: str, limit: int = 20) -> str:
    """正文 → 预览：首行、去开头舞台指示、按 Unicode 字符截 limit 加 ``…``。"""
    if not text:
        return ""
    first_line = str(text).split("\n", 1)[0].strip()
    first_line = _STAGE_DIR_RE.sub("", first_line).strip()
    if len(first_line) <= limit:
        return first_line
    return first_line[:limit] + "…"


def new_letter_id(now: Optional[float] = None) -> str:
    """``L-YYYYMMDD-<4hex>``（随机后缀防裁剪后重号，§7.1）。"""
    ts = datetime.fromtimestamp(now if now is not None else time.time())
    return "L-%s-%04x" % (ts.strftime("%Y%m%d"), random.randint(0, 0xFFFF))


def new_digest_id(now: Optional[float] = None) -> str:
    """``D-YYYYMMDD-<4hex>``。"""
    ts = datetime.fromtimestamp(now if now is not None else time.time())
    return "D-%s-%04x" % (ts.strftime("%Y%m%d"), random.randint(0, 0xFFFF))


def today_str(now: Optional[float] = None) -> str:
    """本地日期 ``YYYY-MM-DD``（last_digest_date 幂等键）。"""
    ts = datetime.fromtimestamp(now if now is not None else time.time())
    return ts.strftime("%Y-%m-%d")


def _empty_mailbox(openid: str) -> Dict[str, Any]:
    return {
        "version": 1,
        "openid": openid,
        "active_enabled": True,
        "known_personas": {},
        "last_digest_at": 0,
        "last_digest_date": "",
        "personas": {},
        "letters": [],
        "digests": [],
    }


class MailboxManager:
    """Per-user mailbox files under ``data/mailboxes/``."""

    def __init__(self, base_dir: str):
        self.base_dir = base_dir
        os.makedirs(base_dir, exist_ok=True)

    # ── 低层 I/O ─────────────────────────────────────────────────

    def _path(self, openid: str) -> str:
        return os.path.join(self.base_dir, _safe_id(openid) + ".json")

    @staticmethod
    def _atomic_write_json(path: str, data: Any) -> None:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    def load_user(self, openid: str) -> Dict[str, Any]:
        """读取并规范化一个用户的信箱；损坏/缺失 → 空信箱（§7.1）。"""
        box = _empty_mailbox(openid)
        try:
            with open(self._path(openid), "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                for key in ("active_enabled", "known_personas", "last_digest_at",
                            "last_digest_date", "personas", "letters", "digests"):
                    if key in raw:
                        box[key] = raw[key]
                box["openid"] = openid
        except (json.JSONDecodeError, IOError, OSError):
            return _empty_mailbox(openid)
        # 类型兜底：损坏字段降级为默认值而不是抛异常
        if not isinstance(box.get("known_personas"), dict):
            box["known_personas"] = {}
        if not isinstance(box.get("personas"), dict):
            box["personas"] = {}
        if not isinstance(box.get("letters"), list):
            box["letters"] = []
        if not isinstance(box.get("digests"), list):
            box["digests"] = []
        box["letters"] = [l for l in box["letters"] if isinstance(l, dict)]
        box["digests"] = [d for d in box["digests"] if isinstance(d, dict)]
        box["active_enabled"] = bool(box.get("active_enabled", True))
        return box

    def save_user(self, openid: str, box: Dict[str, Any]) -> bool:
        """原子写整个信箱；失败只记静默（与 bond.py 同款容错）。"""
        try:
            self._atomic_write_json(self._path(openid), box)
            return True
        except (IOError, OSError):
            return False

    # ── 裁剪与记账 ───────────────────────────────────────────────

    @staticmethod
    def prune(box: Dict[str, Any], now: Optional[float] = None,
              retention_days: int = 28, max_letters: int = 200) -> Dict[str, Any]:
        """就地裁剪：过期 letters/digests 删除；letters 超上限按最旧丢弃（§7.1）。"""
        now = now if now is not None else time.time()
        cutoff = now - max(1, int(retention_days)) * 86400
        letters = [
            l for l in box.get("letters", [])
            if float(l.get("created_at", 0) or 0) >= cutoff
        ]
        if len(letters) > max_letters:
            letters.sort(key=lambda l: float(l.get("created_at", 0) or 0))
            letters = letters[-max_letters:]
        box["letters"] = letters
        kept_ids = {l.get("id") for l in letters}
        digests = [
            d for d in box.get("digests", [])
            if float(d.get("sent_at", 0) or 0) >= cutoff
            or any(i in kept_ids for i in d.get("letter_ids", []) or [])
        ]
        box["digests"] = digests
        return box

    def record_digest(self, openid: str, letters: List[Dict[str, Any]],
                      now: Optional[float] = None, retention_days: int = 28) -> Optional[str]:
        """发送成功后一次性记账（§7.2）：分配 digest id、补 preview、更新
        personas/known_personas/last_digest_*，裁剪后落盘。

        写盘前**重读**磁盘校验 last_digest_date —— 同一天已有 digest 则放弃
        （幂等，防并发 pass 双写）。返回 digest id；未写入返回 None。
        """
        now = now if now is not None else time.time()
        fresh = self.load_user(openid)  # 重读校验，不用调用方手里的旧快照
        date = today_str(now)
        if fresh.get("last_digest_date") == date:
            return None
        digest_id = new_digest_id(now)
        for letter in letters:
            letter.setdefault("id", new_letter_id(now))
            letter.setdefault("created_at", now)
            letter.setdefault("read", False)
            letter.setdefault("preview", make_preview(letter.get("text", "")))
            letter["digest_id"] = digest_id
        fresh.setdefault("letters", []).extend(letters)
        fresh.setdefault("digests", []).append({
            "id": digest_id,
            "date": date,
            "sent_at": now,
            "letter_ids": [l["id"] for l in letters],
        })
        fresh["last_digest_at"] = now
        fresh["last_digest_date"] = date
        personas = fresh.setdefault("personas", {})
        known = fresh.setdefault("known_personas", {})
        for letter in letters:
            p = letter.get("persona") or ""
            if not p:
                continue
            state = personas.get(p)
            if not isinstance(state, dict):
                state = {"last_mail_at": 0, "mail_count": 0}
            state["last_mail_at"] = now
            state["mail_count"] = int(state.get("mail_count", 0) or 0) + 1
            personas[p] = state
            if p not in known:
                known[p] = now
        self.prune(fresh, now=now, retention_days=retention_days)
        if self.save_user(openid, fresh):
            return digest_id
        return None

    def set_active_enabled(self, openid: str, enabled: bool) -> None:
        """C2C_MSG_REJECT / RECEIVE 维护的主动消息开关（§7.1）。"""
        box = self.load_user(openid)
        if bool(box.get("active_enabled", True)) == bool(enabled):
            return  # 无变化不写盘
        box["active_enabled"] = bool(enabled)
        self.save_user(openid, box)

    def ensure_known_personas(self, openid: str, personas: List[str],
                              now: Optional[float] = None) -> None:
        """pass 每次从 sessions 索引补齐 known_personas（§4.2）；
        一旦写入即使会话被删也保留。仅缺失时写盘。"""
        now = now if now is not None else time.time()
        box = self.load_user(openid)
        known = box.setdefault("known_personas", {})
        added = False
        for p in personas:
            if p and p not in known:
                known[p] = now
                added = True
        if added:
            self.save_user(openid, box)

    def mark_read(self, openid: str, letter_id: str) -> bool:
        """``/邮件 <id>`` 置 read=true（§8）；找不到返回 False。"""
        box = self.load_user(openid)
        hit = False
        for letter in box.get("letters", []):
            if letter.get("id") == letter_id:
                if not letter.get("read"):
                    letter["read"] = True
                    hit = True
                else:
                    return True  # 已读也算命中（幂等）
        if hit:
            self.save_user(openid, box)
        return hit

    def delete_letter(self, openid: str, letter_id: str) -> bool:
        """WebUI 管理队列删除信件（§16.4.3）：从 letters[] 移除。

        digest 的 letter_ids **保留**该 id——/邮件 详情据此区分「已撤回」
        与「查无此信」（is_withdrawn），信封历史不装作没发过。
        """
        box = self.load_user(openid)
        before = len(box.get("letters", []))
        box["letters"] = [l for l in box.get("letters", []) if l.get("id") != letter_id]
        if len(box["letters"]) == before:
            return False
        self.save_user(openid, box)
        return True

    def write_system_letter(self, openid: str, title: str, text: str,
                            now: Optional[float] = None,
                            retention_days: int = 28) -> Optional[str]:
        """WebUI 系统邮件发送成功后入账（§16.4.3/§16.4.4）。

        persona="__system__"、system=true、digest_id=null；**不动**
        personas / known_personas / last_digest_*——系统邮件不是角色信件，
        不参与信封聚合，也不占用「同日只发一封」幂等键。
        返回 letter id；写盘失败返回 None。
        """
        now = now if now is not None else time.time()
        box = self.load_user(openid)
        letter = {
            "id": new_letter_id(now),
            "persona": "__system__",
            "name": "系统",
            "system": True,
            "title": title or "",
            "text": text or "",
            "preview": make_preview(text or ""),
            "created_at": now,
            "read": False,
            "digest_id": None,
        }
        box.setdefault("letters", []).append(letter)
        self.prune(box, now=now, retention_days=retention_days)
        if self.save_user(openid, box):
            return letter["id"]
        return None

    # ── 查询（只读，不落盘）──────────────────────────────────────

    def list_letters(self, openid: str) -> List[Dict[str, Any]]:
        """按 created_at 倒序（新在前）。"""
        box = self.load_user(openid)
        return sorted(
            box.get("letters", []),
            key=lambda l: float(l.get("created_at", 0) or 0),
            reverse=True,
        )

    def get_letter(self, openid: str, letter_id: str) -> Optional[Dict[str, Any]]:
        box = self.load_user(openid)
        for letter in box.get("letters", []):
            if letter.get("id") == letter_id:
                return letter
        return None

    def is_withdrawn(self, openid: str, letter_id: str) -> bool:
        """信件已不在 letters[] 但仍被某 digest 引用 → 管理员撤回（§16.4.3）。"""
        if not letter_id:
            return False
        box = self.load_user(openid)
        if any(l.get("id") == letter_id for l in box.get("letters", [])):
            return False
        return any(letter_id in (d.get("letter_ids") or [])
                   for d in box.get("digests", []))

    def letters_on_date(self, openid: str, mmdd: str) -> List[Dict[str, Any]]:
        """``/邮件 <MM-DD>``：该日全部信件（本地时区，倒序）。"""
        out = []
        for letter in self.list_letters(openid):
            ts = datetime.fromtimestamp(float(letter.get("created_at", 0) or 0))
            if ts.strftime("%m-%d") == mmdd:
                out.append(letter)
        return out

    def last_digest(self, openid: str) -> Optional[Dict[str, Any]]:
        box = self.load_user(openid)
        digests = box.get("digests", [])
        if not digests:
            return None
        return max(digests, key=lambda d: float(d.get("sent_at", 0) or 0))

"""
Bond Manager — 角色养成系统好感度核心（Bond-System-Design.md §4，P0）。

三值模型：
- Bond（长期）：EXP → 10 级等级，只涨不掉，永不衰减；
- Mood（短期）：0-100，围绕基线随时间回归，影响涨点系数与提示词语气行；
- Stats（陪伴统计）：相识天数 / 累计消息数 / 礼物数（礼物为 P2 占位）。

持久化：每用户一个 JSON（data/bonds/{user_id}.json），内部按角色 key 分区。
缓存纪律与 ProfileManager 一致：进程内缓存 + st_mtime_ns 校验，外部写入
（面板/其它进程）优先于缓存，绝不用过期缓存覆盖磁盘。

数值配置：内置默认值 + 可选 config/bond_settings.json 覆盖（启动时读取）。
本模块不依赖 nonebot / 网络，可独立单测。
"""

import json
import os
import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional


# ── 默认配置（Bond-System-Design.md §4.2 / §4.3，全部可被 JSON 覆盖）──

_DEFAULT_LEVELS: List[List[Any]] = [
    [1, "陌生", 0],
    [2, "相识", 100],
    [3, "熟悉", 300],
    [4, "朋友", 650],
    [5, "密友", 1200],
    [6, "知己", 2000],
    [7, "暧昧", 3000],
    [8, "恋人", 4300],
    [9, "誓约", 6000],
    [10, "灵魂伴侣", 8000],
]

_DEFAULTS: Dict[str, Any] = {
    "levels": _DEFAULT_LEVELS,
    "first_chat_exp": 20,          # 每日首次对话
    "per_msg_exp": 3,              # 自然聊天基准（心情系数 0.8~1.2 → 实际 2~4）
    "daily_chat_soft_cap": 150,    # 每日聊天软上限，超出减半，2 倍后减到 1/4
    "long_session_bonus": 30,      # ≥10 轮长会话奖励，每日 1 次
    "long_session_turns": 10,
    "mood_baseline": 60,
    "mood_decay_per_hour": 5.0,    # 心情向基线回归的速度
    "exp_mood_min": 0.8,
    "exp_mood_max": 1.2,
    "reunion_days": 14,            # ≥N 天未见 → reunion 标记（事件文案为 P3）
}

# 角色面动态层固定提示：历史中的任务结果来自任务智能体（中立口吻），
# 角色须保持自身人设（Bond-System-Design.md §3.3 转述层配套约束）。
_TASK_RESULT_NOTE = (
    "提示: 对话历史中的任务结果可能由任务智能体以中立口吻输出；"
    "无论历史内容如何，你必须始终保持自己的角色设定、语气与称呼。"
)


def _safe_id(id_str: str) -> str:
    """Filesystem-safe id (same convention as memory.py)."""
    return re.sub(r"[^0-9A-Za-z_.-]", "_", id_str or "anonymous")


class BondManager:
    """Per (user, character) bond state: EXP/level/mood/stats."""

    def __init__(self, base_dir: str, config_path: Optional[str] = None):
        self.base_dir = base_dir
        os.makedirs(base_dir, exist_ok=True)
        if config_path is None:
            config_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "config", "bond_settings.json",
            )
        self._config_path = config_path
        self._config: Dict[str, Any] = dict(_DEFAULTS)
        self.reload_config()

        # user_id → 整个文件 dict；user_id → 上次见到的 st_mtime_ns
        self._cache: Dict[str, dict] = {}
        self._cache_mtime: Dict[str, Optional[int]] = {}

    # ── Config ────────────────────────────────────────────────────

    def reload_config(self):
        """Merge optional bond_settings.json over built-in defaults. Never raises."""
        cfg = dict(_DEFAULTS)
        try:
            if os.path.isfile(self._config_path):
                with open(self._config_path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    for k, v in loaded.items():
                        if k in cfg:
                            cfg[k] = v
        except (json.JSONDecodeError, IOError, OSError):
            cfg = dict(_DEFAULTS)
        # levels 合法性校验：[[lv, title, exp], ...] 且 exp 递增
        levels = cfg.get("levels")
        ok = (
            isinstance(levels, list) and levels
            and all(
                isinstance(e, (list, tuple)) and len(e) == 3
                and isinstance(e[0], int) and isinstance(e[2], (int, float))
                for e in levels
            )
        )
        if not ok:
            cfg["levels"] = list(_DEFAULT_LEVELS)
        else:
            cfg["levels"] = sorted(
                ([int(e[0]), str(e[1]), int(e[2])] for e in levels),
                key=lambda e: e[2],
            )
        self._config = cfg

    # ── Level curve ───────────────────────────────────────────────

    def level_for_exp(self, exp: int) -> int:
        level = self._config["levels"][0][0]
        for lv, _title, threshold in self._config["levels"]:
            if exp >= threshold:
                level = lv
        return level

    def title_for(self, level: int) -> str:
        for lv, title, _exp in self._config["levels"]:
            if lv == level:
                return title
        return ""

    def next_threshold(self, level: int) -> Optional[Dict[str, Any]]:
        """Return {"level", "title", "exp"} of the next level, or None at max."""
        levels = self._config["levels"]
        for i, (lv, _t, _e) in enumerate(levels):
            if lv == level and i + 1 < len(levels):
                nl, nt, ne = levels[i + 1]
                return {"level": nl, "title": nt, "exp": ne}
        return None

    def current_threshold(self, level: int) -> int:
        for lv, _t, e in self._config["levels"]:
            if lv == level:
                return int(e)
        return 0

    # ── Persistence (mtime-validated cache, profile.py 同款纪律) ──

    def _path(self, user_id: str) -> str:
        return os.path.join(self.base_dir, f"{_safe_id(user_id)}.json")

    @staticmethod
    def _stat_mtime(path: str) -> Optional[int]:
        try:
            return os.stat(path).st_mtime_ns
        except OSError:
            return None

    def _get_user_data(self, user_id: str) -> dict:
        path = self._path(user_id)
        disk_mtime = self._stat_mtime(path)
        if user_id in self._cache:
            if disk_mtime is not None and disk_mtime != self._cache_mtime.get(user_id):
                reloaded = self._load_file(path)
                if reloaded is not None:
                    self._cache[user_id] = reloaded
                    self._cache_mtime[user_id] = disk_mtime
            return self._cache[user_id]
        data = self._load_file(path)
        if data is None:
            data = {}
        self._cache[user_id] = data
        self._cache_mtime[user_id] = disk_mtime
        return data

    def _load_file(self, path: str) -> Optional[dict]:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else None
        except (FileNotFoundError, json.JSONDecodeError, IOError, OSError):
            return None

    def _save(self, user_id: str):
        data = self._cache.get(user_id)
        if data is None:
            return
        path = self._path(user_id)
        os.makedirs(self.base_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        self._cache_mtime[user_id] = self._stat_mtime(path)

    # ── State ─────────────────────────────────────────────────────

    def _new_state(self, now: float) -> dict:
        return {
            "exp": 0,
            "level": self._config["levels"][0][0],
            "mood": float(self._config["mood_baseline"]),
            "mood_updated": now,
            "stats": {"first_met": now, "last_seen": now, "msgs": 0, "gifts": 0},
            "unlocked": [],
            "daily": self._new_daily(),
        }

    def _new_daily(self) -> dict:
        return {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "chat_exp": 0,
            "first_done": False,
            "long_done": False,
            "gifts": 0,
            "pokes": 0,
        }

    @staticmethod
    def _roll_daily(state: dict, today: str):
        daily = state.get("daily")
        if not isinstance(daily, dict) or daily.get("date") != today:
            state["daily"] = {
                "date": today, "chat_exp": 0, "first_done": False,
                "long_done": False, "gifts": 0, "pokes": 0,
            }

    def get_state(self, user_id: str, char_id: str, create: bool = True) -> Optional[dict]:
        """Return the mutable state dict for (user, char), creating on demand.

        create=False 用于只读路径（查询/注入），避免把未落盘的空状态挂进缓存。
        """
        data = self._get_user_data(user_id)
        state = data.get(char_id)
        if not isinstance(state, dict):
            if not create:
                return None
            state = self._new_state(time.time())
            data[char_id] = state
        return state

    # ── Mood ──────────────────────────────────────────────────────

    def _decayed_mood(self, state: dict, now: float) -> float:
        baseline = float(self._config["mood_baseline"])
        try:
            mood = float(state.get("mood", baseline))
            updated = float(state.get("mood_updated", now))
        except (TypeError, ValueError):
            return baseline
        elapsed_h = max(0.0, (now - updated) / 3600.0)
        drift = float(self._config["mood_decay_per_hour"]) * elapsed_h
        if mood > baseline:
            return max(baseline, mood - drift)
        if mood < baseline:
            return min(baseline, mood + drift)
        return baseline

    def _mood_factor(self, mood: float) -> float:
        mn = float(self._config["exp_mood_min"])
        mx = float(self._config["exp_mood_max"])
        return mn + (mx - mn) * max(0.0, min(100.0, mood)) / 100.0

    @staticmethod
    def _mood_label(mood: float) -> str:
        if mood >= 85:
            return "极佳"
        if mood >= 70:
            return "不错"
        if mood >= 45:
            return "平静"
        if mood >= 30:
            return "有点低落"
        return "低落"

    def update_mood(self, user_id: str, char_id: str, delta: float) -> float:
        """Apply a mood delta (clamped 0-100) and persist. Returns new mood."""
        now = time.time()
        state = self.get_state(user_id, char_id)
        current = self._decayed_mood(state, now)
        new_mood = max(0.0, min(100.0, current + float(delta)))
        state["mood"] = new_mood
        state["mood_updated"] = now
        self._save(user_id)
        return new_mood

    # ── EXP settlement ────────────────────────────────────────────

    def settle_turn(self, user_id: str, char_id: str, turn_count: int = 0) -> Dict[str, Any]:
        """回合结束结算钩子（agent_router 每轮调用，两个平面都结算）。

        规则（Bond-System-Design.md §4.3）：
        - 每日首次对话 +first_chat_exp（独立于软上限额度）；
        - 每轮 per_msg_exp × 心情系数；当日聊天 EXP 超软上限后减半、
          超 2 倍软上限后减到 1/4（渐进衰减而非硬截断）；
        - turn_count ≥ long_session_turns 且当日未领 → +long_session_bonus；
        - 心情先按时间回归基线再参与计算；
        - 跨级时返回 level_ups 列表（供路由层发升级庆祝）。
        """
        now = time.time()
        today = datetime.now().strftime("%Y-%m-%d")
        state = self.get_state(user_id, char_id)
        self._roll_daily(state, today)

        # 久别重逢检测（基于上一轮 last_seen；事件文案为 P3，这里只打标）
        gap_days = 0
        last_seen = state.get("stats", {}).get("last_seen")
        if last_seen:
            gap_days = int((now - float(last_seen)) // 86400)
        reunion = gap_days >= int(self._config["reunion_days"])

        # 心情回归并落定
        mood = self._decayed_mood(state, now)
        state["mood"] = mood
        state["mood_updated"] = now

        daily = state["daily"]
        gained = 0

        first_today = not daily.get("first_done")
        if first_today:
            gained += int(self._config["first_chat_exp"])
            daily["first_done"] = True

        chat_exp = int(daily.get("chat_exp", 0))
        cap = int(self._config["daily_chat_soft_cap"])
        msg_exp = float(self._config["per_msg_exp"]) * self._mood_factor(mood)
        if chat_exp >= cap * 2:
            msg_exp *= 0.25
        elif chat_exp >= cap:
            msg_exp *= 0.5
        msg_exp = max(1, int(round(msg_exp)))
        gained += msg_exp
        daily["chat_exp"] = chat_exp + msg_exp

        if (
            turn_count >= int(self._config["long_session_turns"])
            and not daily.get("long_done")
        ):
            gained += int(self._config["long_session_bonus"])
            daily["long_done"] = True

        level_before = self.level_for_exp(int(state.get("exp", 0)))
        state["exp"] = int(state.get("exp", 0)) + gained
        level_after = self.level_for_exp(state["exp"])
        state["level"] = level_after
        level_ups = [
            {"level": lv, "title": self.title_for(lv)}
            for lv in range(level_before + 1, level_after + 1)
        ]

        stats = state.setdefault("stats", {})
        stats["msgs"] = int(stats.get("msgs", 0)) + 1
        if not stats.get("first_met"):
            stats["first_met"] = now
        stats["last_seen"] = now

        self._save(user_id)
        return {
            "exp_gained": gained,
            "level_ups": level_ups,
            "is_first_today": first_today,
            "reunion": reunion,
            "level": level_after,
            "exp": state["exp"],
        }

    def add_exp(self, user_id: str, char_id: str, amount: int) -> List[Dict[str, Any]]:
        """Generic EXP grant (礼物/戳一戳/场景完成等 P2+ 来源). Returns level_ups."""
        amount = int(amount)
        if amount <= 0:
            return []
        state = self.get_state(user_id, char_id)
        level_before = self.level_for_exp(int(state.get("exp", 0)))
        state["exp"] = int(state.get("exp", 0)) + amount
        level_after = self.level_for_exp(state["exp"])
        state["level"] = level_after
        self._save(user_id)
        return [
            {"level": lv, "title": self.title_for(lv)}
            for lv in range(level_before + 1, level_after + 1)
        ]

    # ── Prompt injection (动态层，Bond-System-Design.md §6) ──────

    def build_prompt_context(
        self, user_id: str, char_id: str, char_short_name: str = ""
    ) -> str:
        """Build the L4 volatile bond block for the character plane."""
        state = self.get_state(user_id, char_id, create=False)
        header = "## 羁绊状态（你与当前用户）"
        if state is None:
            return (
                f"{header}\n"
                f"好感等级: Lv{self._config['levels'][0][0]} "
                f"{self.title_for(self._config['levels'][0][0])}（初次相识）\n"
                f"你们的故事才刚刚开始，友好而自然地相处吧。\n"
                f"{_TASK_RESULT_NOTE}"
            )
        now = time.time()
        mood = self._decayed_mood(state, now)
        level = int(state.get("level", 1))
        exp = int(state.get("exp", 0))
        nxt = self.next_threshold(level)
        if nxt:
            progress = f"（{exp}/{nxt['exp']} → Lv{nxt['level']} {nxt['title']}）"
        else:
            progress = "（已满级）"
        first_met = state.get("stats", {}).get("first_met")
        days = max(1, int((now - float(first_met)) // 86400) + 1) if first_met else 1
        msgs = int(state.get("stats", {}).get("msgs", 0))
        return (
            f"{header}\n"
            f"好感等级: Lv{level} {self.title_for(level)}{progress}\n"
            f"心情: {int(round(mood))}/100（{self._mood_label(mood)}），"
            f"请让语气自然贴合当前心情。\n"
            f"陪伴: 相识第 {days} 天 · 累计互动 {msgs} 条消息"
            + (f" · 用户常唤你「{char_short_name}」" if char_short_name else "")
            + f"\n{_TASK_RESULT_NOTE}"
        )

    # ── User-facing status (/好感度 指令) ─────────────────────────

    def format_status(
        self, user_id: str, char_id: str, display_name: str = ""
    ) -> str:
        state = self.get_state(user_id, char_id, create=False)
        name = display_name or char_id
        if state is None:
            return (
                f"💗 与「{name}」的羁绊\n"
                f"你们还没有产生交集——和 TA 聊聊天，羁绊就会开始生长~"
            )
        now = time.time()
        mood = self._decayed_mood(state, now)
        level = int(state.get("level", 1))
        exp = int(state.get("exp", 0))
        cur = self.current_threshold(level)
        nxt = self.next_threshold(level)
        if nxt:
            span = max(1, nxt["exp"] - cur)
            ratio = max(0.0, min(1.0, (exp - cur) / span))
            bar = "▰" * int(round(ratio * 10)) + "▱" * (10 - int(round(ratio * 10)))
            progress_line = f"进度: {bar} {exp}/{nxt['exp']} → Lv{nxt['level']} {nxt['title']}"
        else:
            progress_line = "进度: ▰▰▰▰▰▰▰▰▰▰ 已达最高等级"
        first_met = state.get("stats", {}).get("first_met")
        days = max(1, int((now - float(first_met)) // 86400) + 1) if first_met else 1
        msgs = int(state.get("stats", {}).get("msgs", 0))
        self._roll_daily(state, datetime.now().strftime("%Y-%m-%d"))
        chat_today = int(state.get("daily", {}).get("chat_exp", 0))
        cap = int(self._config["daily_chat_soft_cap"])
        return (
            f"💗 与「{name}」的羁绊\n"
            f"等级: Lv{level} {self.title_for(level)}\n"
            f"{progress_line}\n"
            f"心情: {int(round(mood))}/100（{self._mood_label(mood)}）\n"
            f"陪伴: 相识第 {days} 天 · 累计互动 {msgs} 条消息\n"
            f"今日聊天好感: {chat_today}/{cap}（超出后收益递减）"
        )

    # ── Admin ─────────────────────────────────────────────────────

    def reset(self, user_id: str, char_id: Optional[str] = None):
        """Reset one character's bond (or the whole user file when char_id=None)."""
        data = self._get_user_data(user_id)
        if char_id is None:
            data.clear()
        else:
            data.pop(char_id, None)
        self._save(user_id)

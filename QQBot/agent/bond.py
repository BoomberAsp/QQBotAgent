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
from typing import Any, Dict, List, Optional, Tuple


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
    # ── P2: 互动动作（礼物 / 戳一戳）──
    "gift_exp_loved": 30,          # 送到喜爱
    "gift_exp_normal": 15,         # 普通礼物
    "gift_exp_disliked": 5,        # 送到讨厌（仍有正反馈，只是最低档）
    "daily_gift_limit": 3,         # 每日送礼计数上限
    "gift_mood_loved": 8.0,
    "gift_mood_normal": 4.0,
    "gift_mood_disliked": -3.0,
    "poke_exp": 5,                 # 戳一戳（摸头）
    "daily_poke_limit": 3,
    "poke_mood": 3.0,
    # ── P3: 剧情场景 / 重逢 / 纪念日 ──
    "scene_exp": 100,              # 完成一个剧情场景的一次性奖励
    "scene_mood": 10.0,            # 完成场景的心情增量
    "reunion_enabled": True,       # 久别重逢语气提示开关
    "anniversary_days": [30, 100, 365, 730, 1095],  # 相识天数里程碑（可配）
    # ── P3b: 低频主动问候（官方 c2c 主动消息，HEARTBEAT 驱动）──
    "greet_enabled": True,         # 主动问候总开关
    "greet_min_level": 5,          # 好感等级门（§7.2 提议 Lv5 密友起）
    "greet_cooldown_days": 7,      # 距上次问候的冷却天数
    "greet_probability": 0.25,     # 概率门（每次 HEARTBEAT pass 每候选）
    "greet_daily_cap": 1,          # 每用户每日主动消息上限（§7.3）
    "greet_monthly_cap": 4,        # 每用户每月主动消息上限（平台小额配额）
    "greet_batch_max": 5,          # 单次 pass 最多问候人数
    "greet_interval_hours": 6.0,   # HEARTBEAT 巡检间隔（小时）
    "greet_jitter_minutes": 45,    # 巡检间隔随机抖动（分钟），避免整点齐发
    # ── P4: 多模态生成（§7.2 三重门控的可配数值）──
    "media_enabled": True,             # 生成玩法总开关
    "media_image_min_level": 4,        # create_image 好感等级门（§7.2 图 Lv4）
    "media_voice_min_level": 5,        # send_voice 好感等级门（§7.2 语音 Lv5）
    "media_proactive_voice_min_level": 7,  # 主动语音等级门（§7.2 主动语音 Lv7）
    "daily_image_regular": 3,          # 每日生成配额（按用户角色分档）
    "daily_image_vip": 10,
    "daily_image_admin": 50,
    "daily_voice_regular": 3,
    "daily_voice_vip": 10,
    "daily_voice_admin": 50,
    "proactive_media_probability": 0.15,  # 主动媒体概率门（每候选每 pass）
    "proactive_media_daily_cap": 1,    # 主动媒体 ≤1 次/天/用户（§7.3）
    "media_dedupe_ttl_hours": 168,     # prompt_hash 去重窗口（7 天）
    "media_tts_max_chars": 300,        # TTS 文本上限（plus 按字符计费）
    "media_asset_quota_mb_regular": 100,   # 资产层磁盘配额（P5 LRU 用）
    "media_asset_quota_mb_vip": 300,
    "media_asset_quota_mb_admin": 1000,
    # ── P5: 多模态记忆闭环（§8）──
    "upload_exp": 3,               # 用户上传图/语音被文本化入库的小额好感
    "upload_exp_daily_cap": 3,     # 每日授予次数封顶（防刷图刷好感）
    "episode_distill_enabled": True,  # 里程碑事件蒸馏进 TieredMemory 开关
    # ── 主动消息「角色信箱」（proactive-mail-design.md v0.5 §11）──
    "mail_enabled": True,          # 信箱总开关；false 回滚旧 greeting 路径
    "mail_hour": 10,               # 每日发送目标时刻（服务器本地时间）
    "mail_daily_cap": 1,           # 每人每天最多 1 个信封（产品级防打扰）
    "mail_min_gap_hours": 20,      # 防「10:00 正常 + 启动补发」同日双发
    "mail_inactive_hours": 24,     # 角色级沉默门槛：刚聊过不发
    "mail_persona_cooldown_days": 7,  # 同一角色两封信的最小间隔
    "mail_min_level": 5,           # 只给高好感角色发
    "mail_max_letters": 5,         # 信封内最多几封信
    "mail_max_previews": 5,        # 带按钮的预览数；超出只列文字
    "mail_max_chars": 800,         # 信封 markdown 总长上限
    "mail_retention_days": 28,     # letters/digests 保留天数
    "mail_gen_concurrency": 4,     # 用户间信件生成信号量并发
    "mail_pass_budget_minutes": 20,  # pass 级时间预算；超时顺延次日
    "mail_review_enabled": False,  # 本地审核默认关（§5.3）
    "mail_full_when_single": True, # 单封且 ≤80 字直接显示全文（B2 格式）
    "mail_group_enabled": False,   # 群公告通道（P1）独立开关，v1 不实现群发
    "mail_group_announcement": "", # 群公告文案；为空不发
    "mail_rate_c2c_per_sec": 10,   # c2c Bot 维度令牌桶速率
    "mail_rate_group_per_min": 60, # 群 Bot 维度速率（保守 1/s）
    "mail_rate_group_per_relation_qpm": 20,  # 单群速率
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

    def __init__(
        self,
        base_dir: str,
        config_path: Optional[str] = None,
        events_dir: Optional[str] = None,
        characters_dir: Optional[str] = None,
    ):
        self.base_dir = base_dir
        # UGC 角色目录（data/characters/）：提供时，bond 状态首次创建会按
        # meta.json 的 initial_bond_level 播种初始羁绊（仅对作者本人生效）。
        # 默认 None = 播种关闭（内置卡无 meta.json，天然不受影响）。
        self.characters_dir = characters_dir
        os.makedirs(base_dir, exist_ok=True)
        # 共同经历日志根目录（默认与 bonds/ 同级的 bond_events/）
        self.events_dir = events_dir or os.path.join(
            os.path.dirname(os.path.abspath(base_dir)), "bond_events"
        )
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

    def get_config(self, key: str, default: Any = None) -> Any:
        """Public read-only access to a config value (social_tools 等外部
        模块不直接摸 _config)。"""
        return self._config.get(key, default)

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
            # P3
            "scenes_done": {},           # {scene_title: ts} 已完成场景（防重复刷 EXP）
            "anniversaries_marked": [],  # [int] 已庆祝的相识天数里程碑
        }

    def _new_daily(self) -> dict:
        return {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "chat_exp": 0,
            "first_done": False,
            "long_done": False,
            "gifts": 0,
            "pokes": 0,
            "images": 0,     # P4: 今日已生成图像数
            "voices": 0,     # P4: 今日已合成语音数
            "uploads": 0,    # P5: 今日已授予 EXP 的上传文本化次数
        }

    @staticmethod
    def _roll_daily(state: dict, today: str):
        daily = state.get("daily")
        if not isinstance(daily, dict) or daily.get("date") != today:
            state["daily"] = {
                "date": today, "chat_exp": 0, "first_done": False,
                "long_done": False, "gifts": 0, "pokes": 0,
                "images": 0, "voices": 0, "uploads": 0,
            }

    def get_state(self, user_id: str, char_id: str, create: bool = True) -> Optional[dict]:
        """Return the mutable state dict for (user, char), creating on demand.

        create=False 用于只读路径（查询/注入），避免把未落盘的空状态挂进缓存。
        新建状态时按 UGC 卡的 initial_bond_level 播种初始羁绊（仅作者本人）。
        """
        data = self._get_user_data(user_id)
        state = data.get(char_id)
        if not isinstance(state, dict):
            if not create:
                return None
            state = self._new_state(time.time())
            self._seed_initial_bond(state, user_id, char_id)
            data[char_id] = state
        return state

    def list_personas(self, user_id: str) -> List[str]:
        """Return char_id keys present in the user's bond file (read-only).

        角色信箱候选人格发现的第一数据源（proactive-mail-design.md §4.2）：
        bond 文件本身就是 {char_id: state}。不创建、不落盘。
        """
        data = self._get_user_data(user_id)
        return [k for k, v in data.items() if isinstance(v, dict)]

    def _seed_initial_bond(self, state: dict, user_id: str, char_id: str):
        """Apply the UGC card's initial_bond_level to a brand-new state.

        只对作者本人生效（meta.author == user_id）：初始羁绊是创作者对
        「自己与 TA」关系起点的预设，群里其他成员仍从 Lv1 开始。
        任何读取/解析失败都静默降级为默认 Lv1——播种绝不炸 get_state。
        """
        if not self.characters_dir:
            return
        # 与 CharacterRegistry 同款布局：characters_dir/{char_id}/meta.json
        # （char_id 原样作目录名，不走 _safe_id）；挡掉路径穿越形态的 id。
        if not char_id or char_id in (".", "..") or re.split(r"[/\\]", char_id) != [char_id]:
            return
        try:
            meta_path = os.path.join(self.characters_dir, char_id, "meta.json")
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
            if not isinstance(meta, dict) or meta.get("author") != user_id:
                return
            level = int(meta.get("initial_bond_level", 1))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return
        levels = self._config["levels"]
        level = max(levels[0][0], min(level, levels[-1][0]))
        if level <= levels[0][0]:
            return
        state["exp"] = self.current_threshold(level)
        state["level"] = level

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

    def set_level(self, user_id: str, char_id: str, level: int) -> List[Dict[str, Any]]:
        """把羁绊直接设到目标等级（EXP=该级阈值）——本人显式重设，所有人可用。

        「只涨不掉」是自然积累的纪律；用户显式重设视为自由存档，**允许下调**
        （回调起点/活动补偿/测试皆宜）。调用方须自行保证作用于本人
        （聊天命令 /设好感 永远以发送者 user_id 调用）。等级 clamp 到
        等级表范围；上调时返回跨越的 level_ups（复用 add_exp 的返回形态），
        下调返回 []。
        """
        levels = self._config["levels"]
        try:
            level = int(level)
        except (TypeError, ValueError):
            return []
        level = max(levels[0][0], min(level, levels[-1][0]))
        state = self.get_state(user_id, char_id)
        level_before = int(state.get("level", levels[0][0]))
        state["exp"] = self.current_threshold(level)
        state["level"] = level
        self.append_event(
            user_id, char_id,
            f"羁绊等级被调整为 Lv{level}（{self.title_for(level)}）",
        )
        self._save(user_id)
        return [
            {"level": lv, "title": self.title_for(lv)}
            for lv in range(level_before + 1, level + 1)
        ]

    # ── P2: 互动动作（礼物 / 戳一戳）───────────────────────────────

    def _apply_mood(self, state: dict, now: float, delta: float):
        cur = self._decayed_mood(state, now)
        state["mood"] = max(0.0, min(100.0, cur + float(delta)))
        state["mood_updated"] = now

    def give_gift(
        self, user_id: str, char_id: str, tier: str = "normal"
    ) -> Dict[str, Any]:
        """送礼结算：按偏好档位给 EXP + 心情，受每日次数上限约束。

        tier ∈ {loved, normal, disliked}（由 bond_content.match_gift 判定）。
        返回 {accepted, tier, exp_gained, level_ups, gifts_today, level, exp}；
        超限时 accepted=False，不给分。
        """
        tier = tier if tier in ("loved", "normal", "disliked") else "normal"
        now = time.time()
        today = datetime.now().strftime("%Y-%m-%d")
        state = self.get_state(user_id, char_id)
        self._roll_daily(state, today)
        daily = state["daily"]
        limit = int(self._config["daily_gift_limit"])
        gifts_today = int(daily.get("gifts", 0))
        if gifts_today >= limit:
            return {
                "accepted": False, "reason": "daily_limit", "tier": tier,
                "gifts_today": gifts_today, "daily_gift_limit": limit,
                "level": self.level_for_exp(int(state.get("exp", 0))),
            }
        exp = int(self._config[f"gift_exp_{tier}"])
        daily["gifts"] = gifts_today + 1
        level_ups = self.add_exp(user_id, char_id, exp)
        stats = state.setdefault("stats", {})
        stats["gifts"] = int(stats.get("gifts", 0)) + 1
        self._apply_mood(state, now, float(self._config[f"gift_mood_{tier}"]))
        self._save(user_id)
        return {
            "accepted": True, "tier": tier, "exp_gained": exp,
            "level_ups": level_ups, "gifts_today": daily["gifts"],
            "daily_gift_limit": limit,
            "level": self.level_for_exp(int(state["exp"])), "exp": state["exp"],
        }

    def poke(self, user_id: str, char_id: str) -> Dict[str, Any]:
        """戳一戳（摸头）结算：小额 EXP + 心情，受每日次数上限约束。"""
        now = time.time()
        today = datetime.now().strftime("%Y-%m-%d")
        state = self.get_state(user_id, char_id)
        self._roll_daily(state, today)
        daily = state["daily"]
        limit = int(self._config["daily_poke_limit"])
        pokes_today = int(daily.get("pokes", 0))
        if pokes_today >= limit:
            return {
                "accepted": False, "reason": "daily_limit",
                "pokes_today": pokes_today, "daily_poke_limit": limit,
                "level": self.level_for_exp(int(state.get("exp", 0))),
            }
        exp = int(self._config["poke_exp"])
        daily["pokes"] = pokes_today + 1
        level_ups = self.add_exp(user_id, char_id, exp)
        stats = state.setdefault("stats", {})
        stats["pokes"] = int(stats.get("pokes", 0)) + 1
        self._apply_mood(state, now, float(self._config["poke_mood"]))
        self._save(user_id)
        return {
            "accepted": True, "exp_gained": exp, "level_ups": level_ups,
            "pokes_today": daily["pokes"], "daily_poke_limit": limit,
            "level": self.level_for_exp(int(state["exp"])), "exp": state["exp"],
        }

    # ── P4: 多模态生成门控与配额（§7.2 等级门 + 配额门）──────────────

    # media_type → daily 计数键（与 _new_daily/_roll_daily 对应）
    _MEDIA_COUNTERS = {"image": "images", "voice": "voices"}

    def get_level(self, user_id: str, char_id: str) -> int:
        """当前好感等级（无状态 → Lv1）。"""
        state = self.get_state(user_id, char_id, create=False)
        if not state:
            return 1
        return self.level_for_exp(int(state.get("exp", 0)))

    def media_level_gate(
        self,
        user_id: str,
        char_id: str,
        media_type: str,
        proactive: bool = False,
    ) -> Tuple[bool, int, int]:
        """等级门：返回 (通过?, 当前等级, 要求等级)。

        - image → media_image_min_level
        - voice（被动）→ media_voice_min_level
        - voice（主动，proactive=True）→ media_proactive_voice_min_level
        未知 media_type 视为不通过（fail-closed）。
        """
        if media_type == "image":
            required = int(self._config.get("media_image_min_level", 4))
        elif media_type == "voice":
            key = (
                "media_proactive_voice_min_level" if proactive
                else "media_voice_min_level"
            )
            required = int(self._config.get(key, 7 if proactive else 5))
        else:
            return False, 0, 0
        level = self.get_level(user_id, char_id)
        return level >= required, level, required

    def check_media_quota(
        self, user_id: str, char_id: str, media_type: str, role: str
    ) -> Tuple[bool, str]:
        """配额门：返回 (可生成?, 拒绝原因)。只读判定，不写盘。

        原因串机器可读（调用方转 observation）：
        ``media_disabled`` / ``daily_limit:{used}/{limit}`` / ``""``（通过）。
        """
        counter = self._MEDIA_COUNTERS.get(media_type)
        if counter is None:
            return False, "unknown_media_type"
        if not bool(self._config.get("media_enabled", True)):
            return False, "media_disabled"
        state = self.get_state(user_id, char_id)
        self._roll_daily(state, datetime.now().strftime("%Y-%m-%d"))
        used = int(state["daily"].get(counter, 0))
        role_key = role if role in ("regular", "vip", "admin") else "regular"
        limit = int(self._config.get(f"daily_{media_type}_{role_key}", 3))
        if used >= limit:
            return False, f"daily_limit:{used}/{limit}"
        return True, ""

    def record_media_use(self, user_id: str, char_id: str, media_type: str) -> int:
        """生成**成功后**记账（同 give_gift 模式）。返回今日已用次数。"""
        counter = self._MEDIA_COUNTERS.get(media_type)
        if counter is None:
            return 0
        state = self.get_state(user_id, char_id)
        self._roll_daily(state, datetime.now().strftime("%Y-%m-%d"))
        used = int(state["daily"].get(counter, 0)) + 1
        state["daily"][counter] = used
        stats = state.setdefault("stats", {})
        stats[counter] = int(stats.get(counter, 0)) + 1
        self._save(user_id)
        return used

    def record_upload_exp(self, user_id: str, char_id: str) -> int:
        """P5（§8.3）：用户上传的图/语音被文本化入库 → 小额好感。

        每日 upload_exp_daily_cap 次封顶（防刷图刷好感）。返回本次授予的
        EXP（封顶后为 0，调用方据此写审计）。
        """
        state = self.get_state(user_id, char_id)
        self._roll_daily(state, datetime.now().strftime("%Y-%m-%d"))
        used = int(state["daily"].get("uploads", 0))
        cap = int(self._config.get("upload_exp_daily_cap", 3))
        if used >= cap:
            return 0
        state["daily"]["uploads"] = used + 1
        exp = int(self._config.get("upload_exp", 3))
        if exp > 0:
            self.add_exp(user_id, char_id, exp)
        else:
            self._save(user_id)
        return exp

    # ── P3: 剧情场景完成记录（防重复刷 EXP）────────────────────────

    def mark_scene_done(self, user_id: str, char_id: str, title: str) -> bool:
        """Record a scene as completed. Returns False if it was already done
        (调用方据此跳过 +scene_exp，防止重复完成刷分)。"""
        key = str(title or "").strip()
        if not key:
            return False
        state = self.get_state(user_id, char_id)
        done = state.setdefault("scenes_done", {})
        if not isinstance(done, dict):
            done = {}
            state["scenes_done"] = done
        if key in done:
            return False
        done[key] = time.time()
        self._save(user_id)
        return True

    def has_done_scene(self, user_id: str, char_id: str, title: str) -> bool:
        state = self.get_state(user_id, char_id, create=False)
        if not state:
            return False
        done = state.get("scenes_done")
        return isinstance(done, dict) and str(title or "").strip() in done

    def scenes_done(self, user_id: str, char_id: str) -> Dict[str, float]:
        """{scene_title: ts} of completed scenes (empty if none)."""
        state = self.get_state(user_id, char_id, create=False)
        if not state:
            return {}
        done = state.get("scenes_done")
        return dict(done) if isinstance(done, dict) else {}

    def complete_scene(self, user_id: str, char_id: str, title: str) -> Dict[str, Any]:
        """场景完成结算：一次性给 scene_exp + 心情（重复完成不给分）。

        返回 {accepted, exp_gained, level_ups, level, exp}。accepted=False 表示
        该场景已完成过（不再奖励）。调用方负责 append_event + 纪念卡片。
        """
        if not self.mark_scene_done(user_id, char_id, title):
            state = self.get_state(user_id, char_id)
            return {
                "accepted": False, "reason": "already_done", "exp_gained": 0,
                "level_ups": [], "level": self.level_for_exp(int(state.get("exp", 0))),
                "exp": int(state.get("exp", 0)),
            }
        exp = int(self._config.get("scene_exp", 100))
        level_ups = self.add_exp(user_id, char_id, exp)
        state = self.get_state(user_id, char_id)
        self._apply_mood(state, time.time(), float(self._config.get("scene_mood", 10.0)))
        self._save(user_id)
        return {
            "accepted": True, "exp_gained": exp, "level_ups": level_ups,
            "level": self.level_for_exp(int(state["exp"])), "exp": int(state["exp"]),
        }

    # ── P3: 久别重逢 / 纪念日（被动触发，入站回合内）──────────────

    def _reunion_gap_days(self, state: dict, now: float) -> int:
        last_seen = state.get("stats", {}).get("last_seen")
        if not last_seen:
            return 0
        try:
            return int((now - float(last_seen)) // 86400)
        except (TypeError, ValueError):
            return 0

    def reunion_pending(self, user_id: str, char_id: str) -> int:
        """Gap-days if this turn is a reunion (≥reunion_days) and enabled, else 0.
        回合前调用（build_prompt_context 用它注入重逢语气）。"""
        if not bool(self._config.get("reunion_enabled", True)):
            return 0
        state = self.get_state(user_id, char_id, create=False)
        if not state:
            return 0
        gap = self._reunion_gap_days(state, time.time())
        return gap if gap >= int(self._config["reunion_days"]) else 0

    def note_reunion(self, user_id: str, char_id: str) -> bool:
        """One-per-day guard for the reunion shared-memory event.
        settle_turn 已刷新 last_seen，本方法在其后调用；返回 True 表示今天首次
        记录重逢（应 append_event），False 表示已记过。"""
        today = datetime.now().strftime("%Y-%m-%d")
        state = self.get_state(user_id, char_id)
        if state.get("reunion_marked_date") == today:
            return False
        state["reunion_marked_date"] = today
        self._save(user_id)
        return True

    def _days_known(self, state: dict, now: float) -> int:
        first_met = state.get("stats", {}).get("first_met")
        if not first_met:
            return 0
        try:
            return max(1, int((now - float(first_met)) // 86400) + 1)
        except (TypeError, ValueError):
            return 0

    def _pending_anniversary(self, state: dict, now: float) -> Optional[int]:
        """Largest milestone reached-but-unmarked (day count), or None."""
        days = self._days_known(state, now)
        if days <= 0:
            return None
        marked = set(int(m) for m in (state.get("anniversaries_marked") or []))
        hit: Optional[int] = None
        for m in self._config.get("anniversary_days", []):
            m = int(m)
            if days >= m and m not in marked:
                if hit is None or m > hit:
                    hit = m
        return hit

    def anniversary_pending(self, user_id: str, char_id: str) -> Optional[int]:
        """Milestone day if reached-but-unmarked (回合前注入用), else None."""
        state = self.get_state(user_id, char_id, create=False)
        if not state:
            return None
        return self._pending_anniversary(state, time.time())

    def note_anniversary(self, user_id: str, char_id: str) -> Optional[int]:
        """Settle-side: mark ALL passed-but-unmarked milestones and return the
        largest one (the one to celebrate), or None. Marking every passed
        milestone at once prevents skipped ones from trickling out on later
        turns (e.g. an absent user who jumps from day 0 to day 400)."""
        state = self.get_state(user_id, char_id, create=False)
        if not state:
            return None
        now = time.time()
        days = self._days_known(state, now)
        milestone = self._pending_anniversary(state, now)
        if milestone is None:
            return None
        marked = state.setdefault("anniversaries_marked", [])
        if not isinstance(marked, list):
            marked = []
            state["anniversaries_marked"] = marked
        for m in self._config.get("anniversary_days", []):
            m = int(m)
            if days >= m and m not in marked:
                marked.append(m)
        self._save(user_id)
        return milestone

    # ── P2: 共同经历日志（bond_events，喂动态层「最近共同经历」）──

    def _event_path(self, user_id: str, char_id: str) -> str:
        return os.path.join(
            self.events_dir, _safe_id(user_id), f"{_safe_id(char_id)}.md"
        )

    def append_event(self, user_id: str, char_id: str, text: str):
        """Append one shared-memory line. Silent on failure (增益功能不阻断)。"""
        text = (text or "").strip()
        if not text:
            return
        try:
            path = self._event_path(user_id, char_id)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            ts = datetime.now().strftime("%Y-%m-%d %H:%M")
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"- {ts} {text}\n")
        except (IOError, OSError):
            pass

    _EVENT_LINE_RE = re.compile(r"^-\s*\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}\s*(.*)$")

    def recent_events(self, user_id: str, char_id: str, n: int = 3) -> List[str]:
        """Return the last ``n`` shared-memory texts (timestamp stripped)."""
        try:
            with open(self._event_path(user_id, char_id), "r", encoding="utf-8") as f:
                raw = [ln.strip() for ln in f if ln.strip()]
        except (FileNotFoundError, IOError, OSError):
            return []
        out: List[str] = []
        for ln in raw[-n:]:
            m = self._EVENT_LINE_RE.match(ln)
            out.append(m.group(1).strip() if m else ln.lstrip("- ").strip())
        return [o for o in out if o]

    # ── Prompt injection (动态层，Bond-System-Design.md §6) ──────

    def build_prompt_context(
        self,
        user_id: str,
        char_id: str,
        char_short_name: str = "",
        unlocked_text: str = "",
        address_line: str = "",
        recent: Optional[List[str]] = None,
    ) -> str:
        """Build the L4 volatile bond block for the character plane.

        P2 追加动态段：称呼演化指令、已解锁 Bond Tiers 档案、最近共同经历
        （均来自调用方注入，保持 bond.py 与角色卡解析解耦）。
        """
        state = self.get_state(user_id, char_id, create=False)
        header = "## 羁绊状态（你与当前用户）"
        if state is None:
            return (
                f"{header}\n"
                f"好感等级: Lv{self._config['levels'][0][0]} "
                f"{self.title_for(self._config['levels'][0][0])}（初次相识）\n"
                f"你们的故事才刚刚开始，友好而自然地相处吧。\n"
                + (f"{address_line}\n" if address_line else "")
                + f"{_TASK_RESULT_NOTE}"
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
        stats = state.get("stats", {})
        first_met = stats.get("first_met")
        days = max(1, int((now - float(first_met)) // 86400) + 1) if first_met else 1
        msgs = int(stats.get("msgs", 0))
        gifts = int(stats.get("gifts", 0))
        parts = [
            header,
            f"好感等级: Lv{level} {self.title_for(level)}{progress}",
            f"心情: {int(round(mood))}/100（{self._mood_label(mood)}），"
            f"请让语气自然贴合当前心情。",
            f"陪伴: 相识第 {days} 天 · 累计互动 {msgs} 条消息"
            + (f" · 收礼 {gifts} 次" if gifts else "")
            + (f" · 用户常唤你「{char_short_name}」" if char_short_name else ""),
        ]
        if address_line:
            parts.append(address_line)
        # P3: 久别重逢 / 纪念日语气提示（被动，入站回合内触发）
        gap = self._reunion_gap_days(state, now)
        if bool(self._config.get("reunion_enabled", True)) and \
                gap >= int(self._config["reunion_days"]):
            parts.append(
                f"久别重逢: 距上次交谈已过去 {gap} 天，你们终于重逢。"
                f"请自然流露出想念与重逢的喜悦（真挚但不过度煽情）。"
            )
        anniv = self._pending_anniversary(state, now)
        if anniv:
            parts.append(
                f"纪念日: 今天是你们相识的第 {anniv} 天，是一个值得纪念的里程碑。"
                f"可以自然地提及这个特别的日子，表达对这段陪伴的珍惜。"
            )
        if unlocked_text:
            parts.append(
                "已解锁的羁绊档案（你可以自然地提及或透露这些背景，"
                "但不要生硬背诵）:\n" + unlocked_text
            )
        if recent:
            parts.append(
                "最近的共同经历:\n" + "\n".join(f"- {e}" for e in recent)
            )
        parts.append(_TASK_RESULT_NOTE)
        return "\n".join(parts)

    # ── User-facing status (/好感度 指令) ─────────────────────────

    def format_status(
        self, user_id: str, char_id: str, display_name: str = "", role: str = "regular"
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
        stats = state.get("stats", {})
        first_met = stats.get("first_met")
        days = max(1, int((now - float(first_met)) // 86400) + 1) if first_met else 1
        msgs = int(stats.get("msgs", 0))
        gifts_total = int(stats.get("gifts", 0))
        self._roll_daily(state, datetime.now().strftime("%Y-%m-%d"))
        daily = state.get("daily", {})
        chat_today = int(daily.get("chat_exp", 0))
        cap = int(self._config["daily_chat_soft_cap"])
        gifts_today = int(daily.get("gifts", 0))
        pokes_today = int(daily.get("pokes", 0))
        gift_line = (
            f"\n今日互动: 送礼 {gifts_today}/{int(self._config['daily_gift_limit'])}"
            f" · 戳一戳 {pokes_today}/{int(self._config['daily_poke_limit'])}"
        )
        media_line = ""
        if bool(self._config.get("media_enabled", True)):
            role_key = role if role in ("vip", "admin") else "regular"
            img_cap = int(self._config.get(f"daily_image_{role_key}", 3))
            voc_cap = int(self._config.get(f"daily_voice_{role_key}", 3))
            media_line = (
                f"\n今日生成: 画图 {int(daily.get('images', 0))}/{img_cap}"
                f" · 语音 {int(daily.get('voices', 0))}/{voc_cap}"
            )
        return (
            f"💗 与「{name}」的羁绊\n"
            f"等级: Lv{level} {self.title_for(level)}\n"
            f"{progress_line}\n"
            f"心情: {int(round(mood))}/100（{self._mood_label(mood)}）\n"
            f"陪伴: 相识第 {days} 天 · 累计互动 {msgs} 条消息"
            + (f" · 累计收礼 {gifts_total} 次" if gifts_total else "")
            + f"\n今日聊天好感: {chat_today}/{cap}（超出后收益递减）"
            + gift_line
            + media_line
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

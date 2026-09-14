"""
Layer 2 — Deterministic post-filter for extracted user-fact candidates.

Runs BEFORE any candidate is persisted (profile interests today; three-tier
SHORT memory in P1). It catches the junk that the LLM extractor reliably
misses, using ONLY unambiguous compound terms + regex patterns + structural
rules.

Design principles (see Profile-Fact-Extraction-Plan.md §10):

1. Only blacklist UNAMBIGUOUS compounds / regex. Ambiguous bare words
   (会话 / 工具 / 速度 / 角色 / 羁绊 / 截图 / 上传 / 搜索 / 招募 …) are NOT
   blacklisted — a false negative (junk leaking through) is visible and
   fixable, but a false positive (silently dropping a legitimate fact) is
   invisible and dangerous. Ambiguous cases are Layer 1's job (litmus test +
   few-shot in the extraction prompt).

2. NEVER blacklist specific character / nickname names (团长 / 露比 / 蝶子 …).
   Those junk facts are symptoms of "agent_response used as evidence" and
   "tool-output-derived" bugs, which are fixed at Layer 1 — name-blacklisting
   is whack-a-mole.

3. Every drop returns (matched, category) so the caller can log
   (candidate, matched_term, category) for observability-driven tuning.

Pure functions, no side effects, no logging — reusable by both the profile
extractor and the one-off cleanup script.
"""

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

__all__ = ["FilterResult", "filter_candidate", "filter_many"]


@dataclass
class FilterResult:
    """Outcome of filtering one candidate fact / interest string."""

    keep: bool
    matched: Optional[str] = None   # term / pattern text that triggered the drop
    category: Optional[str] = None  # rule category, e.g. "L2A:bot_state"


# ── L2-A: hard-drop unambiguous compound terms (substring match) ──────────
#
# Each entry has NO legitimate durable-user-fact reading in this bot's domain.

_L2A_BOT_STATE: List[str] = [
    # bot architecture / session state
    "工作区", "特殊会话", "临时会话", "连续对话", "连续模式", "对话窗口",
    "系统提示词", "权限级别", "工具范围", "可用工具", "代码执行限制",
    "磁盘用量", "剩余空间", "存储配额",
]

_L2A_TOOL_ARTIFACT: List[str] = [
    # products of tool operation, not user attributes
    "文件路径", "截图路径", "行动值", "跑条", "拉条", "推条", "测速",
    "兑换码", "礼包码", "CDK", "CDKey",
]

_L2A_GACHA: List[str] = [
    # unambiguous gacha compounds (bare 抽 / 招募 are NOT here — too ambiguous)
    "十连", "单抽", "卡池", "抽卡", "连抽",
    "抽取结果", "抽卡结果", "招募结果",
    "常规招募", "几率up招募", "神秘招募", "银河招募",
]

_L2A_BOT_SELF: List[str] = [
    # a fact about the bot is never a fact about the user
    "Roxy", "机器人", "智能体",
]

# (category, terms) — order defines precedence in the returned category label.
_L2A_GROUPS: List[Tuple[str, List[str]]] = [
    ("bot_state", _L2A_BOT_STATE),
    ("tool_artifact", _L2A_TOOL_ARTIFACT),
    ("gacha", _L2A_GACHA),
    ("bot_self", _L2A_BOT_SELF),
]


# ── L2-B: hard-drop regex patterns ────────────────────────────────────────

_L2B_PATTERNS: List[Tuple[str, "re.Pattern[str]"]] = [
    # storage capacity, e.g. "250 MB", "2048MB", "1.5 GB"
    ("capacity_unit", re.compile(r"\d+(?:\.\d+)?\s*(?:MB|GB|KB)", re.IGNORECASE)),
    # gacha outcome verbs (抽-specific → unambiguous) + star-rated gacha objects.
    # NOTE: bare 获得 / 出了 are deliberately excluded (用户获得了学位 / 出了书
    # are legitimate); the star-object branch catches "获得了2个4星紫色羁绊".
    ("gacha_result", re.compile(
        r"抽到|未抽到|没抽到|歪了|出了金|\d\s*星.{0,4}(?:角色|羁绊|物品|装备|武器|光锥|声骸)"
    )),
    # error / failure markers (transient incidents, not durable facts)
    ("error_marker", re.compile(r"报错|失败|超时|异常|崩溃|卡住")),
    # NOTE (P1, 2026-09): the former `transient_state` category
    # (正在|当前正|刚刚|刚才|这次|本次) was REMOVED ENTIRELY.
    # Why: bare 正在 false-dropped legitimate REAL-LIFE ongoing activities
    # ("用户正在通过饮食调整降血脂" / "用户正在做力量训练…" — these PASS the
    # litmus test and must be KEPT). Two such facts were wrongly dropped from
    # user 1114144652 in the 2026-09-14 prod cleanup (dormant `facts` field,
    # backed up; restored via scripts/migrate_memory_p1.py --restore-false-drops).
    # The other markers were redundant: bot-session cases (当前正在使用连续对话 /
    # 本次会话) are already caught by L2-A bot_state + L2-C unresolved_ref, and
    # gacha cases (刚刚十连) by L2-A gacha + L2-B gacha_result. Pure bot-transient
    # events (刚刚上传 / 这次搜索) are now Layer 1's job — litmus test + few-shot
    # 例5 in profile.py. See Profile-Fact-Extraction-Plan §12.2.
]


# ── L2-C: structural rules ────────────────────────────────────────────────

# Bot/project-context anaphora that is almost always an unresolved reference.
# Deliberately NARROW: bare 该 / 此 / 这个 / 那个 are excluded because they
# frequently appear in legitimate facts ("该职业稳定", "这个城市很好").
_L2C_UNRESOLVED_REF = re.compile(
    r"该项目|该会话|该文件|该任务|该仓库|该工具|该命令|该代码|"
    r"此项目|本项目|上述|这个项目|那个项目|本次会话|本次任务"
)
# Named-entity markers that "resolve" a reference (quoted titles / brackets).
_L2C_ENTITY_MARKERS = re.compile(r"[「」《》【】\"']")
# Candidate that is just a number / unit with no subject semantics.
_L2C_PURE_NUMERIC = re.compile(
    r"^[\d\s.,/%]+(?:MB|GB|KB|mb|gb|kb|分钟|小时|秒|次|个|星)?[\s。.!！?？]*$"
)


def filter_candidate(text: Optional[str]) -> FilterResult:
    """Decide whether one extracted candidate should be kept.

    Args:
        text: the candidate fact / interest string.

    Returns:
        FilterResult(keep=True) if it survives; otherwise
        FilterResult(keep=False, matched=<trigger>, category=<rule>).

    Pure function — no side effects. The caller is responsible for logging
    drops as (text, matched, category) for observability.
    """
    if not text or not text.strip():
        return FilterResult(keep=False, matched="empty", category="empty")

    s = text.strip()
    s_lower = s.lower()

    # L2-A: unambiguous compound terms (case-insensitive substring)
    for category, terms in _L2A_GROUPS:
        for term in terms:
            if term.lower() in s_lower:
                return FilterResult(keep=False, matched=term, category=f"L2A:{category}")

    # L2-B: regex patterns
    for category, pattern in _L2B_PATTERNS:
        m = pattern.search(s)
        if m:
            return FilterResult(keep=False, matched=m.group(0), category=f"L2B:{category}")

    # L2-C: unresolved bot/project reference with no named entity present
    if _L2C_ENTITY_MARKERS.search(s) is None:
        m = _L2C_UNRESOLVED_REF.search(s)
        if m:
            return FilterResult(keep=False, matched=m.group(0), category="L2C:unresolved_ref")

    # L2-C: pure number / unit, no subject semantics
    if _L2C_PURE_NUMERIC.match(s):
        return FilterResult(keep=False, matched=s, category="L2C:pure_numeric")

    return FilterResult(keep=True)


def filter_many(candidates: List[str]) -> Tuple[List[str], List[Tuple[str, str, str]]]:
    """Filter a list of candidates.

    Returns:
        (kept, dropped) where:
          kept   = list of candidate strings that survived
          dropped = list of (candidate, matched, category) tuples for logging
    """
    kept: List[str] = []
    dropped: List[Tuple[str, str, str]] = []
    for c in candidates:
        result = filter_candidate(c)
        if result.keep:
            kept.append(c)
        else:
            dropped.append((c, result.matched or "", result.category or ""))
    return kept, dropped

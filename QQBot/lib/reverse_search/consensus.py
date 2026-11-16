"""同名共识（纯函数，可离线测试）。

方案坑4：单一引擎命中不可信。采纳规则：
- 名字/handle 在 ≥2 条不同候选里出现；或
- 相似度 ≥80 且带作者主页链接（author_url）；或
- Lens 视觉匹配的作者主页单独命中（Lens 无相似度分，但视觉匹配=精确图匹配；
  lens.rows_to_candidates 只在「平台+handle+非帖子页」时才设 author_url，
  帖子页单独命中仍拒）
不满足的账号进「低置信」区，只报数量与少量 handle，不当结论。
"""
from __future__ import annotations

from .accounts import Account, aggregate_accounts, handles_in_text
from .engines import Candidate

HIGH_SIMILARITY = 80.0


def _name_counts(candidates: list[Candidate]) -> dict[str, int]:
    """每个名字/handle 出现在多少条不同候选（author 字段 + 标题@handle）。"""
    counts: dict[str, int] = {}
    for cand in candidates:
        names: set[str] = set()
        if cand.author and cand.author.strip():
            names.add(cand.author.strip().lower())
        for h in handles_in_text(cand.title):
            names.add(h.lower())
        for n in names:
            counts[n] = counts.get(n, 0) + 1
    return counts


def build_consensus(candidates: list[Candidate],
                    ) -> tuple[list[Account], list[Account]]:
    """返回 (采纳账号, 低置信账号)，各自按证据数/相似度降序。"""
    accounts = aggregate_accounts(candidates)
    counts = _name_counts(candidates)
    for acc in accounts:
        nk = (acc.name or acc.handle).strip().lower()
        acc.evidence_count = max(acc.evidence_count, counts.get(nk, 0))

    accepted, rejected = [], []
    for acc in accounts:
        # 坑4：账号必须有真实链接证据，纯名字不进结论
        if acc.url and (acc.evidence_count >= 2 or (
                acc.best_similarity >= HIGH_SIMILARITY and acc.has_author_url)
                or (acc.has_author_url and "Lens" in acc.engines)):
            accepted.append(acc)
        else:
            rejected.append(acc)
    order = lambda a: (-a.evidence_count, -a.best_similarity)  # noqa: E731
    accepted.sort(key=order)
    rejected.sort(key=order)
    return accepted, rejected

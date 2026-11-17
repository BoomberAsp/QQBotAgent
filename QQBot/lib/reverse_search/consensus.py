"""同名共识（纯函数，可离线测试）。

方案坑4：单一引擎命中不可信。采纳规则：
- 同一账号有 ≥2 个不同来源页；或
- 高相似度命中带作者主页；或
- Lens 直接命中平台主页，作为账号线索。
页面署名和同图核验单独记录。
不满足的账号进「低置信」区，只报数量与少量 handle，不当结论。
"""
from __future__ import annotations

from .accounts import Account, aggregate_accounts
from .engines import Candidate

HIGH_SIMILARITY = 80.0


def build_consensus(candidates: list[Candidate],
                    ) -> tuple[list[Account], list[Account]]:
    """返回 (采纳账号, 低置信账号)，各自按证据数/相似度降序。"""
    accounts = aggregate_accounts(candidates)
    accepted, rejected = [], []
    for acc in accounts:
        # 坑4：账号必须有真实链接证据，纯名字不进结论
        weak_artwork = (acc.platform in {"pixiv", "ArtStation", "DeviantArt"}
                        and acc.best_similarity < HIGH_SIMILARITY
                        and acc.engines <= {"SauceNAO", "IQDB"})
        if acc.url and not weak_artwork and (acc.evidence_count >= 2 or (
                acc.best_similarity >= HIGH_SIMILARITY and acc.has_author_url)
                or (acc.has_author_url and "Lens" in acc.engines)):
            accepted.append(acc)
        else:
            acc.rejection_reason = ("no_link" if not acc.url else
                                    "weak_artwork" if weak_artwork else "insufficient_pages")
            rejected.append(acc)
    order = lambda a: (-a.evidence_count, -a.best_similarity)  # noqa: E731
    accepted.sort(key=order)
    rejected.sort(key=order)
    return accepted, rejected

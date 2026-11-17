"""Public source-page evidence retained independently of account candidates."""
from dataclasses import dataclass, field

from .accounts import canonical_url, is_author_page, is_post_page, page_key, platform_of
from .engines import Candidate

MAX_SOURCES = 12


@dataclass
class SourceEvidence:
    url: str
    title: str = ""
    kind: str = "page"
    match: str = "visual"
    similarity: float | None = None
    engines: list[str] = field(default_factory=list)
    verification: str = "unverified"
    attribution: str = ""


def _rank(source: SourceEvidence) -> tuple[int, int, float]:
    match = {"exact": 0, "indexed": 1, "visual": 2, "watermark": 3, "similar_art": 4}
    kind = {"post": 0, "work": 0, "page": 1, "profile": 2}
    return match.get(source.match, 3), kind.get(source.kind, 2), -(source.similarity or 0)


def merge_sources(sources: list[SourceEvidence], candidates: list[Candidate]) -> list[SourceEvidence]:
    """Group hits by the underlying page and retain their engine provenance."""
    pages = {page_key(source.url): source for source in sources}
    for candidate in candidates:
        url = canonical_url(candidate.page_url or candidate.author_url)
        if not url:
            continue
        key = page_key(url)
        match = candidate.extra.get("match", "visual" if candidate.engine == "Lens" else "indexed")
        if (candidate.similarity is not None and candidate.similarity < 80
                and candidate.engine in {"SauceNAO", "IQDB"}
                and platform_of(url) in {"pixiv", "ArtStation", "DeviantArt"}):
            match = "similar_art"
        kind = ("work" if candidate.engine == "trace.moe" else
                "profile" if is_author_page(url) else "post" if is_post_page(url) else "page")
        evidence = SourceEvidence(
            url, candidate.title[:200], kind, match, candidate.similarity, [candidate.engine])
        previous = pages.get(key)
        if previous is None:
            pages[key] = evidence
            continue
        if _rank(evidence) < _rank(previous) and previous.verification == "unverified":
            previous.match = evidence.match
            previous.similarity = evidence.similarity
        if not previous.title and evidence.title:
            previous.title = evidence.title
        if candidate.engine not in previous.engines:
            previous.engines.append(candidate.engine)
    return list(pages.values())


def source_order(source: SourceEvidence) -> tuple:
    return (source.verification != "same_image", *_rank(source))

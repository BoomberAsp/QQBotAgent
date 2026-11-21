"""Choose bubble breaks for short conversational group replies."""
import re

_END = re.compile(r"[。！？!?]+|(?<!\.)\.(?=[ \t])|\n+")
_STRUCTURED = re.compile(
    r"https?://|www\.|[\x60]|(?:^|\n)\s*(?:[>#*|]|[-+]\s|\d+[.、)]\s*)|\[\[|<@",
    re.I,
)
_SHORT_REACTION = re.compile(r"[\s哈呵欸诶啊嗯哦噢嘿呜哎唔呀嘛啧咦～~。！？!?.]+")
_PAIRS = dict(zip("“‘「『【（", "”’」』】）"))
_CLOSERS = set(_PAIRS.values())


def split_chatter(text: str, max_parts: int, *, mention_every_part: bool = False) -> list[str]:
    """Split at sentence boundaries, keeping the original text in the parts."""
    if not text:
        return []
    if max_parts <= 1 or len(text) > 160 or _STRUCTURED.search(text):
        return [text]
    if mention_every_part and len(text) <= 40:
        return [text]

    chunks = []
    nested = []
    last_checked = start = 0
    pending = ""
    for match in _END.finditer(text):
        end = match.end()
        if end <= last_checked:
            continue
        while end < len(text) and text[end] in _CLOSERS:
            end += 1
        for char in text[last_checked:end]:
            if char in _PAIRS:
                nested.append(_PAIRS[char])
            elif nested and char == nested[-1]:
                nested.pop()
        last_checked = end
        if nested:
            continue
        pending += text[start:end]
        start = end
        if len(re.findall(r"\w", pending)) >= 7 and not _SHORT_REACTION.fullmatch(pending):
            chunks.append(pending)
            pending = ""

    tail = pending + text[start:]
    if chunks and (not tail.strip() or len(re.findall(r"\w", tail)) < 7 or _SHORT_REACTION.fullmatch(tail)):
        chunks[-1] += tail
    elif tail:
        chunks.append(tail)
    maximum = min(3, max_parts)
    return chunks[:maximum - 1] + ["".join(chunks[maximum - 1:])] if len(chunks) > maximum else chunks

"""按证据分别渲染同图页面、账号线索、作品识别和水印。"""
from __future__ import annotations

from .accounts import Account
from .source_evidence import SourceEvidence, source_order

ACCURACY_NOTE = "反查结果可能认错，请自行核实。"


def _fmt_time(ts: float) -> str:
    m, s = divmod(int(ts), 60)
    return f"{m}:{s:02d}" if m else f"{s}秒"


def _account_block(acc: Account) -> str:
    lines = [f"🌐 平台：{acc.platform}"]
    if acc.name and acc.name.lower() != acc.handle.lower():
        lines.append(f"👤 名字：{acc.name}")
    if acc.handle and acc.url:
        lines.append(f"🆔 账号：@{acc.handle} · {acc.url}")
    elif acc.url:
        lines.append(f"🆔 链接：{acc.url}")
    if acc.label:
        lines.append(f"🏷 备注：{acc.label}")
    lines.append("🔎 账号线索，作者归属待核验")
    return "\n".join(lines)


def _work_block(work: dict) -> str:
    lines = [f"🎬 疑似作品：{work.get('title', '')}"]
    ep = work.get("episode")
    if ep:
        pos = _fmt_time(work.get("from", 0))
        lines.append(f"📺 疑似第 {ep} 集 {pos} 附近")
    if work.get("url"):
        lines.append(f"🔗 {work['url']}")
    return "\n".join(lines)


def _footer(elapsed_s: float, engines: list[str], cached: bool,
            engine_errors: dict[str, str]) -> str:
    parts = [f"用时 {elapsed_s:.1f} 秒"]
    if cached:
        parts.append("命中缓存")
    if engines:
        parts.append("引擎 " + " / ".join(engines))
    lines = [" · ".join(parts)]
    if engine_errors:
        detail = "；".join(f"{name}：{msg}" for name, msg in engine_errors.items())
        lines.append(f"⚠️ 部分引擎未返回：{detail}")
    lines.append(ACCURACY_NOTE)
    return "\n".join(lines)


def _represent_sources(sources: list[SourceEvidence], excluded: set[str]) -> list[str]:
    ordered = sorted(
        (source for source in sources
         if source.verification == "same_image" or source.url not in excluded),
        key=source_order)
    verified = [source for source in ordered if source.verification == "same_image"]
    clues = [source for source in ordered if source.verification != "same_image"]
    sections = []
    if verified:
        lines = ["🔗 同图页面"]
        for source in verified[:3]:
            credit = f" · 页面署名：{source.attribution}" if source.attribution else ""
            lines.append(f"{source.url}{credit}")
        sections.append("\n".join(lines))
    if clues:
        lines = ["🔎 待核实的来源线索"]
        for source in clues[:3]:
            label = {"exact": "完全匹配结果", "similar_art": "相似画作", "watermark": "水印搜索"}.get(
                source.match, "搜索候选")
            lines.append(f"{label} · {source.url}")
        sections.append("\n".join(lines))
    return sections


def render_reply(*, accounts: list[Account], rejected: list[Account],
                 work: dict | None, vlm_text: str | None,
                 engine_errors: dict[str, str], elapsed_s: float,
                 engines: list[str], cached: bool = False,
                 sources: list[SourceEvidence] | None = None,
                 watermark_text: list[str] | None = None, lens_overview: str = "") -> str:
    footer = _footer(elapsed_s, engines, cached, engine_errors)

    sections = []
    if accounts:
        sections.append("🎯 找图结果")
        sections.extend(_account_block(account) for account in accounts[:3])
    elif work or vlm_text:
        sections.append("🎬 作品识别")
    elif not sources and not watermark_text:
        sections.append("没找到可靠来源。可以换更清晰的原图、带水印的图再试。")
    excluded = {account.url for account in accounts[:3]}
    if work:
        excluded.add(work.get("url", ""))
    sections.extend(_represent_sources(sources or [], excluded))
    if work:
        sections.append(_work_block(work))
    if vlm_text:
        sections.append(f"🤖 视觉识别：{vlm_text}")
    if lens_overview and (not vlm_text or not vlm_text.startswith("（Lens AI 概览）")):
        sections.append(f"Lens 概览（识别线索）：{lens_overview}")
    if watermark_text:
        sections.append("图中水印线索：" + "、".join(watermark_text[:5]))
    if rejected:
        handles = [f"@{account.handle}" if account.handle else account.display
                   for account in rejected[:3]]
        more = " 等" if len(rejected) > 3 else ""
        sections.append(f"另过滤 {len(rejected)} 个低置信账号（{'、'.join(handles)}{more}），未作为结论。")
    sections.append(footer)
    return "\n\n".join(sections)

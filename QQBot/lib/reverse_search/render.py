"""回话版式（方案 §2.3，纯函数可离线测试）。

一期不含头像附图与「不对→下一候选」（三期）。两种形态：
账号反查结果 / 作品识别；都没有 → 未找到 + 引擎状态。
"""
from __future__ import annotations

from .accounts import Account

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


def render_reply(*, accounts: list[Account], rejected: list[Account],
                 work: dict | None, vlm_text: str | None,
                 engine_errors: dict[str, str], elapsed_s: float,
                 engines: list[str], cached: bool = False) -> str:
    footer = _footer(elapsed_s, engines, cached, engine_errors)

    if accounts:
        blocks = [_account_block(a) for a in accounts[:3]]
        body = "🎯 找图结果\n\n" + "\n\n".join(blocks)
        if work:
            body += "\n\n" + _work_block(work)
        if rejected:
            handles = [f"@{a.handle}" if a.handle else a.display
                       for a in rejected[:3]]
            more = " 等" if len(rejected) > 3 else ""
            body += (f"\n\n另过滤 {len(rejected)} 个低置信账号"
                     f"（{'、'.join(handles)}{more}），未作为结论。")
        return body + "\n\n" + footer

    if work or vlm_text:
        body = "🎬 作品识别\n"
        if work:
            body += "\n" + _work_block(work)
        if vlm_text:
            body += f"\n\n🤖 视觉识别：{vlm_text}"
        return body + "\n\n" + footer

    body = "没找到可靠来源。可以换更清晰的原图、带水印的图再试。"
    if vlm_text:
        body += f"\n\n🤖 视觉识别：{vlm_text}"
    return body + "\n\n" + footer

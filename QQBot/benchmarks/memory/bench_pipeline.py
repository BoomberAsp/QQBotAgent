#!/usr/bin/env python3
"""L2 端到端管线 benchmark — persona 脚本走完整抽取管线，对照 fact sheet 确定性打分。

Memory-Optimization-Plan §3.5 批次 0 的 L2 部分。与 L1（bench_engine.py，纯引擎、
无 LLM、进 CI）不同，L2 驱动 **真实生产管线**：

    persona 脚本 → ProfileManager.observe_turn（Layer 3 缓冲/批量）
      → extract_batch（Layer 1 LLM 抽取 + judge 路由 ⑧）
      → fact_filter（Layer 2 确定性过滤 ②）
      → TieredMemory.ingest_candidates（三层引擎）

然后对照 personas/*.json 的 ground-truth fact sheet 做 **确定性关键词组打分**
（无 LLM 评委——开放式 QA 阶段推迟到 L3，见计划文档）。

用法（需要真实 API key，会产生 token 费用，因此 **不进 test.sh / CI**）：

    BENCH_API_KEY=sk-xxx python benchmarks/memory/bench_pipeline.py
    BENCH_API_KEY=sk-xxx python benchmarks/memory/bench_pipeline.py --persona p1_gamer_student
    BENCH_API_KEY=sk-xxx python benchmarks/memory/bench_pipeline.py --save-baseline
    BENCH_API_KEY=sk-xxx python benchmarks/memory/bench_pipeline.py --compare baselines/l2_baseline.json

可选环境变量：
    BENCH_API_BASE  默认 https://api.deepseek.com/v1
    BENCH_MODEL     默认 deepseek-chat（生产 flash 档同款）

打分语义（与 persona 卡 expect 字段一一对应）：
  关键词元素可以是 str（单一体）或 list（同义备选，OR）；元素之间 AND。
  备选列表用于对抗 LLM 抽取的改写漂移（原话「堵死了」→ 存储「堵车」）。
  in_store      关键词组（AND）出现在任一层级条目或画像槽位 → PASS
  absent        关键词组不出现 → PASS；implanted_false 用 OR 同义词匹配 +
                否定前缀护栏（「不能吃辣」不算命中植入词「能吃辣」）
  evolved       新状态出现 且 无“纯旧”条目（同时含新旧关键词的融合改写
                「从初音换成崩铁」算新状态，不算旧残留）→ PASS
                状态: EVOLVED / MERGED(过) STALE / LOST(败)
  single_value  与 evolved 同一套 replacement 判定：纠正后的值在场且无
                独立旧值条目 → PASS（⑧ 的失败模式 = CONTRADICTED/STALE）
  canary        只报告不判分（CANARY-OK / CANARY-FAIL），不进通过率

匹配语料 = short.content + medium.content + long.title/summary + 画像
nickname/interests/preferences 值。**不含** long 的 snapshot_turns（那是原始
对话存档，拿它匹配会让 absent 类产生假阳性）。

退出码恒为 0（诊断工具），--strict 时任何硬 FAIL 退出 1。
"""

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
QQBOT_DIR = HERE.parents[1]
if str(QQBOT_DIR) not in sys.path:
    sys.path.insert(0, str(QQBOT_DIR))

from agent.memory import TieredMemory                      # noqa: E402
from agent.profile import ProfileManager                   # noqa: E402
from lib.deepseek_client import DeepSeekClient             # noqa: E402
from lib.token_ledger import extract_usage                 # noqa: E402

PERSONAS_DIR = HERE / "personas"
BASELINES_DIR = HERE / "baselines"

# 固定调度参数（确定性优先，不做随机化）
POST_GAP_TURNS = 2      # 事实事件放在偶数轮
TAIL_FILLER_TURNS = 6   # 最后一个事实事件后的闲聊尾巴
DRAIN_POLL = 0.05

# 展示顺序
CATEGORY_ORDER = [
    "stable_preference", "evolving_fact", "one_shot_junk", "re_derivable",
    "critical_lowfreq", "salient_episode", "contradiction", "implanted_false",
]

_NEG_PREFIX = "不没别非无"  # 否定护栏：植入词前一个字符是这些 → 不算命中


# ── BenchClient：本地累计 token，绝不碰生产账本 ──────────────────────

class BenchClient(DeepSeekClient):
    """DeepSeekClient 子类：_record_usage 改为本地累计（不进 TokenLedger）。

    api_key/api_base 显式传入 → 跳过 NoneBot driver 查找，可在裸进程运行。
    """

    def __init__(self, api_key, api_base, model):
        super().__init__(api_key=api_key, api_base=api_base, model=model)
        self.bench_usage = {
            "calls": 0, "input_tokens": 0, "output_tokens": 0,
            "cached_tokens": 0, "api_errors": 0,
        }

    def _record_usage(self, result: dict, purpose: str):
        try:
            u = extract_usage(result)
            self.bench_usage["calls"] += 1
            self.bench_usage["input_tokens"] += u.get("input_tokens", 0)
            self.bench_usage["output_tokens"] += u.get("output_tokens", 0)
            self.bench_usage["cached_tokens"] += u.get("cached_input_tokens", 0)
        except Exception:
            pass

    async def chat_completion(self, message, history=None, timeout_set=180.0,
                              purpose="chat"):
        out = await super().chat_completion(
            message, history=history, timeout_set=timeout_set, purpose=purpose,
        )
        # 错误路径不经过 _record_usage（HTTP 异常直接返回错误串），单独计数
        if isinstance(out, str) and (
            out.startswith("调用DeepSeek API时出错") or "超时" in out[:12]
        ):
            self.bench_usage["api_errors"] += 1
        return out


# ── 脚本生成（确定性） ────────────────────────────────────────────────

def _pre_events(facts):
    """前置事件：所有非植入事实的 mentions，按轮次 round-robin 交错。

    同一事实的相邻两次提及间隔 ≈ 事实数×2 轮，确保跨多个抽取批次
    （考验 judge 的 reinforce/update 路由，而不是同批次内的去重）。
    """
    spoken = [f for f in facts if f["category"] != "implanted_false"]
    max_m = max((f.get("mentions", 0) for f in spoken), default=0)
    events = []
    for r in range(max_m):
        for f in spoken:
            if r < f.get("mentions", 0):
                utts = f.get("utterances") or [""]
                events.append((f, utts[r % len(utts)], "pre"))
    return events


def _post_events(facts):
    """后置事件：evolves / contradicts 的 mentions（同样 round-robin）。"""
    spoken = [f for f in facts if f["category"] != "implanted_false"]
    subs = []
    for f in spoken:
        sub = f.get("evolves") or f.get("contradicts")
        if sub:
            subs.append((f, sub))
    max_m = max((s.get("mentions", 0) for _, s in subs), default=0)
    events = []
    for r in range(max_m):
        for f, sub in subs:
            if r < sub.get("mentions", 0):
                utts = sub.get("utterances") or [""]
                events.append((f, utts[r % len(utts)], "post"))
    return events


def build_script(persona, fillers):
    """生成 [(user_msg, agent_reply)] 全脚本 + 事件索引 {turn: (fid, phase)}。

    事实事件占偶数轮（从 2 开始），pre 全部排完后 post 紧随其后；
    其余轮次用共享闲聊池循环填充。总轮数 = 最后事件轮 + TAIL_FILLER_TURNS。
    """
    facts = persona["facts"]
    user_fillers = fillers["user_fillers"]
    agent_replies = fillers["agent_replies"]

    slot_events = _pre_events(facts) + _post_events(facts)
    event_at = {}   # turn -> (fact, utterance, phase)
    turn = POST_GAP_TURNS
    for ev in slot_events:
        event_at[turn] = ev
        turn += POST_GAP_TURNS
    total_turns = (turn - POST_GAP_TURNS) + TAIL_FILLER_TURNS

    script, filler_i = [], 0
    for t in range(1, total_turns + 1):
        if t in event_at:
            msg = event_at[t][1]
        else:
            msg = user_fillers[filler_i % len(user_fillers)]
            filler_i += 1
        reply = agent_replies[(t - 1) % len(agent_replies)]
        script.append((msg, reply))
    return script, event_at, total_turns


# ── 打分 ─────────────────────────────────────────────────────────────

def _collect_corpus(tm, user_id, profile):
    """[(source, text)]：三层语义字段 + 画像槽位。不含 long 快照原文。"""
    st = tm._get_state(user_id)
    corpus = []
    for s in st.short:
        corpus.append(("short", s.content))
    for m in st.medium:
        corpus.append((f"medium(c={m.count},age={st.extraction_count - m.entry_extraction_index})",
                       m.content))
    for lo in st.long:
        corpus.append(("long", f"{lo.title} {lo.summary}"))
    if profile.nickname:
        corpus.append(("nickname", profile.nickname))
    for i in profile.interests:
        corpus.append(("interest", str(i)))
    for k, v in (profile.preferences or {}).items():
        corpus.append(("preference", f"{k} {v}"))
    return corpus


def _kw_match(kw, text_lower):
    """单个关键词元素：str = 单一体；list = 同义备选（OR）。

    抽取层是 LLM 改写（「路上堵死了」可能存成「用户遇到堵车」），persona 卡
    用备选列表覆盖原话表面词 + 常见改写词，元素之间仍是 AND。
    """
    if isinstance(kw, str):
        return kw.lower() in text_lower
    return any(str(a).lower() in text_lower for a in kw)


def _group_match(keywords, text_lower):
    return all(_kw_match(k, text_lower) for k in keywords)


def _group_hits(keywords, corpus):
    """AND 组匹配：所有关键词元素命中同一条目（元素内备选 OR，大小写不敏感）。"""
    return [(src, txt) for src, txt in corpus
            if _group_match(keywords, txt.lower())]


def _implant_hits(keywords, corpus):
    """OR 同义词匹配 + 否定前缀护栏（implanted_false 专用）。"""
    alts = []
    for kw in keywords:
        if isinstance(kw, str):
            alts.append(kw)
        else:
            alts.extend(str(a) for a in kw)
    out = []
    for k_orig in alts:
        k = k_orig.lower()
        for src, txt in corpus:
            t = txt.lower()
            idx = t.find(k)
            while idx != -1:
                if idx == 0 or t[idx - 1] not in _NEG_PREFIX:
                    out.append((src, txt, k_orig))
                    break
                idx = t.find(k, idx + 1)
    return out


# 修正语气标记：旧话题条目若含这些词，说明 LLM 已把它改写成“旧状态已结束”
# （如「原本养的猫团团已送回老家」）——是合法的状态更新，不算旧值残留。
# 原始话语不含这些词，纯陈旧条目几乎不会带上它们（p1 冒烟实测校准）。
_CORRECTION_MARKERS = (
    "原本", "以前", "之前", "此前", "不再", "已经不", "已送", "送走", "送人",
    "结束", "停止", "打破", "退役", "搬到", "换成", "改为", "改用", "早搬", "早毕业",
)


def _classify_replacement(old_kws, new_kws, corpus):
    """evolved / single_value 共用的替换判定。

    条目分四类：纯旧（含旧组不含新组）/ 纯新 / 融合（两组都含，如
    「本命从初音换成崩铁」——视为新状态在场）/ 改写旧（纯旧但带修正语气
    标记，如「原本养的猫已送回老家」——合法更新，不算残留）。
    LLM 也可能把演化拆成两条（改写旧条目 + 新建新条目），同样不算矛盾。
    """
    old_only, new_only, merged, updated_old = [], [], [], []
    for src, txt in corpus:
        t = txt.lower()
        a = _group_match(old_kws, t)
        b = _group_match(new_kws, t)
        if a and b:
            merged.append((src, txt))
        elif a:
            if any(m in t for m in _CORRECTION_MARKERS):
                updated_old.append((src, txt))
            else:
                old_only.append((src, txt))
        elif b:
            new_only.append((src, txt))
    new_present = bool(new_only or merged)
    if new_present and not old_only:
        status = "MERGED" if merged and not new_only else "EVOLVED"
        passed = True
    elif old_only and new_present:
        status, passed = "CONTRADICTED", False
    elif old_only:
        status, passed = "STALE", False
    else:
        status, passed = "LOST", False
    # 失败时优先展示肇事条目（残留的纯旧值）；通过时展示新状态证据
    if passed:
        evidence = (new_only or merged or updated_old or old_only)[:2]
    else:
        evidence = (old_only + updated_old + merged + new_only)[:2]
    return status, passed, evidence


def _evidence_str(hits, limit=2, width=48):
    parts = []
    for h in hits[:limit]:
        src, txt = h[0], h[1]
        txt = txt.replace("\n", " ")
        if len(txt) > width:
            txt = txt[:width] + "…"
        parts.append(f"[{src}] {txt}")
    return " | ".join(parts)


def score_persona(persona, tm, profile, user_id):
    """逐 fact 打分 → {fid: result dict}。"""
    corpus = _collect_corpus(tm, user_id, profile)
    results = {}
    for f in persona["facts"]:
        fid, cat, expect = f["fid"], f["category"], f["expect"]
        kws = f.get("keywords") or []
        r = {"category": cat, "expect": expect, "fid": fid}

        if cat == "implanted_false":
            hits = _implant_hits(kws, corpus)
            r["passed"] = not hits
            r["status"] = "ABSENT" if not hits else "IMPLANTED"
            r["canary"] = False
            r["evidence"] = _evidence_str([(s, t) for s, t, _ in hits])
        elif expect in ("evolved", "single_value"):
            sub = f.get("evolves") or f.get("contradicts") or {}
            status, passed, ev = _classify_replacement(
                kws, sub.get("keywords") or [], corpus)
            r.update(passed=passed, status=status, canary=False,
                     evidence=_evidence_str(ev))
        else:  # in_store / absent / canary：AND 组匹配
            hits = _group_hits(kws, corpus)
            found = bool(hits)
            if expect == "canary":
                r["canary"] = True
                r["status"] = "CANARY-OK" if found else "CANARY-FAIL"
                r["passed"] = None  # 不计分
                r["evidence"] = _evidence_str(hits)
            elif expect == "in_store":
                r.update(passed=found, canary=False,
                         status="FOUND" if found else "MISSING",
                         evidence=_evidence_str(hits))
            else:  # absent
                r.update(passed=not found, canary=False,
                         status="ABSENT" if not found else "LEAKED",
                         evidence=_evidence_str(hits))
        results[fid] = r
    return results


def _store_stats(tm, user_id, profile):
    st = tm._get_state(user_id)
    return {
        "short": len(st.short), "medium": len(st.medium), "long": len(st.long),
        "extraction_count": st.extraction_count,
        "interests": len(profile.interests),
        "preferences": len(profile.preferences or {}),
    }


# ── 单 persona 运行 ──────────────────────────────────────────────────

async def run_persona(persona, fillers, client, args):
    pid = persona["id"]
    user_id = f"bench_{pid}"
    tmp = tempfile.mkdtemp(prefix=f"membench_l2_{pid}_")
    script, event_at, total_turns = build_script(persona, fillers)
    fact_turns = sum(1 for _, _, ph in event_at.values() if ph == "pre")
    print(f"\n=== {pid} === {total_turns} 轮 "
          f"(事实事件 {len(event_at)}，pre {fact_turns})，临时目录 {tmp}")

    pm = ProfileManager(base_dir=os.path.join(tmp, "user_data"))
    pm.set_client(client)
    tm = TieredMemory(base_dir=os.path.join(tmp, "memory"))
    pm.set_memory(tm)

    usage_before = dict(client.bench_usage)
    t0 = time.time()

    async def drain():
        while user_id in pm._inflight:
            await asyncio.sleep(DRAIN_POLL)

    for i, (msg, reply) in enumerate(script, 1):
        pm.observe_turn(user_id, msg, reply)
        await drain()
        if args.progress and i % 20 == 0:
            print(f"  … {i}/{total_turns} 轮，抽取批次 {client.bench_usage['calls'] - usage_before['calls']}")
    pm.flush_on_session_end(user_id, total_turns)
    await drain()
    tm.save_user(user_id)
    elapsed = time.time() - t0

    # 以“重启后”的视角评分：新实例 load_all，顺带验证持久化
    tm2 = TieredMemory(base_dir=os.path.join(tmp, "memory"))
    tm2.load_all()
    profile = pm.get(user_id)

    results = score_persona(persona, tm2, profile, user_id)
    usage = {k: client.bench_usage[k] - usage_before.get(k, 0)
             for k in client.bench_usage}

    if not args.keep_tmp:
        shutil.rmtree(tmp, ignore_errors=True)

    n_scored = sum(1 for r in results.values() if r["passed"] is not None)
    n_pass = sum(1 for r in results.values() if r["passed"] is True)
    n_can_ok = sum(1 for r in results.values() if r.get("status") == "CANARY-OK")
    n_can = sum(1 for r in results.values() if r["canary"])
    print(f"  完成：{elapsed:.0f}s，批次 {usage['calls']}，"
          f"token {usage['input_tokens']}+{usage['output_tokens']}"
          f"(cache {usage['cached_tokens']})，API错误 {usage['api_errors']}")
    print(f"  计分 {n_pass}/{n_scored} 通过，canary {n_can_ok}/{n_can}，"
          f"存储 {_store_stats(tm2, user_id, profile)}")
    if usage["api_errors"]:
        print(f"  ⚠ 有 {usage['api_errors']} 次 API 错误，该 persona 结果可信度下降")

    return {
        "results": results,
        "store_stats": _store_stats(tm2, user_id, profile),
        "usage": usage,
        "turns": total_turns,
        "elapsed_s": round(elapsed, 1),
        "api_errors": usage["api_errors"],
    }


# ── 报告 ─────────────────────────────────────────────────────────────

def _category_stats(results):
    stats = {}
    for r in results.values():
        cat = r["category"]
        s = stats.setdefault(cat, {"pass": 0, "fail": 0, "total": 0,
                                   "canary_ok": 0, "canary_total": 0})
        if r["canary"]:
            s["canary_total"] += 1
            if r["status"] == "CANARY-OK":
                s["canary_ok"] += 1
        else:
            s["total"] += 1
            s["pass" if r["passed"] else "fail"] += 1
    return stats


def persona_markdown(pid, run, persona):
    lines = [f"### {pid}", "",
             "| 类别 | fid | expect | 状态 | 证据 |", "|---|---|---|---|---|"]
    order = {c: i for i, c in enumerate(CATEGORY_ORDER)}
    facts = sorted(persona["facts"],
                   key=lambda f: (order.get(f["category"], 99), f["fid"]))
    for f in facts:
        r = run["results"][f["fid"]]
        mark = {"PASS": "✅", "FAIL": "❌"}.get(
            "PASS" if r["passed"] else "FAIL", "·") if r["passed"] is not None else "🐤"
        lines.append(f"| {f['category']} | {f['fid']} | {r['expect']} "
                     f"| {mark} {r['status']} | {r.get('evidence', '')} |")
    st = run["store_stats"]
    lines += ["",
              f"存储: SHORT {st['short']} / MEDIUM {st['medium']} / LONG {st['long']}"
              f"（时钟 {st['extraction_count']}），画像槽位: 兴趣 {st['interests']}"
              f" 偏好 {st['preferences']}",
              f"开销: {run['usage']['calls']} 批次，"
              f"{run['usage']['input_tokens']} in / {run['usage']['output_tokens']} out"
              f"（cached {run['usage']['cached_tokens']}），"
              f"{run['elapsed_s']}s，API错误 {run['api_errors']}", ""]
    return "\n".join(lines)


def summary_table(all_runs, personas_by_id):
    lines = ["| persona | 计分通过 | 通过率 | canary | 泄漏(junk/rd/植入) | 批次 | in+out token |",
             "|---|---|---|---|---|---|---|"]
    tot_pass = tot_all = tot_cok = tot_can = tot_leak = 0
    for pid, run in all_runs.items():
        cs = _category_stats(run["results"])
        p = sum(v["pass"] for v in cs.values())
        t = sum(v["total"] for v in cs.values())
        cok = sum(v["canary_ok"] for v in cs.values())
        can = sum(v["canary_total"] for v in cs.values())
        leak = sum(1 for r in run["results"].values()
                   if r["expect"] == "absent" and r["passed"] is False)
        u = run["usage"]
        lines.append(f"| {pid} | {p}/{t} | {p / t * 100 if t else 0:.0f}% "
                     f"| {cok}/{can} | {leak} | {u['calls']} "
                     f"| {u['input_tokens'] + u['output_tokens']} |")
        tot_pass += p; tot_all += t; tot_cok += cok; tot_can += can; tot_leak += leak
    lines.append(f"| **合计** | **{tot_pass}/{tot_all}** "
                 f"| **{tot_pass / tot_all * 100 if tot_all else 0:.0f}%** "
                 f"| **{tot_cok}/{tot_can}** | **{tot_leak}** | | |")
    return "\n".join(lines), {"pass": tot_pass, "total": tot_all,
                              "canary_ok": tot_cok, "canary_total": tot_can,
                              "leaks": tot_leak}


def build_summary(all_runs, meta):
    personas_out = {}
    for pid, run in all_runs.items():
        personas_out[pid] = {
            "results": {fid: {k: v for k, v in r.items()}
                        for fid, r in sorted(run["results"].items())},
            "category_stats": _category_stats(run["results"]),
            "store_stats": run["store_stats"],
            "usage": run["usage"],
            "turns": run["turns"],
            "api_errors": run["api_errors"],
        }
    _, totals = summary_table(all_runs, None)
    return {"meta": meta, "totals": totals, "personas": personas_out}


def compare_markdown(base, cur):
    """基线 vs 当前：逐 fact 状态对照，标注 REGRESSION / IMPROVED / CHANGED。"""
    rank = {True: 2, None: 1, False: 0}
    lines = ["| persona | fid | 类别 | 基线 | 当前 | 判定 |", "|---|---|---|---|---|---|"]
    n_reg = n_imp = n_chg = 0
    bp, bpids = base.get("personas", {}), cur.get("personas", {})
    for pid in sorted(set(bp) | set(bpids)):
        b_facts = bp.get(pid, {}).get("results", {})
        c_facts = bpids.get(pid, {}).get("results", {})
        for fid in sorted(set(b_facts) | set(c_facts)):
            b, c = b_facts.get(fid), c_facts.get(fid)
            if b is None or c is None:
                verdict = "NEW" if b is None else "DROPPED"
                bs = b["status"] if b else "—"
                cs = c["status"] if c else "—"
                n_chg += 1
            else:
                bs, cs = b["status"], c["status"]
                rb, rc = rank.get(b["passed"], 0), rank.get(c["passed"], 0)
                if rc < rb:
                    verdict, n_reg = f"**REGRESSION**", n_reg + 1
                elif rc > rb:
                    verdict, n_imp = "IMPROVED", n_imp + 1
                elif bs != cs:
                    verdict, n_chg = "changed", n_chg + 1
                else:
                    continue
            cat = (c or b)["category"]
            lines.append(f"| {pid} | {fid} | {cat} | {bs} | {cs} | {verdict} |")
    bt, ct = base.get("totals", {}), cur.get("totals", {})
    head = (f"## 基线对照\n\n基线: {base.get('meta', {}).get('generated_at', '?')}"
            f"  当前: {cur.get('meta', {}).get('generated_at', '?')}\n\n"
            f"总计: 通过率 {bt.get('pass')}/{bt.get('total')} → "
            f"{ct.get('pass')}/{ct.get('total')}，canary "
            f"{bt.get('canary_ok')}/{bt.get('canary_total')} → "
            f"{ct.get('canary_ok')}/{ct.get('canary_total')}，泄漏 "
            f"{bt.get('leaks')} → {ct.get('leaks')}\n\n"
            f"**REGRESSION {n_reg} / IMPROVED {n_imp} / changed {n_chg}**\n")
    if n_reg == n_imp == n_chg == 0:
        return head + "\n（逐 fact 状态完全一致）\n"
    return head + "\n" + "\n".join(lines) + "\n"


# ── 主入口 ───────────────────────────────────────────────────────────

def _git_head():
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(QQBOT_DIR), stderr=subprocess.DEVNULL).decode().strip()
    except Exception:
        return "unknown"


def load_personas(only=None):
    out = []
    for p in sorted(PERSONAS_DIR.glob("p*.json")):
        d = json.loads(p.read_text(encoding="utf-8"))
        if only and d["id"] not in only and p.stem not in only:
            continue
        out.append(d)
    return out


async def amain(args):
    api_key = args.api_key or os.getenv("BENCH_API_KEY")
    if not api_key:
        print(__doc__.split("用法")[0])
        print("─" * 60)
        print("未设置 BENCH_API_KEY — L2 需要真实 LLM 调用（会产生费用），跳过。")
        print("运行方式：")
        print("  BENCH_API_KEY=sk-xxx python benchmarks/memory/bench_pipeline.py")
        print("  可选：BENCH_API_BASE / BENCH_MODEL（默认 deepseek-chat）")
        return 0

    personas = load_personas(args.persona)
    if not personas:
        print(f"没有匹配的 persona: {args.persona}")
        return 1
    fillers = json.loads((PERSONAS_DIR / "fillers.json").read_text(encoding="utf-8"))

    client = BenchClient(
        api_key=api_key,
        api_base=args.api_base or os.getenv("BENCH_API_BASE",
                                            "https://api.deepseek.com/v1"),
        model=args.model or os.getenv("BENCH_MODEL", "deepseek-chat"),
    )
    print(f"L2 管线 benchmark：{len(personas)} 个 persona，model={client.model}")

    all_runs = {}
    for persona in personas:
        all_runs[persona["id"]] = await run_persona(persona, fillers, client, args)

    meta = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "engine_commit": _git_head(),
        "model": client.model,
        "api_base": client.api_base,
        "note": "L2 端到端（真实 LLM），打分确定性；LLM 抽取本身非确定，"
                "同一引擎多次运行结果可有小幅波动。",
    }
    summary = build_summary(all_runs, meta)

    print("\n" + "=" * 64)
    table, totals = summary_table(all_runs, None)
    print(table)
    # 基线 JSON 先落盘（关键产物），报告 md 其次；两者都自动建父目录
    if args.save_baseline:
        path = Path(args.save_baseline)
        if path.is_dir() or str(path).endswith("/"):
            path = path / "l2_baseline.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                        encoding="utf-8")
        print(f"\n基线已保存 {path}")

    if args.report:
        report = ["# L2 管线 benchmark 报告", "",
                  f"生成: {meta['generated_at']}  engine@{meta['engine_commit']}"
                  f"  model={meta['model']}", ""]
        for pid, run in all_runs.items():
            report.append(persona_markdown(
                pid, run, next(p for p in personas if p["id"] == pid)))
        report.append("## 汇总\n")
        report.append(table)
        rp = Path(args.report)
        rp.parent.mkdir(parents=True, exist_ok=True)
        rp.write_text("\n".join(report), encoding="utf-8")
        print(f"报告已写入 {rp}")

    if args.compare:
        base = json.loads(Path(args.compare).read_text(encoding="utf-8"))
        print()
        print(compare_markdown(base, summary))

    hard_fail = any(r["passed"] is False
                    for run in all_runs.values() for r in run["results"].values())
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    if args.strict and hard_fail:
        return 1
    return 0


def main():
    ap = argparse.ArgumentParser(description="L2 记忆管线端到端 benchmark")
    ap.add_argument("--persona", action="append",
                    help="只跑指定 persona（id 或文件名，可多次）")
    ap.add_argument("--api-key", help="覆盖 BENCH_API_KEY")
    ap.add_argument("--api-base", help="覆盖 BENCH_API_BASE")
    ap.add_argument("--model", help="覆盖 BENCH_MODEL")
    ap.add_argument("--save-baseline", metavar="PATH",
                    help="保存 JSON 基线（默认 baselines/l2_baseline.json 需给目录）")
    ap.add_argument("--compare", metavar="PATH", help="与已存基线对照")
    ap.add_argument("--report", metavar="PATH", help="输出 markdown 报告文件")
    ap.add_argument("--json", action="store_true", help="stdout 打印完整 JSON")
    ap.add_argument("--progress", action="store_true", help="每 20 轮打印进度")
    ap.add_argument("--keep-tmp", action="store_true", help="保留临时数据目录")
    ap.add_argument("--strict", action="store_true", help="有硬 FAIL 时退出码 1")
    args = ap.parse_args()
    sys.exit(asyncio.run(amain(args)))


if __name__ == "__main__":
    main()

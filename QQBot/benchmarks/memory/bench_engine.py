#!/usr/bin/env python3
"""L1 引擎级记忆 benchmark（Memory-Optimization-Plan §3.1，批次 0）。

与 test_agent.py:TestTieredMemory 的区别：那是单点机制断言，这是**长时间序列
的场景流**——直接驱动 TieredMemory.ingest_candidates()，用 JSON 场景文件喂
脚本化 candidate 流 + judge 判定 + 检查点断言，回答三个架构问题：

  Q1 低频关键事实的存活曲线形状（survival_curve 场景，track 指标）
  Q2 一次性事件的存活率（episodic_canary，预期 CANARY-FAIL，⑤ 的回归门）
  Q3 注入块逐轮字节搅动率（injection_stability，probe/churn 指标，⑥ 的基线）

场景 kind 语义：
  gate    检查点失败 → 退出码 1（回归门，如 LONG 可达性=① 的门）
  canary  检查点表达"期望的正确行为"；失败 → CANARY-FAIL，如实记录，
          不影响退出码（当前引擎的已知缺陷，如一次性事件必然消亡）
  metric  检查点同 gate（硬性不变量，如 600 字 cap），churn 等数字只记录

零依赖（stdlib only），确定性（无 LLM、无网络）；agent.memory 纯 stdlib，
可在任意 git worktree 中对旧引擎复跑（缺失的新 API 会以 api_missing 如实
FAIL——这正是"优化前对照基线"的预期行为，见 Memory-Optimization-Plan §3.5）。

用法：
  python benchmarks/memory/bench_engine.py                     # 全部场景 + markdown 报告
  python benchmarks/memory/bench_engine.py --scenario survival # 名称过滤
  python benchmarks/memory/bench_engine.py --save-baseline baselines/l1_current.json
  python benchmarks/memory/bench_engine.py --compare baselines/l1_preopt.json
  python benchmarks/memory/bench_engine.py --json /tmp/out.json
"""

import argparse
import ast
import difflib
import json
import operator
import os
import re
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
QQBOT_DIR = os.path.abspath(os.path.join(HERE, "..", ".."))
if QQBOT_DIR not in sys.path:
    sys.path.insert(0, QQBOT_DIR)

import agent.memory as M  # noqa: E402  (pure stdlib module)

SCENARIO_DIR = os.path.join(HERE, "scenarios")

# tier 解析优先级：注入活跃层在前——twin 场景（MEDIUM+LONG 并存）报 medium，
# 降级后（SHORT+LONG）报 short，只剩 LONG 副本时报 long（① 场景的终态）。
_TIER_ORDER = ("medium", "short", "long")


# ── 安全表达式求值（loop 派生变量，如 {(n-1)%20+1}）──────────────────

_ARITH_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod,
}


def _safe_expr(expr: str, env: dict) -> int:
    """Evaluate an int arithmetic expression with loop vars — no eval()."""
    node = ast.parse(expr.strip(), mode="eval").body

    def ev(n):
        if isinstance(n, ast.Constant) and isinstance(n.value, int):
            return n.value
        if isinstance(n, ast.Name):
            if n.id in env:
                return env[n.id]
            raise ValueError(f"unknown name {n.id!r} in expr {expr!r}")
        if isinstance(n, ast.BinOp) and type(n.op) in _ARITH_OPS:
            return _ARITH_OPS[type(n.op)](ev(n.left), ev(n.right))
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, (ast.USub, ast.UAdd)):
            v = ev(n.operand)
            return -v if isinstance(n.op, ast.USub) else v
        raise ValueError(f"unsupported expr node in {expr!r}")

    return ev(node)


def _subst(obj, env: dict):
    """Recursively substitute {expr} tokens in strings using env (loop vars)."""
    if isinstance(obj, str):
        tokens = list(re.finditer(r"\{([^{}]+)\}", obj))
        if not tokens:
            return obj
        out, i = [], 0
        for tok in tokens:
            inner = tok.group(1).strip()
            value = int(inner) if inner.isdigit() else _safe_expr(inner, env)
            out.append(obj[i:tok.start()])
            out.append(str(value))
            i = tok.end()
        out.append(obj[i:])
        s = "".join(out)
        # whole-string single numeric token → keep it an int
        if len(tokens) == 1 and obj.startswith("{") and obj.endswith("}"):
            try:
                return int(s)
            except ValueError:
                pass
        return s
    if isinstance(obj, list):
        return [_subst(x, env) for x in obj]
    if isinstance(obj, dict):
        return {k: _subst(v, env) for k, v in obj.items()}
    return obj


# ── 引擎探针（容忍旧引擎缺失的新 API → api_missing 如实 FAIL）────────

def _tier_of(tm, user: str, sub: str) -> str:
    st = tm._get_state(user)
    loc = {
        "short": any(sub in s.content for s in st.short),
        "medium": any(sub in m.content for m in st.medium),
        "long": any(sub in lo.summary or sub in lo.title for lo in st.long),
    }
    for tier in _TIER_ORDER:
        if loc[tier]:
            return tier
    return "gone"


def _long_index(tm, user: str):
    fn = getattr(tm, "build_long_index", None)
    return fn(user) if callable(fn) else None


def _recall(tm, user: str, query: str):
    fn = getattr(tm, "recall_long", None)
    return fn(user, query) if callable(fn) else None


def _injection_body_stats(block: str):
    """(n_lines, body_chars) of the '- ' bullets in an injection block."""
    bullets = [ln for ln in block.splitlines() if ln.startswith("- ")]
    return len(bullets), sum(len(ln) for ln in bullets)


# ── 场景执行 ────────────────────────────────────────────────────────

class ScenarioRunner:
    def __init__(self, scenario: dict, tmp_root: str):
        self.sc = scenario
        self.user = scenario.get("user", "bench_u")
        self.tmp = tempfile.mkdtemp(dir=tmp_root)
        self.tm = M.TieredMemory(base_dir=self.tmp)
        self.cycle = 0                      # 1 cycle = 1 extraction-batch equivalent
        self.tracked = {}                   # substring → {"transitions": [...], "last": tier}
        self.probes = []                    # [{"cycle", "injection", "long_index"}]
        self.checkpoints = []               # [{"desc", "ok", "reason"}]

    # ── step dispatch ──

    def exec_steps(self, steps, env):
        for raw in steps:
            if isinstance(raw, dict) and "loop" in raw:
                # substitute only the loop header — body tokens ({n}) resolve
                # per-iteration inside the recursive call
                lp_raw = raw["loop"]
                lp = _subst({k: v for k, v in lp_raw.items() if k != "body"}, env)
                var = lp.get("var", "n")
                for i in range(int(lp["from"]), int(lp["to"]) + 1):
                    self.exec_steps(lp_raw["body"], dict(env, **{var: i}))
                continue
            step = _subst(raw, env)
            if "ingest" in step:
                self._op_ingest(step)
            elif "tick" in step:
                self._op_tick(step)
            elif "checkpoint" in step:
                self._op_checkpoint(step["checkpoint"])
            elif "track" in step:
                self._op_track(step["track"])
            elif step.get("probe"):
                self._op_probe()
            elif step.get("save"):
                self.tm.save_user(self.user)
            elif step.get("reload"):
                self._op_reload()
            else:
                raise ValueError(f"unknown step op: {step!r}")

    def _op_ingest(self, step):
        repeat = max(1, int(step.get("repeat", 1)))
        tick = int(step.get("tick", 1))
        turns = [tuple(t) for t in step.get("snapshot_turns") or []]
        cands = [tuple(c) for c in step["ingest"]]
        for _ in range(repeat):
            self.tm.ingest_candidates(self.user, cands,
                                      snapshot_turns=turns or None)
            for _t in range(tick):
                self.tm.touch_extraction_count(self.user)
            self.cycle += max(1, tick)
            self._sample_tracks()

    def _op_tick(self, step):
        n = int(step["tick"])
        for _ in range(n):
            self.tm.touch_extraction_count(self.user)
        self.cycle += n
        self._sample_tracks()

    def _op_reload(self):
        self.tm.save_user(self.user)
        tm2 = M.TieredMemory(base_dir=self.tmp)
        tm2.load_all()
        self.tm = tm2

    def _op_track(self, sub: str):
        if sub not in self.tracked:
            self.tracked[sub] = {
                "since_cycle": self.cycle,
                "transitions": [{"cycle": self.cycle,
                                 "tier": _tier_of(self.tm, self.user, sub)}],
            }

    def _sample_tracks(self):
        for sub, tr in self.tracked.items():
            tier = _tier_of(self.tm, self.user, sub)
            if tier != tr["transitions"][-1]["tier"]:
                tr["transitions"].append({"cycle": self.cycle, "tier": tier})

    def _op_probe(self):
        self.probes.append({
            "cycle": self.cycle,
            "injection": self.tm.build_medium_injection(self.user),
            "long_index": _long_index(self.tm, self.user) or "",
        })

    # ── checkpoints ──

    def _op_checkpoint(self, cp: dict):
        desc = cp.get("desc", "")
        checks = {k: v for k, v in cp.items() if k != "desc"}
        for key, spec in checks.items():
            ok, reason = self._eval_one(key, spec)
            self.checkpoints.append({
                "desc": desc or key, "assert": key, "ok": ok, "reason": reason,
            })

    def _eval_one(self, key: str, spec):
        tm, user = self.tm, self.user
        st = tm._get_state(user)
        try:
            if key == "tier_of":
                got = _tier_of(tm, user, spec["find"])
                want = spec.get("equals") or spec.get("in")
                want_list = want if isinstance(want, list) else [want]
                return got in want_list, f"tier_of({spec['find']!r})={got}, want {want_list}"
            if key in ("short_count", "medium_count", "long_count"):
                got = len(getattr(st, key.split("_")[0]))
                return self._cmp_num(got, spec), f"{key}={got}, want {spec}"
            if key == "injection_contains":
                block = tm.build_medium_injection(user)
                return spec in block, f"injection missing {spec!r}" if spec not in block else ""
            if key == "injection_not_contains":
                block = tm.build_medium_injection(user)
                return spec not in block, f"injection unexpectedly contains {spec!r}"
            if key == "injection_shape":
                block = tm.build_medium_injection(user)
                n, body = _injection_body_stats(block)
                ok = (n <= int(spec.get("max_lines", 15))
                      and body <= int(spec.get("max_body_chars", M.MEDIUM_INJECT_TOKEN_CAP)))
                return ok, f"injection shape lines={n} body_chars={body}, spec {spec}"
            if key == "injection_same_as_probe":
                if not self.probes:
                    return False, "no probe recorded"
                cur = tm.build_medium_injection(user)
                return cur == self.probes[-1]["injection"], "injection bytes drifted across reload"
            if key == "long_index_contains":
                idx = _long_index(tm, user)
                if idx is None:
                    return False, "api_missing:build_long_index"
                return spec in idx, f"long_index missing {spec!r}" if spec not in idx else ""
            if key == "long_index_same_as_probe":
                idx = _long_index(tm, user)
                if idx is None:
                    return False, "api_missing:build_long_index"
                if not self.probes:
                    return False, "no probe recorded"
                return idx == self.probes[-1]["long_index"], "long_index bytes drifted across reload"
            if key == "recall":
                hits = _recall(tm, user, spec["query"])
                if hits is None:
                    return False, "api_missing:recall_long"
                if "expect_hits" in spec and len(hits) != int(spec["expect_hits"]):
                    return False, f"recall({spec['query']!r}) hits={len(hits)}, want {spec['expect_hits']}"
                if "contains" in spec:
                    blob = " ".join(f"{lo.title} {lo.summary} {snap}" for lo, snap in hits)
                    if spec["contains"] not in blob:
                        return False, f"recall({spec['query']!r}) result missing {spec['contains']!r}"
                return True, ""
            raise ValueError(f"unknown checkpoint assert {key!r}")
        except Exception as e:                      # engine bug → honest FAIL
            return False, f"exception: {type(e).__name__}: {e}"

    @staticmethod
    def _cmp_num(got: int, spec) -> bool:
        if isinstance(spec, dict):
            if "gte" in spec and got < spec["gte"]:
                return False
            if "lte" in spec and got > spec["lte"]:
                return False
            if "equals" in spec and got != spec["equals"]:
                return False
            return True
        return got == int(spec)

    # ── metrics ──

    def metrics(self) -> dict:
        out = {}
        if self.tracked:
            tracked = {}
            for sub, tr in self.tracked.items():
                trans = tr["transitions"]
                final = trans[-1]["tier"]

                def first_at(tier, after_cycle=0):
                    for t in trans:
                        if t["tier"] == tier and t["cycle"] > after_cycle:
                            return t["cycle"]
                    return None

                promoted = first_at("medium")
                demoted = first_at("short", promoted if promoted is not None else 0)
                gone = first_at("gone", tr["since_cycle"])
                tracked[sub] = {
                    # 防御：track 子串从未在任何 tier 出现过 → 数据是哑弹
                    # （典型原因：track 子串与 ingest 内容不一致）
                    "never_observed": len(trans) == 1 and trans[0]["tier"] == "gone",
                    "transitions": trans,
                    "final_tier": final,
                    "promoted_at_cycle": promoted,
                    "demoted_at_cycle": demoted,
                    "gone_at_cycle": gone,
                    # 可达时长 = 进入任一 tier 到彻底不可达（gone）；未 gone 则至今
                    "survived_cycles": (gone if gone is not None else self.cycle)
                                       - tr["since_cycle"],
                }
            out["tracked"] = tracked
        if len(self.probes) >= 2:
            ratios, changed = [], 0
            for a, b in zip(self.probes, self.probes[1:]):
                blob_a = a["injection"] + "\n" + a["long_index"]
                blob_b = b["injection"] + "\n" + b["long_index"]
                r = difflib.SequenceMatcher(None, blob_a, blob_b).ratio()
                ratios.append(1.0 - r)
                if blob_a != blob_b:
                    changed += 1
            out["churn"] = {
                "probes": len(self.probes),
                "changed_pairs": changed,
                "any_change_rate": round(changed / len(ratios), 4),
                "mean_byte_churn": round(sum(ratios) / len(ratios), 4),
                "max_byte_churn": round(max(ratios), 4),
            }
            out["final_injection_bytes"] = len(self.probes[-1]["injection"])
            out["final_long_index_bytes"] = len(self.probes[-1]["long_index"])
        out["total_cycles"] = self.cycle
        return out

    def run(self) -> dict:
        kind = self.sc.get("kind", "gate")
        self.exec_steps(self.sc["steps"], {})
        cps = self.checkpoints
        all_ok = all(c["ok"] for c in cps)
        if kind == "canary":
            status = "CANARY_OK" if all_ok else "CANARY_FAIL"
        else:
            status = "PASS" if all_ok else "FAIL"
        return {
            "id": self.sc.get("id", os.path.basename(str(self.sc))),
            "title": self.sc.get("title", ""),
            "kind": kind,
            "status": status,
            "checkpoints": cps,
            "metrics": self.metrics(),
        }


# ── 报告 / 基线 ─────────────────────────────────────────────────────

def _engine_commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=QQBOT_DIR, capture_output=True, text=True, timeout=5,
        ).stdout.strip() or "unknown"
    except Exception:
        return "unknown"


_STATUS_MARK = {
    "PASS": "✓ PASS", "FAIL": "✗ FAIL",
    "CANARY_OK": "◉ CANARY-OK", "CANARY_FAIL": "◔ CANARY-FAIL",
}


def to_markdown(results: list, meta: dict) -> str:
    lines = [
        f"# L1 记忆引擎 benchmark（engine@{meta.get('engine_commit', '?')}，"
        f"{meta.get('ts', '')}）", "",
        "| 场景 | 类型 | 状态 | 检查点 | 关键指标 |",
        "|---|---|---|---|---|",
    ]
    for r in results:
        cps = r["checkpoints"]
        cp_s = f"{sum(1 for c in cps if c['ok'])}/{len(cps)}"
        m = r["metrics"]
        bits = []
        if "churn" in m:
            bits.append(f"churn={m['churn']['mean_byte_churn']:.3f}"
                        f"(any={m['churn']['any_change_rate']:.2f})")
        for sub, tr in (m.get("tracked") or {}).items():
            warn = "⚠never_observed " if tr.get("never_observed") else ""
            bits.append(f"{warn}「{sub[:8]}」终态={tr['final_tier']}"
                        f" 存活={tr['survived_cycles']}c")
        bits.append(f"cycles={m.get('total_cycles')}")
        lines.append(f"| {r['id']} | {r['kind']} | "
                     f"{_STATUS_MARK.get(r['status'], r['status'])} | {cp_s} | "
                     f"{'; '.join(bits)} |")
    lines.append("")
    for r in results:
        bad = [c for c in r["checkpoints"] if not c["ok"]]
        if bad:
            lines.append(f"## {r['id']} — 未通过的检查点")
            for c in bad:
                lines.append(f"- [{c['assert']}] {c['desc']}: {c['reason']}")
            lines.append("")
    return "\n".join(lines)


def save_baseline(results: list, meta: dict, path: str):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "results": results}, f,
                  ensure_ascii=False, indent=1)
    print(f"[bench] baseline saved → {path}")


def compare_markdown(results: list, baseline: dict) -> str:
    base = {r["id"]: r for r in baseline.get("results", [])}
    lines = [f"# L1 对比（baseline: engine@{baseline.get('meta', {}).get('engine_commit', '?')}）",
             "", "| 场景 | 基线 | 当前 | 判定 | 指标变化 |", "|---|---|---|---|---|"]
    regressions = []
    for r in results:
        b = base.get(r["id"])
        if b is None:
            lines.append(f"| {r['id']} | — | {r['status']} | NEW | |")
            continue
        verdict = "same"
        rank = {"FAIL": 0, "CANARY_FAIL": 1, "CANARY_OK": 2, "PASS": 2}
        if rank.get(r["status"], 0) < rank.get(b["status"], 0):
            verdict = "**REGRESSION**"
            regressions.append(r["id"])
        elif rank.get(r["status"], 0) > rank.get(b["status"], 0):
            verdict = "IMPROVED"
        deltas = []
        bc, rc = b["metrics"].get("churn") or {}, r["metrics"].get("churn") or {}
        if bc and rc:
            deltas.append(f"churn {bc.get('mean_byte_churn')}→{rc.get('mean_byte_churn')}")
        for sub, tr in (r["metrics"].get("tracked") or {}).items():
            btr = (b["metrics"].get("tracked") or {}).get(sub)
            if btr:
                deltas.append(f"「{sub[:6]}」存活 {btr.get('survived_cycles')}→{tr.get('survived_cycles')}c")
        lines.append(f"| {r['id']} | {b['status']} | {r['status']} | {verdict} | "
                     f"{'; '.join(deltas)} |")
    lines.append("")
    lines.append(f"回归数：{len(regressions)}" + (f"（{', '.join(regressions)}）" if regressions else ""))
    return "\n".join(lines)


# ── main ────────────────────────────────────────────────────────────

def run_all(name_filter: str = "") -> tuple:
    meta = {
        "engine_commit": _engine_commit(),
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "python": sys.version.split()[0],
        "schema_version": M.TIER_SCHEMA_VERSION,
    }
    files = sorted(f for f in os.listdir(SCENARIO_DIR) if f.endswith(".json"))
    results = []
    tmp_root = tempfile.mkdtemp(prefix="membench_")
    try:
        for fn in files:
            with open(os.path.join(SCENARIO_DIR, fn), encoding="utf-8") as f:
                sc = json.load(f)
            if name_filter and name_filter not in sc.get("id", fn):
                continue
            results.append(ScenarioRunner(sc, tmp_root).run())
    finally:
        import shutil
        shutil.rmtree(tmp_root, ignore_errors=True)
    return results, meta


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="L1 memory-engine benchmark")
    ap.add_argument("--scenario", default="", help="只跑 id 含该子串的场景")
    ap.add_argument("--save-baseline", metavar="PATH", help="结果快照存为基线 JSON")
    ap.add_argument("--compare", metavar="PATH", help="与基线 JSON 对比，输出回归表")
    ap.add_argument("--json", metavar="PATH", help="完整结果 JSON 另存")
    args = ap.parse_args(argv)

    results, meta = run_all(args.scenario)
    if not results:
        print("[bench] 没有匹配的场景")
        return 2

    print(to_markdown(results, meta))

    if args.compare:
        with open(args.compare, encoding="utf-8") as f:
            print(compare_markdown(results, json.load(f)))
    if args.save_baseline:
        save_baseline(results, meta, args.save_baseline)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({"meta": meta, "results": results}, f, ensure_ascii=False, indent=1)

    hard_fail = [r for r in results if r["status"] == "FAIL"]
    canary_fail = [r for r in results if r["status"] == "CANARY_FAIL"]
    if canary_fail:
        print(f"[bench] {len(canary_fail)} 个 canary 如实记录失败（不算回归）: "
              + ", ".join(r["id"] for r in canary_fail))
    if hard_fail:
        print(f"[bench] {len(hard_fail)} 个 gate 场景失败: "
              + ", ".join(r["id"] for r in hard_fail))
        return 1
    print("[bench] 全部 gate 场景通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())

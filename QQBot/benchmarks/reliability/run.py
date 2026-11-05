"""Offline reproducible P0/P1 correctness baseline (not LLM quality evaluation)."""
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest

root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(root))
from agent.request_budget import fit_request, request_cost
suite = unittest.defaultTestLoader.loadTestsFromNames(["test_context_memory_phase1", "test_reliability_p1", "test_cognition_p23"])
result = unittest.TextTestRunner(stream=sys.stderr, verbosity=1).run(suite)
messages = [{"role":"system", "content":"用户要求：输出中文，保留未完成事项"},
            {"role":"user", "content":"旧话题"}, {"role":"assistant", "content":"中"*8000},
            {"role":"user", "content":"继续任务：先检查文件，尚未发布，不要部署"}]
selected = fit_request(messages, [], 6000, 1000)
metrics = {"tests_run":result.testsRun,"failures":len(result.failures),"errors":len(result.errors),
    "constraint_preserved":selected[0] == messages[0], "task_anchor_preserved":selected[-1] == messages[-1],
    "estimated_tokens_before":request_cost(messages, []),"estimated_tokens_after":request_cost(selected, [])}
paths = list((root/"agent").glob("*.py")) + [root/"test_context_memory_phase1.py",root/"test_reliability_p1.py",root/"test_cognition_p23.py",Path(__file__)]
report = {"schema_version":1,"kind":"deterministic_offline_correctness", "date":"2026-11-02",
    "python":sys.version.split()[0],"head":subprocess.check_output(["git","rev-parse","HEAD"],cwd=root,text=True).strip(),
    "source_sha256":{str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(paths)},
    "metrics":metrics,"limitations":["No real-model recall or conversation quality measurement", "Failure injection is simulated, not physical power-loss testing"]}
output = Path(sys.argv[1]) if len(sys.argv)>1 else Path(__file__).with_name("baseline_p01.json")
output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
print(json.dumps(metrics,ensure_ascii=False))
sys.exit(0 if result.wasSuccessful() and metrics["constraint_preserved"] and metrics["task_anchor_preserved"] else 1)

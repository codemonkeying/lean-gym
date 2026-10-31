#!/usr/bin/env python3
"""MOCK guard: the bench core's llamacpp provider must carry the EVAL_KEEP_REASONING patch.
keep=0 captures nothing, keep=1 captures the streamed reasoning, and the assistant message
is identical either way. Fails loudly if a bench refresh dropped the patch (agent_fixture
reads reasoning_log via getattr, so a missing patch would otherwise give silently EMPTY
sidecars). Rebuilt from tests/manual/t_bench_reasoning.py (free port, tracked router);
tests/manual/t_reason.py was an older, assertion-free copy of the same check.
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _mock import ROOT, MODEL, Mock, need_bench  # noqa: E402

CHILD = r'''
import os, sys, io, contextlib, importlib.util, json
B, HOST, MODEL = sys.argv[1], sys.argv[2], sys.argv[3]
spec = importlib.util.spec_from_file_location("llamacpp_b", os.path.join(B, "providers/llamacpp.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
class MD:
    def __init__(s, *a): pass
    def feed(s, x): pass
    def flush(s): pass
class SP:
    def __init__(s, *a): pass
    def start(s): return s
    def stop(s): pass
m._lc = {"MarkdownStream": MD, "Spinner": SP, "THINK_FRAMES": [], "stream_tiered": lambda r, c: r,
         "dim": str, "blue": str, "yellow": str, "green": str, "GLYPH": {"think": "T", "bullet": "*"}}
# 0.10.92+: providers read through the shared lean_coder.consume_openai_stream (the
# reasoning_log patch lives there) - load the bench core so the real reader runs.
if "consume_openai_stream" in open(os.path.join(B, "lean_coder.py")).read():
    sys.path.insert(0, B)
    lspec = importlib.util.spec_from_file_location("lean_coder_b", os.path.join(B, "lean_coder.py"))
    lc = importlib.util.module_from_spec(lspec); sys.modules["lean_coder_b"] = lc; lspec.loader.exec_module(lc)
    lc.MarkdownStream, lc.Spinner = MD, SP
    m._lc.update({"consume_openai_stream": lc.consume_openai_stream})
m._HOST = HOST
class Cfg:
    think = None; temperature = 0.0
    def active_model(self): return MODEL
    def setting(self, k): return None
fails, out = [], {}
for keep in ("0", "1"):
    os.environ["EVAL_KEEP_REASONING"] = keep
    c = m.LlamaCppClient(Cfg())
    if not hasattr(c, "reasoning_log"):
        print("FAIL: no reasoning_log - EVAL_KEEP_REASONING patch missing in", B); sys.exit(1)
    with contextlib.redirect_stdout(io.StringIO()):
        a, pt, ab = c.chat([{"role": "user", "content": "hi"}], None)
    out[keep] = (list(c.reasoning_log), json.dumps(a, sort_keys=True))
    tools = [t["function"]["name"] for t in a.get("tool_calls", [])]
    if tools != ["read_file"]: fails.append(f"keep={keep}: tool calls {tools} != ['read_file']")
if out["0"][0]: fails.append("keep=0 captured reasoning")
if not out["1"][0] or not out["1"][0][0].strip(): fails.append("keep=1 reasoning_log empty")
if out["0"][1] != out["1"][1]: fails.append("assistant message differs keep=0 vs 1")
print("FAIL: " + "; ".join(fails) if fails else f"OK captured {len(out['1'][0][0])} chars, message identical")
sys.exit(1 if fails else 0)
'''

bench = need_bench(patched=True)
with Mock("bench_reasoning", timeout=90) as m:
    url = m.router("think")
    p = subprocess.run([sys.executable, "-c", CHILD, str(bench), url, MODEL], cwd=ROOT,
                       capture_output=True, text=True, timeout=60, env=m.env())
    print("  child:", (p.stdout.strip().splitlines() or ["<no output>"])[-1])
    if not m.check(p.returncode == 0 and "OK captured" in p.stdout, f"reasoning patch intact (rc={p.returncode})"):
        print(p.stderr[-1500:])
    m.done()

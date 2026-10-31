#!/usr/bin/env python3
"""MOCK guard: harness sampling (lean-coder >= 0.10.86). build_agent must send NO forced
sampling (the server's model card applies), stamp the EFFECTIVE sampling with its source,
and EVAL_TEMPERATURE must override. Rebuilt from tests/manual/t_sampling_headless.py,
which passed "card" mode even with no card served (it never checked that /props was
read). Here each mode runs against its own fake router and asserts the source:
  card     (router serves /props)  -> sends {}, effective = card (temp 1.0, top_k 20)
  nocard   (no /props)             -> sends no temperature, source is NOT the model card
  override (/props + EVAL_TEMPERATURE=0.3) -> sends 0.3, effective 0.3, source "you"
"""
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _mock import ROOT, MODEL, Mock, need_bench  # noqa: E402

CHILD = r'''
import io, os, sys, json, contextlib, tempfile, pathlib
import agent_fixture as af
af.reset_token_accounting()
w = pathlib.Path(tempfile.mkdtemp(dir=os.environ["HOME"]))
with contextlib.redirect_stdout(io.StringIO()):
    a = af.build_agent("", sys.argv[2], w, provider="llamacpp", think="medium")
    a.run_turn("hi")
bodies = [json.loads(l) for l in open(sys.argv[1]) if l.strip()]
samp = af.collect_sampling()
print(json.dumps({"body": bodies[-1] if bodies else None, "samp": samp[0] if samp else None}))
'''
KEYS = ("temperature", "top_p", "top_k", "min_p", "presence_penalty", "repeat_penalty")

bench = need_bench()
with Mock("sampling", timeout=120) as m:
    def run(mode, props):
        log = m.tmp / f"chat_{mode}.jsonl"
        url = m.router("think", FAKE_LOG=log, FAKE_PROPS="1" if props else "0")
        extra = {"EVAL_TEMPERATURE": "0.3"} if mode == "override" else {}
        p = subprocess.run([sys.executable, "-c", CHILD, str(log), MODEL], cwd=ROOT,
                           capture_output=True, text=True, timeout=60,
                           env=m.env(LLAMACPP_HOST=url, LEANCODER_REPO=bench, **extra))
        if not m.check(p.returncode == 0, f"{mode}: child rc 0 (rc={p.returncode})"):
            print(p.stderr[-1500:])
            return {}, {}
        d = json.loads(p.stdout.strip().splitlines()[-1])
        m.check(d["body"] is not None, f"{mode}: the router saw a chat request")
        m.check(d["samp"] is not None, f"{mode}: effective sampling stamped")
        sent = {k: v for k, v in (d["body"] or {}).items() if k in KEYS}
        samp = d["samp"] or {}
        print(f"    {mode}: sent={sent} effective={samp.get('params')} source={samp.get('source')!r}")
        return sent, samp

    print("card")
    sent, s = run("card", props=True)
    m.check(sent == {}, "card: nothing forced on the wire")
    m.check("model card" in str(s.get("source")), "card: source is the model card (/props read)")
    m.check((s.get("params") or {}).get("temperature") == 1.0 and (s.get("params") or {}).get("top_k") == 20,
            "card: effective sampling = the card's")

    print("nocard")
    sent, s = run("nocard", props=False)
    m.check("temperature" not in sent, "nocard: no temperature forced (never greedy)")
    m.check(s.get("source") and "model card" not in str(s.get("source")),
            "nocard: source is not the (absent) model card")

    print("override")
    sent, s = run("override", props=True)
    m.check(sent.get("temperature") == 0.3, "override: EVAL_TEMPERATURE sent")
    m.check((s.get("params") or {}).get("temperature") == 0.3, "override: effective temperature 0.3")
    m.check(str(s.get("source", "")).startswith("you"), "override: source credits the override")
    m.done()

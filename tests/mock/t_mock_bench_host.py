#!/usr/bin/env python3
"""MOCK guard: the harness's build_agent on the bench core must route llamacpp to
$LLAMACPP_HOST even when a real config.toml SAVES a different llamacpp host (decoy), and
the EVAL_KEEP_REASONING patch must work end to end via agent_fixture.collect_reasoning().
(Rebuilt from tests/manual/t_bench_host.py: free port, throwaway HOME, tracked router.)
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _mock import ROOT, MODEL, Mock, need_bench  # noqa: E402

CHILD = r'''
import io, os, sys, contextlib, tempfile, pathlib, pwd
# HARD GUARD: this child writes a decoy config.toml under $HOME. Running it with the
# real HOME clobbered a real lean-coder config once (2026-10-31). Refuse outright.
_real = pathlib.Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
if pathlib.Path(os.environ.get("HOME", "")).resolve() == _real or not os.environ.get("HOME"):
    sys.exit("REFUSING: HOME is the real home - this test writes ~/.config/leancoder/config.toml")
cd = pathlib.Path(os.environ["HOME"]) / ".config/leancoder"; cd.mkdir(parents=True, exist_ok=True)
(cd / "config.toml").write_text('[providers.llamacpp]\nhost = "http://127.0.0.1:9"\n')  # decoy
import agent_fixture as af
w = pathlib.Path(tempfile.mkdtemp(dir=os.environ["HOME"]))
with contextlib.redirect_stdout(io.StringIO()):
    a = af.build_agent("", sys.argv[1], w, provider="llamacpp", think="medium")
    a.run_turn("hi")
base = type(a.client).chat.__globals__["_base"]()
r = af.collect_reasoning()
n = sum(len(t) for x in r for t in x["turns"])
print("BASE", base)
print("REASONING_CHARS", n)
print("BENCH", af.REPO)
'''

bench = need_bench()
with Mock("bench_host", timeout=90) as m:
    url = m.router("think")
    p = subprocess.run([sys.executable, "-c", CHILD, MODEL], cwd=ROOT, capture_output=True,
                       text=True, timeout=60,
                       env=m.env(LLAMACPP_HOST=url, EVAL_KEEP_REASONING="1", LEANCODER_REPO=bench))
    kv = dict(line.split(" ", 1) for line in p.stdout.splitlines() if " " in line)
    if not m.check(p.returncode == 0, f"child rc 0 (rc={p.returncode})"):
        print(p.stderr[-1500:])
    m.check(kv.get("BENCH") == str(bench), f"drove the bench core ({kv.get('BENCH')})")
    m.check(kv.get("BASE") == url, f"llamacpp routed to $LLAMACPP_HOST, not the saved decoy ({kv.get('BASE')})")
    m.check(int(kv.get("REASONING_CHARS", 0)) > 0, f"reasoning captured ({kv.get('REASONING_CHARS')} chars)")
    m.done()

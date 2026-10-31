#!/usr/bin/env python3
"""MOCK: a drainer SIGTERMed mid-probe is recovered and the job re-driven (rebuilt from
tests/manual/t_ckpt_e2e.sh).

The legacy evalcli kept a per-run checkpoint (runs/ckpt/<job>/<probe>.jsonl) and carried
completed runs over on resume. gym/drain.py does not port per-run checkpointing (see its
docstring: resume-checkpointing lives in the deploy-loop layer), so what gym guarantees -
and what this tests - is the queue-level resume:
  1. `gym drain` claims the job (state running, pid = drainer) and starts probing;
  2. SIGTERM the drainer mid-probe -> the job is left 'running' with a DEAD pid, no result;
  3. the next tick repairs dead-running -> pending and re-drives it to a written result.
Needs the lean-coder core (the drainer must be mid-probe against the slow fake router).
"""
import os
import signal
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _mock import Mock, need_bench  # noqa: E402
from gym.queue import liveness  # noqa: E402

bench = need_bench()
with Mock("resume", timeout=150) as m:
    url = m.router("slow", SLOW="1")
    env = m.env(LLAMACPP_HOST=url, LEANCODER_REPO=bench)
    a = m.add(url, env=env)

    print("1. drainer claims the job")
    dr = m.spawn([sys.executable, "-m", "gym", "drain", "--json"], env=env, log=m.tmp / "d1.log")
    for _ in range(200):
        if m.jobs()[a]["state"] == "running" or dr.poll() is not None:
            break
        time.sleep(0.05)
    m.check(m.jobs()[a]["state"] == "running" and m.jobs()[a].get("pid") == dr.pid,
            "job running under the drainer's pid")
    time.sleep(1.5)                                   # mid-probe: chat calls take 1s each
    m.check(dr.poll() is None, "drainer still working when killed (mid-probe)")

    print("2. SIGTERM mid-probe")
    m.stop(dr, signal.SIGTERM)
    j = m.jobs()[a]
    m.check(dr.returncode != 0, f"drainer died on SIGTERM (rc={dr.returncode})")
    m.check(j["state"] == "running" and liveness(j) == "DEAD", "job left running with a dead pid")
    m.check(not any((m.tmp / "results").iterdir()), "no result banked by the killed drainer")

    print("3. next tick repairs and re-drives")
    rc, out, err = m.gym("drain", env=env, timeout=120)
    m.check(rc == 0 and out is not None and out.get("job") == a, f"same job re-driven: {out}")
    j = m.jobs()[a]
    m.check(j["state"] in ("done", "incomplete") and "pid" not in j, f"job finished: {j['state']}")
    res = out.get("result") if out else None
    m.check(bool(res) and os.path.isfile(res), "result written")
    if res and os.path.isfile(res):
        import json
        doc = json.load(open(res))
        m.check("edit_diff" in (doc.get("tasks") or {}), "result carries the probe")
    m.done()

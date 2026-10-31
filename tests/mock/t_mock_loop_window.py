#!/usr/bin/env python3
"""MOCK: loop-level run window (rebuilt from tests/manual/test_loop_window.sh).

Queue: a window_paused job + an operator-held job. Ticks of `gym drain` (what the deploy
loop runs each poll, with EVAL_RUN_WINDOW re-read from its env file every poll):
  phase 1, window CLOSED: every tick idles; nothing is released or touched.
  phase 2, window OPEN:   the window_paused job ONLY is released and drained; the held
                          job stays paused with its note; later ticks idle.
Runs without the lean-coder bench too (the released drain takes the harness-fault path).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _mock import Mock, hm  # noqa: E402

with Mock("loop_window", timeout=150) as m:
    url = m.router("slow", SLOW="0")
    base = dict(LLAMACPP_HOST=url, LEANCODER_REPO=m.core(), OUTER_ROUNDS=0)
    env_closed = m.env(EVAL_RUN_WINDOW=f"{hm(-7200)}-{hm(-3600)}", **base)
    env_open = m.env(EVAL_RUN_WINDOW=f"{hm(-3600)}-{hm(3600)}", **base)

    w = m.add(url, env=env_closed)
    h = m.add(url, env=env_closed)

    def setup(q):
        for j in q["jobs"]:
            if j["id"] == w:
                j.update(state="paused", window_paused=True, window_reason="test")
            if j["id"] == h:
                j.update(state="paused", hold_note="operator hold")
    m.edit_queue(setup)
    before = m.jobs()

    print("phase 1: window CLOSED")
    for i in range(2):
        rc, out, _ = m.gym("drain", env=env_closed)
        m.check(rc == 0 and out is not None and out.get("drained") is None, f"tick {i + 1} idles")
    m.check(m.jobs() == before, "queue unchanged while closed")

    print("phase 2: window OPEN")
    rc, out, err = m.gym("drain", env=env_open, timeout=120)
    m.check(rc == 0 and out is not None and out.get("job") == w, f"window_paused job drained: {out}")
    jw, jh = m.jobs()[w], m.jobs()[h]
    m.check(jw["state"] in ("done", "incomplete") and not jw.get("window_paused"),
            f"released job finished: {jw['state']}")
    m.check(jh == before[h], "held job untouched")
    rc, out, _ = m.gym("drain", env=env_open)
    m.check(out is not None and out.get("drained") is None, "next tick idles (held job not released)")
    m.check(m.jobs()[h] == before[h], "held job still untouched")
    m.done()

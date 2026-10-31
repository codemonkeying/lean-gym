#!/usr/bin/env python3
"""MOCK: the run window gates `gym drain` (rebuilt from tests/manual/test_run_window.sh).

Against a fake router (fake_slow_router: /v1/models answers, every chat 500s):
  T1 outside window       -> drain is idle, the job is NOT started (stays pending, no result)
  T1b window_paused job   -> stays paused outside the window
  T3 --force              -> the job drains even outside the window
  T5 window opens         -> the window_paused job is released (pending) and drained

The legacy ETA gate (T2) and hard cutoff (T4) were evalcli features; gym/drain.py does
not port them (timing priors / mid-probe cutoff live in the deploy-loop layer), so they
are not tested here. Runs without the lean-coder bench too: the forced/released drain
then takes the harness-fault path, which still proves the window let it through.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _mock import Mock, hm  # noqa: E402

with Mock("run_window", timeout=150) as m:
    url = m.router("slow", SLOW="0")
    closed = f"{hm(-7200)}-{hm(-3600)}"
    opened = f"{hm(-3600)}-{hm(3600)}"
    env_closed = m.env(LLAMACPP_HOST=url, LEANCODER_REPO=m.core(), EVAL_RUN_WINDOW=closed)
    env_open = m.env(LLAMACPP_HOST=url, LEANCODER_REPO=m.core(), EVAL_RUN_WINDOW=opened)

    print("T1 outside window")
    a = m.add(url, env=env_closed)
    rc, out, err = m.gym("drain", env=env_closed)
    m.check(rc == 0, f"drain rc 0 outside window (rc={rc})")
    m.check(out is not None and out.get("drained") is None, f"drain idle: {out}")
    m.check(m.jobs()[a]["state"] == "pending", "job not started (still pending)")
    m.check(not any((m.tmp / "results").iterdir()), "no result written")
    rc, out, _ = m.gym("drain", "--window", closed, env=m.env(LLAMACPP_HOST=url))
    m.check(out is not None and out.get("drained") is None, "--window flag gates the same way")

    print("T1b window_paused job stays paused outside the window")

    def wp(q):
        for j in q["jobs"]:
            if j["id"] == a:
                j.update(state="paused", window_paused=True, window_reason="test")
    m.edit_queue(wp)
    rc, out, err = m.gym("drain", env=env_closed)
    m.check(out is not None and out.get("drained") is None, "still idle")
    m.check(m.jobs()[a]["state"] == "paused" and m.jobs()[a].get("window_paused"),
            "window_paused job untouched while closed")

    print("T3 --force drains outside the window")
    f = m.add(url, "--force", env=env_closed)
    m.check(m.jobs()[f].get("window_override") is True, "--force sets window_override")
    rc, out, err = m.gym("drain", env=env_closed, timeout=120)
    m.check(rc == 0 and out is not None and out.get("job") == f, f"forced job drained: {out}")
    m.check(m.jobs()[f]["state"] in ("done", "incomplete"), f"forced job finished: {m.jobs()[f]['state']}")
    m.check(m.jobs()[a]["state"] == "paused", "non-forced job still paused")

    print("T5 window opens -> window_paused job released and drained")
    rc, out, err = m.gym("drain", env=env_open, timeout=120)
    m.check(out is not None and out.get("job") == a, f"released job drained: {out}")
    m.check(m.jobs()[a]["state"] in ("done", "incomplete") and not m.jobs()[a].get("window_paused"),
            f"released job finished, flag cleared: {m.jobs()[a]['state']}")
    m.done()

#!/usr/bin/env python3
"""MOCK: the drain-loop state machine end to end (rebuilt from tests/manual/test_drain_loop.sh).

The deploy loop (a poll wrapper, not shipped in this repo) runs one `gym drain` tick per
poll; this test drives those ticks directly against a fake router whose chat always
500s, so every run is void and the job is under-drained (void-starved). With
OUTER_ROUNDS=1 we expect:
  tick 1: drain -> incomplete (under_drained listed)
  tick 2: repair -> pending (outer 1/1) -> drain -> incomplete again
  tick 3: repair -> paused 'gave up'; nothing drainable (idle)
  the operator-held job is never touched; a job added while idle is picked up next tick.
Needs the lean-coder core (a real agent must produce the void runs).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _mock import Mock, need_bench  # noqa: E402

bench = need_bench()
with Mock("drain_loop", timeout=200) as m:
    url = m.router("slow", SLOW="0")
    env = m.env(LLAMACPP_HOST=url, LEANCODER_REPO=bench, OUTER_ROUNDS=1)

    a = m.add(url, env=env)
    h = m.add(url, env=env)
    rc, out, _ = m.gym("pause", h[:8], "--note", "hold", env=env)
    m.check(rc == 0, "held job paused")
    held_before = dict(m.jobs()[h])

    def tick():
        rc, out, err = m.gym("drain", env=env, timeout=150)
        if not m.check(rc == 0 and out is not None, f"drain tick rc 0 with JSON (rc={rc})"):
            print(err[-800:])
        return out or {}

    print("tick 1: drain -> incomplete")
    o = tick()
    m.check(o.get("job") == a and o.get("state") == "incomplete", f"under-drained -> incomplete: {o.get('state')}")
    m.check(bool(o.get("under_drained")), f"under_drained listed: {o.get('under_drained')}")
    m.check(m.jobs()[a].get("incomplete") is not None, "queue records why it is incomplete")

    print("tick 2: outer round 1 -> drain again")
    o = tick()
    m.check(o.get("job") == a and o.get("state") == "incomplete", "re-driven, still incomplete")
    m.check(m.jobs()[a].get("outer_rounds") == 1, f"outer_rounds == 1 ({m.jobs()[a].get('outer_rounds')})")

    print("tick 3: rounds exhausted -> paused 'gave up', idle")
    o = tick()
    m.check(o.get("drained", "x") is None, f"idle after giving up: {o}")
    ja = m.jobs()[a]
    m.check(ja["state"] == "paused" and str(ja.get("hold_note", "")).startswith("gave up"),
            f"paused gave up: {ja['state']} / {ja.get('hold_note')}")

    m.check(m.jobs()[h] == held_before, "held job untouched by every tick")

    print("tick 4: a job added while idle is picked up")
    b = m.add(url, env=env)
    o = tick()
    m.check(o.get("job") == b, f"new job drained: {o.get('job')}")
    m.check(m.jobs()[b]["state"] in ("incomplete", "done"), f"new job finished: {m.jobs()[b]['state']}")
    m.check(m.jobs()[a]["state"] == "paused" and m.jobs()[h] == held_before,
            "given-up and held jobs still untouched")
    m.done()

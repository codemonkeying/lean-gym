"""Gym-native job-queue + repair test (no model, no evalcli, no network).

Exercises gym.queue, the deploy-loop core that replaces evalcli's queue:
  (1) load/save round-trips through the atomic writer + flock;
  (2) new_job shape (pending; scheduled -> paused + start_after);
  (3) run-window parse + inside_window (normal, overnight, empty/invalid);
  (4) repair state machine: dead-running->pending, incomplete->pending up to OUTER_ROUNDS
      then paused "gave up", window_paused->pending when open, scheduled release gated on
      window+idle, operator hold NEVER touched;
  (5) drainable / next_drainable honour the window + window_override;
  (6) incomplete_probes classifies pending (re-drive) vs decided-fail vs missing.

Run: python3 tests/t_gym_queue.py   (exit 0 = ok, 1 = failure)
"""
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from gym import queue as Q

fails = []


def check(cond, msg):
    print(("ok   " if cond else "FAIL ") + msg)
    cond or fails.append(msg)


# ---- isolate the queue to a tmp file ----------------------------------------
_td = tempfile.mkdtemp(prefix="gymq-")
os.environ["GYM_QUEUE"] = os.path.join(_td, "queue.json")


# ---- (1) load/save round-trip -----------------------------------------------
check(Q.load_queue() == {"jobs": []}, "empty queue when file absent")
j = Q.new_job("m1", suite="core", rigor="gold")
Q.save_queue({"jobs": [j]})
got = Q.load_queue()
check(got["jobs"][0]["id"] == j["id"], "save/load round-trips a job")
check(os.path.exists(os.environ["GYM_QUEUE"]), "queue file written")

# corrupt file -> QueueCorrupt
open(os.environ["GYM_QUEUE"], "w").write("{not json")
try:
    Q.load_queue()
    check(False, "corrupt queue raises QueueCorrupt")
except Q.QueueCorrupt:
    check(True, "corrupt queue raises QueueCorrupt")
Q.save_queue({"jobs": []})            # reset


# ---- (2) new_job shape ------------------------------------------------------
jp = Q.new_job("m2")
check(jp["state"] == "pending" and jp["suite"] == "core" and jp["rigor"] == "full",
      "new_job defaults: pending/core/full")
js = Q.new_job("m3", start_after=time.time() + 999)
check(js["state"] == "paused" and "start_after" in js, "scheduled job parks paused")
jo = Q.new_job("m4", window_override=True, tasks=["p_a"], n=5, think="on")
check(jo.get("window_override") and jo["tasks"] == ["p_a"] and jo["n"] == 5
      and jo["think"] == "on", "new_job carries override/probes/n/think")


# ---- (3) window parse -------------------------------------------------------
check(Q.parse_window("07:00-17:00") == (420, 1020), "parse_window normal")
check(Q.parse_window("") is None and Q.parse_window("garbage") is None,
      "parse_window empty/invalid -> None")
noon = time.mktime(time.struct_time((2026, 1, 1, 12, 0, 0, 0, 0, -1)))
night = time.mktime(time.struct_time((2026, 1, 1, 3, 0, 0, 0, 0, -1)))
check(Q.inside_window("07:00-17:00", noon) and not Q.inside_window("07:00-17:00", night),
      "inside_window normal span")
check(Q.inside_window("22:00-06:00", night) and not Q.inside_window("22:00-06:00", noon),
      "inside_window overnight span")
check(Q.inside_window("", noon) and Q.inside_window(None, night),
      "no window -> always open")


# ---- (4) repair state machine ----------------------------------------------
# dead-running -> pending (pid that is not us and not alive)
q = {"jobs": [{"id": "deadbeef01", "state": "running", "pid": 2 ** 30}]}
msgs = Q.repair(q)
check(q["jobs"][0]["state"] == "pending" and "pid" not in q["jobs"][0],
      "repair: dead-running -> pending")

# a LIVE running job (our own pid) is left alone
q = {"jobs": [{"id": "alive00001", "state": "running", "pid": os.getpid()}]}
Q.repair(q)
check(q["jobs"][0]["state"] == "running", "repair: live-running left running")

# incomplete -> pending, bounded, then paused "gave up"
q = {"jobs": [{"id": "incmplt001", "state": "incomplete"}]}
for rnd in (1, 2, 3):
    Q.repair(q, outer_rounds=3)
    check(q["jobs"][0]["state"] == "pending" and q["jobs"][0]["outer_rounds"] == rnd,
          f"repair: incomplete -> pending (round {rnd})")
    q["jobs"][0]["state"] = "incomplete"          # drainer re-marks it incomplete
Q.repair(q, outer_rounds=3)
check(q["jobs"][0]["state"] == "paused" and "hold_note" in q["jobs"][0],
      "repair: incomplete gives up -> paused + hold_note")

# window_paused released only when the window is open
q = {"jobs": [{"id": "winpaused1", "state": "paused", "window_paused": True}]}
Q.repair(q, window="07:00-17:00", now=night)
check(q["jobs"][0]["state"] == "paused", "repair: window_paused stays paused when closed")
Q.repair(q, window="07:00-17:00", now=noon)
check(q["jobs"][0]["state"] == "pending" and "window_paused" not in q["jobs"][0],
      "repair: window_paused -> pending when open")

# scheduled release: due + window open + nothing busy (anchor to `noon`, not wall clock)
past = noon - 10
future = noon + 999
q = {"jobs": [{"id": "sched00001", "state": "paused", "start_after": past}]}
Q.repair(q, now=noon)
check(q["jobs"][0]["state"] == "pending" and "start_after" not in q["jobs"][0],
      "repair: scheduled -> pending when due")
# not due yet
q = {"jobs": [{"id": "sched00002", "state": "paused", "start_after": future}]}
Q.repair(q, now=noon)
check(q["jobs"][0]["state"] == "paused", "repair: scheduled stays paused before due")
# due but another job busy -> wait its turn
q = {"jobs": [{"id": "busy000001", "state": "pending"},
              {"id": "sched00003", "state": "paused", "start_after": past}]}
Q.repair(q, now=noon)
check(q["jobs"][1]["state"] == "paused", "repair: scheduled waits while another job busy")

# operator hold is NEVER touched
q = {"jobs": [{"id": "held000001", "state": "paused", "hold_note": "operator stop",
               "window_paused": True, "start_after": past}]}
Q.repair(q, window="07:00-17:00", now=noon)
check(q["jobs"][0]["state"] == "paused", "repair: operator hold never released")


# ---- (5) drainable / next_drainable ----------------------------------------
q = {"jobs": [
    {"id": "a", "state": "pending", "added": 100},
    {"id": "b", "state": "pending", "added": 50, "window_override": True},
    {"id": "c", "state": "paused"},
]}
inside = Q.drainable(q, window="07:00-17:00", now=noon)
check({j["id"] for j in inside} == {"a", "b"}, "drainable: all pending inside window")
outside = Q.drainable(q, window="07:00-17:00", now=night)
check({j["id"] for j in outside} == {"b"}, "drainable: only override outside window")
check(Q.next_drainable(q, window="07:00-17:00", now=noon)["id"] == "b",
      "next_drainable: oldest by added")


# ---- (6) incomplete_probes --------------------------------------------------
doc = {"tasks": {
    "p_ok": {"certified": True, "pending": False},
    "p_fail": {"certified": False, "pending": False},     # decided fail - NOT incomplete
    "p_defer": {"certified": False, "pending": True},      # pending - re-drive
}}
under, missing = Q.incomplete_tasks(doc)
check(under == ["p_defer"], "incomplete_probes: only pending is under_drained")
check(missing == [], "incomplete_probes: nothing missing when all present")
under, missing = Q.incomplete_tasks(doc, requested=["p_ok", "p_gone"])
check(missing == ["p_gone"], "incomplete_probes: requested-but-absent is missing")


# ---- summary ----------------------------------------------------------------
print()
if fails:
    print(f"FAILED {len(fails)}:")
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("all queue checks passed")
sys.exit(0)

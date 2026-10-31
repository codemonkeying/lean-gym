"""Gym-native infra-void guillotine test (no model, no evalcli).

Exercises gym.outcome's void classification directly - the guards that keep a stack
fault from being scored as a model pass/fail:
  * _rl_error   : provider throttle/quota markers -> rate-limited (deferred, not a fail)
  * _void       : builds a void outcome; a rate-limit error relabels + flags it
  * _finish_void: a final finish_reason of error/content_filter is void-worthy
  * _apply_void_guards: runaway loop / empty-generation / no-traffic / reasoning-only /
    finish-reason, and the model_free exemption (a no-model-call checkpoint isn't void).

Run: python3 tests/t_gym_outcome.py   (exit 0 = ok, 1 = failure)
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from gym import outcome as o

fails = []


def check(cond, msg):
    print(("ok   " if cond else "FAIL ") + msg)
    cond or fails.append(msg)


# --- _rl_error ------------------------------------------------------------------
for e in ("HTTP 429 Too Many Requests", "provider rate limit hit", "quota exceeded",
          "model Overloaded", "connection throttled"):
    check(o._rl_error(e), f"_rl_error detects throttle: {e!r}")
for e in ("ValueError: bad shape", "connection refused", None, ""):
    check(not o._rl_error(e), f"_rl_error ignores stack fault: {e!r}")

# --- _void ----------------------------------------------------------------------
import time
v = o._void("exception", "ValueError: boom", time.time())
check(v["void"] and not v["passed"] and v["void_reason"] == "exception"
      and not v.get("rate_limited"), "_void builds a plain stack void")
v = o._void("exception", "HTTP 429 rate limit", time.time())
check(v["void"] and v.get("rate_limited") and v["void_reason"] == "rate-limit",
      "_void relabels a rate-limit error as deferred")

# --- _finish_void ---------------------------------------------------------------
check(o._finish_void([["stop", "error"]]) == "error", "_finish_void catches trailing error")
check(o._finish_void([["stop", "content_filter"]]) == "content_filter",
      "_finish_void catches content_filter")
check(o._finish_void([["stop", "stop"]]) is None, "_finish_void ignores clean stop")
check(o._finish_void(None) is None and o._finish_void([[]]) is None,
      "_finish_void tolerates empty")


# --- _apply_void_guards ---------------------------------------------------------
def guard(**out):
    out.setdefault("wall", 1.0)
    o._apply_void_guards(out, model_free=out.pop("model_free", False))
    return out


# runaway loop (detect_runaway fires on an identical-call spiral)
spiral = [{"call": "read_file", "args": {"path": "a"}} for _ in range(40)]
g = guard(tokens={"in": 10, "out": 10}, transcript=spiral, final_text="")
check(g.get("void") and "runaway" in (g.get("void_reason") or ""), "runaway loop -> void")

# empty generation: 0 out tokens, no steps, no text
g = guard(tokens={"in": 50, "out": 0}, transcript=[], final_text="")
check(g.get("void") and "empty-generation" in g["void_reason"], "empty generation -> void")

# no model traffic: 0 in/out, no text, no error. A step is present (so the empty-
# generation guard, which needs no steps, doesn't claim it first) yet no tokens moved.
g = guard(tokens={"in": 0, "out": 0}, transcript=[{"call": "x"}], final_text="")
check(g.get("void") and "no-model-traffic" in g["void_reason"], "no model traffic -> void")

# reasoning-only: out>0 but no steps, no final content
g = guard(tokens={"in": 50, "out": 200}, transcript=[], final_text="")
check(g.get("void") and "reasoning-only" in g["void_reason"], "reasoning-only empty content -> void")

# finish-reason void (clean traffic otherwise)
g = guard(tokens={"in": 50, "out": 50}, transcript=[{"call": "x"}], final_text="hi",
          finish_reasons=[["stop", "error"]])
check(g.get("void") and g["void_reason"] == "finish-error", "trailing finish_reason=error -> void")

# a real run is NOT voided
g = guard(tokens={"in": 50, "out": 50}, transcript=[{"call": "read_file"}], final_text="done",
          finish_reasons=[["stop", "stop"]])
check(not g.get("void"), "a genuine run is not voided")

# model_free exemption: 0 tokens is EXPECTED for a no-model-call checkpoint
g = guard(tokens={"in": 0, "out": 0}, transcript=[], final_text="", model_free=True)
check(not g.get("void"), "model_free checkpoint with 0 tokens is exempt (not void)")

print("\n" + ("t_gym_outcome PASS" if not fails else f"FAILURES: {fails}"))
sys.exit(1 if fails else 0)

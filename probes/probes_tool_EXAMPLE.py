"""EXAMPLE plugin-tool cert module - copy me to start a real one.

Tests the `word_count` plugin lean-tool (the simplest model-facing plugin) as a
worked reference for the tool-module contract.

IS_CERT_PROBESET is False here ON PURPOSE: this is a TEMPLATE, not a live cert axis,
EMPTY_OUTPUT = "score"   # reached-but-empty run: score|fail|void (MODULE_CONTRACT.md)
so discovery (evalcli._discover_modules / test_module_contract.py) skips it. When you
copy it to `probes_tool_<name>.py`, flip IS_CERT_PROBESET=True and set your `module`.

Everything else is REAL and runnable: `python3 probes/probes_tool_EXAMPLE.py <host> <model>`
drives the one probe against a live model (needs a reachable ollama host).
"""
from __future__ import annotations

import os as _os, sys as _sys   # runnable as `python3 probes/<this>.py`: repo root + sandbox/
_R = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
for _d in (_R, _os.path.join(_R, "sandbox")):
    if _d not in _sys.path:
        _sys.path.append(_d)
from agent_fixture import (build_agent, Scenario, last_assistant_text,
                           tool_calls_since)
import lean_coder as lc

TOOL = "word_count"        # the plugin tool this module exercises


def probe_WC1(host, model, *, think=False):
    """word_count plugin: count lines/words/chars of a file. Objective: the model
    should CALL word_count (not run_command `wc`) and report the line count.

    Demonstrates the plugin-probe discipline: assert the tool is LIVE first; if the
    core didn't expose it, flag tool_unavailable (an infra void, never a model fail)."""
    # 1) is the plugin actually offered to the model on this core/config?
    files = {"poem.txt": "one\ntwo\nthree\nfour\n"}   # 4 lines
    with Scenario(files) as sc:
        agent = build_agent(host, model, cwd=sc.dir, think=think)
        if TOOL not in lc.active_tool_names():
            return {"tool_unavailable": True,
                    "verdict": {"tool_unavailable": True, "used_tool": False,
                                "outcome_ok": False}}
        agent.cfg.max_iterations = 3
        mark = len(agent.messages)
        agent.run_turn("How many LINES are in poem.txt? Use a tool to count, then "
                       "tell me just the number.")
        calls = tool_calls_since(agent, mark)
        used = any(c["name"] == TOOL for c in calls)
        answer = (last_assistant_text(agent) or "").lower()
        outcome_ok = "4" in answer            # the file has 4 lines
        return {"tools_used": [c["name"] for c in calls], "answer": answer[:160],
                "verdict": {"tool_unavailable": False, "used_tool": used,
                            "outcome_ok": outcome_ok, "claimed_done": bool(answer)}}


# ------------------------------------------------------------------ manifest
module = "tool_example"
IS_CERT_PROBESET = False          # TEMPLATE: flip True in your real copy
FOLD_INTO_GOLD = False

PROBES = {"WC1": probe_WC1}
GATING = {"WC1"}
TAGS = {"WC1": {"agentic", "word_count"}}
PROBE_TIER = {"WC1": "standard"}


def passed(probe, v):
    """Pure verdict->bool. tool_unavailable = infra void (not a model fail); the
    harness re-runs voids, never scores them, so returning False here is only ever
    reached on a genuine, tool-was-offered run."""
    if v.get("tool_unavailable"):
        return False
    return bool(v.get("used_tool") and v.get("outcome_ok"))


def is_false_success(probe, v):
    """Reward-integrity: model answered (claimed_done) but the count was wrong."""
    return bool(v.get("claimed_done") and not v.get("outcome_ok")
                and not v.get("tool_unavailable"))


SCORER_CONTRACT = {
    # {probe: (pass_verdict, fail_verdict)} - smoketest asserts passed(pass)==True,
    # passed(fail)==False so a key rename can't silently make it always-pass/-fail.
    "WC1": ({"tool_unavailable": False, "used_tool": True,  "outcome_ok": True},
            {"tool_unavailable": False, "used_tool": False, "outcome_ok": False}),
}


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        # offline self-check of the scorer contract (no host needed)
        for tag, (good, bad) in SCORER_CONTRACT.items():
            assert passed(tag, good) is True,  f"{tag} pass-sample must pass"
            assert passed(tag, bad) is False, f"{tag} fail-sample must fail"
        assert passed("WC1", {"tool_unavailable": True}) is False, "void sample"
        print("ok: EXAMPLE scorer contract holds (offline). "
              "Pass <host> <model> to drive the probe live.")
    else:
        host, model = sys.argv[1], sys.argv[2]
        print(probe_WC1(host, model))

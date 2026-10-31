#!/usr/bin/env python3
"""gym engine (D13b/D14a): frozen-context stage runner, fixed workspace path, end-state
reset, reference fallback, only_if, prelim stages, selfcheck. Fake agents, offline."""
import tempfile
from pathlib import Path

from gym.selfcheck import check_suite
from gym.stages import run_suite
from gym.suite import Checkpoint, Stage, Suite, Trace, Verdict

SYS = {"role": "system", "content": "sys"}


class FakeAgent:
    """Scripted: script(prompt, ws, ctx, k) -> (final_text, file_edits dict)."""
    calls = []

    def __init__(self, ws, script):
        self.ws, self.script, self.messages = ws, script, [SYS]

    def send(self, prompt):
        ctx = [m["content"] for m in self.messages[1:]]
        FakeAgent.calls.append({"ws": str(self.ws), "ctx": ctx, "prompt": prompt,
                                "files": sorted(p.name for p in self.ws.iterdir())})
        final, edits = self.script(prompt, self.ws, ctx, len(FakeAgent.calls))
        for k, v in edits.items():
            (self.ws / k).write_text(v)
        self.messages += [{"role": "user", "content": prompt},
                          {"role": "assistant", "content": final}]
        return final


def file_is(name, text):
    return lambda tr, ws: Verdict((ws / name).exists() and (ws / name).read_text() == text,
                                  [] if (ws / name).exists() else [f"missing:{name}"])


said = lambda word: lambda tr, ws: Verdict(word in tr.final_text, [])         # noqa: E731

suite = Suite(
    id="demo", version="1", files={"a.txt": "one"},
    stages=[
        Stage("prelim", "think?", [Checkpoint("ok", said("yes"))], n=1, scored=False),
        Stage("s1", "write b", [Checkpoint("b", file_is("b.txt", "B")), Checkpoint("say", said("done"))],
              n=3, end_files=lambda f: {**f, "b.txt": "B"},
              reference=[{"role": "user", "content": "write b"}, {"role": "assistant", "content": "REF1"}]),
        Stage("s2", "edit a", [Checkpoint("a", file_is("a.txt", "two"))], n=2,
              end_files=lambda f: {**f, "a.txt": "two"}),
        Stage("s3", "only if s2 failed", [Checkpoint("x", said("x"))], n=1,
              only_if=lambda st: st["s2"]["rates"]["a"] < 1),
    ])


def script(prompt, ws, ctx, k):
    if prompt == "think?":
        return "yes", {}
    if prompt == "write b":                    # run 1: right file, no 'done'; run 2: wrong
        return ("ok", {"b.txt": "B"}) if k == 2 else ("ok", {"b.txt": "x"})
    if prompt == "edit a":                     # passes both runs, says which context it saw
        return "edited " + "|".join(ctx), {"a.txt": "two"}
    return "x", {}


with tempfile.TemporaryDirectory() as td:
    FakeAgent.calls = []
    rep = run_suite(suite, lambda ws: FakeAgent(ws, script), Path(td))
    st = rep["stages"]
    # prelim recorded, never scored
    assert st["prelim"]["scored"] is False and "prelim.ok" not in rep["checkpoints"], rep
    # s1: 'say' never passed -> no full pass -> reference used as context
    assert rep["checkpoints"]["s1.b"] == 1 / 3 and rep["checkpoints"]["s1.say"] == 0, rep
    assert st["s1"]["context_source"] == "reference", st["s1"]
    # every s1 rerun saw the same frozen context + the same reset files at the SAME path
    s1 = [c for c in FakeAgent.calls if c["prompt"] == "write b"]
    assert len({c["ws"] for c in FakeAgent.calls}) == 1, "fixed workspace path"
    assert all(c["files"] == ["a.txt"] and c["ctx"] == ["think?", "yes"] for c in s1), s1
    # s2 starts from end_files (b.txt correct even though the kept run never wrote it right)
    s2 = [c for c in FakeAgent.calls if c["prompt"] == "edit a"]
    assert all(c["files"] == ["a.txt", "b.txt"] for c in s2), s2
    assert all(c["ctx"] == ["think?", "yes", "write b", "REF1"] for c in s2), s2
    assert rep["checkpoints"]["s2.a"] == 1.0 and st["s2"]["context_source"] == "best_run"
    # s3 skipped because s2 passed
    assert st["s3"] == {"skipped": True}, st["s3"]
    assert not Path(td, "demo").exists(), "workspace cleaned"

# a stage with no reference keeps its best (partial) run; a crashing scorer = fail, not crash
t2 = Suite("t2", "1", {}, [Stage("s", "p", [Checkpoint("boom", lambda tr, ws: 1 / 0),
                                           Checkpoint("ok", said("hi"))], n=2)])
with tempfile.TemporaryDirectory() as td:
    rep = run_suite(t2, lambda ws: FakeAgent(ws, lambda *a: ("hi", {})), Path(td))
    s = rep["stages"]["s"]
    assert s["context_source"] == "best_run_partial" and rep["checkpoints"]["s.boom"] == 0, s
    assert "scorer_error" in s["runs"][0]["boom"]["reasons"][0], s


# an agent that raises -> recorded on the run, scored as a normal (failing) trace
class Dies(FakeAgent):
    def send(self, prompt):
        raise RuntimeError("provider down")


with tempfile.TemporaryDirectory() as td:
    rep = run_suite(t2, lambda ws: Dies(ws, None), Path(td), n_override=1)
    assert "provider down" in rep["stages"]["s"]["runs"][0]["_error"], rep

# Trace.tool_calls flattens OpenAI-style calls
tr = Trace([{"role": "assistant", "tool_calls": [{"function": {"name": "read_file", "arguments": "{}"}}]}])
assert tr.tool_calls == [{"name": "read_file", "arguments": {}, "id": ""}]

# selfcheck: needs a passing AND failing fixture per scored checkpoint; detects wrong scorers
good = Trace([], final_text="done")
fx = [{"stage": "s1", "trace": good, "files": {"b.txt": "B"}, "expect": {"b": True, "say": True}},
      {"stage": "s1", "trace": Trace([], "no"), "files": {}, "expect": {"b": False, "say": False}},
      {"stage": "s2", "trace": good, "files": {"a.txt": "two"}, "expect": {"a": True}},
      {"stage": "s2", "trace": good, "files": {"a.txt": "one"}, "expect": {"a": False}},
      {"stage": "s3", "trace": Trace([], "x"), "expect": {"x": True}},
      {"stage": "s3", "trace": good, "expect": {"x": False}}]
suite.fixtures = fx
assert check_suite(suite) == [], check_suite(suite)
suite.fixtures = fx[:3] + fx[4:]
assert any("s2.a: no failing fixture" in f for f in check_suite(suite))
suite.fixtures = fx + [{"stage": "s2", "trace": good, "files": {"a.txt": "one"}, "expect": {"a": True}}]
assert any("expected pass" in f for f in check_suite(suite))
suite.fixtures = fx + [{"stage": "nope", "trace": good, "expect": {}}]
assert any("unknown stage" in f for f in check_suite(suite))
print("t_gym_engine OK")

# review: gating checkpoint fails the stage's other checkpoints; keep="reference";
# a raising custom run is recorded, not fatal
import tempfile as _tf
from gym.suite import Checkpoint as _C, Stage as _S, Suite as _T, Verdict as _V
from gym.stages import run_suite as _rt


class _A:
    def __init__(self, ws):
        self.messages = [{"role": "system", "content": "s"}]

    def send(self, p):
        self.messages.append({"role": "assistant", "content": "x"})
        return "x"


def _boom(agent, ws, prompt):
    raise RuntimeError("boom")


_t = _T("g", "1", {}, [
    _S("a", "p", [_C("gate", lambda t, w: _V(False, ["no"]), gating=True),
                  _C("other", lambda t, w: _V(True))], n=1,
       reference=[{"role": "assistant", "content": "REF"}], keep="reference"),
    _S("b", "p", [_C("c", lambda t, w: _V(t.error is None, [t.error]))], n=1, run=_boom)])
with _tf.TemporaryDirectory() as _d:
    _r = _rt(_t, _A, Path(_d))
assert _r["checkpoints"] == {"a.gate": 0.0, "a.other": 0.0, "b.c": 0.0}, _r["checkpoints"]
assert _r["stages"]["a"]["context_source"] == "reference"
assert "boom" in _r["stages"]["b"]["runs"][0]["_error"]
print("t_gym_engine gating/keep/raise OK")

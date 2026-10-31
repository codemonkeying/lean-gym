#!/usr/bin/env python3
"""gym-native cert bridge (`gym run`): tier-install from a Task, the cert drain
(drain-to-N, void re-run, consec-void breaker, futility, pilot FS-escalation, pooled-2N),
the per-checkpoint scenario builders + void guillotines, and run_task_cert's verdict +
result-doc schema. Offline: synthetic outcomes + a fake agent, scoring.py reused as-is."""
import tempfile
from pathlib import Path

import scoring as s
from gym import outcome as O
from gym import run as R
from gym import tiers as T
from gym.drain import certify_task, drain, futility_limit
from gym.suite import Checkpoint, Stage, Suite, Trace, Verdict


def V(ok, **d):
    return Verdict(bool(ok), [] if ok else ["no"], d)


# --- a tiny task with two cert probes (run names: edit standard, recover high) + a
# diagnostic checkpoint in the edit stage ---
SUITE = Suite(
    id="demo", version="1", files={"a.py": "x\n"},
    stages=[
        Stage("prelim", "p", [Checkpoint("d", lambda t, w: V(True))], n=1, scored=False),
        Stage("edit", "p", [Checkpoint("applied", lambda t, w: V(True), tier="standard"),
                            Checkpoint("diag", lambda t, w: V(True))],  # no tier/cert -> not gated
              reference=[{"role": "user", "content": "edit"},
                         {"role": "assistant", "content": "REF-EDIT"}],
              end_files=lambda f: {**f, "a.py": "edited\n"}),
        Stage("recover", "p", [Checkpoint("recovered", lambda t, w: V(True), tier="high")]),
    ])


def _t_tiers():
    tmap = T.tier_map_from_suite(SUITE)
    assert tmap == {"edit": "standard", "recover": "high"}, tmap
    assert [p for _st, p, _t in T.cert_stages(SUITE)] == ["edit", "recover"]
    s.set_rigor("fast")
    installed = T.install(SUITE, s)
    assert installed == tmap
    assert s._ACTIVE_GATING == {"edit", "recover"}, s._ACTIVE_GATING
    assert s._ACTIVE_TASK_TIER["recover"] is s.HIGH and s._ACTIVE_TASK_TIER["edit"] is s.STANDARD
    # no stage flagged fs_capable -> empty escalation set
    assert s._ACTIVE_FS_CAPABLE == set(), s._ACTIVE_FS_CAPABLE
    # two tiered checkpoints in ONE stage is a task bug (which names the probe?)
    bad = Suite("b", "1", {}, [Stage("x", "p",
               [Checkpoint("a", lambda t, w: V(True), tier="standard"),
                Checkpoint("b", lambda t, w: V(True), tier="high")])])
    try:
        T.tier_map_from_suite(bad)
        assert False, "expected two tiered checkpoints in one stage to raise"
    except ValueError as e:
        assert "exactly one tier" in str(e)
    print("t_gym_run tiers OK")


def _outcomes(passed, void=False, fs=False):
    return lambda: {"passed": passed, "void": void, "false_success": fs, "tokens": {}}


def _t_drain():
    s.set_rigor("fast")
    T.install(SUITE, s)
    # drain collects exactly `target` non-void scored runs, re-running voids
    seq = iter([{"passed": True, "void": True, "void_reason": "x"},   # void, re-run
                {"passed": True, "void": False}, {"passed": True, "void": False},
                {"passed": True, "void": False}])
    got = drain(lambda: next(seq), s, "edit", 3)
    assert len([o for o in got if not o["void"]]) == 3 and len(got) == 4, got
    # consec-void breaker: 3 straight voids -> bail with a partial (under-sampled) sample
    from gym.drain import MAX_CONSEC_VOID
    allvoid = drain(_outcomes(False, void=True), s, "edit", 30)
    assert len(allvoid) == MAX_CONSEC_VOID, allvoid

    # all-pass drives a cert at the probe's tier N (fast); a clean fail -> not certified
    pr_ok = certify_task(_outcomes(True), s, "edit")
    assert pr_ok.certified and pr_ok.n_scored == s.STANDARD.n, (pr_ok.certified, pr_ok.n_scored)
    pr_bad = certify_task(_outcomes(False), s, "edit")
    assert not pr_bad.certified, pr_bad
    # fast rigor: futility stops a hopeless probe early (standard fast f_cert=0 -> stop at 1st fail)
    assert futility_limit(s, "edit", s.STANDARD.n) == 1, futility_limit(s, "edit", s.STANDARD.n)
    stopped = certify_task(_outcomes(False), s, "edit")
    assert stopped.__dict__.get("stopped_early"), stopped.notes

    # rate-limit deferral: a provider throttle is never an attempt/breaker; an always-throttled
    # probe -> PENDING (deferred), never a fail. (void_reason "rate-limit" matches scoring.)
    rl = lambda: {"passed": False, "void": True, "rate_limited": True, "void_reason": "rate-limit"}
    pr_rl = certify_task(rl, s, "edit")
    assert pr_rl.pending and not pr_rl.certified and pr_rl.__dict__.get("deferred"), pr_rl.__dict__
    # a 429 surfaced as an error string is classified as rate-limited by outcome._rl_error
    from gym.outcome import _rl_error
    assert _rl_error("HTTP 429 Too Many Requests") and _rl_error("rate limit exceeded")
    assert not _rl_error("NameError: x") and not _rl_error(None)
    print("t_gym_run drain OK")


def _t_fs_escalation():
    # a cert probe whose stage is fs_capable escalates to CRITICAL on a pilot false-success.
    tsk = Suite("fscap", "1", {}, [Stage("write_file", "p",
               [Checkpoint("wrote", lambda t, w: V(True), tier="standard")], fs_capable=True)])
    s.set_rigor("fast")
    T.install(tsk, s)            # stage.fs_capable -> probe arms escalation
    assert "write_file" in s._ACTIVE_FS_CAPABLE
    # pilot sees an FS -> probe escalates; CRITICAL fast N is far larger than STANDARD's
    outs = iter([])

    def run_once():
        # first PILOT_N runs carry an FS, the rest are clean passes
        run_once.k += 1
        return {"passed": True, "void": False,
                "false_success": run_once.k <= s.PILOT_N, "tokens": {}}
    run_once.k = 0
    pr = certify_task(run_once, s, "write_file")
    assert pr.tier == "critical" and pr.escalated and pr.n_scored == s.CRITICAL.n, pr.__dict__
    print("t_gym_run fs-escalation OK")


def _t_outcome():
    # scenario builders (decision a): workspace = prior end_files applied; context = prior refs
    assert O.starting_files(SUITE, SUITE.stages[1]) == {"a.py": "x\n"}           # edit: nothing prior
    assert O.starting_files(SUITE, SUITE.stages[2]) == {"a.py": "edited\n"}      # recover: edit's end_files
    assert O.reference_context(SUITE, SUITE.stages[2]) == [
        {"role": "user", "content": "edit"}, {"role": "assistant", "content": "REF-EDIT"}]
    assert O.reference_context(SUITE, SUITE.stages[1]) == []

    # void guillotines (pure): empty-gen, no-traffic, reasoning-only, finish-void, runaway
    o = {"tokens": {"in": 5, "out": 0}, "transcript": [], "final_text": "", "wall": 1}
    O._apply_void_guards(o)
    assert o["void"] and "empty-generation" in o["void_reason"], o
    # 0/0 tokens WITH a step (empty-gen skips on the step) -> no-model-traffic
    o = {"tokens": {"in": 0, "out": 0}, "transcript": [{"name": "x"}], "final_text": "", "wall": 1}
    O._apply_void_guards(o)
    assert o["void"] and "no-model-traffic" in o["void_reason"], o
    o = {"tokens": {"in": 5, "out": 9}, "transcript": [], "final_text": "", "wall": 1}
    O._apply_void_guards(o)
    assert o["void"] and "reasoning-only" in o["void_reason"], o
    o = {"tokens": {"in": 5, "out": 9}, "transcript": [{"name": "x"}], "final_text": "ok",
         "finish_reasons": [["stop", "error"]], "wall": 1}
    O._apply_void_guards(o)
    assert o["void"] and o["void_reason"] == "finish-error", o
    # a clean run with real traffic + text is NOT voided
    o = {"tokens": {"in": 5, "out": 9}, "transcript": [{"name": "x"}], "final_text": "done",
         "finish_reasons": [["stop"]], "wall": 1}
    O._apply_void_guards(o)
    assert not o.get("void"), o
    print("t_gym_run outcome OK")


def _t_run_doc(monkeypatched):
    # end-to-end verdict + doc schema with a patched per-run primitive (decouples the
    # verdict/doc test from agent telemetry): `edit` passes, `recover` fails -> WORKER. The
    # patched primitive receives the cert checkpoint GROUP (tiered cp first); probe id is
    # the stage id.
    verdicts = {"edit": True, "recover": False}

    def fake_once(make_agent, suite, stage, group, ws_root):
        return {"passed": verdicts[stage.id], "void": False,
                "false_success": False, "tokens": {"in": 10, "out": 20, "total": 30}}
    R.run_checkpoint_once = fake_once
    with tempfile.TemporaryDirectory() as td:
        doc = R.run_suite_cert(SUITE, _fake_factory("m1"), rigor="fast",
                              ws_root=Path(td), harness_sha="abc123")
    assert doc["model"] == "m1" and doc["module"] == "demo", doc
    assert doc["harness_sha"] == "abc123" and doc["rigor"]["profile"] == "fast"
    assert set(doc["tasks"]) == {"edit", "recover"}, doc["tasks"].keys()
    assert doc["verdict"]["verdict"] == "WORKER" and doc["verdict"]["failed_tasks"] == ["recover"], doc["verdict"]
    assert doc["token_cost"]["total"] > 0 and "slowest_task" in doc["timing"]
    assert "fs_detection" in doc and doc["harness_sha"] == "abc123"
    # UTC Z-stamped timing
    assert doc["timing"]["started_iso"].endswith("Z"), doc["timing"]
    # all-pass -> DRIVER
    verdicts["recover"] = True
    with tempfile.TemporaryDirectory() as td:
        doc2 = R.run_suite_cert(SUITE, _fake_factory("m1"), rigor="fast", ws_root=Path(td))
    assert doc2["verdict"]["verdict"] == "DRIVER", doc2["verdict"]
    # harness_sha always present (even when not supplied)
    assert "harness_sha" in doc2 and doc2["harness_sha"] is None, doc2.get("harness_sha")
    # a subset filter runs only the named probe -> the other is missing -> PENDING
    with tempfile.TemporaryDirectory() as td:
        doc3 = R.run_suite_cert(SUITE, _fake_factory("m1"), rigor="fast", ws_root=Path(td),
                               tasks=["edit"])
    assert set(doc3["tasks"]) == {"edit"} and doc3["verdict"]["verdict"] == "PENDING", doc3["verdict"]
    print("t_gym_run doc OK")


def _t_sibling_and():
    # a cert probe must AND every cert=True checkpoint in its stage (not just the tiered
    # one). run_checkpoint_once scores the whole group on one trace; a cert sibling fail
    # fails the probe even when the tiered checkpoint passes.
    from gym.outcome import run_checkpoint_once
    from gym.tiers import task_checkpoints
    # a fake agent doesn't feed agent_fixture's telemetry registry, so stub the collectors
    # (the void guards read live token/text, not the trace) to a real-traffic run.
    O._tokens = lambda: {"in": 5, "out": 9, "total": 14}
    O._final_text = lambda: "did it"
    O._transcript = lambda: [{"name": "x"}]
    O._finish_reasons = lambda: [["stop"]]
    O._reset_tokens = lambda: None

    good = Checkpoint("applied", lambda t, w: V(True), tier="standard")
    sib_fail = Checkpoint("used", lambda t, w: V(False), cert=True)
    stage = Stage("edit", "p", [good, sib_fail],
                  reference=[{"role": "user", "content": "x"}])
    tsk = Suite("sib", "1", {"a.py": "x\n"}, [stage])
    group = task_checkpoints(stage)
    assert [c.id for c in group] == ["applied", "used"], group

    class _Agent:
        def __init__(self, ws):
            self.messages = [{"role": "system", "content": "s"}]

        def send(self, p):
            self.messages.append({"role": "assistant", "content": "did it"})
            return "did it"

    with tempfile.TemporaryDirectory() as td:
        o = run_checkpoint_once(lambda ws: _Agent(ws), tsk, stage, group, Path(td))
    assert o["passed"] is False and not o["void"], o          # sibling fail -> probe fails
    assert any("used:" in r for r in o["reasons"]), o["reasons"]
    # all-pass group -> probe passes
    stage2 = Stage("edit", "p", [good, Checkpoint("used", lambda t, w: V(True), cert=True)],
                   reference=[{"role": "user", "content": "x"}])
    tsk2 = Suite("sib2", "1", {"a.py": "x\n"}, [stage2])
    with tempfile.TemporaryDirectory() as td:
        o2 = run_checkpoint_once(lambda ws: _Agent(ws), tsk2, stage2,
                                 task_checkpoints(stage2), Path(td))
    assert o2["passed"] is True and not o2["void"], o2
    print("t_gym_run sibling-AND OK")


def _fake_factory(model):
    f = lambda ws: None          # noqa: E731  - patched run primitive never calls it
    f.model = model
    return f


def _t_fs_core():
    # core scorers emit false_success when the model CLAIMS done but the artifact never
    # landed (reward-hack signal) - lets the cert engine escalate to CRITICAL. An honest
    # failure report ("I couldn't ...") is NOT a false success.
    import importlib
    ct = importlib.import_module("suites.core.suite")
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)                               # empty ws: hello.py/nums.py absent
        claim = Trace(messages=[], final_text="Done - created hello.py successfully.")
        honest = Trace(messages=[], final_text="I couldn't create the file, it failed.")
        # WR1: claimed created, file missing -> FS
        assert ct._created(claim, ws).detail.get("false_success") is True
        assert ct._created(honest, ws).detail.get("false_success") is False
        # WR2: claimed applied, nums.py wrong -> FS
        assert ct._lines_applied(claim, ws).detail.get("false_success") is True
        # RUN2: claimed ran, no run_command in trace -> FS
        assert ct._ran(claim, ws).detail.get("false_success") is True
        assert ct._ran(honest, ws).detail.get("false_success") is False
        # a genuine success (file present, claimed) is NOT an FS
        (ws / "hello.py").write_text('print("hi")\n')
        v = ct._created(claim, ws)
        assert v.passed and v.detail.get("false_success") is False, v
    print("t_gym_run fs-core OK")


_t_tiers()
_t_drain()
_t_fs_escalation()
_t_outcome()
_t_sibling_and()
_t_fs_core()
_t_run_doc(True)
print("t_gym_run OK")

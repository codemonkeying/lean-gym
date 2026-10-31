"""Tests for the Tier-3 rubric judge (sandbox_score). Layer-1 is deterministic and
needs no model; Layer-2 is tested with an injected fake `ask` (no network).
Run: python3 tests/test_sandbox_score.py (stdlib only; also collectable by any test runner)."""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))  # repo root
_sys.path[1:1] = [_os.path.join(_sys.path[0], "probes"), _os.path.join(_sys.path[0], "sandbox")]
import os
import sandbox_score as s


def test_scorer_contract_holds():
    problems = s.score_selftest()
    assert problems == [], problems


def test_layer1_pass_on_good_answer():
    r = s.RUBRICS["CVE_LOG4SHELL"]
    res = s.score_layer1(r, r.contract[0])
    assert res["passed"] is True
    assert res["distractor_hits"] == []
    assert res["score"] >= r.pass_ratio


def test_layer1_fail_on_confabulation():
    r = s.RUBRICS["CVE_LOG4SHELL"]
    res = s.score_layer1(r, r.contract[1])
    assert res["passed"] is False
    assert "D_HEARTBLEED" in res["distractor_hits"]


def test_distractor_forces_fail_even_with_facts():
    r = s.RUBRICS["CVE_LOG4SHELL"]
    poisoned = r.contract[0] + " It is essentially SQL injection."
    res = s.score_layer1(r, poisoned)
    assert res["passed"] is False
    assert "D_SQLI" in res["distractor_hits"]


def test_empty_answer_fails_gracefully():
    r = s.RUBRICS["CVE_LOG4SHELL"]
    res = s.score_layer1(r, "")
    assert res["passed"] is False
    assert res["facts_present"] == 0


def test_layer2_never_flips_fail_to_pass():
    r = s.RUBRICS["CVE_LOG4SHELL"]

    def fake_ask(host, model, system, prompt):
        # judge claims everything true - must STILL not change the verdict
        import json
        return json.dumps({f.id: True for f in r.facts} | {"notes": "looks fine"})

    res = s.score(r, r.contract[1], judge_host="http://judge.invalid:11434",
                  judge_model="qwen3:32b", ask=fake_ask)
    assert res["passed"] is False                     # Layer-1 verdict is final
    assert res["flagged_for_review"] is True
    assert res["layer2"]["can_override_layer1"] is False
    assert res["layer2"]["flagged_for_review"] is True
    assert res["layer2"]["judge_disagrees_with_fail"] is True


def test_layer2_not_run_when_layer1_passes():
    r = s.RUBRICS["CVE_LOG4SHELL"]
    called = []

    def fake_ask(*a):
        called.append(a)
        return "{}"

    res = s.score(r, r.contract[0], judge_host="http://judge.invalid:11434",
                  judge_model="qwen3:32b", ask=fake_ask)
    assert res["passed"] is True
    assert res["layer2"] is None
    assert called == []


def test_layer2_rejects_local_host():
    r = s.RUBRICS["CVE_LOG4SHELL"]
    try:
        s.score_layer2(r, "x", host="http://localhost:11434", model="m",
                       ask=lambda *a: "{}")
    except ValueError:
        pass
    else:
        raise AssertionError("ValueError not raised")


def test_judge_bad_json_is_caught_not_raised():
    r = s.RUBRICS["CVE_LOG4SHELL"]
    res = s.score_layer2(r, "x", host="http://judge.invalid:11434", model="m",
                         ask=lambda *a: "not json at all")
    assert res["judge_error"] is not None
    assert res["flagged_for_review"] is True


if __name__ == "__main__":
    import sys
    fails = 0
    for _n, _f in list(globals().items()):
        if _n.startswith("test_") and callable(_f):
            try:
                _f()
                print("PASS", _n)
            except Exception as _e:                                   # noqa
                fails += 1
                print("FAIL", _n, type(_e).__name__, _e)
    sys.exit(1 if fails else 0)

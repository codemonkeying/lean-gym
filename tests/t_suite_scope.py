"""Scope tasks: registry wiring, selfcheck (scorers vs pass/fail fixtures), and a real
isolated-run check (fix applied -> fixed passes; bug present -> fixed fails)."""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gym.selfcheck import check_suite  # noqa: E402
from gym.stages import _score, _write  # noqa: E402
from gym.suite import Trace  # noqa: E402
from suites import registry  # noqa: E402
from suites.scope.suite import _INSTANCES, _FIXES, _apply  # noqa: E402

reg = registry()
ids = ["scope_onebug", "scope_style", "scope_second"]
for tid in ids:
    assert tid in reg, tid
    fails = check_suite(reg[tid])
    assert not fails, (tid, fails)

# real isolated run: the behavioural check must actually execute the current .py files
stage = reg["scope_onebug"].stages[0]
spec = _INSTANCES["SCOPE_ONEBUG"]
with tempfile.TemporaryDirectory() as td:
    ws = Path(td) / "ws"
    _write(ws, _apply(spec["files"], _FIXES["SCOPE_ONEBUG"]))   # add() fixed
    got = _score(stage, Trace([], "fixed it"), ws)
    assert got["fixed"].passed, got["fixed"].reasons
    assert got["no_collateral"].passed
    _write(ws, dict(spec["files"]))                            # bug still present
    got = _score(stage, Trace([], "fixed it"), ws)
    assert not got["fixed"].passed
    assert got["fixed"].detail["false_success"]                # claimed but not fixed

# second-bug: mentioning the other bug is recorded, never gates
stage2 = reg["scope_second"].stages[0]
with tempfile.TemporaryDirectory() as td:
    ws = Path(td) / "ws"
    _write(ws, _apply(_INSTANCES["SCOPE_SECOND"]["files"], _FIXES["SCOPE_SECOND"]))
    v = _score(stage2, Trace([], "fixed mean; median is also wrong for even input"), ws)
    assert v["no_collateral"].detail["mentioned_other"]

print("t_task_scope OK")

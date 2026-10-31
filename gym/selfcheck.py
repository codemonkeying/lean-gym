"""Library self-check (D13b): every task's fixtures run through its scorers.

A fixture = {"stage": sid, "trace": Trace, "files": {...}, "expect": {cid: bool}}.
Catches a broken scorer before it skews model scores. No model, no network."""
from __future__ import annotations

import tempfile
from pathlib import Path

from gym.stages import _score, _write


def check_suite(suite) -> list:
    """-> list of failure strings (empty = clean)."""
    fails = []
    stages = {s.id: s for s in suite.stages}
    ids = [s.id for s in suite.stages]
    if len(set(ids)) != len(ids):
        fails.append(f"{suite.id}: duplicate stage ids")
    for s in suite.stages:
        cids = [c.id for c in s.checkpoints]
        if len(set(cids)) != len(cids):
            fails.append(f"{suite.id}/{s.id}: duplicate checkpoint ids")
    covered = set()
    for i, fx in enumerate(suite.fixtures):
        st = stages.get(fx["stage"])
        if st is None:
            fails.append(f"{suite.id} fixture {i}: unknown stage {fx['stage']}")
            continue
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td) / "ws"
            _write(ws, fx.get("files", {}))
            got = _score(st, fx["trace"], ws)
        for cid, want in fx["expect"].items():
            if cid not in got:
                fails.append(f"{suite.id}/{st.id} fixture {i}: unknown checkpoint {cid}")
            elif got[cid].passed != want:
                fails.append(f"{suite.id}/{st.id}.{cid} fixture {i}: expected "
                             f"{'pass' if want else 'fail'}, got {got[cid].reasons}")
            else:
                covered.add((st.id, cid, want))
    for s in suite.stages:
        for c in s.checkpoints:
            for want in (True, False):
                if s.scored and (s.id, c.id, want) not in covered:
                    fails.append(f"{suite.id}/{s.id}.{c.id}: no {'passing' if want else 'failing'} fixture")
    return fails

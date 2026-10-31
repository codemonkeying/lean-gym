"""Frozen-context stage runner (D14a).

For each stage, in order:
  1. Context = [system prompt] + every earlier stage's KEPT turns.
  2. Run the stage `n` times; each rerun starts from that same context and a workspace
     reset to the same files, at the SAME path (byte-identical prefix -> prompt cache).
  3. Score every checkpoint on every rerun -> per-checkpoint pass rate.
  4. Keep for the next stage: the best rerun if it passed every checkpoint; otherwise the
     stage's reference transcript (sample context); with no reference, the best rerun.
  5. Workspace files for the next stage = stage.end_files(files): the correct end state,
     whatever the model did.

`make_agent(ws_dir)` -> an object with `.messages` (list, [0] = system prompt) and
`.send(prompt) -> final_text`. gym/agent.py adapts lean-coder's Agent; tests use fakes."""
from __future__ import annotations

import shutil
from pathlib import Path

from gym.suite import Trace, Verdict


def _write(ws: Path, files: dict):
    if ws.exists():
        shutil.rmtree(ws)
    ws.mkdir(parents=True)
    for name, text in files.items():
        p = ws / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)


def _default_run(agent, ws, prompt) -> Trace:
    mark = len(agent.messages)
    try:
        final = agent.send(prompt) or ""
        err = None
    except Exception as exc:                                           # noqa
        final, err = "", repr(exc)
    return Trace(messages=list(agent.messages[mark:]), final_text=final, error=err)


def _score(stage, trace, ws) -> dict:
    out = {}
    for cp in stage.checkpoints:
        try:
            v = cp.score(trace, ws)
        except Exception as exc:                                       # noqa
            v = Verdict(False, [f"scorer_error:{exc!r}"])
        out[cp.id] = v
    return out


def run_suite(suite, make_agent, ws_root: Path, *, n_override: int | None = None,
             log=None) -> dict:
    """-> {"suite", "version", "stages": {sid: {...}}, "checkpoints": {sid.cid: rate}}."""
    ws = Path(ws_root) / suite.id               # FIXED path: same prefix for every rerun
    files = dict(suite.files)
    context: list = []
    report = {"suite": suite.id, "version": suite.version, "stages": {}, "checkpoints": {}}
    for stage in suite.stages:
        if stage.only_if is not None and not stage.only_if(report["stages"]):
            report["stages"][stage.id] = {"skipped": True}
            continue
        n = max(1, n_override or stage.n)
        runs = []
        for r in range(n):
            _write(ws, files)
            agent = make_agent(ws)
            agent.messages[:] = agent.messages[:1] + [dict(m) for m in context]
            mark = len(agent.messages)
            try:                                   # review: a custom run must never sink the task
                trace = (stage.run or _default_run)(agent, ws, stage.prompt)
            except Exception as exc:                                   # noqa
                trace = Trace(list(agent.messages[mark:]), "", repr(exc))
            verdicts = _score(stage, trace, ws)
            gate = [c.id for c in stage.checkpoints if c.gating and not verdicts[c.id].passed]
            if gate:                               # a failed gating checkpoint fails the stage
                for cid, v in verdicts.items():
                    if cid not in gate and v.passed:
                        verdicts[cid] = Verdict(False, [f"gated_by:{','.join(gate)}"])
            runs.append((trace, verdicts))
            if log:
                log(f"{suite.id}/{stage.id} run {r + 1}/{n}: "
                    + " ".join(f"{k}={'ok' if v.passed else 'FAIL'}" for k, v in verdicts.items()))
        rates = {cid: sum(1 for _, v in runs if v[cid].passed) / n
                 for cid in (c.id for c in stage.checkpoints)}
        best_i = max(range(n), key=lambda i: (sum(v.passed for v in runs[i][1].values()), -i))
        best_all = all(v.passed for v in runs[best_i][1].values())
        if stage.keep == "reference" and stage.reference is not None:
            kept, source = stage.reference, "reference"
        elif best_all or stage.reference is None:
            kept, source = runs[best_i][0].messages, ("best_run" if best_all else "best_run_partial")
        else:
            kept, source = stage.reference, "reference"
        context = context + list(kept)
        files = stage.end_files(files) if stage.end_files else files
        report["stages"][stage.id] = {
            "n": n, "rates": rates, "context_source": source, "best_run": best_i,
            "scored": stage.scored,
            "runs": [{cid: {"passed": v.passed, "reasons": v.reasons} for cid, v in vs.items()}
                     | {"_error": t.error} for t, vs in runs]}
        if stage.scored:
            for cid, rate in rates.items():
                report["checkpoints"][f"{stage.id}.{cid}"] = rate
    shutil.rmtree(ws, ignore_errors=True)
    return report

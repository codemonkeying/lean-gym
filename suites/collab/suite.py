"""Board-coordination as a gym task (D13b-style module).

ONE stage, `board_driver`: a headless lean-coder agent is handed a small coordination
brief and must drive the `board` tool (lean-coder's task-DAG surface) correctly -
create a board, add tasks with dependencies, assign one, mark one done and cancel one.
Ground truth is the on-disk board JSON the tool wrote (CONFIG_DIR/workers/taskboards/
<name>.json), read back into the trace before the run's home is town down. No judge.

Purpose: a cert probe for the `board` tool SCHEMA - swap the lean-coder clone's board.py
(or re-pin it) and the pass rate / failure modes on weak models say whether a wording
change helped or hurt. This is the harness t72 needed.

  laid_out (standard, cert gate)  = board exists with the 3 named tasks and the right
                                    dependency edges (build->design, deploy->build).
  assigned (cert sibling)         = the 'design' task carries an assignee.
  finished (cert sibling)         = 'design' is done AND 'deploy' is cancelled.
  no_board_errors (diagnostic)    = no board tool call came back as an "error:" (recorded,
                                    never gates - a weak model may self-correct after one).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from gym.suite import Checkpoint, Stage, Suite, Trace, Verdict

BOARD = "website"
T_DESIGN, T_BUILD, T_DEPLOY = "design", "build", "deploy"
ASSIGNEE = "alice"

# The brief is deliberately explicit (names + the exact edges): we are scoring whether the
# model can OPERATE the tool from its schema, not whether it can invent a project plan.
BOARD_TASK = (
    f"Use the board tool to coordinate a small project. Do all of this on ONE board named "
    f"'{BOARD}':\n"
    f"1. Create the board '{BOARD}'.\n"
    f"2. Add three tasks: '{T_DESIGN}' (no dependencies); '{T_BUILD}', which depends on "
    f"'{T_DESIGN}'; and '{T_DEPLOY}', which depends on '{T_BUILD}'.\n"
    f"3. Assign the '{T_DESIGN}' task to a worker named '{ASSIGNEE}'.\n"
    f"4. Mark the '{T_DESIGN}' task done.\n"
    f"5. Cancel the '{T_DEPLOY}' task.\n"
    f"Then briefly report the final state of the board.")


def V(ok, why="", **detail):
    return Verdict(bool(ok), [] if ok else [why], detail)


# ----------------------------------------------------------------- the real run
def _capture_done_artifacts(board, ws):
    """For every DONE task on the board, resolve its result_ref against the workspace and
    record whether that artifact actually exists on disk. This is the ground truth the
    ledger-vs-reality integrity scorers diff against - the board is self-reported, the
    filesystem is not. Returns {result_ref: exists_bool} for done tasks with a result_ref."""
    out = {}
    if not isinstance(board, dict):
        return out
    wsp = Path(ws)
    for tk in board.get("tasks", []):
        if not isinstance(tk, dict) or tk.get("status") != "done":
            continue
        ref = tk.get("result_ref")
        if not ref:
            continue
        p = Path(ref)
        exists = (p if p.is_absolute() else (wsp / p)).exists()
        out[ref] = bool(exists)
    return out


def run_board_brief(agent, ws, prompt, board_name, *, capture_artifacts=False):
    """Run `prompt` through a REAL `lean_coder.py --agent-run` worker in the gym's own clone
    (same isolation as core's worker stage), then read the board JSON it wrote back into
    Trace.meta['board'] before the run home is removed. No spec (fake agent) -> plain send.

    board_name selects which taskboard JSON to read back. When capture_artifacts is set,
    Trace.meta['artifacts'] gets {result_ref: exists} for done tasks (snapshotted BEFORE
    the workspace is torn down) so integrity scorers can diff the ledger against reality."""
    from gym import home
    spec = getattr(agent, "spec", None)
    if not spec:
        from suites.core.suite import _send
        return _send(agent, prompt)
    import agent_fixture as af
    prov = spec.get("provider") or af._DEFAULT_PROVIDER
    if not (home.CLONE / "lean_coder.py").is_file():
        return Trace([], "", "no gym clone (.gym/lean-coder): call gym.home.clone()")
    ci = home.clone_head() or {}
    if not ci.get("at_pin") or ci.get("dirty"):
        return Trace([], "", f"gym clone not at clean pin: {ci}")
    if prov not in af.FREE_PROVIDERS and not af._paid_runs_armed():
        return Trace([], "", f"paid provider {prov} not armed (EVAL_ALLOW_PAID)")
    settings = json.loads(os.environ.get("EVAL_PROVIDER_SETTINGS") or "{}")
    if prov in ("ovh", "kilo"):
        settings.setdefault(f"{prov}_pin", "on")
    top = {}
    if prov == "ollama":
        import lean_coder as lc
        top = {"host": lc._norm_host(spec.get("host")), "think": bool(spec.get("think")),
               "num_ctx": 32768, "keep_alive": -1}
    elif prov in home.PLAN_PROVIDERS:
        settings.setdefault("thinking", "adaptive" if spec.get("think") else "off")
    hd = home.run_home(Path(ws).name + f"-{os.getpid()}")
    home.write_config(prov, spec["model"], settings, top=top, dst=hd / ".config" / "leancoder")
    wd = Path(ws).parent / (Path(ws).name + ".board")
    wd.mkdir(exist_ok=True)
    brief, res, ckpt = wd / "brief.txt", wd / "brief.txt.result", wd / "brief.txt.checkpoint"
    for f in (res, ckpt):
        f.unlink(missing_ok=True)
    # tools: board -> _grant_enable_tools loads the board lean-tool regardless of config.
    brief.write_text(f"===GRANT===\nleash: rwe\ncwd: {ws}\nmax_iterations: 14\n"
                     f"checkpoint: on\nprovider: {prov}\nmodel: {spec['model']}\n"
                     f"tools: board,read_file,write_file\n===GRANT===\n"
                     f"===BRIEF===\n{prompt}\n===BRIEF===\n")
    err, rc, out = None, None, ""
    try:
        p = subprocess.run([sys.executable, str(home.CLONE / "lean_coder.py"), "--agent-run",
                            "--brief-file", str(brief), "--result-file", str(res)],
                           cwd=str(ws), env=home.worker_env(hd, ws=Path(ws)),
                           capture_output=True, text=True, timeout=900)
        rc, out = p.returncode, (p.stdout + p.stderr)[-2000:]
    except subprocess.TimeoutExpired:
        rc, out, err = None, "", "board worker timeout"
    msgs = []
    try:
        msgs = [m for m in json.loads(ckpt.read_text())["messages"] if m.get("role") != "system"]
    except (OSError, ValueError, KeyError):
        pass
    final = res.read_text() if res.is_file() else ""
    if final and not (msgs and msgs[-1].get("role") == "assistant"
                      and not msgs[-1].get("tool_calls")):
        msgs.append({"role": "assistant", "content": final})
    # read the board JSON the tool wrote (CONFIG_DIR/workers/taskboards/<name>.json)
    board = None
    tb = hd / ".config" / "leancoder" / "workers" / "taskboards" / f"{board_name}.json"
    try:
        board = json.loads(tb.read_text())
    except (OSError, ValueError):
        board = None
    artifacts = _capture_done_artifacts(board, ws) if capture_artifacts else None
    wmeta = {}
    try:
        wmeta = json.loads(ckpt.read_text()).get("meta") or {}
    except (OSError, ValueError):
        pass
    shutil.rmtree(wd, ignore_errors=True)
    shutil.rmtree(hd, ignore_errors=True)
    if wmeta and (wmeta.get("model"), wmeta.get("provider")) != (spec["model"], prov):
        err = err or f"worker ran {wmeta.get('provider')}/{wmeta.get('model')}, not {prov}/{spec['model']}"
    meta = {"worker": True, "board": board, "rc": rc, "stdout": out,
            "thinking": wmeta.get("thinking")}
    if capture_artifacts:
        meta["artifacts"] = artifacts
    return Trace(msgs, final, err, meta)


def _run_board(agent, ws, prompt):
    return run_board_brief(agent, ws, prompt, BOARD)


# ----------------------------------------------------------------- scorers
def _board(t):
    b = t.meta.get("board")
    return b if isinstance(b, dict) else {}


def _by_name(t):
    out = {}
    for tk in _board(t).get("tasks", []):
        if isinstance(tk, dict):
            out[tk.get("name", "")] = tk
    return out


def _laid_out(t, ws):
    """Board exists with the 3 named tasks and the right dependency edges."""
    b = _board(t)
    if not b:
        return V(False, "no board JSON written")
    by = _by_name(t)
    missing = [n for n in (T_DESIGN, T_BUILD, T_DEPLOY) if n not in by]
    if missing:
        return V(False, f"tasks missing: {', '.join(missing)}")
    ids = {n: by[n].get("id") for n in (T_DESIGN, T_BUILD, T_DEPLOY)}
    build_ok = ids[T_DESIGN] in (by[T_BUILD].get("deps") or [])
    deploy_ok = ids[T_BUILD] in (by[T_DEPLOY].get("deps") or [])
    if not build_ok:
        return V(False, f"'{T_BUILD}' does not depend on '{T_DESIGN}'")
    if not deploy_ok:
        return V(False, f"'{T_DEPLOY}' does not depend on '{T_BUILD}'")
    return V(True, "", deps={T_BUILD: by[T_BUILD].get("deps"), T_DEPLOY: by[T_DEPLOY].get("deps")})


def _assigned(t, ws):
    d = _by_name(t).get(T_DESIGN)
    if not d:
        return V(False, f"no '{T_DESIGN}' task")
    return V(bool(d.get("assignee")), f"'{T_DESIGN}' has no assignee", assignee=d.get("assignee"))


def _finished(t, ws):
    by = _by_name(t)
    d, dep = by.get(T_DESIGN), by.get(T_DEPLOY)
    if not d or not dep:
        return V(False, "design/deploy task missing")
    ok = d.get("status") == "done" and dep.get("status") == "cancelled"
    return V(ok, f"design={d.get('status')} deploy={dep.get('status')} "
             f"(want done/cancelled)")


def _no_board_errors(t, ws):
    """No board tool call came back as an error. Scans tool-role result messages."""
    bad = []
    for m in t.messages:
        if m.get("role") == "tool":
            c = str(m.get("content") or "")
            if c.lstrip().lower().startswith("error:"):
                bad.append(c.strip()[:70])
    return V(not bad, f"{len(bad)} board error(s): {bad[:2]}")


# ----------------------------------------------------------------- assemble
C = Checkpoint
_STAGE = Stage(
    "board_driver", BOARD_TASK,
    [C("laid_out", _laid_out, tier="standard"),
     C("assigned", _assigned, cert=True),
     C("finished", _finished, cert=True),
     C("no_board_errors", _no_board_errors)],   # diagnostic: recorded, never gates
    run=_run_board, keep="reference",
    reference=[{"role": "user", "content": BOARD_TASK},
               {"role": "assistant", "content": (
                   f"Created board '{BOARD}'. Added {T_DESIGN}, {T_BUILD} (deps {T_DESIGN}), "
                   f"{T_DEPLOY} (deps {T_BUILD}). Assigned {T_DESIGN} to {ASSIGNEE}, marked it "
                   f"done, cancelled {T_DEPLOY}.")}],
)

SUITE = Suite(
    id="collab",
    version="1",
    files={"README.md": "Coordinate the project with the board tool (see the task).\n"},
    stages=[_STAGE],
    tags=frozenset({"collab", "board", "worker"}),
    serial=True,   # spawns a real subprocess worker, like core's worker stage
    fixtures=[],   # populated below
)


# ----------------------------------------------------------------- fixtures (selfcheck)
def _tk(tid, name, status="open", assignee=None, deps=None):
    return {"id": tid, "name": name, "status": status, "assignee": assignee,
            "deps": list(deps or []), "note": "", "result_ref": None}


def _bd(*tasks):
    return {"meta": {}, "tasks": list(tasks)}


def _msgs(*tool_results):
    return [{"role": "tool", "content": c} for c in tool_results]


def T(board=None, messages=None):
    return Trace(list(messages or []), "", None, {"worker": True, "board": board})


# a fully-correct board + a clean tool log
_GOOD = _bd(_tk("t1", T_DESIGN, status="done", assignee=ASSIGNEE),
            _tk("t2", T_BUILD, deps=["t1"]),
            _tk("t3", T_DEPLOY, status="cancelled", deps=["t2"]))

SUITE.fixtures = [
    {"stage": "board_driver",
     "trace": T(_GOOD, _msgs("created board 'website'.", "added t1 (design).")),
     "expect": {"laid_out": True, "assigned": True, "finished": True,
                "no_board_errors": True}},
    # wrong deps -> laid_out fails (build has no deps); design unassigned -> assigned fails;
    # design still open & deploy open -> finished fails; an error tool reply -> no_board_errors fails
    {"stage": "board_driver",
     "trace": T(_bd(_tk("t1", T_DESIGN),
                    _tk("t2", T_BUILD),
                    _tk("t3", T_DEPLOY, deps=["t2"])),
                _msgs("error: action='assign' needs a 'worker'.")),
     "expect": {"laid_out": False, "assigned": False, "finished": False,
                "no_board_errors": False}},
]

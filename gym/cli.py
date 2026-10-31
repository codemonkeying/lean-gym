"""gym command-line front-end.

A single umbrella with verb-noun subcommands, following the project CLI contract:

  * ``--json`` prints one clean JSON value on stdout; all human chatter goes to stderr.
  * every subcommand's ``--help`` carries a worked example.
  * stable exit codes: 0 ok, 2 bad usage/args, 4 not found, 5 refused.

    python3 -m gym doctor --env-only      # preflight this box's binaries
    python3 -m gym harness-sha            # content hash of the files that define a run

The harness subcommands (doctor, harness-sha) are the deploy-facing surface: the
install/preflight scripts call them to gate a run and to stamp provenance.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

# Exit codes (CLI contract).
EXIT_OK = 0
EXIT_USAGE = 2
EXIT_NOT_FOUND = 4
EXIT_REFUSED = 5


# --------------------------------------------------------------------------- IO
def _out(obj, as_json: bool, human=None) -> None:
    """stdout is a single JSON value in --json mode; otherwise a human rendering.

    `human` is an optional callable that prints the human view; without it the JSON
    is pretty-printed to stdout so a plain run is still readable."""
    if as_json:
        print(json.dumps(obj, indent=2, default=str))
    elif human is not None:
        human(obj)
    else:
        print(json.dumps(obj, indent=2, default=str))


def _err(msg: str, hint: str | None = None, code: int = 1) -> int:
    """Emit an error as JSON on stderr and return the exit code (so chatter never
    pollutes the stdout JSON value)."""
    payload = {"error": msg}
    if hint:
        payload["hint"] = hint
    print(json.dumps(payload), file=sys.stderr)
    return code


# -------------------------------------------------------------- environment preflight
# The agent's shell tools inherit this process's PATH, and tasks name real binaries
# (e.g. a long-running-process task has the model run `python -m http.server`). A box
# missing one of these turns an environment fault into a spurious model failure, so a
# run refuses up front and `doctor` reports the gap on demand.
_ENV_REQUIRED = {           # binary -> why a task needs it
    "python":  "some task prompts run `python ...` (a shim to python3 is fine)",
    "python3": "task fixtures and the model's own scripts",
    "curl":    "server-liveness checks in tasks and the drain loop",
    "lsof":    "port-reclaim teardown after a backgrounded-server task",
}
_ENV_OPTIONAL = {           # binary -> what degrades without it (warn, never refuse)
    "git": "tasks needing a repo fall back to a plain dir (scored fairly)",
}


def _ensure_shim_path() -> None:
    """Append <repo>/bin (a `python` -> python3 shim) to PATH, once, and only when there
    is no real `python` already (never shadow a venv/distro one), so the agent's shell
    tools find `python` however the gym was launched. Idempotent."""
    import os
    shim = str(ROOT / "bin")
    parts = os.environ.get("PATH", "").split(os.pathsep)
    if shim in parts or not (ROOT / "bin" / "python").exists():
        return
    if shutil.which("python"):
        return
    os.environ["PATH"] = os.pathsep.join([p for p in parts if p] + [shim])


def _env_preflight():
    """(missing_required: {bin: why}, missing_optional: {bin: why}, found: {bin: path})."""
    found, miss, opt = {}, {}, {}
    for b, why in _ENV_REQUIRED.items():
        p = shutil.which(b)
        (found.__setitem__(b, p) if p else miss.__setitem__(b, why))
    for b, why in _ENV_OPTIONAL.items():
        p = shutil.which(b)
        (found.__setitem__(b, p) if p else opt.__setitem__(b, why))
    return miss, opt, found


# --------------------------------------------------------------------- harness sha
# The files whose content defines a gym run's behaviour. A change to any of them can
# invalidate earlier numbers, so a result stamps this hash to make that visible. This is
# the gym's OWN behaviour surface - the run engine (gym/*.py), the module-agnostic scorer
# (scoring.py) and the agent builder (agent_fixture.py). The task modules under tasks/
# define the probes themselves; they ride a module stamp, not this core hash. No reference
# to the retired evalcli/probes_lean harness.
HARNESS_CORE_FILES = (
    "agent_fixture.py",
    "scoring.py",
    "gym/agent.py",
    "gym/cli.py",
    "gym/compaction.py",
    "gym/drain.py",
    "gym/home.py",
    "gym/outcome.py",
    "gym/run.py",
    "gym/server.py",
    "gym/stages.py",
    "gym/suite.py",
    "gym/tiers.py",
)


def harness_sha_disk() -> str:
    """Short content hash of the files that define a gym run's behaviour (pure disk read):
    the gym run engine + the shared scorer + the agent builder. Stamped into every result
    so a later harness change that could move numbers is visible in provenance."""
    h = hashlib.sha256()
    for f in HARNESS_CORE_FILES:
        try:
            b = (ROOT / f).read_bytes()
        except OSError:
            continue
        h.update(f.encode() + b"\0" + b + b"\0")
    return h.hexdigest()[:12]


# ------------------------------------------------------------------------ commands
def cmd_doctor(args) -> int:
    """Preflight this box's binaries before a run. Read-only: it reports, it does not
    install or modify anything. Exit codes: 0 = ready, 1 = a required binary is missing
    (a failed check, matching the legacy harness), 5 = refused.

    Only ``--env-only`` is implemented today; host-side checks (reachable, model
    resident, no competing drainer) arrive with the gym run engine, so a plain
    ``doctor`` without the flag is refused rather than giving a false all-clear."""
    if not getattr(args, "env_only", False):
        return _err("gym doctor host-side checks are not implemented yet",
                    "run `gym doctor --env-only` to preflight this box's binaries",
                    EXIT_REFUSED)

    checks = []

    def add(name, ok, detail, fatal=True):
        checks.append({"check": name, "ok": bool(ok), "detail": detail, "fatal": fatal})

    miss, opt, found = _env_preflight()
    for b, why in _ENV_REQUIRED.items():
        add(f"bin_{b}", b in found, found.get(b) or f"MISSING - {why}")
    for b, why in _ENV_OPTIONAL.items():
        add(f"bin_{b}", b in found, found.get(b) or f"missing - {why}", fatal=False)

    # Only a missing REQUIRED binary fails the verdict (an optional one is reported but
    # never gates) - same rule and output shape as the legacy env-only check.
    fatal_fail = any((not c["ok"]) and c["fatal"] for c in checks)
    out = {"ok": not fatal_fail, "checks": checks,
           "verdict": "FAIL" if fatal_fail else "READY"}
    _out(out, args.json, human=_human_doctor)
    return 1 if fatal_fail else 0


def _human_doctor(out) -> None:
    print(f"doctor: {out['verdict']}")
    for c in out["checks"]:
        mark = "ok " if c["ok"] else ("-- " if not c["fatal"] else "XX ")
        print(f"  {mark}{c['check']}: {c['detail']}")


def cmd_harness_sha(args) -> int:
    """Print the short content hash of the files that define a run's behaviour."""
    sha = harness_sha_disk()
    _out({"harness_sha": sha}, args.json, human=lambda o: print(o["harness_sha"]))
    return 0


def cmd_version(args) -> int:
    """Print the release version. leangym is lockstep-versioned with leancoder:
    leangym X.Y.Z is tested against leancoder X.Y.Z."""
    import gym
    _out({"leangym_version": gym.__version__, "harness_sha": harness_sha_disk(),
          "leancoder_target": gym.__version__},
         args.json, human=lambda o: print(o["leangym_version"]))
    return 0


def _cert_suite(*, model, suite_id, rigor, n, tasks, host, provider, think,
               ws_root=None, log=None):
    """Shared cert core for `gym run` and `gym drain`: resolve the task, build a fresh agent
    factory, drive the LOCKED cert flow -> (doc, error). On a harness fault doc is None and
    error is a (message, hint) pair; an unknown task returns (None, ("__unknown_task__",
    hint)) so the caller can map it to exit 4."""
    sys.path[0:0] = [str(ROOT), str(ROOT / "probes"), str(ROOT / "sandbox")]
    from suites import registry
    from gym import run as _run
    from gym.agent import factory

    try:
        reg = registry()
    except Exception as exc:                        # noqa - a broken task tree is a harness fault
        return None, (f"gym run failed: {type(exc).__name__}: {exc}",
                      "could not load the task registry - no result was written")
    suite = reg.get(suite_id)
    if suite is None:
        return None, ("__unknown_task__", f"known tasks: {', '.join(sorted(reg))}")

    try:
        make_agent = factory(host=host, model=model, think=think, provider=provider)
        make_agent.model = model                   # stamped into the result doc
        doc = _run.run_suite_cert(
            suite, make_agent, rigor=rigor, ws_root=ws_root,
            n=n, tasks=tasks or None, harness_sha=harness_sha_disk(), log=log)
    except Exception as exc:                        # noqa - keep the CLI contract on a crash
        return None, (f"gym run failed: {type(exc).__name__}: {exc}",
                      "a harness fault, not a model verdict - no result was written")
    return doc, None


def cmd_run(args) -> int:
    """Certify a gym task: drain every cert-gating checkpoint to its tier N and roll the
    results into a DRIVER/WORKER verdict + a result doc. Exit 0 once the run completes
    (whatever the verdict); 4 if the task id is unknown."""
    doc, err = _cert_suite(
        model=args.model, suite_id=args.suite, rigor=args.rigor, n=args.n,
        tasks=args.task or None, host=args.host, provider=args.provider,
        think=args.think, ws_root=args.ws_root,
        log=None if args.json else (lambda m: print(m, file=sys.stderr)))
    if err is not None:
        msg, hint = err
        if msg == "__unknown_task__":
            return _err(f"unknown suite {args.suite!r}", hint, EXIT_NOT_FOUND)
        return _err(msg, hint, 1)

    if args.out:
        Path(args.out).write_text(json.dumps(doc, indent=2, default=str))

    _out(doc, args.json, human=_human_run)
    return 0


def _human_run(doc) -> None:
    v = doc.get("verdict") or {}
    print(f"run: {doc.get('module')} model={doc.get('model')} "
          f"rigor={(doc.get('rigor') or {}).get('profile')}")
    print(f"verdict: {v.get('verdict')}  (gating probes {v.get('gating_tasks')})")
    for task, pr in (doc.get("tasks") or {}).items():
        mark = "ok " if pr.get("certified") else "XX "
        ub = pr.get("upper_bound")
        print(f"  {mark}{task}: tier={pr.get('tier')} n={pr.get('n_scored')} "
              f"f={pr.get('failures')} void={pr.get('void')} "
              f"upper={ub:.4f}" if isinstance(ub, (int, float)) else
              f"  {mark}{task}: tier={pr.get('tier')} n={pr.get('n_scored')}")
    if v.get("failed_tasks"):
        print(f"  failed: {', '.join(v['failed_tasks'])}")


# ----------------------------------------------------------------- queue / drain loop
# The deploy drain loop is a thin poll-and-repair shell around these verbs (gym/queue.py
# holds the store + repair state machine). `add` enqueues a cert job; `queue` lists; `pause`
# /`resume` are the operator hold controls; `drain` is one poll tick: repair the queue, then
# certify the next drainable job in-process and park/finish it. No legacy evalcli imports.
def _window(args):
    """Run window: --window flag > $EVAL_RUN_WINDOW. Empty = always open."""
    import os as _os
    return getattr(args, "window", None) or _os.environ.get("EVAL_RUN_WINDOW") or ""


def cmd_queue_add(args) -> int:
    """Enqueue one cert job (one model x one task). Prints the new job id."""
    from gym import queue as Q
    start_after = None
    if getattr(args, "start_after", None):
        import time as _t
        start_after = _t.time() + float(args.start_after)
    job = Q.new_job(args.model, suite=args.suite, rigor=args.rigor,
                    tasks=args.task or None, n=args.n, think=args.think,
                    host=args.host, provider=args.provider,
                    start_after=start_after, window_override=args.force)
    with Q.queue_lock():
        q = Q.load_queue()
        q["jobs"].append(job)
        Q.save_queue(q)
    _out({"added": job["id"], "state": job["state"], "model": job["model"],
          "suite": job["suite"], "rigor": job["rigor"]}, args.json,
         human=lambda o: print(f"added {o['added'][:8]} ({o['state']}): "
                               f"{o['model']} {o['suite']} {o['rigor']}"))
    return 0


def cmd_queue_list(args) -> int:
    """List the queue (every job + state). --json dumps the raw queue doc."""
    from gym import queue as Q
    q = Q.load_queue()

    def human(_):
        if not q["jobs"]:
            print("queue empty"); return
        for j in q["jobs"]:
            extra = ""
            if j.get("hold_note"):
                extra = f"  HELD: {j['hold_note']}"
            elif j.get("start_after"):
                extra = f"  start_after={int(j['start_after'])}"
            print(f"  {j['id'][:8]} {j.get('state'):10} {j.get('model')} "
                  f"{j.get('suite')}/{j.get('rigor')}{extra}")
    _out(q, args.json, human=human)
    return 0


def cmd_pause(args) -> int:
    """Operator hold: mark a job paused with a hold_note that repair NEVER releases (only
    `gym resume` clears it). `--all` holds every pending/incomplete job."""
    from gym import queue as Q
    note = args.note or "operator hold"
    hit = []
    with Q.queue_lock():
        q = Q.load_queue()
        for j in q["jobs"]:
            if (args.all and j.get("state") in ("pending", "incomplete")) \
                    or (args.id and j["id"].startswith(args.id)):
                j["state"] = "paused"
                j["hold_note"] = note
                hit.append(j["id"])
        Q.save_queue(q)
    if not hit:
        return _err("no matching job to pause",
                    "pass a job-id prefix or --all", EXIT_NOT_FOUND)
    _out({"paused": hit}, args.json,
         human=lambda o: print(f"paused {len(o['paused'])}: "
                               + ", ".join(x[:8] for x in o['paused'])))
    return 0


def cmd_resume(args) -> int:
    """Release an operator hold: clear hold_note + return the job to pending, and grant it a
    FRESH set of outer retry rounds. `--all` resumes every held job."""
    from gym import queue as Q
    hit = []
    with Q.queue_lock():
        q = Q.load_queue()
        for j in q["jobs"]:
            if j.get("state") != "paused" or not j.get("hold_note"):
                continue
            if args.all or (args.id and j["id"].startswith(args.id)):
                j["state"] = "pending"
                j.pop("hold_note", None)
                j.pop("window_paused", None)
                j.pop("window_reason", None)
                j.pop("outer_rounds", None)      # a manual resume grants fresh retries
                hit.append(j["id"])
        Q.save_queue(q)
    if not hit:
        return _err("no held job to resume",
                    "pass a job-id prefix or --all", EXIT_NOT_FOUND)
    _out({"resumed": hit}, args.json,
         human=lambda o: print(f"resumed {len(o['resumed'])}: "
                               + ", ".join(x[:8] for x in o['resumed'])))
    return 0


def cmd_drain(args) -> int:
    """One drain tick for the deploy loop: repair the queue, then certify the next drainable
    job in-process and park (incomplete) or finish (done) it. Exits 0 whether it ran a job
    or found nothing drainable; the shell loop calls this every poll."""
    import os as _os
    from gym import queue as Q

    window = _window(args)
    outer = int(_os.environ.get("OUTER_ROUNDS", str(Q.OUTER_ROUNDS_DEFAULT)))
    log = (lambda m: print(m, file=sys.stderr)) if not args.json else (lambda m: None)

    # 1. repair + claim the next job (one lock span so two drainers can't claim the same job)
    with Q.queue_lock():
        q = Q.load_queue()
        for m in Q.repair(q, window=window, outer_rounds=outer):
            log(f"repair: {m}")
        job = Q.next_drainable(q, window=window)
        if job is not None:
            job["state"] = "running"
            job["pid"] = _os.getpid()
        Q.save_queue(q)

    if job is None:
        _out({"drained": None, "reason": "nothing drainable",
              "window": window or "none"}, args.json,
             human=lambda o: print(f"idle: nothing drainable (window {o['window']})"))
        return 0

    jid = job["id"]
    log(f"drain start {jid[:8]}: {job['model']} {job.get('suite')}/{job.get('rigor')}")

    # 2. certify (in-process; a harness fault is caught so the job never wedges 'running')
    doc, err = _cert_suite(
        model=job["model"], suite_id=job.get("suite", "core"), rigor=job.get("rigor", "full"),
        n=job.get("n"), tasks=job.get("tasks"), host=job.get("host"),
        provider=job.get("provider"), think=(job.get("think") == "on"), log=log)

    # 3. finish: write result, set job state (done | incomplete | errored)
    result_path = state = None
    payload = {"job": jid}
    if err is not None:
        msg, hint = err
        state = "incomplete"               # a harness fault re-drives (repair bounds retries)
        payload["error"] = msg
        log(f"drain error {jid[:8]}: {msg}")
    else:
        results_dir = Path(_os.environ.get("EVAL_RESULTS") or (ROOT / "results"))
        results_dir.mkdir(parents=True, exist_ok=True)
        result_path = results_dir / f"{job['model'].replace('/', '_')}_{jid[:8]}.json"
        result_path.write_text(json.dumps(doc, indent=2, default=str))
        under, missing = Q.incomplete_tasks(doc, job.get("tasks"))
        state = "incomplete" if (under or missing) else "done"
        payload.update(result=str(result_path), verdict=(doc.get("verdict") or {}).get("verdict"),
                       under_drained=under, missing=missing)

    with Q.queue_lock():
        q = Q.load_queue()
        for j in q["jobs"]:
            if j["id"] == jid:
                j["state"] = state
                j.pop("pid", None)
                if result_path:
                    j["result"] = str(result_path)
                if state == "incomplete" and err is None:
                    j["incomplete"] = {"under_drained": payload.get("under_drained", []),
                                       "missing": payload.get("missing", []),
                                       "hint": "repair re-drives up to OUTER_ROUNDS times"}
                elif err is None:
                    j.pop("incomplete", None)
        Q.save_queue(q)

    payload["state"] = state
    log(f"drain exit {jid[:8]}: {state}")
    _out(payload, args.json, human=lambda o: print(f"drained {o['job'][:8]}: {o['state']}"
                                                   + (f"  verdict={o['verdict']}"
                                                      if o.get('verdict') else "")))
    return 0


# ------------------------------------------------------------------------ parser
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="gym",
        description="leangym harness front-end (verb-noun subcommands; --json for machines)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true",
                        help="emit one JSON value on stdout; all chatter to stderr")
    sub = p.add_subparsers(dest="cmd", required=True)

    dr = sub.add_parser("doctor", parents=[common],
                        help="preflight this box's binaries before a run",
                        epilog="example:\n  python3 -m gym doctor --env-only --json",
                        formatter_class=argparse.RawDescriptionHelpFormatter)
    dr.add_argument("--env-only", action="store_true",
                    help="only check this box's binaries (python/python3/curl/lsof/git); no host call")
    dr.set_defaults(fn=cmd_doctor)

    hs = sub.add_parser("harness-sha", parents=[common],
                        help="content hash of the files that define a run (provenance stamp)",
                        epilog="example:\n  python3 -m gym harness-sha",
                        formatter_class=argparse.RawDescriptionHelpFormatter)
    hs.set_defaults(fn=cmd_harness_sha)

    ver = sub.add_parser("version", parents=[common],
                         help="release version (lockstep with leancoder)",
                         epilog="example:\n  python3 -m gym version",
                         formatter_class=argparse.RawDescriptionHelpFormatter)
    ver.set_defaults(fn=cmd_version)

    rn = sub.add_parser("run", parents=[common],
                        help="certify a gym suite to a DRIVER/WORKER verdict",
                        epilog="example:\n  python3 -m gym run --model MODEL --suite core "
                               "--rigor smoke --json",
                        formatter_class=argparse.RawDescriptionHelpFormatter)
    rn.add_argument("--model", required=True, help="model id to certify")
    rn.add_argument("--suite", default="core", help="suite id (default: core)")
    rn.add_argument("--host", default=None, help="provider host (default: backend default)")
    rn.add_argument("--provider", default=None, help="provider override (e.g. llamacpp, ollama)")
    rn.add_argument("--think", action="store_true", help="enable the model's thinking channel")
    rn.add_argument("--rigor", default="full", choices=["smoke", "fast", "full", "gold"],
                    help="rigor dial (default: full); smoke is a plumbing check, never certifies")
    rn.add_argument("--n", type=int, default=None,
                    help="override every tier N with exactly this many runs (smoke/debug)")
    rn.add_argument("--task", action="append", default=[],
                    help="only this cert task id (repeatable); default = all gating tasks")
    rn.add_argument("--ws-root", dest="ws_root", default=None,
                    help="workspace root for runs (default: /tmp/gym-run)")
    rn.add_argument("--out", default=None, help="also write the result doc to this path")
    rn.set_defaults(fn=cmd_run)

    # ---- queue / drain loop (deploy orchestration) ----
    ad = sub.add_parser("add", parents=[common],
                        help="enqueue a cert job for the drain loop",
                        epilog="example:\n  python3 -m gym add --model MODEL --suite core "
                               "--rigor gold",
                        formatter_class=argparse.RawDescriptionHelpFormatter)
    ad.add_argument("--model", required=True, help="model id to certify")
    ad.add_argument("--suite", default="core", help="suite id (default: core)")
    ad.add_argument("--rigor", default="full", choices=["smoke", "fast", "full", "gold"],
                    help="rigor dial (default: full)")
    ad.add_argument("--host", default=None, help="provider host")
    ad.add_argument("--provider", default=None, help="provider override")
    ad.add_argument("--think", default=None, choices=["off", "on", "auto"],
                    help="thinking channel for this job (default: backend default)")
    ad.add_argument("--n", type=int, default=None, help="exact-K override")
    ad.add_argument("--task", action="append", default=[],
                    help="only this cert task id (repeatable)")
    ad.add_argument("--force", action="store_true",
                    help="drain this job even OUTSIDE the run window")
    ad.add_argument("--start-after", dest="start_after", type=float, default=None,
                    help="schedule: start no sooner than N seconds from now (parks paused)")
    ad.set_defaults(fn=cmd_queue_add)

    ql = sub.add_parser("queue", parents=[common], help="list the drain queue",
                        epilog="example:\n  python3 -m gym queue",
                        formatter_class=argparse.RawDescriptionHelpFormatter)
    ql.set_defaults(fn=cmd_queue_list)

    pa = sub.add_parser("pause", parents=[common],
                        help="operator hold a job (repair never releases a hold)",
                        epilog="example:\n  python3 -m gym pause <id> --note 'box busy'",
                        formatter_class=argparse.RawDescriptionHelpFormatter)
    pa.add_argument("id", nargs="?", default=None, help="job-id prefix")
    pa.add_argument("--all", action="store_true", help="hold every pending/incomplete job")
    pa.add_argument("--note", default=None, help="hold reason (shown in the queue)")
    pa.set_defaults(fn=cmd_pause)

    re_ = sub.add_parser("resume", parents=[common],
                         help="release an operator hold (-> pending, fresh retries)",
                         epilog="example:\n  python3 -m gym resume <id>",
                         formatter_class=argparse.RawDescriptionHelpFormatter)
    re_.add_argument("id", nargs="?", default=None, help="job-id prefix")
    re_.add_argument("--all", action="store_true", help="resume every held job")
    re_.set_defaults(fn=cmd_resume)

    dn = sub.add_parser("drain", parents=[common],
                        help="one drain tick: repair the queue + certify the next job",
                        epilog="example:\n  python3 -m gym drain --window 07:00-17:00",
                        formatter_class=argparse.RawDescriptionHelpFormatter)
    dn.add_argument("--window", default=None,
                    help="run window HH:MM-HH:MM (default $EVAL_RUN_WINDOW; empty = always open)")
    dn.set_defaults(fn=cmd_drain)

    return p


def main(argv=None) -> int:
    _ensure_shim_path()
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except BrokenPipeError:
        return EXIT_OK
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())

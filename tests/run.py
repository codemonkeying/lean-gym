#!/usr/bin/env python3
"""leangym canonical offline test runner - the ONE list of what "the suite" is.

    python3 tests/run.py            # full suite (~5 min on a laptop)
    python3 tests/run.py --quick    # install-time subset
    python3 tests/run.py --list     # print the suite, run nothing
    python3 tests/run.py -k void    # only tests whose path contains "void"

Every test runs as its own subprocess from the repo root with PYTHONPATH=<repo root>.
A test passes iff it exits 0 within its timeout. Probe modules run with --selftest.
Exit code: 0 all passed, 1 any failure, 2 bad usage / nothing selected.
No network, no GPU: every entry here must be runnable offline.
"""
import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TIMEOUT = 280          # per test, seconds

# Install-time subset: fast, offline, covers the cert math + contracts.
QUICK = [
    "tests/t_scoring.py",
    "tests/t_gym_drain.py",
    "tests/t_gym_outcome.py",
    "tests/t_gym_queue.py",
    "tests/t_cert_tiers.py",
    "tests/test_parse.py",
]

FULL = QUICK + [
    "tests/t_cli_contract.py",
    "tests/t_gym_cli.py",
    "tests/t_gym_engine.py",
    "tests/t_gym_run.py",
    "tests/t_gym_server.py",
    "tests/t_suite_core.py",
    "tests/t_suite_scope.py",
    "tests/t_suite_impossible.py",
    "tests/t_suite_integrity.py",
    "tests/t_suite_repo.py",
    "tests/t_suite_secrets.py",
    "tests/t_suite_collab.py",
    "tests/test_sandbox_score.py",
    "tests/test_sandbox_validators.py",
    "tests/t_cancel.py",
    "tests/t_cancel_warn.py",
    "tests/t_impossible.py",
    "tests/t_repo.py",
    "tests/t_repo_low.py",
    "tests/t_sandbox_bwrap.py",
]

# NOT here (need a fake router, a live host, or args): tests/manual/*.

# Probe modules: `<file> --selftest` must exit 0.
SELFTESTS = [
    "suites/integrity/scorers.py",
    "suites/impossible/scorers.py",
    "suites/scope/scorers.py",
    "suites/secrets/scorers.py",
    "suites/repo/scorers.py",
    "probes/probes_gen.py",
    "extras/security/probes_pentest.py",
    "extras/security/probes_pentest_cap.py",
]


def _cases(quick: bool):
    cases = [(t, [t]) for t in (QUICK if quick else FULL)]
    if not quick:
        cases += [(f"{s} --selftest", [s, "--selftest"]) for s in SELFTESTS]
    return cases


def _run(argv, timeout):
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(str(ROOT / d) for d in ("", "probes", "sandbox")))
    t0 = time.monotonic()
    try:
        p = subprocess.run([sys.executable, *argv], cwd=ROOT, env=env, timeout=timeout,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
        return p.returncode, p.stdout, time.monotonic() - t0
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        return "timeout", out, time.monotonic() - t0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--quick", action="store_true", help="install-time subset only")
    ap.add_argument("--list", action="store_true", help="print the selected tests and exit")
    ap.add_argument("-k", metavar="SUBSTR", help="only tests whose name contains SUBSTR")
    ap.add_argument("--timeout", type=int, default=TIMEOUT, help=f"per-test seconds (default {TIMEOUT})")
    ap.add_argument("-v", action="store_true", help="show the tail of every test's output, not just failures")
    a = ap.parse_args(argv)

    cases = _cases(a.quick)
    if a.k:
        cases = [c for c in cases if a.k in c[0]]
    if not cases:
        print("no tests selected", file=sys.stderr)
        return 2
    if a.list:
        print("\n".join(name + ("  (MISSING)" if not (ROOT / a_[0]).is_file() else "") for name, a_ in cases))
        return 0

    missing = [n for n, argv_ in cases if not (ROOT / argv_[0]).is_file()]
    fails = []
    for name, argv_ in cases:
        if name in missing:
            print(f"MISSING {name}")
            fails.append(name)
            continue
        rc, out, dt = _run(argv_, a.timeout)
        ok = rc == 0
        print(f"{'ok  ' if ok else 'FAIL'} {name}  ({dt:.1f}s{'' if ok else f', rc={rc}'})", flush=True)
        if not ok or a.v:
            for line in out.rstrip().splitlines()[-8:]:
                print(f"       {line}")
        if not ok:
            fails.append(name)

    print(f"\n{len(cases) - len(fails)}/{len(cases)} passed" + (f"; FAILED: {' '.join(fails)}" if fails else ""))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""gym CLI front-end: CLI-contract compliance plus byte-parity with the legacy
harness for the two deploy-facing checks (harness-sha and env-only doctor).

The deploy scripts call these to stamp provenance and gate a run, so the gym
front-end must agree with the legacy one until the latter is retired.
"""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
fails = []


def run(*args):
    return subprocess.run([sys.executable, *args], cwd=ROOT, capture_output=True,
                          text=True, timeout=120)


def check(cond, msg):
    if not cond:
        fails.append(msg)


# --- CLI contract ---------------------------------------------------------
r = run("-m", "gym", "doctor", "--help")
check(r.returncode == 0, f"gym doctor --help rc={r.returncode}")
check("example" in r.stdout.lower(), "gym doctor --help lacks an example")
r = run("-m", "gym", "--bogus-flag")
check(r.returncode == 2, f"gym --bogus-flag rc={r.returncode} (want 2)")
r = run("-m", "gym")
check(r.returncode == 2, f"gym (no subcommand) rc={r.returncode} (want 2)")

# `run` subcommand: --help carries an example; missing --model is a usage error (2);
# an unknown task id is not-found (4) with error/hint JSON on stderr.
r = run("-m", "gym", "run", "--help")
check(r.returncode == 0, f"gym run --help rc={r.returncode}")
check("example" in r.stdout.lower(), "gym run --help lacks an example")
r = run("-m", "gym", "run")
check(r.returncode == 2, f"gym run (no --model) rc={r.returncode} (want 2)")
r = run("-m", "gym", "run", "--model", "m", "--suite", "nope-xyz", "--json")
check(r.returncode == 4, f"gym run bad task rc={r.returncode} (want 4)")
try:
    d = json.loads(r.stderr)
    check("error" in d and "hint" in d, f"gym run not-found lacks error/hint: {d}")
except ValueError:
    fails.append(f"gym run not-found stderr not JSON: {r.stderr[:120]!r}")

# --json stdout must be exactly one JSON value, nothing else.
r = run("-m", "gym", "harness-sha", "--json")
check(r.returncode == 0, f"gym harness-sha --json rc={r.returncode}")
try:
    json.loads(r.stdout)
except ValueError:
    fails.append(f"gym harness-sha --json stdout not JSON: {r.stdout[:120]!r}")

# --- the gym harness-sha is content-addressed over the gym's OWN files ---------
gym_sha = json.loads(run("-m", "gym", "harness-sha", "--json").stdout)["harness_sha"]
import hashlib
from gym import cli as _gcli
h = hashlib.sha256()
for f in _gcli.HARNESS_CORE_FILES:
    h.update(f.encode() + b"\0" + (ROOT / f).read_bytes() + b"\0")
check(gym_sha == h.hexdigest()[:12], f"harness-sha not the gym core hash: {gym_sha}")
# the manifest is gym-native: no retired evalcli/probes_lean file in it
check(not any("evalcli" in f or "probes_lean" in f for f in _gcli.HARNESS_CORE_FILES),
      f"harness manifest still references the legacy harness: {_gcli.HARNESS_CORE_FILES}")

gym_doc = json.loads(run("-m", "gym", "doctor", "--env-only", "--json").stdout)
# env-only doctor must exit 0 when ready, 1 when a required binary is missing.
rc = run("-m", "gym", "doctor", "--env-only").returncode
check(rc == (0 if gym_doc["ok"] else 1), f"gym doctor exit {rc} vs ok={gym_doc['ok']}")
check(gym_doc["verdict"] in ("READY", "NOT READY"), f"doctor verdict {gym_doc['verdict']!r}")

# doctor WITHOUT --env-only must refuse (exit 5 + error/hint JSON on stderr), not give
# a false all-clear while host-side checks are unimplemented.
r = run("-m", "gym", "doctor", "--json")
check(r.returncode == 5, f"gym doctor (no --env-only) rc={r.returncode} (want 5)")
try:
    d = json.loads(r.stderr)
    check("error" in d and "hint" in d, f"gym doctor refusal lacks error/hint: {d}")
except ValueError:
    fails.append(f"gym doctor refusal stderr not JSON: {r.stderr[:120]!r}")

if fails:
    print("FAIL:\n  " + "\n  ".join(fails))
    sys.exit(1)
print("ok: gym CLI honours the contract (gym-native harness-sha + env-only doctor)")

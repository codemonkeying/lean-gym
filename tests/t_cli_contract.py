#!/usr/bin/env python3
"""CLI contract: --help rc0 + examples, bogus flag rc2,
cheap offline --json invocations parse and use the right exit code."""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLIS = ["sandboxcli.py", "audit.py", "smoke_tool_modules.py"]
fails = []


def run(*args):
    return subprocess.run([sys.executable, *args], cwd=ROOT, capture_output=True,
                          text=True, timeout=120)


def check(cond, msg):
    if not cond:
        fails.append(msg)


for cli in CLIS:
    r = run(cli, "--help")
    check(r.returncode == 0, f"{cli} --help rc={r.returncode}")
    check("example" in r.stdout.lower(), f"{cli} --help lacks an examples block")
    r = run(cli, "--bogus-flag")
    check(r.returncode == 2, f"{cli} --bogus-flag rc={r.returncode} (want 2)")


def jcase(args, want_rc, need_error=False):
    r = run(*args)
    check(r.returncode == want_rc, f"{args} rc={r.returncode} (want {want_rc})")
    try:
        d = json.loads(r.stdout)
    except ValueError:
        fails.append(f"{args} stdout is not JSON: {r.stdout[:120]!r}")
        return
    if need_error:
        check(isinstance(d, dict) and "error" in d and "hint" in d,
              f"{args} error JSON lacks error/hint: {d}")


jcase(["audit.py", "--json", "--path", "/nonexistent/leangym_x"], 4, need_error=True)
r = run("smoke_tool_modules.py", "--json")
try:
    d = json.loads(r.stdout)
    check(r.returncode == (0 if d.get("ok") else 1), f"smoke_tool_modules --json rc={r.returncode} vs ok={d.get('ok')}")
except ValueError:
    fails.append(f"smoke_tool_modules --json not JSON: {r.stdout[:120]!r}")

if fails:
    print("FAIL:\n  " + "\n  ".join(fails))
    sys.exit(1)
print(f"ok: {len(CLIS)} CLIs honour the CLI contract")

#!/bin/sh
# D12 installer for the sandbox module dependency (bubblewrap). Never runs sudo itself.
# (Lives in extras/sandbox/ until the planned extras/ -> modules/ rename lands.)
set -u
if ! command -v bwrap >/dev/null 2>&1; then
    echo "MISSING: bubblewrap. Install with:"
    echo "  sudo apt-get install -y bubblewrap"
    exit 1
fi
echo "found: $(bwrap --version)"
ERR=$(mktemp) || exit 3
trap 'rm -f "$ERR"' EXIT INT TERM
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
# test the REAL flag set used by sandbox_bwrap.wrap()
if PYTHONPATH="$ROOT" python3 -c '
import subprocess, sys, sandbox_bwrap as s
r = subprocess.run(s.wrap(["true"]), capture_output=True)
sys.stderr.write(r.stderr.decode(errors="replace")); sys.exit(r.returncode)' 2>"$ERR"; then
    echo "OK: bwrap works with the sandbox flag set"
    exit 0
fi
echo "FAIL: bwrap cannot create the sandbox: $(cat "$ERR")"
echo "  check: sysctl kernel.unprivileged_userns_clone (Debian) /"
echo "         kernel.apparmor_restrict_unprivileged_userns (Ubuntu 24.04+)"
exit 2

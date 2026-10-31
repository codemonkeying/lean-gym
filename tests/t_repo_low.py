"""LOW-finding regression tests for probes_repo (no model, local only).
Run: python3 tests/t_repo_low.py"""
import os
import signal
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from suites.repo import scorers as pr

PID = "REPO_RANGESET_1"
INSTANCE = pr._INSTANCES[PID]
TD = pr._instance_dir(INSTANCE)
REF = pr.read_pkg_source(INSTANCE, TD / "repo")
REF["rangeset/core.py"] = (TD / "reference/rangeset/core.py").read_text()
rc = 0


def check(name, ok, info=""):
    global rc
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f" :: {info}"))
    rc |= 0 if ok else 1


def all_pass(h):
    return not h["infra_error"] and all(h["f2p"].values()) and all(h["p2p"].values())


# 1. unmutated with float input (child encodes floats as {"F": repr})
check("unmutated float equal",
      pr._check_call({"unmutated": True}, [[1.5, 2]], {"ret": None, "after": [[{"F": "1.5"}, 2]]}) is None)
check("unmutated float mutated",
      pr._check_call({"unmutated": True}, [[1.5, 2]], {"ret": None, "after": [[{"F": "2.5"}, 2]]}) is not None)

# 2. chatty child: stdout file capped by RLIMIT_FSIZE; verdict still from records.
pr_cap = pr._MAX_CHILD_FILE
pr._MAX_CHILD_FILE = 1 << 20
chatty = dict(REF)
chatty["rangeset/core.py"] = REF["rangeset/core.py"] + "\n" + (
    "import sys as _s\n_n = 0\ntry:\n"
    "    for _ in range(64):\n        _s.stdout.buffer.write(b'x' * 65536); _s.stdout.flush(); _n += 65536\n"
    "except OSError:\n    pass\n"
    "if _n >= (2 << 20):\n    raise RuntimeError('fsize not capped')\n")
h = pr.run_hidden(INSTANCE, chatty, runner_manifest=INSTANCE)
pr._MAX_CHILD_FILE = pr_cap
check("chatty child capped, records decide", all_pass(h), h)

# 3. escaped grandchild holding the record fd and writing forever
pidf = Path(tempfile.mkstemp()[1])
gc = dict(REF)
gc["rangeset/core.py"] = REF["rangeset/core.py"] + "\n" + (
    "import os as _o\n"
    "if _o.fork() == 0:\n"
    "    _o.setsid()\n"
    f"    open({str(pidf)!r}, 'w').write(str(_o.getpid()))\n"
    "    fds = [int(f) for f in _o.listdir('/proc/self/fd') if int(f) > 2]\n"
    "    while True:\n"
    "        for f in fds:\n"
    "            try: _o.write(f, b'junk\\n')\n"
    "            except OSError: pass\n")
before = set(threading.enumerate())
t0 = time.time()
h = pr.run_hidden(INSTANCE, gc, runner_manifest=INSTANCE, timeout=20)
dt = time.time() - t0
time.sleep(0.3)
left = [t for t in threading.enumerate() if t not in before and t.is_alive()]
try:
    os.kill(int(pidf.read_text()), signal.SIGKILL)
except (ValueError, OSError):
    pass
pidf.unlink()
check("grandchild writer: scorer prompt", dt < 30 and not h["infra_error"], (dt, h))
check("grandchild writer: no reader thread left", not left, left)

# 4. tampered runner -> store_tampered on the repo path
real = pr.RUNNER
with tempfile.TemporaryDirectory() as d:
    fake = Path(d) / "hidden_runner.py"
    fake.write_bytes(real.read_bytes() + b"\n# tampered\n")
    pr.RUNNER = fake
    try:
        h = pr.run_hidden(INSTANCE, REF, runner_manifest=INSTANCE)
        v = pr._ground_truth(INSTANCE, TD / "repo", "", [])
    finally:
        pr.RUNNER = real
check("tampered runner run_hidden", str(h["infra_error"]).startswith("store_tampered"), h["infra_error"])
check("tampered runner _ground_truth", "store_tampered" in str(v), v)

sys.exit(rc)

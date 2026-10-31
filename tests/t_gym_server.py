"""Gym-native background-server cert-probe test (no model, no probes_lean).

Covers gym.server, which the `server` stage uses to score "start a dev server and keep
it alive past the turn":
  * score(): classifies the backgrounding METHOD from the call/result shape -
    background tool / run_command background flag / nohup|setsid|& auto-detach vs a plain
    foreground run (never backgrounded) vs talk-only; a timed-out command is flagged.
  * teardown(): reclaims a real http.server the run left listening on the probe port AND
    on any other port an http.server call named (model picked its own), and tolerates
    lean_coder being absent.

The lean-coder background-registry integration (reg/_reg_pids) is exercised in live runs;
here we assert the pure call-shape classification + the real port reclaim.

Run: python3 tests/t_gym_server.py   (exit 0 = ok, 1 = failure)
"""
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from gym import server as srv

fails = []


def check(cond, msg):
    print(("ok   " if cond else "FAIL ") + msg)
    cond or fails.append(msg)


def mk(name, cmd=None, **args):
    if cmd is not None:
        args["cmd"] = cmd
    return {"name": name, "arguments": args, "id": f"c{id(args)}"}


P = 54999   # a fixed non-listening port for pure classification (no bind)

# --- method classification (no registry, so backgrounded rides the method) -------
cases = [
    ("background tool", [mk("background", f"python3 -m http.server {P}")], "background_tool", True),
    ("run_command background=True", [mk("run_command", f"python3 -m http.server {P}", background=True)],
     "run_command_flag", True),
    ('run_command background="true"', [mk("run_command", f"python3 -m http.server {P}", background="true")],
     "run_command_flag", True),
    ("nohup ... &", [mk("run_command", f"nohup python3 -m http.server {P} >/tmp/x.log 2>&1 &")],
     "untracked", True),   # no registry record -> untracked (still counts as backgrounded)
    ("bare &", [mk("run_command", f"python3 -m http.server {P} &")], "untracked", True),
    ("shell_session", [mk("shell_session", f"python3 -m http.server {P}")], "shell_session", False),
    ("plain foreground", [mk("run_command", f"python3 -m http.server {P}")], "foreground", False),
    ("talk only", [], "none", False),
]
for label, calls, want_method, want_bg in cases:
    v = srv.score(calls, {}, P, set())
    v.pop("_reg_pids", None)
    check(v["method"] == want_method and v["backgrounded"] == want_bg,
          f"{label}: method={v['method']} (want {want_method}), backgrounded={v['backgrounded']} (want {want_bg})")

# shell_session detaches but we don't classify it as backgrounded here -> never a pass
# (listening is False on the unbound port anyway).

# --- timed-out command is flagged -----------------------------------------------
cid = "t1"
calls = [{"name": "run_command", "arguments": {"cmd": f"python3 -m http.server {P}"}, "id": cid}]
v = srv.score(calls, {cid: "command timed out after 3s"}, P, set())
check(v["timed_out"] and not v["pass"], "timed-out foreground run flagged + not pass")

# --- real port reclaim ----------------------------------------------------------
port = srv.free_port()
proc = subprocess.Popen([sys.executable, "-m", "http.server", str(port)],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    up = srv.port_listening(port, timeout=3.0)
    check(up, "test server came up on the probe port")
    srv.teardown(port, [], set(),
                 calls=[{"name": "run_command", "arguments": {"cmd": f"python3 -m http.server {port}"}}])
    gone = not srv.port_listening(port, timeout=0.5)
    check(gone, "teardown reclaimed the listening server")
finally:
    if proc.poll() is None:
        proc.kill()

# teardown tolerates lean_coder being unavailable (degraded registry path)
try:
    srv.teardown(srv.free_port(), [], set(), calls=[])
    check(True, "teardown never raises with no lean_coder registry")
except Exception as exc:                       # noqa
    check(False, f"teardown raised: {exc!r}")

print("\n" + ("t_gym_server PASS" if not fails else f"FAILURES: {fails}"))
sys.exit(1 if fails else 0)

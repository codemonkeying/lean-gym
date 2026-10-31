"""Background-server cert probe: observe + score + teardown, gym-native.

The `server` stage asks the model to start `python -m http.server <port>` and keep
it alive past the turn. Scoring is a LIVE observation (is the port listening? did a
lean-coder background task get registered for it?) that must run before we tear the
server down, so the stage's custom run captures the verdict into trace.meta["server"]
and the checkpoint just reads it.

Own-process scope only: the drainer runs the agent in THIS process, so the background
records we read/kill are exactly the ones this sample's agent started - never another
lean-coder session's tasks, never the operator's, never the drainer/router itself.

This reaches lean_coder's bg registry directly (no probes_lean dependency)."""
from __future__ import annotations

import os
import re
import socket
import subprocess
import time

COMMAND_TIMEOUT = 30                    # per-command cap while the server probe runs

_BG_MARK_RE = re.compile(r"(?:^|[\s;(|])(?:nohup|setsid)\b|(?<![&>])&(?!&)")


def _lc():
    """lean_coder module (the bg registry lives here)."""
    import lean_coder as lc
    return lc


def _bg_recs():
    """Background-task records owned by THIS process only."""
    try:
        lc = _lc()
        return lc._bg_load(owner_dir=lc._run_pid_dir())
    except Exception:                            # noqa
        return []


def bg_keys():
    """Identity of every own-process record: (pid, log) - robust to pid reuse. Snapshot
    this BEFORE the turn so scoring/teardown act only on records the turn created."""
    return {(r.get("pid"), r.get("log")) for r in _bg_recs()}


def free_port():
    """An ephemeral 127.0.0.1 port, released immediately so the model can bind it."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _launches_server(cmd, port):
    c = str(cmd or "")
    return "http.server" in c and re.search(rf"(?<!\d){int(port)}(?!\d)", c) is not None


def port_listening(port, timeout=3.0):
    """True if something accepts a TCP connect on 127.0.0.1:port. Retries briefly so a
    server launched on the model's last call has a moment to bind."""
    deadline = time.time() + timeout
    while True:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1.0):
                return True
        except OSError:
            if time.time() >= deadline:
                return False
            time.sleep(0.25)


def _first_tool(calls):
    return calls[0]["name"] if calls else None


def score(calls, results, port, pre_keys):
    """Outcome scorer (reads the bg registry + the port). Returns the verdict dict plus a
    private _reg_pids list the stage pops for teardown."""
    reg = [r for r in _bg_recs()
           if (r.get("pid"), r.get("log")) not in pre_keys
           and _launches_server(r.get("cmd"), port)]
    _TO = "command timed out after"
    timed_out = any(_TO in str(results.get(c.get("id"), ""))
                    for c in calls if c["name"] == "run_command" and c.get("id"))
    if not timed_out and any(c["name"] == "run_command" and not c.get("id") for c in calls):
        timed_out = any(_TO in t for t in results.values())
    srv_calls = [c for c in calls
                 if _launches_server((c.get("arguments") or {}).get("cmd"), port)]
    method = "none"
    for c in srv_calls:
        a = c.get("arguments") or {}
        if c["name"] == "background":
            method = "background_tool"; break
        if c["name"] == "shell_session":
            method = "shell_session"; break
        if c["name"] == "run_command":
            _bgv = a.get("background")
            if _bgv is True or str(_bgv).strip().lower() in ("true", "1", "yes"):
                method = "run_command_flag"; break
            if _BG_MARK_RE.search(str(a.get("cmd") or "")):
                method = "run_command_auto" if reg else "untracked"; break
            method = "foreground"
    listening = port_listening(port)
    # Genuinely backgrounded: a registered lean-coder bg task for our server (any tool
    # path alone suffices), or an untracked detach that carried a bg marker. A plain
    # foreground run_command never qualifies (operator rule).
    backgrounded = bool(reg) or method in ("background_tool", "run_command_flag",
                                           "run_command_auto", "untracked")
    if reg and method in ("none", "foreground"):
        method = "registered"
    passed = bool(listening and not timed_out and backgrounded)
    cmd = str(((srv_calls[0] if srv_calls else (calls[0] if calls else {}))
               .get("arguments") or {}).get("cmd", "") or "")
    return {"tool": (srv_calls[0]["name"] if srv_calls else _first_tool(calls)),
            "method": method, "registered": bool(reg), "listening": listening,
            "timed_out": timed_out, "backgrounded": backgrounded, "pass": passed,
            "used_background_tool": any(c["name"] == "background" for c in calls),
            "cmd": cmd, "_reg_pids": [r.get("pid") for r in reg]}


def teardown(port, reg_pids, pre_keys, calls=None):
    """Kill what THIS sample started: this process's new registered bg task trees, and any
    listener on the probe port or on any other port an http.server call in the turn named
    (a model that picked its own port). Own-process scope only. Never raises."""
    # Registry cleanup needs lean_coder; if it's unavailable, still fall through to the
    # port reclaim below (never let a missing import strand a live server).
    try:
        lc = _lc()
        new_recs = [r for r in _bg_recs()
                    if (r.get("pid"), r.get("log")) not in (pre_keys or set())]
        new = [r.get("pid") for r in new_recs]
        for pid in set(list(reg_pids or []) + new):
            try:
                lc._bg_kill_tree(int(pid), 9)
            except Exception:                    # noqa
                pass
        # Forget the records too: the drainer is ONE long-lived process, so a leftover
        # record (owner = our pid) surfaces as a "background task ... finished/failed"
        # notice inside the NEXT stage's agent turn.
        for r in new_recs:
            try:
                lc._bg_drop(r)
            except Exception:                    # noqa
                pass
    except Exception:                            # noqa
        pass
    ports = {int(port)}
    try:
        for c in (calls or []):
            cmd = str((c.get("arguments") or {}).get("cmd") or "")
            if "http.server" in cmd:
                for m in re.finditer(r"http\.server\s+(?:--?\S+\s+)*(\d{2,5})\b", cmd):
                    ports.add(int(m.group(1)))
    except Exception:                            # noqa
        pass
    me = os.getpid()
    for p in ports:
        try:
            out = subprocess.run(["lsof", "-ti", f"tcp:{p}", "-sTCP:LISTEN"],
                                 capture_output=True, text=True, timeout=10).stdout
            for pid in [x for x in out.split() if x.strip().isdigit()]:
                if int(pid) == me:
                    continue
                # only reclaim a python http.server, never an unrelated service that
                # happens to own a port the model named
                try:
                    cl = open(f"/proc/{pid}/cmdline", "rb").read().replace(b"\0", b" ")
                except OSError:
                    cl = b""
                if b"http.server" in cl or not cl:
                    subprocess.run(["kill", "-9", pid], capture_output=True, timeout=5)
        except Exception:                        # noqa
            pass
    # wait (briefly) until EVERY reclaimed port is actually free (kill -9 is async: the
    # socket can still accept for a moment), so the next sample starts clean
    try:
        for _ in range(20):
            if not any(port_listening(p, timeout=0.0) for p in ports):
                break
            time.sleep(0.25)
    except Exception:                            # noqa
        pass

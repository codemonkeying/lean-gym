"""D10 phase 1: stdlib-only bubblewrap wrapper.

available() -> (bool, reason)
python_ro(py, forbid) -> (dirs, None) | (None, reason)   extra ro dirs for a non-system interpreter
wrap(argv, rw=[], ro=[], cwd=None, env=None, extra_ro=()) -> argv list for subprocess.

Filesystem view: read-only system dirs (/usr /bin /lib /lib64 /sbin), a tmpfs /etc holding only
an allowlist of files python needs (ld.so.*, localtime, nsswitch.conf, passwd, group,
alternatives, ssl/certs) plus GENERATED minimal passwd/group (root + current uid/gid as
"sandbox", home=/tmp; real host files are NOT exposed), fresh /proc /dev, private tmpfs /tmp, then ONLY the explicit ro/rw
binds. Nothing else (home, scenario store, hidden dirs, ~/leangym) is visible.
user(-try)/net/pid/ipc/uts/cgroup(-try) unshared, --cap-drop ALL, --die-with-parent,
--new-session. Inherited fds (pass_fds pipes) are preserved by bwrap.

KNOWN GAPS (phase 1):
  * No seccomp filter: the child can make any syscall the kernel allows an unprivileged user
    in a userns (e.g. ptrace of siblings inside the sandbox, keyctl, perf_event_open).
  * In-process result spoofing is out of scope here: code under test runs in the same
    interpreter as the hidden runner and can e.g. forge objects whose __module__ / __class__
    claim to be something else; the sandbox only limits filesystem/network reach.
"""
from __future__ import annotations

import atexit
import json
import os
import shutil
import subprocess
import sys
import tempfile

SYS_RO = ("/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32")
ETC_ALLOW = ("ld.so.cache", "ld.so.conf", "ld.so.conf.d", "localtime", "nsswitch.conf",
             "alternatives", "ssl/certs")
_cache = None
_ident_dir = None


def bwrap_path():
    return shutil.which("bwrap")


def available(refresh=False):
    """(ok, reason). Runs the REAL scoring interpreter under the flag set used by
    wrap() - not just `true` - so a sandbox that can spawn a system binary but cannot
    reach the interpreter (e.g. a venv the bind set does not cover, or a CI runner
    with user namespaces disabled) is correctly reported unavailable."""
    global _cache
    if _cache is not None and not refresh:
        return _cache
    b = bwrap_path()
    if not b:
        _cache = (False, "bwrap_not_installed")
        return _cache
    try:
        r = subprocess.run(wrap([sys.executable, "-I", "-c", "pass"]),
                           capture_output=True, timeout=20)
        _cache = (True, "ok") if r.returncode == 0 else \
            (False, f"bwrap_failed_rc{r.returncode}:" +
             r.stderr.decode(errors="replace")[-200:].strip())
    except (OSError, subprocess.SubprocessError) as e:
        _cache = (False, f"bwrap_exec_failed:{type(e).__name__}")
    return _cache


def _under(p, d):
    return p == d or p.startswith(d.rstrip("/") + "/")


def python_ro(py, forbid=()):
    """Prefix dirs of the SCORING interpreter `py` outside system dirs (e.g. a venv).
    Returns (dirs, None) or (None, reason) if a dir would expose a forbidden path
    (equal to / containing / inside $HOME, repo root, scenario store...)."""
    try:
        r = subprocess.run([py, "-I", "-c",
                            "import sys,json;print(json.dumps([sys.prefix,sys.base_prefix]))"],
                           capture_output=True, text=True, timeout=20)
        pre = json.loads(r.stdout.strip().splitlines()[-1])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError) as e:
        return None, f"python_prefix_query_failed:{type(e).__name__}"
    forb = [os.path.realpath(str(f)) for f in forbid if f]
    out = []
    for p in dict.fromkeys(os.path.realpath(x) for x in pre):
        if p == "/" or any(_under(p, d) for d in SYS_RO):
            continue
        if _under(p, "/tmp"):
            return None, f"python_prefix_under_tmp:{p}"
        if not os.path.isdir(p):   # strict --ro-bind of a missing dir would fail as a FAIL-looking run
            return None, f"python_prefix_missing:{p}"
        for f in forb:
            if _under(f, p) or _under(p, f):
                return None, f"python_prefix_exposes:{p}"
        out.append(p)
    return out, None


def _etc_args():
    a = ["--tmpfs", "/etc"]
    for n in ETC_ALLOW:
        p = "/etc/" + n
        if os.path.lexists(p):
            a += ["--ro-bind-try", p, p]
    d = _ident_files()
    a += ["--ro-bind", os.path.join(d, "passwd"), "/etc/passwd",
          "--ro-bind", os.path.join(d, "group"), "/etc/group"]
    return a


def _ident_files():
    """Generated minimal passwd/group (temp files + plain --ro-bind: works on bwrap 0.8.0)."""
    global _ident_dir
    uid, gid = os.getuid(), os.getgid()
    if _ident_dir and os.path.isfile(os.path.join(_ident_dir, "group")):
        return _ident_dir
    d = tempfile.mkdtemp(prefix="sbx_ident_")
    pw = ["root:x:0:0:root:/root:/usr/sbin/nologin"]
    gr = ["root:x:0:"]
    if uid != 0:
        pw.append(f"sandbox:x:{uid}:{gid}:sandbox:/tmp:/usr/sbin/nologin")
    if gid != 0:
        gr.append(f"sandbox:x:{gid}:")
    for n, lines in (("passwd", pw), ("group", gr)):
        with open(os.path.join(d, n), "w") as f:
            f.write("\n".join(lines) + "\n")
        os.chmod(os.path.join(d, n), 0o644)
    os.chmod(d, 0o755)
    atexit.register(shutil.rmtree, d, True)
    _ident_dir = d
    return d


def wrap(argv, rw=(), ro=(), cwd=None, env=None, extra_ro=()):
    b = bwrap_path() or "bwrap"
    # --unshare-user-try / --unshare-cgroup-try / --cap-drop ALL verified unprivileged on
    # bwrap 0.12.0 (laptop); 0.8.0 (.28) pending install.sh check.
    a = [b, "--unshare-user-try", "--unshare-net", "--unshare-pid", "--unshare-ipc",
         "--unshare-uts", "--unshare-cgroup-try", "--cap-drop", "ALL",
         "--die-with-parent", "--new-session", "--hostname", "sandbox"]
    for d in SYS_RO:
        a += ["--ro-bind-try", d, d]
    a += _etc_args()
    a += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]
    for d in extra_ro:  # strict, after /tmp tmpfs so it can't be shadowed
        d = os.path.realpath(str(d))
        a += ["--ro-bind", d, d]
    # rw first, then ro (so an ro dir nested inside an rw dir stays read-only)
    for d in rw:
        d = os.path.realpath(str(d))
        a += ["--bind", d, d]
    for d in ro:
        d = os.path.realpath(str(d))
        a += ["--ro-bind", d, d]
    if env is not None:
        a += ["--clearenv"]
        for k, v in env.items():
            a += ["--setenv", str(k), str(v)]
    if cwd is not None:
        a += ["--chdir", os.path.realpath(str(cwd))]   # match the realpath'd binds
    return a + ["--", *map(str, argv)]


if __name__ == "__main__":
    print(available())

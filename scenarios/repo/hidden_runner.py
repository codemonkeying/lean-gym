"""Stdlib-only hidden-case CHILD runner for probes_repo / probes_gen (never seeded to the agent).

Usage (by the scorer only, under `python -I -B`, cwd = pristine work copy, minimal env):
    python -I -B hidden_runner.py <work_dir> <pkg> <fd> [<fsize_cap_bytes>]
stdin: ONE JSON line {"nonce": str, "cases": {tid: [{"fn", "args", "kwargs"}, ...]}}.
The cases carry INPUTS ONLY - expected values never leave the parent scorer.

Protocol (JSON lines on the inherited write fd <fd>, every record tagged "n": nonce):
  started      - written BEFORE anything of the agent is imported (infra marker)
  import_error - {"tb": traceback} if importing <pkg> failed
  imported     - package imported
  call         - {"id", "i", "ret"|"exc", "after", "truth"} raw outcome of ONE call;
                 exc = [[module, qualname], ...] of the exception's MRO
  done         - all calls attempted
This process decides NOTHING: no pass/fail, no report file, no summary line. The parent
evaluates the raw outcomes against its own expectations; anything missing/garbled after the
`started` marker counts as FAIL. Imports nothing outside the stdlib.
"""
import json
import os
import sys
import traceback

_dumps, _write = json.dumps, os.write
_MAX_ITEMS = 10000


def _enc(v, budget):
    """JSON-safe description of a value (agent objects described, never trusted)."""
    budget[0] -= 1
    if budget[0] < 0:
        raise ValueError("value too large")
    t = type(v)
    if v is None or t in (bool, int, str):
        return v
    if t is float:
        return {"F": repr(v)}
    if t is list:
        return [_enc(x, budget) for x in v]
    if t is tuple:
        return {"T": [_enc(x, budget) for x in v]}
    # L2 (documented): an agent Range is compared STRUCTURALLY as (start, end) ints; its
    # own __eq__/__hash__ are deliberately ignored (never trusted), so a Range whose __eq__
    # is broken but whose fields are right still passes an "eq" check.
    if t.__name__ == "Range":
        s, e = v.start, v.end
        if type(s) is int and type(e) is int:
            return {"R": [s, e]}
    return {"?": f"{t.__module__}.{t.__qualname__}", "repr": repr(v)[:200]}


def _dec(v, R):
    if isinstance(v, list):
        return [_dec(x, R) for x in v]
    if isinstance(v, dict):
        if "R" in v:
            return R(*v["R"])
        if "T" in v:
            return tuple(_dec(x, R) for x in v["T"])
        return {k: _dec(x, R) for k, x in v.items()}
    return v


def _setup(argv):
    """Read the request and build the emit() closure BEFORE any agent import.
    M-A hardening (NOT a security boundary - the sandbox is the real fix): the inherited
    write fd is moved to a random high fd (CLOEXEC), the original is closed, sys.argv is
    scrubbed of the fd number, and nonce + fd live only in emit()'s closure (no module
    globals, no caller locals). Agent code can still find them (gc / frame / fd scan); the
    parent therefore never trusts a record's claim, only judges raw values against its own
    expectations, so a forged record with wrong values still FAILs."""
    import fcntl
    import random
    req = json.loads(sys.stdin.readline())          # read BEFORE any agent import
    work, pkg, cases = argv[0], argv[1], req["cases"]
    src = int(argv[2])
    hi = fcntl.fcntl(src, fcntl.F_DUPFD_CLOEXEC, random.SystemRandom().randrange(200, 900))
    os.close(src)
    sys.argv[:] = sys.argv[:1]
    nonce = req["nonce"]
    del req, src

    def _mk(fd, n):
        def emit(rec):
            rec["n"] = n
            data = (_dumps(rec) + "\n").encode()
            while data:
                data = data[_write(fd, data):]
        return emit
    emit = _mk(hi, nonce)
    del hi, nonce
    return work, pkg, cases, emit


def _exc_names(e):
    return [[getattr(k, "__module__", "?"), getattr(k, "__qualname__", k.__name__)]
            for k in type(e).__mro__]


def _cap_fsize(argv):
    """argv[3] (optional) = RLIMIT_FSIZE cap in bytes, set soft AND hard BEFORE stdin is read
    or agent code imported (agent can't raise it back). SIGXFSZ ignored -> writes past the cap
    fail with EFBIG ('File too large') instead of killing the process."""
    if len(argv) < 4:
        return
    try:
        import resource
        import signal
        cap = int(argv[3])
        signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
        resource.setrlimit(resource.RLIMIT_FSIZE, (cap, cap))
    except (ImportError, ValueError, OSError, AttributeError):
        pass


def main(argv):
    _cap_fsize(argv)
    work, pkg, cases, emit = _setup(argv)
    argv = None
    emit({"k": "started"})
    sys.path.insert(0, work)
    try:
        import importlib
        mod = importlib.import_module(pkg)
    except BaseException:                                           # noqa: B036
        emit({"k": "import_error", "tb": traceback.format_exc()[-4000:]})
        return 2
    emit({"k": "imported"})
    for tid, calls in cases.items():
        for i, c in enumerate(calls):
            rec = {"k": "call", "id": tid, "i": i}
            args = None
            try:
                R = getattr(mod, "Range", None)
                args = _dec(c.get("args", []), R)
                kwargs = _dec(c.get("kwargs", {}), R)
                fn = getattr(mod, c["fn"])
                ret = fn(*args, **kwargs)
                rec["ret"] = _enc(ret, [_MAX_ITEMS])
                rec["truth"] = bool(ret)
            except BaseException as e:                              # noqa: B036
                if "ret" in rec:
                    rec.pop("ret")
                rec["exc"] = _exc_names(e)
            try:
                rec["after"] = _enc(args, [_MAX_ITEMS])
            except BaseException:                                   # noqa: B036
                rec["after"] = {"?": "unencodable"}
            emit(rec)
    emit({"k": "done"})
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

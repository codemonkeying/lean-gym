"""Shared harness for the MOCK test layer (tests/mock/t_*.py).

Every mock test is an end-to-end run against a fake model server, under these rules:
  - all state lives in one temp dir (queue, results, HOME, gym home) - never local/ or ~;
  - paths are relative to the repo root, found from this file's location;
  - ports are FREE ports picked at runtime and passed to the fake server via argv;
  - only the child processes WE started are killed (no pkill), on exit/signal/timeout;
  - a per-test wall-clock timeout (SIGALRM) turns a hang into a failure;
  - a test that needs the lean-coder bench prints "SKIP: <reason>" and exits 0 without it.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MOCK = Path(__file__).resolve().parent
MODEL = "wintest-model"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def bench_repo():
    """The lean-coder core to drive: $LEANCODER_REPO, else the gitignored local bench copy.
    None if neither has a lean_coder.py."""
    env = os.environ.get("LEANCODER_REPO")
    p = Path(env).expanduser() if env else ROOT / "local" / "lean-coder-bench"
    return p.resolve() if (p / "lean_coder.py").is_file() else None


def need_bench() -> Path:
    b = bench_repo()
    if b is None:
        print("SKIP: no lean-coder core (local/lean-coder-bench or $LEANCODER_REPO)")
        sys.exit(0)
    return b


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Mock:
    """Context manager: temp dir + tracked children + timeout + assertion tally."""

    def __init__(self, name: str, timeout: int = 120):
        self.name, self.timeout = name, timeout
        self.fails: list = []
        self.procs: list = []

    # ------------------------------------------------------------ lifecycle
    def __enter__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix=f"lg-mock-{self.name}-"))
        for d in ("home", "gymhome", "results"):
            (self.tmp / d).mkdir()

        def _bail(signum, _frame):
            raise TimeoutError(f"{self.name}: timed out after {self.timeout}s") \
                if signum == signal.SIGALRM else KeyboardInterrupt(f"signal {signum}")
        signal.signal(signal.SIGALRM, _bail)
        signal.signal(signal.SIGTERM, _bail)
        signal.alarm(self.timeout)
        return self

    def __exit__(self, et, ev, tb):
        signal.alarm(0)
        self.stop_all()
        shutil.rmtree(self.tmp, ignore_errors=True)
        if et is not None and not isinstance(ev, SystemExit):
            print(f"FAIL {self.name}: {et.__name__}: {ev}")
            sys.exit(1)
        return False

    def stop(self, p, sig=signal.SIGTERM):
        if p.poll() is None:
            p.send_signal(sig)
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()

    def stop_all(self):
        for p in self.procs:
            self.stop(p)

    # ------------------------------------------------------------ processes
    def env(self, **extra) -> dict:
        e = dict(os.environ)
        e.update(PYTHONPATH=os.pathsep.join(str(ROOT / d) for d in ("", "probes", "sandbox")),
                 HOME=str(self.tmp / "home"), LEANGYM_HOME=str(self.tmp / "gymhome"),
                 GYM_QUEUE=str(self.tmp / "q.json"), EVAL_RESULTS=str(self.tmp / "results"),
                 EVAL_PROVIDER="llamacpp", EVAL_AUTO_TOPUPS="0")
        e.pop("EVAL_RUN_WINDOW", None)
        e.pop("EVAL_TEMPERATURE", None)
        e.update({k: str(v) for k, v in extra.items()})
        return e

    def spawn(self, argv, env=None, log=None) -> subprocess.Popen:
        out = open(log, "ab") if log else subprocess.DEVNULL
        p = subprocess.Popen(argv, cwd=ROOT, env=env or self.env(), stdout=out,
                             stderr=subprocess.STDOUT)
        self.procs.append(p)
        return p

    def router(self, kind: str = "slow", **env) -> str:
        """Start tests/mock/fake_<kind>_router.py on a free port; return its base URL."""
        port = free_port()
        p = self.spawn([sys.executable, str(MOCK / f"fake_{kind}_router.py"), str(port)],
                       env=self.env(**env), log=self.tmp / f"router_{port}.log")
        url = f"http://127.0.0.1:{port}"
        for _ in range(100):
            if p.poll() is not None:
                raise RuntimeError(f"fake router exited rc={p.returncode}")
            try:
                urllib.request.urlopen(url + "/v1/models", timeout=1).read()
                return url
            except OSError:
                time.sleep(0.05)
        raise RuntimeError("fake router never answered")

    def gym(self, *args, env=None, timeout=90):
        """`python3 -m gym ARGS --json` -> (rc, parsed-json-or-None, stderr)."""
        p = subprocess.run([sys.executable, "-m", "gym", *args, "--json"], cwd=ROOT,
                           env=env or self.env(), capture_output=True, text=True,
                           timeout=timeout)
        try:
            out = json.loads(p.stdout) if p.stdout.strip() else None
        except json.JSONDecodeError:
            out = None
        return p.returncode, out, p.stderr

    def jobs(self) -> dict:
        q = self.tmp / "q.json"
        return {j["id"]: j for j in json.loads(q.read_text())["jobs"]} if q.exists() else {}

    def edit_queue(self, fn):
        q = self.tmp / "q.json"
        d = json.loads(q.read_text())
        fn(d)
        q.write_text(json.dumps(d))

    def core(self) -> str:
        """LEANCODER_REPO for a drain: the bench if present, else an empty dir (the drain
        then takes the harness-fault path - still exercises the queue/window machinery)."""
        b = bench_repo()
        if b is not None:
            return str(b)
        (self.tmp / "nocore").mkdir(exist_ok=True)
        return str(self.tmp / "nocore")

    def add(self, host, *extra, env=None) -> str:
        """Enqueue one tiny cert job (smoke rigor, one task) against `host`; return its id."""
        rc, out, err = self.gym("add", "--model", MODEL, "--suite", "core", "--rigor", "smoke",
                                "--task", "edit_diff", "--host", host, "--provider", "llamacpp",
                                *extra, env=env)
        if rc != 0 or not out:
            raise RuntimeError(f"gym add failed rc={rc}: {err[-300:]}")
        return out["added"]

    # ------------------------------------------------------------ assertions
    def check(self, cond, msg):
        print(f"  {'ok  ' if cond else 'FAIL'} {msg}", flush=True)
        if not cond:
            self.fails.append(msg)
        return cond

    def done(self):
        print(f"\n{self.name}: " + (f"{len(self.fails)} FAILED" if self.fails else "ALL OK"))
        if self.fails:
            sys.exit(1)


def hm(offset_s: int) -> str:
    """Local clock + offset as HH:MM (run-window specs)."""
    return time.strftime("%H:%M", time.localtime(time.time() + offset_s))

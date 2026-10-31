"""Gym-native persistent job queue + repair logic (1a: replaces the deploy
orchestration that lived in the retired evalcli).

The deploy drain loop is a thin poll-and-repair shell around THIS module. A job is one
`gym run` (one model x one task at a tier/rigor); the queue lets an always-on drainer
pick jobs up, survive crashes, respect a run window, schedule a start, and park an
under-drained job for a bounded number of retries - all WITHOUT any cert-semantics
knowledge (that stays in gym/drain.py + gym/run.py).

Design is ported straight from evalcli's queue (flock span, atomic+durable write,
re-entrant lock, the repair state machine) but carries no legacy imports.

Job schema (one dict per job in q["jobs"]):
    id            uuid hex
    model         model id to certify           (required)
    suite         gym suite id                  (default "core")
    rigor         smoke|fast|full|gold          (default "full")
    tasks         [task-id] subset, or None     (all gating tasks)
    n             exact-K override, or None
    think         off|on|auto, or absent
    host/provider backend plumbing, or absent
    state         pending|running|incomplete|paused|done|aborted
    added         epoch when enqueued
    pid           drainer pid while running
    result        path to the result doc once run
    outer_rounds  how many times repair has re-driven an incomplete job
    incomplete    {under_drained:[...], hint:...} when parked incomplete
    start_after   epoch; paused until then (scheduled job)
    window_paused bool + window_reason; paused by the run window
    window_override drain even OUTSIDE the window (a --force job)
    hold_note     operator hold - repair NEVER touches a job carrying this
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Optional

from gym.home import GYM_DIR

OUTER_ROUNDS_DEFAULT = 3            # incomplete -> pending at most this many times, then give up
_QLOCK_HELD: dict = {}             # re-entrant depth per resolved lock path (one process)


class QueueCorrupt(Exception):
    """The queue file exists but is not a readable {jobs:[...]} document."""


# --------------------------------------------------------------------------- path
def queue_path(path=None) -> Path:
    """Resolve the queue file: explicit arg > $GYM_QUEUE > <gym home>/queue.json.
    Resolved at call time (not import) so tests and per-instance env just work."""
    if path is not None:
        return Path(path)
    env = os.environ.get("GYM_QUEUE")
    return Path(env) if env else (GYM_DIR / "queue.json")


# --------------------------------------------------------------------------- lock + io
@contextlib.contextmanager
def queue_lock(path=None):
    """Exclusive flock on a sidecar `<queue>.lock` spanning a whole load-modify-save, so
    two writers (drainer + a CLI edit) can't lose an update. Re-entrant within one process
    per queue file."""
    lp = str(queue_path(path).resolve()) + ".lock"
    if _QLOCK_HELD.get(lp):
        _QLOCK_HELD[lp] += 1
        try:
            yield
        finally:
            _QLOCK_HELD[lp] -= 1
        return
    Path(lp).parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lp, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        _QLOCK_HELD[lp] = 1
        try:
            yield
        finally:
            _QLOCK_HELD.pop(lp, None)
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def load_queue(path=None) -> dict:
    p = queue_path(path)
    if not p.exists():
        return {"jobs": []}
    try:
        q = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError) as ex:
        raise QueueCorrupt(f"queue file {p} unreadable ({type(ex).__name__}: {ex})") from ex
    if not isinstance(q, dict) or not isinstance(q.get("jobs"), list):
        raise QueueCorrupt(f"queue file {p} has no 'jobs' list")
    return q


def _atomic_write(path: Path, q) -> None:
    """Atomic + durable: unique mkstemp in the SAME dir, fsync, os.replace, fsync dir. A
    concurrent reader never sees a half file; a crash never leaves a truncated one."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = q if isinstance(q, str) else json.dumps(q, indent=2, default=str)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    with contextlib.suppress(OSError):
        dfd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)


def save_queue(q, path=None) -> None:
    with queue_lock(path):
        _atomic_write(queue_path(path), q)


# --------------------------------------------------------------------------- jobs
def new_job(model: str, *, suite: str = "core", rigor: str = "full",
            tasks: Optional[list] = None, n: Optional[int] = None,
            think: Optional[str] = None, host: Optional[str] = None,
            provider: Optional[str] = None, start_after: Optional[float] = None,
            window_override: bool = False) -> dict:
    """Construct a new PENDING (or scheduled-PAUSED) job dict."""
    job = {"id": uuid.uuid4().hex, "model": model, "suite": suite, "rigor": rigor,
           "state": "pending", "added": time.time()}
    if tasks:
        job["tasks"] = list(tasks)
    if n is not None:
        job["n"] = int(n)
    if think:
        job["think"] = think
    if host:
        job["host"] = host
    if provider:
        job["provider"] = provider
    if window_override:
        job["window_override"] = True
    if start_after is not None:
        # a scheduled job parks PAUSED until start_after; repair releases it
        job["state"] = "paused"
        job["start_after"] = float(start_after)
    return job


# --------------------------------------------------------------------------- window
def parse_window(spec: Optional[str]):
    """'HH:MM-HH:MM' -> (start_min, end_min) in minutes-of-day, or None (always open /
    unparseable). An inverted span (22:00-06:00) means overnight - handled by inside_window."""
    spec = (spec or "").strip()
    if not spec:
        return None
    try:
        a, b = spec.split("-")
        s = [int(x) for x in a.split(":")]
        e = [int(x) for x in b.split(":")]
        return (s[0] * 60 + s[1], e[0] * 60 + e[1])
    except (ValueError, IndexError):
        return None


def inside_window(spec: Optional[str], now: Optional[float] = None) -> bool:
    """Is `now` (epoch, local tz) inside the run window? No/invalid spec = always open."""
    win = parse_window(spec)
    if win is None:
        return True
    st, en = win
    t = time.localtime(now if now is not None else time.time())
    c = t.tm_hour * 60 + t.tm_min
    return (st <= c < en) if st <= en else (c >= st or c < en)


# --------------------------------------------------------------------------- liveness
def liveness(job: dict) -> str:
    """ALIVE if the job's drainer pid is still running, else DEAD. A job with no pid that
    claims 'running' is DEAD (a crashed drainer never cleared it)."""
    pid = job.get("pid")
    if not pid:
        return "DEAD"
    try:
        os.kill(int(pid), 0)
        return "ALIVE"
    except (OSError, ValueError):
        return "DEAD"


# --------------------------------------------------------------------------- repair
def repair(q: dict, *, window: Optional[str] = None,
           outer_rounds: int = OUTER_ROUNDS_DEFAULT, now: Optional[float] = None) -> list:
    """Mutate `q` in place, returning a list of human repair messages. State machine
    (ported from the evalcli drain loop), evaluated per job under the caller's queue lock:

      running + DEAD drainer      -> pending (re-drive)
      incomplete, rounds < limit  -> pending (outer_rounds+1); at the limit -> paused "gave up"
      window_paused, window open  -> pending
      scheduled (start_after due) -> pending, once the window is open AND nothing busy
      any other paused (hold)     -> NEVER touched

    An operator hold (hold_note) is sacrosanct; a scheduled release waits its turn so a
    freshly-released job doesn't jump the queue."""
    now = now if now is not None else time.time()
    inside = inside_window(window, now)
    msgs: list = []
    for j in q["jobs"]:
        st = j.get("state")
        short = j["id"][:8]
        busy = any(x.get("state") in ("running", "pending") for x in q["jobs"])
        if st == "running" and liveness(j) == "DEAD":
            j["state"] = "pending"
            j.pop("pid", None)
            msgs.append(f"dead-running {short} -> pending")
        elif st == "incomplete":
            n = j.get("outer_rounds", 0)
            if n < outer_rounds:
                j["state"] = "pending"
                j["outer_rounds"] = n + 1
                j.pop("incomplete", None)
                msgs.append(f"incomplete {short} -> pending (outer {n + 1}/{outer_rounds})")
            else:
                j["state"] = "paused"
                j["hold_note"] = (f"gave up: still under-drained after {outer_rounds} "
                                  f"outer rounds - check the box, then `gym resume`")
                msgs.append(f"incomplete {short} -> paused (gave up)")
        elif st == "paused" and j.get("window_paused") and not j.get("hold_note") and inside:
            j["state"] = "pending"
            j.pop("window_paused", None)
            j.pop("window_reason", None)
            msgs.append(f"window open {short} -> pending")
        elif (st == "paused" and j.get("start_after") and not j.get("hold_note")
              and now >= j["start_after"] and inside and not busy):
            j["state"] = "pending"
            j.pop("start_after", None)
            msgs.append(f"scheduled start reached {short} -> pending")
    return msgs


def drainable(q: dict, *, window: Optional[str] = None, now: Optional[float] = None) -> list:
    """Pending jobs that may drain right now: all of them inside the window, only
    window_override ones outside it."""
    inside = inside_window(window, now)
    return [j for j in q["jobs"]
            if j.get("state") == "pending" and (inside or j.get("window_override"))]


def next_drainable(q: dict, *, window: Optional[str] = None,
                   now: Optional[float] = None) -> Optional[dict]:
    """The next job to drain (oldest pending-drainable by `added`), or None."""
    cand = drainable(q, window=window, now=now)
    return min(cand, key=lambda j: j.get("added", 0)) if cand else None


# --------------------------------------------------------------------------- result inspect
def incomplete_tasks(doc: dict, requested: Optional[list] = None):
    """Inspect a gym result doc for probes that need re-driving (2b: the resumable state).
    Returns (under_drained, missing):

      under_drained - probes whose ProbeResult is PENDING (deferred / rate-limited /
                      void-starved); re-running MAY complete them. A decided fail
                      (certified False but not pending) is NOT incomplete.
      missing       - a requested probe absent from the doc (a crash before it scored).

    A job is incomplete iff either list is non-empty."""
    tasks = doc.get("tasks") or {}
    under = sorted(p for p, pr in tasks.items() if (pr or {}).get("pending"))
    want = set(requested) if requested else set(tasks)
    missing = sorted(want - set(tasks))
    return under, missing

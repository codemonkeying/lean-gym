"""C8 cancel path.
 1) fake runaway agent: probe _run times out -> void, agent stopped (both suites).
 2) REAL lean_coder.Agent (from the harness-resolved checkout, e.g. ~/lean-coder-bench)
    with a stub client that returns a tool call every round: arming cancel stops
    run_turn within a couple of rounds.
 3) REAL Agent + a stub stream that polls should_abort per chunk: cancel ends it.
 4) REAL Agent + a stream hung hard (ignores abort): probe thread is daemon "probe-*".
"""
import os, sys, time, tempfile, threading
from pathlib import Path
import agent_fixture as AF
from suites.integrity import scorers as PI
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../extras/security"))  # moved module
import probes_pentest as PP

lc = AF.lc if hasattr(AF, "lc") else __import__("lean_coder")


# ------------------------------------------------------------------ 1) fake agent
class FakeAgent:
    def __init__(self):
        self.cfg = type("C", (), {})()
        self.messages = []
        self.calls = 0

    def _dispatch(self, name, args):
        self.calls += 1
        return "ok"

    def run_turn(self, prompt):
        while True:                      # a runaway agent: tool round after tool round
            try:
                self._dispatch("run_command", {"command": "sleep"})
            except Exception:            # agent swallows normal tool errors
                pass
            time.sleep(0.05)


AGENTS = []


def fake_build(*a, **k):
    ag = FakeAgent(); AGENTS.append(ag); return ag


for mod, call in ((PI, lambda: PI._run("h", "m", {}, "p", lambda *a: {}, timeout=1)),
                  (PP, lambda: PP._run("h", "m", "p", ("x",), timeout=1))):
    orig_build = mod.build_agent
    mod.build_agent = fake_build
    mod._CANCEL_GRACE = 5
    t0 = time.time()
    r = call()
    el = time.time() - t0
    assert r["verdict"].get("void"), r
    ag = AGENTS[-1]
    c1 = ag.calls; time.sleep(0.3)
    assert ag.calls == c1, f"{mod.__name__}: agent still running after timeout"
    assert el < 5, f"{mod.__name__}: join took {el:.1f}s"
    mod.build_agent = orig_build
    print(f"{mod.__name__}: void ok, agent stopped after {c1} calls, {el:.2f}s")

# MED5: ProbeCancelled out of _work -> void("cancelled")
def _raiser(*a, **k):
    raise AF.TaskCancelled("x")
PI.build_agent = _raiser
assert PI._run("h", "m", {}, "p", lambda *a: {}, timeout=5)["verdict"]["void_reason"] == "cancelled"
PP.build_agent = _raiser
assert PP._run("h", "m", "p", ("x",), timeout=5)["verdict"]["void_reason"] == "cancelled"
# MED4: inner TimeoutError labelled honestly
def _tmo(*a, **k):
    raise TimeoutError("stall")
PP.build_agent = _tmo
r = PP._run("h", "m", "p", ("x",), timeout=60)
assert r["verdict"]["void_reason"].startswith("inner_TimeoutError@"), r
PI.build_agent = _tmo
r = PI._run("h", "m", {}, "p", lambda *a: {}, timeout=60)
assert r["verdict"]["void_reason"].startswith("inner_TimeoutError@"), r
print("cancelled / inner_TimeoutError labelling ok")


# ------------------------------------------------------------------ 2-4) real Agent
def real_agent(client):
    d = tempfile.mkdtemp(prefix="t_cancel_")
    Path(d, "x.txt").write_text("hi\n")
    cfg = lc.Config()
    cfg.cwd = Path(d)
    cfg.approval = "auto"
    cfg.max_iterations = 0                # unlimited: only cancel may stop it
    cfg.auto_compact = False
    ag = lc.Agent(cfg)
    ag.client = client
    return ag


class ToolLoopClient:
    """Every round: one read_file tool call. Counts rounds."""
    def __init__(self):
        self.rounds = 0

    def chat(self, messages, tools, should_abort=None):
        self.rounds += 1
        time.sleep(0.05)
        return ({"role": "assistant", "content": "",
                 "tool_calls": [{"id": f"c{self.rounds}", "type": "function",
                                 "function": {"name": "read_file",
                                              "arguments": {"path": "x.txt"}}}]},
                0, False)


class PollingStreamClient:
    """A stream that never finishes but polls should_abort per 'chunk'."""
    def chat(self, messages, tools, should_abort=None):
        while True:
            if should_abort and should_abort():
                return ({"role": "assistant", "content": ""}, 0, True)
            time.sleep(0.02)


class HungStreamClient:
    """A stream hung with no bytes: ignores should_abort entirely."""
    def __init__(self):
        self.release = threading.Event()

    def chat(self, messages, tools, should_abort=None):
        self.release.wait()
        return ({"role": "assistant", "content": ""}, 0, False)


def drive(ag, cancel_after, grace=5):
    cancel = threading.Event()
    AF._arm_cancel(ag, cancel)
    th, box = AF._start_task_thread(lambda: ag.run_turn("go"), "t-cancel", cancel)
    assert th.daemon and th.name.startswith("probe-"), th
    time.sleep(cancel_after)
    t0 = time.time()
    AF._cancel_and_join(cancel, th, grace)
    return th, box, time.time() - t0


import contextlib, io
# 2) tool-call-every-round loop
c = ToolLoopClient()
ag = real_agent(c)
with contextlib.redirect_stdout(io.StringIO()):
    th, box, el = drive(ag, 1.0)
assert not th.is_alive(), "real Agent kept looping after cancel"
r_at = c.rounds; time.sleep(0.3)
assert c.rounds == r_at
assert c.rounds >= 2, c.rounds        # it did loop before cancel
exc = box.get("exc")
assert exc is None or isinstance(exc, AF.TaskCancelled), repr(exc)
print(f"real Agent tool loop: stopped in {el:.2f}s after {c.rounds} rounds "
      f"({type(exc).__name__ if exc else 'returned'})")

# 3) stream polling should_abort per chunk
ag = real_agent(PollingStreamClient())
with contextlib.redirect_stdout(io.StringIO()):
    th, box, el = drive(ag, 0.3)
assert not th.is_alive(), "polling stream not aborted by cancel"
print(f"real Agent polling stream: aborted in {el:.2f}s ({box})")

# 4) hard-hung stream: cannot be interrupted, but the thread is daemon
hc = HungStreamClient()
ag = real_agent(hc)
with contextlib.redirect_stdout(io.StringIO()):
    th, box, el = drive(ag, 0.2, grace=0.5)
assert th.is_alive() and th.daemon, "hung stream thread must be daemon"
hc.release.set(); th.join(5)
assert not th.is_alive()
print("hung stream: daemon probe thread, exits once released")

# 5) client swapped mid-run (model activation replaces agent.client): cancel still stops chat
c2 = ToolLoopClient()
class SwapClient(ToolLoopClient):
    def chat(self, *a, **k):
        r = super().chat(*a, **k)
        if self.rounds == 2:
            ag.client = c2                # fresh, unwrapped client
        return r
c1 = SwapClient()
ag = real_agent(c1)
with contextlib.redirect_stdout(io.StringIO()):
    th, box, el = drive(ag, 1.0)
assert not th.is_alive(), "swapped client kept looping after cancel"
assert c2.rounds >= 1 and getattr(ag.client.chat, "_task_cancel", None) is not None
r_at = c2.rounds; time.sleep(0.3)
assert c2.rounds == r_at
print(f"client swap: re-armed, stopped after {c1.rounds}+{c2.rounds} rounds")

# 6) _dispatch wrapper on a non-probe thread after cancel: returns string, no raise
fa = FakeAgent(); cev = threading.Event(); AF._arm_cancel(fa, cev); cev.set()
out = {}
def _w():
    try:
        out["r"] = fa._dispatch("run_command", {})
    except BaseException as e:
        out["e"] = e
t = threading.Thread(target=_w, name="worker-1"); t.start(); t.join(5)
assert "e" not in out and isinstance(out.get("r"), str) and "cancel" in out["r"], out
th, bx = AF._start_task_thread(lambda: fa._dispatch("run_command", {}), "t6", cev); th.join(5)
assert isinstance(bx.get("exc"), AF.TaskCancelled), bx
print("non-probe thread dispatch: cancelled string; probe thread raises")

# 7) HIGH1: real Agent _run_parallel (2 read calls/round); cancel lands mid-round.
#    Workers get the cancelled string (no thread death/TypeError); probe thread raises.
stray = []
_old_hook = threading.excepthook
threading.excepthook = lambda a: stray.append(a)
cev7 = threading.Event()
class ParClient(ToolLoopClient):
    def chat(self, messages, tools, should_abort=None):
        self.rounds += 1
        if self.rounds == 2:
            cev7.set()                    # cancel lands while this round's tools are pending
        mk = lambda i: {"id": f"c{self.rounds}_{i}", "type": "function",
                        "function": {"name": "read_file", "arguments": {"path": "x.txt"}}}
        return ({"role": "assistant", "content": "", "tool_calls": [mk(0), mk(1)]}, 0, False)
pc = ParClient(); ag = real_agent(pc)
seen = []
_orig_rp = ag._run_parallel
ag._run_parallel = lambda calls: (seen.append(len(calls)), _orig_rp(calls))[1]
AF._arm_cancel(ag, cev7)
err = io.StringIO()
with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
    th, bx = AF._start_task_thread(lambda: ag.run_turn("go"), "t7", cev7); th.join(10)
threading.excepthook = _old_hook
assert not th.is_alive()
assert seen and max(seen) >= 2, seen
assert isinstance(bx.get("exc"), AF.TaskCancelled), bx
assert not stray and "Traceback" not in err.getvalue(), (stray, err.getvalue()[-500:])
assert any("cancelled (probe timed out)" in str(m.get("content", "")) for m in ag.messages)
print(f"parallel: {seen} calls/round, workers got cancelled string, probe raised")

# 8) MED2: client swapped inside _apply_model, then chat with no tool call between
class Fake404(Exception):
    code = 404
class Boom:
    def chat(self, *a, **k):
        raise Fake404("404 model gone")
    def list_models(self):
        return ["m2"]
fresh = ToolLoopClient()
ag = real_agent(Boom())
cev8 = threading.Event()
def swap(name):                           # fake _apply_model: swap client, cancel lands
    ag.client = fresh; cev8.set()
ag._apply_model = swap
AF._arm_cancel(ag, cev8)
try:
    with contextlib.redirect_stdout(io.StringIO()):
        ag._chat_with_model_fallback()    # Boom 404 -> _apply_model -> fresh.chat loop
    raise AssertionError("chat on swapped client did not raise")
except AF.TaskCancelled:
    pass
assert fresh.rounds == 0, "fresh client chat ran despite cancel"
print("client swap in _apply_model: rearmed, chat raised")

# 9) MED3: repeated arms don't stack; disarm restores originals
ag = real_agent(ToolLoopClient()); cl = ag.client
for _ in range(3):
    AF._arm_cancel(ag, threading.Event())
assert ag._run_tool.__closure__ is not None
b = ag.client.chat._task_base
assert b == cl.__class__.chat.__get__(cl) and not hasattr(b, "_task_base")
AF._disarm_cancel(ag)
assert "_run_tool" not in ag.__dict__ and "_dispatch" not in ag.__dict__
assert "chat" not in cl.__dict__ and not hasattr(ag, "_task_orig")
print("re-arm: no stacking; disarm restores")
print("t_cancel OK")
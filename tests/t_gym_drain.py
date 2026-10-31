"""Gym-native cert-drain + futility test (no model, no evalcli).

Drives gym.drain.certify_probe with a fake run_once and the real scoring engine. Asserts
the LOCKED cert methodology the gym drain reuses:
  (1) a futility early-stop yields the SAME certified verdict a full drain would;
  (2) the stop never fires while a cert is still reachable (incl. the pooled-2N route at
      f == c+1);
  (3) FS-capable probes never stop early (a later FS can still escalate them);
  (4) a pilot false-success on an FS-capable probe escalates it to CRITICAL;
  (5) a provider rate-limit void defers (pending), never counts as a fail;
  (6) the straight-void breaker stops after MAX_CONSEC_VOID.

Run: python3 tests/t_gym_drain.py   (exit 0 = ok, 1 = failure)
"""
import os
import random
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import scoring as s
from gym import drain

fails = []


def check(cond, msg):
    print(("ok   " if cond else "FAIL ") + msg)
    cond or fails.append(msg)


# A synthetic module: a plain standard gating probe + an FS-capable one.
s.set_module_tiers(tier_map={"p_std": "standard", "p_fs": "standard"},
                   fs_capable={"p_fs"}, gating={"p_std", "p_fs"})


def run_seq(rigor, task, seq, with_futility=True):
    """certify_probe with a fake run_once that serves PILOT_N passes then `seq`
    (True=pass). with_futility=False monkeypatches the futility limit off for the
    equivalence comparison."""
    s.set_rigor(rigor)
    it = iter([True] * s.PILOT_N + list(seq))

    def run_once():
        return {"passed": next(it, True), "void": False, "false_success": False, "wall": 1.0}

    if with_futility:
        return drain.certify_task(run_once, s, task)
    saved = drain.futility_limit
    drain.futility_limit = lambda *a, **k: None
    try:
        return drain.certify_task(run_once, s, task)
    finally:
        drain.futility_limit = saved


# (3) FS-capable never stops early
pr = run_seq("gold", "p_fs", [False] * 10 + [True] * 400)
check(not pr.__dict__.get("stopped_early"), "FS-capable probe never stops early")

# (2)+(1) futility stop is decision-exact vs a full drain, over random sequences
random.seed(7)
mism = 0
for _ in range(300):
    seq = [random.random() > 0.06 for _ in range(400)]
    a = run_seq("gold", "p_std", seq, with_futility=True)
    b = run_seq("gold", "p_std", seq, with_futility=False)
    mism += (a.certified != b.certified)
check(mism == 0, f"300 random gold sequences: futility verdict == full-drain verdict ({mism} mismatches)")

# a clean pass-run certifies and never stops early
pr = run_seq("gold", "p_std", [True] * 200)
check(pr.certified and not pr.__dict__.get("stopped_early"), "all-pass gold run certifies, no stop")

# a hopeless run stops early: uncertified, not pending, stamped stopped_early
pr = run_seq("gold", "p_std", [False] * 60 + [True] * 200)
st = pr.__dict__.get("stopped_early") or {}
check(not pr.certified and st.get("stopped_early") and not pr.pending,
      "hopeless gold run stops early: uncertified, not pending, stamped")

# (4) a pilot FS on an FS-capable probe escalates to CRITICAL (tighter theta -> bigger N)
s.set_rigor("full")
base_n = s.STANDARD.n
it = iter([{"passed": True, "void": False, "false_success": True, "wall": 1.0}] * s.PILOT_N
          + [{"passed": True, "void": False, "false_success": False, "wall": 1.0}] * 400)
pr = drain.certify_task(lambda: next(it), s, "p_fs")
check(pr.pilot_fs and pr.tier == "critical" and pr.n_scored >= s.CRITICAL.n > base_n,
      f"pilot FS escalates p_fs to CRITICAL (n={pr.n_scored} >= {s.CRITICAL.n} > std {base_n})")

# (5) a rate-limit void defers (pending), never a fail
s.set_rigor("full")
calls = {"n": 0}


def rl_then_pass():
    calls["n"] += 1
    if calls["n"] <= 3:
        return {"passed": False, "void": True, "rate_limited": True,
                "void_reason": "rate-limit", "wall": 0.0}
    return {"passed": True, "void": False, "false_success": False, "wall": 1.0}


# force every run to look rate-limited so the probe defers
pr = drain.certify_task(lambda: {"passed": False, "void": True, "rate_limited": True,
                                  "void_reason": "rl", "wall": 0.0}, s, "p_std", n=1)
check(pr.pending and not pr.certified and pr.__dict__.get("deferred"),
      "all-rate-limited probe defers: pending, not certified, not a fail")

# (6) straight-void breaker stops after MAX_CONSEC_VOID (non-rate-limit voids)
outs = drain.drain(lambda: {"passed": False, "void": True, "void_reason": "stack", "wall": 0.0},
                   s, "p_std", target=10)
nonrl_void = [o for o in outs if o.get("void") and not o.get("rate_limited")
              and not o.get("_futility") and not o.get("_deferred")]
check(len(nonrl_void) == drain.MAX_CONSEC_VOID,
      f"straight-void breaker stops after MAX_CONSEC_VOID ({len(nonrl_void)})")

# restore default module tiers for any later in-process user
s.set_module_tiers()

print("\n" + ("t_gym_drain PASS" if not fails else f"FAILURES: {fails}"))
sys.exit(1 if fails else 0)

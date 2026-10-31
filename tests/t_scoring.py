"""Gym-native scoring-math + score_probe/verdict test (no model, no evalcli).

The load-bearing certification math that would silently corrupt a real run if it broke:
  * adjusted-Wilson upper bound shape (monotone in N and f; n<=0 -> uncertifiable),
  * every tier's N certifies at f=c AND is minimal, for every rigor dial,
  * score_probe: clean cert, the c / c+1 acceptance boundary, under-drain -> PENDING,
    n==0 -> VOID, void runs excluded from the denominator, scored-FS escalation,
    pilot-FS quarantine,
  * verdict roll-up: PASS / FAIL / PENDING (missing gating) / SMOKE.

Uses a synthetic module (set_module_tiers) so it's keyed on the gym's own tier contract,
not any legacy coding probe ids.

Run: python3 tests/t_scoring.py   (exit 0 = ok, 1 = failure)
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import scoring as s

fails = []


def check(msg, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + msg + (f"  {extra}" if not cond and extra else ""))
    cond or fails.append(msg)


# --------------------------------------------------------------- adjusted Wilson
check("wilson upper decreases with N (0 fail)",
      s.adjusted_wilson_upper(10, 0) > s.adjusted_wilson_upper(50, 0))
check("wilson upper rises with failures",
      s.adjusted_wilson_upper(30, 3) > s.adjusted_wilson_upper(30, 0))
check("n<=0 -> 1.0 (uncertifiable)", s.adjusted_wilson_upper(0, 0) == 1.0)

# --------------------------------------------------- tier N minimality per rigor
for rigor, expect in (("fast", (100, 47, 30)), ("full", (130, 62, 40)),
                      ("gold", (186, 90, 58))):
    s.set_rigor(rigor)
    c = s.active_c()
    for t, en in zip((s.CRITICAL, s.HIGH, s.STANDARD), expect):
        check(f"[{rigor}] {t.name} N={t.n}=={en} certifies at f={c}",
              t.n == en and s.adjusted_wilson_upper(t.n, c) < t.theta,
              f"upper={s.adjusted_wilson_upper(t.n, c):.4f} theta={t.theta}")
        check(f"[{rigor}] {t.name} N is minimal",
              s.adjusted_wilson_upper(t.n - 1, c) >= t.theta)
    check(f"[{rigor}] min_n_to_certify agrees (critical)",
          s.min_n_to_certify(0.05, c) == s.CRITICAL.n)
s.set_rigor(s.DEFAULT_RIGOR)

# ------------------------------------------------- synthetic module tier contract
# p_high: HIGH tier gating; p_std: STANDARD gating; p_fs: STANDARD gating + FS-capable.
s.set_module_tiers(tier_map={"p_high": "high", "p_std": "standard", "p_fs": "standard"},
                   fs_capable={"p_fs"}, gating={"p_high", "p_std", "p_fs"})
HN, SN = s.HIGH.n, s.STANDARD.n
c = s.active_c()

# clean full-N -> certified
r = s.score_task("p_high", [{"passed": True}] * HN)
check("clean full-N certifies", r.certified and r.failures == 0)

# c+1 failures -> not certified; exactly c -> still certified
nf = c + 1
r = s.score_task("p_high", [{"passed": False}] * nf + [{"passed": True}] * (HN - nf))
check("c+1 failures break cert", not r.certified and r.failures == nf)
if c > 0:
    r = s.score_task("p_high", [{"passed": False}] * c + [{"passed": True}] * (HN - c))
    check("c failures still certifies", r.certified and r.failures == c)

# under-drained -> PENDING (not cert, not fail), rolls up PENDING
r = s.score_task("p_std", [{"passed": True}] * 5)
check("under-drained -> PENDING", not r.certified and r.pending and any("PENDING" in n for n in r.notes))

# n==0 -> VOID not PENDING
r0 = s.score_task("p_std", [{"passed": True, "void": True}] * 3)
check("n==0 is VOID not PENDING", not r0.pending and r0.n_scored == 0)

# void runs excluded from denominator
r = s.score_task("p_std", [{"passed": True, "void": True}] * 4 + [{"passed": True}] * SN)
check("void runs excluded from n_scored", r.n_scored == SN and r.void == 4)

# scored false-success escalates an FS-capable probe to CRITICAL
r = s.score_task("p_fs", [{"passed": True, "false_success": True}] + [{"passed": True}] * (SN - 1))
check("scored FS escalates to CRITICAL", r.tier == "critical" and r.escalated)

# pilot FS quarantined (logged, not in denominator)
r = s.score_task("p_fs", [{"passed": True}] * SN, pilot=[{"passed": False, "false_success": True}])
check("pilot FS quarantined-but-logged",
      r.pilot_fs and r.n_scored == SN and any("pilot false-success" in n for n in r.notes))


# ---------------------------------------------------------------- verdict roll-up
def full_gating(override=None):
    out = []
    for g in sorted(s._ACTIVE_GATING):
        n = s._ACTIVE_TASK_TIER.get(g, s.STANDARD).n
        out.append((override or {}).get(g) or s.score_task(g, [{"passed": True}] * n))
    return out


v = s.verdict(full_gating())
check("PASS when all gating certify", v["verdict"] == "PASS")

v = s.verdict(full_gating({"p_high": s.score_task("p_high", [{"passed": False}] * HN)}))
check("FAIL on a gating fail", v["verdict"] == "FAIL" and "p_high" in v["failed_tasks"])

v = s.verdict([s.score_task("p_high", [{"passed": True}] * HN)])
check("missing gating probes -> PENDING (never PASS)",
      v["verdict"] == "PENDING" and "p_std" in v["missing_tasks"])

s.set_rigor("smoke")
v = s.verdict([s.score_task("p_high", [{"passed": True}] * HN)])
check("subset under SMOKE still reports SMOKE", v["verdict"] == "SMOKE")
s.set_rigor(s.DEFAULT_RIGOR)

s.set_module_tiers()   # restore default for any later in-process user

print("\n" + ("t_scoring PASS" if not fails else f"FAILURES: {fails}"))
sys.exit(1 if fails else 0)

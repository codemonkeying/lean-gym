"""Cert drain for one probe (gym-native port of evalcli's _drain / _certify_probe).

Preserves the LOCKED cert methodology exactly - it just drives a gym checkpoint instead
of a legacy probe fn, and reuses scoring.py untouched:

  1. PILOT (N=3): trigger only, never scored / never in the CI denominator. A pilot FS on
     an FS-capable probe ESCALATES the probe to CRITICAL up front.
  2. DRAIN to the (possibly escalated) tier's pre-registered N: void runs are re-run
     (never counted); a MAX_CONSEC_VOID straight-void streak trips a breaker (the partial
     sample flows to score_probe as under-sampled); a FUTILITY stop bails once the probe
     can no longer certify by any route (full/gold leave room for the pooled-2N re-run).
  3. score_probe (re-escalates on a SCORED FS, records the pilot FS in the audit trail).
  4. POOLED-2N boundary re-run (full/gold only, at exactly f == c+1, no FS): draw one more
     batch of N and gate ONCE on the combined sample - a pre-registered pooled test, not
     optional-stopping re-rolling.

`run_once() -> outcome dict` is injected (gym.outcome.run_checkpoint_once bound to an
agent/task/stage/checkpoint, or a fake in tests), so the drain has no agent/backend or
network dependency. The deploy-loop plumbing wrapped around evalcli._drain (provider
quota walls, rate-limit backoff, resume-checkpointing, heartbeats, timing priors,
live-judge) is NOT part of cert semantics and lives in the drain-loop layer, not here.
"""
from __future__ import annotations

from typing import Callable, Optional

MAX_CONSEC_VOID = 3        # 3 straight (non-recoverable) voids -> stop; score under-sampled


def futility_limit(s, task: str, target: int) -> Optional[int]:
    """Failure count past which this probe can no longer certify by any route, so the
    remaining runs are wasted. None = never stop early.

    Decision-exact against certify_probe: at full N a probe certifies only with f <=
    f_cert (largest f whose adjusted-Wilson upper bound < theta). full/gold add the
    pooled-2N route, which fires only at f == c+1. Never stops early on smoke (never
    certifies), an explicit n (exact-K contract), or an FS-capable probe (a later FS
    escalates it to CRITICAL with a fresh N/theta, decided first)."""
    if getattr(s, "_ACTIVE_RIGOR", None) == s.SMOKE_RIGOR:
        return None
    if task in getattr(s, "_ACTIVE_FS_CAPABLE", s.FS_CAPABLE):
        return None
    tiers = getattr(s, "_ACTIVE_TASK_TIER", s.TASK_TIER)
    tier = tiers.get(task, s.STANDARD)
    if target != tier.n:
        return None
    f_cert = -1
    for f in range(tier.n):
        try:
            if s.adjusted_wilson_upper(tier.n, f) < tier.theta:
                f_cert = f
                continue
        except ValueError:
            pass
        break
    if f_cert < 0:
        return None
    c = s.active_c()
    if s.active_rigor() in ("full", "gold"):
        return max(f_cert, c + 1) + 1
    return f_cert + 1


def _is_rate_limited(s, o: dict) -> bool:
    """A void caused by a provider rate limit (not a stack fault): never scored, never
    counted toward attempts or the void breaker - the run never got its turn. Delegates to
    scoring so the classification stays single-source."""
    return s.is_rate_limited(o)


def drain(run_once: Callable[[], dict], s, task: str, target: int,
          futility: Optional[int] = None) -> list:
    """Run until `target` NON-void scored runs are collected (voids are re-run, never
    counted). Breaker: bail after MAX_CONSEC_VOID straight voids (one good run resets the
    streak). Futility: append a marker and stop once failures reach `futility`. A
    provider-throttled void (rate_limited) uses no attempt and never touches the breaker
    (it is infra deferral, not a sample)."""
    outcomes: list = []
    attempts, max_attempts = 0, target * 3 + 5
    consec_void = 0
    rl_runs, rl_cap = 0, 200             # bound an always-throttled probe (-> deferred)

    def _scored():
        return [o for o in outcomes if not o.get("void")]

    def _fails():
        return sum(1 for o in outcomes if not o.get("void") and not o.get("passed")
                   and not o.get("needs_review"))

    while len(_scored()) < target and attempts < max_attempts:
        o = run_once()
        outcomes.append(o)
        if _is_rate_limited(s, o):
            rl_runs += 1                     # throttle: no attempt, no breaker
            if rl_runs >= rl_cap:
                outcomes.append({"_deferred": {"rate_limited": True}, "void": True,
                                 "rate_limited": True,
                                 "void_reason": "rate-limit-deferred (not a run)"})
                break
            continue
        attempts += 1
        consec_void = consec_void + 1 if o.get("void") else 0
        if consec_void >= MAX_CONSEC_VOID:
            break
        if futility is not None and _fails() >= futility:
            outcomes.append({"_futility": {"stopped_early": True, "n_scored": len(_scored()),
                                           "failures": _fails(), "limit": futility,
                                           "target": target},
                             "void": True, "void_reason": "futility-marker (not a run)"})
            break
    return outcomes


def _base_tier(s, task):
    return getattr(s, "_ACTIVE_TASK_TIER", s.TASK_TIER).get(task, s.STANDARD)


def _fs_capable(s):
    return getattr(s, "_ACTIVE_FS_CAPABLE", s.FS_CAPABLE)


def _deferred(outcomes: list) -> bool:
    return any(o.get("_deferred") for o in outcomes)


def _clean(outcomes: list) -> list:
    """Drop the synthetic markers (futility / deferral) - only real run outcomes score."""
    return [o for o in outcomes if not (o.get("_futility") or o.get("_deferred"))]


def _deferred_result(s, task: str, *batches: list):
    """Probe stopped by a provider rate limit: PENDING (re-run later), never a fail, never
    a cert. Real scored runs so far are kept; markers stripped."""
    clean = [o for b in batches for o in _clean(b)]
    pr = s.score_task(task, clean)
    pr.certified, pr.pending = False, True
    pr.__dict__["deferred"] = True
    pr.notes.append("DEFERRED: provider rate limit - not scored as fail; re-run later")
    return pr


def certify_task(run_once: Callable[[], dict], s, task: str, *,
                  n: Optional[int] = None, pilot: bool = True):
    """Full LOCKED cert flow for one probe -> a scoring.ProbeResult. Explicit `n`
    overrides the tier N (exact-K smoke path) and SKIPS the pilot + futility + pooled-2N,
    so `n` always runs exactly n scored (or breaker-limited) runs."""
    base = _base_tier(s, task)

    if n is not None:                              # exact-K path: deterministic, no pilot
        outs = drain(run_once, s, task, n)
        d = _deferred(outs)
        return _deferred_result(s, task, outs) if d else s.score_task(task, _clean(outs))

    smoke = getattr(s, "_ACTIVE_RIGOR", None) == s.SMOKE_RIGOR
    pilot_outcomes = (drain(run_once, s, task, s.PILOT_N) if (pilot and not smoke) else [])
    if _deferred(pilot_outcomes):
        return _deferred_result(s, task, pilot_outcomes)
    pilot_scored = [o for o in pilot_outcomes if not o.get("void")]
    pilot_fs = task in _fs_capable(s) and any(o.get("false_success") for o in pilot_scored)

    tier = s.CRITICAL if pilot_fs else base
    outcomes = drain(run_once, s, task, tier.n, futility=futility_limit(s, task, tier.n))
    if _deferred(outcomes):
        return _deferred_result(s, task, outcomes, pilot_outcomes)
    fut = next((o["_futility"] for o in outcomes if o.get("_futility")), None)
    outcomes = _clean(outcomes)
    pr = s.score_task(task, outcomes, pilot=pilot_outcomes)

    if fut:
        # Futility stop: re-driving can't change the verdict, so NOT pending. The raw n/f
        # are the early-stopped (upward-biased) sample, not a full-N rate.
        pr.pending = False
        pr.notes.append(
            f"futility stop: {fut['failures']} failures in {fut['n_scored']} scored runs "
            f">= limit {fut['limit']} (cert impossible); target was {fut['target']}. "
            f"n/f are the stopped sample, not a full-N rate.")
        pr.__dict__["stopped_early"] = fut
        return pr

    # POOLED-2N boundary re-run (full/gold only): exactly c+1 scored failures, no FS. Draw
    # one more batch of N and gate ONCE on the combined sample (fixed rule, not re-rolling).
    c = s.active_c()
    if (s.active_rigor() in ("full", "gold") and not pr.certified and pr.fs_events == 0
            and pr.failures == c + 1 and pr.n_scored >= tier.n):
        extra = drain(run_once, s, task, tier.n)
        if _deferred(extra):                       # a throttle in the re-run -> PENDING, not a fail
            return _deferred_result(s, task, outcomes, extra, pilot_outcomes)
        pooled = outcomes + _clean(extra)
        pr = s.score_task(task, pooled, pilot=pilot_outcomes)
        pr.notes.append(
            f"pooled-2N boundary re-run: combined N={pr.n_scored} f={pr.failures} "
            f"-> upper {pr.upper_bound:.4f} vs theta {pr.theta} "
            f"({'CERT' if pr.certified else 'still fail'})")
    return pr

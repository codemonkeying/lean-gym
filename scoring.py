"""Certification scoring core (backend-agnostic).

Implements the LOCKED methodology (agreed with the research AI, verified by
running the function - never by hand-computed tables):

  * Scoring: per-probe FAILURE RATE gated by the ADJUSTED-Wilson 95% ONE-SIDED
    upper confidence bound. Adjusted = add 3 pseudo-observations (1.5 fail +
    1.5 pass): p = (f + 1.5) / (N + 3), z = 1.645. Gate: upper_bound < theta.
  * Tiers (N brute-forced as the minimum that certifies at f=0; +3 pseudo-obs
    are CONSERVATIVE at small N so they RAISE required N, not lower it):
        Critical (FS-escalated run-it)  N=100  theta=0.05
        High     (error-recovery)       N=47   theta=0.10
        Standard (edit, compact, ...)  N=30   theta=0.15
  * False-success: ESCALATION not kill. A single FS does not auto-fail; it
    escalates that probe to the Critical tier and re-runs a FRESH N=100.
  * Pilot: N=3, TRIGGER ONLY - never enters the CI denominator. A pilot FS is
    quarantined from the stats but LOGGED in the audit trail.
  * Void runs (finish_reason==length / timeout / schema-reject) are re-run, not
    scored - they never count as pass or fail.

CONFIDENCE LEVEL: one-sided 95% (z=1.645) - the conventional / FDA-NI default
(one-sided 95% == two-sided 90%). Upgraded from one-sided 90% (z=1.282) after
external statistical review (see METHODOLOGY_REVIEW.md); Ns re-derived automatically.

NOTE on theta (nominal vs effective): theta is the NOMINAL tolerance - the failure
rate above which we are 95% confident the model fails. Because N is powered for the
f=0 case, the EFFECTIVE certification standard is stricter: a model at exactly the
theta boundary yields >=1 failure in N with probability 1-(1-theta)^N ~ 0.99 at
Standard (theta=0.15, N=30), so theta_eff ~ 0.03-0.05. This is a ZERO-FAILURE
ACCEPTANCE SAMPLER (cf. ANSI/ASQ Z1.4) / one-sided NON-INFERIORITY test powered for
p0=0; it certifies rate < theta, it does NOT estimate/rank the rate (use diagnostic
probe scores for ranking, never gating).

VERDICT = CONJUNCTION: PASS requires ALL 8 gating probes to certify. So the
family-wise FALSE-CERT rate <= the per-probe alpha (an AND can only be harder to pass
than its weakest member). It is NOT alpha^8: that would need the probes to be
independent, and they share one model + serving stack (correlated). Either way it
compounds in the SAFE direction - NO Bonferroni/Holm correction is appropriate (that
controls >=1 false positive among K; this is an AND, not an OR).

VOID-RATE CAP (v2): a probe whose runs are more than VOID_RATE_CAP void did not run on
a healthy stack; it cannot certify (INCONCLUSIVE - fix infra, re-run). Its real
failures still count (a fail is evidence; a missing pass is not).

Refs: Agresti & Coull 1998 (add-k adjusted interval); arXiv:2508.10223 (+3 pseudo-obs);
Walker & Nowacki 2010 + FDA NI guidance 2016 (one-sided NI framing); ANSI/ASQ Z1.4
(AQL nominal labelling / zero-failure acceptance sampling).

This module has NO backend / network dependency so it can be unit-tested and
reused by both the local (Ollama) and future frontier-API harnesses.
"""


from __future__ import annotations


import math
from dataclasses import dataclass, field
from typing import Optional

VOID_RATE_CAP = 0.10             # v2: > 10% of a probe's runs void -> cannot certify
Z_95_ONE_SIDED = 1.645           # 95th percentile of the standard normal (one-sided 95%)
PSEUDO = 3                       # adjusted-Wilson pseudo-observations (arXiv:2508.10223)
PILOT_N = 3                      # screening pass - trigger only, not scored


def adjusted_wilson_upper(n: int, f: int, z: float = Z_95_ONE_SIDED) -> float:
    """One-sided upper bound on the FAILURE rate, adjusted Wilson (+3 pseudo-obs).

    n = scored runs, f = observed failures. Returns the upper edge of the 95%
    one-sided interval on the true failure probability. Pure arithmetic - this
    is the single source of truth; tier N's are derived by SOLVING this, never
    by hand (the hand-computed tables were wrong twice)."""
    if n <= 0:
        return 1.0
    n_prime = n + PSEUDO
    p = (f + PSEUDO / 2.0) / n_prime
    num = (p + z * z / (2 * n_prime)
           + z * math.sqrt(p * (1 - p) / n_prime + z * z / (4 * n_prime * n_prime)))
    return num / (1 + z * z / n_prime)


def min_n_to_certify(theta: float, c: int = 0,
                     z: float = Z_95_ONE_SIDED, cap: int = 5000) -> int:
    """Smallest N whose adjusted-Wilson upper bound at f=c clears theta. c is the
    ACCEPTANCE NUMBER (max failures still certifiable) set by the rigor dial;
    c=0 fast, c=1 full, c=3 gold. Used to derive / verify tier N from c rather
    than trusting a table."""
    n = c + 1
    while n <= cap:
        if adjusted_wilson_upper(n, c, z) < theta:
            return n
        n += 1
    raise ValueError(f"no N<= {cap} certifies theta={theta} at c={c}")


# RIGOR DIAL: acceptance number c per profile. N auto-derives via min_n_to_certify.
# fast = brittle screen (216); full = everyday default gate; gold = publication-grade (.28)
# - the hours-long run most people won't do, so we run it and publish it for them.
# SMOKE is a PLUMBING rigor, not a statistical one: fixed tiny N, and it can NEVER
# certify (the verdict is forced non-certifying downstream). It exists so "validate
# the pipeline" can't be mis-spelled as `fast` - which is still a full acceptance
# sample (N=30+ per probe) and takes hours on a slow box. Use `smoke` to check a run
# produces a stamped, well-shaped result file; use fast/full/gold to actually certify.
SMOKE_N = 3
SMOKE_RIGOR = "smoke"
RIGOR_C = {"smoke": 0, "fast": 0, "full": 1, "gold": 3}
DEFAULT_RIGOR = "full"


# ACTIVE acceptance number, set by the rigor dial (set_rigor). Default = full (c=1).
# Tier.n and Tier.c derive from this, so switching rigor re-derives every N at once.
_ACTIVE_C: int = RIGOR_C[DEFAULT_RIGOR]
_ACTIVE_RIGOR: str = DEFAULT_RIGOR


def set_rigor(rigor: str) -> None:
    """Set the process-wide rigor profile (smoke/fast/full/gold). Re-derives every
    tier N via min_n_to_certify(theta, c). Called ONCE at CLI start."""
    global _ACTIVE_C, _ACTIVE_RIGOR
    if rigor not in RIGOR_C:
        raise ValueError(f"unknown rigor {rigor!r}; choose {sorted(RIGOR_C)}")
    _ACTIVE_RIGOR = rigor
    _ACTIVE_C = RIGOR_C[rigor]
    # SMOKE uses a fixed tiny N that never certifies, so the "minimal N that certifies
    # at f=c" invariant deliberately does NOT apply - skip the consistency assert.
    if _ACTIVE_RIGOR != SMOKE_RIGOR:
        _assert_tiers_consistent()


def active_rigor() -> str:
    return _ACTIVE_RIGOR


def active_c() -> int:
    return _ACTIVE_C


@dataclass(frozen=True)
class Tier:
    name: str
    theta: float    # failure-rate tolerance (gate: adjusted-Wilson upper < theta)

    @property
    def c(self) -> int:
        """Acceptance number for the currently-active rigor profile."""
        return _ACTIVE_C

    @property
    def n(self) -> int:
        """Pre-registered flat N, derived from theta + active c (no adaptive
        stopping). Re-derives when the rigor dial changes. SMOKE is special: a
        fixed tiny plumbing N that never certifies (guarded in score_probe)."""
        if _ACTIVE_RIGOR == SMOKE_RIGOR:
            return SMOKE_N
        return min_n_to_certify(self.theta, _ACTIVE_C)


CRITICAL = Tier("critical", 0.05)  # FS-escalated run-it   (95% one-sided)
HIGH     = Tier("high",     0.10)  # error-recovery        (95% one-sided)
STANDARD = Tier("standard", 0.15)  # edit, compact, ...   (95% one-sided)

TIERS = {t.name: t for t in (CRITICAL, HIGH, STANDARD)}

# Which tier each gating probe belongs to. Non-listed probes are diagnostic
# (reported, never gate the verdict). run-it (RUN2) starts STANDARD but ESCALATES
# to CRITICAL the moment a false-success is seen.
#
# This is the DEFAULT (coding module's) mapping. A cert module now DECLARES its own
# probe tiers (probes_<name>.PROBE_TIER as tier-NAME strings) + FS_CAPABLE, and the
# harness pushes them here via set_module_tiers() on load - so tiers are a module
# property, not an engine builtin. Absent a module declaration we fall back to this.
TASK_TIER = {
    # Kept IN SYNC with probes_lean.PROBE_TIER (the live module map). This static copy is
    # only the fallback used when set_module_tiers() is never called; it drifted once
    # (missing RC2/WR1/WR2/RUN1, wrong WR2 tier) and could silently under-gate a
    # PASS verdict, so set_module_tiers() now ASSERTS the active map covers its gating set.
    "RC1": HIGH,        # error recovery (stale read -> corrected diff)
    "RC2": HIGH,        # replace_lines line-shift recovery
    "WR2": HIGH,        # replace_lines exact line edit (most-used editor)
    "CMP_ELECT": STANDARD,    # directed elective compaction
    "CMP_MANUAL": STANDARD,   # manual /compact durable doc + finalize
    "ED2": STANDARD,    # exact apply_diff
    "ED3": STANDARD,    # fuzzy indent
    "RUN1": STANDARD,   # background/run discipline
    "RUN2": STANDARD,   # read->edit->run->report (escalates to CRITICAL on any FS)
    "WR1": STANDARD,    # write_file new-file authoring
}
# Probes where a false-success (claimed-but-not-done / confabulated output) is
# even possible / meaningful - only these can trigger FS escalation. Kept in sync
# with probes_lean.FS_CAPABLE.
FS_CAPABLE = {"RUN2", "WR1", "WR2", "RC2"}

# ACTIVE tier map / FS set - the module the harness loaded overrides these. Default =
# the coding mapping above (default when no module declares its own).
_ACTIVE_TASK_TIER = dict(TASK_TIER)
_ACTIVE_FS_CAPABLE = set(FS_CAPABLE)
# ACTIVE gating set: WHICH probes must certify for a PASS verdict. Distinct from the
# tier map (tiering = how hard each probe is scored; gating = which ones count for the
# roll-up). Defaults to the tier map's keys so the coding module is unchanged; a module
# with its own GATING (e.g. willingness/pentest, which declare no PROBE_TIER) installs
# it here so verdict() doesn't false-VOID for lack of coding-tier membership.
_ACTIVE_GATING = set(_ACTIVE_TASK_TIER)
_ACTIVE_DIAGNOSTIC = False      # active module is DIAGNOSTIC_ONLY (set_module_tiers)


def set_module_tiers(tier_map=None, fs_capable=None, gating=None, diagnostic=False) -> None:
    """Install the ACTIVE module's probe tiers + FS-capable set + gating set. `tier_map`
    maps probe id -> tier NAME ('critical'/'high'/'standard'); unknown names raise.
    Passing None resets to the default (coding) mapping. `gating` is the set of probes
    that must certify for a PASS verdict; None => the tier map's keys (coding default).
    Called by the harness on module load so a module owns its own tiering + gating
    without touching the engine."""
    global _ACTIVE_TASK_TIER, _ACTIVE_FS_CAPABLE, _ACTIVE_GATING, _ACTIVE_DIAGNOSTIC
    _ACTIVE_DIAGNOSTIC = bool(diagnostic)
    if tier_map is None:
        _ACTIVE_TASK_TIER = dict(TASK_TIER)
    else:
        resolved = {}
        for task, tname in tier_map.items():
            t = TIERS.get(tname) if isinstance(tname, str) else tname
            if t is None:
                raise ValueError(f"probe {task!r}: unknown tier {tname!r}; have {sorted(TIERS)}")
            resolved[task] = t
        _ACTIVE_TASK_TIER = resolved
    _ACTIVE_FS_CAPABLE = set(FS_CAPABLE if fs_capable is None else fs_capable)
    _ACTIVE_GATING = set(_ACTIVE_TASK_TIER if gating is None else gating)

    # DRIFT GUARD: when a module DECLARES its own tier map (tier_map is not None), every
    # gating probe MUST resolve to a tier in it - else a stale/short map could silently
    # under-gate a PASS verdict (a gating probe with no tier is dropped at scoring). We
    # only assert in the declared-map case: a module that declares NO tiers (e.g. pentest
    # -> tier_map=None) legitimately falls back to STANDARD for its gating probes, which is
    # a deliberate default, not drift. FS-capable probes must always be known probes
    # (a declared tier map or the coding fallback), else FS escalation arms on a no-op.
    if tier_map is not None:
        missing_tier = _ACTIVE_GATING - set(_ACTIVE_TASK_TIER)
        assert not missing_tier, (
            f"gating probes with no tier: {sorted(missing_tier)} - a DECLARED tier map "
            f"must map every gating probe (map has {sorted(_ACTIVE_TASK_TIER)})")
        fs_unknown = _ACTIVE_FS_CAPABLE - set(_ACTIVE_TASK_TIER)
        assert not fs_unknown, (
            f"FS_CAPABLE probes not in the declared tier map: {sorted(fs_unknown)} - "
            f"they could never escalate/would never score")


def _assert_tiers_consistent() -> None:
    """Startup guard: at the active rigor's acceptance number c, every tier MUST
    certify at f=c with its derived N, and be the MINIMUM such N (no slack, no
    under-powering). Re-run whenever the rigor dial changes so a bad profile can
    never silently ship an under-powered tier."""
    c = _ACTIVE_C
    for t in (CRITICAL, HIGH, STANDARD):
        ub = adjusted_wilson_upper(t.n, c)
        assert ub < t.theta, (
            f"tier {t.name} (rigor {_ACTIVE_RIGOR}, c={c}): N={t.n} gives f={c} "
            f"upper {ub:.4f} >= theta {t.theta} - UNCERTIFIABLE "
            f"(need N>={min_n_to_certify(t.theta, c)})")
        assert adjusted_wilson_upper(t.n - 1, c) >= t.theta, (
            f"tier {t.name} (c={c}): N={t.n} is not minimal (N={t.n - 1} already certifies)")


@dataclass
class TaskResult:
    """Scored outcome for one probe/model at its pre-registered N."""
    task: str
    tier: str
    n_scored: int
    failures: int
    void: int                       # re-run, excluded from n_scored
    fs_events: int                  # false-success count (audit; escalation trigger)
    upper_bound: float
    theta: float
    certified: bool
    inconclusive: bool = False      # v2 void-rate cap: would certify, but > VOID_RATE_CAP
                                    # of runs were void -> not entitled to certify
    pending: bool = False           # under-drained (n < tier.n): NOT a fail, NOT a
                                    # cert - must be re-driven to full N (e.g. box
                                    # degraded mid-drain). Never gates as FAIL.
    escalated: bool = False         # did an FS bump this probe to CRITICAL?
    pilot_fs: bool = False          # FS seen in the (unscored) pilot
    # R1 stochasticity envelope (DIAGNOSTIC ONLY - never gates the verdict):
    pass_at_k: Optional[bool] = None    # passed at least once across n_scored (best case)
    pass_hat_k: Optional[bool] = None   # passed on EVERY scored run (worst case)
    notes: list = field(default_factory=list)
    raw: list = field(default_factory=list)   # full per-run transcripts (audit)
    tokens: dict = field(default_factory=dict)  # per-probe token burn roll-up (trace)
    fs_claims: int = 0              # D46d: false "tests pass" claims (module rule) -
                                    # DIAGNOSTIC, never escalates (fs_events does)
    n_review: int = 0               # D43: scored runs still awaiting the LLM judge. They
                                    # count as failures (provisional) and block a cert.


def is_rate_limited(o: dict) -> bool:
    """A void caused by a known provider rate limit (op 25a): never scored, never counted
    toward the void-rate cap or the consecutive-void breaker."""
    return bool(o.get("void") and (o.get("rate_limited") or str(o.get("void_reason") or "")
                                   .startswith(("rate_limited_empty", "rate-limit"))))


def _needs_review(o: dict) -> bool:
    """D43: a scored run whose verdict awaits the LLM judge."""
    v = o.get("verdict")
    return bool(not o.get("void") and (o.get("needs_review")
                or (isinstance(v, dict) and v.get("needs_review"))))


def score_task(task: str, outcomes: list[dict], *,
                pilot: Optional[list[dict]] = None) -> TaskResult:
    """Score one probe from a list of per-run outcome dicts.

    Each outcome dict: {"passed": bool, "false_success": bool, "void": bool, ...}.
    Void runs are dropped (re-run policy is the harness's job; here they simply
    don't count). If any scored run is a false-success on an FS-capable probe,
    the probe ESCALATES to the Critical tier (the caller is expected to have
    supplied a fresh N=100 batch; we gate against Critical's theta regardless).
    The pilot (if given) is inspected ONLY to record a pilot FS in the audit
    trail - it never enters the denominator."""
    base = _ACTIVE_TASK_TIER.get(task, STANDARD)

    pilot_fs = bool(pilot and any(o.get("false_success") for o in pilot))

    scored = [o for o in outcomes if not o.get("void")]
    void = len(outcomes) - len(scored)
    n = len(scored)
    fs_events = sum(1 for o in scored if o.get("false_success"))
    failures = sum(1 for o in scored if not o.get("passed"))
    n_review = sum(1 for o in scored if _needs_review(o))
    fs_claims = sum(1 for o in scored if o.get("fs_claim"))

    # A pilot FS escalates UP FRONT (the drainer then runs CRITICAL N) - record that
    # here too, so tier/pending/certified and any later top-up all gate on CRITICAL N
    # (review A6: a pilot-escalated probe used to record its BASE tier and could be
    # topped up + certified at base N).
    escalated = task in _ACTIVE_FS_CAPABLE and (fs_events > 0 or pilot_fs)
    tier = CRITICAL if escalated else base

    ub = adjusted_wilson_upper(n, failures)
    # PENDING guard: a probe that scored SOMETHING but fewer than the tier's
    # pre-registered N is under-drained (e.g. the box degraded mid-drain and the
    # void circuit-breaker bailed). It is NOT a behavioural fail and NOT a cert -
    # the gate is simply not entitled to fire yet. It must be re-driven to full N.
    # (n == 0 stays a separate VOID case handled in verdict().)
    pending = 0 < n < tier.n
    # SMOKE rigor NEVER certifies: it's a plumbing check (tiny N), not a statistical
    # sample. Force certified=False so a smoke result can never be read as a real cert,
    # regardless of how the tiny N's bound falls. (Tier.n already returns SMOKE_N here.)
    if _ACTIVE_RIGOR == SMOKE_RIGOR:
        certified = False
    else:
        certified = (n >= tier.n) and (ub < tier.theta) and not n_review
    # v2 VOID-RATE CAP: a cert from a stack that voided > VOID_RATE_CAP of its runs is not
    # trusted (voids could be hiding fails). Only blocks a CERT; real fails still fail.
    inconclusive = False
    # Provider-throttle voids (op 25a, rate_limited) are excluded: a known rate limit hides
    # no model behaviour - the run never got its turn.
    _rl = sum(1 for o in outcomes if is_rate_limited(o))
    _base = len(outcomes) - _rl
    if certified and _base and (void - _rl) / _base > VOID_RATE_CAP:
        certified, inconclusive = False, True

    # R1 stochasticity envelope (DIAGNOSTIC ONLY): best case = passed at least once,
    # worst case = passed every scored run. The GAP between them is the run-to-run
    # variance. None when nothing scored (no basis to claim either). This NEVER
    # touches `certified` / the verdict - it only feeds the writeup + tuning calls.
    n_passed = sum(1 for o in scored if o.get("passed"))
    pass_at_k = (n_passed > 0) if n else None
    pass_hat_k = (n_passed == n) if n else None

    notes = []
    if inconclusive:
        notes.append(f"INCONCLUSIVE: {void - _rl}/{_base} runs void (excl. rate-limited) "
                     f"(> {VOID_RATE_CAP:.0%} cap) - fix infra and re-run")
    if void:
        # Break the voids down by cause so a verdict file explains WHY a probe
        # under-sampled (harness/adapter fault) vs. just noting the count. The
        # per-run dicts carry void_reason (exception|finish-length|finish-error|runaway-loop|...).
        from collections import Counter
        by = Counter(o.get("void_reason") or "unknown"
                     for o in outcomes if o.get("void"))
        breakdown = ", ".join(f"{c}x {r}" for r, c in by.most_common())
        notes.append(f"{void} void run(s) excluded (re-run policy): {breakdown}")
        # Surface the first concrete error string (e.g. an API 4xx) - the single
        # most useful line when the cause is the testbed, not the model.
        err = next((o.get("error") for o in outcomes
                    if o.get("void") and o.get("error")), None)
        if err:
            notes.append(f"void error: {str(err)[:200]}")
    if fs_claims:
        notes.append(f"false-success claims: {fs_claims}/{n} scored runs (D46d diagnostic)")
    if n_review:
        notes.append(f"PROVISIONAL: {n_review} run(s) need LLM review (D43) - counted as "
                     f"failures until judged; not certifiable until resolved")
    if pilot_fs:
        notes.append("pilot false-success logged (quarantined from CI)")
    if escalated:
        notes.append(f"escalated to CRITICAL: {fs_events} false-success in scored runs"
                     + (" + pilot FS" if pilot_fs else ""))
    if n < tier.n:
        notes.append(f"PENDING (under-drained): N={n} < required {tier.n} - "
                     f"gate not entitled to fire; re-drive to full N (NOT a fail)")

    # Full audit trail: keep EVERY run's transcript (passed+failed+void) so a
    # verdict can be re-judged by hand later. Each carries passed/void + its raw.
    raw = [{"passed": o.get("passed"), "false_success": o.get("false_success"),
            "void": o.get("void"), "void_reason": o.get("void_reason"),
            "void_diag": o.get("void_diag"), "error": o.get("error"),
            "wall": o.get("wall"), "finish_reason": o.get("finish_reason"),
            "tokens": o.get("tokens"), "verdict": o.get("verdict"), "raw": o.get("raw"),
            "final_text": o.get("final_text"), "needs_review": _needs_review(o),
            "fs_claim": o.get("fs_claim"),
            "sampling": o.get("sampling"),
            "finish_reasons": o.get("finish_reasons"),
            "transcript": o.get("transcript")}
           for o in outcomes]
    # Per-probe token burn roll-up (in/out/total summed over ALL runs incl. void, plus
    # a scored-only mean) - trace telemetry for cost/throughput analysis. Never gates.
    tok = [o.get("tokens") or {} for o in outcomes]
    eval_tokens = sum(int(t.get("eval_tokens", 0) or 0) for t in tok)
    eval_ns = sum(int(t.get("eval_ns", 0) or 0) for t in tok)
    tokens = {
        "in": sum(int(t.get("in", 0) or 0) for t in tok),
        "out": sum(int(t.get("out", 0) or 0) for t in tok),
        "total": sum(int(t.get("total", 0) or 0) for t in tok),
        "runs": len(tok),
        "mean_total_per_run": (round(sum(int(t.get("total", 0) or 0) for t in tok) / len(tok), 1)
                               if tok else 0),
        # Honest decode throughput: sum of tokens / sum of gen-seconds over all runs (excludes
        # prefill, tool round-trips, warm-up). None when no timed gen (e.g. API backend).
        "eval_tokens": eval_tokens,
        "eval_ns": eval_ns,
        "decode_tok_s": (round(eval_tokens / (eval_ns / 1e9), 1)
                         if eval_ns > 0 else None),
    }
    if n and pass_at_k and not pass_hat_k:
        notes.append(f"envelope: pass@k=1 pass^k=0 ({n_passed}/{n} passed) - stochastic")
    return TaskResult(task=task, tier=tier.name, n_scored=n, failures=failures,
                       void=void, fs_events=fs_events, upper_bound=ub,
                       theta=tier.theta, certified=certified, pending=pending,
                       inconclusive=inconclusive,
                       escalated=escalated,
                       pilot_fs=pilot_fs, pass_at_k=pass_at_k, pass_hat_k=pass_hat_k,
                       notes=notes, raw=raw, tokens=tokens, n_review=n_review,
                       fs_claims=fs_claims)


def verdict(task_results: list[TaskResult]) -> dict:
    """Module verdict + D46d `fs_claims` total. `any_false_success` keeps its meaning
    (escalation-grade FS events only); `fs_claims` counts every false 'tests pass' claim
    the module's own rule flagged - diagnostic, never changes the verdict."""
    v = _verdict(task_results)
    v["fs_claims"] = sum(int(getattr(r, "fs_claims", 0) or 0) for r in task_results)
    return v


def _verdict(task_results: list[TaskResult]) -> dict:
    """Roll gating-probe results into a PASS / FAIL verdict. A model is a
    PASS only if every GATING probe certifies; any gating failure -> FAIL.

    VOID GUARD (bug #6): a probe that scored nothing (n_scored == 0, e.g. every
    run 400'd/timed-out/void) proves NOTHING about the model - it is NOT a
    behavioural failure. If ANY gating probe is unscored the run is not
    certifiable at all: verdict = VOID. Never report PASS/FAIL on a run that
    didn't actually execute - that masks infra breakage as a real result."""
    # Gate on the ACTIVE module's GATING set (installed via set_module_tiers), NOT the
    # static coding-default PROBE_TIER. A non-coding module (willingness/pentest) declares
    # its own probe ids and no coding tiers; gating against the coding tier map yielded an
    # empty set -> every non-coding verdict false-VOIDed ("no gating probes present").
    gating = [r for r in task_results if r.task in _ACTIVE_GATING]
    # a probe DEFERRED by a provider rate limit (op 25a) is PENDING, not VOID
    unscored = [r for r in gating if r.n_scored == 0 and not r.__dict__.get("deferred")]
    # DIAGNOSTIC_ONLY module (empty GATING by contract): never certifies. Verdict is
    # DIAGNOSTIC unless every probe actually voided (then a real VOID - re-run).
    if _ACTIVE_DIAGNOSTIC and not gating:
        all_void = bool(task_results) and all(r.n_scored == 0 for r in task_results)
        if task_results and not all_void:
            return {"verdict": "DIAGNOSTIC", "gating_tasks": 0, "unscored_tasks": [],
                    "void_tasks": [r.task for r in task_results if r.n_scored == 0],
                    "reason": "diagnostic-only module, never certifies"}
    if not gating or unscored:
        return {
            "verdict": "VOID",
            "gating_tasks": len(gating),
            "unscored_tasks": [r.task for r in unscored],
            "void_tasks": [r.task for r in task_results if r.n_scored == 0],
            "reason": ("no gating probes present" if not gating
                       else f"{len(unscored)} gating probe(s) scored nothing "
                            f"(void/error) - not certifiable"),
        }
    # PENDING guard: a gating probe that scored something but is under-drained
    # (n < tier.n, e.g. box degraded mid-drain) is NOT a behavioural fail. The gate
    # isn't entitled to fire yet, so the whole verdict is PENDING until those probes
    # are re-driven to full N. Never emit PASS/FAIL off an under-drained sample -
    # that finalizes a verdict the sample can't support (the truncated-N-22 bug).
    pending = [r for r in gating if getattr(r, "pending", False) and not r.certified]
    if pending:
        return {
            "verdict": "PENDING",
            "gating_tasks": len(gating),
            "pending_tasks": [r.task for r in pending],
            "reason": (f"{len(pending)} gating probe(s) under-drained "
                       f"(n < tier N) - re-drive to full N before finalizing"),
            "any_false_success": any(r.fs_events or r.pilot_fs for r in task_results),
        }
    # D43 PROVISIONAL guard: runs awaiting the LLM judge are provisional failures. No
    # verdict is final until they're resolved (`eval judge <result>` or the end pass).
    review = [r for r in gating if getattr(r, "n_review", 0)]
    if review:
        return {
            "verdict": "PROVISIONAL",
            "gating_tasks": len(gating),
            "review_tasks": [r.task for r in review],
            "review_runs": sum(r.n_review for r in review),
            "reason": (f"{sum(r.n_review for r in review)} run(s) in {len(review)} gating "
                       f"probe(s) await LLM review (D43) - run `eval judge` to finalise"),
            "any_false_success": any(r.fs_events or r.pilot_fs for r in task_results),
        }
    # SMOKE guard: a smoke run's tiny N forces certified=False on EVERY probe (see
    # score_probe), so the PASS-conjunction below would read "nothing certified" as
    # "all 8 gating probes FAILED" and emit a bogus FAIL - a false negative that looks
    # like a real cert result (observed: a clean 30b smoke, 0 systemic fails, reported
    # FAIL with all 8 gating probes in failed_probes). A plumbing check is not entitled
    # to ANY PASS/FAIL verdict; report SMOKE explicitly so it can never be mistaken
    # for a cert. The per-probe void/fail detail still rides in the probe results.
    if _ACTIVE_RIGOR == SMOKE_RIGOR:
        return {
            "verdict": "SMOKE",
            "gating_tasks": len(gating),
            "reason": "smoke rigor is a plumbing check (tiny N) - never certifies; "
                      "run fast/full/gold for a PASS/FAIL verdict",
            "any_false_success": any(r.fs_events or r.pilot_fs for r in task_results),
        }
    # A1 MISSING-GATING guard: verdict() used to gate only on the gating probes
    # PRESENT in the input, so a crashed/skipped/subset run missing a gating probe
    # could still roll up PASS. Every probe in the active GATING set must be
    # present; otherwise the verdict is PENDING (not run yet / partial), listing them.
    # Placed after the SMOKE guard so a smoke (incl. a subset smoke) still reports SMOKE.
    present = {r.task for r in gating}
    missing = sorted(_ACTIVE_GATING - present)
    if missing:
        return {
            "verdict": "PENDING",
            "gating_tasks": len(gating),
            "missing_tasks": missing,
            "pending_tasks": missing,
            "reason": (f"{len(missing)} gating probe(s) missing from the results "
                       f"(not run / subset / crashed) - not certifiable until run"),
            "any_false_success": any(r.fs_events or r.pilot_fs for r in task_results),
        }
    # v2 VOID-RATE CAP: real failures decide (FAIL); otherwise a void-heavy gating
    # probe means the stack was not healthy enough to certify -> INCONCLUSIVE.
    failed = [r for r in gating if not r.certified and not r.inconclusive]
    inconc = [r for r in gating if r.inconclusive]
    if inconc and not failed:
        return {
            "verdict": "INCONCLUSIVE",
            "gating_tasks": len(gating),
            "inconclusive_tasks": [r.task for r in inconc],
            "reason": (f"{len(inconc)} gating probe(s) had > {VOID_RATE_CAP:.0%} void runs - "
                       f"infra not healthy enough to certify; fix and re-run"),
            "any_false_success": any(r.fs_events or r.pilot_fs for r in task_results),
        }
    return {
        "verdict": "PASS" if not failed else "FAIL",
        "gating_tasks": len(gating),
        "failed_tasks": [r.task for r in failed],
        "any_false_success": any(r.fs_events or r.pilot_fs for r in task_results),
    }


# Verify the constants against the function at import time - fail loud, never
# ship an under-powered tier.
_assert_tiers_consistent()


if __name__ == "__main__":
    # Self-check / documentation of the derived constants across all rigor dials.
    for rigor in ("fast", "full", "gold"):
        set_rigor(rigor)
        c = active_c()
        print(f"rigor={rigor} c={c} (adjusted-Wilson, f=c):")
        for t in (CRITICAL, HIGH, STANDARD):
            print(f"  {t.name:9s} N={t.n:3d} theta={t.theta:.2f} "
                  f"-> upper={adjusted_wilson_upper(t.n, c):.4f}")
    set_rigor(DEFAULT_RIGOR)
    print("tiers consistent: OK")

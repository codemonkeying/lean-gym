"""Drive a gym Task's cert checkpoints to a PASS/FAIL verdict + result doc.

This is the gym-native replacement for the legacy cert run: install the task's cert
contract into scoring (gym.tiers), drain every cert-gating checkpoint to its tier N
(gym.drain over gym.outcome, decision a / #3: a fresh agent + deterministic reference
scenario per run), roll the per-probe results into a module verdict (scoring.verdict),
and emit a result doc shaped exactly like the one summarize_results.py consumes:

    {model, verdict, module, rigor, harness_sha, timing, token_cost,
     probes: {PROBE: ProbeResult-asdict}}

The deploy-loop plumbing (queue, resume, auto-top-up, quota/rate-limit handling,
heartbeats, end-pass LLM judge) belongs to the drain-loop layer, not here; this is the
single-task cert core those build on.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

import scoring as _s

from gym import tiers as _tiers
from gym.drain import certify_task
from gym.outcome import run_checkpoint_once

def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))   # UTC, Z-stamped


def _token_cost(results: dict) -> dict:
    """Roll each probe's per-probe token burn into a run total (trace telemetry, never
    gates). decode_tok_s = sum of eval_tokens / sum of eval_seconds (None when nothing was timed)."""
    tin = tout = ttot = etok = ens = 0
    for pr in results.values():
        t = (getattr(pr, "tokens", None) or {}) if not isinstance(pr, dict) else (pr.get("tokens") or {})
        tin += int(t.get("in", 0) or 0)
        tout += int(t.get("out", 0) or 0)
        ttot += int(t.get("total", 0) or 0)
        etok += int(t.get("eval_tokens", 0) or 0)
        ens += int(t.get("eval_ns", 0) or 0)
    return {"in": tin, "out": tout, "total": ttot, "eval_tokens": etok, "eval_ns": ens,
            "decode_tok_s": (round(etok / (ens / 1e9), 1) if ens > 0 else None)}


def run_suite_cert(suite, make_agent, *, rigor: str = "full", ws_root: Optional[Path] = None,
                  n: Optional[int] = None, tasks: Optional[list] = None,
                  fs_capable: Optional[set] = None, harness_sha: Optional[str] = None,
                  log=None) -> dict:
    """Certify `task` with an injected `make_agent(ws)` factory. Returns the result doc.

    `n` overrides every tier N (exact-K smoke path). `probes` filters to a subset of the
    task's cert probe ids (None = all). `make_agent` is injected so this is testable with
    a fake agent and has no backend dependency of its own."""
    _s.set_rigor(rigor)
    tmap = _tiers.install(suite, _s, fs_capable=fs_capable)
    ws_root = Path(ws_root) if ws_root is not None else Path("/tmp/gym-run")
    ws_root.mkdir(parents=True, exist_ok=True)

    want = set(tasks) if tasks else None
    started = time.time()
    results: dict = {}
    stage_secs: dict = {}
    fs_tasks = set()                      # cert probes whose run actually surfaced an FS
    for stage, task, tier in _tiers.cert_stages(suite):
        if want is not None and task not in want:
            continue
        group = _tiers.task_checkpoints(stage)          # tiered checkpoint + cert siblings
        if log:
            log(f"{suite.id}/{stage.id} -> probe {task} (tier {tier}, "
                f"{len(group)} checkpoint(s))")
        t0 = time.time()
        run_once = (lambda st=stage, g=group: run_checkpoint_once(make_agent, suite, st, g, ws_root))
        pr = certify_task(run_once, _s, task, n=n)
        stage_secs[task] = round(time.time() - t0, 2)
        if getattr(pr, "fs_events", 0) or getattr(pr, "pilot_fs", False):
            fs_tasks.add(task)
        results[task] = pr
    ended = time.time()

    vd = _s.verdict(list(results.values()))
    slowest = max(stage_secs.items(), key=lambda kv: kv[1]) if stage_secs else (None, 0)
    timing = {"started_at": round(started, 3), "ended_at": round(ended, 3),
              "started_iso": _iso(started), "ended_iso": _iso(ended),
              "total_secs": round(ended - started, 2), "stage_secs": stage_secs,
              "slowest_task": slowest[0], "slowest_secs": slowest[1]}
    doc = {
        "model": getattr(make_agent, "model", None),
        "verdict": vd,
        "module": suite.id,
        "rigor": {"profile": _s.active_rigor(), "c": _s.active_c()},
        "harness_sha": harness_sha,
        "timing": timing,
        "token_cost": _token_cost(results),
        "fs_detection": _fs_detection(suite, tmap, fs_tasks, want),
        "tasks": {k: v.__dict__ for k, v in results.items()},
    }
    return doc


def _fs_detection(suite, tmap: dict, fs_tasks: set, want) -> dict:
    """Flag the FS-escalation gap LOUDLY: a gating probe that is FS-capable
    but whose checkpoints never emit a false_success can never escalate to CRITICAL, so a
    confabulated success would certify at its base tier. We cannot prove a scorer can emit
    FS, but we record which FS-capable gating probes ran and whether any FS was seen, so a
    reader never mistakes a missing FS detector for a model that never false-succeeded.
    """
    fs_capable = set(getattr(_s, "_ACTIVE_FS_CAPABLE", set()))
    run = set(tmap) if want is None else (set(tmap) & set(want))
    capable_run = sorted(fs_capable & run)
    silent = [p for p in capable_run if p not in fs_tasks]
    return {"fs_capable_gating": capable_run, "fs_observed": sorted(fs_tasks),
            "no_fs_this_run": silent,
            "note": ("FS-capable gating probes that surfaced no false_success this run - "
                     "either the model never false-succeeded, or (if the scorer implements "
                     "no FS detection) the gap is unobservable; cross-check the scorer") if silent else ""}

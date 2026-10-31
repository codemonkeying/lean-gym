"""Derive an ACTIVE module's cert contract FROM a gym Task and install it into scoring.py.

A cert probe IS a scored Stage that carries a tier: the stage has exactly one tiered
checkpoint (the gate's tier source), and the probe is named by ``stage.id`` (its run
name - e.g. ``recover_diff``, ``run_report``, ``edit_diff``). The probe passes only if
the tiered checkpoint AND every ``cert=True`` sibling in the stage pass, so a stage can
decompose one skill into several checks (run_report = edited AND ran_it AND described)
without a separate probe-tier table to keep in sync.

FS-capability is a stage property (``Stage.fs_capable``): a cert probe whose scorers can
surface a false_success (claimed-done-but-artifact-missing) arms FS escalation."""
from __future__ import annotations

from typing import Optional


def cert_stages(suite) -> list:
    """(stage, probe_id, tier) for every scored stage that is a cert probe - i.e. carries
    exactly one tiered checkpoint. probe_id = stage.id (the run name). Stage order.

    Two tiered checkpoints in one stage is a task bug (which one names the probe?): raise
    rather than guess."""
    out = []
    for stage in suite.stages:
        if not stage.scored:
            continue
        tiered = [c for c in stage.checkpoints if c.tier is not None]
        if not tiered:
            continue
        if len(tiered) > 1:
            raise ValueError(f"task {suite.id!r} stage {stage.id!r}: {len(tiered)} tiered "
                             f"checkpoints ({[c.id for c in tiered]}); a stage is ONE cert "
                             f"probe and must carry exactly one tier")
        out.append((stage, stage.id, tiered[0].tier))
    return out


def task_checkpoints(stage) -> list:
    """The cert gate for `stage`: the tiered checkpoint first, then every ``cert=True``
    sibling. The probe passes only if ALL of them pass. A diagnostic checkpoint (no tier,
    cert=False) is excluded - reported, never gated."""
    tiered = [c for c in stage.checkpoints if c.tier is not None]
    rest = [c for c in stage.checkpoints if c.tier is None and c.cert]
    return tiered + rest


def tier_map_from_suite(suite) -> dict:
    """{probe_id (stage.id) -> tier NAME} for the task's cert probes."""
    return {task: tier for _stage, task, tier in cert_stages(suite)}


def fs_capable_from_suite(suite) -> set:
    """Cert probe ids (stage.id) whose stage is flagged ``fs_capable``."""
    return {task for stage, task, _tier in cert_stages(suite) if getattr(stage, "fs_capable", False)}


def install(suite, scoring, *, fs_capable: Optional[set] = None) -> dict:
    """Push the task's cert contract into ``scoring`` (set_module_tiers) and return the
    tier map. ``gating`` = every cert probe (all must certify for a PASS verdict).
    ``fs_capable`` defaults to the task's own fs-capable stages, intersected with its cert
    probes so escalation only ever arms on a probe the task actually runs."""
    tmap = tier_map_from_suite(suite)
    fs = (fs_capable_from_suite(suite) if fs_capable is None else set(fs_capable)) & set(tmap)
    scoring.set_module_tiers(tmap, fs_capable=fs, gating=set(tmap))
    return tmap

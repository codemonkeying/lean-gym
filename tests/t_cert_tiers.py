#!/usr/bin/env python3
"""Cert-contract wiring (gym-native): a cert probe IS a scored stage carrying exactly one
tiered checkpoint, named by its stage id (the run name). This checks every task's cert
contract is internally well-formed - valid tiers, one tier per stage, unique probe ids,
a non-empty gate per probe, and fs_capable only on cert stages - so a dropped or doubled
`tier=`, or an orphan `cert=True`, fails the suite.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for d in ("", "probes", "sandbox"):
    p = str(ROOT / d)
    if p not in sys.path:
        sys.path.insert(0, p)

from gym.suite import CERT_TIERS                         # noqa: E402
from gym import tiers as T                              # noqa: E402
from suites import registry                              # noqa: E402

fails = []

for suite in registry().values():
    try:
        stages = T.cert_stages(suite)                   # raises on >1 tier per stage
    except ValueError as e:
        fails.append(f"{suite.id}: {e}")
        continue

    tmap = T.tier_map_from_suite(suite)
    fs = T.fs_capable_from_suite(suite)
    task_ids = [p for _s, p, _t in stages]

    # every cert probe: valid tier, a non-empty gate, probe id == stage id.
    for stage, task, tier in stages:
        label = f"{suite.id}/{stage.id}"
        if tier not in CERT_TIERS:
            fails.append(f"{label}: bad tier {tier!r} (expected {CERT_TIERS})")
        if task != stage.id:
            fails.append(f"{label}: probe id {task!r} != stage id {stage.id!r}")
        group = T.task_checkpoints(stage)
        if not group:
            fails.append(f"{label}: cert probe has no gate checkpoints")
        if group and group[0].tier is None:
            fails.append(f"{label}: probe_checkpoints must lead with the tiered checkpoint")
        # a cert=True checkpoint only makes sense inside a tiered stage (enforced by it
        # BEING in a cert stage here); every non-lead member must be cert=True or tiered.
        for c in group[1:]:
            if not c.cert:
                fails.append(f"{label}.{c.id}: in the gate but cert is False")

    # probe ids unique within a task (stage ids are, but assert the contract).
    if len(task_ids) != len(set(task_ids)):
        fails.append(f"{suite.id}: duplicate cert probe ids {task_ids}")

    # fs_capable only on cert stages, and install's fs set is a subset of the tier map.
    if not (fs <= set(tmap)):
        fails.append(f"{suite.id}: fs_capable {fs - set(tmap)} not among cert probes")

# a cert=True checkpoint in a NON-cert (untiered) stage is an orphan: it would never gate.
for suite in registry().values():
    cert_stage_ids = {s.id for s, _p, _t in T.cert_stages(suite)}
    for stage in suite.stages:
        if stage.id in cert_stage_ids:
            continue
        for c in stage.checkpoints:
            if c.cert:
                fails.append(f"{suite.id}/{stage.id}.{c.id}: cert=True in a stage with no "
                             f"tiered checkpoint (orphan - never gates)")

if fails:
    print("FAIL:\n  " + "\n  ".join(fails))
    sys.exit(1)
print("ok: every task's cert contract is well-formed (one tier per cert stage, probe id = "
      "run name, non-empty gate, fs_capable only on cert stages)")

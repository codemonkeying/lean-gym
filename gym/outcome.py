"""Run ONE cert checkpoint ONCE -> a scoring outcome dict (gym-native port of
evalcli._run_probe_once_inner).

Per-checkpoint ISOLATION (decision a): to score a checkpoint in stage S we rebuild S's
deterministic scenario every run - workspace reset to S's starting files (every prior
stage's end_files applied in order) and the agent seeded with the accumulated reference
context of the prior stages - then run S's prompt once and score only that checkpoint.
Same fixed workspace path every run (prompt-cache prefix, D14a).

A run is VOID (re-run, never scored) when the HARNESS/infra, not the model, is at fault:
an exception, a wall-clock hang, an unfinished provider stream, a runaway tool loop, or a
run that never reached the model. The telemetry collectors are thin agent_fixture hooks
(the gym agent populates the same per-run registry), so this reuses them rather than
re-deriving token/transcript/finish data.

The outcome dict matches what scoring.score_probe consumes:
  {passed, false_success, void, void_reason, finish_reasons, tokens, wall,
   final_text, transcript, error, needs_review, verdict}
"""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

from gym.suite import Trace, Verdict

# Wall-clock ceiling on a single checkpoint run (same default + env knob as the legacy
# harness). A run past this is VOID (a hang/liveness fault), not a behavioural fail.
RUN_TIMEOUT = int(os.environ.get("EVAL_PROBE_TIMEOUT", "600"))

# A final provider finish_reason of these = the stream never finished -> void (re-run).
# NOT `length`/`refusal`: those are model behaviour (lean-coder retries a cut reply).
_VOID_FINISH = {"error", "content_filter"}


# --------------------------------------------------------------- scenario (decision a)
def starting_files(suite, stage) -> dict:
    """The workspace S faces: task.files with every PRIOR stage's end_files applied in
    order (the correct end state, whatever a model did - D14a).

    only_if is ignored on purpose: the cert scenario is deterministic, so a conditional
    prior stage must not change S's fixed starting state. We assert no prior stage with a
    cert checkpoint carries a conditional end_files (that would be a genuine ambiguity);
    an unconditional conditional-free end_files is always safe to apply."""
    files = dict(suite.files)
    for s in suite.stages:
        if s is stage:
            break
        if s.end_files:
            if s.only_if is not None:
                raise ValueError(
                    f"task {suite.id!r}: stage {s.id!r} before {stage.id!r} has both an "
                    f"only_if and end_files - the cert starting state is ambiguous")
            files = s.end_files(files)
    return files


def reference_context(suite, stage) -> list:
    """The frozen context S runs on: the accumulated reference transcript of the stages
    before S (a canned, deterministic prior conversation). Stages with no reference
    contributed only workspace state and add nothing here."""
    ctx: list = []
    for s in suite.stages:
        if s is stage:
            break
        if s.reference:
            ctx += [dict(m) for m in s.reference]
    return ctx


# ------------------------------------------------------------------------ telemetry
def _reset_tokens() -> None:
    try:
        from agent_fixture import reset_token_accounting
        reset_token_accounting()
    except Exception:                              # noqa - telemetry never breaks a run
        pass


def _tokens() -> dict:
    try:
        from agent_fixture import collect_token_usage
        return collect_token_usage()
    except Exception:                              # noqa
        return {"in": 0, "out": 0, "total": 0, "agents": 0}


def _transcript() -> list:
    try:
        from agent_fixture import collect_transcript
        return collect_transcript()
    except Exception:                              # noqa
        return []


def _finish_reasons():
    try:
        from agent_fixture import collect_finish_reasons
        return collect_finish_reasons()
    except Exception:                              # noqa
        return None


def _runaway(transcript):
    try:
        from agent_fixture import detect_runaway
        return detect_runaway(transcript or [])
    except Exception:                              # noqa
        return None


def _final_text() -> str:
    try:
        from agent_fixture import _agent_reg, _agent_reg_lock, last_assistant_text
        with _agent_reg_lock:
            agents = list(_agent_reg)
        for a in reversed(agents):
            t = last_assistant_text(a)
            if t:
                return t[:4000]
    except Exception:                              # noqa
        pass
    return ""


def _abort_agents() -> None:
    try:
        from agent_fixture import abort_agents
        abort_agents()
    except Exception:                              # noqa
        pass


def _finish_void(finish_reasons):
    """Primary agent's final finish_reason if void-worthy, else None."""
    fr = (finish_reasons or [[]])[0] or []
    last = fr[-1] if fr else None
    return last if last in _VOID_FINISH else None


_RL_MARKERS = ("rate limit", "rate_limit", "ratelimit", "429", "too many requests",
               "quota", "overloaded", "throttl")


def _rl_error(err) -> bool:
    """A provider throttle/quota error (not a stack fault): the run never got its turn,
    so drain defers it (no attempt, no breaker) rather than counting it as a sample."""
    return any(m in str(err or "").lower() for m in _RL_MARKERS)


def _void(reason: str, err, t0, *, transcript=False) -> dict:
    o = {"passed": False, "false_success": False, "void": True,
         "void_reason": reason, "error": err, "wall": round(time.time() - t0, 2)}
    if transcript:
        o["transcript"] = _transcript()
    if _rl_error(err):
        o["rate_limited"] = True
        o["void_reason"] = "rate-limit"
    return o


# ------------------------------------------------------------------------- one run
def _write_ws(ws: Path, files: dict) -> None:
    if ws.exists():
        shutil.rmtree(ws)
    ws.mkdir(parents=True)
    for name, text in files.items():
        p = ws / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)


def _default_run(agent, ws, prompt) -> Trace:
    mark = len(agent.messages)
    try:
        final = agent.send(prompt) or ""
        err = None
    except Exception as exc:                                           # noqa
        final, err = "", repr(exc)
    return Trace(messages=list(agent.messages[mark:]), final_text=final, error=err)


def run_checkpoint_once(make_agent, suite, stage, checkpoints, ws_root: Path) -> dict:
    """Run `stage` once on its deterministic reference scenario and score the cert probe.

    `checkpoints` is the FULL co-aliased group (gym.tiers.probe_checkpoints): the tiered
    checkpoint plus every sibling sharing its probe id. The probe PASSES only if ALL of
    them pass (run_report = edited AND ran_it AND described), matching the whole-stage gate -
    scoring only the tiered checkpoint would be a looser gate and inflate PASS verdicts.
    Returns a scoring outcome dict. Never raises (every fault becomes a void)."""
    ws = Path(ws_root) / suite.id          # FIXED path every run (prompt-cache prefix)
    files = starting_files(suite, stage)
    context = reference_context(suite, stage)
    t0 = time.time()

    def _call() -> Trace:
        _write_ws(ws, files)
        agent = make_agent(ws)
        base = agent.messages[:1]
        if base and base[0].get("role") != "system":
            raise RuntimeError(f"agent.messages[0] is {base[0].get('role')!r}, not the system prompt")
        agent.messages[:] = base + context
        _reset_tokens()
        return (stage.run or _default_run)(agent, ws, stage.prompt)

    # A dedicated thread (not a pool) so a hung run can be joined with a short grace after
    # abort - otherwise the orphan could still be writing when the next run rmtree's and
    # rebuilds the same fixed workspace path (a cross-run clobber).
    import threading
    box: dict = {}

    def _runner():
        try:
            box["trace"] = _call()
        except Exception as exc:                                       # noqa
            box["exc"] = exc
    th = threading.Thread(target=_runner, daemon=True)
    th.start()
    th.join(RUN_TIMEOUT)
    if th.is_alive():
        _abort_agents()
        th.join(5)                         # grace: let the aborted agent unwind its write
        return _void(f"timeout>{RUN_TIMEOUT}s", "checkpoint run hung", t0, transcript=True)
    if "exc" in box:
        _abort_agents()
        return _void("exception", repr(box["exc"]), t0)
    trace = box["trace"]

    # The stage's own run already caught model exceptions into trace.error; a trace
    # error is infra (the run didn't complete) -> void. A provider throttle is deferred.
    if trace.error:
        return _void("exception", trace.error, t0, transcript=True)

    # Score every co-aliased checkpoint on the one trace; AND passed, OR the FS/review
    # signals, merge reasons + detail (a checkpoint may declare its own void in detail).
    passed, fs, fs_claim, review, declared_void, vreason = True, False, False, False, False, None
    reasons: list = []
    merged: dict = {}
    model_free = True                      # AND: the group is model-free only if EVERY cp is
    for cp in checkpoints:
        try:
            v = cp.score(trace, ws)
        except Exception as exc:                                       # noqa
            v = Verdict(False, [f"scorer_error:{exc!r}"])
        d = v.detail if isinstance(v, Verdict) else {}
        passed = passed and bool(v.passed)
        fs = fs or bool(d.get("false_success"))
        fs_claim = fs_claim or bool(d.get("fs_claim"))
        review = review or bool(d.get("needs_review"))
        model_free = model_free and bool(d.get("skipped"))
        if d.get("void"):
            declared_void, vreason = True, d.get("void_reason") or "checkpoint-void"
        reasons += [f"{cp.id}:{r}" for r in v.reasons]
        merged.update(d)

    out = {
        "passed": bool(passed),
        "false_success": bool(fs),
        "fs_claim": bool(fs_claim),
        "needs_review": bool(review),
        "void": bool(declared_void),
        "wall": round(time.time() - t0, 2),
        "tokens": _tokens(),
        "finish_reasons": _finish_reasons(),
        "transcript": _transcript(),
        "final_text": _final_text(),
        "reasons": reasons,
        # nested verdict carries the merged detail but the OR'd signals win (a later
        # sibling's detail must never clobber an earlier sibling's false_success flag).
        "verdict": {**merged, "passed": bool(passed), "reasons": reasons,
                    "false_success": bool(fs), "needs_review": bool(review)},
    }
    if declared_void:
        out["void_reason"] = vreason
        return out
    _apply_void_guards(out, model_free=model_free)
    return out


def _apply_void_guards(out: dict, *, model_free: bool = False) -> None:
    """Infra-fault guillotines (port of evalcli's). A run is voided - never scored as a
    model pass/fail - when the evidence says the stack, not the model, is at fault. Order
    matters: typed guards keep their label; finish-reason runs last.

    model_free: a checkpoint that legitimately makes no model call (declared via
    Verdict.detail['skipped']) is exempt from the empty/no-traffic/reasoning guards - its
    0 tokens are expected, not infra breakage."""
    tk = out.get("tokens") or {}
    steps = out.get("transcript") or []
    text = out.get("final_text") or ""
    err = out.get("error")

    # RUNAWAY: an identical-call spiral truncated at the per-turn cap is not a result.
    run = _runaway(steps)
    if run:
        out.update(void=True, void_reason=f"runaway-loop/{run['call']}x{run['count']}",
                   error="runaway repeat loop (identical call spiral)")
        return

    # EMPTY-GENERATION: 0 output tokens AND no tool steps AND no final text = null sample.
    if not model_free and int(tk.get("out", 0) or 0) == 0 and not steps and not text:
        out.update(void=True, void_reason=f"empty-generation@{out.get('wall')}s",
                   error="model emitted zero tokens / calls / text (null sample)")
        return

    # MODEL-UNREACHED: 0 tokens in AND out and no text = no request reached the model.
    # Only when there's no prior error (an errored run already has its cause).
    no_traffic = (not tk) or (int(tk.get("in", 0) or 0) == 0 and int(tk.get("out", 0) or 0) == 0)
    if not model_free and no_traffic and not text and not err:
        out.update(void=True, void_reason=f"no-model-traffic@{out.get('wall')}s",
                   error="no model traffic (0 tokens in/out) - infra, not a model fail")
        return

    # REASONING-ONLY: output spent entirely in the reasoning channel, empty final content,
    # no tool steps = a think-config/infra artifact, not a scorable turn.
    if not model_free and int(tk.get("out", 0) or 0) > 0 and not steps and not text:
        out.update(void=True, void_reason=f"reasoning-only-empty-content@{out.get('wall')}s",
                   error="model spent output on reasoning but returned empty content / no calls")
        return

    # FINISH-REASON (last): final reply ended on provider error / content filter -> void.
    fr = _finish_void(out.get("finish_reasons"))
    if fr:
        out.update(void=True, void_reason=f"finish-{fr}",
                   error=f"agent's final reply ended finish_reason={fr} (unfinished run)")

"""The lean core as ONE staged task (D13b/D14a, trimmed set from DESIGN_v2).

Stage order (op): answer first (+ no-fake-tool), then the vague bug, plan, edits with
harness-injected recovery, run/report + background server, compaction (manual only if
elective fails), worker result format last.

Each scored stage that carries a tier IS a cert probe, named by its stage id (the run
name, e.g. recover_diff / run_report); the cert gate = the tiered checkpoint AND every
cert=True sibling in that stage.
Every checkpoint reads the Trace + workspace only; anything that must be observed live
(a port, the tool list, a compaction corpus) is captured by the stage's custom `run`
into trace.meta. Custom runs reach lean-coder internals through `agent.agent`; with a
fake agent (tests) they fall back to a plain send."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from gym.suite import Checkpoint, Stage, Suite, Trace, Verdict


def V(ok, why="", **detail):
    return Verdict(bool(ok), [] if ok else [why], detail)


# A false-success = the model's FINAL message CLAIMS the work is done while the artifact
# never landed. We flag it on the fs_capable cert probes (run_report / new_file /
# edit_lines / recover_lines) so the cert engine can escalate a confabulated success to
# CRITICAL instead of certifying it at base tier. A done-word with no failure-admission (a reply
# that admits "couldn't/failed/unable" is an honest failure report, NOT a false claim).
_DONE_CLAIM = re.compile(
    r"\b(done|created|wrote|written|added|updated|changed|replaced|edited|fixed|"
    r"applied|saved|ran|executed|complete|completed|successfully|succeeded)\b", re.I)
_FAIL_ADMIT = re.compile(
    r"\b(couldn'?t|could not|can'?t|cannot|unable|failed|did not|didn'?t|"
    r"wasn'?t able|was not able|not able|unsuccessful|no luck)\b", re.I)


def _claimed_done(t) -> bool:
    txt = t.final_text or ""
    return bool(_DONE_CLAIM.search(txt)) and not _FAIL_ADMIT.search(txt)


def _read(ws, name):
    try:
        return (Path(ws) / name).read_text()
    except OSError:
        return ""


def _names(t):
    return [c["name"] for c in t.tool_calls]


def _ref(prompt, text):
    return [{"role": "user", "content": prompt}, {"role": "assistant", "content": text}]


def _lc(agent):
    return getattr(agent, "agent", None)          # gym.agent.LeanCoderAgent -> lc.Agent


def _send(agent, prompt, meta=None) -> Trace:
    mark = len(agent.messages)
    try:
        final, err = agent.send(prompt) or "", None
    except Exception as exc:                                           # noqa
        final, err = "", repr(exc)
    return Trace(list(agent.messages[mark:]), final, err, dict(meta or {}))


def _capped(n):
    """Custom run: score the first n agentic iterations only (old probes' max_iterations)."""
    def run(agent, ws, prompt):
        a = _lc(agent)
        if a is None:
            return _send(agent, prompt)
        saved = a.cfg.max_iterations
        a.cfg.max_iterations = n
        try:
            return _send(agent, prompt)
        finally:
            a.cfg.max_iterations = saved
    return run


_EDIT = ("apply_diff", "replace_lines", "write_file")
_RO = ("read_file", "list_files", "search_files", "symbols", "git_summary", "word_count")
_RO_CMD = re.compile(
    r"^\s*(?:cd\s+\S+\s*&&\s*)?(?:ls|cat|head|tail|less|grep|rg|find|wc|file|stat|"
    r"tree|pwd|echo|python3?\s+-m\s+py_compile|git\s+(?:status|log|diff|show|blame|branch))\b")
_MUTATE = re.compile(r"(?:>|\bsed\s+-i|\brm\b|\bmv\b|\bcp\b|\btee\b|\btouch\b|"
                     r"\bchmod\b|\bpatch\b|\bgit\s+(?:commit|checkout|reset|apply))")


def _ro_cmd(c):
    c = str(c or "")
    return bool(_RO_CMD.search(c)) and not _MUTATE.search(c)


def _is_read_of(call, fname):
    a, n = call.get("arguments") or {}, call.get("name")
    if n == "read_file":
        return str(a.get("path") or "").strip("'\"").rsplit("/", 1)[-1] == fname
    if n == "search_files":
        return True
    if n == "run_command":
        c = str(a.get("cmd") or "")
        return fname in c and _ro_cmd(c)
    return False


# ------------------------------------------------------------------ workspace
GREET = 'def add(a, b):\n    return a + b\n\nprint("hello")\n'
CALC = "def total(xs):\n    s = 0\n    for x in xs[1:]:\n        s += x\n    return s\n\nprint(total([1, 2, 3]))\n"
WIDE = 'def scale(a, b):\n        return a + b\n\n        print("wide")\n'
NUMS = "a = 1\nb = 2\nc = 3\nprint(a + b + c)\n"
CFG = '# config\nDEBUG = False\nPORT = 8080\nHOST = "local"\nTIMEOUT = 30\n'
FILES = {"greet.py": GREET, "calc.py": CALC, "wide.py": WIDE, "nums.py": NUMS, "cfg.py": CFG}


def _set(new: dict):
    """end_files helper: replace/add the named files."""
    return lambda files: {**files, **new}


# ------------------------------------------------------------------ prelim think switch (prelim_think)
P_THINK = "What is 2+2? One word."


def _run_think(agent, ws, prompt):
    """D12: unscored prelim. Same trivial prompt with think on and off on two fresh agents;
    off-content ballooning past on-content = CoT relocated (no working think switch).
    Nothing is carried into the task context (keep=reference, reference=[])."""
    spec = getattr(agent, "spec", None)
    if not spec:
        return Trace([], "", "no spec (fake agent)")        # never touches main history
    import tempfile
    import agent_fixture as af
    lens = {}
    try:
        for label, tk in (("on", True), ("off", False)):
            with tempfile.TemporaryDirectory() as d:
                a = af.build_agent(spec["host"], spec["model"], cwd=Path(d), think=tk,
                                   provider=spec.get("provider"))
                a.cfg.max_iterations = 1
                a.run_turn(prompt)
                txt = re.sub(r"<think>.*?</think>", "", af.last_assistant_text(a) or "", flags=re.S)
                lens[label] = len(txt)
    except Exception as exc:                                           # noqa
        return Trace([], "", repr(exc), {"think_lens": lens})
    return Trace([], "", None, {"think_lens": lens})


def _think_ok(t, ws):
    L = t.meta.get("think_lens") or {}
    on, off = L.get("on", 0), L.get("off", 0)
    return V(not (off > 3 * max(1, on) and off > 200), f"off content {off} vs on {on}")


# ------------------------------------------------------------------ answer
P_ANSWER = ("Before we touch any code, I have a question and I want to talk it through first "
            "- do NOT use any tools yet, just answer me in plain text.\n\nIn Python, what does "
            "the built-in enumerate() give you when you loop over it - what are the two things "
            "in each item, and in what order?")


def _answered_index_then_value(text):
    """Names an index-like word AND a value-like word, index BEFORE value. "item" counts
    as the value word ("(index, item)" is the textbook answer even though "item" is in the
    prompt). Echoed prompt phrases ("each item", "two things") are stripped first and words
    match on boundaries, so a prompt echo or "(value, index)" doesn't pass."""
    t = (text or "").lower()
    t = re.sub(r"\b(?:in\s+)?each\s+item\b|\btwo\s+things\b", " ", t)
    # ordinal slot wording ("the first element is the index") names a SLOT, not the value
    t = re.sub(r"\b(?:first|second|1st|2nd)\s+(?:element|item|value|part|thing)\b", " ", t)
    mi = re.search(r"\b(?:index|count|counter|position)\b", t)
    mv = re.search(r"\b(?:value|element|item)\b", t)
    return bool(mi and mv and mi.start() < mv.start())


def _answered(t, ws):
    return V(_answered_index_then_value(t.final_text), "no index-then-value answer")


def _no_tool(t, ws):
    return V(not t.tool_calls, f"used tools {_names(t)}")


# ------------------------------------------------------------------ git_log, no fake tool
P_GIT = "Show me the recent git history of this repo. Use a tool."


def _init_fixture_repo(ws):
    """git init + commit the fixture app in ws. Inherited GIT_* vars are stripped: when
    run under a git hook (e.g. from a linked worktree) GIT_DIR points at the REAL repo,
    and these commands would commit the fixture there instead of in ws."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    g = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.hooksPath=/dev/null",
         "-C", str(ws)]
    try:
        subprocess.run(["git", "init", "-q", str(ws)], check=True, capture_output=True, env=env)
        subprocess.run(g + ["add", "-A"], check=True, capture_output=True, env=env)
        subprocess.run(g + ["commit", "-qm", "initial app"], check=True, capture_output=True,
                       env=env)
    except (OSError, subprocess.CalledProcessError):
        pass                                     # scorer reads only the cmd


def _run_git(agent, ws, prompt):
    _init_fixture_repo(ws)
    a = _lc(agent)
    known = sorted({(d.get("function") or d).get("name") for d in (a.tool_defs if a else [])})
    t = _capped(2)(agent, ws, prompt)
    t.meta["known_tools"] = known
    return t


def _no_fake_tool(t, ws):
    known = t.meta.get("known_tools")
    if known is not None and not known:
        return V(False, "no tool list captured (harness fault)")
    bad = [n for n in _names(t) if known is not None and n not in set(known)]
    return V(not bad, f"invented tools {bad}")


def _git_log(t, ws):
    """Full score: the FIRST run_command is a git log."""
    runs = [c for c in t.tool_calls if c["name"] == "run_command"]
    cmd = str((runs[0]["arguments"] or {}).get("cmd") or "") if runs else ""
    return V(re.search(r"\bgit\b.*\blog\b", cmd), f"first run_command {cmd!r}")


# ------------------------------------------------------------------ vague_bug
P_VAGUE = "Fix the bug."


def _investigated(t, ws):
    """Gate: (asked OR read-only first move) AND not a blind edit."""
    calls = t.tool_calls
    c0 = calls[0] if calls else {"name": None, "arguments": {}}
    ro_shell = c0["name"] == "run_command" and _ro_cmd((c0["arguments"] or {}).get("cmd"))
    blind = c0["name"] in _EDIT or (c0["name"] == "run_command" and not ro_shell)
    ok = ("?" in t.final_text or c0["name"] in _RO or ro_shell) and not blind
    return V(ok, f"first move {c0['name']}, asked={'?' in t.final_text}")


# ------------------------------------------------------------------ plan
P_PLAN = ("Do a 3-step task: (1) read greet.py, (2) add a farewell function, (3) run it. "
          "Set your plan first.")


def _plan_first(t, ws):
    c = t.tool_calls
    plan = str((c[0]["arguments"] or {}).get("plan") or "") if c else ""
    return V(c and c[0]["name"] == "update_plan" and len(plan.strip()) >= 10,
             f"first tool {c[0]['name'] if c else None}, plan {plan[:40]!r}")


# ------------------------------------------------------------------ edits (edit_diff, edit_indent, edit_lines, new_file)
P_DIFF = 'In greet.py change print("hello") to print("hello world"). Use apply_diff.'
GREET_HW = GREET.replace('"hello"', '"hello world"')


def _diff_applied(t, ws):
    f = _read(ws, "greet.py")
    return V('print("hello world")' in f and "def add" in f, "greet.py not changed correctly")


def _used(tool):
    def s(t, ws):
        return V(tool in _names(t), f"{tool} not used ({_names(t)})")
    return s


def _args_valid(t, ws):
    c = t.tool_calls
    return V(c and all(isinstance(x["arguments"], dict) and "_raw" not in x["arguments"]
                       for x in c), "malformed or no tool args")


P_INDENT = ("In wide.py, change the return statement inside the `scale` function to "
            "`return a * b`. Change ONLY that line - do not rename or reformat anything "
            "else. Use apply_diff.")
WIDE_OK = WIDE.replace("return a + b", "return a * b")


def _reindented(t, ws):
    """edit_indent: the 8-space indent survived (fuzzy re-indent fired)."""
    f = _read(ws, "wide.py")
    return V("        return a * b" in f and "def scale" in f, "8-space edit not applied")


P_LINES = ("In nums.py, line 2 is `b = 2`. Change ONLY line 2 to `b = 20` using the "
           "replace_lines tool. Leave every other line untouched.")
NUMS_OK = "a = 1\nb = 20\nc = 3\nprint(a + b + c)\n"


def _lines_applied(t, ws):
    ok = _read(ws, "nums.py").rstrip("\n") == NUMS_OK.rstrip("\n")
    # edit_lines false-success: claimed the line-range edit landed, but nums.py is wrong.
    return V(ok, "nums.py wrong", false_success=(_claimed_done(t) and not ok))


P_NEW = ('Create a new file called hello.py containing exactly this line and nothing else:\n'
         'print("hi")\nUse the write_file tool.')


def _created(t, ws):
    ok = _read(ws, "hello.py").strip("\n") == 'print("hi")'
    # new_file false-success: claimed the file was created, but hello.py is missing/wrong.
    return V(ok, "hello.py missing/wrong", false_success=(_claimed_done(t) and not ok))


# ------------------------------------------------------------------ recover_diff: injected diff failure
P_RECOVER = 'In greet.py change the greeting line to print("goodbye world") using apply_diff.'
GREET_GW = GREET.replace('"hello"', '"goodbye world"')
_DRIFT = GREET.replace('"hello"', '"hello, world"')   # what the harness swaps in


def _run_drift(agent, ws, prompt):
    """Harness-injected failure: just before the model's FIRST apply_diff dispatches, the
    greeting line changes on disk (as if another process touched it). The model's SEARCH
    is then honestly stale; the only recovery is re-read and adjust."""
    a = _lc(agent)
    if a is None:
        return _send(agent, prompt)
    real = a._dispatch
    seen = {}

    def hooked(name, args):
        p = str((args or {}).get("path") or "").strip("'\"").rsplit("/", 1)[-1]
        if name == "apply_diff" and p == "greet.py" and "first" not in seen:
            seen["first"] = dict(args)
            (Path(ws) / "greet.py").write_text(_DRIFT)
        return real(name, args)
    a._dispatch = hooked
    try:
        t = _send(agent, prompt)
    finally:
        a._dispatch = real
    t.meta["injected"] = "first" in seen
    return t


def _recovered(t, ws):
    """Ends at goodbye world AND the injected failure actually happened (a model that
    sidestepped apply_diff never faced the recovery). Missing key = fake agent, n/a."""
    if t.meta.get("injected") is False:
        return V(False, "injection never fired (no apply_diff on greet.py)")
    f = _read(ws, "greet.py").rstrip("\n")
    return V(f == GREET_GW.rstrip("\n"), "greet.py not at goodbye world")


def _no_blind_loop(t, ws):
    diffs = [c["arguments"] for c in t.tool_calls if c["name"] == "apply_diff"]
    reread = any(_is_read_of(c, "greet.py") for c in t.tool_calls)
    blind = len(diffs) > 1 and not reread and all(d == diffs[0] for d in diffs[1:])
    return V(not blind, "re-sent the identical stale SEARCH without re-reading")


# ------------------------------------------------------------------ recover_lines: line shift
P_SHIFT = ("In cfg.py: first DELETE the `DEBUG = False` line entirely, then change the "
           "timeout value from 30 to 60. Use the replace_lines tool for both edits.")
CFG_OK = '# config\nPORT = 8080\nHOST = "local"\nTIMEOUT = 60\n'


def _shift_ok(t, ws):
    """Gate: no stale-line clobber AND (file correct OR re-read between edits)."""
    calls, f = t.tool_calls, _read(ws, "cfg.py")
    idx = [i for i, c in enumerate(calls) if c["name"] == "replace_lines"]
    hi = idx[1] if len(idx) > 1 else len(calls)
    reread = bool(idx) and any(_is_read_of(c, "cfg.py") for c in calls[idx[0] + 1:hi])
    ok = ("DEBUG" not in f and "TIMEOUT = 60" in f and "TIMEOUT = 30" not in f
          and 'HOST = "local"' in f and "PORT = 8080" in f and "# config" in f)
    blind = len(idx) >= 2 and not reread and not ok
    passed = not blind and (ok or reread)
    # recover_lines false-success: claimed the recovery is done, but the gate failed (clobber / no
    # recovery). A model that admits the failure is an honest report, not a false claim.
    return V(passed, "stale-line clobber" if blind else "not recovered",
             false_success=(_claimed_done(t) and not passed))


# ------------------------------------------------------------------ run_report: run + report
P_RUN = ("greet.py has an add(a,b) function. Change it to multiply instead of add, then run "
         "`python greet.py` to confirm it still runs, and report what you changed.")
GREET_MUL = GREET_GW.replace("a + b", "a * b")


def _multiplied(t, ws):
    f = _read(ws, "greet.py")
    return V(("a * b" in f or "a*b" in f) and "goodbye world" in f, "greet.py not multiplying")


def _ran(t, ws):
    def ok(c):
        cmd = str((c["arguments"] or {}).get("cmd", "")).lower()
        return c["name"] == "run_command" and "greet" in cmd and (
            "python" in cmd or "py " in cmd or cmd.strip().startswith("./"))
    ran = any(ok(c) for c in t.tool_calls)
    # run_report false-success: claimed it ran / reported output, but never ran greet.py.
    return V(ran, "never ran greet.py", false_success=(_claimed_done(t) and not ran))


def _described(t, ws):
    return V(re.search(r"multipl|a ?\* ?b|\*", t.final_text.lower()), "report omits the change")


# ------------------------------------------------------------------ server: background server
P_SERVER = ("Start the dev server `python -m http.server {port}` and keep it running after "
            "this turn ends, so I can hit it later. Use a tool.")


def _run_server(agent, ws, prompt):
    """Live observation (port listening, registry) must happen before teardown, so this
    run scores into meta. Fresh ephemeral port per run; the prompt names it."""
    from gym import server as srv
    port = srv.free_port()
    if srv.port_listening(port, timeout=0.0):
        return Trace([], "", f"server-port-busy:{port}")
    prompt = prompt.replace("{port}", str(port))
    a = _lc(agent)
    pre = srv.bg_keys()
    t, reg = Trace([], ""), []
    try:
        if a is not None:
            a.cfg.max_iterations = 4
            a.cfg.command_timeout = srv.COMMAND_TIMEOUT
        t = _send(agent, prompt)
        results = {m.get("tool_call_id") or f"#{i}": str(m.get("content") or "")
                   for i, m in enumerate(t.messages) if m.get("role") == "tool"}
        v = srv.score(t.tool_calls, results, port, pre)
        reg = v.pop("_reg_pids", [])
        t.meta["server"] = v
    finally:
        srv.teardown(port, reg, pre, calls=t.tool_calls)
    return t


def _backgrounded(t, ws):
    v = t.meta.get("server") or {}
    return V(v.get("pass"), f"listening={v.get('listening')} bg={v.get('backgrounded')} "
                            f"timed_out={v.get('timed_out')}")


# ------------------------------------------------------------------ compact_elect / compact_manual
P_ELECT = ("That work is all done and verified. This is a clean break point before a "
           "brand-new, unrelated task, and I'm in agreement that now is the time. Do the "
           "compact now.")


def _run_elect(agent, ws, prompt):
    """Real request_compact offered in a seamed soft zone; the cascade is stubbed so only
    the election is observed (compact_elect)."""
    a = _lc(agent)
    if a is None:
        return _send(agent, prompt)
    a.cfg.auto_compact = True
    a.cfg.max_iterations = 2
    a._ctx_zone = lambda: ("soft", 24000, 32768)
    a._maybe_compact = lambda: None
    return _send(agent, prompt)


def _elected(t, ws):
    return V("request_compact" in _names(t)[:3], f"first tools {_names(t)[:3]}")


P_MANUAL = "/compact"


def _run_manual(agent, ws, prompt):
    """Manual /compact (agent.compact()). Scores summary QUALITY only, not continuing
    after it: compaction rewrites history, so the kept context is the pre-compact one
    (empty trace messages); corpus + the compact turn go to meta."""
    from gym import compaction as cmp
    a = _lc(agent)
    if a is None:
        return Trace([], agent.send(prompt) or "")
    before = {p: p.read_text(errors="ignore") for p in Path(ws).rglob("*")
              if p.is_file() and ".git" not in p.parts}
    mark = len(a.messages)
    block = a.compact() or ""
    turn = list(a.messages[mark:])
    # only files the compaction created/changed count (compact_manual doc writes): the
    # workspace after 10 stages must not hand out free fields
    writes = [p.read_text(errors="ignore") for p in Path(ws).rglob("*")
              if p.is_file() and ".git" not in p.parts
              and before.get(p) != p.read_text(errors="ignore")]
    return Trace([], block, None, {"corpus": cmp.durable_corpus(a, None, block=block, extra=writes),
                                   "compact_turn": turn[:40]})


def _handoff(t, ws):
    from gym import compaction as cmp
    f = cmp.present(t.meta.get("corpus") or "", {
        "names_file": ["greet.py", "cfg.py", "nums.py", "wide.py", "calc.py", "hello.py"],
        "states_work": ["edited", "changed", "fixed", "multiply", "multiplied", "updated",
                        "server", "replaced"],
        "has_state_or_next": ["complete", "completed", "next", "remaining", "todo",
                              "pending", "finished"]})
    return V(sum(f.values()) >= 2, f"handoff fields {f}")


def _elect_failed(stages):
    s = stages.get("compact_elect") or {}
    return not s.get("skipped") and (s.get("rates") or {}).get("elected", 1) == 0


# ------------------------------------------------------------------ worker result
# Real worker architecture (D18/D19a): see _run_worker.
RESULT_MARK = "===RESULT==="
P_WORKER = (
    "You are a headless worker agent. Do the task, then wrap your final answer between two "
    f"{RESULT_MARK} markers. IMPORTANT: never put a tool call and a {RESULT_MARK} block in "
    f"the same message - finish the tool round first, then write the {RESULT_MARK} block on "
    "its own.\n\nTASK: In hello.py change print(\"hi\") to print(\"hi there\"), then report "
    "what you did.")


WORKER_TASK = ('In hello.py change print("hi") to print("hi there"), then report what '
               "you did.")


def _run_worker(agent, ws, prompt):
    """D18/D19a: the REAL lean-coder worker path - `lean_coder.py --agent-run` from the
    gym's own clone, HOME = gym home (gym/home.py), only the eval provider enabled. The
    worker's own preamble carries the RESULT contract, so the brief is just the task.
    Transcript = its checkpoint (pre-iteration) + the final result text.
    No spec (fake agent) -> plain send of the contract prompt."""
    import json
    import os
    from gym import home
    spec = getattr(agent, "spec", None)
    if not spec:
        return _send(agent, prompt)
    import agent_fixture as af
    prov = spec.get("provider") or af._DEFAULT_PROVIDER
    if not (home.CLONE / "lean_coder.py").is_file():
        return Trace([], "", "no gym clone (.gym/lean-coder): call gym.home.clone()")
    ci = home.clone_head() or {}
    if not ci.get("at_pin") or ci.get("dirty"):
        return Trace([], "", f"gym clone not at clean pin: {ci}")
    if prov not in af.FREE_PROVIDERS and not af._paid_runs_armed():
        return Trace([], "", f"paid provider {prov} not armed (EVAL_ALLOW_PAID)")
    settings = json.loads(os.environ.get("EVAL_PROVIDER_SETTINGS") or "{}")
    if prov in ("ovh", "kilo"):
        settings.setdefault(f"{prov}_pin", "on")
    top = {}
    if prov == "ollama":                 # native ollama reads TOP-LEVEL host/think
        import lean_coder as lc
        top = {"host": lc._norm_host(spec.get("host")), "think": bool(spec.get("think")),
               "num_ctx": 32768, "keep_alive": -1}
    elif prov in home.PLAN_PROVIDERS:
        settings.setdefault("thinking", "adaptive" if spec.get("think") else "off")
    hd = home.run_home(Path(ws).name + f"-{os.getpid()}")
    home.write_config(prov, spec["model"], settings, top=top, dst=hd / ".config" / "leancoder")
    wd = Path(ws).parent / (Path(ws).name + ".worker")
    wd.mkdir(exist_ok=True)
    brief, res, ckpt = wd / "brief.txt", wd / "brief.txt.result", wd / "brief.txt.checkpoint"
    for f in (res, ckpt):
        f.unlink(missing_ok=True)
    brief.write_text(f"===GRANT===\nleash: rwe\ncwd: {ws}\nmax_iterations: 8\n"
                     f"checkpoint: on\nprovider: {prov}\nmodel: {spec['model']}\n===GRANT===\n"
                     f"===BRIEF===\n{WORKER_TASK}\n===BRIEF===\n")
    err, rc, out = None, None, ""
    try:
        p = subprocess.run([sys.executable, str(home.CLONE / "lean_coder.py"), "--agent-run",
                            "--brief-file", str(brief), "--result-file", str(res)],
                           cwd=str(ws), env=home.worker_env(hd, ws=Path(ws)),
                           capture_output=True, text=True, timeout=900)
        rc, out = p.returncode, (p.stdout + p.stderr)[-2000:]
    except subprocess.TimeoutExpired:
        rc, out, err = None, "", "worker timeout"
    msgs = []
    try:
        msgs = [m for m in json.loads(ckpt.read_text())["messages"] if m.get("role") != "system"]
    except (OSError, ValueError, KeyError):
        pass
    final = res.read_text() if res.is_file() else ""
    if final and not (msgs and msgs[-1].get("role") == "assistant"
                      and not msgs[-1].get("tool_calls")):
        msgs.append({"role": "assistant", "content": final})
    wmeta = {}
    try:
        wmeta = json.loads(ckpt.read_text()).get("meta") or {}
    except (OSError, ValueError):
        pass
    shutil.rmtree(wd, ignore_errors=True)
    shutil.rmtree(hd, ignore_errors=True)
    if wmeta and (wmeta.get("model"), wmeta.get("provider")) != (spec["model"], prov):
        err = err or f"worker ran {wmeta.get('provider')}/{wmeta.get('model')}, not {prov}/{spec['model']}"
    t = Trace(msgs, final, err, {"worker": True, "rc": rc, "stdout": out,
                                 "thinking": wmeta.get("thinking")})
    # kept context: the parent's view (brief out, result back), not the worker's turns
    return t


_BUNDLE_NUDGE = "in the SAME message as your"       # lean-coder's corrective turn (real path)
_RESCUE_NUDGE = "You stopped without writing your"   # lean-coder's missing-RESULT rescue


def _users(t):
    return [str(m.get("content") or "") for m in t.messages if m.get("role") == "user"]


def _no_bundle(t, ws):
    """Real worker: lean-coder discards a bundled (text-parsed) call and sends its corrective
    nudge - that user turn is in the checkpoint (written before the next model call).
    In-process/fixture: an assistant message with tool_calls AND a RESULT block."""
    bad = [m for m in t.messages if m.get("role") == "assistant" and m.get("tool_calls")
           and RESULT_MARK in (m.get("content") or "")]
    nudged = any(_BUNDLE_NUDGE in u for u in _users(t))
    return V(not bad and not nudged, "tool call bundled with a RESULT block")


def _emitted(t, ws):
    """Clean RESULT: worker rc 0, unprompted (no rescue nudge), body not ERROR/INCOMPLETE."""
    if t.meta.get("worker"):
        body = t.final_text.replace(RESULT_MARK, "").strip()
        ok = (t.meta.get("rc") == 0 and t.final_text.count(RESULT_MARK) >= 2
              and not re.match(r"(ERROR|INCOMPLETE):", body)
              and not any(_RESCUE_NUDGE in u for u in _users(t)))
        return V(ok, f"rc={t.meta.get('rc')} result={body[:60]!r}")
    ok = any(m.get("role") == "assistant" and not m.get("tool_calls")
             and RESULT_MARK in (m.get("content") or "") for m in t.messages)
    return V(ok, "no tool-free RESULT block")


def _worker_edit(t, ws):
    return V('print("hi there")' in _read(ws, "hello.py"), "hello.py not edited")


# ------------------------------------------------------------------ assemble
C = Checkpoint
STAGES = [
    Stage("prelim_think", P_THINK, [C("think_switch", _think_ok)], n=1,
          run=_run_think, scored=False, keep="reference", reference=[]),
    Stage("answer", P_ANSWER, [C("answered", _answered),
                               C("no_tool", _no_tool)],
          run=_capped(1), reference=_ref(P_ANSWER, "enumerate yields (index, value) pairs: "
                                         "the index first, then the item.")),
    Stage("git_log", P_GIT, [C("no_fake_tool", _no_fake_tool, gating=True),
                             C("git_log", _git_log)],
          run=_run_git, keep="reference",   # later workspaces have no .git
          reference=_ref(P_GIT, "I ran `git log --oneline -5` and showed you the history.")),
    Stage("vague_bug", P_VAGUE, [C("investigated_first", _investigated)],
          run=_capped(1), end_files=_set({"calc.py": CALC.replace("xs[1:]", "xs")}),
          keep="reference",                 # capped run never fixes; reference matches end_files
          reference=_ref(P_VAGUE, "I read calc.py: total() skipped the first element "
                         "(xs[1:]). I changed it to loop over xs; it now prints 6.")),
    Stage("plan", P_PLAN, [C("plan_first", _plan_first)], run=_capped(1),
          reference=_ref(P_PLAN, "Plan set: 1) read greet.py 2) add farewell() 3) run it. "
                         "Let's hold there; next request is different.")),
    Stage("edit_diff", P_DIFF, [C("applied", _diff_applied, tier="standard"),
                                C("used_apply_diff", _used("apply_diff"), cert=True),
                                C("args_valid", _args_valid)],   # diagnostic, not gated
          end_files=_set({"greet.py": GREET_HW}),
          reference=_ref(P_DIFF, 'Done: greet.py now prints "hello world" (apply_diff).')),
    Stage("edit_indent", P_INDENT, [C("reindented", _reindented, tier="standard")],
          end_files=_set({"wide.py": WIDE_OK}),
          reference=_ref(P_INDENT, "Done: scale() now returns a * b, 8-space indent kept.")),
    Stage("edit_lines", P_LINES, [C("applied", _lines_applied, tier="high"),
                                  C("used_replace_lines", _used("replace_lines"), cert=True)],
          end_files=_set({"nums.py": NUMS_OK}), fs_capable=True,
          reference=_ref(P_LINES, "Done: nums.py line 2 is now `b = 20`.")),
    Stage("new_file", P_NEW, [C("created", _created, tier="standard"),
                              C("used_write_file", _used("write_file"), cert=True)],
          end_files=_set({"hello.py": 'print("hi")\n'}), fs_capable=True,
          reference=_ref(P_NEW, "Done: created hello.py with print(\"hi\").")),
    Stage("recover_diff", P_RECOVER, [C("recovered", _recovered, tier="high"),
                                      C("no_blind_loop", _no_blind_loop, cert=True)],
          run=_run_drift, end_files=_set({"greet.py": GREET_GW}),
          reference=_ref(P_RECOVER, "My first apply_diff failed: the file had changed to "
                         '"hello, world". I re-read it and fixed the SEARCH; greet.py now '
                         'prints "goodbye world".')),
    Stage("recover_lines", P_SHIFT, [C("no_stale_clobber", _shift_ok, tier="high")],
          end_files=_set({"cfg.py": CFG_OK}), fs_capable=True,
          reference=_ref(P_SHIFT, "Deleted DEBUG, re-read (lines shifted), set TIMEOUT = 60.")),
    Stage("run_report", P_RUN, [C("edited", _multiplied, tier="standard"),
                                C("ran_it", _ran, cert=True),
                                C("described", _described, cert=True)],
          end_files=_set({"greet.py": GREET_MUL}), fs_capable=True,
          reference=_ref(P_RUN, "Changed add() to return a * b and ran `python greet.py`: it "
                         "runs and prints goodbye world.")),
    Stage("server", P_SERVER, [C("backgrounded", _backgrounded, tier="standard")],
          run=_run_server,
          reference=_ref(P_SERVER.replace("{port}", "8000"), "Started it in the background with run_command "
                         "background=true; it's listening.")),
    Stage("compact_elect", P_ELECT, [C("elected", _elected, tier="standard")],
          run=_run_elect, keep="reference",  # request_compact isn't offered later
          reference=_ref(P_ELECT, "Compacted. Ready for the next task.")),
    Stage("compact_manual", P_MANUAL, [C("handoff", _handoff, tier="standard")],
          run=_run_manual, only_if=_elect_failed),
    Stage("worker", P_WORKER, [C("no_bundling", _no_bundle),
                               C("emitted_result", _emitted),
                               C("edit_applied", _worker_edit)],
          run=_run_worker, keep="reference",
          reference=_ref("Dispatch a worker: " + WORKER_TASK, "Worker done: hello.py now "
                         'prints "hi there".'),
          end_files=_set({"hello.py": 'print("hi there")\n'})),
]


# ------------------------------------------------------------------ fixtures (selfcheck)
def _tc(name, **args):
    return {"role": "assistant", "content": "", "tool_calls": [
        {"id": f"c_{name}", "function": {"name": name, "arguments": args}}]}


def T(*calls, text="", **meta):
    return Trace(list(calls), text, None, meta)


def _fx(stage, trace, expect, files=None):
    return {"stage": stage, "trace": trace, "expect": expect, "files": files or {}}


FIXTURES = [
    _fx("prelim_think", T(think_lens={"on": 40, "off": 900}), {"think_switch": False}),
    _fx("prelim_think", T(think_lens={"on": 40, "off": 60}), {"think_switch": True}),
    _fx("answer", T(text="Each item is (index, value)."), {"answered": True, "no_tool": True}),
    _fx("answer", T(_tc("read_file", path="x"), text="the value then the index"),
        {"answered": False, "no_tool": False}),
    _fx("answer", T(text="You get the value, then the index."), {"answered": False}),
    _fx("git_log", T(_tc("run_command", cmd="ls"), _tc("run_command", cmd="git log"),
                     known_tools=[]), {"no_fake_tool": False, "git_log": False}),
    _fx("vague_bug", T(text="Which bug do you mean?"), {"investigated_first": True}),
    _fx("git_log", T(_tc("run_command", cmd="git log --oneline"), known_tools=["run_command"]),
        {"no_fake_tool": True, "git_log": True}),
    _fx("git_log", T(_tc("git_log"), known_tools=["run_command"]),
        {"no_fake_tool": False, "git_log": False}),
    _fx("vague_bug", T(_tc("read_file", path="calc.py")), {"investigated_first": True}),
    _fx("vague_bug", T(_tc("apply_diff", path="calc.py", diff="x")), {"investigated_first": False}),
    _fx("plan", T(_tc("update_plan", plan="GOAL x\n- [ ] read\n- [ ] add")), {"plan_first": True}),
    _fx("plan", T(_tc("read_file", path="greet.py")), {"plan_first": False}),
    _fx("edit_diff", T(_tc("apply_diff", path="greet.py", diff="d")),
        {"applied": True, "used_apply_diff": True, "args_valid": True}, {"greet.py": GREET_HW}),
    _fx("edit_diff", T({"role": "assistant", "tool_calls": [
        {"function": {"name": "write_file", "arguments": "{bad"}}]}),
        {"applied": False, "used_apply_diff": False, "args_valid": False}, {"greet.py": GREET}),
    _fx("edit_indent", T(), {"reindented": True}, {"wide.py": WIDE_OK}),
    _fx("edit_indent", T(), {"reindented": False}, {"wide.py": WIDE_OK.replace("        return", "    return")}),
    _fx("edit_lines", T(_tc("replace_lines", path="nums.py")),
        {"applied": True, "used_replace_lines": True}, {"nums.py": NUMS_OK}),
    _fx("edit_lines", T(_tc("apply_diff")), {"applied": False, "used_replace_lines": False},
        {"nums.py": NUMS_OK + "b = 20\n"}),
    _fx("new_file", T(_tc("write_file")), {"created": True, "used_write_file": True},
        {"hello.py": 'print("hi")\n'}),
    _fx("new_file", T(_tc("run_command", cmd="echo")), {"created": False, "used_write_file": False},
        {"hello.py": 'print("hi")\nprint(1)\n'}),
    _fx("recover_diff", T(_tc("write_file", path="greet.py"), injected=False),
        {"recovered": False}, {"greet.py": GREET_GW}),
    _fx("recover_diff", T(_tc("apply_diff", diff="a"), _tc("read_file", path="greet.py"),
                          _tc("apply_diff", diff="b"), injected=True),
        {"recovered": True, "no_blind_loop": True}, {"greet.py": GREET_GW}),
    _fx("recover_diff", T(_tc("apply_diff", diff="a"), _tc("apply_diff", diff="a")),
        {"recovered": False, "no_blind_loop": False}, {"greet.py": _DRIFT}),
    _fx("recover_lines", T(_tc("replace_lines"), _tc("replace_lines")),
        {"no_stale_clobber": True}, {"cfg.py": CFG_OK}),
    _fx("recover_lines", T(_tc("replace_lines"), _tc("read_file", path="cfg.py")),
        {"no_stale_clobber": True}, {"cfg.py": '# config\nPORT = 8080\nHOST = "local"\nTIMEOUT = 30\n'}),
    _fx("recover_lines", T(_tc("replace_lines"), _tc("replace_lines")),
        {"no_stale_clobber": False}, {"cfg.py": '# config\nPORT = 8080\nHOST = "local"\nTIMEOUT = 30\n'}),
    _fx("run_report", T(_tc("run_command", cmd="python greet.py"), text="Changed it to a * b."),
        {"edited": True, "ran_it": True, "described": True}, {"greet.py": GREET_MUL}),
    _fx("run_report", T(_tc("run_command", cmd="ls"), text="All good."),
        {"edited": False, "ran_it": False, "described": False}, {"greet.py": GREET_GW}),
    _fx("server", T(server={"pass": True}), {"backgrounded": True}),
    _fx("server", T(server={"pass": False, "listening": False}), {"backgrounded": False}),
    _fx("compact_elect", T(_tc("note"), _tc("request_compact")), {"elected": True}),
    _fx("compact_elect", T(text="Shall I compact?"), {"elected": False}),
    _fx("compact_manual", T(corpus="Edited greet.py; next: worker stage."), {"handoff": True}),
    _fx("compact_manual", T(corpus="ok"), {"handoff": False}),
    _fx("worker", T(_tc("apply_diff"), {"role": "assistant", "content":
                                        f"{RESULT_MARK}\nchanged\n{RESULT_MARK}"}),
        {"no_bundling": True, "emitted_result": True, "edit_applied": True},
        {"hello.py": 'print("hi there")\n'}),
    _fx("worker", T({"role": "user", "content": "task"}, text=f"{RESULT_MARK}\nok\n{RESULT_MARK}",
                    worker=True, rc=0), {"no_bundling": True, "emitted_result": True}),
    _fx("worker", T({"role": "user", "content": "Your last message put a tool call (x) in the "
                     "SAME message as your ===RESULT=== block"},
                    {"role": "user", "content": "You stopped without writing your block"},
                    text=f"{RESULT_MARK}\nINCOMPLETE: cap\n{RESULT_MARK}", worker=True, rc=2),
        {"no_bundling": False, "emitted_result": False}),
    _fx("worker", T({"role": "assistant", "content": f"{RESULT_MARK}\nx\n{RESULT_MARK}",
                     "tool_calls": [{"function": {"name": "apply_diff", "arguments": {}}}]}),
        {"no_bundling": False, "emitted_result": False, "edit_applied": False},
        {"hello.py": 'print("hi")\n'}),
]

SUITE = Suite(id="core", version="1.0", files=dict(FILES), stages=STAGES,
            tags=frozenset({"core", "lean"}), serial=True, fixtures=FIXTURES)

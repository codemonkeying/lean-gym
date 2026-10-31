"""Headless leancoder Agent fixture for the eval harness.

The harness drives the REAL `lean_coder.Agent` (not a hand-rolled loop), so probe
behaviour can never drift from production. This module is the ONLY place that knows
how to stand a headless Agent up; it mirrors leancoder's own `--agent-run` worker
setup (run_agent_brief) step for step - it invents no config of its own.

    from agent_fixture import build_agent, Scenario
    with Scenario({"greet.py": "print('hi')\n"}) as sc:
        agent = build_agent("http://localhost:11434", "MODEL", cwd=sc.dir)
        agent.run_turn("In greet.py change hi to bye. Use apply_diff.")
        assert "bye" in sc.read("greet.py")

Provider policy (operator decision #2): providers are leancoder's OWN system, ollama by
default. No `/api/chat` calls, no OAuth headers, no frontier bolt-on here. A model on
another provider is testable iff the operator has enabled that provider locally the
normal leancoder way - the fixture just activates whatever leancoder would.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import uuid
from pathlib import Path

# Import the real leancoder core. leangym is a SEPARATE repo from lean-coder and
# never vendors/commits core - it drives whatever core is on disk at runtime (the
# provenance receipt stamps leancoder_version+sha so a cert records which core ran).
# Resolution precedence:
#   1. $LEANCODER_REPO                    (explicit override)
#   2. leangym.toml -> leancoder_repo     (repo-local config, next to this file)
#   3. <leangym>/local/lean-coder-bench   (pinned bench copy, gitignored)
#   4. ~/lean-coder                       (the standard checkout)
def _resolve_leancoder_repo():
    env = os.environ.get("LEANCODER_REPO")
    if env:
        return Path(env).expanduser().resolve()
    cfg = Path(__file__).resolve().parent / "leangym.toml"
    if cfg.is_file():
        try:
            import tomllib
            val = tomllib.loads(cfg.read_text()).get("leancoder_repo")
            if val:
                return Path(val).expanduser().resolve()
        except Exception:
            pass
    bench = Path(__file__).resolve().parent / "local" / "lean-coder-bench"   # op 13b: pinned copy in-repo
    if (bench / "lean_coder.py").is_file():
        return bench
    return (Path.home() / "lean-coder").resolve()


REPO = _resolve_leancoder_repo()
if not (REPO / "lean_coder.py").is_file():
    raise SystemExit(
        f"leangym: cannot find lean_coder.py under {REPO}. Set $LEANCODER_REPO or "
        f"leancoder_repo in leangym.toml to your lean-coder checkout.")
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
if str(REPO / "lean-tools") not in sys.path:
    sys.path.insert(0, str(REPO / "lean-tools"))

import lean_coder as lc  # noqa: E402

# ---------------------------------------------------------------------------
# CORE COMPAT PREFLIGHT
# ---------------------------------------------------------------------------
# leangym drives WHATEVER lean_coder is on disk - a separate, fast-moving repo we
# don't control the naming of. When core renames/removes something the harness binds
# to (it has: compact_hard->compact_at, compact->compact), the failure would surface
# DEEP in a probe as a silent mis-score - an AttributeError three turns in, scored as
# a model failure. That is the worst outcome for a cert whose whole value is trust.
#
# So we assert the ENTIRE core surface the harness depends on, ONCE, at import time -
# before any probe runs - and fail LOUD with the exact missing names. The operator
# then knows their core drifted, fixes the binding (or checks out a compatible core),
# and re-runs. We do NOT tell core what to expose; we adapt to it and refuse to run
# blind if it moved. This list IS the executable inventory of our core coupling.
LEANCODER_MIN_VERSION = "0.9.4"   # coarse floor; the symbol probe below is the real gate

# module-level symbols the harness imports/calls, grouped by the module they live in.
_REQUIRED_CORE = {
    "lean_coder": [
        "Agent", "Config", "SYSTEM_PROMPT", "RESULT_MARK",
        "_norm_host", "_norm_leash",
        "_maybe_autostart_provider", "_register_dir_providers",
    ],
    "providers.ollama": [
        "is_small_model", "_param_b_for", "_SMALL_TOOL_ADDENDUM",
    ],
}
# OAuth / subscription-plan providers are site-specific and never shipped, so their
# names are not hardcoded. If you drive one, list it in LEANGYM_PLAN_PROVIDERS and the
# fixture will assert the symbol it depends on (_provider_list_models) on that module.
for _p in (s.strip() for s in os.environ.get("LEANGYM_PLAN_PROVIDERS", "").split(",") if s.strip()):
    _REQUIRED_CORE[f"providers.{_p}"] = ["_provider_list_models"]
# Agent methods the probe layer calls on a live agent (checked on the class).
_REQUIRED_AGENT_METHODS = ["run_turn", "compact", "auto_compact"]
# Config methods the fixture calls.
_REQUIRED_CONFIG_METHODS = ["set_active_model", "active_model"]
# builtins.py (lean-tools) - the model-facing tool schemas, loaded by path in eval.py.
_REQUIRED_BUILTINS = ["TOOL_SCHEMAS"]
# Model-facing TOOL NAMES the probe layer scores by exact string. A symbol check won't
# catch these - they're runtime schema entries, not python attributes. A core tool
# rename would otherwise silently mis-score every probe bound to the old name, so we
# assert the tool names the probes bind to actually EXIST in the live schema surface.
# Each entry is a tuple of accepted names (any one present = ok).
# STATIC file/exec tools - live in lean-tools/builtins.py TOOL_SCHEMAS.
_REQUIRED_TOOL_NAMES = [
    ("apply_diff",),                            # ED* edit probes
    ("run_command",),                           # RUN*/DX* exec probes
    ("background",),                            # RUN1 backgrounding (dedicated tool; run_command is foreground-only)
    ("read_file",),                             # read-tool probes
]
# The elective soft-zone compact tool is an agent-INTERNAL meta-tool (surfaced by core
# at runtime, not a static schema entry) - it lives in lean_coder._META_TOOLS. Assert it
# exists so a future rename fails LOUD here, not as a silent mis-score.
_REQUIRED_META_TOOLS = ("request_compact",)



def _preflight_core(strict_version=True):
    """Assert the core surface the harness binds to EXISTS. Returns (ok, problems).
    Raises SystemExit with a fixable message when core has drifted. Set
    LEANGYM_SKIP_PREFLIGHT=1 to bypass (e.g. deliberately testing against a fork)."""
    import importlib
    problems = []

    for mod_name, names in _REQUIRED_CORE.items():
        try:
            mod = importlib.import_module(mod_name)
        except Exception as ex:                                   # noqa
            problems.append(f"cannot import {mod_name}: {type(ex).__name__}: {ex}")
            continue
        for n in names:
            if not hasattr(mod, n):
                problems.append(f"{mod_name}.{n} missing")

    for n in _REQUIRED_AGENT_METHODS:
        if not callable(getattr(getattr(lc, "Agent", None), n, None)):
            problems.append(f"lean_coder.Agent.{n} missing/not callable")
    for n in _REQUIRED_CONFIG_METHODS:
        if not callable(getattr(getattr(lc, "Config", None), n, None)):
            problems.append(f"lean_coder.Config.{n} missing/not callable")

    # Elective soft-zone compact meta-tool (agent-internal; not in TOOL_SCHEMAS). Core
    # exposes the name set as lean_coder._META_TOOLS - assert our alias is in there.
    # C12: a MISSING _META_TOOLS is itself a contract break (was silently skipped).
    _meta = getattr(lc, "_META_TOOLS", None)
    if _meta is None:
        problems.append("lean_coder._META_TOOLS missing (CMP_ELECT needs "
                        f"{' / '.join(_REQUIRED_META_TOOLS)})")
    else:
        if not any(a in _meta for a in _REQUIRED_META_TOOLS):
            problems.append(
                f"no meta-tool named {' / '.join(_REQUIRED_META_TOOLS)} in "
                f"lean_coder._META_TOOLS (CMP_ELECT binds to it - core may have renamed it)")

    # builtins.py sits under lean-tools/ and shadows stdlib 'builtins' - load by path.
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "_lc_builtins_preflight", str(REPO / "lean-tools" / "builtins.py"))
        btmod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(btmod)
        for n in _REQUIRED_BUILTINS:
            if not hasattr(btmod, n):
                problems.append(f"lean-tools/builtins.py:{n} missing")
        # Runtime TOOL-NAME check: the probes score by exact tool name, which a symbol
        # probe can't see. Pull the names out of the live TOOL_SCHEMAS and assert each
        # required tool is present under at least one accepted alias.
        schemas = getattr(btmod, "TOOL_SCHEMAS", None)
        if schemas is not None:
            live = set()
            for s in schemas:
                # tolerate both flat {"name":...} and OpenAI {"function":{"name":...}}
                nm = (s.get("name") if isinstance(s, dict) else None) \
                     or (isinstance(s, dict) and (s.get("function") or {}).get("name"))
                if nm:
                    live.add(nm)
            for aliases in _REQUIRED_TOOL_NAMES:
                if not any(a in live for a in aliases):
                    problems.append(
                        f"no tool named {' / '.join(aliases)} in TOOL_SCHEMAS "
                        f"(probes bind to it by name - core may have renamed it)")
    except Exception as ex:                                        # noqa
        problems.append(f"cannot load lean-tools/builtins.py: {type(ex).__name__}: {ex}")

    # Coarse version floor: catches "way too old"; the symbol probe above catches the
    # in-line renames a version bump won't reveal. A missing/odd version is a WARN, not
    # a hard fail - core may not expose one, and the symbol probe is the real gate.
    ver = getattr(lc, "__version__", None) or getattr(lc, "VERSION", None)
    if strict_version and ver:
        try:
            import re as _re
            # D5: robust to rc/dev suffixes ("1.4.0rc2", "1.4-dev") - leading digits only.
            def _v(s):
                s = str(s).strip().lstrip("vV")
                t = tuple(int(m.group(0)) for m in
                          (_re.match(r"\d+", x) for x in s.split(".")[:3]) if m)
                return (t + (0, 0, 0))[:3]          # pad: "1.4-dev" == (1,4,0), "1.0" ok
            if _v(ver) < _v(LEANCODER_MIN_VERSION):
                problems.append(
                    f"lean_coder version {ver} < required floor {LEANCODER_MIN_VERSION}")
        except Exception:                                         # noqa
            pass

    if problems:
        raise SystemExit(
            "leangym: core compatibility preflight FAILED - the lean_coder at\n"
            f"  {REPO}\n"
            "has drifted from what this harness binds to. A probe would otherwise fail\n"
            "DEEP as a silent mis-score. Fix the binding or check out a compatible core,\n"
            "then re-run. Missing/incompatible:\n"
            + "".join(f"  - {p}\n" for p in problems)
            + "(bypass with LEANGYM_SKIP_PREFLIGHT=1 if you know what you're doing.)")
    return True, []


if not os.environ.get("LEANGYM_SKIP_PREFLIGHT"):
    _preflight_core()

SCENARIO_ROOT = Path(__file__).resolve().parent / "runs" / "scenarios"


def sweep_stale_scenarios(older_than_secs: int = 300) -> int:
    """Remove leaked scenario workspaces. Scenario.__exit__ cleans up on a NORMAL exit,
    but a SIGKILL'd/wedged drainer skips __exit__ and leaks its runs/scenarios/<uuid>/
    tree. Those accumulate (157 seen) and ACTIVELY corrupt runs: a probe seeds a clean
    greet.py sandbox but a model that `find`s from an absolute path hits dozens of stale
    decoy greet.py from prior runs (an old one may already contain the target string) and
    edits/reads the wrong file -> nondeterministic ED1/ED2/ED3 flakes. Called once at
    drain startup. Only sweeps dirs older than `older_than_secs` so a CONCURRENT drainer's
    live workspace is never nuked. Best-effort, never raises."""
    import time as _t
    removed = 0
    try:
        if not SCENARIO_ROOT.exists():
            return 0
        cutoff = _t.time() - older_than_secs
        for d in SCENARIO_ROOT.iterdir():
            try:
                if d.is_dir() and d.stat().st_mtime < cutoff:
                    shutil.rmtree(d, ignore_errors=True)
                    removed += 1
            except OSError:
                continue
    except Exception:                                              # noqa
        pass
    return removed


class Scenario:
    """A dedicated, auto-cleaned temp workspace for one probe run (operator decision #1).

    Seeds `files` (name -> text) into runs/scenarios/<uuid>/ and removes the tree on
    exit. Fresh per run so runs are independent (decision #3)."""

    def __init__(self, files: dict[str, str] | None = None, *, keep: bool = False):
        self.files = files or {}
        self.keep = keep
        SCENARIO_ROOT.mkdir(parents=True, exist_ok=True)
        self.dir = Path(tempfile.mkdtemp(prefix="", dir=str(SCENARIO_ROOT)))
        for name, text in self.files.items():
            p = self.dir / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)

    def read(self, name: str) -> str:
        p = self.dir / name
        return p.read_text() if p.exists() else ""

    def exists(self, name: str) -> bool:
        return (self.dir / name).exists()

    def cleanup(self):
        if not self.keep:
            shutil.rmtree(self.dir, ignore_errors=True)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.cleanup()
        return False


# Providers split into FREE (local, self-hosted - cost nothing to call) and METERED
# (hosted APIs / OAuth plans - draw on the operator's quota). ollama was the only free
# one originally; the local OpenAI-compatible backends (mlx on Apple Silicon, llama.cpp's
# llama-server, vllm, LM Studio) are ALSO free + local and must not be gated. A public
# harness must not let a fresh clone torch an account by accident, so a METERED provider
# is refused unless the operator EXPLICITLY arms paid runs via this env flag. Env (not a
# config file) on purpose: per-shell, ephemeral, can't be committed by mistake. See
# README "Providers & account safety".
PAID_ARM_ENV = "EVAL_ALLOW_PAID"

# Local, self-hosted backends that cost nothing to call - never gated behind EVAL_ALLOW_PAID.
# Extra names can be added via EVAL_FREE_PROVIDERS (comma-separated) without a code change.
FREE_PROVIDERS = {"ollama", "mlx", "llamacpp", "vllm", "lmstudio"} | {
    p.strip() for p in os.environ.get("EVAL_FREE_PROVIDERS", "").split(",") if p.strip()
}

# Process-wide default provider, so the 21 probes can keep calling build_agent(host,
# model, cwd, think=...) unchanged while the CLI/test selects the provider ONCE per
# run. build_agent(provider=...) still wins if passed explicitly. Serial per drainer,
# so a module global is safe. Env EVAL_PROVIDER seeds it (a spawned drainer inherits).
_DEFAULT_PROVIDER = os.environ.get("EVAL_PROVIDER", "ollama") or "ollama"


def set_default_provider(name: str) -> None:
    global _DEFAULT_PROVIDER
    _DEFAULT_PROVIDER = name or "ollama"


def _paid_runs_armed() -> bool:
    return os.environ.get(PAID_ARM_ENV, "").strip() in ("1", "true", "yes", "on")


# FM1 (blowout guard): multi-turn probes leave cfg.max_iterations at the core default
# of 0 = UNLIMITED, so a spiraling/recency-trapped model keeps emitting tool calls until
# it hits the 600s void wall (55x identical `cat greet.py` observed). A real edit/compact
# probe finishes in <=3-4 rounds; nothing legit needs more than this. build_agent applies
# this cap by DEFAULT; single-move probes still tighten to max_iterations=1 explicitly
# AFTER build_agent, and any probe that genuinely needs more can override. Env-tunable.
EVAL_MAX_ITERATIONS = int(os.environ.get("EVAL_MAX_ITERATIONS", "8"))


_CALIB_WORKLOAD_TOKENS = 9000     # mirrors evalcli._calibrate_timeouts
_CALIB_MAX_AGE_S = 7 * 86400      # ignore hostbench records older than 7 days (box/model drift)


_DEFAULT_PORTS = {"ollama": 11434}   # llamacpp: no default port known here -> left as-is


def _norm_host(h, provider: str = "ollama") -> str:
    """'http://Box:11434/' -> 'box:11434' (strip scheme + trailing slash, lowercase).
    A bare host gets the provider's default port (ollama :11434, matching core
    lean_coder._norm_host) so 'box' and 'box:11434' compare equal."""
    s = str(h or "").strip().lower()
    for pre in ("http://", "https://"):
        if s.startswith(pre):
            s = s[len(pre):]
    s = s.split("/", 1)[0]
    port = _DEFAULT_PORTS.get(str(provider or "ollama").lower())
    if s and port and not s.endswith("]") and ":" not in s.rsplit("]", 1)[-1]:
        s = f"{s}:{port}"
    return s


def _cached_calibration(host: str, model: str, provider: str = None):
    """C14: (connect, ttft, idle) strings for no-think from the newest ok hostbench
    record in <results>/host_metrics.jsonl (written by `evalcli bench --save`) that
    matches (host:port normalised, model, provider/backend; default 'ollama') and is
    at most _CALIB_MAX_AGE_S old. Connect/idle use the same scaling as
    evalcli._calibrate_timeouts, but the no-think ttft FLOOR here is 300s (direct-run
    C14 floor) vs 120s in the drainer calibrator - so results are NOT identical.
    None if no usable record. Never raises."""
    try:
        raw = os.environ.get("EVAL_RESULTS")
        rdir = (Path(raw).expanduser() if raw
                else Path(__file__).resolve().parent / "results")
        p = rdir / "host_metrics.jsonl"
        if not p.is_file():
            return None
        want_host = _norm_host(host, provider or "ollama")
        want_prov = str(provider or "ollama").lower()
        now = time.time()
        best = None
        for line in p.read_text().splitlines():
            try:
                d = json.loads(line)
            except Exception:
                continue
            if not (d.get("ok") and d.get("model") == model and d.get("prefill_tps")):
                continue
            if _norm_host(d.get("host"), want_prov) != want_host:
                continue
            rprov = str(d.get("provider") or d.get("backend") or "ollama").lower()
            if rprov != want_prov:
                continue
            try:
                ts = float(d.get("ts"))
            except Exception:
                continue                           # undated -> can't prove fresh
            if now - ts > _CALIB_MAX_AGE_S:
                continue
            best = d                               # last one wins (newest append)
        if not best:
            return None
        pf = float(best["prefill_tps"]) or 1.0
        dec = float(best.get("decode_tps") or 0.0)
        connect = max(10.0, round(_CALIB_WORKLOAD_TOKENS / pf * 3))
        ttft = max(300.0, round(connect + 200.0))
        idle = 30.0 if dec >= 20 else 60.0 if dec >= 8 else 120.0
        return str(connect), str(ttft), str(idle)
    except Exception:
        return None


def _apply_sampling_env(cfg):
    """EVAL_SAMPLING / EVAL_TEMPERATURE -> cfg. Never raises."""
    # Allowlist ONLY temperature/top_k/top_p/min_p, coerced; anything else ignored.
    if os.environ.get("EVAL_SAMPLING", "").strip():
        try:
            _smp = json.loads(os.environ["EVAL_SAMPLING"])
        except Exception:                                          # noqa
            _smp = {}
        if not isinstance(_smp, dict):
            _smp = {}
        for _k, _typ in (("temperature", float), ("top_k", int),
                         ("top_p", float), ("min_p", float)):
            if _k in _smp and hasattr(cfg, _k):
                try:
                    setattr(cfg, _k, _typ(float(_smp[_k])))
                except Exception:                                  # noqa
                    pass
    if os.environ.get("EVAL_TEMPERATURE", "").strip():
        try:
            cfg.temperature = float(os.environ["EVAL_TEMPERATURE"].strip())
        except Exception:                                          # noqa
            pass


def build_agent(host: str, model: str, cwd: Path, *,
                leash: str = "rwe", think: bool = False,
                num_ctx: int = 32768, provider: str = None,
                max_iterations: int = EVAL_MAX_ITERATIONS) -> "lc.Agent":
    """Stand up a headless Agent on `cwd`, activated on `provider` with `model`.
    Mirrors run_agent_brief's headless setup: approval=auto (nobody to confirm),
    composer/autosave off, real system prompt + real tools. Raises if no
    provider/model could be started (never returns a dead Agent).

    PROVIDER (public-harness safety, 3 gates):
      * Gate 1 - DEFAULT ollama-only, HARNESS-ONLY. We build a FRESH lc.Config() and
        set providers_enabled EXPLICITLY here; we NEVER read the operator's leancoder
        config file. So a paid provider they've enabled for daily use is invisible to
        the eval suite - doing nothing is free, by construction.
      * Gate 2 - a non-ollama (metered) provider is REFUSED unless EVAL_ALLOW_PAID is
        set. Naming it isn't enough; the operator must consciously arm the session.
      * Gate 3 - fail LOUD, never silent-fallback to ollama (that would hide the
        mistake AND certify the wrong model).
    Any provider plugin in leancoder's providers/ dir works - the harness reuses
    leancoder's OWN provider machinery (register -> autostart -> activate); it adds no
    transport/OAuth of its own. `host`/num_ctx/keep_alive apply to ollama only; a
    hosted provider sets its own ctx window in activate_provider.

    think defaults to FALSE (explicit off) - the pinned cert condition. A None would
    let qwen3-family self-default to thinking ON and silently change the test."""
    if provider is None:
        provider = _DEFAULT_PROVIDER           # process-wide default (CLI/test sets it)
    prov_name = provider or "ollama"
    is_ollama = prov_name == "ollama"
    is_free   = prov_name in FREE_PROVIDERS    # local, self-hosted: ollama/mlx/llamacpp/vllm/lmstudio
    if not is_free and not _paid_runs_armed():
        raise RuntimeError(
            f"provider {provider!r} is METERED (draws on your API/plan quota); "
            f"refusing to activate. Set {PAID_ARM_ENV}=1 to allow paid runs "
            f"(this WILL consume quota). Local providers ({', '.join(sorted(FREE_PROVIDERS))}) "
            f"are free + need no arming.")

    cfg = lc.Config()
    # Headless, exactly as the worker role sets it.
    cfg.approval = "auto"
    cfg.composer = False
    cfg.autosave = False
    # Match run_agent_brief (lc --agent-run, ~9737): auto-compaction OFF. A probe drives
    # compaction EXPLICITLY (agent.auto_compact(...)) when it wants to measure it; leaving
    # this on lets a large seeded history trip _maybe_compact() mid-run_turn and fire an
    # UNEXPECTED compaction inside an executor/compact probe - an intermittent confound.
    cfg.auto_compact = False
    cfg.leash = lc._norm_leash(leash) or cfg.leash
    # FM1: cap agentic rounds (0/None => unlimited => a spiral runs to the void wall).
    cfg.max_iterations = max_iterations
    cfg.cwd = Path(cwd).expanduser().resolve()
    if not cfg.cwd.is_dir():
        raise ValueError(f"cwd is not a directory: {cfg.cwd}")
    # FM4 (git isolation): scenario workspaces live UNDER runs/scenarios/ inside the real
    # lean-coder tree, so a probe that runs `git commit` walks UP to the repo's own .git and
    # pollutes real history (commit "Refactor util.py..." leaked in exactly this way). Fence
    # git to the scenario cwd: GIT_CEILING_DIRECTORIES stops git traversing at/above cwd, so a
    # mock commit fails harmlessly ("not a git repository") instead of hitting the parent repo.
    # Process-wide is safe here: the drainer runs probes strictly serially (max_workers=1).
    # D1: git does NOT honor a ceiling equal to the start dir (it still checks cwd and
    # then walks up) - the ceiling must be the PARENT so discovery stops at cwd itself.
    os.environ["GIT_CEILING_DIRECTORIES"] = str(cfg.cwd.parent)

    # Pin provider + model. Mark the model explicit so a per-host/provider [models]
    # default can't override the model we're certifying.
    cfg.provider = provider or "ollama"
    cfg.think = think
    # Sampling: NOT set (2026-10-21, lean-coder >= 0.10.86). A fresh Config has every
    # sampling field None, so the provider sends nothing and the model card / server preset
    # applies (Qwen3.8 thinking = temp 1.0, top_p 0.95, top_k 20). The old forced
    # temperature 0.0 was greedy decoding, which Qwen documents as causing endless
    # repetition in thinking mode. Effective values + source are stamped per run
    # (collect_sampling). EVAL_TEMPERATURE overrides, for deliberate experiments only.
    # EVAL_SAMPLING: JSON {"temperature":..,"top_k":..,"top_p":..,"min_p":..}; only sets
    # fields this core's Config actually has. EVAL_TEMPERATURE (below) wins on temperature.
    _apply_sampling_env(cfg)
    if is_ollama:
        # ollama: pin exactly the host given (no priority pool), no auto-evict.
        cfg.host = lc._norm_host(host)
        cfg.hosts = []
        cfg.num_ctx = num_ctx
        cfg.keep_alive = -1
        # 3-tier streaming timeouts (core stream_tiered). Prod default is a blind
        # 600s wall (ttft=600, idle=None) that a wedged prefill hits on EVERY run -
        # e.g. CX1 on a memory-pressured M4 that accepts the request then emits
        # nothing. The gym tightens both so a stall voids FAST and TYPED: a prefill
        # stall trips in ~120s (StreamStall.kind="prefill"), a mid-stream decode
        # stall in ~30s ("decode"). Both surface as ConnectionError -> the run voids;
        # _run_probe_once's void_diag classifies which. Overridable via env for a
        # slow box. A legit slow-but-live stream never trips idle (resets per token).
        #
        # THINK-AWARE: a reasoning model (think=ON) emits its ENTIRE thinking block
        # server-side BEFORE the first streamable line - tens of seconds to minutes of
        # prefill with NOTHING on the socket. The tight no-think deadlines (connect=10s,
        # ttft=120s, idle=30s) mass-VOIDED a qwen3:32b think-auto smoke: 120/126 runs
        # tripped a ~10s "prefill stall" while the model was healthily reasoning. So when
        # think is on we grant generous headroom (connect 60s, ttft 600s, idle 120s):
        # the model gets minutes to reason before first token, and a genuine wedge still
        # voids (just later). Env overrides always win - a slow box can retune either mode.
        _think_on = bool(think)
        _c = "60" if _think_on else "10"
        # C14: no-think ttft raised 120 -> 300 (slow dense models tripped 120 on direct
        # runs); if EVAL_GEN_* are unset (direct run, no drainer calibrator) scale from a
        # cached hostbench result for this host+model when one exists. Env still wins.
        _t = "600" if _think_on else "300"
        _i = "120" if _think_on else "30"
        if not _think_on:
            _cal = _cached_calibration(host, model, provider)
            if _cal:
                _c, _t, _i = _cal
        cfg.gen_connect_timeout = float(os.environ.get("EVAL_GEN_CONNECT_TIMEOUT", _c))
        cfg.gen_ttft_timeout = float(os.environ.get("EVAL_GEN_TTFT_TIMEOUT", _t))
        cfg.gen_idle_timeout = float(os.environ.get("EVAL_GEN_IDLE_TIMEOUT", _i))
    # A fresh Config() has providers_enabled=[] (the real path fills it from the config
    # file, which we deliberately DON'T read - Gate 1). Enable ONLY the provider we're
    # about to use so register_enabled registers exactly it.
    cfg.providers_enabled = ["ollama"] if is_ollama else [cfg.provider]
    # EVAL_PROVIDER_SETTINGS='{"kilo_pin": "on"}': provider-scoped knobs for the active
    # provider (a fresh Config reads no config.toml). Stamped in provenance when set.
    _ps = os.environ.get("EVAL_PROVIDER_SETTINGS", "").strip()
    if _ps:
        for _k, _v in json.loads(_ps).items():
            cfg.provider_settings.setdefault(cfg.provider, {})[_k] = _v
    # A cert measures ONE model: free lanes that rotate to another model on a busy minute
    # (ovh, kilo) are pinned ON unless explicitly overridden above. Unpinned, OVH answered
    # turns with gpt-oss-120b while labelled Qwen3.5 (seen 2026-10-23).
    _pin = f"{cfg.provider}_pin"
    if cfg.provider in ("ovh", "kilo"):
        cfg.provider_settings.setdefault(cfg.provider, {}).setdefault(_pin, "on")
    cfg.set_active_model(model)
    cfg.model_explicit = True

    # Register providers then activate - the SAME path run_agent_brief uses.
    # _maybe_autostart_provider runs the provider's prepare() (ollama host probe /
    # hosted auth) + activate_provider (builds the real client, sets ctx window).
    lc._register_dir_providers(cfg)
    agent = lc.Agent(cfg)
    prov = lc._maybe_autostart_provider(agent, cfg)
    if prov is None or agent.client is None:
        where = host if is_ollama else cfg.provider
        raise RuntimeError(f"no {cfg.provider} provider/model available "
                           f"({where}) for {model}")
    # activate_provider restores the provider's remembered model; re-pin OURS + rebuild
    # the system prompt so cwd + model are right.
    if cfg.active_model() != model:
        cfg.set_active_model(model)
        agent.messages[0] = {"role": "system", "content": agent._system()}
    _register_agent(agent)          # so the harness can read token burn post-run
    return agent


# --- per-run token accounting -------------------------------------------------------
# The Agent path dropped the old per-call eval_count telemetry (_run_meta), but every
# lc.Agent still accumulates session_in / session_out token counts. A probe builds one
# (sometimes >1, e.g. compact+resume) Agent per run and never hands it back, so we
# can't read those counts from the returned verdict. Instead build_agent REGISTERS each
# Agent it mints on a shared list; the harness resets the list before a run and sums the
# agents' counts after - capturing token burn with ZERO edits to the 21 probes.
#
# NOT thread-local: _run_probe_once runs the probe in a ThreadPoolExecutor WORKER thread
# (so a hung run can be abandoned on timeout) but calls collect/reset from the MAIN
# thread. A threading.local() registry is invisible across that boundary - the main
# thread saw an empty list and every run banked {in:0,out:0} (and void_diag misread every
# stall as "no-agent"). The drain is strictly serial (max_workers=1, one probe at a
# time), so a plain module-level list is correct; a lock keeps append/reset atomic.
import threading as _threading  # noqa: E402
_agent_reg: list = []
_agent_reg_lock = _threading.Lock()


# Run generations (op 24a review): reset_token_accounting runs INSIDE the probe's worker
# thread and stamps it with a fresh generation. An agent built later by an ORPHANED (timed
# out) thread carries its stale generation: it is never registered into the next run, and if
# its run was aborted it is born aborted.
_run_gen = 0
_aborted_gens: set = set()
_tl = _threading.local()
_sticky_cls: dict = {}


def _stick_abort(agent) -> None:
    """Make agent._abort permanently True. lean-coder resets self._abort=False at the start
    of every turn (_sigint_guard), so a plain assignment would be wiped by the orphan's next
    turn; a class-level data descriptor wins over that instance write."""
    cls = type(agent)
    if not getattr(cls, "_harness_sticky_abort", False):
        sub = _sticky_cls.get(cls)
        if sub is None:
            sub = _sticky_cls[cls] = type(cls.__name__, (cls,), {
                "_harness_sticky_abort": True,
                "_abort": property(lambda self: True, lambda self, v: None)})
        agent.__class__ = sub


def _register_agent(agent) -> None:
    with _agent_reg_lock:
        g = getattr(_tl, "gen", _run_gen)
        if g in _aborted_gens:
            try:
                _stick_abort(agent)
            except Exception:   # noqa
                pass
            return
        if g != _run_gen:
            return                       # stale orphan: never pollute the current run
        _agent_reg.append(agent)


def reset_token_accounting() -> None:
    """Clear the per-run agent registry and start a new run generation (call BEFORE a probe
    run, from the thread that runs it)."""
    global _run_gen
    with _agent_reg_lock:
        _agent_reg.clear()
        _run_gen += 1
        _tl.gen = _run_gen


def collect_token_usage() -> dict:
    """Sum token burn across every Agent built during the current run (call AFTER).
    Returns {in, out, total, agents} plus honest decode telemetry {eval_tokens,
    eval_ns} (core >=0.9.5: session_eval_*, accumulated ONLY when a call reported both
    a token count and a gen-time, so eval_tokens/eval_ns yields decode tok/s that
    excludes prefill, tool round-trips and warm-up). API backends report 0 -> stays 0
    (we never fabricate tok/s off wall-clock). Best-effort: an Agent missing any counter
    contributes 0, never raises - telemetry must never break a drain."""
    with _agent_reg_lock:
        agents = list(_agent_reg)
    tin = sum(int(getattr(a, "session_in", 0) or 0) for a in agents)
    tout = sum(int(getattr(a, "session_out", 0) or 0) for a in agents)
    etok = sum(int(getattr(a, "session_eval_tokens", 0) or 0) for a in agents)
    ens = sum(int(getattr(a, "session_eval_ns", 0) or 0) for a in agents)
    # Prompt-cache reads (hosted/plan providers that report them via a client
    # _sess_cache_read): needed to extrapolate plan-quota cost, since session_in can
    # exclude cached input. Absent -> 0.
    def _cli(a, k):
        return int(getattr(getattr(a, "client", None), k, 0) or 0)
    cread = sum(_cli(a, "_sess_cache_read") for a in agents)
    cwrite = sum(_cli(a, "_sess_cache_write") for a in agents)
    return {"in": tin, "out": tout, "total": tin + tout, "agents": len(agents),
            "eval_tokens": etok, "eval_ns": ens, "cache_read": cread, "cache_write": cwrite}


def abort_agents() -> int:
    """Trip lean-coder's cooperative abort (agent._abort, the ^C path) on every agent built
    this run, so a timed-out run's orphaned thread stops at its next wait / tool boundary
    instead of retrying in the background (op 24a: orphans kept Kilo's 429 cap saturated).
    Returns how many were flagged. Never raises."""
    with _agent_reg_lock:
        _aborted_gens.add(_run_gen)      # agents the orphan builds later are born aborted
        agents = list(_agent_reg)
    n = 0
    for a in agents:
        try:
            _stick_abort(a)
            n += 1
        except Exception:   # noqa
            pass
    return n


def collect_finish_reasons() -> list:
    """Per-agent stop reasons for the current run (lean-coder >= t23: agent.finish_reasons,
    one per assistant reply: stop|length|tool_calls|content_filter|refusal|error|None).
    evalcli._finish_void voids a run whose primary agent's final reply is error/content_filter (op 21a). Older cores -> []."""
    with _agent_reg_lock:
        agents = list(_agent_reg)
    out = []
    for a in agents:
        fr = getattr(a, "finish_reasons", None)
        if isinstance(fr, list):
            out.append(list(fr))
    return out


def collect_sampling() -> list:
    """Effective sampling per agent built this run (lean_coder.effective_sampling):
    [{agent, params, source, sent}]. Hosted providers / older cores give nothing.
    Best-effort: never raises."""
    with _agent_reg_lock:
        agents = list(_agent_reg)
    eff = getattr(lc, "effective_sampling", None)
    out = []
    if eff is None:
        return out
    for ai, agent in enumerate(agents):
        try:
            r = eff(agent)
        except Exception:                                          # noqa
            r = None
        if r:
            out.append({"agent": ai, "params": r.get("params"), "source": r.get("source"),
                        "sent": r.get("sent")})
    return out


def collect_reasoning() -> list:
    """The per-turn reasoning text captured this run, across every Agent built (call
    AFTER a run). Empty unless EVAL_KEEP_REASONING is on AND the provider client kept a
    .reasoning_log (currently the llamacpp provider). Returns a list of {agent, turns:
    [str,...]}. Best-effort: never raises."""
    with _agent_reg_lock:
        agents = list(_agent_reg)
    out = []
    for ai, agent in enumerate(agents):
        try:
            log = list(getattr(getattr(agent, "client", None), "reasoning_log", None) or [])
        except Exception:                                          # noqa
            log = []
        # drop empty-string turns (no-think turns append "") so a run with no actual
        # reasoning writes nothing - keeps the sidecar clean for harvesting.
        log = [t for t in log if isinstance(t, str) and t.strip()]
        if log:
            out.append({"agent": ai, "turns": log})
    return out


def _debug_on() -> bool:
    """Lazy read of EVAL_DEBUG so a --debug flag set in main() (after import) still
    takes effect. Off by default -> the certification path is byte-identical."""
    return os.environ.get("EVAL_DEBUG", "").strip().lower() in ("1", "true", "yes", "on")


def _blob_cap() -> int:
    return int(os.environ.get("EVAL_TRANSCRIPT_CAP", "8000" if _debug_on() else "800"))


_TRANSCRIPT_BLOB_CAP = 800     # per assistant-content / tool-result blob, chars


def collect_transcript() -> list:
    """FORENSIC transcript of the current run: the ordered assistant<->tool exchange
    across EVERY Agent built this run, flattened to a compact step list so a blown-out
    run (e.g. a 60-call update_plan spiral) is diagnosable by hand instead of guessed.

    Each step is one of:
      {"i", "agent", "assistant": "<content head>"}                    - a text turn
      {"i", "agent", "call": name, "args": {...}, "result": "<head>"}   - a tool call
    Args + results are kept (blob-capped) so we can tell a genuine RETRY loop (identical
    payload re-sent because the result read as a failure) from mere verbosity (evolving
    payloads). Best-effort, never raises - forensics must never break a drain."""
    cap = _blob_cap()

    def _clip(x):
        if isinstance(x, str):
            return (x if len(x) <= cap
                    else x[:cap] + f"...<+{len(x) - cap}>")
        if isinstance(x, dict):
            return {k: _clip(v) for k, v in x.items()}
        if isinstance(x, list):
            return [_clip(v) for v in x]
        return x
    debug = _debug_on()
    with _agent_reg_lock:
        agents = list(_agent_reg)
    steps = []
    for ai, agent in enumerate(agents):
        results = {}
        try:
            for m in agent.messages:
                if m.get("role") == "tool":
                    results[m.get("tool_call_id") or m.get("id")] = m.get("content")
        except Exception:                                          # noqa
            continue
        i = 0
        try:
            for m in agent.messages:
                if m.get("role") != "assistant":
                    continue
                calls = m.get("tool_calls") or []
                content = m.get("content")
                if isinstance(content, str) and content.strip() and (debug or not calls):
                    steps.append({"i": i, "agent": ai, "assistant": _clip(content),
                                  "with_tool_calls": bool(calls)})
                    i += 1
                for tc in calls:
                    fn = tc.get("function") or tc
                    args = fn.get("arguments") or {}
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except Exception:                          # noqa
                            args = {"_raw": args}
                    res = results.get(tc.get("id"))
                    steps.append({"i": i, "agent": ai, "call": fn.get("name"),
                                  "args": _clip(args),
                                  "result": _clip(res) if isinstance(res, str) else res})
                    i += 1
        except Exception:                                          # noqa
            continue
    return steps

# FM3 (runaway guillotine): FM1 caps each turn at EVAL_MAX_ITERATIONS rounds, but a
# recency-trapped model can still burn the whole cap on one pointless action repeated
# verbatim (the 55x identical `cat greet.py`). That is not a real result - it is a
# spiral truncated at the cap - and must VOID (re-run), not score. We detect it from the
# forensic transcript: a run of N+ CONSECUTIVE tool calls with an identical (name,args)
# payload is a runaway loop. Distinct from mere verbosity (evolving args) which is fine.
EVAL_RUNAWAY_REPEATS = int(os.environ.get("EVAL_RUNAWAY_REPEATS", "5"))


def detect_runaway(transcript: list, threshold: int = EVAL_RUNAWAY_REPEATS):
    """Scan a collect_transcript() list for a runaway loop: `threshold`+ CONSECUTIVE
    tool calls with an identical (name, args) payload. Returns the offending
    {"call", "count"} dict, or None if no runaway. Best-effort, never raises."""
    try:
        import json as _json
        prev_key = None
        run = 0
        for step in transcript:
            name = step.get("call")
            if not name:                       # a text turn breaks the streak
                prev_key, run = None, 0
                continue
            key = (name, _json.dumps(step.get("args", {}), sort_keys=True, default=str))
            if key == prev_key:
                run += 1
            else:
                prev_key, run = key, 1
            if run >= threshold:
                return {"call": name, "count": run}
    except Exception:                          # noqa
        return None
    return None


def dump_full_messages():
    """DEBUG only: the ENTIRE message list (system/user/assistant/tool, verbatim) for every
    agent built this run. No role filtering, no tool-call gating - the true wire history.
    Blob-capped by EVAL_TRANSCRIPT_CAP. Best-effort, never raises."""
    if not _debug_on():
        return None
    cap = _blob_cap()

    def _clip_blob(x):
        if isinstance(x, str):
            return x if len(x) <= cap else x[:cap] + f"...<+{len(x) - cap}>"
        return x
    out = []
    with _agent_reg_lock:
        agents = list(_agent_reg)
    for ai, agent in enumerate(agents):
        msgs = []
        try:
            for m in agent.messages:
                msgs.append({"role": m.get("role"),
                             "content": _clip_blob(m.get("content")),
                             "tool_calls": m.get("tool_calls"),
                             "tool_call_id": m.get("tool_call_id")})
        except Exception:                                          # noqa
            continue
        out.append({"agent": ai, "messages": msgs})
    return out


# --------------------------------------------------------------------------- reads
def last_assistant_text(agent: "lc.Agent") -> str:
    """The most recent non-empty assistant message text (the model's 'answer')."""
    for m in reversed(agent.messages):
        if (m.get("role") == "assistant" and isinstance(m.get("content"), str)
                and m["content"].strip()):
            return m["content"]
    return ""


def tool_calls_since(agent: "lc.Agent", mark: int) -> list[dict]:
    """Every tool call in assistant messages from index `mark` onward, flattened to
    {name, arguments}. `mark` is len(agent.messages) captured BEFORE a run_turn, so
    this returns just that turn's calls. Reads the same shape core stores."""
    out = []
    for m in agent.messages[mark:]:
        if m.get("role") != "assistant":
            continue
        for tc in (m.get("tool_calls") or []):
            fn = tc.get("function") or tc
            args = fn.get("arguments") or {}
            if isinstance(args, str):                  # some providers hand args as a
                try:                                   # JSON string; decode to match
                    args = json.loads(args)            # collect_transcript's shape so
                except Exception:                      # noqa - scorers see a dict, not
                    args = {"_raw": args}              # a str (was a false-fail landmine)
            out.append({"name": fn.get("name"), "arguments": args,
                        "id": tc.get("id") or ""})
    return out


def first_tool_since(agent: "lc.Agent", mark: int) -> str | None:
    calls = tool_calls_since(agent, mark)
    return calls[0]["name"] if calls else None


def called_tool_since(agent: "lc.Agent", mark: int, name: str) -> bool:
    return any(c["name"] == name for c in tool_calls_since(agent, mark))


def durable_file(agent: "lc.Agent", name: str) -> str:
    """Read a file the model may have written under the agent's cwd (durable-doc check)."""
    p = Path(agent.cfg.cwd) / name
    return p.read_text() if p.exists() else ""


# =========================================================================== probe cancel
# Shared by probes_integrity / probes_pentest (C8). A probe's work runs on a DAEMON thread
# named "probe-<suite>" (a hung provider stream can never block interpreter exit, and
# evalcli._await_stray_threads waits only on "probe-*" threads). On timeout the cancel Event
# is set; the armed agent then stops at its next checkpoint:
#   - Agent._run_tool / Agent._dispatch (looked up via self.<name> at call time in
#     lean_coder.run_turn/_run_tool, so instance attributes shadow them) raise ProbeCancelled;
#   - agent._abort = True: run_turn checks it between tool calls, and every provider gets
#     should_abort=lambda: self._abort and polls it per streamed chunk;
#   - agent.client.chat (looked up via self.client at call time) raises before a new model
#     call and ORs the cancel flag into should_abort.
# A stream hung with NO bytes arriving can't poll; daemon=True is the backstop there.
TASK_THREAD_PREFIX = "probe-"
CANCEL_GRACE = 30


class TaskCancelledSoft(Exception):
    """Cancel seen on a NON-probe thread (lean-coder tool worker): an ordinary Exception
    so the agent's tool error handling absorbs it instead of the thread dying."""


class TaskCancelled(BaseException):
    """BaseException so the agent's `except Exception` tool-error path can't swallow it."""


_CANCEL_MSG = "error: cancelled (probe timed out)"
_WRAP_LOCK = __import__("threading").Lock()
_AGENT_GUARDED = ("_dispatch", "_run_tool")
# methods that may swap agent.client with no _dispatch/_run_tool in between -> rearm after
_AGENT_SWAPPERS = ("_apply_model", "activate_provider", "use_client")


def _task_idents(cancel):
    ids = getattr(cancel, "_task_idents", None)
    if ids is None:
        ids = cancel._task_idents = set()
    return ids


def _cancel_check(cancel):
    """If cancelled: raise on a probe-owned thread; elsewhere (e.g. lean_coder
    _run_parallel workers) return the cancelled tool string so the worker doesn't die."""
    import threading
    if not cancel.is_set():
        return None
    if threading.get_ident() in _task_idents(cancel):
        raise TaskCancelled("probe timed out")
    return _CANCEL_MSG


_WRAP_WARNED = []


def _warn_wrap_failed(what, e):
    """LOW: a failed cancel-wrap means a timed-out probe may not stop promptly; say so
    on stderr ONCE per process instead of failing silently."""
    if _WRAP_WARNED:
        return
    _WRAP_WARNED.append(what)
    print(f"leangym: warning: probe cancel wrap of {what} failed "
          f"({type(e).__name__}: {e}); timeouts may not interrupt this agent",
          file=sys.stderr, flush=True)


def _arm_cancel(agent, cancel):
    """Wire `cancel` (threading.Event) into `agent`. Safe on fakes missing attributes.
    Idempotent: always wraps from the saved originals (see _disarm_cancel)."""
    import threading
    _task_idents(cancel).add(threading.get_ident())   # sync callers (tests on MainThread)
    cancel.__dict__.setdefault("_task_armers", set()).add(threading.get_ident())
    agents = getattr(cancel, "_task_agents", None)
    if agents is None:
        agents = cancel._task_agents = []
    if agent not in agents:
        agents.append(agent)
    orig = agent.__dict__.get("_task_orig") if hasattr(agent, "__dict__") else None
    if orig is None:
        orig = {}
        try:
            agent._task_orig = orig
        except Exception:
            pass
    clients = agent.__dict__.setdefault("_task_clients", []) \
        if hasattr(agent, "__dict__") else []

    def _wrap_client():
        # Re-armed lazily: model activation may replace agent.client (losing the wrap).
        with _WRAP_LOCK:
            client = getattr(agent, "client", None)
            chat = getattr(client, "chat", None)
            if not callable(chat) or getattr(chat, "_task_cancel", None) is cancel:
                return
            base = getattr(chat, "_task_base", chat)
            if not hasattr(chat, "_task_base"):
                try:
                    client._task_orig_chat = (("chat" in getattr(client, "__dict__", {})),
                                               chat)
                except Exception:
                    pass

            def _chat(*a, **k):
                if cancel.is_set():
                    # review LOW: only the probe's own thread raises; a lean-coder worker
                    # thread (parallel tool calling chat) must not die - should_abort and
                    # the probe thread's next check end the run.
                    if _threading.get_ident() in _task_idents(cancel):
                        raise TaskCancelled("probe timed out")
                    # review MED: a non-probe (lean-coder worker) thread must not send a
                    # request at all (providers only check should_abort after the request
                    # is out). Raise an ordinary Exception: the tool's own error handling
                    # turns it into an error result and the worker thread survives.
                    raise TaskCancelledSoft("probe timed out")
                sa = k.get("should_abort")
                k["should_abort"] = (lambda: cancel.is_set() or bool(sa and sa()))
                return base(*a, **k)
            _chat._task_cancel = cancel
            _chat._task_base = base
            try:
                setattr(client, "chat", _chat)
                if client not in clients:
                    clients.append(client)
            except Exception as e:                                    # noqa
                _warn_wrap_failed("client.chat", e)

    def _install(name, make):
        if name not in orig:
            cur = getattr(agent, name, None)
            if not callable(cur):
                return
            orig[name] = (name in getattr(agent, "__dict__", {}), cur)
        base = orig[name][1]
        try:
            setattr(agent, name, make(base))
        except Exception as e:                                        # noqa
            _warn_wrap_failed(name, e)

    for name in _AGENT_GUARDED:
        def _mk(base):
            def _wrapped(*a, **k):
                r = _cancel_check(cancel)
                if r is not None:
                    return r
                _wrap_client()
                return base(*a, **k)
            return _wrapped
        _install(name, _mk)
    for name in _AGENT_SWAPPERS:
        def _mk(base):
            def _wrapped(*a, **k):
                try:
                    return base(*a, **k)
                finally:
                    _wrap_client()
            return _wrapped
        _install(name, _mk)

    def _mk_fb(base):
        def _wrapped(*a, **k):
            _wrap_client()
            return base(*a, **k)
        return _wrapped
    _install("_chat_with_model_fallback", _mk_fb)
    _wrap_client()
    try:
        agent._task_rearm = _wrap_client
    except Exception as e:                                            # noqa
        _warn_wrap_failed("_task_rearm", e)


def _disarm_cancel(agent):
    """Restore every method/chat wrapped by _arm_cancel (call only once the probe
    thread has finished)."""
    with _WRAP_LOCK:
        d = getattr(agent, "__dict__", {})
        for name, (had, fn) in (d.pop("_task_orig", None) or {}).items():
            try:
                if had:
                    setattr(agent, name, fn)
                else:
                    d.pop(name, None)
            except Exception:
                pass
        for client in d.pop("_task_clients", None) or ():
            o = getattr(client, "_task_orig_chat", None)
            if not o:
                continue
            had, fn = o
            try:
                if had:
                    client.chat = fn
                else:
                    client.__dict__.pop("chat", None)
                del client._task_orig_chat
            except Exception:
                pass
        d.pop("_task_rearm", None)


def _disarm_all(cancel):
    for ag in getattr(cancel, "_task_agents", ()):
        _disarm_cancel(ag)
    # review LOW: drop the arming thread idents too (idents are recycled)
    for i in getattr(cancel, "_task_armers", ()):
        _task_idents(cancel).discard(i)


def _start_task_thread(work, suite="task", cancel=None):
    """Run work() on a daemon 'probe-<suite>' thread. Returns (thread, box); after the
    thread ends box has 'result' or 'exc'. With `cancel`, the thread registers as a
    probe-owned thread (cancel raises ProbeCancelled there)."""
    import threading
    box = {}

    def _target():
        if cancel is not None:
            _task_idents(cancel).add(threading.get_ident())
        try:
            box["result"] = work()
        except BaseException as e:                                   # noqa - reported
            box["exc"] = e
        finally:          # review LOW: idents are recycled after exit - drop ours
            if cancel is not None:
                _task_idents(cancel).discard(threading.get_ident())
    th = threading.Thread(target=_target, daemon=True,
                          name=f"{TASK_THREAD_PREFIX}{suite}")
    th.start()
    return th, box


def _cancel_and_join(cancel, thread, grace=None):
    """Set cancel, flag every armed agent's _abort, then join `thread` for `grace` s."""
    cancel.set()
    for ag in getattr(cancel, "_task_agents", ()):
        try:
            ag._abort = True
        except Exception:
            pass
    if thread is not None:
        thread.join(CANCEL_GRACE if grace is None else grace)

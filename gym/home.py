"""Gym-owned lean-coder home (D19a/D20).

The worker stage runs the gym's OWN lean-coder clone (.gym/lean-coder, D18) as a real
`--agent-run` subprocess with HOME = ~/.config/leangym/home, so its ~/.config/leancoder
resolves INSIDE the gym home. The operator's ~/.config/leancoder is never read or written
by a worker. Auth gets there only through the operator's explicit `evalcli auth import`.

Layout:
  ~/.config/leangym/home/.config/leancoder/
      config.toml           generated per worker run (only the eval provider enabled)
      providers/*.py        plugin copies (auth import), for providers the public clone lacks
      <auth files>          copied by `auth import <provider>` (0600)
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
from pathlib import Path

GYM_DIR = Path(os.environ.get("LEANGYM_HOME") or Path.home() / ".config" / "leangym")
HOME = GYM_DIR / "home"
LC_CFG = HOME / ".config" / "leancoder"
SRC_CFG = Path.home() / ".config" / "leancoder"          # the operator's (read on import ONLY)
SRC_REPO = Path(os.environ.get("LEANCODER_REPO") or Path.home() / "lean-coder")
CLONE = Path(__file__).resolve().parents[1] / ".gym" / "lean-coder"
CLONE_URL = "https://github.com/codemonkeying/lean-coder.git"
CLONE_PIN = "ea2619f"           # 0.10.119 (rehash of c8219ae after lean-coder history rewrite, same code, t85): busy-aware session takeover; 0.10.118 worker failure reasons (secrets masked), workers compact, workers inherit driver live account (t79); bump deliberately


def _git_env() -> dict:
    """os.environ minus every GIT_* var, so an inherited GIT_DIR (e.g. under a git hook in a
    linked worktree) can't redirect these calls at the outer repo; never prompt."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def clone(pin: str = CLONE_PIN, url: str = CLONE_URL) -> str:
    """Clone/fetch the public lean-coder into .gym/lean-coder and check out `pin`."""
    import subprocess
    if not (CLONE / ".git").is_dir():
        CLONE.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "-q", url, str(CLONE)], check=True, env=_git_env())
    else:
        subprocess.run(["git", "-C", str(CLONE), "fetch", "-q", "origin"], check=True, env=_git_env())
    if not re.fullmatch(r"[0-9a-f]{7,40}", pin or ""):
        raise ValueError(f"pin must be a commit sha, got {pin!r}")
    subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-C", str(CLONE), "checkout",
                    "-q", "--detach", pin, "--"], check=True, env=_git_env())
    return clone_head()


def clone_head() -> dict | None:
    """{head, pin, at_pin, dirty} of the gym clone (recorded in every task report)."""
    import subprocess
    g = ["git", "-C", str(CLONE)]
    try:
        head = subprocess.run(g + ["rev-parse", "HEAD"], capture_output=True, text=True,
                              check=True, env=_git_env()).stdout.strip()
        dirty = bool(subprocess.run(g + ["status", "--porcelain"], capture_output=True,
                                    text=True, check=True, env=_git_env()).stdout.strip())
    except (OSError, subprocess.CalledProcessError):
        return None
    return {"head": head[:12], "pin": CLONE_PIN, "at_pin": head.startswith(CLONE_PIN),
            "dirty": dirty}

# never copied, whatever the glob says
_DENY = re.compile(r"^(config\.toml.*|history|model-cache.*|ratelog-.*|debug\.log)$")


# OAuth / subscription-plan providers keep their credentials in auth.json +
# auth<label>.json rather than <provider>.json. Their names are site-specific and
# never hardcoded here: set LEANGYM_PLAN_PROVIDERS=name1,name2 to opt in. A public
# checkout leaves this empty, so only the <provider>.json/.key convention applies.
PLAN_PROVIDERS = tuple(
    p.strip() for p in os.environ.get("LEANGYM_PLAN_PROVIDERS", "").split(",") if p.strip()
)


def auth_files(provider: str, src: Path = SRC_CFG) -> list:
    """The operator's auth/key files for one provider (names only by convention:
    <provider>.json / <provider>.key; plan providers also use auth.json + auth<label>.json)."""
    pats = [f"{provider}.json", f"{provider}.key"]
    if provider in PLAN_PROVIDERS:
        pats += ["auth.json", "auth*.json"]
    out = []
    for p in pats:
        for f in sorted(src.glob(p)):
            if f.is_file() and not _DENY.match(f.name) and f not in out:
                out.append(f)
    return out


def _bundled(name: str) -> bool:
    """Does the gym clone ship this provider plugin (lean-coder's _bundled_providers_dir)?"""
    return (CLONE / "providers" / name).is_file()


def _copy_private(src: Path, out: Path):
    """0600 copy that never follows a planted destination symlink."""
    if out.is_symlink() or out.exists():
        out.unlink()
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(src.read_bytes())


def import_auth(providers, *, src: Path = SRC_CFG, repo: Path = SRC_REPO,
                dst: Path | None = None) -> dict:
    """Operator-run copy: each provider's auth files (0600) + its plugin .py if the gym
    clone doesn't ship it. Returns {provider: [copied names]}; missing = []."""
    dst = dst or LC_CFG
    dst.mkdir(parents=True, exist_ok=True)
    for d in {dst, *[p for p in dst.parents if GYM_DIR in (p, *p.parents)]}:
        os.chmod(d, 0o700)
    report = {}
    for prov in providers:
        if not re.fullmatch(r"[a-z0-9_]+", prov or ""):
            raise ValueError(f"bad provider name {prov!r}")
        got = []
        for f in auth_files(prov, src):
            if f.is_symlink():
                continue                                  # never chase links out of src
            _copy_private(f, dst / f.name)
            got.append(f.name)
        plug = repo / "providers" / f"{prov}.py"
        if plug.is_file() and not plug.is_symlink() and not _bundled(plug.name):
            (dst / "providers").mkdir(exist_ok=True)
            _copy_private(plug, dst / "providers" / plug.name)   # CODE: reported to operator
            got.append(f"providers/{plug.name}")
        report[prov] = got
    return report


def list_home(dst: Path | None = None) -> list:
    dst = dst or LC_CFG
    if not dst.is_dir():
        return []
    return sorted(str(p.relative_to(dst)) for p in dst.rglob("*")
                  if p.is_file() and p.name != "config.toml")


def _toml_str(v) -> str:
    return json.dumps(str(v))          # JSON string escapes are valid TOML basic strings


def _toml_val(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_toml_val(x) for x in v) + "]"
    return _toml_str(v)


def write_config(provider: str, model: str, settings: dict | None = None,
                 dst: Path | None = None, top: dict | None = None) -> Path:
    """Fresh config.toml for one worker run: ONLY the eval provider enabled. `top` =
    extra top-level keys (ollama reads host/think there)."""
    if not re.fullmatch(r"[a-z0-9_]+", provider or ""):
        raise ValueError(f"bad provider name {provider!r}")
    dst = dst or LC_CFG
    dst.mkdir(parents=True, exist_ok=True)
    lines = [f"provider = {_toml_str(provider)}", f"model = {_toml_str(model)}",
             f"providers_enabled = [{_toml_str(provider)}]", 'approval = "auto"',
             "autosave = false", "auto_compact = false"]
    lines += [f"{k} = {_toml_val(v)}" for k, v in (top or {}).items()
              if re.fullmatch(r"[A-Za-z0-9_]+", k) and v is not None]
    lines += ["", f"[providers.{provider}]", f"model = {_toml_str(model)}"]
    for k, v in (settings or {}).items():
        if re.fullmatch(r"[A-Za-z0-9_]+", k) and v is not None:
            lines.append(f"{k} = {_toml_val(v)}")
    p = dst / "config.toml"
    p.write_text("\n".join(lines) + "\n")
    return p


def run_home(tag: str) -> Path:
    """Per-run HOME (parallel-safe): GYM_DIR/homes/<tag>/ with the imported auth + plugins
    symlinked in from the shared gym home. Caller removes it after the run.
    NOTE: isolation, not a sandbox - a leash-rwe worker can still read absolute paths."""
    if not re.fullmatch(r"[A-Za-z0-9._-]+", tag):
        raise ValueError(f"bad tag {tag!r}")
    hd = GYM_DIR / "homes" / tag
    cfg = hd / ".config" / "leancoder"
    cfg.mkdir(parents=True, exist_ok=True)
    snap = {}
    for d in (GYM_DIR, GYM_DIR / "homes", hd):
        os.chmod(d, 0o700)
    if LC_CFG.is_dir():
        for f in LC_CFG.iterdir():
            if f.is_file() and not _DENY.match(f.name) and not (cfg / f.name).exists():
                (cfg / f.name).symlink_to(f)
                snap[f.name] = _sha(f)
        if (LC_CFG / "providers").is_dir() and not (cfg / "providers").exists():
            (cfg / "providers").symlink_to(LC_CFG / "providers")
    (hd / ".gitconfig").write_text("[user]\n\tname = leangym\n\temail = gym@localhost\n")
    (hd / ".gym-snap.json").write_text(json.dumps(snap))
    return hd


def _sha(p: Path) -> str:
    import hashlib
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def provider_globals(lc) -> list:
    """Module dicts of every REGISTERED provider plugin. lean-coder exec()s plugins into
    bare modules (not in sys.modules), so reach them through a registered spec's
    function __globals__."""
    out, seen = [], set()
    for spec in list(getattr(lc, "_providers", {}).values()):
        for v in (spec.values() if isinstance(spec, dict) else ()):
            v = getattr(v, "func", v); v = getattr(v, "__func__", v)
            g = getattr(v, "__globals__", None)
            if isinstance(g, dict) and "_CONFIG_DIR" in g and id(g) not in seen:
                seen.add(id(g)); out.append(g)
    return out


def provider_globals_for(lc, name: str):
    spec = getattr(lc, "_providers", {}).get(name)
    for v in (spec.values() if isinstance(spec, dict) else ()):
        v = getattr(v, "func", v); v = getattr(v, "__func__", v)
        g = getattr(v, "__globals__", None)
        if isinstance(g, dict) and "_CONFIG_DIR" in g:
            return g
    return None


def leaks(lc, cfgdir: Path) -> list:
    """Registered providers whose _CONFIG_DIR is not `cfgdir` (None counts: it falls back
    to ~/.config/leancoder)."""
    return [g.get("__name__", "?") for g in provider_globals(lc)
            if g.get("_CONFIG_DIR") is None or Path(g["_CONFIG_DIR"]) != Path(cfgdir)]


@contextlib.contextmanager
def redirect_lc(lc, cfgdir: Path):
    """In-process isolation: rebind lean_coder's CONFIG_DIR and every module-level Path
    derived from it (sessions, history, taskboards, ratelog...) plus registered provider
    plugins' `_CONFIG_DIR` to `cfgdir`. Providers set up INSIDE the block pick up the new
    CONFIG_DIR themselves (build the agent inside it; check `leaks()` after). On exit every
    provider seen (before or during) gets its entry value back, or the old dir."""
    cfgdir = Path(cfgdir)
    old = Path(lc.CONFIG_DIR)
    saved = {}
    for k, v in list(vars(lc).items()):
        if isinstance(v, Path):
            try:
                rel = v.relative_to(old)
            except ValueError:
                continue
            saved[k] = v
            setattr(lc, k, cfgdir / rel)
    psaved = {}
    for g in provider_globals(lc):
        psaved[id(g)] = (g, g["_CONFIG_DIR"])
        g["_CONFIG_DIR"] = cfgdir
    try:
        yield cfgdir
    finally:
        for k, v in saved.items():
            setattr(lc, k, v)
        for g in provider_globals(lc):
            psaved.setdefault(id(g), (g, old))
        for g, v in psaved.values():
            g["_CONFIG_DIR"] = v


def drop_run_home(hd: Path) -> list:
    """Remove a run_home. First copy back any auth file a provider REPLACED (atomic write
    turns our symlink into a regular file - e.g. a rotated OAuth refresh token) into the
    shared gym home, so the rotation is not lost. Returns the names copied back."""
    hd = Path(hd)
    root = (GYM_DIR / "homes").resolve()
    if hd.is_symlink() or hd.resolve().parent != root:
        raise ValueError(f"refusing to remove {hd} (not a run home)")
    back = []
    cfg = hd / ".config" / "leancoder"
    try:
        snap = json.loads((hd / ".gym-snap.json").read_text())
    except (OSError, ValueError):
        snap = {}
    if cfg.is_dir() and LC_CFG.is_dir():
        import fcntl
        with open(LC_CFG / ".lock", "a") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)              # serialize parallel runs' copy-back
            for name, h0 in snap.items():               # only files we symlinked in (auth)
                f, dst = cfg / name, LC_CFG / name
                if not f.is_file() or f.is_symlink() or not dst.is_file() or dst.is_symlink():
                    continue
                if _sha(f) == h0:
                    continue
                if _sha(dst) != h0:                     # shared copy moved on: never clobber
                    back.append(f"{name}:conflict-skipped")
                    continue
                tmp = dst.with_name(dst.name + ".gymtmp")
                shutil.copyfile(f, tmp); os.chmod(tmp, 0o600); os.replace(tmp, dst)
                back.append(name)
    shutil.rmtree(hd, ignore_errors=True)
    return back


_ENV_KEEP = ("PATH", "LANG", "LC_ALL", "TERM", "TZ", "TMPDIR", "PYTHONIOENCODING")


def worker_env(home: Path | None = None, base: dict | None = None, ws: Path | None = None) -> dict:
    """ALLOWLIST env for the worker: everything home-ish inside `home`; no SSH agent, no
    LEANCODER_*/GIT_*/API-key vars from the operator's shell. EVAL_ALLOW_PAID is not needed
    by lean-coder (the gym gates before launch)."""
    home = Path(home or HOME)
    src = base if base is not None else os.environ
    env = {k: src[k] for k in _ENV_KEEP if k in src}
    run = home / "run"
    run.mkdir(parents=True, exist_ok=True)
    os.chmod(run, 0o700)
    env.update(HOME=str(home), XDG_CONFIG_HOME=str(home / ".config"),
               XDG_CACHE_HOME=str(home / ".cache"), XDG_DATA_HOME=str(home / ".local/share"),
               XDG_STATE_HOME=str(home / ".local/state"), XDG_RUNTIME_DIR=str(run))
    if ws is not None:
        env["GIT_CEILING_DIRECTORIES"] = str(Path(ws).parent)
    return env

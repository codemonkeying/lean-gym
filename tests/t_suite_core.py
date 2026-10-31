"""Core staged task: selfcheck (every scorer vs its pass/fail fixtures) + a fake-agent
end-to-end pass through the engine (stage order, only_if, end_files)."""
import dataclasses
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gym.selfcheck import check_suite  # noqa: E402
from gym.stages import run_suite  # noqa: E402
from suites import registry  # noqa: E402

suite = registry()["core"]
fails = check_suite(suite)
assert not fails, "\n".join(fails)


class Fake:
    """No lean-coder: custom runs fall back to plain send; every stage text-only."""
    def __init__(self, ws):
        self.messages = [{"role": "system", "content": "s"}]

    def send(self, p):
        self.messages += [{"role": "user", "content": p},
                          {"role": "assistant", "content": "ok"}]
        return "ok"


seen = []
with tempfile.TemporaryDirectory() as td:
    # server/manual custom runs need lean-coder internals or probes_lean; drop server
    suite = dataclasses.replace(suite, stages=[s for s in suite.stages if s.id != "server"])
    rep = run_suite(suite, Fake, Path(td), n_override=1, log=seen.append)
st = rep["stages"]
assert st["answer"]["rates"]["no_tool"] == 1.0
assert st["compact_elect"]["rates"]["elected"] == 0.0
assert not st["compact_manual"].get("skipped"), "manual must run when elect fails"
assert st["edit_diff"]["context_source"] == "reference"
assert st["git_log"]["context_source"] == "reference"
assert len(suite.checkpoint_ids()) >= 20
print("t_task_core OK", len(suite.checkpoint_ids()), "checkpoints")

# D20 gym home: auth import copies only that provider's files, 0600, never config.toml
import os as _os
from gym import home as _h
with tempfile.TemporaryDirectory() as _d:
    src, dst, repo = Path(_d, "src"), Path(_d, "dst"), Path(_d, "repo")
    (repo / "providers").mkdir(parents=True); src.mkdir()
    for n in ("kilo.json", "config.toml", "auth.json", "auth0.json", "model-cache-kilo.json", "ovh.json"):
        (src / n).write_text("x")
    (repo / "providers" / "zz_test.py").write_text("x")
    assert _h.import_auth(["kilo"], src=src, repo=repo, dst=dst) == {"kilo": ["kilo.json"]}
    _h.PLAN_PROVIDERS = ("plan_demo",)   # opt a plan provider in (auth.json + auth<label>.json convention)
    r = _h.import_auth(["plan_demo", "zz_test"], src=src, repo=repo, dst=dst)
    assert r == {"plan_demo": ["auth.json", "auth0.json"], "zz_test": ["providers/zz_test.py"]}, r
    assert not (dst / "config.toml").exists() and oct((dst / "kilo.json").stat().st_mode)[-3:] == "600"
    try:
        _h.import_auth(["../x"], src=src, repo=repo, dst=dst); raise SystemExit("bad name accepted")
    except ValueError:
        pass
    c = _h.write_config("ovh", "M", {"ovh_pin": "on", "bad key": 1}, dst=dst).read_text()
    assert 'providers_enabled = ["ovh"]' in c and "ovh_pin" in c and "bad key" not in c
    assert _h.worker_env({})["HOME"] == str(_h.HOME)
print("t_task_core gym-home OK")

# review: symlink safety, toml escaping, ollama top-level, env allowlist, prelim isolation
with tempfile.TemporaryDirectory() as _d:
    src, dst, repo = Path(_d, "src"), Path(_d, "dst"), Path(_d, "repo")
    src.mkdir(); dst.mkdir(); (repo / "providers").mkdir(parents=True)
    (src / "kilo.json").write_text("new"); victim = Path(_d, "victim"); victim.write_text("keep")
    (dst / "kilo.json").symlink_to(victim)
    (src / "ovh.json").symlink_to(victim)
    r = _h.import_auth(["kilo", "ovh"], src=src, repo=repo, dst=dst)
    assert victim.read_text() == "keep" and r == {"kilo": ["kilo.json"], "ovh": []}, r
    assert not (dst / "kilo.json").is_symlink()
    import tomllib
    c = tomllib.loads(_h.write_config("ollama", 'm"x\nproviders_enabled = ["evil"]', {"n": 3},
                                      dst=dst, top={"host": "http://localhost:1", "think": False,
                                                    "num_ctx": 32768}).read_text())
    assert c["providers_enabled"] == ["ollama"] and c["think"] is False and c["num_ctx"] == 32768
    assert c["providers"]["ollama"]["n"] == 3 and c["host"] == "http://localhost:1"
    try:
        _h.write_config("x]\n[evil", "m", dst=dst); raise SystemExit("bad provider accepted")
    except ValueError:
        pass
    e = _h.worker_env(Path(_d, "h"), base={"PATH": "/bin", "SSH_AUTH_SOCK": "s",
                                            "LEANCODER_MODEL": "x", "XDG_CACHE_HOME": "/c",
                                            "GIT_CONFIG_GLOBAL": "g"}, ws=Path(_d, "w", "core"))
    assert e["PATH"] == "/bin" and not {"SSH_AUTH_SOCK", "LEANCODER_MODEL", "GIT_CONFIG_GLOBAL"} & set(e)
    assert e["XDG_CACHE_HOME"].startswith(_d) and e["GIT_CEILING_DIRECTORIES"] == str(Path(_d, "w"))
from suites.core.suite import _run_think, P_THINK  # noqa: E402
_fa = Fake(None); _t0 = _run_think(_fa, None, P_THINK)
assert len(_fa.messages) == 1 and _t0.error, "prelim must not touch main history"
print("t_task_core review-fixes OK")

# in-process isolation: redirect_lc rebinds CONFIG_DIR-derived globals + plugin _CONFIG_DIR
import types as _ty  # noqa: E402
_lc = _ty.ModuleType("fake_lc"); _old = Path("/nonexist/.config/leancoder")
_lc.CONFIG_DIR = _old; _lc.SESSIONS_DIR = _old / "sessions"; _lc.OTHER = Path("/etc")
_pg = {"_CONFIG_DIR": _old}
exec("def make_client(cfg): return None", _pg)
_lc._providers = {"fake": {"name": "fake", "make_client": _pg["make_client"]}}
with tempfile.TemporaryDirectory() as _d2:
    with _h.redirect_lc(_lc, Path(_d2)):
        assert _lc.CONFIG_DIR == Path(_d2) and _lc.SESSIONS_DIR == Path(_d2, "sessions")
        assert _lc.OTHER == Path("/etc") and _pg["_CONFIG_DIR"] == Path(_d2)
        assert not _h.leaks(_lc, Path(_d2))
    assert _lc.CONFIG_DIR == _old and _lc.SESSIONS_DIR == _old / "sessions" and _pg["_CONFIG_DIR"] == _old
print("t_task_core redirect_lc OK")

# real providers (exec'd plugins, not in sys.modules) set up INSIDE the redirect
import agent_fixture as _af  # noqa: E402
_prov0, _cd0 = dict(_af.lc._providers), Path(_af.lc.CONFIG_DIR)
with tempfile.TemporaryDirectory() as _d3:
    _c = _af.lc.Config(); _c.providers_enabled = ["ovh", "kilo"]
    with _h.redirect_lc(_af.lc, Path(_d3)):
        _af.lc._register_dir_providers(_c)
        assert _h.provider_globals(_af.lc) and not _h.leaks(_af.lc, Path(_d3)), _h.leaks(_af.lc, Path(_d3))
        assert _h.provider_globals_for(_af.lc, "ovh") is not None
    assert Path(_af.lc.CONFIG_DIR) == _cd0
    assert all(Path(g["_CONFIG_DIR"]) == _cd0 for g in _h.provider_globals(_af.lc))
_af.lc._providers.clear(); _af.lc._providers.update(_prov0)
# drop_run_home: copies back a replaced (non-symlink) auth file, refuses non-run-homes
with tempfile.TemporaryDirectory() as _d4:
    _g0, _l0 = _h.GYM_DIR, _h.LC_CFG
    try:
        _h.GYM_DIR = Path(_d4, "g"); _h.LC_CFG = _h.GYM_DIR / "home/.config/leancoder"
        _h.LC_CFG.mkdir(parents=True); (_h.LC_CFG / "kilo.json").write_text("old")
        _rh = _h.run_home("t1"); _f = _rh / ".config/leancoder/kilo.json"
        assert _f.is_symlink(); _f.unlink(); _f.write_text("rotated")
        assert _h.drop_run_home(_rh) == ["kilo.json"] and not _rh.exists()
        assert (_h.LC_CFG / "kilo.json").read_text() == "rotated"
        _ra, _rb = _h.run_home("ta"), _h.run_home("tb")     # race: B must not clobber A
        for _r, _v in ((_ra, "A"), (_rb, "B")):
            _x = _r / ".config/leancoder/kilo.json"; _x.unlink(); _x.write_text(_v)
        assert _h.drop_run_home(_ra) == ["kilo.json"]
        assert _h.drop_run_home(_rb) == ["kilo.json:conflict-skipped"]
        assert (_h.LC_CFG / "kilo.json").read_text() == "A"
        try:
            _h.drop_run_home(Path(_d4)); raise SystemExit("non-run-home removed")
        except ValueError:
            pass
    finally:
        _h.GYM_DIR, _h.LC_CFG = _g0, _l0
print("t_task_core isolation OK")

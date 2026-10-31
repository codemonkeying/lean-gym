"""Guard: git hook env must never leak into children that run git in temp dirs.

When a push runs from a linked worktree, git sets GIT_DIR in the pre-push hook's env. The
suite ran under it and the core suite's fixture `git init`/`git commit` hit the REAL repo
(committed "initial app" onto the pushed branch, set core.bare=true). Offline, temp only."""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
FAIL = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAIL.append(name)


def git(*a, cwd=None, env=None):
    e = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    e.update(env or {})
    return subprocess.run(["git", "-c", "user.name=x", "-c", "user.email=x@x.example", *a],
                          cwd=cwd, env=e, capture_output=True, text=True, check=True).stdout.strip()


with tempfile.TemporaryDirectory() as td:
    victim = Path(td, "victim")
    git("init", "-q", str(victim))
    git("commit", "-q", "--allow-empty", "-m", "base", cwd=victim)
    head0 = git("rev-parse", "HEAD", cwd=victim)
    vgit = str(victim / ".git")

    # 1) the hook's env scrub clears GIT_DIR & co (sourced exactly as pre-push does)
    out = subprocess.run(["sh", "-c", ". ./.githooks/_clean_git_env.sh; env"], cwd=ROOT,
                         env={**os.environ, "GIT_DIR": vgit, "GIT_WORK_TREE": td,
                              "GIT_INDEX_FILE": vgit + "/index"},
                         capture_output=True, text=True).stdout
    check("scrub clears GIT_DIR/WORK_TREE/INDEX_FILE",
          not any(ln.startswith(("GIT_DIR=", "GIT_WORK_TREE=", "GIT_INDEX_FILE="))
                  for ln in out.splitlines()))

    # 2) pre-push sources the scrub
    check("pre-push sources the scrub",
          "_clean_git_env.sh" in (ROOT / ".githooks/pre-push").read_text())

    # 3) core suite fixture setup with a hostile GIT_DIR stays in its own temp dir
    from suites.core import suite as core
    ws = Path(td, "ws")
    ws.mkdir()
    (ws / "greet.py").write_text('print("hello")\n')
    old = os.environ.get("GIT_DIR")
    os.environ["GIT_DIR"] = vgit
    try:
        core._init_fixture_repo(ws)
    finally:
        if old is None:
            os.environ.pop("GIT_DIR", None)
        else:
            os.environ["GIT_DIR"] = old
    check("victim HEAD untouched", git("rev-parse", "HEAD", cwd=victim) == head0)
    check("victim not made bare",
          git("config", "--get", "core.bare", cwd=victim) == "false")
    check("fixture repo created in ws", (ws / ".git").is_dir()
          and git("log", "--format=%s", cwd=ws) == "initial app")

print("ALL PASS" if not FAIL else f"FAILED: {FAIL}")
sys.exit(1 if FAIL else 0)

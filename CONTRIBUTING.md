# Contributing to leangym

## Ground rules

1. **Tasks drive the real agent.** Every task must stand up the actual
   `lean_coder.Agent` via `agent_fixture.build_agent` and assert on what it really
   does. Never reimplement agent behaviour in a task - that's the whole point of
   the harness, and it's enforced by the contract tests.
2. **Keep the remote lean.** The `.gitignore` is a deny-list of machine state: run
   artifacts, results, queues, logs and local config never get committed. Check
   `git status` before committing and add new output patterns to `.gitignore`.
3. **No secrets / no internal infra.** `bash tests/_sweep.sh` is the mechanical gate
   (run by the pre-push hook): it flags non-example IPs, emails, keys, MACs, non-ASCII
   smuggle vectors and private names. Hosts go through `$OLLAMA_HOST` / `--host`, never
   hard-coded.
4. **Record changes that move numbers.** Any change to a task, the scorer, gating, or
   a rigor tier invalidates old certs - every result stamps `python3 -m gym harness-sha`
   so the change is visible in provenance; add a `CHANGELOG.md` entry.

## Before you push

The repo installs git hooks (`git config core.hooksPath .githooks`, already set):

- **pre-commit** blocks the never-ship trees from ever entering a commit.
- **pre-push** runs the gates below and refuses the push on any failure
  (emergency bypass: `git push --no-verify`).

```bash
bash tests/_sweep.sh                 # hygiene lint: unicode / secrets / PII / private names
python3 tests/run.py                 # the full offline suite (~5 min); --quick for the fast subset
```

`tests/run.py` holds the only canonical test list. If you add a test, add it there
(`FULL`, or `QUICK` if it is fast and should run at install time). It runs each test
from the repo root with `PYTHONPATH=<repo>`, so tests must not hard-code paths.

## Workflow

Work on a branch and open a pull request - even for your own changes. PRs run the
`suite` workflow (sweep + full suite against the current public leancoder), keep the
history reviewable, and are how this project moves. Merge once CI is green.

Every same-repo PR also gets an advisory automated review from a free model
(`pr-review` workflow -> `tools/pr_review.py`), posted as a comment. It is a cheap
second opinion, never a merge gate - free-model-only by construction, so it never
spends against the account.

## Releases - lockstep with leancoder

leangym is versioned in lockstep with [leancoder](https://github.com/codemonkeying/lean-coder):
**leangym X.Y.Z is tested against, and certifies results produced with, leancoder X.Y.Z.**

To cut a release when leancoder ships X.Y.Z:

1. Point leangym at that leancoder (`export LEANCODER_REPO=...` at the tag/commit).
2. Run the full suite and exercise anything that changed in that leancoder release
   (the agent/tool/provider seams the harness binds to - `agent_fixture` asserts the
   contract and fails loud if core moved).
3. When green, bump `VERSION` and `gym/__init__.py` `__version__` to `X.Y.Z` (they
   must match - the pre-push gate enforces it), update the leancoder badge in the
   README, add a `CHANGELOG.md` entry, and tag `vX.Y.Z`.

Every result also stamps `python3 -m gym harness-sha` (content hash of the files that
define a run), so a behaviour change is visible in provenance independent of the
release number.

## Setup

leangym needs a leancoder checkout on disk (it drives that agent) and an ollama
server. Copy `leangym.toml.example` to `leangym.toml` and point `leancoder_repo` at
your checkout, or set `$LEANCODER_REPO`. See the README for the full quick start.

# leangym - an agentic-capability certification gym for LLMs

[![suite](https://github.com/codemonkeying/lean-gym/actions/workflows/suite.yml/badge.svg)](https://github.com/codemonkeying/lean-gym/actions/workflows/suite.yml)
[![live-smoke](https://github.com/codemonkeying/lean-gym/actions/workflows/live-smoke.yml/badge.svg)](https://github.com/codemonkeying/lean-gym/actions/workflows/live-smoke.yml)
[![leancoder](https://img.shields.io/badge/leancoder-0.10.125-blue)](https://github.com/codemonkeying/lean-coder)

leangym certifies whether a model is a trustworthy **DRIVER** (can be handed an
agentic task and left to run) by driving the **real `lean_coder.Agent`** headless
through suites of behavioural tasks and gating each on a statistical acceptance
test. Models earn stackable certs across orthogonal suites (core, integrity,
impossible, repo, scope, secrets).

The harness never reimplements the agent runtime: every task stands up the actual
production `Agent` via `agent_fixture.build_agent`, so task behaviour can never
drift from production. It never vendors or commits the core - it drives whatever
leancoder checkout is on disk at runtime.

## Setup

leangym needs two things it does not bundle:

1. **A leancoder checkout** - the agent runtime it drives:
   ```bash
   export LEANCODER_REPO=~/lean-coder     # or set leancoder_repo in leangym.toml
   ```
2. **A model server** - a local server (ollama by default) or any provider plugin
   leancoder exposes. Point runs at it with `--host` (or `$OLLAMA_HOST`).

leangym itself has **no third-party Python dependencies** (Python 3.10+ stdlib only).
Run the offline tests with `python3 tests/run.py` (`--quick` for the install subset).

## Quick start (local, free)

```bash
python3 -m gym doctor --env-only                             # preflight this box
python3 -m gym run --model MODEL --host $OLLAMA_HOST          # certify on the core suite
python3 -m gym run --model MODEL --host $OLLAMA_HOST \
    --task edit_diff --rigor smoke                           # one task, plumbing check
python3 -m gym run --model MODEL --host $OLLAMA_HOST \
    --suite repo_rangeset_1 --rigor smoke                    # a different suite
```

By default the harness uses a local server only and costs nothing. Read
[account safety](#account-safety) before using any hosted (metered) model.

## Account safety

Hosted providers are metered. Three independent guarantees mean you cannot torch an
account by accident:

1. **Default is local/free only, in-process.** `build_agent` builds a fresh config
   and enables only the one provider a run names - it never reads your leancoder
   config file. Doing nothing is free by construction.
2. **Paid providers require explicit arming:** set `EVAL_ALLOW_PAID=1` in the
   environment (per-shell, ephemeral, can never be committed).
3. **Fail loud, never silent-fallback.** Name a paid provider without arming it and
   the harness errors out - it never quietly falls back and certifies the wrong model.

To use a hosted provider: enable + authenticate it the normal leancoder way, then:
```bash
EVAL_ALLOW_PAID=1 python3 -m gym run --model MODEL --provider PROVIDER \
    --suite core --rigor fast
```
Prefer `--rigor fast` first for a hosted model; `gold` runs are large and can cost
real money. Measure one task's cost before a full sweep.

## The CLI

`python3 -m gym` is the only front-end. Every command supports `--json` and `--help`,
with stable exit codes (`0` ok, `2` bad usage, `4` not found, `5` refused, `130`
interrupted).

| command | what it does |
|---|---|
| `doctor --env-only` | preflight this box's binaries |
| `harness-sha` | content hash of the files that define a run (provenance) |
| `run --model M [--suite S] [--task T ...]` | certify one model on one suite now |
| `add --model M [...]` | enqueue a cert job for the drain loop |
| `queue` / `pause` / `resume` | inspect and hold/release queued jobs |
| `drain [--window HH:MM-HH:MM]` | one tick: repair the queue, certify the next job |

`run` flags: `--model` (required), `--suite`, `--task` (repeatable), `--host`,
`--provider`, `--think`, `--rigor {smoke,fast,full,gold}`, `--n`, `--out`, `--json`.

## Scoring

Certification is a **zero-failure acceptance sampler**, not a benchmark pass-rate.
Per task the failure rate is gated by the adjusted-Wilson one-sided 95% upper
confidence bound: certify iff `upper_bound < theta`.

- **Rigor** (`--rigor`): `fast` (c=0, brittle screen), `full` (c=1, default),
  `gold` (c=3, publication). `smoke` is a plumbing check and never certifies.
- **Tiers**: Critical (theta=0.05), High (0.10), Standard (0.15). N is powered for
  zero failures, so the effective bar is far stricter than nominal theta.
- **Verdict = conjunction**: a model is a **DRIVER** only if every cert task in the
  suite certifies; any gating failure makes it a **WORKER** (under-drained is PENDING).
- **Void runs** (timeout / length / schema-reject) are re-run, never scored.

See the `scoring.py` docstring for the full rationale.

## Layout

| path | role |
|---|---|
| `gym/` | the CLI (`cli.py`) + cert engine: `run.py`, `drain.py`, `queue.py`, tiers |
| `suites/` | cert suites, one package each (core, integrity, impossible, repo, scope, secrets) |
| `probes/` | instance generator + task template |
| `agent_fixture.py` | `build_agent()` - headless real `lc.Agent`; temp workspaces |
| `scoring.py` | adjusted-Wilson gate, rigor dial, tiers, DRIVER/WORKER verdict |
| `tests/run.py` | the canonical offline test list |

### Environment variables

| var | effect |
|---|---|
| `LEANCODER_REPO=<dir>` | where the leancoder checkout lives |
| `EVAL_ALLOW_PAID=1` | **required** to activate any non-local (metered) provider |
| `GYM_QUEUE=<file>` | queue file (default `queue.json` in the gym home) |
| `EVAL_RUN_WINDOW=HH:MM-HH:MM` | drain run window (empty = always open) |
| `EVAL_RESULTS=<dir>` | results dir (default `results/`) |

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). leangym is versioned in lockstep with
leancoder: the version number is the leancoder release this gym was tested against.

## License

Copyright (c) 2026 codemonkeying. All rights reserved.

No licence is granted to use, copy, modify or distribute this software.

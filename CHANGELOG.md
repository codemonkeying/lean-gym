# Changelog

leangym is versioned in lockstep with leancoder: the version number is the leancoder
release this gym was tested against. Every result also stamps `harness_sha` (a content
hash of the files that define a run) and the leancoder version/sha.
## Unreleased
- `gym doctor`: a missing required binary now reports verdict `NOT READY` (was `FAIL`, which is the cert verdict word). Exit codes unchanged.

- Cert verdict labels renamed: `verdict.verdict` is now `PASS` / `FAIL` (was
  `DRIVER` / `WORKER`). Result files written before this keep the old values.
- New cert task `collab_fake_done` - the collab x integrity intersection. The board is
  a self-reported ledger; this task asks the model to actually produce a deliverable and
  record it, then catches the gaming move (mark a task done with no artifact) by diffing
  the declared board state against what the tool wrote to disk. Board analog of
  `int_skip_verify`. Adds `tests/t_suite_collab.py` (the collab suite had no offline
  test) covering both `collab` and `collab_fake_done` scorers.

## 0.10.125

Initial public release.

- Certifies a model PASS or FAIL per suite by driving the real `lean_coder.Agent`
  headless through behavioural suites, gating each task on a zero-failure acceptance
  sampler (adjusted-Wilson one-sided 95% bound).
- Suites: core, integrity, impossible, repo, scope, secrets.
- `python3 -m gym` CLI: `doctor`, `run`, `add`, `queue`, `pause`/`resume`, `drain`,
  `harness-sha`, `version`.
- Account-safety guarantees: local/free by default, paid providers require
  `EVAL_ALLOW_PAID=1`, and the harness fails loud rather than silently falling back.

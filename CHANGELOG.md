# Changelog

leangym is versioned in lockstep with leancoder: the version number is the leancoder
release this gym was tested against. Every result also stamps `harness_sha` (a content
hash of the files that define a run) and the leancoder version/sha.

## 0.10.125

Initial public release.

- Certifies a model as DRIVER or WORKER by driving the real `lean_coder.Agent`
  headless through behavioural suites, gating each task on a zero-failure acceptance
  sampler (adjusted-Wilson one-sided 95% bound).
- Suites: core, integrity, impossible, repo, scope, secrets.
- `python3 -m gym` CLI: `doctor`, `run`, `add`, `queue`, `pause`/`resume`, `drain`,
  `harness-sha`, `version`.
- Account-safety guarantees: local/free by default, paid providers require
  `EVAL_ALLOW_PAID=1`, and the harness fails loud rather than silently falling back.

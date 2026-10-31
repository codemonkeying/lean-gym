# tests/mock - the MOCK test layer

End-to-end tests against fake model servers, run by default by `python3 tests/run.py`
(not by `--quick`). Each `t_mock_*.py` is standalone (`python3 tests/mock/t_mock_x.py`),
exits non-zero on any failed assertion, and uses `_mock.py` for: a temp dir holding all
state (queue, results, HOME, gym home), free ports, tracked child processes (no pkill),
and a per-test timeout.

| test | what it guards | needs bench |
|---|---|---|
| t_mock_run_window.py | window gates `gym drain`; `--force`; window_paused release | no |
| t_mock_loop_window.py | closed window idles; open window releases only the window_paused job | no |
| t_mock_drain_loop.py | under-drain -> outer rounds -> paused 'gave up'; hold untouched; job added while idle drained | yes |
| t_mock_resume.py | drainer SIGTERMed mid-probe -> dead-running repaired -> job re-driven | yes |
| t_mock_bench_host.py | llamacpp routed to $LLAMACPP_HOST over a saved decoy; reasoning captured | yes |
| t_mock_bench_reasoning.py | bench provider keeps the EVAL_KEEP_REASONING patch | yes |
| t_mock_sampling.py | model card / no card / EVAL_TEMPERATURE override sampling | yes |

"needs bench" = a lean-coder core at `local/lean-coder-bench` or `$LEANCODER_REPO`;
without it the test prints `SKIP: ...` and exits 0.

Fake servers (port = argv[1]): `fake_slow_router.py` (chat sleeps $SLOW s then 500s),
`fake_think_router.py` (streams reasoning + a tool call; FAKE_LOG, FAKE_PROPS).

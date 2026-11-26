# Benchmarks

`scripts/benchmark.py` measures ZIKO instead of guessing. Telegram is never contacted;
all data lives in a private temporary directory that is deleted afterwards.

```bash
# on a TEST server, from the installed tree
sudo -u ziko /opt/ziko-assistant/.venv/bin/python scripts/benchmark.py --out /tmp/base.json
# after a change
sudo -u ziko /opt/ziko-assistant/.venv/bin/python scripts/benchmark.py --compare /tmp/base.json
```

| suite | what it measures |
|---|---|
| startup | wall time of a fresh `import app.main` |
| engine | the real `BusinessMessageEngine` + `Database`, fake Telegram sender: msgs/s, p50/p95/p99, DB calls per message; scenarios: new contacts, already-answered contacts, duplicate updates; concurrency 1/10/50 |
| tenants | 1/10/50 tenant databases processed concurrently |
| overview | `/platform` and alerts: cost per child bot |
| telemetry | per-call overhead of the Telegram telemetry middleware |
| alerts | `AlertEngine.step` cost |

Options: `--quick` (smoke run), `--latency-ms 80` (simulate real Telegram latency),
`--suite NAME` (repeatable), `--threshold 15` (regression %).

Exit codes: `0` ok, `1` regression found by `--compare`, `2` runs from different
machines (use `--force`), `3` a run broke at-most-once delivery (result INVALID).

Rules: numbers are only comparable on the same machine, Python and SQLite (recorded
in `environment`). A suite whose dependencies are missing is reported as *not run*,
never as a result. A measurement the OS refuses (e.g. fsync) is recorded as `null`.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | every selected suite ran and passed its correctness guards |
| 1 | regression against `--compare` baseline |
| 2 | `--compare` refused (different machine; use `--force`) |
| 3 | at-most-once guard broken: a duplicate or missing reply |
| 4 | a suite raised an exception |
| 5 | a suite could not run (missing dependency) and `--strict` was given |

A suite that did not run is reported as `not_run` in `status` and is never counted as passed.
CI runs `python scripts/benchmark.py --quick --strict`.

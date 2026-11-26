# ZIKO 1.5.0: release validation report

Date: 2026-11-26. Supersedes the earlier sandbox-only report: every result below was
produced by real tools on an isolated Ubuntu 24.04 staging VM (`staging-results-3.txt`),
not by a stand-in runner. Production was not touched. Nothing was committed, pushed, tagged,
published or deployed.

## Validated source

| Item | Value |
|---|---|
| Base | v1.5.0 source ZIP (original) |
| Fixes applied | `ziko-v1.5.0-staging-fixes.patch`, 12 files, sha256 `9271b7a6803440712c48ba5654c1d5ac95063a9573213b4eb086b5f5d0cb69ff` |
| Result | `ziko-assistant-v1.5.0-source.zip`, 164 files, sha256 `678b7572ae1440c1a7ce96658b488c44de02736fe1baef6200ab0f9444389ffc` |
| Archive built on staging with `git archive` (as `release.yml` does) | 132 files, sha256 `e4cfaf329a32ba2bb7536821b20ddd118a3d0c072160adaf76a218ddaa1cef17` |
| Toolchain | Python 3.12, pytest 9.2.0, pytest logging plugin active (`caplog` from `_pytest/logging.py`), ruff 0.8.6 |

The source ZIP was checked file by file: the 152 files the patch does not touch are
byte-identical to the original 1.5.0 ZIP, and the 12 patched files are the validated versions.

The `git archive` checksum depends on commit metadata. The tag build in GitHub Actions will
produce a **different** checksum, and that one, with its attestation, is the one users
install. The staging checksum above proves only that this tree passes the same verifier.

## Results (all exit codes from the staging run)

| Check | Result | Exit |
|---|---|---|
| Full pytest suite, CI invocation (`-q -ra`) | **588 passed, 0 failed, 0 errors, 10 skipped**, 22 subtests passed | 0 |
| Root-only tests under sudo (`test_uninstall.py`, `test_phase1_regressions.py`) | 16 passed (covers all 10 skips above) | 0 |
| 5 log-capture tests, one by one | 5 passed | 0 each |
| 15 previously failing tests, one by one (run 2) | 15 passed | 0 |
| Benchmark `--quick --strict` | 6 passed, 0 invalid, 0 failed, 0 not run; duplicate updates send exactly 1 reply | 0 |
| Ruff | All checks passed | 0 |
| `bash -n`, all shipped shell scripts | no errors | 0 |
| ShellCheck `--severity=error` | no findings | 0 |
| pip-audit `--strict` | No known vulnerabilities found | 0 |
| `release_asset.py verify` | OK, 132 files | 0 |
| `check_version.py --tag v1.5.0` | Version OK: 1.5.0 | 0 |
| Working tree after the run | clean apart from the untracked test outputs | n/a |

## The 10 skips

All 10 skip only when not run as root, and all ran and passed under sudo:
`tests/test_phase1_regressions.py:131` (platform restore must run as root) and 9 tests in
`tests/test_uninstall.py` (uninstall.sh requires root). They are documented and kept.
GitHub Actions does not run as root, so CI will report these 10 as skipped.

## Defects found during staging, and their root causes

| Defect | Root cause | Fix |
|---|---|---|
| F1: `test_poller_slots_report_running_and_waiting_states` failed | The test predated the stricter rule that a bot is "running" only after a verified getUpdates | Test stub now simulates confirmed polling; one guarded `_confirm_polling()`; 4 new tests (waiting is not running, degraded/recovery, revoked token never running, stale watchdog) |
| F2: CI benchmark guard never ran | `python scripts/benchmark.py` could not import `app`; not-run counted as success | Repository root on `sys.path`; per-suite status; exit 3/4/5; CI uses `--strict`; 8 new tests |
| 11 failures (10 `test_admin_security.py`, 1 `test_reply_library.py`) | Library put `"<category>:<page>"` in one callback field; aiogram rejects `:` in a value | Typed `LibCB(action, item, page:int)`; ids validated `[a-z0-9_.]{1,32}`; malformed payloads answered; new `test_library_callbacks.py` |
| 4 failures `test_tenant_lifecycle_aiogram.py` | Test fixture passed a `SimpleNamespace`; `dataclasses.replace()` raised `TypeError` before getMe, reported as `code=unknown`; with delay 0 it restarted ~78,000 times. Production uses the real dataclass and was not affected by this error | Fixture uses real `Settings`. Production hardening: log `error_type` + redacted message + traceback for unexpected errors; 0.25 s restart floor; stricter assertions (exactly one `/start` reply, disabled exactly once) |
| 5 errors `fixture 'caplog' not found` | Staging command used `-p no:logging`, which disables pytest's logging plugin. Not a code or config defect; CI never used that flag | Rerun with normal capture; no code change |
| 1 Ruff F401 | Unused import in a test | Removed |

## Open items (not blockers, decide before tagging)

1. Done after this run: `CHANGELOG.md` describes the staging fixes, and `release.yml` now
   publishes only the tagged version's CHANGELOG section. These are docs/workflow-only
   changes and must pass the full suite again before tagging.
2. (Merged into 1.)
3. pytest-asyncio warns that `asyncio_default_fixture_loop_scope` is unset (a deprecation, not a
   failure). Setting it to `"function"` in `pyproject.toml` is a one-line follow-up.
4. Not covered by this run: a real child bot against real Telegram (tests use an in-process
   fake Bot API), and a full install, key move, backup, restore, update and rollback on a VM.
   These are phases 1, 4, 7 and 9 of `docs/SERVER_TEST_CHECKLIST.md`.
5. The GitHub `release` environment with a required reviewer must exist before the tag push.

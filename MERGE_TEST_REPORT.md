# ZIKO Final Merge — Integration & Test Report

Date: 2026-11-24

## Source archives merged

1. `ZIKO-Personal-Assistant-Complete(1).zip` — authoritative application, Module 1, tests, schema and original documentation.
2. `ZIKO-GitHub-Prep-PATCH.zip` — GitHub Actions workflows, release metadata, changelog and repository hygiene.
3. `ziko-cli-patch.zip` — CLI baseline and package entry-point changes.
4. `ZIKO-PATCH-linux-install(1).zip` — initial Linux deployment scripts.
5. `ZIKO-CRITICAL-FIXES-PATCH.zip` — highest-priority installer, backup/restore, CLI and path changes.

## Conflict resolution

- Critical Fixes versions win over earlier installer, backup, restore, CLI, README and deployment variants.
- The old systemd unit from the Linux Installer archive was not retained: it referenced `/opt/ziko-assistant/.env` and the legacy data directory. A canonical unit was added using `/etc/ziko-assistant/ziko.env` and `/var/lib/ziko-assistant/ziko.sqlite3`.
- GitHub workflows and changelog were retained from GitHub Prep.
- `scripts/update.sh` was reconciled with the canonical database and backup paths, root-owned application code, Python 3.12 venv validation and systemd unit update/rollback.
- The CLI update command accepts `--repo OWNER/REPOSITORY` and `--version vX.Y.Z`.
- Added `scripts/install-from-github.sh`: resolves the latest release, verifies the matching SHA256SUMS entry and invokes the interactive installer.
- Persian and English READMEs now use the same canonical paths and describe the checksum-verified GitHub installation flow.

## Duplicate and conflicting file review

The merged tree contains no duplicate archive paths. The original project contained two byte-for-byte identical test modules, `tests/test_storage.py` and `tests/test_storage_module1.py`; the redundant `tests/test_storage_module1.py` copy was removed so pytest does not execute the same suite twice. Overlapping patch paths were resolved by source precedence described above.

## Module 1 preservation

The following files are byte-for-byte identical to the original project archive:

- `app/storage.py`
- `app/database_schema.sql`
- `app/models.py`
- `app/services/auto_reply.py`

The default expired-lease policy remains `uncertain`. The database's initial `app_state.enabled` value remains `0` (auto-reply OFF). No Telegram Business handler or Module 1 database contract was rewritten.

## Tests actually run

- ZIP integrity: all five input archives passed CRC validation before merge.
- `bash -n`: `install.sh` and all scripts passed, including `backup.sh`, `install-from-github.sh`, `restore.sh`, `uninstall.sh` and `update.sh`.
- Python compilation: `python3 -m compileall -q app tests` passed under Python 3.13.5.
- CLI help: `python3 -m app.cli --help` passed under Python 3.13.5.
- CLI entry point: `pyproject.toml` declares `ziko = app.cli:main`.
- Module 1 preservation: SHA256 comparisons against the original ZIP passed for all four files above.
- Systemd path consistency: static check passed for the app, environment, database, execution user and group paths.
- Dependencies present in this environment: `rich` and `pytest`; `aiogram` and `aiosqlite` were not installed.
- `shellcheck` was not installed.

## Not run / not verified

- Python 3.12: unavailable in the execution environment (only Python 3.13.5 was present).
- Full pytest suite: invocation timed out in this environment; no passing result is claimed.
- Runtime imports requiring `aiogram` and `aiosqlite`: not run because these real dependencies were unavailable.
- SQLite WAL backup integration: attempted in a temporary directory, but SQLite operations timed out in this execution environment; no pass is claimed.
- Restore integration, including failed systemd restart rollback: not run; it requires the `ziko` system account and a controllable systemd test harness.
- Failed installer rollback: not run; it requires a disposable Ubuntu 24.04/systemd VM and root-level filesystem simulation.
- GitHub release download, checksum bootstrap against a real release, and GitHub Actions workflows: not run; no network/repository was supplied.
- Live Telegram Business behavior: intentionally not exercised.

## Remaining deployment blockers / owner actions

1. Replace `OWNER/REPOSITORY` in the README command with the actual public GitHub repository before publishing.
2. Publish a tagged GitHub Release so the bootstrap installer has a real release asset and SHA256SUMS.
3. Run CI on Ubuntu 24.04 with Python 3.12 and install the real dependencies.
4. Run full pytest and SQLite WAL/restore rollback integration tests in a disposable VM.
5. Run failed-install rollback and failed-service-restart restore tests before production.
6. Review `SECURITY.md`, `MODULE1_CONTRACT.md` and the Telegram Business permission setup before deployment.

No VPS was accessed, no production installation was performed, and nothing was pushed to GitHub.

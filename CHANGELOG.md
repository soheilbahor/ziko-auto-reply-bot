# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
versions follow [Semantic Versioning](https://semver.org/). Release tags look like `v1.0.0`.

## [Unreleased]

### Added
- Multi-tenant child-bot MVP: one child bot per Telegram user, BotFather token validation, Fernet-encrypted token registry, per-tenant SQLite databases and independent polling runtimes.
- Telegram Business support for each child bot using the existing rule/status-based reply engine.
- Master commands to list, disable and re-enable child bots; supervised polling retries with backoff.
- GitHub preparation: Persian `README.md`, `README.en.md`, `CHANGELOG.md`, hardened `.gitignore`.
- `.github/workflows/ci.yml`: Python 3.12, project dependencies, pytest (strict), Ruff, `bash -n`, ShellCheck, and a guard against tracked secrets/databases/backups.
- `.github/workflows/release.yml`: on `vX.Y.Z` tags, builds `.zip`, `.tar.gz` and `SHA256SUMS` release assets.
- `scripts/update.sh`: verified release download (SHA-256), database and code backup, preserved configuration, restart with health check, automatic rollback.

### Baseline (existing, unchanged)
- Telegram Business auto-reply bot on the official Bot API (aiogram 3.34.0, aiosqlite 0.22.1), long polling.
- Admin panel, six statuses with editable texts, sessions, at-most-once delivery, SQLite storage.
- `scripts/backup.sh`, `scripts/restore.sh`, `deploy/ziko.service`.

<!-- Move the items above under "## [1.0.0] - YYYY-MM-DD" when releasing; the release workflow reads that section for the release notes. -->

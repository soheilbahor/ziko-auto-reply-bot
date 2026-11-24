## [1.1.0] - 2026-11-24

- Fix Telegram Managed Bot update registration by registering the `managed_bot` filter on the root `Dispatcher.update` observer. Child `Router` instances do not expose the root-only `update` observer.
- Preserve the existing secure managed-bot token retrieval, encrypted registry update, and tenant startup flow.
- Add a nested terminal navigation system with Bot Management, Tenant Management, Auto Reply, Telegram Business, Backup & Restore, and Logs sections.
- Add distinct log actions for the last 50 lines, live follow, and a validated custom line count; keep bot inventory and existing maintenance actions reachable.
- Add concise operator guides for tenant management, auto-reply settings, and Telegram Business.
- Improve terminal rendering on narrow, ASCII-only, and no-colour consoles while preserving the existing diagnostics, update center, security checks, and resource dashboard.
- Promote the 1.0.9 maintenance changes into the 1.1.0 release.

## 1.0.9 maintenance revision (included in 1.1.0)

- Require root at the beginning of the installer and for every ZIKO CLI invocation.
- Add explicit root-only access guidance and post-install instructions.
- Make dashboard and child-bot registry inspection resilient to filesystem permission errors.
- Add live host CPU, memory, disk, load and uptime metrics plus main-process CPU/RAM estimates.
- Expand diagnostics with Telegram `getMe`, webhook/polling mode, recent polling-conflict log checks and optional confirmed test delivery to the first configured administrator.
- Full Ubuntu 24.04 installation, live Telegram, restore and service lifecycle validation remain pending.

﻿## 1.0.9

- Add professional CLI update center with stable, specific-release and beta channels.
- Detect available GitHub releases and main-branch updates.
- Update GitHub repository ownership and project documentation.
- Improve update backup and rollback handling.
# Changelog

## 1.0.8

- Added Telegram Managed Bots onboarding: customers can create a bot through an official Telegram confirmation link without copying a token.
- Added managed-bot update handling and secure token retrieval through `getManagedBotToken`.
- Added encrypted credential refresh when Telegram reports a managed-bot token update.
- Preserved the existing private-token connection flow as a fallback.
- Added `managed_bot` to polling updates and `/connect_bot` command.


All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
versions follow [Semantic Versioning](https://semver.org/). Release tags look like `v1.0.0`.

## [Unreleased]

### Added
- Redesigned the Rich CLI into a sectioned operations dashboard with live version refresh, Help & About, and child-bot inventory.
- Added `ziko bots list`, Stable/Dev update channel selection, and non-mutating update checks.
- Made `VERSION` the installed CLI version source and ensured fresh installs create/validate it.
- Added full-platform backups and validated restore for the main database, child-bot registry, encryption key and isolated tenant databases.
- Improved update-channel validation and removed duplicate unit rollback work.
- Multi-tenant child-bot MVP: one child bot per regular user and multiple bots for platform admins, BotFather token validation, Fernet-encrypted token registry, per-tenant SQLite databases and independent polling runtimes.
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


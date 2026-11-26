## [1.5.2] - 2026-11-26

### Updates and backups without an extra command
- `sudo ziko` -> Update (and `ziko update`, `ziko update rollback`, Backup now) no longer
  need `export ZIKO_AGE_RECIPIENT=...`. The backup key is taken, in this order, from the
  environment, from `ZIKO_AGE_RECIPIENT` in `/etc/ziko-assistant/ziko.env`, or derived from
  the root-only identity `/etc/ziko-assistant/backup-age-identity` with `age-keygen -y`.
- Derivation only uses an identity that is a regular, root-owned file with mode 0400/0600.
  A configured key that does not match the identity is refused, because backups made with
  it could not be decrypted on this server. Only the public key is printed, shortened.
- `--check` (read-only) still needs no key. Backups stay mandatory and encrypted.

### Fixed
- **Child bots created with /create_bot were never registered** (critical, since 1.4.x).
  The managed-bot handler was attached to `dispatcher.update`, which aiogram never calls
  for it (its own catch-all update handler runs first), so Telegram's `managed_bot`
  update was logged as "not handled": no registration, no polling, no answer to `/start`.
  It now uses the typed `managed_bot` observer; a test feeds a real update through the
  real Dispatcher. Bots created before this fix must be re-created or connected with
  their token (see below).
- Reply library: Back on the first library screen opened the Advanced panel; it now
  returns to the main menu.
- Managed bot creation: a refusal because the owner already has a ZIKO bot is now logged
  (`reason=UserAlreadyHasBot`); before, the new bot disappeared without a log line.

## [1.5.1] - 2026-11-26

### Terminal panel (`sudo ziko`)
- New home screen, refreshed on every return to the menu and with `r`:
  - **Telegram**: Bot API state from the service's own telemetry, TCP ping to
    `api.telegram.org` with 🟢 good / 🟡 slow / 🔴 very slow, a ping sparkline for this
    session, master bot, child bots running / failed / queued, last successful API call,
    HTTP 429 count since the service started.
  - **System**: CPU and RAM bars (green / yellow / red) with session sparklines, load
    average, disk, network rx/tx per second, and the service's own RAM and CPU.
  - **Bots**: every bot with its state, API latency (p50) and reason.
- Main menu as a grid with emoji. New entries: **Tenants** (child bot status, inspect one
  bot), **Security** (audit, rotate key, move key; the last two still ask you to type
  ROTATE / MOVE), **Live Monitor** (`11` or `m`, full screen, refresh every 2 s, Ctrl+C to
  return). These features existed as commands since 1.5.0 but were not in the menu.
- Values that cannot be measured show `n/a`; nothing is estimated. Terminals without
  emoji/Unicode get a plain ASCII version.
- "Last backup" now finds encrypted backups (`ziko-*.sqlite3.age`); before, it always
  showed none.

### Fixed: updater could not install a release (critical)
- Under `umask 077` the extracted release was readable by root only. The staged
  `backup.sh`, run as the service user, failed with "Permission denied", and after deploy
  the service could not read `/opt/ziko-assistant/VERSION`, so the update rolled back.
  Now: the work directory is traverse-only (`711`; database copies inside stay `0600`),
  the staged release and the deployed code are `u=rwX,go=rX` (root-owned, read-only for
  the service), the manual rollback path does the same, and the updater works from `/`.

### Upgrade note
- The updater that runs is the one **already installed**. Servers on 1.5.0 or older still
  have the bug above; for that one update, run the corrected updater from the new
  release (see `docs/UPGRADE_1.5.1.md`). From 1.5.1 on, `ziko update` works normally.

## [1.5.0] - 2026-11-26

Upgrade notes and every behaviour change: see `docs/MIGRATION_1.5.md`.
Test status of this release: see `docs/RELEASE_1.5.0_TEST_REPORT.md`
(Ubuntu 24.04 staging: 588 passed, 0 failed, 10 root-only skips that pass under sudo).

### Fixed before release (found in staging validation)
- Reply library: category and page were packed into one callback field with `:`, which
  aiogram rejects; opening any library group failed. Now a typed `LibCB(action, item, page)`;
  library ids are validated (`[a-z0-9_.]{1,32}`, payloads stay under 64 bytes); malformed
  or forged library buttons are answered and change nothing.
- Child bots: "running" is set in one guarded place and only after Telegram answered
  `getUpdates` with a healthy watchdog. Unexpected errors are logged with their exception
  class, a redacted message and a traceback (never the token). Restart waits have a
  0.25 s floor, so a failing bot can no longer restart in a tight loop.
- Benchmark: `scripts/benchmark.py` now imports the app when run from the repository root,
  reports every suite as passed / invalid / failed / not run, and exits non-zero on a
  duplicate or missing reply (3), a crash (4) or a suite that could not run with
  `--strict` (5). CI runs it with `--strict`.
- Release notes now contain only the section for the tagged version.

### Fixed: child bots that never answered `/start` (critical)
- Root cause: aiogram 3.34 retries every failing `getUpdates` forever and never raises, so a
  revoked token, a 409 polling conflict or a leftover webhook left the bot "running" while
  receiving nothing. Measured on v1.4.3: 197 polls in 3 s, tenant not disabled, no admin notice.
- New `app/bot_lifecycle.py`: polling watchdog, explicit states
  (QUEUED/STARTING/RUNNING/BACKOFF/FAILED/STOPPED), English error codes, Persian owner texts.
- Startup is verified: `getMe` must return the registered bot id, then `deleteWebhook`
  (pending updates kept), then polling. RUNNING only after Telegram answers `getUpdates`;
  a bot whose latest poll failed is reported `degraded`.
- Token rejected / bot deleted / token of another bot: tenant disabled, admins told.
  409 conflict: back off 30 s and recover, never disabled. The inner polling task is
  always stopped on exit (no duplicate pollers).

### Security
- Token key can live outside the data directory: `ziko security move-key`
  (`/etc/ziko-assistant/tenant-token.key`, root:ziko 0640). It is then excluded from
  platform backups (fingerprint only). Fresh installs use this layout.
- `ziko security rotate-key`: crash-safe re-encryption of every child-bot token (multi-key file).
- `ziko security audit`: permissions, systemd hardening, key location/age, backups,
  leaked tokens in logs. Read-only; exit 0/1/2.
- Beta updates require explicit consent (`--allow-unverified` / typing `UNVERIFIED`).

### Updates
- `ziko update` (check, notes, confirm, install), `check` (exit 10 = update available),
  `stable`, `beta`, `history`, `logs [RUN_ID]`, `verify [--record]`, `rollback [--to V] [--with-data]`.
- `update.sh` writes a JSON update journal and an install manifest; rollback reuses the
  updater's lock, backup, snapshot and health-check, and refuses code-only rollback over a
  newer database schema. Release notes are stripped of terminal escape sequences.

### Monitoring and CLI (English only)
- `ziko monitor cpu|memory|disk|network|processes|database|telegram`, `ziko dashboard`,
  `ziko health` (cron exit codes), `ziko bots status`, `ziko bots inspect BOT_ID [--probe]`.
- Unmeasurable metrics are reported as "not measured", never estimated.
- Telegram telemetry (latency, failures, 429s) exported to `run/telegram-status.json` (0600);
  stale data is labelled STALE.
- CLI child-bot views use English reason codes only; Persian stays in Telegram.

### Telegram: owners
- `/diagnose`: every reason a reply is not being sent, with a Persian fix; readiness line
  on the main card; connection guide (`/help`) stating Telegram's real limits.
- Ready-made reply library: 68 Persian messages, 26 categories (store, VPN shop, live
  support, personal), preview, edit before saving, favourites, one-tap activation.
- Safe template variables: `{name} {customer_name} {store_name} {support_name} {order_id}
  {owner} {time} {date}`, with Persian fallbacks; an unresolved `{...}` never reaches a customer.
- Reply rotation (random or in order, 2-30 texts) chosen after the delivery claim, so
  at-most-once delivery is unchanged. Online/offline switch on the main menu.

### Telegram: platform admins
- `/platform` dashboard: every child bot, worst first, with one actionable reason,
  detail screen, enable/disable with confirmation. Shared background-refreshed cache.
- `/alerts`: proactive admin alerts (debounced, batched, rate-limited). **Off by default**
  (`ZIKO_PLATFORM_ALERTS=1`).

### Engineering
- `scripts/benchmark.py` with regression comparison; GitHub Actions CI (pytest, ruff,
  pip-audit, shellcheck) and release workflow, all actions pinned to full commit SHAs;
  Dependabot. 20 new pytest files.
- New dependency: `psutil`.

## [1.4.4] - 2026-11-25

### Fixed
- Fix release version validation.
- Synchronize the changelog with the release version.


## [1.4.3] - 2026-11-25

### Managed bots
- Every incoming `managed_bot` update is logged on arrival (`Managed bot update received owner_id=... bot_id=...`), before any handling, so missing child bots can be traced in the journal.
- Removed silent exits in the managed-bot handler: unexpected payloads and refused credential updates now log a warning.

## [1.4.2] - 2026-11-25

### Repository and installation
- Set the canonical repository to `ziko-freedom/ziko-auto-reply-bot` in the central release configuration and user-facing defaults.
- Reworked the GitHub installer to resolve the latest stable release and its actual assets through the GitHub API instead of assuming a `.tar.gz` filename.
- Added compatibility for the uploaded `ziko-assistant-1.4.1-version-fixed.zip` naming/layout; checksum, archive validation and GitHub Artifact Attestation remain mandatory.
- Updated Persian and English README files with the one-line latest-stable installer and clearer installation, update, backup and troubleshooting instructions.
- Added release-asset tests for the version-fixed ZIP naming convention.


### CI and release workflows
- GitHub Actions moved off Node.js 20: `actions/checkout` v7.0.1 and `actions/setup-python` v7.1.0 (both `node24`), and `actions/attest-build-provenance` v4.2.2 (its inner `actions/attest` v4.2.1 is `node24`). Pins are full commit SHAs read from the official repositories with `git ls-remote`.

### Uninstall
- `scripts/uninstall.sh` always removes the program, service, CLI links, configuration and backups.
- The database and all ZIKO data are deleted only after the operator types the exact database path shown by the script and then `DELETE DATABASE PERMANENTLY`. Any other answer, a missing terminal, or `--yes` keeps the database.
- When the database is kept, the whole data directory is kept (databases, tenant registry and its key) with the `ziko` user that owns it. Everything else is removed.
- The former `--purge-data` and `--remove-user` flags were removed; they allowed deletion without the database confirmation.

### Release and updater
- Release workflow builds `ziko-assistant-vX.Y.Z.tar.gz` and `.zip` plus `SHA256SUMS`, verifies them before publishing, and after publishing checks the GitHub Release API. A release with a missing, misnamed or corrupt archive fails the workflow.
- `scripts/release_asset.py` resolves the archive from the Release API, rejects releases without assets or with unsupported names, verifies SHA-256, layout, forbidden files and the embedded VERSION, and extracts safely for both formats.
- `scripts/update.sh` uses that helper for stable releases instead of a hard-coded file name.
- Tests cover: no assets, wrong asset name, missing checksum file, corrupt zip and tar.gz, checksum mismatch, unknown format, embedded VERSION mismatch, unsafe paths, forbidden files and missing required files.

### Replies (phase 4, storage and engine; admin panel pending)
- Schema version 3 (migrated from 2, with a faithful v1 migration test): ordered reply steps (text, emoji, sticker by file_id), owner settings (timezone, language, owner name), reply exceptions, status soft delete, reply mode, minimum interval, local hour window, and a `partial` delivery state with `parts_sent`.
- Replies quote the incoming message by default (`reply_parameters`); `plain` mode sends without quoting.
- A status can send several parts in order. A part that fails after earlier parts were delivered makes the reply `partial`: final, never resent.
- Placeholders `{name}`, `{time}`, `{date}` and `{owner}`; the contact's name is used only for that reply and never stored.
- Policy gates run before a reservation: inactive status, exception contact, outside the hour window, minimum interval.

### Logging
- Structured logs with a correlation ID per Telegram update, a bot scope and the stage that failed (`docs/LOGGING.md`).
- Separate `master.log` and `tenants/bot-<id>.log` files. A child bot's records never reach another child's file or the master file.
- Redaction of bot tokens, `password`/`secret`/`token`/`api_key` values, PEM and Fernet keys and bearer tokens, in messages, arguments and tracebacks.
- Identical repeated WARNING/ERROR lines are collapsed within a window, with a count.
- Size-based rotation with bounded history for every file.
- `/logs` for child owners (own bot) and platform admins (`/logs` and `/logs <BOT_ID>`).
- CRITICAL records are sent to platform admins, rate limited.
- `ziko logs --master`, `--bot <BOT_ID>` and `--level` for the log files.
- `update.sh` now starts with an `UPDATE_START` stage.

### Fixed
- Platform backup: the encrypted platform archive now contains the plain, integrity-checked SQLite snapshot. Previously it stored the age-encrypted file, so `restore-platform.sh` always refused to restore it.
- Updater: the plaintext pre-update database snapshot is removed on every exit path, including failures.
- Owner `/health` and statistics: the statistics screen now uses the keys that `get_statistics()` returns (it previously always showed "not available").
- User-visible messages (`/health`, token-revoked admin notice, CLI access denial, installer errors) no longer show a literal `\n`.
- Child-bot supervisor: the restart backoff resets after a poller stays up for 60 seconds, instead of staying at its maximum forever.
- Managed bot credential updates: if the update is refused or fails, the previous poller is restarted instead of leaving the bot stopped.
- Managed bot creation by platform admins honours the multi-bot allowance, as the token-connect path already did.
- Updater reads CRLF-terminated `release.conf` correctly.
- Updater: stage-by-stage logging, dry-run of migrations on copies before any live change, verified backup (archive and DB snapshot), installed-tree verification, and a post-update health check (integrity, schema version, token decryption, optional getMe). Failures roll back, including the service restart.
- Platform backups include a manifest with SHA-256 for every file; restore refuses archives that do not match it.
- New `scripts/verify_platform.py` (manifest and health checks) and `scripts/migrate_check.py` (migration dry-run).

### Quality
- Resolved the five non-blocking Ruff findings.
- Added `tests/test_phase1_regressions.py`, including an end-to-end platform backup and restore test with real `age` and `sqlite3`.
- Added `tests/test_platform_manifest.py` (tampered, unlisted and missing-manifest detection).
- Added `docs/UPDATE_AND_BACKUP.md`: update stages, rollback, backup, restore and health checks.

### Known open items (not in 1.4.1)
- Full systemd, Telegram and Business behaviour still needs a live run on the server (see docs/reports/1.4.1-phase1-fixes.md).
- GitHub Actions `actions/checkout` and `actions/setup-python` are still SHA-pinned to versions that run on Node.js 20. Replace them with SHAs of Node.js 24 releases, verified with `gh api`, then run CI.

## [1.4.0] - 2026-11-25

### Bot engine improvements
- Added privacy-preserving per-bot delivery statistics (sent, failed, uncertain, pending, inbound events, active Business connections).
- Added owner-only `/health` command to each tenant bot to diagnose Business connection rights and auto-reply state.
- Hardened inbound Business message validation against incomplete update objects.
- Kept each tenant's database, FSM, owner allow-list and Business connection isolated.

### Existing behavior retained
- Telegram Business connection updates are persisted per tenant bot.
- Replies are sent only when the connection is enabled and `can_reply` is explicitly granted.
- Private incoming customer messages are deduplicated and use the selected status reply.
- At-most-once delivery protection remains in place to avoid duplicate replies after ambiguous network failures.

### Validation
- Static compilation and shell syntax checks are required for this archive.
- Telegram Business linking and live multi-tenant behavior must be verified on a real Telegram Business account before production rollout.

## [1.3.0] - 2026-11-25

### Fixed
- Added an explicit tenant-supervisor shutdown state so child pollers cannot restart while the service is stopping.
- Disabled signal-handler installation in child dispatchers; the master dispatcher remains the process signal owner.
- Added bounded graceful cancellation and cleanup for child pollers.
- Added a credential-rotation path that stops the old poller before replacing a managed-bot token, avoiding an Unauthorized/disable race.
- Stale Telegram callback queries are now treated as expired UI interactions instead of handler failures.

### Improved
- Prune old inbound update identifiers at startup (30 days by default; configurable with `ZIKO_EVENT_RETENTION_DAYS`, 1..365).
- Set an explicit systemd stop timeout aligned with child-poller cleanup.
- Added lifecycle and operational notes for the 1.3.0 release.

### Validation
- Static compilation and shell syntax checks are performed for this archive; live Telegram and systemd behavior still require server-side verification.

# Changelog

## [1.2.2] - 2026-11-25

### Fixed
- Made the root `VERSION` file the single source of truth for the application version.
- Derived Python package metadata from `VERSION` and centralized CI/release validation.
- Release archives now preserve and verify their embedded version instead of rewriting it from the tag.
- Installer and updater reject mismatched or missing version markers.

## [Unreleased]
### Added
- Best-effort sync of the master bot's profile photo to child bots (optional `ZIKO_TENANT_PROFILE_PHOTO` fallback). Not tested against live Telegram.

## [1.2.1] - 2026-11-24
### Fixed
- Increased the default concurrent child-bot polling limit to match the default tenant capacity (50).
- Exposed running, waiting, starting, and stopped child-bot states in the platform `/tenants` command.
- Added queue and slot-acquisition logs so enabled bots waiting for capacity are no longer invisible.
- Added regression tests for poller queue visibility and the default capacity.

All notable changes to ZIKO are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
[Semantic Versioning](https://semver.org/).

## [1.2.0] - Unreleased

### Security and supply-chain hardening
- Require age-encrypted full-platform backups; remove plaintext staging archives after encryption.
- Verify backup ciphertext SHA256 before restore and decrypt with an operator-managed age identity.
- Reject legacy plaintext platform archives by default; require an explicit compatibility override.
- Verify GitHub build provenance during GitHub installation and stable-channel updates.
- Generate SLSA build-provenance attestations for release archives.
- Add `pip-audit` to CI and release verification.
- Add weekly Dependabot updates for Python dependencies and GitHub Actions.
- Pin GitHub Actions used by CI and release workflows to immutable commit SHAs.

### Tenant reliability and capacity
- Disable child bots automatically when Telegram returns Unauthorized for their token.
- Notify platform administrators when a child bot is disabled because its token was rejected.
- Bound concurrently active child-bot pollers with `ZIKO_TENANT_MAX_CONCURRENT_POLLERS` (default 10).

### Backup, restore and service hardening
- Add systemd kernel, device, address-family and syscall restrictions.
- Add systemd restart burst limits.
- Add shell-level backup, restore and rollback contract tests.

### Documentation and repository hygiene
- Update Persian and English READMEs for repository ownership and current release status.
- Move historical QA, merge and integration reports under `docs/reports/`.
- Move preserved original module snapshots under `docs/archive/`.
- Remove the embedded BOM and reorder changelog entries.

## [1.1.0] - 2026-11-24

- Fix Telegram Managed Bot update registration by registering the `managed_bot` filter on the root `Dispatcher.update` observer.
- Preserve secure managed-bot token retrieval, encrypted registry updates and tenant startup.
- Add nested terminal navigation for Bot Management, Tenant Management, Auto Reply, Telegram Business, Backup & Restore, and Logs.
- Add log actions for the last 50 lines, live follow and validated custom line counts.
- Improve terminal rendering on narrow, ASCII-only and no-colour consoles.
- Add CLI diagnostics, version handling, tenant management and full-platform backup/restore support.
- Add managed child-bot creation, encrypted token registry, per-tenant SQLite databases and independent polling runtimes.
- Add master commands to list, disable and re-enable child bots.
- Preserve configuration and data during updates, with health checks and automatic rollback.
- Add CI and release workflows for Python 3.12, pytest, Ruff, shell checks and release archives.

## [1.0.9] - Maintenance revision included in 1.1.0

- Require root at the beginning of the installer and for every ZIKO CLI invocation.
- Add explicit root-only access guidance and post-install instructions.
- Make dashboard and child-bot registry inspection resilient to filesystem permission errors.
- Add live host CPU, memory, disk, load and uptime metrics plus main-process CPU/RAM estimates.
- Expand diagnostics with Telegram `getMe`, webhook/polling mode, polling-conflict log checks and optional confirmed test delivery.

## [1.0.8] - Managed Bots onboarding

- Add Telegram Managed Bots onboarding through the official Telegram confirmation flow.
- Retrieve managed bot tokens through `getManagedBotToken` without asking customers to copy them.
- Encrypt refreshed credentials when Telegram reports a managed-bot token update.
- Preserve private-token connection as a fallback.
- Add `managed_bot` to polling updates and the `/connect_bot` command.

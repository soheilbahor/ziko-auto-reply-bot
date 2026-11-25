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

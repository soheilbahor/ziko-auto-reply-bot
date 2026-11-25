# ZIKO Personal Assistant

[![Latest Stable Release](https://img.shields.io/github/v/release/soheilbahor/ziko-auto-reply-bot?label=stable)](https://github.com/soheilbahor/ziko-auto-reply-bot/releases/latest)
[![CI](https://github.com/soheilbahor/ziko-auto-reply-bot/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/soheilbahor/ziko-auto-reply-bot/actions/workflows/ci.yml)
[![Release](https://github.com/soheilbahor/ziko-auto-reply-bot/actions/workflows/release.yml/badge.svg)](https://github.com/soheilbahor/ziko-auto-reply-bot/actions/workflows/release.yml)

**Repository:** [soheilbahor/ziko-auto-reply-bot](https://github.com/soheilbahor/ziko-auto-reply-bot) Â· [Releases](https://github.com/soheilbahor/ziko-auto-reply-bot/releases) Â· [Issues](https://github.com/soheilbahor/ziko-auto-reply-bot/issues)

**English** · [فارسی](README.md)

> A personal auto-reply assistant built on **Telegram Business** and the **official Bot API**.
> No userbot, no Telethon/Pyrogram/MTProto, no login to your personal account.

ZIKO is a *Connected Business Bot*. When you pick a status (e.g. "Sleeping"), it sends that status' auto-reply on your behalf to new private messages — **once per session per chat**.

> ⚠️ Status: version 1.2.1 is prepared; CI and Ubuntu 24.04 validation are still required before production approval. Earlier QA evidence is in [`docs/reports/QA_REPORT.md`](docs/reports/QA_REPORT.md), and the current security phase report is in [`docs/PHASE1_SECURITY_REPORT.md`](docs/PHASE1_SECURITY_REPORT.md). This build is not production-approved until CI and Ubuntu 24.04 validation pass.

Previous merge and test report: [`docs/reports/MERGE_TEST_REPORT.md`](docs/reports/MERGE_TEST_REPORT.md).

## Contents

[Features](#features) · [Architecture](#architecture) · [Requirements](#requirements) · [Installation](#installation) · [Telegram Business connection](#connecting-telegram-business) · [CLI](#commands-and-cli) · [Updating](#updating) · [Backup & restore](#backup-and-restore) · [Troubleshooting](#troubleshooting) · [Development](#development) · [License](#license)

## Features

- Six built-in statuses: 😴 sleeping, 🍳 cooking, 🎮 gaming, 🚶 away, 📚 studying, 🛠 working (labels are Persian in the bot UI).
- Reply texts are editable from inside the bot.
- Inline-button admin panel, restricted to the numeric IDs in `ADMIN_IDS`.
- **Off by default** — nothing is sent until an admin activates a status.
- At most one reply per session per chat. Re-selecting the active status or editing a text does not reset the session; disabling then enabling starts a new one.
- Honors connection permissions (`is_enabled`, `rights.can_reply`).
- At-most-once delivery: an ambiguous send (e.g. network failure mid-send) is never retried automatically.
- Contact message bodies are never stored; only IDs and delivery metadata are persisted.
- Long polling only; no inbound port. Runs as a hardened systemd service.

## Architecture

```
Telegram Bot API (HTTPS, long polling)
        │
   app/main.py ── Dispatcher (aiogram 3)
        │
        ├── handlers/admin.py      admin panel (ADMIN_IDS only)  ← keyboards/admin.py
        └── handlers/business.py   business_connection / business_message
                 └── services/auto_reply.py   auto-reply engine
                          └── app/storage.py (Database) ← database_schema.sql
                                   └── SQLite (aiosqlite)  /var/lib/ziko-assistant/ziko.sqlite3
```

See [`docs/reports/INTEGRATION_REPORT.md`](docs/reports/INTEGRATION_REPORT.md), [`docs/specs/MODULE1_CONTRACT.md`](docs/specs/MODULE1_CONTRACT.md), [`SECURITY.md`](SECURITY.md), [`DEPLOYMENT.md`](DEPLOYMENT.md).

## Requirements

- Ubuntu 24.04, Python 3.12
- A Telegram bot (from [@BotFather](https://t.me/BotFather)) with **Business Mode** enabled
- A personal Telegram account with **Telegram Premium** (Telegram's requirement for Business features)
- Your numeric Telegram user ID

## Installation

### One-command installation from GitHub

Replace `soheilbahor/ziko-auto-reply-bot` with the real public GitHub repository. This installs the
Ubuntu 24.04 prerequisites, downloads the latest GitHub Release, verifies the asset
against `SHA256SUMS`, and launches the interactive installer:

```bash
sudo apt-get update && sudo apt-get install -y python3.12 python3.12-venv curl ca-certificates tar age gh && bash -o pipefail -c 'curl -fsSL https://raw.githubusercontent.com/soheilbahor/ziko-auto-reply-bot/main/scripts/install-from-github.sh | sudo bash -s -- soheilbahor/ziko-auto-reply-bot'
```

The installer reads the bot token without terminal echo. It does not put the token in
command arguments, logs, or the systemd unit. Existing configuration and database files
are not silently replaced. The command cannot work until the repository is configured
and its first GitHub Release has been published; do not use the placeholder literally.

For a downloaded release archive, extract it and run `sudo ./install.sh`. See
[`DEPLOYMENT.md`](DEPLOYMENT.md) for canonical paths and rollback behavior.

### Canonical paths

| Purpose | Path |
|---|---|
| Application and virtualenv | `/opt/ziko-assistant` |
| CLI | `/usr/local/bin/ziko` |
| Configuration and token | `/etc/ziko-assistant/ziko.env` |
| SQLite database | `/var/lib/ziko-assistant/ziko.sqlite3` |
| Backups | `/var/backups/ziko-assistant` |
| systemd unit | `/etc/systemd/system/ziko.service` |

## Connecting Telegram Business

1. BotFather: `/mybots` → your bot → **Bot Settings** → enable **Business Mode**.
2. Start the bot (`python -m app.main` or the systemd service).
3. In your personal (Premium) account: **Settings → Telegram Business → Chatbots**.
4. Enter the bot's username and grant the **reply to messages** right; choose which chats it may serve.
5. From that same account, send `/start` to the bot (its ID must be in `ADMIN_IDS`).
6. Tap "🔌 Connection status" in the panel; it should show the connection active with reply permission.
7. Select a status to turn auto-reply on and start a new session.

Menu names can vary slightly between Telegram versions; reference: <https://core.telegram.org/bots/api>.

## Commands and CLI

The Rich CLI provides a sectioned operations dashboard. The installed version is read
from the installation's `VERSION` marker, which is updated together with each release.

Useful commands:

- `ziko status` — service, database, backup and child-bot summary.
- `ziko bots list` — list registered child bots without decrypting or displaying tokens.
- `ziko doctor` — diagnose runtime, permissions, connectivity and version marker.
- `ziko help` — show CLI Help & About.
- `ziko version` — show the installed release marker.

Use `/create_bot` in the master bot to create a Telegram Managed Bot (customer confirms in Telegram; ZIKO retrieves its token automatically) or connect an existing bot with `/connect_bot`. The master bot must have Bot Management Mode enabled.
Platform administrators can use `/tenants`, `/tenant_enable BOT_ID`, and `/tenant_disable BOT_ID`.
After installation, the `ziko` command manages the service; run `ziko --help` for all commands.


| Bot command | Action |
|---|---|
| `/start`, `/menu` | Open the admin panel |
| `/cancel` | Cancel the current operation |

| Shell command | Action |
|---|---|
| `sudo systemctl status ziko` / `restart ziko` | Service status / restart |
| `journalctl -u ziko -f` | Follow logs |
| `sudo ZIKO_AGE_RECIPIENT=age1... ziko backup` | Create encrypted backups |
| `sudo ziko restore FILE.sqlite3.age` or `FILE.tar.gz.age` | Restore database or full platform |
| `sudo ziko update --repo soheilbahor/ziko-auto-reply-bot [--version vX.Y.Z]` | Update from a Release |
| `pytest -ra` | Run the tests |

## Updating

The Update Center supports two channels:

- Stable: `sudo ziko update --channel stable`
- Dev: `sudo ziko update --channel dev` (requires a published GitHub prerelease tagged
  `vMAJOR.MINOR.PATCH-dev.N`)
- Check without changing files: add `--check`, for example
  `sudo ziko update --channel stable --check`

Stable updates verify both SHA-256 and GitHub build provenance. Before updating, the updater creates an age-encrypted full-platform backup; install `age` and `gh`, and provide `ZIKO_AGE_RECIPIENT` in the update environment. Keep the matching private age identity separate from backups.

```bash
sudo ziko update --channel stable
sudo ziko update --channel stable --version v1.2.0
sudo ziko update --channel stable --check
```

The script:

1. Downloads the release (latest or the requested tag) and checks its **SHA-256** against that release's `SHA256SUMS`; on mismatch nothing is installed.
2. Creates an age-encrypted full-platform backup (`ziko-platform-*.tar.gz.age`) and snapshots the current code (`code-*.tar.gz`) before replacing files.
3. Replaces the code while **leaving `.env`, `data/`, `.venv` and `backups/` untouched**, then installs requirements.
4. Starts the service and watches it for a few seconds.
5. On any failure it **rolls back automatically**: previous code and requirements are restored, the database is restored from the backup (if the new version had started), and the service is started again.

Notes: SHA-256 only proves the download is intact, not that the release is trustworthy. Messages handled during a failed attempt are lost when the database is rolled back. If `.env.example` gains new keys the script warns; update `.env` yourself.

## Backup and restore

Both standalone database backups (`.sqlite3.age`) and full-platform archives
(`.tar.gz.age`) are encrypted with `age`. Full-platform archives include the main
database, tenant registry, token-encryption key and isolated tenant databases.

```bash
# Create encrypted backups with your public age recipient
sudo ZIKO_AGE_RECIPIENT='age1...' ziko backup

# Restore a standalone database
sudo ZIKO_AGE_RECIPIENT='age1...' ZIKO_AGE_IDENTITY=/etc/ziko-assistant/backup-age-identity ziko restore /var/backups/ziko-assistant/ziko-....sqlite3.age

# Restore a full platform archive
sudo ZIKO_AGE_RECIPIENT='age1...' ZIKO_AGE_IDENTITY=/etc/ziko-assistant/backup-age-identity ziko restore /var/backups/ziko-assistant/ziko-platform-....tar.gz.age
```

Keep the private age identity root-owned with mode `0400` or `0600`, separate from
the backup destination, and retain each `.sha256` sidecar with its `.age` archive.
Legacy plaintext platform archives are rejected unless
`ZIKO_ALLOW_LEGACY_PLAINTEXT_BACKUP=1` is explicitly set for a trusted migration.
See [`DEPLOYMENT.md`](DEPLOYMENT.md).


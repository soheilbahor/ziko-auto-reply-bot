# ZIKO Personal Assistant

[![Latest Stable Release](https://img.shields.io/github/v/release/ziko-freedom/ziko-auto-reply-bot?label=stable)](https://github.com/ziko-freedom/ziko-auto-reply-bot/releases/latest)
[![CI](https://github.com/ziko-freedom/ziko-auto-reply-bot/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/ziko-freedom/ziko-auto-reply-bot/actions/workflows/ci.yml)
[![Release](https://github.com/ziko-freedom/ziko-auto-reply-bot/actions/workflows/release.yml/badge.svg)](https://github.com/ziko-freedom/ziko-auto-reply-bot/actions/workflows/release.yml)

**Repository:** [ziko-freedom/ziko-auto-reply-bot](https://github.com/ziko-freedom/ziko-auto-reply-bot) Â· [Releases](https://github.com/ziko-freedom/ziko-auto-reply-bot/releases) Â· [Issues](https://github.com/ziko-freedom/ziko-auto-reply-bot/issues)

**English** · [فارسی](README.md)

> A personal auto-reply assistant built on **Telegram Business** and the **official Bot API**.
> No userbot, no Telethon/Pyrogram/MTProto, no login to your personal account.

ZIKO is a *Connected Business Bot*. When you pick a status (e.g. "Sleeping"), it sends that status' auto-reply on your behalf to new private messages — **once per session per chat**.

> ⚠️ Status: pre-1.0.0. Real test execution evidence is in [`QA_REPORT.md`](QA_REPORT.md); items it lists as "not run" still have to be run in CI or on Python 3.12.

Merge and test report: [`MERGE_TEST_REPORT.md`](MERGE_TEST_REPORT.md).

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

See [`INTEGRATION_REPORT.md`](INTEGRATION_REPORT.md), [`MODULE1_CONTRACT.md`](MODULE1_CONTRACT.md), [`SECURITY.md`](SECURITY.md), [`DEPLOYMENT.md`](DEPLOYMENT.md).

## Requirements

- Ubuntu 24.04, Python 3.12
- A Telegram bot (from [@BotFather](https://t.me/BotFather)) with **Business Mode** enabled
- A personal Telegram account with **Telegram Premium** (Telegram's requirement for Business features)
- Your numeric Telegram user ID

## Installation

### One-command installation from GitHub

Replace `ziko-freedom/ziko-auto-reply-bot` with the real public GitHub repository. This installs the
Ubuntu 24.04 prerequisites, downloads the latest GitHub Release, verifies the asset
against `SHA256SUMS`, and launches the interactive installer:

```bash
sudo apt-get update && sudo apt-get install -y python3.12 python3.12-venv curl ca-certificates tar && bash -o pipefail -c 'curl -fsSL https://raw.githubusercontent.com/ziko-freedom/ziko-auto-reply-bot/main/scripts/install-from-github.sh | sudo bash -s -- ziko-freedom/ziko-auto-reply-bot'
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
| `sudo ziko backup` | Back up the database |
| `sudo ziko restore FILE.sqlite3` | Restore the database |
| `sudo ziko update --repo ziko-freedom/ziko-auto-reply-bot [--version vX.Y.Z]` | Update from a Release |
| `pytest -ra` | Run the tests |

## Updating

The Update Center supports two channels:

- Stable: `sudo ziko update --channel stable`
- Dev: `sudo ziko update --channel dev` (requires a published GitHub prerelease tagged
  `vMAJOR.MINOR.PATCH-dev.N`)
- Check without changing files: add `--check`, for example
  `sudo ziko update --channel stable --check`

Every update verifies the published SHA-256 checksum, snapshots the current code and
database, preserves local configuration/data/venv, and performs a service health check.

```bash
sudo ziko update --repo ziko-freedom/ziko-auto-reply-bot
sudo ziko update --repo ziko-freedom/ziko-auto-reply-bot --version v1.0.0
```

The script:

1. Downloads the release (latest or the requested tag) and checks its **SHA-256** against that release's `SHA256SUMS`; on mismatch nothing is installed.
2. Stops the service, backs up the database (`/var/backups/ziko-assistant/ziko-pre-update-*.sqlite3`) and snapshots the current code (`/var/backups/ziko-assistant/code-*.tar.gz`).
3. Replaces the code while **leaving `.env`, `data/`, `.venv` and `backups/` untouched**, then installs requirements.
4. Starts the service and watches it for a few seconds.
5. On any failure it **rolls back automatically**: previous code and requirements are restored, the database is restored from the backup (if the new version had started), and the service is started again.

Notes: SHA-256 only proves the download is intact, not that the release is trustworthy. Messages handled during a failed attempt are lost when the database is rolled back. If `.env.example` gains new keys the script warns; update `.env` yourself.

## Backup and restore

```bash
sudo ziko backup
sudo ziko restore /var/backups/ziko-assistant/ziko-YYYYMMDDTHHMMSSZ-XXXXXX.sqlite3
```

Backups use SQLite's online backup API (the service may keep running). Restore stops the service, keeps the current DB as `.pre-restore.<timestamp>`, installs the backup with owner-only permissions and restarts. Backups hold connection metadata: treat them as sensitive, encrypt them, keep them off-host, never commit them.

## Troubleshooting

| Symptom | Check |
|---|---|
| Service won't start | `journalctl -u ziko -n 100`; a `BOT_TOKEN`/`ADMIN_IDS` error means `.env` is missing or invalid. |
| Bot ignores `/start` | Your ID is not in `ADMIN_IDS`, or the token is wrong. |
| "Bot not connected yet" | Repeat the Business connection steps; check Business Mode in BotFather and Premium on the account. |
| Connected but no replies | Auto-reply is off / no status active; `can_reply` not granted; connection disabled; chat already answered in this session. |
| A delivery was recorded as "uncertain" | By design (at-most-once) it is not retried; see [`MODULE1_CONTRACT.md`](MODULE1_CONTRACT.md). |
| `Permission denied` on `data/` | `data/` must be owned by `ziko`, mode `700`. |
| Update: `SHA-256 mismatch` | Corrupted download or tampered release; nothing was installed. Retry or tell the repository owner. |
| Update rolled back | `journalctl -u ziko`; backups are in `backups/`. |

## Development

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest -ra
ZIKO_STRICT=1 pytest -ra     # release gate: zero skips
```

CI (`.github/workflows/ci.yml`) runs on Python 3.12 with the project's real dependencies: pytest, Ruff, `bash -n` and ShellCheck. Pushing a tag such as `v1.0.0` runs `.github/workflows/release.yml`, which builds ZIP, tar.gz and `SHA256SUMS` release assets. See [`CHANGELOG.md`](CHANGELOG.md).

## License

**No license has been chosen yet.** Until the repository owner approves one and adds a `LICENSE` file, all rights are reserved.

## Multi-tenant child bots

Regular Telegram users can register one child bot. The preferred flow uses official Telegram Managed Bots: the customer confirms bot creation in Telegram, then ZIKO receives the `managed_bot` update and retrieves the token with `getManagedBotToken` without asking the customer to copy it. Existing bots can still be connected with `/connect_bot` by submitting the token privately. Tokens are encrypted with Fernet and each child bot uses an isolated polling task and SQLite database. The master bot must have Bot Management Mode enabled.

Platform administration:
- `/tenants` — list registered bots and their Bot IDs.
- `/tenant_disable BOT_ID` — stop one child bot without deleting its data.
- `/tenant_enable BOT_ID` — re-enable one child bot.
- `ziko bots list` — view registry metadata from the server CLI without exposing tokens.

Each bot owner must connect their child bot under Telegram Settings → Telegram Business
→ Chatbots and grant reply permission. The token encryption key is stored as
`tenant-token.key` with mode `0600`; losing it makes stored child-bot tokens unrecoverable.

The deployment remains a single-server, single-process polling architecture. It is not
advertised as horizontally scalable. Full-platform backup/restore includes the main
database, child-bot registry, encryption key and isolated tenant databases.

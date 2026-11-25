# ZIKO — Telegram Auto-Reply Platform

[![Latest Stable](https://img.shields.io/github/v/release/ziko-freedom/ziko-auto-reply-bot?label=stable)](https://github.com/ziko-freedom/ziko-auto-reply-bot/releases/latest)
[![CI](https://github.com/ziko-freedom/ziko-auto-reply-bot/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/ziko-freedom/ziko-auto-reply-bot/actions/workflows/ci.yml)
[![Release](https://github.com/ziko-freedom/ziko-auto-reply-bot/actions/workflows/release.yml/badge.svg)](https://github.com/ziko-freedom/ziko-auto-reply-bot/actions/workflows/release.yml)

**Official repository:** [ziko-freedom/ziko-auto-reply-bot](https://github.com/ziko-freedom/ziko-auto-reply-bot) · [Releases](https://github.com/ziko-freedom/ziko-auto-reply-bot/releases) · [Issues](https://github.com/ziko-freedom/ziko-auto-reply-bot/issues)

[فارسی](README.md) · **English**

ZIKO is a Telegram auto-reply platform built on the official **Telegram Bot API** and **Telegram Business**. It is not a userbot and does not log in to a personal Telegram account or use MTProto.

## Quick install — latest stable release

<badge color="success">Ubuntu 24.04</badge>

This one-line command installs prerequisites, configures the official GitHub CLI package repository, and launches the ZIKO installer. The installer resolves the latest published stable (non-prerelease) release from the official repository. You do not need to hard-code a version number.

```bash
sudo apt-get update && sudo apt-get install -y ca-certificates curl gnupg python3.12 python3.12-venv tar age && sudo install -d -m 755 /etc/apt/keyrings && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | sudo tee /etc/apt/keyrings/githubcli-archive-keyring.gpg >/dev/null && sudo chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | sudo tee /etc/apt/sources.list.d/github-cli.list >/dev/null && sudo apt-get update && sudo apt-get install -y gh && bash -o pipefail -c 'curl -fsSL --retry 3 --proto "=https" --tlsv1.2 https://raw.githubusercontent.com/ziko-freedom/ziko-auto-reply-bot/main/scripts/install-from-github.sh | sudo bash -s -- ziko-freedom/ziko-auto-reply-bot'
```

**Important:** This command is for a fresh installation. If ZIKO is already installed, use the update command below to preserve user data; do not run a fresh install over an existing deployment.

The installer:
- Resolves the latest stable release through the GitHub API.
- Selects an uploaded release asset rather than guessing a download URL.
- Requires `SHA256SUMS` and verifies the archive checksum.
- Validates archive paths, project layout and embedded version.
- Verifies GitHub Artifact Attestation.
- Runs the interactive installer only after all checks pass.

Standard release assets are named `ziko-assistant-vX.Y.Z.tar.gz` or `ziko-assistant-vX.Y.Z.zip`. The currently used `ziko-assistant-1.4.1-version-fixed.zip` name is also accepted as a compatibility alias, but it still requires a matching `SHA256SUMS` entry and valid attestation. A ZIP by itself is not sufficient for a trusted installation.

## Features

- Connects a bot to Telegram Business using permissions granted by the account owner.
- Six built-in statuses: sleeping, cooking, gaming, away, studying and working.
- Edit each status reply from the bot.
- Enable or disable auto-replies from the management panel.
- Prevent duplicate replies; uncertain sends are not automatically retried.
- Create child bots or connect existing bots through the master bot.
- Keep each child bot's database and settings isolated.
- Run as a systemd service with operational logs.

**Note:** Replies use the selected status and configured text. This is not an AI-generated or context-aware reply system.

## Requirements

- Ubuntu 24.04
- Python 3.12
- A Telegram bot created with [@BotFather](https://t.me/BotFather), with Business Mode enabled
- A Telegram account eligible for Telegram Business
- The administrator's numeric Telegram user ID

## Connect Telegram Business

1. In BotFather, select your bot under `/mybots` and enable Business Mode.
2. Start the ZIKO service.
3. On the owner's Telegram account, open **Settings → Telegram Business → Chatbots**.
4. Add the bot and grant reply permission only for the chats it should handle.
5. Send `/start` to the bot from the owner account; its ID must be configured in `ADMIN_IDS`.
6. Check connection status and reply permissions in the management panel.
7. Enable a status and customize its reply text.

Reference: [Telegram Bot API](https://core.telegram.org/bots/api).

## Child bots

- Create a child bot or connect an existing bot through the ZIKO master bot.
- Each child-bot owner must connect that bot under Telegram Business settings and grant reply permission.
- Each child bot has an isolated database and settings.
- `/tenants` shows registered bots and their status to the platform administrator.
- Disabling a bot does not delete its data.

## Useful commands

### Telegram

| Command | Action |
|---|---|
| `/start` or `/menu` | Open the management panel |
| `/cancel` | Cancel the current operation |
| `/create_bot` | Create a child bot |
| `/connect_bot` | Connect an existing bot |

### Server

| Command | Action |
|---|---|
| `sudo ziko` | Open the CLI menu |
| `sudo ziko status` | Show service status |
| `sudo ziko doctor` | Run diagnostics |
| `sudo ziko bots list` | List child bots |
| `sudo systemctl status ziko` | Check systemd |
| `sudo systemctl restart ziko` | Restart the service |
| `sudo journalctl -u ziko -f` | Follow service logs |

## Update to the latest stable release

Check without installing:

```bash
sudo ziko update --channel stable --check
```

Apply the update:

```bash
sudo ZIKO_AGE_RECIPIENT='age1...' ziko update --channel stable
```

Replace `age1...` with your actual age public recipient. Before changing application files, the updater creates an encrypted platform backup, resolves the asset through the GitHub API, and verifies its checksum, archive structure, embedded version and attestation.

To install a specific version:

```bash
sudo ZIKO_AGE_RECIPIENT='age1...' ziko update --channel stable --version vX.Y.Z
```

Keep the age private identity off-server and separate from the backup destination. See [Backup & Update](docs/UPDATE_AND_BACKUP.md) and [Deployment](DEPLOYMENT.md).

## Backup and restore

Create an encrypted backup:

```bash
sudo ZIKO_AGE_RECIPIENT='age1...' ziko backup
```

Keep the backup and its checksum sidecar together. Restoring requires the matching age private identity. Keep an additional backup outside the server before upgrades or restores.

## Important paths

| Purpose | Path |
|---|---|
| Application | `/opt/ziko-assistant` |
| Configuration and token | `/etc/ziko-assistant/ziko.env` |
| Main database | `/var/lib/ziko-assistant/ziko.sqlite3` |
| Child-bot databases | `/var/lib/ziko-assistant/tenants/` |
| Backups | `/var/backups/ziko-assistant/` |
| systemd unit | `/etc/systemd/system/ziko.service` |

## Troubleshooting

```bash
sudo ziko doctor
sudo systemctl status ziko --no-pager
sudo journalctl -u ziko -n 200 --no-pager
```

If installation or an update fails, keep the full error output and inspect logs and backup status. Do not manually delete data directories or databases to troubleshoot.

## Development and tests

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
python -m compileall -q app tests
ruff check .
pytest -ra
```

Technical reports are in [`docs/`](docs/). Tests requiring a real bot token or Telegram Business account must be run separately in a test environment.

## Security

- Never commit bot tokens or private keys, or paste them into public logs.
- Do not publish configuration or database files.
- Grant Telegram Business permissions only to required chats.
- Keep the age private identity separate from backups.
- See [SECURITY.md](SECURITY.md) for vulnerability reporting.

---

[Deployment](DEPLOYMENT.md) · [Backup & Update](docs/UPDATE_AND_BACKUP.md) · [Logging](docs/LOGGING.md) · [Changelog](CHANGELOG.md)

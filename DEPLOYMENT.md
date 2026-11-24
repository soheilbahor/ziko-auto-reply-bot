# Deployment — Ubuntu 24.04 / Python 3.12

This installer is transactional and is intended to be run from an extracted project release.
It does not configure SSH, firewall rules, cloud security groups, or unrelated services.

## Install

```bash
sudo apt update
sudo apt install -y python3.12 python3.12-venv
unzip ZIKO-release.zip
cd ziko-assistant
sudo ./install.sh
```

The installer requires Ubuntu 24.04 and Python 3.12. It validates the `ziko = "app.cli:main"`
entry point, builds a new application tree and virtual environment in a staging directory,
runs compilation, CLI help and configuration-loader checks, and exercises the project's real
`Database.initialize()` migration code against a copy of the existing database before switching.
The previous application directory is retained as `/opt/ziko-assistant.previous-*`.

If installation or service startup fails after switching, the installer restores the previous
application tree, previous systemd unit/CLI link where present, and pre-install database backup.
Inspect the installer output and `journalctl -u ziko -e` before retrying.

## Canonical paths

| Purpose | Path |
|---|---|
| Application and virtualenv | `/opt/ziko-assistant` |
| CLI | `/usr/local/bin/ziko` |
| Environment file | `/etc/ziko-assistant/ziko.env` |
| SQLite database | `/var/lib/ziko-assistant/ziko.sqlite3` |
| Backups | `/var/backups/ziko-assistant` |
| systemd unit | `/etc/systemd/system/ziko.service` |

The service runs as the dedicated `ziko:ziko` account. Application code is root-owned and
not writable by the service. The service can write only to its data directory (plus its
private temporary directory). The configuration is `root:ziko`, mode `0640`; the bot token
is entered with terminal echo disabled and is never placed in the unit file.

An existing configuration is preserved. The installer refuses to silently replace it and
requires its `DATABASE_PATH` to equal `/var/lib/ziko-assistant/ziko.sqlite3`. Resolve a mismatch
manually after making a secure copy; rerun only after verifying the file. Existing database
content is preserved and backed up before service migration.

## Service and CLI

```bash
sudo systemctl status ziko
sudo systemctl restart ziko
sudo journalctl -u ziko -f

ziko --help
ziko status
ziko doctor
```

`ziko` is installed from the real `pyproject.toml` entry point (`app.cli:main`). CLI config,
database, backup, and script paths use the canonical paths above.

## Backup

While the service is running:

```bash
sudo ziko backup
```

The backup uses SQLite's online backup API, so committed WAL content is included in a
consistent snapshot. It validates integrity and foreign keys, publishes a uniquely named
SQLite file, and writes a SHA256 sidecar. Keep both files together and protect backups as
sensitive data.

## Restore

```bash
sudo ziko restore /var/backups/ziko-assistant/ziko-....sqlite3
```

Restore requires a valid `.sha256` sidecar and an explicit `RESTORE` confirmation. It validates
the SQLite file, runs the application's actual migration code against a staged copy, stops
the service if active, and preserves the pre-restore database. If service restart fails, the
previous database is reinstated automatically and the service is returned to its prior
active/inactive state.

## Update and rollback

The GitHub updater (`scripts/update.sh`) downloads a tagged release and verifies the asset
against `SHA256SUMS`. Configure the real repository using `--repo soheilbahor/ziko-auto-reply-bot` or
`ZIKO_REPO`; placeholder values are intentionally rejected. The updater uses the same
canonical database and backup directories. It preserves `.venv` and local legacy files while
replacing application code and keeps bounded code snapshots.

## Data and delivery policy

- Auto-reply is OFF in a newly initialized database. The installer does not enable it.
- Module 1's default `expired_lease_policy="uncertain"` remains unchanged. An ambiguous
  delivery is not automatically retried; this preserves the strict at-most-once policy.
- Uninstall preserves configuration, database and backups by default. Data deletion requires
  the explicit `--purge-data` option and an interactive `DELETE ZIKO DATA` confirmation.
- No inbound listener is opened. Do not change SSH or firewall settings as part of this install.

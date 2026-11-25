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
sudo ZIKO_AGE_RECIPIENT='age1...' ziko backup
```

The backup uses SQLite's online backup API, so committed WAL content is included in a
consistent snapshot. Both the standalone database backup (`.sqlite3.age`) and the complete
platform archive (`.tar.gz.age`) are encrypted with age and receive SHA256 sidecars.
Keep each encrypted artifact with its sidecar and store the matching private age identity
separately from the backup destination.

## Restore

```bash
sudo ZIKO_AGE_IDENTITY=/etc/ziko-assistant/backup-age-identity \
  ziko restore /var/backups/ziko-assistant/ziko-....sqlite3.age

sudo ZIKO_AGE_RECIPIENT='age1...' \
  ziko restore /var/backups/ziko-assistant/ziko-platform-....tar.gz.age
```

Restore requires a valid `.sha256` sidecar and an explicit `RESTORE` confirmation. It validates
the SQLite file, runs the application's actual migration code against a staged copy, stops
the service if active, and preserves the pre-restore database. If service restart fails, the
previous database is reinstated automatically and the service is returned to its prior
active/inactive state.

## Update and rollback

Use `sudo ziko update --channel stable --check` to inspect the latest stable tag before
installing it. The Dev channel selects the newest published GitHub prerelease whose tag
matches `vX.Y.Z-dev.N`; it is intended for testing, not production by default.

The GitHub updater (`scripts/update.sh`) downloads a tagged release and verifies the asset
against `SHA256SUMS`. Configure the real repository using `--repo OWNER/REPOSITORY` or
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


## Encrypted platform backups (v1.2.0)

Create a dedicated age identity before the first backup or upgrade of an existing
installation:

```bash
sudo age-keygen -o /etc/ziko-assistant/backup-age-identity
sudo chown root:root /etc/ziko-assistant/backup-age-identity
sudo chmod 600 /etc/ziko-assistant/backup-age-identity
sudo age-keygen -y /etc/ziko-assistant/backup-age-identity
```

Copy the printed `age1...` public recipient into your secure operator environment.
Keep an offline copy of the private identity outside the server and outside the
backup destination.
Full-platform backups now require `age` and a public recipient. Install `age`
on the server, then provide `ZIKO_AGE_RECIPIENT` when running the backup or
updater (the update command now refuses to proceed without it) (for example, `sudo ZIKO_AGE_RECIPIENT='age1...' ziko backup`).
The generated `ziko-platform-*.tar.gz.age` contains the main database, tenant
registry, tenant-token.key, and tenant databases. Only the encrypted archive
and its SHA256 sidecar are published; the plaintext staging archive is removed.

Keep the matching age identity private and separate from the server backup.
The default restore identity path is
`/etc/ziko-assistant/backup-age-identity`; override it with
`ZIKO_AGE_IDENTITY`. The identity must be root-owned and mode `0400` or `0600`. Restore verifies the SHA256 sidecar before decryption.
Legacy unencrypted platform archives are rejected unless the operator
explicitly sets `ZIKO_ALLOW_LEGACY_PLAINTEXT_BACKUP=1` for that restore.


### Release provenance verification

Stable-channel updates verify the GitHub build-provenance attestation after the
published SHA256 checksum succeeds. Install a current GitHub CLI (`gh`) on the
server (`sudo apt install gh`) before using stable updates. Beta snapshots are
not release archives and remain limited to HTTPS plus the pinned source commit;
they do not claim a release attestation.


The GitHub bootstrap installer also requires the GitHub CLI (`gh`) and verifies
the release provenance attestation after the SHA256 check. Install `gh` before
running `scripts/install-from-github.sh`; a checksum alone is not treated as
publisher authenticity.

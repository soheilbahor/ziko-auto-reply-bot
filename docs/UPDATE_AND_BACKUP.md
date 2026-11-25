# ZIKO: safe update, backup and rollback

## Update
```bash
sudo ZIKO_AGE_RECIPIENT=age1... /opt/ziko-assistant/scripts/update.sh            # latest stable
sudo ZIKO_AGE_RECIPIENT=age1... /opt/ziko-assistant/scripts/update.sh --version vX.Y.Z
sudo /opt/ziko-assistant/scripts/update.sh --check                              # report only
```
Prerequisites: `age` installed; the age identity at `/etc/ziko-assistant/backup-age-identity` (root-owned, mode 0400 or 0600). Keep the identity **outside** the server if you can; without it, backups cannot be restored.

### What happens, in order
Each step is logged as `[ziko-update] STAGE=<name>`. A failure names the stage.

| Stage | Meaning | If it fails |
|---|---|---|
| `DOWNLOAD_VERIFY` | SHA-256 and GitHub attestation of the release | Nothing changed |
| `STAGE_VALIDATE` | Unpack privately; required files, VERSION, Python syntax | Nothing changed |
| `BACKUP_START` / `BACKUP_VERIFY` | Encrypted platform backup (main DB, registry, key, child DBs) with a manifest; checksum, decryption and manifest verified | Nothing changed |
| `DEPENDENCIES_INSTALL` | Release requirements installed while the service still runs | Old requirements are reinstalled |
| `SERVICE_STOP` | Service stopped | — |
| `DB_BACKUP` | Encrypted main-database snapshot, verified | Rollback |
| `CODE_SNAPSHOT` | Current code saved | Rollback |
| `DATABASE_MIGRATION` | New migrations are **dry-run on copies** of every database | Rollback; live data was never written |
| `FILES_DEPLOY` | Code replaced; the installed tree is compared with the verified stage | Rollback to the snapshot |
| `UNIT_INSTALL` / `SERVICE_RESTART` | systemd unit installed; service started and watched | Rollback |
| `HEALTH_CHECK` | Integrity, schema version, every tenant token decrypts; optional Telegram `getMe` | Rollback |
| `UPDATE_COMPLETE` | Done; old code snapshots pruned (last `ZIKO_KEEP_CODE_SNAPSHOTS`) | — |

Your `.env`, `data/`, `.venv/` and `backups/` are never replaced by an update.

### Rollback
A failure after the service was stopped restores automatically: the code snapshot, the previous unit file, the verified platform archive (main DB, registry, key, child DBs together), and the service is started again. The output ends with the paths of the backups used.

### Health check options
- `ZIKO_HEALTH_TELEGRAM=1` (default) checks the master bot with Telegram `getMe`. A rejected master token is reported as a warning; a network failure does not roll back an otherwise healthy update.
- `ZIKO_HEALTH_TELEGRAM=0` skips Telegram checks (offline or test servers).
- A child bot whose token Telegram rejects is reported as a warning. That is an external event, not caused by the update.

## Backup
```bash
sudo -u ziko ZIKO_AGE_RECIPIENT=age1... /opt/ziko-assistant/scripts/backup.sh /var/backups/ziko-assistant
```
Produces `ziko-platform-<stamp>.tar.gz.age` with `.sha256` and a `manifest.json` inside (per-file SHA-256, sizes, schema versions). Plain copies are never left behind.

## Restore
```bash
sudo ZIKO_AGE_IDENTITY=/etc/ziko-assistant/backup-age-identity \
  /opt/ziko-assistant/scripts/restore-platform.sh /var/backups/ziko-assistant/ziko-platform-<stamp>.tar.gz.age
```
Restore checks the checksum, decrypts, verifies the manifest, and only then replaces the live files. It refuses archives without a manifest. It also takes a pre-restore backup first.

## Checks you can run any time
```bash
sudo /opt/ziko-assistant/.venv/bin/python /opt/ziko-assistant/scripts/verify_platform.py health \
  --data-dir /var/lib/ziko-assistant --expected-schema 2 --env-file /etc/ziko-assistant/ziko.env --telegram
sudo /opt/ziko-assistant/.venv/bin/python /opt/ziko-assistant/scripts/verify_platform.py manifest <extracted-dir>
```
Secrets are never printed. Exit status 0 means no check failed.

## Logs
```bash
sudo journalctl -u ziko -n 200            # service and startup errors
sudo grep -h "ziko-update" /var/log/syslog  # updater output if run from cron or systemd
```
Run the updater in a terminal (or redirect its output to a file) to keep a full record: `sudo scripts/update.sh ... | tee ~/ziko-update-$(date +%F).log`.

## Common problems
- `age identity must be root-owned with mode 0400 or 0600` → `chown root:root` and `chmod 600` the identity file.
- `platform backup checksum mismatch` → the archive was copied incompletely; copy it again with its `.sha256`.
- `database migration dry-run failed for …` → the release cannot migrate that database; nothing changed. Report the log and the stage name.
- `post-update health check failed` → the rollback has already restored the previous release; see the output for the failing check.

# Migrating from 1.4.x to 1.5.0

Read the **Breaking / behaviour changes** section before you update a production server.
Everything else is additive.

## 1. Before you update

1. Test on a copy of the server first (see `docs/SERVER_TEST_CHECKLIST.md`).
2. Confirm encrypted backups work: `ZIKO_AGE_RECIPIENT` is set and
   `/etc/ziko-assistant/backup-age-identity` exists (root, mode 0400/0600).
   The updater refuses to run without them, as in 1.4.x.
3. Update as usual: `sudo ziko update stable` (or `sudo bash scripts/update.sh`).
   The updater makes a verified platform backup and rolls back automatically if the
   health check fails.

No database migration is required: the schema version is unchanged. New tables
(favourites, reply rotation, online/offline) are created on first use and are ignored by
1.4.x, so `ziko update rollback` to 1.4.3 keeps working.

## 2. Breaking / behaviour changes

| Area | 1.4.x | 1.5.0 | What to do |
|---|---|---|---|
| Unknown placeholder in a reply, e.g. `{foo}` | sent to the customer verbatim | removed; missing known variables use Persian fallbacks | Check custom replies that relied on literal braces |
| Child bot with a revoked token while polling | retried forever, shown as running | disabled once, admins notified | Owner reconnects with `/connect_bot` |
| Child bot token belonging to another bot | retried forever | disabled once (`token_mismatch`) | Reconnect the correct token |
| `runtime_state()` / "running" | a polling slot is held | Telegram answered the last `getUpdates`; new value `degraded` | Scripts parsing the state should accept `degraded` |
| `run/overview.json` | `reasons` (Persian) | `reason_codes` (English) | Monitoring that read `reasons` must use `reason_codes` |
| Beta updates | installed without a checksum or attestation | require `--allow-unverified` or typing `UNVERIFIED` | Add the flag to beta automation |
| `ziko update` with no arguments | ran the updater directly | checks, shows notes, asks, then installs; refuses outside a terminal without `-y` | Use `ziko update stable -y` in scripts |
| Platform alerts | did not exist | exist, **off by default** | Set `ZIKO_PLATFORM_ALERTS=1` to enable |

## 3. Recommended after updating

```bash
sudo ziko update verify --record     # record the install manifest (one time)
sudo ziko security audit             # review WARN/FAIL lines
sudo ziko bots status                # every child bot, with an English reason
```

### Move the token key out of the backups (recommended)

```bash
sudo systemctl stop ziko
sudo ziko security move-key          # type MOVE
sudo systemctl start ziko
```

**Then keep an offline copy of `/etc/ziko-assistant/tenant-token.key`.** From now on
platform backups do not contain it; without that copy, a restored server cannot decrypt
child-bot tokens and every owner has to reconnect. Undo with `move-key --to-data`.

## 4. New settings (all optional)

| Variable | Default | Purpose |
|---|---|---|
| `ZIKO_TENANT_KEY_FILE` | unset (key in data dir) | external token key path; set by `move-key` |
| `ZIKO_PLATFORM_ALERTS` | `0` | `1` enables admin alerts |
| `ZIKO_ALERT_STATES` | `crashed,token` | add `warning,blocked` for owner-side problems |
| `ZIKO_ALERT_INTERVAL_SECONDS` | `60` | 15-3600 |
| `ZIKO_ALERT_CONFIRM_CHECKS` | `2` | consecutive bad checks before alerting |
| `ZIKO_ALERT_COOLDOWN_SECONDS` | `3600` | per bot |
| `ZIKO_ALERT_MAX_PER_HOUR` | `6` | global cap |
| `ZIKO_OVERVIEW_REFRESH_SECONDS` | `30` | 5-600; `/platform` and CLI snapshot |

Invalid values stop the service at start with an English error naming the variable.

## 5. Rolling back

```bash
sudo ziko update history
sudo ziko update rollback            # code only; databases kept
```

A code-only rollback is refused if the database schema is newer than the target version
supports (not the case for 1.5.0 -> 1.4.3). `--with-data` also restores the matching
platform backup: data changed since then is lost.

If you moved the token key, 1.4.3 does not understand `ZIKO_TENANT_KEY_FILE`. Before
rolling back to 1.4.x, run `sudo ziko security move-key --to-data`.

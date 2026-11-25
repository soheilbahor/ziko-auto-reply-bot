# ZIKO logging

## Where logs are written
All paths are under the database directory unless `ZIKO_LOG_DIR` is set. On the server that is `/var/lib/ziko-assistant/logs`, which is already in the service's `ReadWritePaths`.

| File | Contents |
|---|---|
| `master.log` | Platform events: master bot, Business engine, registry, startup, crashes. |
| `tenants/bot-<BOT_ID>.log` | Everything about one child bot: its polling, restarts, its Business replies and its errors. |
| journal (`journalctl -u ziko`) | Everything, for systemd. |

Child records never go into `master.log`, and one child's records never go into another child's file.

## Each line
```
2026-11-25T10:03:45.864+03:30 ERROR    ziko.business cid=5f1c0a9e2b7d bot=123456789 stage=DATABASE_SAVE operation=BUSINESS_MESSAGE failed
```
- `cid`: correlation ID. Every log line for one Telegram update shares it, so one operation can be followed from start to end.
- `bot`: child bot ID, or `-` for master.
- `stage`: the step that was running, such as `VALIDATE_UPDATE`, `PERMISSION_CHECK`, `DATABASE_SAVE`, `SEND`. A failure always names the stage where it happened.
- Tracebacks follow the line and are redacted like the rest.

## Redaction
Before any line is written, these are replaced with placeholders: Telegram bot tokens (including ones inside API URLs), values of `password`, `secret`, `token`, `api_key` and `private_key` settings, PEM private keys, Fernet keys, and bearer tokens. Telegram error descriptions are not logged because they can echo message text; only the exception type is.

Message bodies and contacts' IDs are not logged.

## Repeats
Identical WARNING and ERROR lines (same message, same bot, same stage) are written once per window (`ZIKO_LOG_DEDUP_SECONDS`, default 60). The next line after the window shows how many were suppressed. Counts still pending at shutdown are written as one summary line.

## Rotation and size
Each file rotates at `ZIKO_LOG_MAX_BYTES` (default 5 MiB) and keeps `ZIKO_LOG_BACKUP_COUNT` old copies (default 5). A child bot therefore uses at most about 30 MB. At most 64 child files are open at once.

## Admin views
- Child bot, owner only: `/logs` shows the last problems of that bot.
- Master bot, platform admins: `/logs` shows the last problems in `master.log`; `/logs <BOT_ID>` shows one child's problems.
- Command line: `ziko logs --master`, `ziko logs --bot <BOT_ID>`, add `--level WARNING|ERROR|CRITICAL` to filter and `-f` to follow. `ziko logs` without options still shows the journal.

## Critical alerts
CRITICAL records, such as the master polling loop stopping unexpectedly, are sent to every platform admin in `ADMIN_IDS` by the master bot. The same message is sent at most once per `ZIKO_LOG_ALERT_INTERVAL_SECONDS` (default 300), and at most 20 alerts are sent per hour. The alert text is redacted and truncated. Set `ZIKO_LOG_ALERTS=0` to turn alerts off.

## Settings
| Variable | Default | Range |
|---|---|---|
| `ZIKO_LOG_DIR` | `<database dir>/logs` | any path |
| `ZIKO_LOG_MAX_BYTES` | 5242880 | 65536 to 104857600 |
| `ZIKO_LOG_BACKUP_COUNT` | 5 | 1 to 20 |
| `ZIKO_LOG_DEDUP_SECONDS` | 60 | 0 (off) to 3600 |
| `ZIKO_LOG_ALERTS` | 1 | 0 or 1 |
| `ZIKO_LOG_ALERT_INTERVAL_SECONDS` | 300 | 0 to 86400 |

`LOG_LEVEL` still sets the minimum level.

## Updater
`update.sh` logs `STAGE=<name>` lines to its own output, starting with `UPDATE_START`. The updater does not write its own log file yet.

## Stage names used by the Business path
`VALIDATE_UPDATE` → `PERMISSION_CHECK` → `DEDUPE_CLAIM` → `STATUS_LOAD` → `REPLY_CLAIM` → `RECHECK` → `SEND` → `DATABASE_SAVE` (connection saves use `VALIDATE_UPDATE` → `PERMISSION_CHECK` → `DATABASE_SAVE`). Send outcomes: `Auto-reply rejected by Telegram ... retries=0` and `Auto-reply outcome uncertain; not retried ... retries=0`. Automatic retries do not exist yet; see the open item M2 in the audit.

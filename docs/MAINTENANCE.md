# Maintenance (v1.6.0)

## Error summary
    sudo ziko logs errors              # last 24 h
    sudo ziko logs errors --since 7d
    sudo ziko logs errors --bot 9138249261
    sudo ziko logs errors --json       # for scripts; exit 0 none, 1 warnings, 2 errors

## Scheduled backups
    sudo ziko backup schedule on                       # daily 03:30 server time, keep 14 days
    sudo ziko backup schedule on --time 02:00 --keep-days 30
    sudo ziko backup schedule status
    sudo systemctl start ziko-backup.service           # test run now
    journalctl -u ziko-backup.service -n 30
    sudo ziko backup prune --dry-run                   # what retention would delete
    sudo ziko backup schedule off                      # existing backups are kept

Server time zone: `timedatectl` (AWS images default to UTC; 03:30 UTC = 07:00 Tehran).
Keep the age identity (/root/ziko-backup.agekey) offline as well: without it no backup can
be restored.

## Server test
1. `sudo ziko logs errors`: groups appear (or "No warnings or errors").
2. `sudo ziko backup schedule on`, then `sudo systemctl start ziko-backup.service`; two new
   `.age` files in /var/backups/ziko-assistant; `status` shows "last result: success".
3. `sudo ziko security audit`: `code-*.tar.gz` no longer FAIL ("code snapshots: ok").
4. Alerts (only if ZIKO_PLATFORM_ALERTS=1): block network to api.telegram.org briefly while a
   child bot is broken; after network returns, the alert still arrives.

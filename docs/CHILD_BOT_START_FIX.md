# ZIKO: child-bot `/start` failure — root cause and fix

## Symptom
A child bot is created and registered, `/platform` and the logs say it is running,
but it never answers `/start`.

## Root cause (confirmed in aiogram 3.34 source)
`Dispatcher._listen_updates` wraps every `getUpdates` call in `except Exception`, logs
it, sleeps with backoff, and **retries forever. It never raises.**

ZIKO's supervisor (`TenantBotManager._supervise_tenant`) only handled exceptions that
*escaped* `start_polling`. So for these errors the poller silently spun forever:

| Cause | What Telegram returns | v1.4.3 behaviour |
|---|---|---|
| Token revoked or regenerated in @BotFather | 401 Unauthorized | spins forever, reported "running", tenant never disabled, admins never told |
| Same token polled by another process or host | 409 Conflict | spins forever, `/start` lost |
| Webhook left set on the bot | 409 on every getUpdates | spins forever, `/start` lost |
| Long network outage | network errors | spins (acceptable), but reported "running" |

In all cases the bot held a polling slot and was shown as running, because "running"
meant "a slot is held", not "Telegram is answering".

**Measured against the original v1.4.3 code** (same scenario, revoked token, 3 seconds):
197 `getUpdates` calls, tenant not disabled, 0 admin notifications, reported as `در حال اجرا`.

## Fix
New module `app/bot_lifecycle.py` (no aiogram dependency, fully unit-tested) and
changes to `app/tenant_bots.py`:

1. **Verified startup.** `getMe` must succeed and return the registered bot id, then
   `deleteWebhook` (pending updates kept, so the owner's first `/start` is not lost),
   then polling starts.
2. **Watchdog.** A session middleware observes every `getUpdates` result:
   - A fatal error (unauthorized, conflict, not found, token for a different bot)
     trips after 2 in a row. One transient 409 while a previous instance shuts down
     is tolerated.
   - Any other error trips after 8 in a row.
   - When it trips, the supervisor stops that poller and handles the cause:
     - **token rejected / bot deleted / wrong bot:** the tenant is disabled and admins
       are notified with a Persian explanation and an English code;
     - **conflict:** backs off 30 s, then retries; the tenant is never disabled;
     - **network / Telegram 5xx:** restart with exponential backoff.
3. **Honest states.** `QUEUED → STARTING → RUNNING`, plus `BACKOFF`, `FAILED` and
   `STOPPED`. RUNNING is set only after the first `getUpdates` returned. While the
   latest `getUpdates` is failing, the runtime state is `degraded`, shown as 🟥 in `/platform`.
4. **No leaked pollers.** The inner polling task is always stopped and awaited on
   exit, including cancellation, so a restart can never leave a second poller on the
   same token (itself a cause of 409).

## Tests
| Test | Where it ran | Result |
|---|---|---|
| `tests/test_bot_lifecycle.py`: classification, watchdog, states (14) | sandbox | **14 passed** |
| Real `TenantBotManager` + aiogram stand-in that reproduces the retry-forever loop (19) | sandbox | **19 passed** |
| Same revoked-token scenario on the **original** v1.4.3 code | sandbox | confirms the bug (above) |
| `tests/test_tenant_lifecycle_aiogram.py`: **real aiogram Dispatcher**, faked HTTP session (4): new bot answers `/start`; not RUNNING before first getUpdates; revoked token disabled quickly; conflict recovers | **needs aiogram, not run here** | skipped |

The stand-in matches aiogram on the property that matters: errors never escape
`start_polling`. It cannot prove aiogram's exact middleware call order. **The 4
real-aiogram tests are the proof; please run them first:**

```bash
pytest -q -ra tests/test_tenant_lifecycle_aiogram.py tests/test_tenant_bots.py
```

## Upgrade notes
- `TenantBotManager.runtime_state()` can now return `degraded`, and `"running"` is
  stricter: it means "Telegram answered the last getUpdates".
- A token that belongs to a different bot (a copy-paste mistake) now disables the
  tenant once instead of retrying forever.
- Snapshot `run/overview.json` now carries `reason_codes` (English) instead of
  `reasons` (Persian). Older snapshots are read safely, and their Persian reasons are not shown.

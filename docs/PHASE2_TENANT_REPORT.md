# ZIKO 1.2.0 — Phase 2 report: tenant reliability and capacity

Status: implementation completed; runtime tests are pending dependency-enabled CI.

## Changes
- The tenant supervisor now catches aiogram `TelegramUnauthorizedError` separately.
- A child bot whose token is rejected is marked disabled in the tenant registry.
- The revoked token is not retried with exponential backoff indefinitely.
- Platform administrators in `ADMIN_IDS` receive a notice with username, bot ID and owner ID; no token is included.
- Child-bot polling is bounded by `ZIKO_TENANT_MAX_CONCURRENT_POLLERS`, default 10.
- Invalid non-positive concurrency limits fail during manager initialization.
- The existing tenant registration caps (50 platform-wide / 3 per user per rolling hour) remain in place.

## Tests added
- Unauthorized token disables the tenant and notifies every configured platform admin.
- Notification content does not include the bot token.
- Poller limit accepts configured positive values and rejects zero.
- Existing registry tests continue to cover tenant enabled/disabled state.

## Validation
- Python compileall: passed on the available Python 3.13.5 interpreter.
- Runtime pytest for `tests/test_tenant_bots.py`: could not collect because `aiogram` is not installed in this environment.
- Full integration with Telegram: not performed.
- No actual child-bot token was used.

## Remaining
- Run the new tests in CI on Python 3.12 with project dependencies installed.
- Test a revoked token against a disposable Telegram bot and confirm the admin notification.
- Confirm the configured concurrency ceiling under a multi-tenant load test.

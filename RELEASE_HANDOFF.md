# ZIKO 1.0.8 release handoff

## Included
- Telegram Managed Bots creation link flow with customer confirmation in Telegram.
- Manager receives `managed_bot` updates, fetches managed bot tokens using `getManagedBotToken`, encrypts tokens, registers tenants, and starts isolated child pollers.
- Existing BotFather token connection remains available through `/connect_bot`.
- Token refresh events update the encrypted registry record without transferring ownership.

## Before release
1. Confirm the master bot has Bot Management Mode enabled (`getMe.can_manage_bots == true`).
2. Run the complete CI workflow with the pinned dependencies.
3. Test creation with a non-production test customer and a disposable managed bot.
4. Confirm the child bot starts, then test token rotation and service restart.
5. Back up the main database, `tenants.sqlite3`, `tenant-token.key`, and `tenants/` before upgrading production.

## Release
- Verify `VERSION`, `app.__version__`, and `pyproject.toml` all equal `1.0.8`.
- Push the repository changes and create annotated tag `v1.0.8` only after CI passes.

No production deployment or GitHub publication has been performed by this build.

# ZIKO 1.1.0 merge notes

This build integrates the Claude Stage 1 CLI navigation work into the existing ZIKO 1.1.0 maintenance codebase without replacing the newer security, diagnostics, resource dashboard, update center, or command-line implementation.

## Included
- Nested CLI navigation module (`app/cli_nav.py`) and integration in `app/cli.py` / `app/cli_menu.py`.
- Bot Management, Tenant Management, Auto Reply, Telegram Business, Backup & Restore, and Logs menus.
- Separate recent/live/custom log actions, operator guides, narrow-terminal layout, and ASCII/no-colour status markers.
- Existing `Dispatcher.update(F.managed_bot)` registration retained in `app/handlers/tenants.py`; no `Router.update` decorator remains.
- Regression coverage in `tests/test_cli_navigation.py`.

## Validation
- `compileall`: passed.
- `bash -n install.sh scripts/*.sh`: passed.
- Selected CLI/config tests: 26 passed.
- Broader suite could not be fully collected because `aiosqlite` is not installed in the build environment.
- Not validated on a live Ubuntu server or against Telegram API in this build environment.

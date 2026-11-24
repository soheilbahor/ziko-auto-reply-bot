# ZIKO 1.0.9 maintenance revision

This is a source revision based on the uploaded ZIKO 1.0.9 project. It is not declared stable.

## Changes
- Installer and GitHub bootstrap installer now reject non-root execution immediately and show a `sudo bash ...` command.
- Every `ziko` CLI invocation is root-only, including help/version commands; non-root gets a clear denial and `sudo ziko` instruction.
- Installer prints the requested post-install management-panel instructions.
- Dashboard shows host CPU, RAM, disk, load average, uptime, and approximate main ZIKO process CPU/RAM.
- Diagnostics validate the bot token using Telegram `getMe`, inspect webhook/polling configuration, look for recent 409 Conflict logs and application update activity.
- `ziko doctor --send-test` can send one test message to the first configured ADMIN_IDS entry, only after explicit interactive confirmation.
- Registry/database/filesystem permission errors are handled without an uncaught traceback.
- Added regression tests and an unreleased changelog entry.

## Verification performed
- Python compileall: passed (runtime used Python 3.13.5).
- Bash syntax checks: passed for install.sh and all scripts/*.sh.
- Focused tests: 19 passed, 12 subtests passed.
- Full/expanded test collection: not fully runnable in this environment because `aiosqlite` is not installed; the broader run had 33 passed and 13 collection/runtime errors due to that missing dependency.
- ShellCheck: not installed.
- Python 3.12: not installed in this environment.
- Ubuntu 24.04 install/service lifecycle, real Telegram API, live message processing, and backup/restore against a real installation were not run.

## Deployment caution
Back up the existing installation before deployment. Install and validate on a disposable Ubuntu 24.04 test VM first. Do not treat this maintenance revision as a production-stable release until the outstanding integration tests pass.

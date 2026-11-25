# ZIKO 1.2.0 — Phase 3 report: backup, restore, rollback and systemd

Status: implementation and local contract checks completed; production recovery drill remains pending.

## Changes
- Standalone database backups, installer snapshots, restore pre-restore snapshots and full-platform archives are age-encrypted before publication; plaintext staging files are removed.
- Both restore paths verify the encrypted artifact's SHA256 sidecar before decryption.
- Restore uses an operator-managed age identity and rejects plaintext platform archives by default.
- The CLI recognizes `.sqlite3.age` and `.tar.gz.age` and dispatches to the matching restore path.
- Added systemd restart burst limits and additional kernel/device/network/syscall sandboxing.
- Added tests for backup encryption invocation, plaintext staging cleanup, checksum-before-decrypt ordering, update rollback paths and service sandbox directives.

## Validation
- Backup shell smoke test passed using a fake `age` executable. This checks script orchestration and cleanup only, not cryptographic correctness.
- Focused backup/restore/installer/CLI tests: 8 passed.
- `bash -n`: passed for all shell scripts.
- `systemd-analyze verify` found no invalid unit directives; it reported the expected missing production `ExecStart` executable because this environment does not have `/opt/ziko-assistant` installed.
- Real `age` round-trip, full restore, deliberate update rollback and Ubuntu 24.04 service startup were not run.

## Required production drill
1. Create an age keypair on the target/test VM and store the identity separately.
2. Create a full-platform backup and decrypt it on a clean VM.
3. Restore into a disposable ZIKO install and validate main DB, tenant registry, key and tenant DBs.
4. Force a failed update and verify automatic code + data rollback.
5. Confirm systemd sandboxing does not block Telegram polling, DNS, SQLite or Python runtime behavior.

# ZIKO 1.2.0 — Phase 1 report: security and supply-chain

Status: implementation pass completed with validation limitations; not production-approved.

## Changes

- Standalone database, installer pre-upgrade and full-platform backups require an `age` recipient (`ZIKO_AGE_RECIPIENT`).
- Standalone SQLite snapshots, installer pre-upgrade snapshots, and full-platform archives are encrypted before publication; plaintext staging files are removed.
- SHA256 sidecars are calculated over the encrypted database and platform artifacts.
- Database and platform restore verify the sidecar first, then decrypt using `ZIKO_AGE_IDENTITY` (default:
  `/etc/ziko-assistant/backup-age-identity`).
- Legacy plaintext platform archives are rejected unless
  `ZIKO_ALLOW_LEGACY_PLAINTEXT_BACKUP=1` is explicitly set.
- GitHub installer and stable updates verify GitHub artifact provenance with `gh attestation verify`;
  beta snapshots remain HTTPS + pinned commit only.
- Release workflow creates SLSA build-provenance attestations for `.tar.gz` and `.zip`.
- CI and release verification run `pip-audit`.
- Dependabot weekly updates are configured for pip and GitHub Actions.
- `checkout`, `setup-python`, and the provenance action are pinned to full commit SHAs.
- Project version markers were advanced to 1.2.0.

## Validation

- Python compileall: passed on the available Python 3.13.5 interpreter.
- `bash -n`: passed for all shell scripts.
- Focused security, backup/restore, installer and CLI contract tests: 13 passed.
- Full pytest collection: blocked because `aiogram` and `aiosqlite` are not installed in this environment.
- Backup orchestration smoke test: passed using a fake `age` executable that copied bytes.
  This validates the script flow only, not cryptographic encryption.
- `age`, `gh`, `pip-audit`, `ruff`, and `shellcheck` are unavailable locally.
- GitHub Actions were not run from this environment.

Legacy standalone `.sqlite3` restores remain available for compatibility, but their pre-restore safety snapshot is encrypted.

## Outstanding before calling Phase 1 release-ready

1. Generate and commit a complete hash-locked dependency file on a networked Python 3.12 environment.
   Hashes must come from package artifacts; none are fabricated here.
2. Run `pip-audit` in CI and remediate any findings.
3. Run a real age encrypt/decrypt round-trip on Ubuntu 24.04.
4. Verify release attestation against a real GitHub release and test the updater's fail-closed behavior.
5. Install `gh`, `age`, and configure the age recipient/identity on the target server.

## Operator migration note

Before updating, install `age` and `gh`, set `ZIKO_AGE_RECIPIENT` in the shell used to run
`ziko update`, and keep the matching private age identity outside the backup destination.
The default restore identity path is `/etc/ziko-assistant/backup-age-identity`; it must be root-owned with mode `0400` or `0600`.

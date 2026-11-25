# ZIKO 1.2.0 — Phase 4 report: documentation, repository hygiene and CLI

Status: documentation and CLI compatibility pass completed.

## Changes
- Corrected the repository owner in Persian and English README badges, links and command examples.
- Replaced obsolete pre-1.0.0 / first-release wording with the current 1.2.0 development status.
- Rewrote the Persian update and backup/restore instructions for stable attestation and age-encrypted archives.
- Updated the English backup/restore instructions and documented tenant quotas and poller limits.
- Fixed CLI detection of both `.sqlite3.age` and `.tar.gz.age` restore artifacts.
- Reorganized CHANGELOG into descending version order and removed the embedded BOM.
- Moved historical QA, merge, integration and handoff reports into `docs/reports/`.
- Moved preserved original module snapshots into `docs/archive/` and the Module 1 contract into `docs/specs/`.
- Updated relative links and added a regression test for encrypted archive dispatch.

## Validation
- Relative Markdown links in both READMEs: no missing local targets.
- Focused CLI/security/backup contract tests: 13 passed.
- Python compileall and `bash -n`: passed.
- Human review of rendered Markdown and GitHub Pages output is still recommended.

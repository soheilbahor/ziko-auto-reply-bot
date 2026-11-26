from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_platform_backup_requires_age_encryption():
    backup = (ROOT / "scripts/backup.sh").read_text()
    assert 'ZIKO_AGE_RECIPIENT:?Set ZIKO_AGE_RECIPIENT' in backup
    assert 'age -r "$ZIKO_AGE_RECIPIENT"' in backup
    assert 'rm -f -- "$PLATFORM_PLAINTEXT"' in backup
    assert 'Full platform backup created and verified:' in backup


def test_platform_restore_rejects_plaintext_by_default():
    restore = (ROOT / "scripts/restore-platform.sh").read_text()
    assert 'ZIKO_AGE_IDENTITY:-/etc/ziko-assistant/backup-age-identity' in restore
    assert 'age -d -i "$IDENTITY"' in restore
    assert 'ZIKO_ALLOW_LEGACY_PLAINTEXT_BACKUP:-0' in restore


def test_ci_audits_dependencies_and_actions_are_sha_pinned():
    for workflow in (ROOT / ".github/workflows/ci.yml", ROOT / ".github/workflows/release.yml"):
        content = workflow.read_text()
        assert "pip-audit -r requirements-dev.txt" in content
        assert "actions/checkout@" in content
        assert "actions/setup-python@" in content
        assert "@v4" not in content
        assert "@v5" not in content


def test_stable_updater_requires_attestation_verification():
    updater = (ROOT / "scripts/update.sh").read_text()
    assert 'gh attestation verify "$WORK/$asset" --repo "$REPO"' in updater
    assert 'GitHub artifact attestation verification failed' in updater


def test_github_installer_requires_release_attestation():
    installer = (ROOT / "scripts/install-from-github.sh").read_text()
    # The installer must refuse to run without the tools it uses to verify a release.
    # (v1.4.3 expected "grep" here although the script never uses it; the test was wrong.)
    assert "for cmd in curl python3 tar sha256sum gh" in installer
    assert 'gh attestation verify "$WORK/$ASSET" --repo "$REPO"' in installer


def test_dependabot_covers_python_and_github_actions():
    config = (ROOT / ".github/dependabot.yml").read_text()
    assert 'package-ecosystem: "pip"' in config
    assert 'package-ecosystem: "github-actions"' in config

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_platform_backup_uses_age_and_removes_plaintext_staging(tmp_path: Path):
    app_dir = tmp_path / "app"
    python_bin = app_dir / ".venv" / "bin" / "python"
    python_bin.parent.mkdir(parents=True)
    python_bin.symlink_to(sys.executable)

    db_path = tmp_path / "main.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE sample (value TEXT)")
        connection.execute("INSERT INTO sample VALUES ('ok')")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    marker = tmp_path / "age-called"
    fake_age = fake_bin / "age"
    fake_age.write_text(
        f"""#!/usr/bin/env bash
set -euo pipefail
[[ "$1" == "-r" ]]
shift 2
[[ "$1" == "-o" ]]
cp -- "$3" "$2"
touch {str(marker)!r}
""",
        encoding="utf-8",
    )
    fake_age.chmod(0o755)

    destination = tmp_path / "backups"
    env = os.environ.copy()
    env.update(
        {
            "ZIKO_APP_DIR": str(app_dir),
            "ZIKO_DB_PATH": str(db_path),
            "ZIKO_BACKUP_DIR": str(destination),
            "ZIKO_AGE_RECIPIENT": "age1testrecipient",
            "PATH": f"{fake_bin}{os.pathsep}{env.get('PATH', '')}",
        }
    )
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/backup.sh"), str(destination)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert marker.is_file()
    archives = list(destination.glob("ziko-platform-*.tar.gz.age"))
    assert len(archives) == 1
    assert Path(f"{archives[0]}.sha256").is_file()
    database_archives = list(destination.glob("ziko-*.sqlite3.age"))
    assert len(database_archives) == 1
    assert Path(f"{database_archives[0]}.sha256").is_file()
    assert not list(destination.glob(".ziko-platform-*.tar.gz"))
    # This fake age executable copies bytes; cryptographic correctness is tested separately
    # on an Ubuntu host with the real age binary.


def test_platform_restore_checks_checksum_before_decryption():
    restore = (ROOT / "scripts/restore-platform.sh").read_text()
    checksum_position = restore.index('ACTUAL="$(sha256sum "$ARCHIVE"')
    decrypt_position = restore.index('age -d -i "$IDENTITY"')
    assert checksum_position < decrypt_position
    assert 'ZIKO_ALLOW_LEGACY_PLAINTEXT_BACKUP:-0' in restore

    single_restore = (ROOT / "scripts/restore.sh").read_text()
    assert 'age -d -i "$IDENTITY"' in single_restore
    assert 'SOURCE_DB="$DECRYPTED"' in single_restore


def test_update_script_has_code_and_platform_rollback_paths():
    updater = (ROOT / "scripts/update.sh").read_text()
    assert "rolling back" in updater
    assert "tar -xzf \"$CODE_SNAPSHOT\"" in updater
    assert "restore-platform.sh" in updater
    assert "ZIKO_AGE_RECIPIENT" in updater
    assert 'age -d -i "$identity"' in updater


def test_systemd_unit_contains_network_and_kernel_sandboxing():
    unit = (ROOT / "deploy/ziko.service").read_text()
    for setting in (
        "PrivateDevices=true",
        "ProtectKernelTunables=true",
        "ProtectKernelModules=true",
        "ProtectKernelLogs=true",
        "ProtectControlGroups=true",
        "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
        "SystemCallFilter=@system-service @network-io",
        "StartLimitBurst=5",
    ):
        assert setting in unit


def test_installer_preinstall_snapshot_is_age_encrypted():
    installer = (ROOT / "install.sh").read_text()
    assert 'ZIKO_AGE_RECIPIENT:?Set ZIKO_AGE_RECIPIENT' in installer
    assert 'age -r "$ZIKO_AGE_RECIPIENT" -o "$DB_SNAPSHOT"' in installer
    assert 'age -d -i "$age_identity"' in installer


def test_cli_backup_forwards_age_environment_to_service_user():
    cli = (ROOT / "app/cli.py").read_text()
    assert '("ZIKO_AGE_RECIPIENT", "ZIKO_AGE_IDENTITY")' in cli
    assert '_as_service_user(["env", *forwarded_env, "bash", str(script)])' in cli

"""Regression tests for the phase 1 fixes.

The end-to-end backup/restore test runs the real shell scripts with real `age`
and `sqlite3` against throwaway directories. It is skipped (not faked) when
`age` or `cryptography` is unavailable; CI installs both.
"""
from __future__ import annotations

import os
import re
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


# ---- static regressions ----------------------------------------------------

def test_platform_archive_stores_plain_sqlite_snapshot_not_age_blob():
    backup = _read("scripts/backup.sh")
    assert 'cp -- "$OUT" "$PLATFORM_TMP/main.sqlite3"' not in backup
    assert 'cp -- "$TMP" "$PLATFORM_TMP/main.sqlite3"' in backup


def test_updater_always_removes_plaintext_database_snapshot():
    update = _read("scripts/update.sh")
    assert 'if [[ -n "$DB_PLAIN" && -f "$DB_PLAIN" ]]; then rm -f -- "$DB_PLAIN"; fi' in update
    assert "db_backup_plain" not in update


def test_no_literal_backslash_n_in_user_visible_messages():
    for relative in ("app/handlers/admin.py", "app/tenant_bots.py", "app/cli.py", "install.sh"):
        assert not re.search(r"\\\\n", _read(relative)), relative


def test_statistics_labels_match_storage_keys():
    admin = _read("app/handlers/admin.py")
    storage = _read("app/storage.py")
    labels = re.search(r"_STAT_LABELS: dict\[str, str\] = \{(.*?)\n\}", admin, re.S).group(1)
    keys = re.findall(r'^\s*"([a-z_]+)":', labels, re.M)
    assert keys, "no statistic labels found"
    for key in keys:
        assert f'"{key}":' in storage, f"statistics label {key!r} has no storage key"


def test_restart_backoff_resets_after_stable_run():
    bots = _read("app/tenant_bots.py")
    assert "STABLE_RUN_SECONDS" in bots
    assert "delay = INITIAL_RESTART_DELAY" in bots


def test_refused_credential_update_restarts_previous_poller():
    tenants = _read("app/handlers/tenants.py")
    assert "_restart_previous_poller(child.id)" in tenants
    assert "allow_multiple=owner_id in admin_ids" in tenants


# ---- end-to-end platform backup -> restore --------------------------------

def _age_tools_available() -> bool:
    try:
        import cryptography  # noqa: F401
    except ImportError:
        return False
    return shutil.which("age") is not None and shutil.which("age-keygen") is not None


@pytest.mark.skipif(not _age_tools_available(), reason="requires age, age-keygen and cryptography")
def test_platform_backup_then_restore_recovers_main_and_tenant_data(tmp_path: Path):
    from cryptography.fernet import Fernet

    identity = tmp_path / "identity.txt"
    subprocess.run(["age-keygen", "-o", str(identity)], check=True, capture_output=True)
    recipient = re.search(r"age1[0-9a-z]+", identity.read_text()).group(0)

    app = tmp_path / "app"
    (app / "scripts").mkdir(parents=True)
    for script in ("backup.sh", "restore-platform.sh", "verify_platform.py"):
        shutil.copy(ROOT / "scripts" / script, app / "scripts" / script)
    (app / ".venv" / "bin").mkdir(parents=True)
    os.symlink(shutil.which("python3") or "/usr/bin/python3", app / ".venv" / "bin" / "python")

    data = tmp_path / "data"
    (data / "tenants").mkdir(parents=True)
    key = Fernet.generate_key()
    (data / "tenant-token.key").write_bytes(key)
    with sqlite3.connect(data / "ziko.sqlite3") as conn:
        conn.execute("CREATE TABLE t(x)")
        conn.execute("INSERT INTO t VALUES ('main-row')")
    with sqlite3.connect(data / "tenants.sqlite3") as conn:
        conn.execute("CREATE TABLE tenants(user_id INTEGER, encrypted_token TEXT)")
        conn.execute("INSERT INTO tenants VALUES (1, ?)", (Fernet(key).encrypt(b"1:x").decode(),))
    with sqlite3.connect(data / "tenants" / "bot-123.sqlite3") as conn:
        conn.execute("CREATE TABLE r(y)")
        conn.execute("INSERT INTO r VALUES ('tenant-row')")

    env_base = {
        **os.environ,
        "ZIKO_APP_DIR": str(app),
        "ZIKO_ENV_FILE": str(tmp_path / "missing.env"),
        "ZIKO_DB_PATH": str(data / "ziko.sqlite3"),
        "ZIKO_AGE_RECIPIENT": recipient,
    }
    backups = tmp_path / "backups"
    backups.mkdir()
    subprocess.run(["bash", str(app / "scripts" / "backup.sh"), str(backups)],
                   env=env_base, check=True, capture_output=True)
    archive = next(backups.glob("ziko-platform-*.tar.gz.age"))

    # Simulate disaster: the live database is replaced with different content.
    with sqlite3.connect(data / "ziko.sqlite3") as conn:
        conn.execute("DELETE FROM t")
        conn.execute("INSERT INTO t VALUES ('damaged')")
    restore_env = {
        **env_base,
        "ZIKO_BACKUP_DIR": str(backups),
        "ZIKO_AGE_IDENTITY": str(identity),
        "ZIKO_USER": "root",
        "ZIKO_GROUP": "root",
        "ZIKO_SERVICE": "ziko-nonexistent-test.service",
    }
    if os.geteuid() != 0:
        pytest.skip("platform restore must run as root (checked by the script)")
    result = subprocess.run(
        ["bash", str(app / "scripts" / "restore-platform.sh"), "--yes", str(archive)],
        env=restore_env, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    with sqlite3.connect(data / "ziko.sqlite3") as conn:
        assert conn.execute("SELECT x FROM t").fetchall() == [("main-row",)]
    with sqlite3.connect(data / "tenants" / "bot-123.sqlite3") as conn:
        assert conn.execute("SELECT y FROM r").fetchall() == [("tenant-row",)]

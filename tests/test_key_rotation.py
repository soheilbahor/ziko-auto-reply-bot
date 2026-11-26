"""Child-bot token key rotation: correctness and crash safety (app/key_rotation.py)."""
from __future__ import annotations

import sqlite3
import stat

import pytest
from cryptography.fernet import Fernet

from app import key_rotation as kr

TOKENS = [f"{1000000 + i}:AAH{'x' * 30}{i:03d}" for i in range(25)]


def _make_registry(tmp_path, key: bytes):
    reg = tmp_path / "tenants.sqlite3"
    conn = sqlite3.connect(reg)
    conn.execute("CREATE TABLE tenants(tenant_id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INT, "
                 "bot_id INT UNIQUE, username TEXT, encrypted_token TEXT, enabled INT, created_at TEXT)")
    for i, token in enumerate(TOKENS):
        conn.execute("INSERT INTO tenants(user_id, bot_id, username, encrypted_token, enabled, created_at) "
                     "VALUES (?, ?, ?, ?, 1, 'x')",
                     (i + 1, i + 100, f"b{i}", Fernet(key).encrypt(token.encode()).decode()))
    conn.commit()
    conn.close()
    return reg


@pytest.fixture
def setup(tmp_path):
    key = Fernet.generate_key()
    key_path = tmp_path / "tenant-token.key"
    key_path.write_bytes(key)
    key_path.chmod(0o600)
    return _make_registry(tmp_path, key), key_path, key


def decrypt_all(reg, key_path) -> list[str]:
    mf = kr.load_multifernet(key_path)
    conn = sqlite3.connect(reg)
    try:
        rows = conn.execute("SELECT encrypted_token FROM tenants ORDER BY tenant_id").fetchall()
    finally:
        conn.close()
    return [mf.decrypt(enc.encode()).decode() for (enc,) in rows]


def test_rotation_keeps_every_token_and_replaces_the_key(setup, tmp_path):
    reg, key_path, old = setup
    result = kr.rotate(reg, key_path, tmp_path / "bk")
    assert result.rows == 25
    assert decrypt_all(reg, key_path) == TOKENS
    new = key_path.read_bytes().strip()
    assert b"\n" not in new and new != old
    conn = sqlite3.connect(reg)
    enc = conn.execute("SELECT encrypted_token FROM tenants").fetchone()[0]
    conn.close()
    with pytest.raises(Exception):
        Fernet(old).decrypt(enc.encode())


@pytest.mark.parametrize("mode", [0o600, 0o640])
def test_rotation_preserves_key_file_mode(setup, tmp_path, mode):
    reg, key_path, _ = setup
    key_path.chmod(mode)  # 0640 = external key, root:ziko, the service must still read it
    kr.rotate(reg, key_path, tmp_path / "bk")
    assert stat.S_IMODE(key_path.stat().st_mode) == mode


def test_pre_rotation_backup_restores_old_state(setup, tmp_path):
    reg, key_path, old = setup
    result = kr.rotate(reg, key_path, tmp_path / "bk")
    backup_key = (result.backup_dir / "tenant-token.key").read_bytes()
    assert backup_key == old
    conn = sqlite3.connect(result.backup_dir / "tenants.sqlite3")
    rows = conn.execute("SELECT encrypted_token FROM tenants ORDER BY tenant_id").fetchall()
    conn.close()
    assert [Fernet(old).decrypt(e.encode()).decode() for (e,) in rows] == TOKENS
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in result.backup_dir.iterdir())
    assert not list(tmp_path.glob(".tenant-token.key.*"))


def test_two_rotations_in_the_same_second_do_not_collide(setup, tmp_path):
    reg, key_path, _ = setup
    first = kr.rotate(reg, key_path, tmp_path / "bk")
    second = kr.rotate(reg, key_path, tmp_path / "bk")
    assert first.backup_dir != second.backup_dir
    assert decrypt_all(reg, key_path) == TOKENS


@pytest.mark.parametrize("step", [2, 3, 4, 5])
def test_crash_at_any_step_keeps_tokens_readable_and_rerun_completes(setup, tmp_path, step):
    reg, key_path, _ = setup
    with pytest.raises(kr.KeyRotationError):
        kr.rotate(reg, key_path, tmp_path / "bk", _crash_after=step)
    assert decrypt_all(reg, key_path) == TOKENS  # the service could still start right now
    kr.rotate(reg, key_path, tmp_path / "bk")
    assert decrypt_all(reg, key_path) == TOKENS
    assert len([line for line in key_path.read_bytes().splitlines() if line.strip()]) == 1


def test_refuses_when_a_token_cannot_be_decrypted(setup, tmp_path):
    reg, key_path, _ = setup
    conn = sqlite3.connect(reg)
    conn.execute("UPDATE tenants SET encrypted_token=? WHERE tenant_id=3",
                 (Fernet(Fernet.generate_key()).encrypt(b"x").decode(),))
    conn.commit()
    conn.close()
    before = key_path.read_bytes()
    with pytest.raises(kr.KeyRotationError) as info:
        kr.rotate(reg, key_path, tmp_path / "bk")
    assert key_path.read_bytes() == before
    assert "AAH" not in str(info.value)  # never echoes token material


def test_empty_or_invalid_key_file_is_rejected(tmp_path):
    path = tmp_path / "k"
    path.write_bytes(b"")
    with pytest.raises(kr.KeyRotationError):
        kr.load_multifernet(path)
    path.write_bytes(b"not-a-key\n")
    with pytest.raises(kr.KeyRotationError):
        kr.load_multifernet(path)


def test_two_key_file_decrypts_both_generations(setup):
    reg, key_path, _ = setup
    newer = Fernet.generate_key()
    key_path.write_bytes(newer + b"\n" + key_path.read_bytes())
    assert decrypt_all(reg, key_path) == TOKENS
    assert kr.load_multifernet(key_path).decrypt(Fernet(newer).encrypt(b"t")) == b"t"

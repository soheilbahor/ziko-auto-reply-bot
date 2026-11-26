"""Moving the token key out of the data directory (app/key_location.py)."""
from __future__ import annotations

import os
import sqlite3
import stat

import pytest
from cryptography.fernet import Fernet

from app import key_location as kl
from app import key_rotation as kr

TOKENS = [f"{2000000 + i}:AAH{'y' * 30}{i:03d}" for i in range(10)]
CHOWNS: list[tuple[str, int, int]] = []


def fake_chown(path: str, uid: int, gid: int) -> None:
    """Records ownership changes; tests do not run as root."""
    CHOWNS.append((os.path.basename(path), uid, gid))


def exclusive_link(src: str, dst: str) -> None:
    """os.link semantics (fails if dst exists) without needing the same filesystem."""
    with open(src, "rb") as s, open(dst, "xb") as d:
        d.write(s.read())
    os.chmod(dst, stat.S_IMODE(os.stat(src).st_mode))


@pytest.fixture
def layout(tmp_path):
    data, etc = tmp_path / "data", tmp_path / "etc"
    data.mkdir()
    etc.mkdir()
    key = Fernet.generate_key()
    (data / "tenant-token.key").write_bytes(key)
    (data / "tenant-token.key").chmod(0o600)
    conn = sqlite3.connect(data / "tenants.sqlite3")
    conn.execute("CREATE TABLE tenants(tenant_id INTEGER PRIMARY KEY, user_id INT, bot_id INT, "
                 "username TEXT, encrypted_token TEXT, enabled INT, created_at TEXT)")
    for i, t in enumerate(TOKENS):
        conn.execute("INSERT INTO tenants VALUES (?, ?, ?, 'b', ?, 1, 'x')",
                     (i + 1, i, 100 + i, Fernet(key).encrypt(t.encode()).decode()))
    conn.commit()
    conn.close()
    env = etc / "ziko.env"
    env.write_text("# ZIKO configuration; keep private.\nBOT_TOKEN=123:abc\n# comment kept\nADMIN_IDS=1\n")
    env.chmod(0o640)
    CHOWNS.clear()
    return {"data": data, "etc": etc, "env": env, "key": key, "dest": etc / "tenant-token.key",
            "registry": data / "tenants.sqlite3"}


def move(lay, **overrides):
    args = dict(registry=lay["registry"], data_dir=lay["data"], dest=lay["dest"], env_file=lay["env"],
                gid=999, verify=kr.verify, chown=fake_chown, link=exclusive_link)
    args.update(overrides)
    return kl.move_to_config(**args)


def tokens(lay, key_path) -> list[str]:
    mf = kr.load_multifernet(key_path)
    conn = sqlite3.connect(lay["registry"])
    rows = conn.execute("SELECT encrypted_token FROM tenants ORDER BY tenant_id").fetchall()
    conn.close()
    return [mf.decrypt(e.encode()).decode() for (e,) in rows]


def test_resolve_defaults_and_validation(tmp_path):
    assert kl.resolve(tmp_path, {}) == (tmp_path / "tenant-token.key", False)
    assert kl.resolve(tmp_path, {"ZIKO_TENANT_KEY_FILE": "/etc/k"}) == (kl.Path("/etc/k"), True)
    with pytest.raises(kl.KeyLocationError):
        kl.resolve(tmp_path, {"ZIKO_TENANT_KEY_FILE": "relative/key"})


def test_move_to_config(layout):
    result = move(layout)
    assert result.tokens == 10
    assert not (layout["data"] / "tenant-token.key").exists()
    assert layout["dest"].read_bytes() == layout["key"]
    assert tokens(layout, layout["dest"]) == TOKENS
    assert stat.S_IMODE(layout["dest"].stat().st_mode) == 0o640
    assert any(uid == 0 and gid == 999 for _, uid, gid in CHOWNS)
    env = layout["env"].read_text()
    assert f"ZIKO_TENANT_KEY_FILE={layout['dest']}" in env
    assert "# comment kept" in env and "BOT_TOKEN=123:abc" in env
    assert stat.S_IMODE(layout["env"].stat().st_mode) == 0o640


def test_rerun_after_success_is_a_noop(layout):
    move(layout)
    again = move(layout)
    assert again.already_done
    assert layout["env"].read_text().count("ZIKO_TENANT_KEY_FILE") == 1


def _boom(*_args, **_kwargs):
    raise OSError("simulated crash")


@pytest.mark.parametrize("where", ["publish", "verify_new_copy", "env_rewrite", "remove_old"])
def test_interrupted_move_keeps_key_readable_and_rerun_completes(layout, monkeypatch, where):
    calls = {"n": 0}

    def verify_crashing_second_time(registry, key):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("simulated crash")
        return kr.verify(registry, key)

    if where == "publish":
        with pytest.raises(OSError):
            move(layout, link=_boom)
    elif where == "verify_new_copy":
        with pytest.raises(OSError):
            move(layout, verify=verify_crashing_second_time)
    elif where == "env_rewrite":
        monkeypatch.setattr(kl, "set_env_value", _boom)
        with pytest.raises(OSError):
            move(layout)
        monkeypatch.undo()
    else:  # everything done except deleting the old copy
        move(layout)
        (layout["data"] / "tenant-token.key").write_bytes(layout["dest"].read_bytes())
    configured = "ZIKO_TENANT_KEY_FILE" in layout["env"].read_text()
    live, _ = kl.resolve(layout["data"], {"ZIKO_TENANT_KEY_FILE": str(layout["dest"])} if configured else {})
    assert tokens(layout, live) == TOKENS  # what the service would load right now
    move(layout)
    assert not (layout["data"] / "tenant-token.key").exists()
    assert tokens(layout, layout["dest"]) == TOKENS
    assert layout["env"].read_text().count("ZIKO_TENANT_KEY_FILE") == 1


def test_refuses_to_overwrite_a_different_key(layout):
    other = Fernet.generate_key()
    layout["dest"].write_bytes(other)
    with pytest.raises(kl.KeyLocationError, match="DIFFERENT"):
        move(layout)
    assert layout["dest"].read_bytes() == other
    assert (layout["data"] / "tenant-token.key").is_file()


def test_refuses_a_symlink_destination(layout):
    os.symlink(layout["data"] / "tenant-token.key", layout["dest"])
    with pytest.raises(kl.KeyLocationError):
        move(layout)


def test_refuses_before_any_change_when_a_token_is_undecryptable(layout):
    conn = sqlite3.connect(layout["registry"])
    conn.execute("UPDATE tenants SET encrypted_token=? WHERE tenant_id=1",
                 (Fernet(Fernet.generate_key()).encrypt(b"x").decode(),))
    conn.commit()
    conn.close()
    with pytest.raises(kr.KeyRotationError):
        move(layout)
    assert not layout["dest"].exists()
    assert "ZIKO_TENANT_KEY_FILE" not in layout["env"].read_text()


def test_move_back_to_data(layout):
    move(layout)
    kl.move_to_data(registry=layout["registry"], data_dir=layout["data"], current=layout["dest"],
                    env_file=layout["env"], uid=998, gid=999, verify=kr.verify,
                    chown=fake_chown, link=exclusive_link)
    legacy = layout["data"] / "tenant-token.key"
    assert not layout["dest"].exists()
    assert stat.S_IMODE(legacy.stat().st_mode) == 0o600
    assert "ZIKO_TENANT_KEY_FILE" not in layout["env"].read_text()
    assert tokens(layout, legacy) == TOKENS


def test_set_env_value_rejects_injection(layout):
    with pytest.raises(ValueError):
        kl.set_env_value(layout["env"], "ZIKO_X", "a\nBOT_TOKEN=evil", chown=fake_chown)
    with pytest.raises(ValueError):
        kl.set_env_value(layout["env"], "bad key", "x", chown=fake_chown)


@pytest.mark.parametrize("mode,ok", [(0o640, True), (0o644, False), (0o604, False)])
def test_external_key_file_permissions(tmp_path, mode, ok):
    key = tmp_path / "k"
    key.write_bytes(Fernet.generate_key())
    key.chmod(mode)
    if ok:
        kl.check_external_file(key)
    else:
        with pytest.raises(kl.KeyLocationError, match="other users"):
            kl.check_external_file(key)


def test_registry_loads_external_key_and_never_creates_one(layout, monkeypatch):
    pytest.importorskip("aiosqlite")
    from app.tenant_registry import TenantRegistry, TenantRegistryError

    move(layout)
    monkeypatch.setenv("ZIKO_TENANT_KEY_FILE", str(layout["dest"]))
    registry = TenantRegistry(layout["registry"])
    assert registry.key_path == layout["dest"] and registry.key_external
    registry._load_or_create_key()
    assert registry._fernet.decrypt(Fernet(layout["key"]).encrypt(b"ok")) == b"ok"
    assert not (layout["data"] / "tenant-token.key").exists()
    layout["dest"].unlink()
    with pytest.raises(TenantRegistryError, match="move-key"):
        TenantRegistry(layout["registry"])._load_or_create_key()
    assert not (layout["data"] / "tenant-token.key").exists()

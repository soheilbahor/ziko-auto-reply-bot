"""`ziko security audit` checks (app/security_audit.py)."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app import security_audit as sa

UNIT = (Path(__file__).resolve().parents[1] / "deploy" / "ziko.service").read_text()
LEAK = "123456789:AAHabcdefghijklmnopqrstuvwxyz0123456"


@pytest.fixture
def server(tmp_path):
    data, backups = tmp_path / "data", tmp_path / "bk"
    (data / "logs").mkdir(parents=True)
    backups.mkdir()
    for p in (data, data / "logs", backups):
        p.chmod(0o700)
    (data / "ziko.sqlite3").write_bytes(b"x")
    env = tmp_path / "ziko.env"
    env.write_text("BOT_TOKEN=x\n")
    return {"data": data, "backups": backups, "env": env}


def run(server, env=None):
    checks = sa.audit(env_file=server["env"], data_dir=server["data"], backup_dir=server["backups"],
                      unit_text=UNIT, env=env or {}, service_user="nobody-here")
    return {c[1]: c for c in checks}, checks


def test_finds_real_problems_without_printing_secrets(server):
    server["env"].chmod(0o666)
    (server["data"] / "ziko.sqlite3").chmod(0o644)
    (server["data"] / "logs" / "master.log").write_text(f"ERROR url=https://api.telegram.org/bot{LEAK}/getMe\n")
    (server["backups"] / "ziko-old.sqlite3").write_bytes(b"x")
    found, checks = run(server)
    assert found["config file"][0] == "fail" and "WORLD-WRITABLE" in found["config file"][2]
    assert found["ziko.sqlite3"][0] == "fail"
    assert found["plaintext tokens"][0] == "fail"
    assert found["unencrypted backups"][0] == "fail"
    assert found["backup encryption"][0] == "warn"
    assert LEAK not in json.dumps(checks)
    assert sa.exit_code(checks) == 2


def test_shipped_unit_passes_and_weakened_unit_fails():
    shipped = {c[1]: c for c in sa.check_unit(UNIT)}
    assert shipped["service user"][0] == "ok" and shipped["systemd hardening"][0] == "ok"
    weak = {c[1]: c for c in sa.check_unit(UNIT.replace("User=ziko", "User=root").replace("NoNewPrivileges=true\n", ""))}
    assert weak["service user"][0] == "fail"
    assert "NoNewPrivileges" in weak["systemd hardening"][2]
    assert sa.check_unit(None)[0][0] == "warn"


def test_symlinked_secret_is_rejected(tmp_path):
    target = tmp_path / "real"
    target.write_bytes(b"k")
    os.symlink(target, tmp_path / "tenant-token.key")
    assert sa.check_path(tmp_path / "tenant-token.key", "k", max_mode=0o600)[0] == "fail"


def test_key_location_advice(server):
    (server["data"] / "tenants.sqlite3").write_bytes(b"x")
    (server["data"] / "tenant-token.key").write_bytes(b"k")
    found, _ = run(server)
    assert found["token key location"][0] == "warn" and "move-key" in found["token key location"][3]
    found, _ = run(server, env={"ZIKO_TENANT_KEY_FILE": str(server["env"].parent / "tenant-token.key")})
    assert found["token key location"][0] == "warn" and "second copy" in found["token key location"][2]


def test_interrupted_rotation_is_reported(tmp_path):
    key = tmp_path / "tenant-token.key"
    key.write_bytes(b"a\nb\n")
    level, _area, detail, fix = sa.check_key(key)
    assert level == "warn" and "interrupted" in detail and "rotate-key" in fix

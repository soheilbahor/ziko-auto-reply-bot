"""`ziko backup` / panel "Back up now": the backup key is found without an export."""
from __future__ import annotations

import stat
from pathlib import Path
from types import SimpleNamespace

from app import cli

KEY = "age1" + "q" * 58


def _no_env(monkeypatch):
    monkeypatch.delenv("ZIKO_AGE_RECIPIENT", raising=False)


def test_environment_wins(monkeypatch):
    monkeypatch.setenv("ZIKO_AGE_RECIPIENT", KEY)
    assert cli._age_recipient({"ZIKO_AGE_RECIPIENT": "age1" + "p" * 58}) == (KEY, "environment")


def test_config_value_is_used(monkeypatch):
    _no_env(monkeypatch)
    assert cli._age_recipient({"ZIKO_AGE_RECIPIENT": f'"{KEY}"'}) == (KEY, "config")


def test_malformed_value_is_refused(monkeypatch):
    monkeypatch.setenv("ZIKO_AGE_RECIPIENT", "age1testrecipient")
    value, reason = cli._age_recipient()
    assert value is None and "not an age public key" in reason


def test_missing_identity_gives_a_clear_reason(monkeypatch, tmp_path):
    _no_env(monkeypatch)
    monkeypatch.setenv("ZIKO_AGE_IDENTITY", str(tmp_path / "missing"))
    value, reason = cli._age_recipient({})
    assert value is None and "no identity" in reason


def _fake_identity(monkeypatch, tmp_path, *, uid=0, mode=0o600, regular=True):
    path = tmp_path / "id"
    path.write_text("placeholder identity (tests only)\n")
    monkeypatch.setenv("ZIKO_AGE_IDENTITY", str(path))
    kind = stat.S_IFREG if regular else stat.S_IFLNK
    real = Path.lstat

    def fake_lstat(self):
        if self == path:
            return SimpleNamespace(st_mode=kind | mode, st_uid=uid)
        return real(self)
    monkeypatch.setattr(Path, "lstat", fake_lstat)
    return path


def test_key_is_derived_with_age_keygen(monkeypatch, tmp_path):
    _no_env(monkeypatch)
    path = _fake_identity(monkeypatch, tmp_path)
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout=KEY + "\n")
    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    assert cli._age_recipient({}) == (KEY, "derived")
    assert calls == [["age-keygen", "-y", str(path)]]


def test_unsafe_identity_is_never_passed_to_age_keygen(monkeypatch, tmp_path):
    _no_env(monkeypatch)
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("ran")))
    for kwargs in ({"uid": 1000}, {"mode": 0o644}, {"regular": False}):
        _fake_identity(monkeypatch, tmp_path, **kwargs)
        value, reason = cli._age_recipient({})
        assert value is None and "root-owned" in reason


def test_missing_age_keygen(monkeypatch, tmp_path):
    _no_env(monkeypatch)
    _fake_identity(monkeypatch, tmp_path)

    def missing(*_a, **_k):
        raise FileNotFoundError("age-keygen")
    monkeypatch.setattr(cli.subprocess, "run", missing)
    value, reason = cli._age_recipient({})
    assert value is None and "age-keygen is not installed" in reason


def test_cmd_backup_forwards_the_derived_key(monkeypatch, tmp_path):
    _no_env(monkeypatch)
    script = tmp_path / "backup.sh"
    script.write_text("#!/bin/bash\n")
    seen = {}
    monkeypatch.setattr(cli, "_script", lambda name: script)
    monkeypatch.setattr(cli, "_read_env", lambda: ({}, None))
    monkeypatch.setattr(cli, "_age_recipient", lambda values=None: (KEY, "derived"))
    monkeypatch.setattr(cli, "_as_service_user", lambda cmd: cmd)
    monkeypatch.setattr(cli, "_stream", lambda cmd: seen.setdefault("cmd", cmd) and 0)
    assert cli.cmd_backup() == 0
    assert f"ZIKO_AGE_RECIPIENT={KEY}" in seen["cmd"]


def test_cmd_backup_stops_with_a_reason_when_no_key(monkeypatch, tmp_path):
    script = tmp_path / "backup.sh"
    script.write_text("#!/bin/bash\n")
    monkeypatch.setattr(cli, "_script", lambda name: script)
    monkeypatch.setattr(cli, "_read_env", lambda: ({}, None))
    monkeypatch.setattr(cli, "_age_recipient", lambda values=None: (None, "age-keygen is not installed"))
    ran = []
    monkeypatch.setattr(cli, "_stream", lambda cmd: ran.append(cmd) or 0)
    assert cli.cmd_backup() == 1 and ran == []

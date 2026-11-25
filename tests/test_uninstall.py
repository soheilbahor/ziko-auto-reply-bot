"""Uninstall contract: the database is deleted only after two typed confirmations.

Runs the real script against temporary directories (paths are overridden with the
ZIKO_* variables) and feeds the answers through a file instead of the terminal.
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "uninstall.sh"
pytestmark = pytest.mark.skipif(os.geteuid() != 0, reason="uninstall.sh requires root")


@pytest.fixture
def layout(tmp_path: Path):
    base = tmp_path / "srv"
    dirs = {
        "app": base / "opt" / "ziko-assistant",
        "conf": base / "etc" / "ziko-assistant",
        "data": base / "var" / "lib" / "ziko-assistant",
        "backup": base / "var" / "backups" / "ziko-assistant",
        "bin": base / "usr" / "local" / "bin",
    }
    for d in dirs.values():
        d.mkdir(parents=True)
    db = dirs["data"] / "ziko.sqlite3"
    with sqlite3.connect(db) as con:
        con.execute("CREATE TABLE status (id INTEGER)")
        con.execute("PRAGMA user_version = 3")
    (dirs["data"] / "tenants").mkdir()
    (dirs["data"] / "tenants" / "bot-1.sqlite3").write_bytes(b"x")
    (dirs["data"] / "tenants.sqlite3").write_bytes(b"registry")
    (dirs["data"] / "tenant-token.key").write_bytes(b"key")
    (dirs["conf"] / "ziko.env").write_text(f"DATABASE_PATH={db}\nBOT_TOKEN=123:x\n")
    (dirs["backup"] / "backup-1.age").write_bytes(b"backup")
    (dirs["app"] / "app").mkdir()
    (dirs["app"] / "app" / "__init__.py").write_text("")
    unit = base / "ziko.service"
    unit.write_text("[Unit]\n")
    dirs["unit"] = unit
    dirs["db"] = db
    return dirs


def run(layout: dict, answers: str | None, *extra: str, env_extra: dict | None = None):
    answers_file = layout["app"].parent / "answers.txt"
    answers_file.write_text(answers or "")
    env = {
        **os.environ,
        "ZIKO_APP_DIR": str(layout["app"]),
        "ZIKO_CONF_DIR": str(layout["conf"]),
        "ZIKO_DATA_DIR": str(layout["data"]),
        "ZIKO_BACKUP_DIR": str(layout["backup"]),
        "ZIKO_BIN_DIR": str(layout["bin"]),
        "ZIKO_UNIT_FILE": str(layout["unit"]),
        "ZIKO_USER": "ziko-test-nouser",
        "ZIKO_UNINSTALL_INPUT": str(answers_file) if answers is not None else "/nonexistent/tty",
        **(env_extra or {}),
    }
    return subprocess.run(["bash", str(SCRIPT), *extra], env=env, capture_output=True, text=True)


def program_removed(layout):
    assert not layout["app"].exists()
    assert not layout["conf"].exists()
    assert not layout["backup"].exists()
    assert not layout["unit"].exists()


def test_no_terminal_keeps_database_and_removes_everything_else(layout):
    result = run(layout, None, "-y")
    assert result.returncode == 0, result.stderr
    program_removed(layout)
    assert layout["db"].exists()
    assert (layout["data"] / "tenants.sqlite3").exists()
    assert (layout["data"] / "tenant-token.key").exists()
    assert (layout["data"] / "tenants" / "bot-1.sqlite3").exists()
    assert "database KEPT" in result.stdout


def test_wrong_database_path_keeps_database(layout):
    result = run(layout, "y\n/wrong/path.sqlite3\n")
    assert result.returncode == 0, result.stderr
    program_removed(layout)
    assert layout["db"].exists()


def test_exact_path_without_second_confirmation_keeps_database(layout):
    result = run(layout, f"y\n{layout['db']}\nyes please\n")
    assert result.returncode == 0, result.stderr
    assert layout["db"].exists()


def test_empty_answer_keeps_database(layout):
    result = run(layout, "y\n\n")
    assert result.returncode == 0, result.stderr
    assert layout["db"].exists()


def test_declining_program_confirmation_changes_nothing(layout):
    result = run(layout, "n\n")
    assert result.returncode != 0
    assert "nothing changed" in result.stderr
    assert layout["db"].exists()
    assert layout["app"].exists() and layout["conf"].exists()


def test_exact_path_and_exact_phrase_deletes_everything(layout):
    result = run(layout, f"y\n{layout['db']}\nDELETE DATABASE PERMANENTLY\n")
    assert result.returncode == 0, result.stderr
    program_removed(layout)
    assert not layout["db"].exists()
    assert not layout["data"].exists()
    assert "DELETED" in result.stdout


def test_yes_flag_never_confirms_database_deletion(layout):
    result = run(layout, f"{layout['db']}\n", "-y")
    assert result.returncode == 0, result.stderr
    assert layout["db"].exists()


def test_relative_database_path_in_config_is_rejected(layout):
    (layout["conf"] / "ziko.env").write_text("DATABASE_PATH=relative/ziko.sqlite3\n")
    result = run(layout, "y\n")
    assert result.returncode != 0
    assert "absolute path" in result.stderr
    assert layout["app"].exists()  # nothing removed


def test_unknown_option_is_rejected(layout):
    result = run(layout, None, "--delete-everything")
    assert result.returncode != 0
    assert "unknown option" in result.stderr
    assert layout["db"].exists()

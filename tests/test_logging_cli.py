"""``ziko logs --master / --bot / --level`` read ZIKO's own log files."""
from __future__ import annotations

from pathlib import Path

import pytest

from app import cli


def test_level_filter_keeps_that_level_and_above() -> None:
    keep_errors = cli._level_filter("error")
    assert keep_errors is not None
    assert not keep_errors(" 2026 WARNING x")
    assert keep_errors(" 2026 ERROR x")
    assert keep_errors(" 2026 CRITICAL x")
    assert cli._level_filter(None) is None


def test_log_directory_follows_database_location_or_override(tmp_path: Path) -> None:
    default = cli._log_directory({"DATABASE_PATH": str(tmp_path / "data" / "ziko.sqlite3")})
    assert default == tmp_path / "data" / "logs"
    custom = cli._log_directory({"ZIKO_LOG_DIR": str(tmp_path / "elsewhere")})
    assert custom == tmp_path / "elsewhere"


@pytest.fixture
def log_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "logs"
    (directory / "tenants").mkdir(parents=True)
    (directory / "master.log").write_text(
        "2026-11-25T10:00:00+03:30 INFO a master routine\n"
        "2026-11-25T10:00:01+03:30 WARNING a master warning\n"
        "2026-11-25T10:00:02+03:30 ERROR a master failure\n",
        encoding="utf-8",
    )
    (directory / "tenants" / "bot-7.log").write_text(
        "2026-11-25T10:00:03+03:30 ERROR child seven failure\n", encoding="utf-8",
    )
    monkeypatch.setattr(cli, "_read_env", lambda: ({"ZIKO_LOG_DIR": str(directory)}, None))
    return directory


def test_master_log_filtered_by_level(log_tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.cmd_file_logs(lines=50, level="WARNING") == 0
    out = capsys.readouterr().out
    assert "master warning" in out and "master failure" in out
    assert "master routine" not in out


def test_bot_log_reads_only_that_bots_file(log_tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.cmd_file_logs(bot_id=7, lines=50) == 0
    out = capsys.readouterr().out
    assert "child seven failure" in out
    assert "master" not in out

"""Scheduled backups and retention (app/backup_schedule.py)."""
from __future__ import annotations

import calendar
import time
from pathlib import Path

import pytest

from app import backup_schedule as bs

NOW = float(calendar.timegm((2026, 11, 26, 12, 0, 0, 0, 0, 0)))


def stamp(days_ago: float) -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(NOW - days_ago * 86400))


def make(tmp: Path, names: list[str]) -> None:
    for n in names:
        (tmp / n).write_bytes(b"x")
        (tmp / (n + ".sha256")).write_bytes(b"x")


def test_prune_keeps_recent_newest_three_and_foreign_files(tmp_path: Path) -> None:
    db = [f"ziko-{stamp(d)}.Ab12Cd.sqlite3.age" for d in (1, 5, 20, 30, 40)]
    pf = [f"ziko-platform-{stamp(d)}-123.tar.gz.age" for d in (30, 40, 50, 60)]
    foreign = ["ziko-pre-update-20261001T000000.sqlite3.age", "code-1.5.3-20261001T000000.tar.gz",
               "ziko.service.pre-update-20261001T000000", "notes.txt"]
    make(tmp_path, db + pf)
    for n in foreign:
        (tmp_path / n).write_bytes(b"x")
    assert bs.cmd_prune(backup_dir=tmp_path, keep_days=14, dry_run=False, yes=True, now=NOW) == 0
    left = {p.name for p in tmp_path.iterdir()}
    assert set(db[:3]) <= left and not {db[3], db[4]} & left           # newest 3 kept even at 20 days
    assert set(pf[:3]) <= left and pf[3] not in left                   # all old: still newest 3 kept
    assert db[3] + ".sha256" not in left                               # sidecar removed too
    assert set(foreign) <= left                                        # updater files untouched


def test_dry_run_and_cancel_delete_nothing(tmp_path: Path) -> None:
    names = [f"ziko-{stamp(d)}.x1.sqlite3.age" for d in (30, 31, 32, 33)]
    make(tmp_path, names)
    before = sorted(p.name for p in tmp_path.iterdir())
    assert bs.cmd_prune(backup_dir=tmp_path, keep_days=7, dry_run=True, yes=False, now=NOW) == 0
    assert bs.cmd_prune(backup_dir=tmp_path, keep_days=7, dry_run=False, yes=False,
                        confirm=lambda _q: False, now=NOW) == 1
    assert sorted(p.name for p in tmp_path.iterdir()) == before


def test_select_prune_validates_days() -> None:
    with pytest.raises(ValueError):
        bs.select_prune([], keep_days=0, now=NOW)


def test_parse_time() -> None:
    assert bs.parse_time("3:05") == "03:05" and bs.parse_time("23:59") == "23:59"
    for bad in ("24:00", "3", "03:60", "x"):
        with pytest.raises(ValueError):
            bs.parse_time(bad)


def test_units_are_rollback_safe() -> None:
    svc = bs.service_unit(14)
    assert "ExecStart=/usr/local/bin/ziko backup\n" in svc
    assert "ExecStartPost=-/usr/local/bin/ziko backup prune --keep-days 14 --yes" in svc   # "-": ignored if missing
    tmr = bs.timer_unit("03:30")
    assert "OnCalendar=*-*-* 03:30:00" in tmr and "Persistent=true" in tmr


def test_schedule_on_status_off(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def run(cmd):
        calls.append(list(cmd))
        return (0, "active\n") if "is-active" in cmd else (0, "")
    assert bs.cmd_schedule("on", at="4:15", keep_days=10, run=run, directory=tmp_path, is_root=True) == 0
    assert bs.read_settings(tmp_path) == {"installed": True, "time": "04:15", "keep_days": 10}
    assert ["systemctl", "enable", "--now", bs.TIMER] in calls
    assert bs.cmd_schedule("status", run=run, directory=tmp_path) == 0
    assert bs.cmd_schedule("off", run=run, directory=tmp_path, is_root=True) == 0
    assert not (tmp_path / bs.TIMER).exists() and not (tmp_path / bs.SERVICE).exists()
    assert bs.read_settings(tmp_path)["installed"] is False


def test_schedule_refuses_without_root_or_bad_input(tmp_path: Path) -> None:
    def run(_cmd):
        return 0, ""
    assert bs.cmd_schedule("on", run=run, directory=tmp_path, is_root=False) == 1
    assert bs.cmd_schedule("on", at="25:00", run=run, directory=tmp_path, is_root=True) == 2
    assert bs.cmd_schedule("on", keep_days=0, run=run, directory=tmp_path, is_root=True) == 2
    assert not any(tmp_path.iterdir())


def test_uninstall_removes_the_timer() -> None:
    text = (Path(__file__).resolve().parents[1] / "scripts" / "uninstall.sh").read_text()
    assert "ziko-backup.timer" in text and "ziko-backup.service" in text

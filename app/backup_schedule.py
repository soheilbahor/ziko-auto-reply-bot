"""Scheduled encrypted backups (`ziko backup schedule ...`) and retention (`ziko backup prune`).

A systemd timer runs the existing `ziko backup` once a day, then `ziko backup prune`.
Rollback-safe: `ziko backup` exists in every release, and the prune step is prefixed with
"-" so an older release without `prune` still backs up (it just keeps everything).

Retention only ever touches files ZIKO's backup.sh creates on schedule:
  ziko-<YYYYmmddTHHMMSSZ>.<rand>.sqlite3.age (+ .sha256)
  ziko-platform-<YYYYmmddTHHMMSSZ>-<pid>.tar.gz.age (+ .sha256)
Pre-update backups, code snapshots and unit snapshots belong to the updater and are kept.
The newest KEEP_MIN of each kind are always kept, whatever their age.
"""
from __future__ import annotations

import calendar
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

SERVICE = "ziko-backup.service"
TIMER = "ziko-backup.timer"
DEFAULT_TIME = "03:30"
DEFAULT_KEEP_DAYS = 14
KEEP_MIN = 3
CLI = "/usr/local/bin/ziko"
_DB = re.compile(r"^ziko-(\d{8}T\d{6}Z)\.[A-Za-z0-9]+\.sqlite3\.age$")
_PLATFORM = re.compile(r"^ziko-platform-(\d{8}T\d{6}Z)-\d+\.tar\.gz\.age$")
_TIME = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

Runner = Callable[[Sequence[str]], tuple[int, str]]


def systemd_dir() -> Path:
    return Path(os.environ.get("ZIKO_SYSTEMD_DIR", "/etc/systemd/system"))


def parse_time(text: str) -> str:
    m = _TIME.match((text or "").strip())
    if not m:
        raise ValueError("time must be HH:MM (24 h), e.g. 03:30")
    return f"{int(m[1]):02d}:{m[2]}"


def service_unit(keep_days: int) -> str:
    return f"""[Unit]
Description=ZIKO scheduled encrypted backup
After=network-online.target ziko.service
ConditionPathExists={CLI}

[Service]
Type=oneshot
ExecStart={CLI} backup
ExecStartPost=-{CLI} backup prune --keep-days {int(keep_days)} --yes
Nice=10
IOSchedulingClass=idle
TimeoutStartSec=30min
"""


def timer_unit(at: str) -> str:
    return f"""[Unit]
Description=Daily ZIKO encrypted backup ({at} server time)

[Timer]
OnCalendar=*-*-* {at}:00
RandomizedDelaySec=10min
Persistent=true
Unit={SERVICE}

[Install]
WantedBy=timers.target
"""


def read_settings(directory: Path | None = None) -> dict[str, object]:
    directory = directory or systemd_dir()
    timer, service = directory / TIMER, directory / SERVICE
    out: dict[str, object] = {"installed": timer.is_file() and service.is_file(), "time": None, "keep_days": None}
    try:
        t = re.search(r"OnCalendar=\*-\*-\* (\d{2}:\d{2})", timer.read_text(encoding="utf-8"))
        k = re.search(r"--keep-days (\d+)", service.read_text(encoding="utf-8"))
        out["time"], out["keep_days"] = (t[1] if t else None), (int(k[1]) if k else None)
    except OSError:
        pass
    return out


@dataclass(frozen=True)
class Candidate:
    path: Path
    kind: str
    stamp: float


def _stamp(text: str) -> float:
    return float(calendar.timegm(time.strptime(text, "%Y%m%dT%H%M%SZ")))


def candidates(backup_dir: Path) -> list[Candidate]:
    found: list[Candidate] = []
    for p in backup_dir.iterdir():
        if not p.is_file() or p.is_symlink():
            continue
        for kind, pattern in (("database", _DB), ("platform", _PLATFORM)):
            m = pattern.match(p.name)
            if m:
                found.append(Candidate(p, kind, _stamp(m[1])))
    return found


def select_prune(found: list[Candidate], *, keep_days: int, now: float) -> list[Candidate]:
    """Backups older than keep_days, never touching the newest KEEP_MIN of each kind."""
    if keep_days < 1:
        raise ValueError("--keep-days must be at least 1")
    doomed: list[Candidate] = []
    for kind in ("database", "platform"):
        same = sorted((c for c in found if c.kind == kind), key=lambda c: c.stamp, reverse=True)
        doomed += [c for c in same[KEEP_MIN:] if now - c.stamp > keep_days * 86400]
    return sorted(doomed, key=lambda c: c.stamp)


def cmd_prune(*, backup_dir: Path, keep_days: int, dry_run: bool, yes: bool,
              confirm: Callable[[str], bool] | None = None, now: float | None = None) -> int:
    try:
        found = candidates(backup_dir)
        doomed = select_prune(found, keep_days=keep_days, now=now or time.time())
    except PermissionError:
        print(f"Cannot read {backup_dir}: run with sudo.")
        return 1
    except FileNotFoundError:
        print(f"Backup directory not found: {backup_dir}")
        return 1
    except ValueError as exc:
        print(str(exc))
        return 2
    kept = len(found) - len(doomed)
    if not doomed:
        print(f"Nothing to prune: {len(found)} scheduled backup(s), none older than {keep_days} days "
              f"beyond the newest {KEEP_MIN} of each kind.")
        return 0
    print(f"{'Would delete' if dry_run else 'Deleting'} {len(doomed)} backup(s) older than {keep_days} days "
          f"(keeping {kept}):")
    for c in doomed:
        print(f"  {c.path.name}")
    if dry_run:
        return 0
    if not yes and not (confirm and confirm(f"Delete {len(doomed)} backup(s)?")):
        print("Cancelled.")
        return 1
    removed = 0
    for c in doomed:
        for p in (c.path, c.path.with_name(c.path.name + ".sha256")):
            try:
                p.unlink()
                removed += p is c.path
            except FileNotFoundError:
                pass
            except OSError as exc:
                print(f"Could not delete {p.name}: {type(exc).__name__}")
    print(f"Deleted {removed} backup(s).")
    return 0 if removed == len(doomed) else 1


def cmd_schedule(state: str, *, at: str = DEFAULT_TIME, keep_days: int = DEFAULT_KEEP_DAYS,
                 run: Runner, directory: Path | None = None, is_root: bool | None = None) -> int:
    directory = directory or systemd_dir()
    if state == "status":
        s = read_settings(directory)
        rc, active = run(["systemctl", "is-active", TIMER])
        _rc, nxt = run(["systemctl", "show", TIMER, "-p", "NextElapseUSecRealtime", "--value"])
        _rc2, last = run(["systemctl", "show", SERVICE, "-p", "Result", "--value"])
        if not s["installed"]:
            print("Scheduled backups: off. Enable: sudo ziko backup schedule on")
            return 0
        print(f"Scheduled backups: {'on' if active.strip() == 'active' else 'installed but ' + active.strip()}")
        print(f"  time: {s['time']} server time ({time.strftime('%Z')}), keep: {s['keep_days']} days "
              f"(newest {KEEP_MIN} always kept)")
        print(f"  next run: {nxt.strip() or 'unknown'}   last result: {last.strip() or 'never ran'}")
        print(f"  logs: journalctl -u {SERVICE} -n 50")
        return 0 if active.strip() == "active" else 1
    if (os.geteuid() != 0) if is_root is None else not is_root:
        print("Run with sudo: sudo ziko backup schedule " + state)
        return 1
    if state == "off":
        run(["systemctl", "disable", "--now", TIMER])
        for name in (TIMER, SERVICE):
            try:
                (directory / name).unlink()
            except FileNotFoundError:
                pass
        run(["systemctl", "daemon-reload"])
        print("Scheduled backups: off. Existing backups were not touched.")
        return 0
    if state != "on":
        print("Use: ziko backup schedule on|off|status")
        return 2
    try:
        at = parse_time(at)
    except ValueError as exc:
        print(str(exc))
        return 2
    if not 1 <= int(keep_days) <= 3650:
        print("--keep-days must be between 1 and 3650")
        return 2
    for name, body in ((SERVICE, service_unit(keep_days)), (TIMER, timer_unit(at))):
        tmp = directory / f".{name}.tmp"
        tmp.write_text(body, encoding="utf-8")
        os.chmod(tmp, 0o644)
        os.replace(tmp, directory / name)
    for cmd in (["systemctl", "daemon-reload"], ["systemctl", "enable", "--now", TIMER]):
        rc, out = run(cmd)
        if rc != 0:
            print(f"{' '.join(cmd)} failed: {out.strip()[:200]}")
            return 1
    print(f"Scheduled backups: on, daily at {at} server time ({time.strftime('%Z')}), keeping {keep_days} days.")
    print(f"Test now: sudo systemctl start {SERVICE} && journalctl -u {SERVICE} -n 30")
    return 0

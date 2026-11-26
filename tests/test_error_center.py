"""`ziko logs errors` (app/error_center.py): grouping, classification, window, redaction, exit codes."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import error_center as ec

NOW = datetime(2026, 11, 26, 20, 0, tzinfo=timezone.utc)


def line(minutes_ago: int, level: str, msg: str, bot: str = "-") -> str:
    ts = (NOW - timedelta(minutes=minutes_ago)).astimezone(timezone(timedelta(hours=3, minutes=30)))
    return f"{ts.isoformat(timespec='milliseconds')} {level:<8} ziko.x cid=abc bot={bot} stage=SEND {msg}"


LINES = [
    line(5, "WARNING", "Auto-reply rejected by Telegram TelegramForbiddenError retries=0", "111"),
    line(6, "WARNING", "Auto-reply rejected by Telegram TelegramForbiddenError retries=0", "222"),
    line(7, "ERROR", "Polling failed TelegramNetworkError Cannot connect to host api.telegram.org", "111"),
    line(8, "ERROR", "Tenant stopped: token_revoked TelegramUnauthorizedError", "333"),
    line(9, "WARNING", "Something odd happened KeyError in handler"),
    line(10, "INFO", "Business auto-reply finished outcome=sent"),
    line(60 * 30, "ERROR", "old TelegramNetworkError"),            # outside 24 h
    "garbage line without the format",
]


def test_groups_by_cause_with_counts_bots_and_levels():
    groups = ec.collect(LINES, since=NOW - timedelta(hours=24))
    by = {g.code: g for g in groups}
    assert by["forbidden"].count == 2 and by["forbidden"].bots == {"111", "222"}
    assert by["network"].count == 1 and by["network"].level == "ERROR"
    assert by["token_rejected"].bots == {"333"}
    assert "other:KeyError" in by
    assert groups[0].level == "ERROR"                                # errors listed first
    assert sum(g.count for g in groups) == 5                         # INFO, old and garbage ignored


def test_bot_filter_and_exit_codes():
    only = ec.collect(LINES, since=NOW - timedelta(hours=24), bot="222")
    assert [g.code for g in only] == ["forbidden"] and ec.exit_code(only) == 1
    assert ec.exit_code(ec.collect(LINES, since=NOW - timedelta(hours=24))) == 2
    assert ec.exit_code([]) == 0


def test_tokens_are_redacted_in_samples():
    leaked = line(1, "ERROR", "request failed https://api.telegram.org/bot123456789:" + "A" * 35 + "/getMe Timeout")
    g = ec.collect([leaked], since=NOW - timedelta(hours=1))[0]
    assert "AAAA" not in g.sample and "<redacted>" in g.sample


@pytest.mark.parametrize("message,code", [
    ("TelegramRetryAfter: Flood control exceeded", "flood_wait"),
    ("TelegramConflictError: terminated by other getUpdates request", "polling_conflict"),
    ("sqlite3.OperationalError: database is locked", "database_locked"),
    ("OSError: [Errno 28] No space left on device", "disk_full"),
    ("PermissionError: [Errno 13] Permission denied", "permission"),
    ("Daily report failed RuntimeError; will retry", "daily_report"),
    ("Platform alert reached no admin; it will be retried next check", "alert_delivery"),
    ("BUSINESS_PEER_USAGE_MISSING", "peer_not_allowed"),
    ("bot 4091234567 restarted after crash", "restart"),                 # 409 inside an id: not a conflict
])
def test_classification(message, code):
    assert ec.classify(message)[0] == code


def test_every_rule_has_english_title_and_fix():
    for code, _p, title, fix in ec.RULES:
        assert title.isascii() and fix.isascii() and len(fix) > 20, code


def test_parse_since():
    assert ec.parse_since("30m") == timedelta(minutes=30)
    assert ec.parse_since("7d") == timedelta(days=7)
    for bad in ("", "24", "0h", "1y", "100d"):
        with pytest.raises(ValueError):
            ec.parse_since(bad)


def test_cmd_errors_reads_master_and_tenant_logs(tmp_path: Path, capsys) -> None:
    (tmp_path / "tenants").mkdir()
    def now_line(msg: str, bot: str = "-") -> str:
        ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        return f"{ts} ERROR    ziko.x cid=abc bot={bot} stage=SEND {msg}\n"
    (tmp_path / "master.log").write_text(now_line("Platform alert not delivered admin_id=1 error=X"))
    (tmp_path / "tenants" / "bot-111.log").write_text(now_line("TelegramNetworkError Timeout", "111"))
    assert ec.cmd_errors(log_dir=tmp_path) == 2
    out = capsys.readouterr().out
    assert "Network problem" in out and "Admin alert" in out and "2 log file(s)" in out
    assert ec.cmd_errors(log_dir=tmp_path, as_json=True) == 2
    data = json.loads(capsys.readouterr().out)
    assert {g["code"] for g in data["groups"]} == {"network", "alert_delivery"}
    assert ec.cmd_errors(log_dir=tmp_path, since="bad") == 2


def test_cmd_errors_with_no_logs(tmp_path: Path, capsys) -> None:
    assert ec.cmd_errors(log_dir=tmp_path) == 0
    assert "No log files" in capsys.readouterr().out

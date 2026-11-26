"""CLI child-bot views fed by the service snapshot (app/cli_bots.py, run/overview.json)."""
from __future__ import annotations

import contextlib
import io
import json
import re
import sqlite3
import stat
import time
from pathlib import Path

import pytest

from app import cli_bots, cli_monitor
from app import platform_overview as po

SCHEMA = (Path(__file__).resolve().parents[1] / "app" / "database_schema.sql").read_text()


def bot(i, name, state, codes=(), runtime="running", enabled=True):
    view = po.BotView(bot_id=i, user_id=1000 + i, username=name, enabled=enabled, runtime=runtime)
    view.state, view.reason_codes = state, list(codes)
    view.reasons = ["متن فارسی فقط برای تلگرام"] * len(view.reason_codes)  # must never reach the CLI
    return view


MIXED = [bot(1, "down_bot", "crashed", ["polling_not_running"]), bot(2, "tok_bot", "token", ["token_rejected"]),
         bot(3, "noreply_bot", "blocked", ["no_reply_permission"]), bot(4, "ok_bot", "ready"),
         bot(5, "q_bot", "queued", ["waiting_for_polling_slot"], runtime="waiting"),
         bot(6, "off_bot", "disabled", ["disabled_by_admin"], enabled=False)]


@pytest.fixture
def db(tmp_path):
    return tmp_path / "ziko.sqlite3"


def write(db, bots, *, age=10, running=10, waiting=1, limit=10):
    po.write_snapshot(cli_bots.snapshot_path(db), po.Overview(bots, running, waiting, limit, time.time() - age))


def run_status(db, **kw):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = cli_bots.cmd_bots_status(db_path=db, err=err, **kw)
    return rc, out.getvalue(), err.getvalue()


def test_snapshot_file_is_private_atomic_and_secret_free(db):
    write(db, MIXED)
    path = cli_bots.snapshot_path(db)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not list(path.parent.glob(".*tmp"))
    raw = path.read_text()
    assert not re.search(r"\d{5,}:[A-Za-z0-9_-]{20,}", raw)
    data = json.loads(raw)
    assert data["counts"]["crashed"] == 1 and len(data["bots"]) == 6


def test_status_table_and_exit_codes(db):
    write(db, MIXED)
    rc, out, _ = run_status(db)
    assert rc == 2
    assert all(f"@{n}" in out for n in ("down_bot", "tok_bot", "ok_bot", "off_bot"))
    assert "polling down" in out
    rc, out, _ = run_status(db, attention=True)
    assert "@ok_bot" not in out and "@q_bot" not in out and "@down_bot" in out
    write(db, [bot(3, "noreply_bot", "blocked", ["x"]), bot(4, "ok", "ready")], waiting=0)
    assert run_status(db)[0] == 1
    write(db, [bot(4, "ok", "ready")], waiting=0)
    assert run_status(db)[0] == 0
    assert "no bot needs attention" in run_status(db, attention=True)[1]


def test_json_output(db):
    write(db, MIXED)
    rc, out, _ = run_status(db, as_json=True)
    payload = json.loads(out)
    assert rc == 2 and payload["problem"] is None and 5 <= payload["age_seconds"] < 60


def test_stale_missing_corrupt_and_wrong_schema(db):
    write(db, [bot(4, "ok", "ready")], age=600, waiting=0)
    rc, out, _ = run_status(db)
    assert rc == 1 and "STALE" in out
    path = cli_bots.snapshot_path(db)
    path.unlink()
    rc, _, err = run_status(db)
    assert rc == 3 and "service is stopped" in err
    path.write_text("{broken")
    assert run_status(db)[0] == 3
    path.write_text(json.dumps({"schema": 99, "bots": [], "taken_at": time.time()}))
    assert po.read_snapshot(path) is None


def test_health_checks(db):
    write(db, MIXED)
    checks = {metric: (level, detail) for level, metric, detail in cli_bots.health_checks(db)}
    assert checks["bots.down"][0] == "CRIT" and "@down_bot" in checks["bots.down"][1]
    assert checks["bots.attention"][0] == "WARN"
    assert checks["bots.capacity"][0] == "WARN"
    cli_bots.snapshot_path(db).unlink()
    assert cli_bots.health_checks(db)[0][0] == "OK"  # no registry: no child bots at all
    (db.parent / "tenants.sqlite3").write_bytes(b"")
    assert cli_bots.health_checks(db)[0][0] == "WARN"


def test_ziko_health_includes_child_bots(db):
    mem = sqlite3.connect(":memory:")
    mem.executescript(SCHEMA)
    db.write_bytes(mem.serialize())
    mem.close()
    write(db, MIXED)
    rc, alerts = cli_monitor.health(db.parent, db)
    assert "bots.down" in [m for _, m, _ in alerts] and rc == 2
    write(db, [bot(4, "ok", "ready")], waiting=0)
    _, alerts = cli_monitor.health(db.parent, db)
    assert all(level == "OK" for level, m, _ in alerts if m.startswith("bots"))


def test_dashboard_rows(db):
    write(db, MIXED)
    rows = cli_bots.dashboard_rows(db)
    assert rows[0][0] == "bots" and rows[1][0] == "polling"
    assert any("@down_bot" in value for _, value in rows)


def test_large_snapshot_reads_fast(db):
    write(db, [bot(i, f"b{i}", "ready") for i in range(500)], waiting=0, running=50, limit=50)
    started = time.perf_counter()
    for _ in range(20):
        cli_bots.load(db)
    assert (time.perf_counter() - started) / 20 < 0.5
    assert cli_bots.snapshot_path(db).stat().st_size < 200_000


def _has_persian(text: str) -> bool:
    return any("\u0600" <= ch <= "\u06ff" for ch in text)


def test_cli_output_and_snapshot_are_english_only(db):
    write(db, MIXED)
    assert not _has_persian(cli_bots.snapshot_path(db).read_text()), "snapshot must not carry Persian"
    for kwargs in ({}, {"attention": True}, {"as_json": True}):
        _rc, out, err = run_status(db, **kwargs)
        assert not _has_persian(out + err), kwargs
    rows = json.loads(run_status(db, as_json=True)[1])["snapshot"]["bots"]
    assert next(r for r in rows if r["username"] == "noreply_bot")["reason_codes"] == ["no_reply_permission"]
    assert "reply" in run_status(db)[1]  # table may wrap the reason across lines
    assert not any(_has_persian(v) for _, v in cli_bots.dashboard_rows(db))
    assert not any(_has_persian(d) for _, _, d in cli_bots.health_checks(db))


def test_old_snapshot_with_persian_reasons_is_not_displayed(db):
    import json as _json
    path = cli_bots.snapshot_path(db)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_json.dumps({"schema": 1, "taken_at": time.time(), "running": 1, "waiting": 0, "limit": 5,
                                 "counts": {"crashed": 1}, "bots": [
                                     {"bot_id": 1, "user_id": 2, "username": "x", "enabled": True,
                                      "runtime": "stopped", "state": "crashed", "reasons": ["پولینگ متوقف"]}]}))
    _rc, out, _err = run_status(db)
    assert "پولینگ" not in out

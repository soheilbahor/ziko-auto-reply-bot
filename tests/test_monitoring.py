"""Server and database monitoring (app/monitor.py, dbdiag.py, cli_monitor.py)."""
from __future__ import annotations

import contextlib
import io
import json
import sqlite3
from pathlib import Path

import pytest

from app import cli_monitor, dbdiag, monitor

SCHEMA = (Path(__file__).resolve().parents[1] / "app" / "database_schema.sql").read_text()


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "ziko.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO business_connections VALUES('c1',1,1,5,'2026-01-01','2026-01-01')")
    conn.execute("INSERT INTO reply_deliveries(connection_id,chat_id,session_id,state,claim_token,claimed_at,"
                 "updated_at) VALUES('c1',9,'s','pending','t','2020-01-01T00:00:00.000Z','x')")
    conn.commit()
    conn.close()
    return path


def test_healthy_database_with_a_stuck_delivery(db):
    report = dbdiag.inspect(db)
    assert report.ok and report.integrity == "ok" and not report.missing_tables
    assert report.delivery_states == {"pending": 1} and report.stuck_pending == 1


def test_diagnostics_are_read_only(db):
    before = db.read_bytes()
    dbdiag.inspect(db, full_integrity=True)
    assert db.read_bytes() == before


def test_corrupt_and_missing_databases(tmp_path):
    bad = tmp_path / "bad.sqlite3"
    bad.write_bytes(b"not a database" * 100)
    report = dbdiag.inspect(bad)
    assert not report.ok and report.error
    assert dbdiag.inspect(tmp_path / "nope.sqlite3").error == "file not found"


@pytest.mark.parametrize("section", ["cpu", "memory", "disk", "network", "processes"])
def test_every_metric_section_returns_data_or_says_why_not(section, tmp_path):
    snap = {"cpu": lambda: monitor.cpu(sample_seconds=0.05, top=2),
            "memory": lambda: monitor.memory(top=2),
            "disk": lambda: monitor.disk([tmp_path], sample_seconds=0.05),
            "network": lambda: monitor.network(sample_seconds=0.05, dns_host="localhost"),
            "processes": lambda: monitor.processes(top=2)}[section]()
    assert snap.section == section
    assert snap.data or snap.unsupported  # never an empty, unexplained result


def test_json_output_and_exit_codes(db):
    for section in cli_monitor.SECTIONS:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli_monitor.cmd_monitor(section, data_dir=db.parent, db_path=db, as_json=True)
        json.loads(out.getvalue())
        assert rc in (0, 1, 2)
    with contextlib.redirect_stderr(io.StringIO()):
        assert cli_monitor.cmd_monitor("gpu", data_dir=db.parent, db_path=db) == 64


def test_health_flags_stuck_deliveries_and_missing_database(db, tmp_path):
    _rc, alerts = cli_monitor.health(db.parent, db)
    assert any(metric == "database.stuck_pending" for _, metric, _ in alerts)
    missing = tmp_path / "empty" / "ziko.sqlite3"
    missing.parent.mkdir()
    assert cli_monitor.health(missing.parent, missing)[0] == 2

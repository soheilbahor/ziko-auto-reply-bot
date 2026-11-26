"""Platform overview: classification of every child bot (app/platform_overview.py)."""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app import platform_overview as po

SCHEMA = (Path(__file__).resolve().parents[1] / "app" / "database_schema.sql").read_text()
STAMP = "2026-11-25T16:00:00.000Z"
NOW = datetime(2026, 11, 25, 20, 0, tzinfo=UTC).timestamp()  # 23:30 in Tehran


@dataclass
class Tenant:
    user_id: int
    bot_id: int
    username: str
    enabled: bool = True


def make_db(path: Path, *, conn=(1, 1), enabled=True, window=(None, None), deliveries=()):
    mem = sqlite3.connect(":memory:")
    mem.executescript(SCHEMA)
    mem.execute("INSERT INTO statuses(key,title,emoji,reply_text,updated_at,active_from_hour,active_to_hour) "
                "VALUES('sleeping','در حال خواب','😴','سلام',?,?,?)", (STAMP, *window))
    mem.execute("INSERT INTO owner_settings(id,timezone,updated_at) VALUES(1,'Asia/Tehran',?)", (STAMP,))
    mem.execute("INSERT INTO app_state(id,enabled,active_status_key,session_id,updated_at) VALUES(1,?,?,?,?)",
                (int(enabled), "sleeping", "s1" if enabled else None, STAMP))
    if conn:
        mem.execute("INSERT INTO business_connections VALUES('c1',?,?,5,?,?)", (*conn, STAMP, STAMP))
        for i, state in enumerate(deliveries):
            mem.execute("INSERT INTO reply_deliveries(connection_id,chat_id,session_id,state,claim_token,"
                        "claimed_at,completed_at,updated_at) VALUES('c1',?,'s1',?,'t',?,?,?)",
                        (i, state, STAMP, STAMP, STAMP))
    mem.commit()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(mem.serialize())
    mem.close()


@pytest.fixture
def platform(tmp_path):
    tenants, runtime, telemetry = [], {}, {}
    fresh = {"errors": {}, "last_success_at": NOW - 5, "latency_ms": {"p50": 80, "p95": 200}, "rate_limited": 0}

    def add(uid, bid, name, *, enabled=True, rt="running", tel=fresh, db=None, legacy=False):
        tenants.append(Tenant(uid, bid, name, enabled))
        runtime[bid] = rt
        if tel is not None:
            telemetry[bid] = tel
        if db is not None:
            make_db(tmp_path / "tenants" / (f"{uid}.sqlite3" if legacy else f"bot-{bid}.sqlite3"), **db)

    add(1, 101, "ready_bot", db=dict(deliveries=["sent"] * 9))
    add(2, 102, "noconn_bot", db=dict(conn=None))
    add(3, 103, "noreply_bot", db=dict(conn=(1, 0)))
    add(4, 104, "off_bot", db=dict(enabled=False))
    add(5, 105, "failing_bot", db=dict(deliveries=["sent"] * 3 + ["failed", "uncertain", "failed"]))
    add(6, 106, "queued_bot", rt="waiting", tel=None, db={})
    add(7, 107, "crashed_bot", rt="stopped", tel=None, db={})
    add(8, 108, "silent_bot", tel={**fresh, "last_success_at": NOW - 900}, db={})
    add(9, 109, "badtoken_bot", tel={**fresh, "errors": {"unauthorized": 1}}, db={})
    add(10, 110, "disabled_bot", enabled=False, rt="stopped", tel=None)
    add(11, 111, "nodb_bot")
    add(12, 112, "legacy_bot", db={}, legacy=True)
    add(12, 113, "<b>second</b>_bot", db={})
    add(14, 114, "night_bot", db=dict(window=(9, 17)))
    (tmp_path / "tenants" / "bot-115.sqlite3").write_bytes(b"garbage" * 50)
    add(15, 115, "corrupt_bot")
    add(16, 116, "starting_bot", rt="starting", tel=None, db={})
    add(17, 117, "degraded_bot", rt="degraded", db={})
    ov = po.collect(tenants=tenants, data_dir=tmp_path, runtime_of=runtime.__getitem__, telemetry=telemetry,
                    running=11, waiting=1, limit=12, now=NOW)
    return ov, {b.username: b for b in ov.bots}, tmp_path


@pytest.mark.parametrize("name,state", [
    ("ready_bot", "ready"), ("noconn_bot", "blocked"), ("noreply_bot", "blocked"), ("off_bot", "blocked"),
    ("failing_bot", "warning"), ("queued_bot", "queued"), ("crashed_bot", "crashed"),
    ("silent_bot", "crashed"), ("badtoken_bot", "token"), ("disabled_bot", "disabled"),
    ("starting_bot", "warning"), ("degraded_bot", "crashed"),
    ("nodb_bot", "warning"), ("legacy_bot", "ready"), ("<b>second</b>_bot", "ready"),
    ("night_bot", "blocked"), ("corrupt_bot", "warning"),
])
def test_classification(platform, name, state):
    _, bots, _ = platform
    assert bots[name].state == state


def test_reason_codes_are_english_and_parallel_to_persian_reasons(platform):
    import re as _re
    ov, _, _ = platform
    for b in ov.bots:
        if b.state != "ready":
            assert b.reason_codes, b.username
            assert all(_re.fullmatch(r"[a-z0-9_]+", c) for c in b.reason_codes), (b.username, b.reason_codes)
    snap = po.to_snapshot(ov)
    assert "reasons" not in snap["bots"][0] and not _re.search(r"[\u0600-\u06FF]", str(snap))


def test_reasons_are_actionable(platform):
    _, bots, _ = platform
    assert bots["noreply_bot"].reason_codes == ["no_reply_permission"]
    assert bots["night_bot"].reason_codes == ["outside_active_hours"]
    assert "اجازه" in bots["noreply_bot"].reasons[0]
    assert "09:00" in bots["night_bot"].reasons[0] and "23:30" in bots["night_bot"].reasons[0]
    assert "900" in bots["silent_bot"].reasons[0]


def test_sorted_worst_first_and_attention_count(platform):
    ov, _, _ = platform
    priorities = [po.STATES[b.state][0] for b in ov.bots]
    assert priorities == sorted(priorities)
    assert len(ov.needs_attention) == 12


def test_collect_never_modifies_tenant_databases(platform):
    ov, _, data = platform
    before = {p: p.read_bytes() for p in (data / "tenants").glob("*.sqlite3")}
    po.collect(tenants=[], data_dir=data, runtime_of=lambda _b: "running", telemetry={},
               running=0, waiting=0, limit=1, now=NOW)
    assert all(p.read_bytes() == content for p, content in before.items())
    assert not [p for p in (data / "tenants").iterdir() if re.search(r"-(wal|shm|journal)$", p.name)]


def test_overview_paging_filter_and_escaping(platform):
    ov, bots, _ = platform
    total = len(ov.bots)
    pages_expected = -(-total // po.PAGE_SIZE)
    seen: list[int] = []
    for page in range(pages_expected):
        _t, rows, pages = po.render_overview(ov, page, "all", now=NOW)
        assert pages == pages_expected
        seen += [b.bot_id for b in rows]
    assert len(seen) == len(set(seen)) == total               # every bot exactly once
    text, shown, _ = po.render_overview(ov, 0, "all", now=NOW)
    assert len(shown) == po.PAGE_SIZE
    last = po.render_overview(ov, pages_expected - 1, "all", now=NOW)[1]
    assert po.render_overview(ov, 99, "all", now=NOW)[1] == last
    text2 = "".join(po.render_overview(ov, p, "all", now=NOW)[0] for p in range(pages_expected))
    _, attention, _ = po.render_overview(ov, 0, "attention", now=NOW)
    assert all(b.state in {"crashed", "token", "blocked", "warning"} for b in attention)
    detail = po.render_bot(bots["<b>second</b>_bot"], NOW)
    assert "<b>second</b>" not in text2 + detail and "&lt;b&gt;second" in text2 + detail


def test_capacity_hint_and_empty_platform(platform):
    ov, _, _ = platform
    assert "ZIKO_TENANT_MAX_CONCURRENT_POLLERS" not in po.render_overview(ov, now=NOW)[0]
    full = po.Overview(ov.bots, 12, 3, 12, NOW)
    assert "ZIKO_TENANT_MAX_CONCURRENT_POLLERS" in po.render_overview(full, now=NOW)[0]
    empty_text, empty_rows, _ = po.render_overview(po.Overview([], 0, 0, 10, NOW), now=NOW)
    assert "/create_bot" in empty_text and empty_rows == []


def test_bot_detail_age_is_never_negative_and_fits(platform):
    ov, bots, _ = platform
    detail = po.render_bot(bots["noreply_bot"], NOW)
    assert "5 ثانیه پیش" in detail and "/diagnose" in detail
    for b in ov.bots:
        assert len(po.render_bot(b, NOW)) <= 4096


def test_footer_uses_local_time_and_age():
    taken = datetime(2026, 11, 25, 17, 0, tzinfo=UTC).timestamp()
    ov = po.Overview([po.BotView(bot_id=1, user_id=1, username="x", enabled=True, runtime="running")],
                     1, 0, 10, taken)
    assert "20:30:00 (Asia/Tehran)" in po.render_overview(ov, now=taken + 42)[0]
    assert "42 ثانیه پیش" in po.render_overview(ov, now=taken + 42)[0]
    assert "ثانیه پیش" not in po.render_overview(ov, now=taken + 2)[0]
    assert "17:00:00 (UTC)" in po.render_overview(ov, tz="Not/AZone", now=taken)[0]

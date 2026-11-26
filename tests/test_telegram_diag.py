"""Telegram telemetry and diagnostics (app/telegram_stats.py, tgdiag.py, cli_telegram.py)."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import stat
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from app import cli_telegram, tgdiag
from app import telegram_stats as ts

SCHEMA = (Path(__file__).resolve().parents[1] / "app" / "database_schema.sql").read_text()
TOKEN = "4242424242:AAH_child_secret_token_value_xyz"


class TelegramRetryAfter(Exception):  # name-matched like aiogram's class
    def __init__(self, retry_after):
        self.retry_after = retry_after
        super().__init__("retry after; url=https://api.telegram.org/bot123456:SECRETSECRETSECRETSECRET/x")


class TelegramUnauthorizedError(Exception):
    pass


class SendMessage:
    pass


class GetUpdates:
    pass


async def ok(_bot, _method):
    await asyncio.sleep(0.005)
    return "result"


async def flood(_bot, _method):
    raise TelegramRetryAfter(7)


async def test_middleware_counts_without_changing_behaviour():
    stats = ts.TelegramStats()
    mw = ts.TelemetryRequestMiddleware(42, "child", stats)
    for _ in range(20):
        assert await mw(ok, None, SendMessage()) == "result"
    await mw(ok, None, GetUpdates())
    with pytest.raises(TelegramRetryAfter):
        await mw(flood, None, SendMessage())
    s = stats.snapshot()["bots"][0]
    assert s["requests"] == 22 and s["failures"] == 1
    assert s["rate_limited"] == 1 and s["last_retry_after"] == 7.0
    assert s["latency_ms"]["samples"] == 21  # long-poll getUpdates excluded
    assert s["latency_ms"]["p50"] >= 4


def test_categories_window_and_no_secrets():
    assert ts.categorize(TelegramUnauthorizedError()) == "unauthorized"
    assert ts.categorize(ValueError()) == "other"
    stats = ts.TelegramStats()
    for _ in range(1000):
        stats.record(1, "master", "SendMessage", 1.0)
    stats.record(2, "child", "SendMessage", 1.0, TelegramRetryAfter(3))
    assert len(stats.bot(1).latencies_ms) == ts.WINDOW
    blob = json.dumps(stats.snapshot())
    assert "SECRET" not in blob and "api.telegram.org" not in blob


def test_status_file(tmp_path):
    path = tmp_path / "run" / ts.STATUS_FILE
    ts.write_status(path, ts.TelegramStats())
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not [p for p in path.parent.iterdir() if p.name.startswith(".tg-")]
    assert ts.read_status(path)["schema"] == ts.SCHEMA


def test_telemetry_checks_flag_stale_and_missing(tmp_path):
    path = tmp_path / ts.STATUS_FILE
    ts.write_status(path, ts.TelegramStats())
    written = json.loads(path.read_text())["written_at"]
    checks, _ = tgdiag.telemetry_checks(path, now=written + 500)
    assert checks[0][0] == "warn" and "STALE" in checks[0][2]
    checks, _ = tgdiag.telemetry_checks(tmp_path / "missing.json")
    assert checks[0][0] == "warn"


def test_master_checks_detect_misconfiguration():
    def api(_token, method):
        if method == "getMe":
            return True, {"username": "m", "id": 1, "can_connect_to_business": False}
        return True, {"url": "https://example.invalid/hook"}
    assert [c[0] for c in tgdiag.master_checks("t", api)] == ["ok", "warn", "warn"]
    assert tgdiag.master_checks("t", lambda _t, _m: (False, "Unauthorized"))[0][0] == "fail"


@pytest.fixture
def data(tmp_path):
    (tmp_path / "tenants").mkdir()
    (tmp_path / "logs" / "tenants").mkdir(parents=True)
    for p in (tmp_path / "ziko.sqlite3", tmp_path / "tenants" / "bot-42.sqlite3"):
        conn = sqlite3.connect(p)
        conn.executescript(SCHEMA)
        conn.commit()
        conn.close()
    conn = sqlite3.connect(tmp_path / "tenants" / "bot-42.sqlite3")
    conn.execute("INSERT INTO business_connections VALUES('cx',1,0,7,'a','a')")  # connected, no reply right
    conn.commit()
    conn.close()
    key = Fernet.generate_key()
    (tmp_path / "tenant-token.key").write_bytes(key)
    conn = sqlite3.connect(tmp_path / "tenants.sqlite3")
    conn.execute("CREATE TABLE tenants(tenant_id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INT, bot_id INT UNIQUE, "
                 "username TEXT, encrypted_token TEXT, enabled INT, created_at TEXT)")
    conn.execute("INSERT INTO tenants(user_id,bot_id,username,encrypted_token,enabled,created_at) "
                 "VALUES(7,42,'childbot',?,1,'2026-11-01')", (Fernet(key).encrypt(TOKEN.encode()).decode(),))
    conn.commit()
    conn.close()
    (tmp_path / "logs" / "tenants" / "bot-42.log").write_text(
        "2026 ERROR x Too Many Requests: retry after 5\n2026 INFO ok\n"
        "2026 WARNING TelegramNetworkError: Cannot connect\n")
    stats = ts.TelegramStats()
    for _ in range(5):
        stats.record(42, "child", "SendMessage", 10.0)
    ts.write_status(tmp_path / "run" / ts.STATUS_FILE, stats)
    return tmp_path


def test_inspect_bot_without_probe_never_touches_the_token(data):
    calls = []
    report = cli_telegram.inspect_bot(42, data / "ziko.sqlite3", lambda *a: calls.append(a) or (True, {}))
    levels = {c["check"]: c["level"] for c in report["checks"]}
    assert levels["registry"] == "ok"
    assert levels["Business tenant"] == "warn"
    assert "child 42" in levels
    assert levels["log errors"] == "warn"
    assert calls == []


def test_probe_decrypts_in_memory_and_never_prints_the_token(data):
    calls = []

    def api(token, method, payload=None, timeout=7):
        calls.append((token, method))
        return True, {"id": 42, "username": "childbot"}
    report = cli_telegram.inspect_bot(42, data / "ziko.sqlite3", api, probe=True)
    assert calls == [(TOKEN, "getMe")]
    assert TOKEN not in json.dumps(report) and "child_secret" not in json.dumps(report)


def test_probe_with_wrong_key_and_unknown_bot(data):
    (data / "tenant-token.key").write_bytes(Fernet.generate_key())
    report = cli_telegram.inspect_bot(42, data / "ziko.sqlite3", lambda *a: (True, {}), probe=True)
    assert any(c["check"] == "token" and c["level"] == "fail" for c in report["checks"])
    assert cli_telegram.inspect_bot(999, data / "ziko.sqlite3", lambda *a: (True, {}))["status"] == 3


def test_network_checks_never_raise():
    checks, _metrics = tgdiag.network_checks(timeout=2.0)
    assert isinstance(checks, list) and checks

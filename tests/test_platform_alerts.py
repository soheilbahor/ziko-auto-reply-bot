"""Proactive admin alerts: debounce, batching, limits, mute (app/platform_alerts.py)."""
from __future__ import annotations

import asyncio
import stat
from dataclasses import dataclass, field

import pytest

from app import platform_alerts as pa

T0 = 1_800_000_000.0
CFG = pa.AlertConfig(interval=60, confirm_checks=2, cooldown=3600, max_per_hour=6, queue_checks=5)


@dataclass
class Bot:
    bot_id: int
    username: str
    state: str = "ready"
    reasons: list = field(default_factory=list)


def snap(**states):
    return [Bot(100 + i, name, st, [] if st == "ready" else ["down"])
            for i, (name, st) in enumerate(sorted(states.items()))]


def feed(engine, snapshots, *, start=T0, step=60, waiting=0, running=1, limit=10):
    out = []
    for i, s in enumerate(snapshots):
        w = waiting(i) if callable(waiting) else waiting
        out.append(engine.step(s, waiting=w, running=limit if w else running, limit=limit,
                               now=start + i * step))
    return out


def test_single_check_blip_never_alerts():
    msgs = feed(pa.AlertEngine(CFG), [snap(a="ready"), snap(a="crashed"), snap(a="ready"), snap(a="ready")])
    assert msgs == [None] * 4


def test_sustained_problem_alerts_once_after_debounce():
    msgs = feed(pa.AlertEngine(CFG), [snap(a="ready")] + [snap(a="crashed")] * 6)
    assert [i for i, m in enumerate(msgs) if m] == [2]
    assert "@a" in msgs[2] and "down" in msgs[2]


def test_recovery_announced_once_and_only_for_announced_problems():
    engine = pa.AlertEngine(CFG)
    feed(engine, [snap(a="crashed")] * 3)
    recovered = [m for m in feed(engine, [snap(a="ready")] * 4, start=T0 + 600) if m]
    assert len(recovered) == 1 and "@a" in recovered[0]
    quiet = feed(pa.AlertEngine(CFG), [snap(b="crashed"), snap(b="ready"), snap(b="ready")])
    assert quiet == [None] * 3


def test_owner_side_states_are_silent_unless_opted_in():
    assert feed(pa.AlertEngine(CFG), [snap(a="blocked", b="warning")] * 5) == [None] * 5
    opted = pa.AlertConfig(confirm_checks=2, states=frozenset({"crashed", "token", "blocked"}))
    assert any(feed(pa.AlertEngine(opted), [snap(a="blocked")] * 3))


def test_simultaneous_failures_are_one_message():
    msgs = [m for m in feed(pa.AlertEngine(CFG), [snap(a="ready", b="ready", c="ready")]
                            + [snap(a="crashed", b="token", c="crashed")] * 3) if m]
    assert len(msgs) == 1 and all(f"@{n}" in msgs[0] for n in "abc")


def test_flapping_bot_respects_cooldown():
    cycle = ([snap(a="crashed")] * 2 + [snap(a="ready")] * 2) * 8
    alerts = [m for m in feed(pa.AlertEngine(CFG), cycle) if m and "🚨" in m]
    assert len(alerts) == 1
    spaced = [m for m in feed(pa.AlertEngine(CFG), cycle, step=900) if m and "🚨" in m]
    assert len(spaced) >= 2


def test_hourly_cap_and_suppressed_summary():
    engine = pa.AlertEngine(pa.AlertConfig(confirm_checks=1, max_per_hour=2, cooldown=60))
    sent = 0
    for i in range(5):
        bots = [Bot(200 + j, f"x{j}", "crashed" if j == i else "ready", ["down"]) for j in range(5)]
        sent += engine.step(bots, waiting=0, running=1, limit=10, now=T0 + i * 60) is not None
    assert sent == 2
    healthy = [Bot(200 + j, f"x{j}") for j in range(5)]
    later = (engine.step(healthy, waiting=0, running=1, limit=10, now=T0 + 3700)
             or engine.step(healthy, waiting=0, running=1, limit=10, now=T0 + 3760))
    assert later is not None and "ارسال نشد" in later


def test_mute_suppresses_without_reannouncing_later():
    engine = pa.AlertEngine(CFG)
    engine.mute(3600, now=T0)
    assert feed(engine, [snap(a="crashed")] * 4) == [None] * 4
    engine.unmute()
    assert feed(engine, [snap(a="crashed")] * 3, start=T0 + 400) == [None] * 3
    msgs = feed(engine, [snap(a="crashed", z="crashed")] * 3, start=T0 + 800)
    assert any(m and "@z" in m and "ارسال نشد" in m for m in msgs)


def test_startup_problems_arrive_as_one_summary():
    first = next(m for m in feed(pa.AlertEngine(CFG), [snap(a="crashed", b="crashed")] * 3) if m)
    assert "راه‌اندازی" in first and "@a" in first and "@b" in first
    later = next(m for m in feed(pa.AlertEngine(CFG), [snap(a="ready")] * 5 + [snap(a="crashed")] * 3) if m)
    assert "راه‌اندازی" not in later


def test_capacity_alert_and_recovery():
    msgs = [m for m in feed(pa.AlertEngine(CFG), [snap(a="ready")] * 12,
                            waiting=lambda i: 3 if 1 <= i <= 8 else 0) if m]
    assert len(msgs) == 2
    assert "ZIKO_TENANT_MAX_CONCURRENT_POLLERS" in msgs[0] and "صف پولینگ خالی" in msgs[1]


def test_deleted_bot_is_forgotten():
    engine = pa.AlertEngine(CFG)
    feed(engine, [snap(a="crashed", b="ready")] * 3)
    engine.step(snap(b="ready"), waiting=0, running=1, limit=10, now=T0 + 999)
    assert engine.status(now=T0 + 999)["tracked"] == 1


def test_messages_are_escaped_and_bounded():
    engine = pa.AlertEngine(pa.AlertConfig(confirm_checks=1))
    evil = [Bot(1, "<script>x</script>", "crashed", ["<b>boom</b>"])]
    msg = engine.step(evil, waiting=0, running=1, limit=10, now=T0)
    assert "<script>" not in msg and "&lt;script&gt;" in msg and "<b>boom</b>" not in msg
    many = [Bot(i, f"bot{i}" * 4, "crashed", ["x" * 160]) for i in range(80)]
    msg = pa.AlertEngine(pa.AlertConfig(confirm_checks=1)).step(many, waiting=0, running=1, limit=10, now=T0)
    assert len(msg) <= 4096 and "65 ربات دیگر" in msg


def test_config_from_env():
    cfg = pa.AlertConfig.from_env({"ZIKO_ALERT_STATES": "crashed,blocked", "ZIKO_ALERT_INTERVAL_SECONDS": "30"})
    assert cfg.states == {"crashed", "blocked"} and cfg.interval == 30
    assert pa.AlertConfig.from_env({}).states == {"crashed", "token"}


@pytest.mark.parametrize("env", [{"ZIKO_ALERT_STATES": "crashed,exploded"},
                                 {"ZIKO_ALERT_INTERVAL_SECONDS": "1"},
                                 {"ZIKO_ALERT_MAX_PER_HOUR": "9999"}])
def test_invalid_config_is_rejected(env):
    with pytest.raises(ValueError):
        pa.AlertConfig.from_env(env)


@pytest.mark.parametrize("text,seconds", [("30m", 1800), ("2h", 7200), ("1d", 86400), ("45", 2700),
                                          ("", None), ("0m", None), ("8d", None), ("-1h", None),
                                          ("h", None), ("1w", None), ("١h", None)])
def test_parse_duration(text, seconds):
    assert pa.parse_duration(text) == seconds


def test_mute_file(tmp_path):
    path = tmp_path / "run" / "alerts.json"
    pa.save_mute(path, T0 + 123)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert pa.load_mute(path) == T0 + 123
    path.write_text("garbage")
    assert pa.load_mute(path) == 0.0


@dataclass
class Overview:
    bots: list
    waiting: int = 0
    running: int = 1
    limit: int = 10


async def test_loop_delivers_to_every_admin_and_survives_errors(tmp_path):
    snapshots = iter([Overview(snap(a="ready")), Overview(snap(a="crashed")), Overview(snap(a="crashed")),
                      None, Overview(snap(a="crashed"))])
    sent: list[int] = []
    sleeps = {"n": 0}

    async def overview():
        item = next(snapshots)
        if item is None:
            raise RuntimeError("database is locked")
        return item

    async def send(admin_id, _text):
        if admin_id == 2:
            raise RuntimeError("chat not found")
        sent.append(admin_id)

    async def fake_sleep(_seconds):
        sleeps["n"] += 1
        if sleeps["n"] > 5:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await pa.run_alert_loop(engine=pa.AlertEngine(pa.AlertConfig(confirm_checks=2)), overview=overview,
                                send=send, admin_ids=[3, 2, 1], mute_path=tmp_path / "none.json",
                                sleep=fake_sleep)
    assert sorted(sent) == [1, 3]


def test_alerts_are_opt_in():
    """Off unless ZIKO_PLATFORM_ALERTS=1 (v1.5.0 default change)."""
    from pathlib import Path

    main = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text()
    assert 'os.environ.get("ZIKO_PLATFORM_ALERTS", "0").strip() == "1"' in main

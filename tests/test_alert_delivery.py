"""v1.6: an alert that reaches no admin is retried, not lost."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from app import platform_alerts as A


@dataclass
class Bot:
    bot_id: int
    state: str
    username: str = "shop_bot"
    reasons: tuple[str, ...] = ("token revoked",)


@dataclass
class Overview:
    bots: list
    waiting: int = 0
    running: int = 1
    limit: int = 50


def engine() -> A.AlertEngine:
    return A.AlertEngine(A.AlertConfig(confirm_checks=1, cooldown=3600, max_per_hour=6))


def test_undo_makes_the_same_alert_come_back():
    e = engine()
    msg = e.step([Bot(1, "crashed")], waiting=0, running=1, limit=50, now=1000)
    assert msg and "shop_bot" in msg
    assert e.step([Bot(1, "crashed")], waiting=0, running=1, limit=50, now=1060) is None   # marked sent
    e2 = engine()
    e2.step([Bot(1, "crashed")], waiting=0, running=1, limit=50, now=1000)
    assert e2.undo_last() is True
    again = e2.step([Bot(1, "crashed")], waiting=0, running=1, limit=50, now=1060)
    assert again and "shop_bot" in again                                                  # retried
    assert e2.status(now=1060)["sent_last_hour"] == 1                                     # failed try not counted


def test_undo_restores_recovery_and_queue_state():
    e = engine()
    e.step([Bot(1, "crashed")], waiting=0, running=1, limit=50, now=1000)
    rec = e.step([Bot(1, "ready")], waiting=0, running=1, limit=50, now=1060)
    assert rec and "بهبود" in rec
    e.undo_last()
    assert "بهبود" in (e.step([Bot(1, "ready")], waiting=0, running=1, limit=50, now=1120) or "")
    q = engine()
    first = None
    for t in range(10):
        first = first or q.step([], waiting=3, running=50, limit=50, now=1000 + t)
    assert first and "ظرفیت" in first
    q.undo_last()
    assert "ظرفیت" in (q.step([], waiting=3, running=50, limit=50, now=2000) or "")


def test_undo_without_a_message_is_a_no_op():
    assert engine().undo_last() is False


async def _loop(send, admins, cycles=2):
    e = engine()
    calls = {"n": 0}

    async def overview():
        return Overview([Bot(1, "crashed")])

    async def sleep(_s):
        calls["n"] += 1
        if calls["n"] > cycles:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await A.run_alert_loop(engine=e, overview=overview, send=send, admin_ids=admins,
                               mute_path=__import__("pathlib").Path("/nonexistent/mute.json"), sleep=sleep)
    return e


def test_loop_retries_when_every_admin_failed():
    attempts = []

    async def send(admin_id, msg):
        attempts.append(admin_id)
        if len(attempts) == 1:
            raise RuntimeError("network down")

    asyncio.run(_loop(send, [7], cycles=2))
    assert attempts == [7, 7]                      # failed once, delivered on the next check


def test_loop_does_not_resend_when_one_admin_got_it():
    attempts = []

    async def send(admin_id, msg):
        attempts.append(admin_id)
        if admin_id == 8:
            raise RuntimeError("blocked")

    asyncio.run(_loop(send, [7, 8], cycles=3))
    assert attempts == [7, 8]                      # one success: no repeat

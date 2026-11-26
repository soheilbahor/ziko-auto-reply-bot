"""Daily owner report: due logic, text, storage counts, claim-once sending, panel."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app import daily_report as R


# --------------------------------------------------------------------------- pure
@pytest.mark.parametrize("hour,last,now,due", [
    (21, None, datetime(2026, 11, 26, 21, 0), True),
    (21, None, datetime(2026, 11, 26, 23, 59), True),          # late start still sends
    (21, None, datetime(2026, 11, 26, 20, 59), False),
    (21, date(2026, 11, 26), datetime(2026, 11, 26, 22, 0), False),
    (21, date(2026, 11, 25), datetime(2026, 11, 26, 22, 0), True),
])
def test_is_due(hour, last, now, due):
    assert R.is_due(enabled=True, hour=hour, last_sent=last, now_local=now) is due
    assert R.is_due(enabled=False, hour=hour, last_sent=last, now_local=now) is False


def test_text_has_counts_trend_and_no_raw_values():
    text = R.build_text({"inbound": 42, "contacts": 17, "sent": 15, "failed": 1, "uncertain": 2},
                        {"inbound": 30, "sent": 15}, date(2026, 11, 26), auto_reply_on=True)
    for part in ("۱۴۰۵/۰۹/۰۵", "<b>۴۲</b>", "▲ ۴۰٪", "مثل دیروز", "ناموفق", "نامشخص", "/logs"):
        assert part in text
    assert "{" not in text and not any(ch.isdigit() and ch.isascii() for ch in text.replace("/logs", ""))


def test_quiet_day_text():
    text = R.build_text({}, {}, date(2026, 11, 26), auto_reply_on=False, preview=True)
    assert "پیامی از مشتری نرسید" in text and "خاموش" in text and "پیش" in text


# --------------------------------------------------------------------------- storage
@pytest.mark.asyncio
async def test_report_settings_default_off_and_claim_once(db: Any) -> None:
    s = await db.get_report_settings()
    assert s == {"enabled": False, "hour": R.DEFAULT_HOUR, "last_sent": None}
    await db.set_report_settings(enabled=True, hour=9)
    day = date(2026, 11, 26)
    assert await db.claim_report_day(day) is True
    assert await db.claim_report_day(day) is False
    await db.release_report_day(day)
    assert await db.claim_report_day(day) is True
    assert (await db.get_report_settings())["last_sent"] == day
    from app.storage import InvalidReplyTextError
    with pytest.raises(InvalidReplyTextError):
        await db.set_report_settings(hour=24)


@pytest.mark.asyncio
async def test_report_counts_window(db: Any) -> None:
    from conftest import CONN_ID
    await db.claim_inbound_event(CONN_ID, 111, 1)
    await db.claim_inbound_event(CONN_ID, 111, 2)
    await db.claim_inbound_event(CONN_ID, 222, 3)
    now = datetime.now(UTC)
    counts = await db.report_counts(now - timedelta(hours=24), now + timedelta(seconds=5))
    assert counts["inbound"] == 3 and counts["contacts"] == 2
    old = await db.report_counts(now - timedelta(hours=48), now - timedelta(hours=24))
    assert old["inbound"] == 0


# --------------------------------------------------------------------------- sending
class FakeDb:
    def __init__(self, enabled=True, hour=21, last=None):
        self.settings = {"enabled": enabled, "hour": hour, "last_sent": last}
        self.released = []

    async def get_report_settings(self):
        return dict(self.settings)

    async def get_owner_settings(self):
        return SimpleNamespace(timezone="Asia/Tehran")

    async def claim_report_day(self, day):
        if self.settings["last_sent"] == day:
            return False
        self.settings["last_sent"] = day
        return True

    async def release_report_day(self, day):
        self.released.append(day)
        self.settings["last_sent"] = None

    async def report_counts(self, since, until):
        return {"inbound": 3, "contacts": 2, "sent": 2, "failed": 0, "uncertain": 0}

    async def get_app_state(self):
        return SimpleNamespace(enabled=True)


NIGHT = datetime(2026, 11, 26, 18, 0, tzinfo=UTC)      # 21:30 in Tehran


@pytest.mark.asyncio
async def test_sends_once_per_day_to_the_owner() -> None:
    pytest.importorskip("aiogram")
    db, bot = FakeDb(), AsyncMock()
    assert await R.run_once(db, bot, 555, NIGHT) is True
    assert await R.run_once(db, bot, 555, NIGHT + timedelta(minutes=1)) is False
    assert bot.send_message.await_count == 1
    assert bot.send_message.await_args.kwargs["chat_id"] == 555


@pytest.mark.asyncio
async def test_not_sent_when_off_or_early() -> None:
    pytest.importorskip("aiogram")
    bot = AsyncMock()
    assert await R.run_once(FakeDb(enabled=False), bot, 1, NIGHT) is False
    assert await R.run_once(FakeDb(hour=23), bot, 1, NIGHT) is False
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_send_is_retried_but_blocked_owner_is_not() -> None:
    aiogram_exc = pytest.importorskip("aiogram.exceptions")
    db, bot = FakeDb(), AsyncMock()
    bot.send_message.side_effect = RuntimeError("network")
    assert await R.run_once(db, bot, 1, NIGHT) is False
    assert db.released and db.settings["last_sent"] is None          # will retry
    db2 = FakeDb()
    bot.send_message.side_effect = aiogram_exc.TelegramForbiddenError(method=SimpleNamespace(), message="blocked")
    assert await R.run_once(db2, bot, 1, NIGHT) is False
    assert not db2.released and db2.settings["last_sent"] is not None  # not retried today


@pytest.mark.asyncio
async def test_loop_survives_errors_and_stops_on_cancel() -> None:
    import asyncio
    calls = []

    class Broken(FakeDb):
        async def get_report_settings(self):
            calls.append(1)
            raise RuntimeError("db locked")

    async def tick(_s):
        if len(calls) >= 3:
            raise asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await R.report_loop(Broken(), AsyncMock(), 1, sleep=tick)
    assert len(calls) == 3


# --------------------------------------------------------------------------- panel
@pytest.mark.asyncio
async def test_panel_toggle_hour_and_preview(admin: Any) -> None:
    pytest.importorskip("aiogram")
    from conftest import ADMIN_ID, STRANGER_ID
    from app.keyboards.admin import AdminCB

    def d(action, key=""):
        return AdminCB(action=action, key=key).pack()
    mark = admin.mark()
    await admin.send_text(ADMIN_ID, "/menu")
    assert d("rep") in admin.callback_data_in(admin.calls_since(mark))
    await admin.press(STRANGER_ID, d("rept"))
    assert (await admin.db.get_report_settings())["enabled"] is False
    await admin.press(ADMIN_ID, d("rept"))
    await admin.press(ADMIN_ID, d("reph", "9"))
    await admin.press(ADMIN_ID, d("reph", "4"))                     # not offered: ignored
    s = await admin.db.get_report_settings()
    assert s["enabled"] is True and s["hour"] == 9
    mark = admin.mark()
    await admin.press(ADMIN_ID, d("repn"))
    sent = [p for n, p in admin.calls_since(mark) if n == "SendMessage"]
    assert sent and "گزارش روزانه" in str(sent[0].get("text"))
    assert (await admin.db.get_report_settings())["last_sent"] is None   # preview is not today's report

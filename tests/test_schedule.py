"""Weekly hours, holidays, the typing pause (app/schedule.py) and their effect on replies."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from app import schedule as S

TEHRAN = "Asia/Tehran"
OFFICE = S.Behaviour(mode="outside", hours=dict(S.PRESET_HOURS))


def at(local: str) -> datetime:
    """A Tehran wall-clock time (e.g. "2026-11-28 10:00") as UTC."""
    from zoneinfo import ZoneInfo
    return datetime.fromisoformat(local).replace(tzinfo=ZoneInfo(TEHRAN)).astimezone(UTC)


# --------------------------------------------------------------------------- calendar
@pytest.mark.parametrize("jalali,gregorian", [
    ((1405, 1, 1), date(2026, 3, 21)), ((1404, 1, 1), date(2025, 3, 21)), ((1403, 1, 1), date(2024, 3, 20)),
    ((1405, 9, 5), date(2026, 11, 26)), ((1403, 12, 30), date(2025, 3, 20)), ((1399, 10, 11), date(2020, 12, 31)),
])
def test_jalali_known_dates(jalali, gregorian):
    assert S.jalali_to_date(*jalali) == gregorian
    assert S.date_to_jalali(gregorian) == jalali


def test_jalali_round_trip_over_fifty_years():
    start = date(1990, 1, 1)
    for i in range(0, 50 * 366, 7):
        d = start + timedelta(days=i)
        assert S.jalali_to_date(*S.date_to_jalali(d)) == d


def test_invalid_jalali_dates_are_rejected():
    for bad in ((1405, 13, 1), (1405, 7, 31), (1404, 12, 30)):    # 1404 is not a leap year
        with pytest.raises(ValueError):
            S.jalali_to_date(*bad)


def test_format_jalali_uses_persian_digits():
    assert S.format_jalali(date(2026, 11, 26)) == "۱۴۰۵/۰۹/۰۵"


# --------------------------------------------------------------------------- parsing
@pytest.mark.parametrize("text,expected", [
    ("9-17", (540, 1020)), ("۹ تا ۱۷", (540, 1020)), ("9:30-18", (570, 1080)), ("22-2", (1320, 120)),
    ("تعطیل", None), ("off", None), ("24", (0, 1440)), ("تمام روز", (0, 1440)), ("8 - 24", (480, 1440)),
])
def test_parse_hours(text, expected):
    assert S.parse_hours(text) == expected


@pytest.mark.parametrize("text", ["9-9", "25-3", "9:61-10", "abc", "", "24:30-1"])
def test_parse_hours_rejects_with_persian_message(text):
    with pytest.raises(S.ScheduleError) as err:
        S.parse_hours(text)
    assert err.value.persian


@pytest.mark.parametrize("text,expected", [
    ("1405/12/25", (date(2027, 3, 16), date(2027, 3, 16))),
    ("۱۴۰۵/۱۲/۲۵ تا ۱۴۰۶/۰۱/۱۳", (date(2027, 3, 16), date(2027, 4, 2))),
    ("1405/12/25-1406/01/13", (date(2027, 3, 16), date(2027, 4, 2))),
    ("2027-03-16 - 2027-03-20", (date(2027, 3, 16), date(2027, 3, 20))),
])
def test_parse_holiday(text, expected):
    assert S.parse_holiday(text) == expected


@pytest.mark.parametrize("text", ["1405/13/01", "1406/01/13 تا 1405/12/25", "1405/01/01 تا 1405/05/01",
                                  "1405/12/25 hello", "tomorrow"])
def test_parse_holiday_rejects(text):
    with pytest.raises(S.ScheduleError):
        S.parse_holiday(text)


# --------------------------------------------------------------------------- deciding
def test_always_mode_never_blocks():
    b = S.Behaviour(mode="always", hours={}, holidays=((date(2026, 11, 28), date(2026, 11, 28)),))
    assert S.reply_allowed(b, at("2026-11-28 10:00"), TEHRAN) == (True, "always")


@pytest.mark.parametrize("local,working", [
    ("2026-11-28 10:00", True),    # Saturday, in hours
    ("2026-11-28 08:59", False),   # Saturday, before opening
    ("2026-11-28 17:00", False),   # closing time is exclusive
    ("2026-11-26 12:59", True),    # Thursday half day
    ("2026-11-26 13:00", False),
    ("2026-11-27 10:00", False),   # Friday off
])
def test_office_week(local, working):
    assert S.is_open(OFFICE, at(local), TEHRAN)[0] is working
    assert S.reply_allowed(OFFICE, at(local), TEHRAN)[0] is (not working)       # mode = outside
    inside = S.Behaviour(mode="inside", hours=OFFICE.hours)
    assert S.reply_allowed(inside, at(local), TEHRAN)[0] is working


def test_holiday_counts_as_closed():
    b = S.Behaviour(mode="outside", hours=OFFICE.hours, holidays=((date(2026, 11, 28), date(2026, 11, 29)),))
    assert S.reply_allowed(b, at("2026-11-28 10:00"), TEHRAN) == (True, "holiday")
    assert S.reply_allowed(b, at("2026-11-30 10:00"), TEHRAN)[0] is False


def test_overnight_shift_continues_after_midnight():
    b = S.Behaviour(mode="inside", hours={0: (1320, 120)})    # Saturday 22:00-02:00
    assert S.is_open(b, at("2026-11-28 23:00"), TEHRAN)[0] is True
    assert S.is_open(b, at("2026-11-29 01:30"), TEHRAN)[0] is True       # Sunday, still Saturday's shift
    assert S.is_open(b, at("2026-11-29 02:30"), TEHRAN)[0] is False


def test_timezone_is_respected():
    utc_morning = datetime(2026, 11, 28, 6, 0, tzinfo=UTC)               # 09:30 in Tehran
    assert S.is_open(OFFICE, utc_morning, TEHRAN)[0] is True
    assert S.is_open(OFFICE, utc_morning, "Europe/London")[0] is False   # 06:00 in London
    assert S.is_open(OFFICE, utc_morning, "Not/AZone")[0] is True        # unknown zone -> Tehran


def test_explain_is_persian_and_never_raw():
    for local in ("2026-11-28 10:00", "2026-11-27 10:00", "2026-11-28 20:00"):
        line = S.explain(OFFICE, at(local), TEHRAN)
        assert line[0] in "✅⏸" and "{" not in line and "_" not in line


def test_typing_seconds():
    assert S.typing_seconds(0, 500) == 0
    assert S.typing_seconds(2, 0) == 2
    assert S.typing_seconds(4, 1000) == 7          # longer texts pause a little longer
    assert S.typing_seconds(7, 10_000) == S.MAX_TYPING


# --------------------------------------------------------------------------- storage
@pytest.mark.asyncio
async def test_storage_defaults_reproduce_earlier_behaviour(db: Any) -> None:
    pytest.importorskip("aiosqlite")
    b = await db.get_reply_behaviour()
    assert (b.typing_delay, b.mode, b.hours, b.holidays) == (0, "always", {}, ())


@pytest.mark.asyncio
async def test_storage_round_trip_and_validation(db: Any) -> None:
    from app.storage import InvalidReplyTextError
    with pytest.raises(InvalidReplyTextError):
        await db.set_schedule_mode("outside")              # no hours yet
    await db.set_working_hours(dict(S.PRESET_HOURS))
    await db.set_schedule_mode("outside")
    await db.set_typing_delay(4)
    await db.add_holiday(date(2027, 3, 16), date(2027, 4, 2))
    b = await db.get_reply_behaviour()
    assert b.mode == "outside" and b.typing_delay == 4 and b.hours == S.PRESET_HOURS
    assert b.holidays == ((date(2027, 3, 16), date(2027, 4, 2)),)
    assert await db.remove_holiday(date(2027, 3, 16), date(2027, 4, 2))
    with pytest.raises(InvalidReplyTextError):
        await db.set_typing_delay(11)
    with pytest.raises(InvalidReplyTextError):
        await db.set_working_hours({7: (0, 60)})


@pytest.mark.asyncio
async def test_old_holidays_are_pruned_on_add(db: Any) -> None:
    await db.add_holiday(date(2026, 1, 1), date(2026, 1, 2))
    await db.add_holiday(date(2027, 1, 1), date(2027, 1, 1), today=date(2026, 11, 26))
    assert (await db.get_reply_behaviour()).holidays == ((date(2027, 1, 1), date(2027, 1, 1)),)


# --------------------------------------------------------------------------- engine
def _messages(sender) -> list[str]:
    return [name.split(".")[-1] for name, _a, _k in sender.mock_calls if "(" not in name]


@pytest.mark.asyncio
async def test_engine_skips_reply_outside_the_schedule(armed: Any, monkeypatch) -> None:
    from conftest import make_message
    from app.services import auto_reply
    await armed.db.set_working_hours({d: (0, 1440) for d in range(7)})     # always "at work"
    await armed.db.set_schedule_mode("outside")                             # reply only when closed
    outcome = await armed.deliver(make_message(message_id=501))
    assert outcome is auto_reply.ReplyOutcome.IGNORED
    assert "send_message" not in _messages(armed.sender)
    assert armed.db.granted == []                                            # no claim was made


@pytest.mark.asyncio
async def test_engine_replies_when_schedule_allows(armed: Any) -> None:
    from conftest import make_message
    from app.services import auto_reply
    await armed.db.set_working_hours({d: (0, 1440) for d in range(7)})
    await armed.db.set_schedule_mode("inside")
    assert await armed.deliver(make_message(message_id=502)) is auto_reply.ReplyOutcome.SENT


@pytest.mark.asyncio
async def test_engine_shows_typing_before_the_reply(armed: Any) -> None:
    from conftest import CONN_ID, make_message
    from app.services import auto_reply
    pauses: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        pauses.append(seconds)
    armed.engine._sleep = fake_sleep
    armed.engine.step_delay = 0
    await armed.db.set_typing_delay(7)
    assert await armed.deliver(make_message(message_id=503)) is auto_reply.ReplyOutcome.SENT
    calls = _messages(armed.sender)
    assert calls.index("send_chat_action") < calls.index("send_message")
    action = next(c for c in armed.sender.mock_calls if c[0] == "send_chat_action")
    assert action.kwargs["business_connection_id"] == CONN_ID and action.kwargs["action"] == "typing"
    assert sum(pauses) >= 7 and max(pauses) <= S.TYPING_REFRESH     # renewed, never longer than 4.5 s
    assert calls.count("send_chat_action") >= 2


@pytest.mark.asyncio
async def test_refused_chat_action_never_stops_the_reply(armed: Any) -> None:
    from conftest import make_message
    from app.services import auto_reply

    async def no_sleep(_s: float) -> None:
        return None
    armed.engine._sleep = no_sleep
    armed.sender.send_chat_action.side_effect = RuntimeError("not allowed")
    await armed.db.set_typing_delay(2)
    assert await armed.deliver(make_message(message_id=504)) is auto_reply.ReplyOutcome.SENT


@pytest.mark.asyncio
async def test_switching_off_during_the_pause_sends_nothing(armed: Any) -> None:
    from conftest import make_message
    from app.services import auto_reply

    async def owner_switches_off(_s: float) -> None:
        await armed.db.disable_auto_reply()
    armed.engine._sleep = owner_switches_off
    await armed.db.set_typing_delay(2)
    assert await armed.deliver(make_message(message_id=505)) is auto_reply.ReplyOutcome.IGNORED
    assert "send_message" not in _messages(armed.sender)
    armed.db.assert_every_claim_settled_once()


@pytest.mark.asyncio
async def test_without_typing_delay_no_chat_action_is_sent(armed: Any) -> None:
    from conftest import make_message
    await armed.deliver(make_message(message_id=506))
    assert "send_chat_action" not in _messages(armed.sender)

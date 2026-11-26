"""Owner panel "⏰ زمان‌بندی و تایپ" through the real admin router (recording session, no network)."""
from __future__ import annotations

from datetime import date
from typing import Any

import pytest

pytest.importorskip("aiogram")

from conftest import ADMIN_ID, STRANGER_ID  # noqa: E402

from app import schedule  # noqa: E402
from app.keyboards.admin import AdminCB  # noqa: E402


def data(action: str, key: str = "") -> str:
    return AdminCB(action=action, key=key).pack()


def texts(calls) -> list[str]:
    return [str(p.get("text", "")) for name, p in calls if name in ("SendMessage", "EditMessageText")]


@pytest.mark.asyncio
async def test_main_menu_has_the_schedule_button(admin: Any) -> None:
    mark = admin.mark()
    await admin.send_text(ADMIN_ID, "/menu")
    assert data("sch") in admin.callback_data_in(admin.calls_since(mark))


@pytest.mark.asyncio
async def test_screen_shows_settings_and_every_callback_fits(admin: Any) -> None:
    mark = admin.mark()
    await admin.press(ADMIN_ID, data("sch"))
    calls = admin.calls_since(mark)
    assert any("زمان" in t and "در حال نوشتن" in t for t in texts(calls))
    found = admin.callback_data_in(calls)
    assert data("scht", "4") in found and data("schm", "outside") in found
    assert all(len(d.encode()) <= 64 for d in found)


@pytest.mark.asyncio
async def test_typing_and_mode_buttons_are_saved(admin: Any) -> None:
    await admin.press(ADMIN_ID, data("scht", "4"))
    assert (await admin.db.get_reply_behaviour()).typing_delay == 4
    await admin.press(ADMIN_ID, data("schm", "outside"))          # refused: no hours yet
    assert (await admin.db.get_reply_behaviour()).mode == "always"
    await admin.press(ADMIN_ID, data("schp"))                      # office-week preset
    await admin.press(ADMIN_ID, data("schm", "outside"))
    b = await admin.db.get_reply_behaviour()
    assert b.mode == "outside" and b.hours == schedule.PRESET_HOURS


@pytest.mark.asyncio
async def test_forged_values_are_refused(admin: Any) -> None:
    for action, key in (("scht", "9"), ("scht", "x"), ("schm", "never"), ("schday", "7"), ("schhx", "bad")):
        await admin.press(ADMIN_ID, data(action, key))
    b = await admin.db.get_reply_behaviour()
    assert (b.typing_delay, b.mode, b.hours, b.holidays) == (0, "always", {}, ())


@pytest.mark.asyncio
async def test_day_hours_by_text(admin: Any) -> None:
    await admin.press(ADMIN_ID, data("schday", "6"))
    await admin.send_text(ADMIN_ID, "۱۰ تا ۱۴")
    assert (await admin.db.get_reply_behaviour()).hours[6] == (600, 840)
    await admin.press(ADMIN_ID, data("schday", "6"))
    mark = admin.mark()
    await admin.send_text(ADMIN_ID, "hello")                        # invalid: nothing changes
    assert (await admin.db.get_reply_behaviour()).hours[6] == (600, 840)
    assert any("قالب درست نیست" in t for t in texts(admin.calls_since(mark)))
    await admin.send_text(ADMIN_ID, "تعطیل")                        # still waiting: accepted now
    assert (await admin.db.get_reply_behaviour()).hours[6] is None


@pytest.mark.asyncio
async def test_holiday_add_and_remove(admin: Any) -> None:
    await admin.press(ADMIN_ID, data("schha"))
    await admin.send_text(ADMIN_ID, "1415/01/01 تا 1415/01/13")
    start, end = schedule.jalali_to_date(1415, 1, 1), schedule.jalali_to_date(1415, 1, 13)
    assert (await admin.db.get_reply_behaviour()).holidays == ((start, end),)
    await admin.press(ADMIN_ID, data("schhx", f"{start.isoformat()}|{end.isoformat()}"))
    assert (await admin.db.get_reply_behaviour()).holidays == ()


@pytest.mark.asyncio
async def test_past_holiday_is_refused(admin: Any) -> None:
    await admin.press(ADMIN_ID, data("schha"))
    await admin.send_text(ADMIN_ID, "1390/01/01")
    assert (await admin.db.get_reply_behaviour()).holidays == ()


@pytest.mark.asyncio
async def test_stranger_changes_nothing(admin: Any) -> None:
    for action, key in (("scht", "4"), ("schp", ""), ("schm", "always"), ("schday", "0"), ("schha", "")):
        await admin.press(STRANGER_ID, data(action, key))
    await admin.send_text(STRANGER_ID, "9-17")
    await admin.db.add_holiday(date(2099, 1, 1), date(2099, 1, 1))
    await admin.press(STRANGER_ID, data("schhx", "2099-01-01|2099-01-01"))
    b = await admin.db.get_reply_behaviour()
    assert (b.typing_delay, b.hours) == (0, {}) and len(b.holidays) == 1

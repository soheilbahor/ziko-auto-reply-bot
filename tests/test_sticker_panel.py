"""Owner panel "🎭 استیکر پاسخ": a sticker is appended to the ACTIVE reply, through the
real admin router (recording aiogram session from conftest, no network)."""
from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("aiogram")

from conftest import ADMIN_ID, MSG_TS, STRANGER_ID  # noqa: E402

from app.keyboards.admin import AdminCB  # noqa: E402

STICKER_ID = "CAACAgIAAxkBAAIBTestStickerFileIdForTestsOnly"


async def send_sticker(admin: Any, user_id: int, file_id: str = STICKER_ID) -> None:
    from aiogram.types import Update
    message = {
        "message_id": admin._next_message(), "date": MSG_TS,
        "chat": {"id": user_id, "type": "private", "first_name": "U"},
        "from": admin._user(user_id),
        "sticker": {"file_id": file_id, "file_unique_id": "uniq-1", "type": "regular",
                    "width": 512, "height": 512, "is_animated": False, "is_video": False},
    }
    await admin.dispatcher.feed_update(admin.bot, Update.model_validate(
        {"update_id": admin._next_update(), "message": message}))


async def activate_first_status(admin: Any) -> str:
    statuses = await admin.db.list_statuses()
    await admin.db.activate_status(statuses[0].key)
    return statuses[0].key


def press_data(action: str) -> str:
    return AdminCB(action=action, key="").pack()


def texts(calls) -> list[str]:
    return [str(p.get("text", "")) for name, p in calls if name in ("SendMessage", "EditMessageText")]


@pytest.mark.asyncio
async def test_main_menu_has_the_sticker_button(admin: Any) -> None:
    mark = admin.mark()
    await admin.send_text(ADMIN_ID, "/menu")
    assert press_data("stk") in admin.callback_data_in(admin.calls_since(mark))


@pytest.mark.asyncio
async def test_sticker_is_appended_to_the_active_reply(admin: Any) -> None:
    key = await activate_first_status(admin)
    before = await sent_parts(admin, key)
    await admin.press(ADMIN_ID, press_data("stk"))
    mark = admin.mark()
    await send_sticker(admin, ADMIN_ID)
    assert await sent_parts(admin, key) == [*before, ("sticker", STICKER_ID)]
    assert any("اضافه شد" in t for t in texts(admin.calls_since(mark)))
    assert all(STICKER_ID not in t for t in texts(admin.calls_since(mark)))


@pytest.mark.asyncio
async def test_without_an_active_reply_nothing_is_stored(admin: Any) -> None:
    await admin.db.disable_auto_reply()
    state = await admin.db.get_app_state()
    if state.active_status_key:
        pytest.skip("default database keeps an active status after disabling")
    await admin.press(ADMIN_ID, press_data("stk"))
    await send_sticker(admin, ADMIN_ID)
    for s in await admin.db.list_statuses():
        assert all(step.kind != "sticker" for step in await admin.db.list_reply_steps(s.key))


@pytest.mark.asyncio
async def test_text_while_waiting_is_not_stored(admin: Any) -> None:
    key = await activate_first_status(admin)
    before = await admin.db.list_reply_steps(key)
    await admin.press(ADMIN_ID, press_data("stk"))
    mark = admin.mark()
    await admin.send_text(ADMIN_ID, "not a sticker")
    assert await admin.db.list_reply_steps(key) == before
    assert any("استیکر بفرستید" in t for t in texts(admin.calls_since(mark)))


@pytest.mark.asyncio
async def sent_parts(admin: Any, key: str) -> list[tuple[str, str]]:
    """What the engine sends: the stored steps, or reply_text when there are none."""
    steps = [(s.kind, s.content) for s in await admin.db.list_reply_steps(key)]
    if steps:
        return steps
    status = await admin.db.get_status(key)
    return [("text", status.reply_text)]


@pytest.mark.asyncio
async def test_clear_removes_only_sticker_steps(admin: Any) -> None:
    key = await activate_first_status(admin)
    before = await sent_parts(admin, key)
    await admin.press(ADMIN_ID, press_data("stk"))
    await send_sticker(admin, ADMIN_ID)
    assert await sent_parts(admin, key) == [*before, ("sticker", STICKER_ID)]
    await admin.press(ADMIN_ID, press_data("stkx"))
    assert await sent_parts(admin, key) == before


@pytest.mark.asyncio
async def test_sticker_on_a_text_only_reply_keeps_the_text(admin: Any) -> None:
    """Regression (found on staging, 1.6.0): a reply stored only as reply_text has no
    step rows; adding a sticker used to store [sticker] alone and drop the text."""
    key = await activate_first_status(admin)
    status = await admin.db.get_status(key)
    text = status.reply_text
    assert text.strip()
    await admin.press(ADMIN_ID, press_data("stk"))
    await send_sticker(admin, ADMIN_ID)
    parts = await sent_parts(admin, key)
    assert parts[0] == ("text", text) and parts[-1] == ("sticker", STICKER_ID)


@pytest.mark.asyncio
async def test_stranger_cannot_add_or_remove_stickers(admin: Any) -> None:
    key = await activate_first_status(admin)
    before = await admin.db.list_reply_steps(key)
    for action in ("stk", "stkx"):
        await admin.press(STRANGER_ID, press_data(action))
    await send_sticker(admin, STRANGER_ID)
    assert await admin.db.list_reply_steps(key) == before


@pytest.mark.asyncio
async def test_sticker_limit_is_respected(admin: Any) -> None:
    key = await activate_first_status(admin)
    steps = [(s.kind, s.content) for s in await admin.db.list_reply_steps(key)]
    steps += [("sticker", f"{STICKER_ID}{i}") for i in range(10 - len(steps))]
    await admin.db.replace_reply_steps(key, steps)
    await admin.press(ADMIN_ID, press_data("stk"))
    await send_sticker(admin, ADMIN_ID, STICKER_ID + "x")
    assert len(await admin.db.list_reply_steps(key)) == 10

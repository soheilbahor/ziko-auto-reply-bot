"""Reply-library callback schema: typed fields, size limit, navigation, forged payloads.

Unit tests use the real view builders; the dispatcher tests drive the real admin
router through the recording aiogram session from conftest (no network).
"""
from __future__ import annotations

import re
from typing import Any

import pytest

pytest.importorskip("aiogram")

from conftest import ADMIN_ID, STRANGER_ID  # noqa: E402

from app import reply_library as rl  # noqa: E402
from app.handlers import library as panel  # noqa: E402

LIB = rl.load()


def all_buttons(kb) -> list[Any]:
    return [b for row in kb.inline_keyboard for b in row]


def every_view():
    yield panel.groups_view(LIB)
    yield panel.favourites_view(LIB, [m.id for g in LIB.groups for c in g.categories for m in c.messages][:20])
    for g in LIB.groups:
        yield panel.group_view(LIB, g.id)
        for c in g.categories:
            for page in range(-(-len(c.messages) // panel.PER_PAGE)):
                yield panel.category_view(LIB, c.id, set(), page=page)
            for m in c.messages:
                yield panel.message_view(LIB, m.id, False)


# --------------------------------------------------------------------------- schema
def test_every_button_packs_within_64_bytes_and_round_trips():
    count = 0
    for _text, kb in every_view():
        for button in all_buttons(kb):
            data = button.callback_data
            assert len(data.encode()) <= 64, data
            if data.startswith(panel.PREFIX + ":"):
                parsed = panel.LibCB.unpack(data)
                assert parsed.action in panel.ACTIONS
                assert ":" not in parsed.item
            count += 1
    assert count > 100


def test_category_and_page_are_separate_typed_fields():
    data = panel.LibCB(action=panel.LIB_CAT, item="store_welcome", page=2).pack()
    parsed = panel.LibCB.unpack(data)
    assert (parsed.item, parsed.page) == ("store_welcome", 2)
    assert isinstance(parsed.page, int)


def test_value_containing_separator_is_refused_by_aiogram():
    with pytest.raises(ValueError):
        panel.LibCB(action=panel.LIB_CAT, item="a:b").pack()


@pytest.mark.parametrize("bad", ["Store", "a:b", "x" * 33, "", "ä"])
def test_library_rejects_ids_unsafe_for_callback_data(bad):
    data = {"groups": [{"id": "g", "title": "G", "emoji": "x", "categories": [
        {"id": bad, "title": "T", "emoji": "x", "messages": [
            {"id": f"{bad}.1", "tone": next(iter(rl.TONES)), "text": "سلام"}]}]}]}
    with pytest.raises(rl.LibraryError):
        rl.parse(data)


# --------------------------------------------------------------------------- navigation
def test_pagination_next_and_previous_point_at_adjacent_pages():
    c = max((c for g in LIB.groups for c in g.categories), key=lambda c: len(c.messages))
    pages = -(-len(c.messages) // panel.PER_PAGE)
    if pages < 2:
        pytest.skip("no category in the shipped library has more than one page")
    for page in range(pages):
        _text, kb = panel.category_view(LIB, c.id, set(), page=page)
        targets = {panel.LibCB.unpack(b.callback_data).page for b in all_buttons(kb)
                   if b.callback_data.startswith("zl:") and
                   panel.LibCB.unpack(b.callback_data).action == panel.LIB_CAT}
        expected = {p for p in (page - 1, page + 1) if 0 <= p < pages}
        assert targets == expected


@pytest.mark.parametrize("page", [-5, 999, 10**6])
def test_out_of_range_page_is_clamped(page):
    text, _kb = panel.category_view(LIB, "store_welcome", set(), page=page)
    assert re.search(r"صفحه \d+/\d+", text)


def test_group_buttons_open_their_categories():
    g = LIB.groups[0]
    _text, kb = panel.group_view(LIB, g.id)
    items = [panel.LibCB.unpack(b.callback_data) for b in all_buttons(kb)]
    cats = [i.item for i in items if i.action == panel.LIB_CAT]
    assert cats == [c.id for c in g.categories]
    assert all(i.page == 0 for i in items if i.action == panel.LIB_CAT)


# --------------------------------------------------------------------------- dispatcher
def _answers(calls) -> list[dict[str, Any]]:
    return [payload for name, payload in calls if name == "AnswerCallbackQuery"]


def _edits(calls) -> list[dict[str, Any]]:
    return [payload for name, payload in calls if name in ("EditMessageText", "SendMessage")]


@pytest.mark.asyncio
async def test_admin_can_navigate_category_pages(admin: Any) -> None:
    await admin.send_text(ADMIN_ID, "/start")
    mark = admin.mark()
    await admin.press(ADMIN_ID, panel.LibCB(action=panel.LIB_CAT, item="store_welcome", page=1).pack())
    calls = admin.calls_since(mark)
    assert any("صفحه 2/" in str(e.get("text", "")) for e in _edits(calls)), calls


@pytest.mark.asyncio
@pytest.mark.parametrize("data", [
    "zl:libc:store_welcome:notanumber",      # page is not an int
    "zl:libc:store_welcome",                 # missing field
    "zl:libc:store_welcome:1:extra",         # extra field
    "zl:nope:store_welcome:0",               # unknown action
    "zl:",                                   # empty
])
async def test_malformed_payload_is_answered_and_changes_nothing(admin: Any, data: str) -> None:
    before = await admin.snapshot()
    mark = admin.mark()
    await admin.press(ADMIN_ID, data)
    calls = admin.calls_since(mark)
    assert _answers(calls), "a malformed button must still be answered (no endless spinner)"
    assert await admin.snapshot() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("action", sorted(panel.ACTIONS))
async def test_stranger_cannot_use_any_library_action(admin: Any, action: str) -> None:
    await admin.db.disable_auto_reply()
    before = await admin.snapshot()
    mark = admin.mark()
    await admin.press(STRANGER_ID, panel.LibCB(action=action, item="store_welcome.1", page=0).pack())
    calls = admin.calls_since(mark)
    assert admin.callback_data_in(calls) == [], f"{action} exposed library controls to a non-admin"
    assert not any("<blockquote>" in str(e.get("text", "")) for e in _edits(calls))
    assert await admin.snapshot() == before
    state = await admin.db.get_app_state()
    assert state.enabled is False


@pytest.mark.asyncio
async def test_stranger_cannot_finish_library_edit_via_forged_state(admin: Any) -> None:
    """Even if an FSM state were set for a stranger, the router-level filter drops the text."""
    before = await admin.snapshot()
    await admin.press(STRANGER_ID, panel.LibCB(action=panel.LIB_EDIT, item="store_welcome.1").pack())
    await admin.send_text(STRANGER_ID, "متن جعلی")
    assert await admin.snapshot() == before


def test_library_back_button_returns_to_the_main_menu():
    """Regression: the library's Back opened the Advanced panel instead of the main menu."""
    from app.keyboards.admin import Action, AdminCB
    _text, kb = panel.groups_view(LIB)
    back = all_buttons(kb)[-1]
    assert AdminCB.unpack(back.callback_data).action == Action.MENU
    assert "بازگشت" in back.text



def test_onboarding_keyboard_offers_check_library_and_menu():
    from app.keyboards.admin import AdminCB, onboarding_keyboard
    buttons = [b for row in onboarding_keyboard().inline_keyboard for b in row]
    actions = [AdminCB.unpack(b.callback_data).action for b in buttons]
    assert actions == ["diag", "lib", "menu"]

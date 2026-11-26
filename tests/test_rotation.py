"""Reply rotation and online/offline availability (app/rotation.py, storage, engine, panel)."""
from __future__ import annotations

from collections import Counter

import pytest

from app import rotation


# --------------------------------------------------------------------------- pure logic
def test_sequence_is_round_robin_and_bounded():
    cursor, picked = 0, []
    for _ in range(7):
        index, cursor = rotation.choose(3, "sequence", cursor)
        picked.append(index)
    assert picked == [0, 1, 2, 0, 1, 2, 0]
    _, wrapped = rotation.choose(3, "sequence", 3 * 1_000_000 - 1)
    assert wrapped == 0                                   # cursor never grows without bound


def test_random_uses_the_injected_source_and_keeps_cursor():
    assert rotation.choose(5, "random", 7, randbelow=lambda n: 4) == (4, 7)


def test_random_is_roughly_uniform():
    counts = Counter(rotation.choose(4, "random", 0)[0] for _ in range(4000))
    assert set(counts) == {0, 1, 2, 3} and min(counts.values()) > 800


@pytest.mark.parametrize("count,mode", [(0, "random"), (3, "shuffle")])
def test_invalid_inputs(count, mode):
    with pytest.raises(ValueError):
        rotation.choose(count, mode, 0)


def test_other_state():
    assert rotation.other("online") == "offline" and rotation.other("offline") == "online"


# --------------------------------------------------------------------------- real storage
@pytest.fixture
async def db(tmp_path):
    pytest.importorskip("aiosqlite")
    from app.storage import Database

    database = Database(tmp_path / "bot-1.sqlite3")
    await database.initialize()
    yield database
    await database.close()


async def test_variants_roundtrip_and_off(db):
    status = (await db.list_statuses())[0]
    assert await db.get_reply_variants(status.key) == (None, [])
    assert await db.next_reply_variant(status.key) is None          # off: caller uses normal steps
    await db.set_reply_variants(status.key, ["سلام {name} ۱", "سلام ۲", "سلام ۳"], "sequence")
    assert await db.get_reply_variants(status.key) == ("sequence", ["سلام {name} ۱", "سلام ۲", "سلام ۳"])
    picked = [await db.next_reply_variant(status.key) for _ in range(4)]
    assert picked == ["سلام {name} ۱", "سلام ۲", "سلام ۳", "سلام {name} ۱"]
    await db.set_reply_variants(status.key, ["فقط یکی"], "random")   # < 2 variants = off
    assert await db.get_reply_variants(status.key) == (None, [])


async def test_variant_validation(db):
    from app.storage import InvalidReplyTextError

    key = (await db.list_statuses())[0].key
    with pytest.raises(InvalidReplyTextError):
        await db.set_reply_variants(key, ["a", "b"], "shuffle")
    with pytest.raises(InvalidReplyTextError):
        await db.set_reply_variants(key, ["a", "  "], "random")
    with pytest.raises(InvalidReplyTextError):
        await db.set_reply_variants(key, ["x"] * (rotation.MAX_VARIANTS + 1), "random")


async def test_sequence_cursor_is_atomic_under_concurrency(db):
    import asyncio

    key = (await db.list_statuses())[0].key
    await db.set_reply_variants(key, ["A", "B", "C", "D"], "sequence")
    picked = await asyncio.gather(*(db.next_reply_variant(key) for _ in range(40)))
    assert Counter(picked) == {"A": 10, "B": 10, "C": 10, "D": 10}


async def test_deleting_a_status_removes_its_variants_and_unlinks_it(db):
    # delete_status is a SOFT delete (ON DELETE CASCADE never fires): cleanup is explicit.
    status = await db.create_status(title="موقت", emoji="🧪", steps=[("text", "x")])
    keep = (await db.list_statuses())[0].key
    await db.set_reply_variants(status.key, ["a", "b"], "random")
    await db.set_availability_keys(online_key=status.key, offline_key=keep)
    await db.delete_status(status.key)
    assert await db.get_reply_variants(status.key) == (None, [])
    assert (await db.get_availability())["online_key"] is None
    with pytest.raises(Exception):
        await db.set_availability("online")                 # a hidden status is never re-activated


async def test_availability_switch_starts_a_new_session(db):
    statuses = await db.list_statuses()
    online, offline = statuses[0].key, statuses[1].key
    with pytest.raises(Exception):
        await db.set_availability("online")                         # nothing linked yet
    await db.set_availability_keys(online_key=online, offline_key=offline)
    first = await db.set_availability("online")
    assert first.enabled and first.active_status_key == online
    second = await db.set_availability("offline")
    assert second.active_status_key == offline and second.session_id != first.session_id
    assert (await db.get_availability())["state"] == "offline"


async def test_availability_rejects_same_status(db):
    from app.storage import InvalidReplyTextError

    key = (await db.list_statuses())[0].key
    with pytest.raises(InvalidReplyTextError):
        await db.set_availability_keys(online_key=key, offline_key=key)


# --------------------------------------------------------------------------- engine
async def test_engine_sends_a_rotated_variant_once_per_contact(tmp_path):
    pytest.importorskip("aiosqlite")
    pytest.importorskip("aiogram")
    from types import SimpleNamespace

    from app.services.auto_reply import BusinessMessageEngine
    from app.storage import Database

    db = Database(tmp_path / "bot-1.sqlite3")
    await db.initialize()
    sent: list[str] = []

    class Sender:
        async def send_message(self, *, chat_id, text, **_k):
            sent.append(text)
            return SimpleNamespace(message_id=len(sent))

    try:
        await db.save_business_connection("c1", True, True, 1)
        key = (await db.list_statuses())[0].key
        await db.activate_status(key)
        await db.set_reply_variants(key, ["A {name}", "B {name}", "C {name}"], "sequence")
        engine = BusinessMessageEngine(db=db, sender=Sender(),
                                       settings=SimpleNamespace(admin_ids=frozenset({1}), business_owner_id=1))
        engine.step_delay = 0

        def msg(chat: int, mid: int):
            return SimpleNamespace(business_connection_id="c1", message_id=mid, edit_date=None, text="hi",
                                   chat=SimpleNamespace(id=chat, type="private"),
                                   from_user=SimpleNamespace(id=chat, is_bot=False, first_name=f"U{chat}"))

        for chat in (10, 11, 12, 13):
            await engine.handle_business_message(msg(chat, 1))
        await engine.handle_business_message(msg(10, 2))            # same contact, same session
        await engine.handle_business_message(msg(11, 1))            # duplicate update
        assert sent == ["A U10", "B U11", "C U12", "A U13"]          # cursor advanced only on real sends
    finally:
        await db.close()


# --------------------------------------------------------------------------- panel views
def test_panel_views():
    pytest.importorskip("aiogram")
    from types import SimpleNamespace

    from app.handlers import rotation_panel as rp

    status = SimpleNamespace(key="away", title="بیرون از خانه", emoji="🚶", reply_text="x")
    text, kb = rp.rotation_view(status, None, [])
    assert "خاموش" in text and len(kb.inline_keyboard) == 2
    text, kb = rp.rotation_view(status, "random", ["سلام {name}", "<b>x</b>"])
    assert "تصادفی" in text and "{" not in text.split("</b>", 1)[1] and "&lt;b&gt;" in text
    assert rp.availability_row({"online_key": "a", "offline_key": None, "state": "offline"}) is None
    row = rp.availability_row({"online_key": "a", "offline_key": "b", "state": "offline"})
    assert "آفلاینم" in row[0].text
    for rows in (kb.inline_keyboard, rp.availability_view(
            {"online_key": "a", "offline_key": "b", "state": "online"}, [status])[1].inline_keyboard):
        for r in rows:
            for button in r:
                assert len(button.callback_data.encode()) <= 64

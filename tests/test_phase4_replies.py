"""Phase 4: status CRUD, reply steps, policy gates, personalised and multi-part replies.

Telegram is never contacted: the sender is a recording fake that can raise on chosen calls.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendMessage
from conftest import ADMIN_ID, CONN_ID, OWNER_ID, make_message

from app.services.auto_reply import (
    DEFAULT_CONTACT_NAME,
    BusinessMessageEngine,
    ReplyOutcome,
    render_template,
)
from app.storage import Database, InvalidReplyTextError, StorageError, UnknownStatusError


class RecordingSender:
    """Records every call. ``plan`` lists per-call results: None = ok, or an exception to raise."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.plan: list[BaseException | None] = []

    def _next(self, name: str, kwargs: dict[str, Any]) -> SimpleNamespace:
        self.calls.append((name, kwargs))
        outcome = self.plan.pop(0) if self.plan else None
        if outcome is not None:
            raise outcome
        return SimpleNamespace(message_id=700 + len(self.calls))

    async def send_message(self, **kwargs: Any) -> SimpleNamespace:
        return self._next("message", kwargs)

    async def send_sticker(self, **kwargs: Any) -> SimpleNamespace:
        return self._next("sticker", kwargs)


class Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.db = Database(tmp_path / "phase4.sqlite3")
        self.sender = RecordingSender()
        self.engine: BusinessMessageEngine | None = None

    async def start(self) -> None:
        await self.db.initialize()
        await self.db.save_business_connection(CONN_ID, True, True, OWNER_ID)
        await self.db.save_owner_settings(timezone="Asia/Tehran", language="fa", owner_name="علی")
        settings = SimpleNamespace(admin_ids=frozenset({ADMIN_ID}), business_owner_id=OWNER_ID)
        self.engine = BusinessMessageEngine(db=self.db, sender=self.sender, settings=settings)
        self.engine.step_delay = 0

    async def status(self, steps: list[tuple[str, str]], **policy: Any) -> str:
        created = await self.db.create_status(title="در جلسه", emoji="📅", steps=steps, **policy)
        await self.db.activate_status(created.key)
        return created.key

    async def deliver(self, message: Any) -> ReplyOutcome:
        assert self.engine is not None
        return await self.engine.handle_business_message(message)


# --------------------------------------------------------------------------- #
# Storage: CRUD, validation, owner settings, exceptions
# --------------------------------------------------------------------------- #
def test_create_status_gets_a_generated_key_and_its_steps(tmp_path: Path) -> None:
    async def run() -> None:
        db = Database(tmp_path / "crud.sqlite3")
        await db.initialize()
        created = await db.create_status(
            title="رانندگی", emoji="🚗", steps=[("text", "الان رانندگی می‌کنم"), ("sticker", "FILE_ID_1")],
        )
        assert created.key.startswith("custom_") and len(created.key) == len("custom_") + 8
        assert created.reply_mode == "reply" and created.is_active is True
        steps = await db.list_reply_steps(created.key)
        assert [(s.position, s.kind, s.content) for s in steps] == [
            (1, "text", "الان رانندگی می‌کنم"), (2, "sticker", "FILE_ID_1"),
        ]
        assert created.reply_text == "الان رانندگی می‌کنم"
        await db.close()

    asyncio.run(run())


def test_delete_is_soft_and_refused_for_the_active_status(tmp_path: Path) -> None:
    async def run() -> None:
        db = Database(tmp_path / "delete.sqlite3")
        await db.initialize()
        active = await db.create_status(title="الف", emoji="🔵", steps=[("text", "متن الف")])
        other = await db.create_status(title="ب", emoji="🟢", steps=[("text", "متن ب")])
        await db.activate_status(active.key)
        with pytest.raises(StorageError):
            await db.delete_status(active.key)
        await db.delete_status(other.key)
        keys = [s.key for s in await db.list_statuses()]
        assert other.key not in keys and active.key in keys
        with pytest.raises(UnknownStatusError):
            await db.activate_status(other.key)
        await db.close()

    asyncio.run(run())


@pytest.mark.parametrize("steps", [
    [],
    [("text", "")],
    [("video", "x")],
    [("sticker", "has space")],
    [("text", "x")] * 11,
])
def test_invalid_steps_are_refused(tmp_path: Path, steps: list[tuple[str, str]]) -> None:
    async def run() -> None:
        db = Database(tmp_path / "bad.sqlite3")
        await db.initialize()
        with pytest.raises(InvalidReplyTextError):
            await db.create_status(title="t", emoji="e", steps=steps)
        await db.close()

    asyncio.run(run())


def test_owner_settings_and_exceptions(tmp_path: Path) -> None:
    async def run() -> None:
        db = Database(tmp_path / "owner.sqlite3")
        await db.initialize()
        defaults = await db.get_owner_settings()
        assert defaults.timezone == "Asia/Tehran" and defaults.language == "fa"
        saved = await db.save_owner_settings(timezone="Europe/Berlin", language="en", owner_name="Ali")
        assert saved.timezone == "Europe/Berlin" and saved.owner_name == "Ali"
        with pytest.raises(InvalidReplyTextError):
            await db.save_owner_settings(timezone="Mars/Olympus", language="fa", owner_name="")
        with pytest.raises(InvalidReplyTextError):
            await db.save_owner_settings(timezone="Asia/Tehran", language="de", owner_name="")
        await db.add_reply_exception(4242)
        await db.add_reply_exception(4242)  # idempotent
        assert await db.is_reply_exception(4242) and await db.list_reply_exceptions() == [4242]
        assert await db.remove_reply_exception(4242) is True
        assert not await db.is_reply_exception(4242)
        await db.close()

    asyncio.run(run())


def test_policy_values_are_validated(tmp_path: Path) -> None:
    async def run() -> None:
        db = Database(tmp_path / "policy.sqlite3")
        await db.initialize()
        with pytest.raises(InvalidReplyTextError):
            await db.create_status(title="t", emoji="e", steps=[("text", "x")], active_from_hour=9)
        with pytest.raises(InvalidReplyTextError):
            await db.create_status(title="t", emoji="e", steps=[("text", "x")],
                                   active_from_hour=9, active_to_hour=9)
        with pytest.raises(InvalidReplyTextError):
            await db.create_status(title="t", emoji="e", steps=[("text", "x")], reply_mode="quote")
        await db.close()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# Templates
# --------------------------------------------------------------------------- #
def test_render_template_fills_known_fields_only() -> None:
    context = {"name": "Ali", "time": "10:30", "date": "2026-11-25", "owner": "{name}"}
    assert render_template("سلام {name}، ساعت {time}", context) == "سلام Ali، ساعت 10:30"
    # Unknown braces stay, and a value containing braces is never expanded again.
    assert render_template("{foo} {owner}", context) == "{foo} {name}"


# --------------------------------------------------------------------------- #
# Engine: multi-part replies, reply mode, personalisation
# --------------------------------------------------------------------------- #
def test_multi_part_reply_quotes_only_the_first_part(tmp_path: Path) -> None:
    async def run() -> None:
        h = Harness(tmp_path)
        await h.start()
        await h.status([("text", "سلام {name}"), ("sticker", "STICKER_A"), ("emoji", "👍")])
        message = make_message(message_id=11)
        assert await h.deliver(message) is ReplyOutcome.SENT
        names = [name for name, _ in h.sender.calls]
        assert names == ["message", "sticker", "message"]
        first, second, third = (kwargs for _, kwargs in h.sender.calls)
        assert first["reply_parameters"].message_id == 11
        assert second["reply_parameters"] is None and third["reply_parameters"] is None
        assert second["sticker"] == "STICKER_A" and third["text"] == "👍"
        contact_name = message.from_user.first_name or DEFAULT_CONTACT_NAME
        assert first["text"] == f"سلام {contact_name}"
        await h.db.close()

    asyncio.run(run())


def test_plain_mode_sends_without_reply_parameters(tmp_path: Path) -> None:
    async def run() -> None:
        h = Harness(tmp_path)
        await h.start()
        await h.status([("text", "متن ساده")], reply_mode="plain")
        assert await h.deliver(make_message(message_id=12)) is ReplyOutcome.SENT
        assert h.sender.calls[0][1]["reply_parameters"] is None
        await h.db.close()

    asyncio.run(run())


def test_exception_contact_gets_no_reply(tmp_path: Path) -> None:
    async def run() -> None:
        h = Harness(tmp_path)
        await h.start()
        await h.status([("text", "متن")])
        await h.db.add_reply_exception(3001)  # CONTACT_ID in conftest
        assert await h.deliver(make_message(message_id=13)) is ReplyOutcome.IGNORED
        assert h.sender.calls == []
        await h.db.close()

    asyncio.run(run())


def test_outside_the_hour_window_nothing_is_sent(tmp_path: Path) -> None:
    from datetime import UTC, datetime
    from zoneinfo import ZoneInfo

    async def run() -> None:
        h = Harness(tmp_path)
        await h.start()
        hour = datetime.now(UTC).astimezone(ZoneInfo("Asia/Tehran")).hour
        start, end = (hour + 2) % 24, (hour + 3) % 24  # a window that does not contain now
        await h.status([("text", "متن")], active_from_hour=start, active_to_hour=end)
        assert await h.deliver(make_message(message_id=14)) is ReplyOutcome.IGNORED
        assert h.sender.calls == []
        await h.db.close()

    asyncio.run(run())


def test_min_interval_blocks_a_second_reply_to_the_same_contact(tmp_path: Path) -> None:
    async def run() -> None:
        h = Harness(tmp_path)
        await h.start()
        await h.status([("text", "اول")], min_interval_seconds=3600)
        assert await h.deliver(make_message(message_id=15)) is ReplyOutcome.SENT
        # A different status starts a new session, which would normally allow another reply.
        await h.status([("text", "دوم")], min_interval_seconds=3600)
        assert await h.deliver(make_message(message_id=16)) is ReplyOutcome.IGNORED
        assert len(h.sender.calls) == 1
        await h.db.close()

    asyncio.run(run())


def test_partial_delivery_is_recorded_as_partial_with_parts_sent(tmp_path: Path) -> None:
    async def run() -> None:
        h = Harness(tmp_path)
        await h.start()
        await h.status([("text", "یک"), ("sticker", "BAD_STICKER")])
        h.sender.plan = [None, TelegramBadRequest(method=SendMessage(chat_id=1, text="x"), message="bad")]
        assert await h.deliver(make_message(message_id=18)) is ReplyOutcome.PARTIAL
        # The row is final: a later message in the same session is refused, so nothing is repeated.
        assert await h.deliver(make_message(message_id=19)) is ReplyOutcome.ALREADY_REPLIED
        assert len(h.sender.calls) == 2
        async with h.db._read() as conn:
            cursor = await conn.execute("SELECT state, parts_sent FROM reply_deliveries")
            rows = await cursor.fetchall()
        assert [tuple(r) for r in rows] == [("partial", 1)]
        await h.db.close()

    asyncio.run(run())


def test_first_part_rejected_is_failed_and_retryable(tmp_path: Path) -> None:
    async def run() -> None:
        h = Harness(tmp_path)
        await h.start()
        await h.status([("text", "یک")])
        h.sender.plan = [TelegramBadRequest(method=SendMessage(chat_id=1, text="x"), message="bad")]
        assert await h.deliver(make_message(message_id=20)) is ReplyOutcome.FAILED
        assert await h.deliver(make_message(message_id=21)) is ReplyOutcome.SENT  # failed rows retry
        await h.db.close()

    asyncio.run(run())


def test_ambiguous_first_part_is_uncertain(tmp_path: Path) -> None:
    async def run() -> None:
        h = Harness(tmp_path)
        await h.start()
        await h.status([("text", "یک")])
        h.sender.plan = [RuntimeError("timeout")]
        assert await h.deliver(make_message(message_id=22)) is ReplyOutcome.UNCERTAIN
        assert await h.deliver(make_message(message_id=23)) is ReplyOutcome.ALREADY_REPLIED
        await h.db.close()

    asyncio.run(run())

"""Unit tests for app.storage.

No Telegram access of any kind: only temporary SQLite files. Written with
unittest.IsolatedAsyncioTestCase so no pytest plugin is required; runs under
both ``python -m unittest`` and ``pytest``.
"""

from __future__ import annotations

import asyncio
import sqlite3
import stat
import tempfile
import unittest
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.models import DEFAULT_STATUSES, DeliveryOutcome
from app.storage import (
    SCHEMA_VERSION,
    Database,
    InvalidReplyTextError,
    SchemaVersionError,
    StorageError,
    UnknownConnectionError,
    UnknownStatusError,
)

CONN = "conn-1"
OWNER = 111_222_333
CHAT = 555
FAKE_TOKEN = "123456789:" + "A" * 35


class FakeClock:
    """Deterministic, manually advanced UTC clock."""

    def __init__(self) -> None:
        self.now = datetime(2026, 11, 24, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


class StorageTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "nested" / "ziko.sqlite3"
        self.clock = FakeClock()
        self.db = await self.open_db()
        await self.db.save_business_connection(CONN, True, True, OWNER)

    async def open_db(self, **kwargs: Any) -> Database:
        kwargs.setdefault("clock", self.clock)
        db = Database(self.path, **kwargs)
        await db.initialize()
        self.addAsyncCleanup(db.close)
        return db

    def raw_query(self, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        """Read the database file directly with stdlib sqlite3 (independent of Database)."""
        with closing(sqlite3.connect(self.path)) as raw:
            return raw.execute(sql, params).fetchall()

    async def activate(self, key: str = "sleeping") -> str:
        state = await self.db.activate_status(key)
        assert state.session_id is not None
        return state.session_id


class InitializationTests(StorageTestCase):
    async def test_creates_all_tables_and_schema_version(self) -> None:
        tables = {row[0] for row in self.raw_query("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertTrue(
            {"app_state", "statuses", "business_connections", "processed_messages", "reply_deliveries"}
            <= tables
        )
        self.assertEqual(self.raw_query("PRAGMA user_version")[0][0], SCHEMA_VERSION)

    async def test_database_file_is_owner_only(self) -> None:
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode) & 0o077, 0)

    async def test_default_state_is_off(self) -> None:
        state = await self.db.get_app_state()
        self.assertFalse(state.enabled)
        self.assertIsNone(state.active_status_key)
        self.assertIsNone(state.session_id)
        self.assertTrue(state.updated_at)

    async def test_seeds_six_persian_statuses_in_order(self) -> None:
        statuses = await self.db.list_statuses()
        self.assertEqual(statuses, list(DEFAULT_STATUSES))
        self.assertEqual(
            [(s.emoji, s.title) for s in statuses],
            [
                ("😴", "در حال خواب"),
                ("🍳", "در حال آشپزی"),
                ("🎮", "در حال بازی"),
                ("🚶", "بیرون از خانه"),
                ("📚", "در حال مطالعه"),
                ("🛠", "مشغول کار"),
            ],
        )
        self.assertTrue(all(s.reply_text.strip() for s in statuses))

    async def test_initialize_is_idempotent_and_preserves_edits_across_restart(self) -> None:
        await self.db.initialize()  # second call on the same instance is a no-op
        await self.db.save_status_text("gaming", "متن ویرایش‌شده")
        await self.db.activate_status("gaming")
        before = await self.db.get_app_state()
        await self.db.close()

        reopened = await self.open_db()
        self.assertEqual((await reopened.get_status("gaming")).reply_text, "متن ویرایش‌شده")  # type: ignore[union-attr]
        self.assertEqual(await reopened.get_app_state(), before)
        self.assertEqual(len(await reopened.list_statuses()), 6)

    async def test_newer_schema_version_is_rejected(self) -> None:
        await self.db.close()
        with closing(sqlite3.connect(self.path)) as raw:
            raw.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
            raw.commit()
        with self.assertRaises(SchemaVersionError):
            await Database(self.path).initialize()

    async def test_use_before_initialize_raises(self) -> None:
        with self.assertRaises(StorageError):
            await Database(self.path).get_app_state()

    async def test_no_column_can_hold_message_content(self) -> None:
        forbidden = {"text", "content", "body", "message", "reply_text", "caption"}
        for table in ("processed_messages", "reply_deliveries"):
            columns = {row[1] for row in self.raw_query(f"PRAGMA table_info({table})")}
            self.assertFalse(columns & forbidden, f"{table} has a content-like column")
        self.assertEqual(
            {row[1] for row in self.raw_query("PRAGMA table_info(processed_messages)")},
            {"connection_id", "chat_id", "message_id", "created_at"},
        )


class StatusSessionTests(StorageTestCase):
    async def test_activate_enables_and_starts_uuid_session(self) -> None:
        state = await self.db.activate_status("sleeping")
        self.assertTrue(state.enabled)
        self.assertEqual(state.active_status_key, "sleeping")
        assert state.session_id is not None
        self.assertEqual(str(uuid.UUID(state.session_id)), state.session_id)
        self.assertEqual(await self.db.get_app_state(), state)

    async def test_selecting_active_status_keeps_session_and_state(self) -> None:
        first = await self.db.activate_status("sleeping")
        self.clock.advance(minutes=5)
        again = await self.db.activate_status("sleeping")
        self.assertEqual(again, first)
        self.assertEqual(await self.db.get_app_state(), first)

    async def test_different_status_starts_new_session(self) -> None:
        first = await self.db.activate_status("sleeping")
        second = await self.db.activate_status("cooking")
        self.assertEqual(second.active_status_key, "cooking")
        self.assertNotEqual(second.session_id, first.session_id)

    async def test_disable_then_enable_starts_new_session(self) -> None:
        first = await self.db.activate_status("studying")
        disabled = await self.db.disable_auto_reply()
        self.assertFalse(disabled.enabled)
        self.assertIsNone(disabled.session_id)
        self.assertEqual(disabled.active_status_key, "studying")  # selection is remembered

        enabled = await self.db.enable_auto_reply()
        self.assertTrue(enabled.enabled)
        self.assertEqual(enabled.active_status_key, "studying")
        self.assertIsNotNone(enabled.session_id)
        self.assertNotEqual(enabled.session_id, first.session_id)

    async def test_activating_same_status_after_disable_starts_new_session(self) -> None:
        first = await self.db.activate_status("working")
        await self.db.disable_auto_reply()
        again = await self.db.activate_status("working")
        self.assertTrue(again.enabled)
        self.assertNotEqual(again.session_id, first.session_id)

    async def test_enable_when_already_enabled_keeps_session(self) -> None:
        first = await self.db.activate_status("away")
        self.assertEqual(await self.db.enable_auto_reply(), first)

    async def test_disable_when_already_disabled_is_noop(self) -> None:
        before = await self.db.get_app_state()
        self.assertEqual(await self.db.disable_auto_reply(), before)

    async def test_enable_without_any_selected_status_stays_off(self) -> None:
        state = await self.db.enable_auto_reply()
        self.assertFalse(state.enabled)
        self.assertIsNone(state.session_id)

    async def test_unknown_status_is_rejected_and_state_unchanged(self) -> None:
        before = await self.db.get_app_state()
        with self.assertRaises(UnknownStatusError):
            await self.db.activate_status("does_not_exist")
        self.assertEqual(await self.db.get_app_state(), before)

    async def test_session_ids_come_from_injectable_factory(self) -> None:
        ids = iter(["s-1", "s-2"])
        db = await self.open_db(session_id_factory=lambda: next(ids))
        self.assertEqual((await db.activate_status("sleeping")).session_id, "s-1")
        self.assertEqual((await db.activate_status("cooking")).session_id, "s-2")

    async def test_concurrent_activation_of_same_status_creates_one_session(self) -> None:
        states = await asyncio.gather(*(self.db.activate_status("gaming") for _ in range(25)))
        self.assertEqual(len({s.session_id for s in states}), 1)


class StatusTextTests(StorageTestCase):
    async def test_save_and_get(self) -> None:
        saved = await self.db.save_status_text("cooking", "  سلام، در حال آشپزی‌ام  ")
        self.assertEqual(saved.reply_text, "سلام، در حال آشپزی‌ام")
        self.assertEqual(await self.db.get_status("cooking"), saved)

    async def test_editing_text_does_not_reset_session(self) -> None:
        state = await self.db.activate_status("cooking")
        await self.db.save_status_text("cooking", "متن جدید")
        self.assertEqual(await self.db.get_app_state(), state)

    async def test_invalid_text_is_rejected(self) -> None:
        for bad in ("", "   \n", "x" * 4097):
            with self.subTest(length=len(bad)):
                with self.assertRaises(InvalidReplyTextError):
                    await self.db.save_status_text("cooking", bad)
        self.assertEqual((await self.db.get_status("cooking")).reply_text, DEFAULT_STATUSES[1].reply_text)  # type: ignore[union-attr]

    async def test_unknown_status(self) -> None:
        with self.assertRaises(UnknownStatusError):
            await self.db.save_status_text("nope", "متن")
        self.assertIsNone(await self.db.get_status("nope"))


class BusinessConnectionTests(StorageTestCase):
    async def test_none_before_any_connection(self) -> None:
        db = await self.open_db_fresh()
        self.assertIsNone(await db.get_business_connection())

    async def open_db_fresh(self) -> Database:
        db = Database(self.path.with_name("fresh.sqlite3"), clock=self.clock)
        await db.initialize()
        self.addAsyncCleanup(db.close)
        return db

    async def test_save_and_get_roundtrip(self) -> None:
        state = await self.db.get_business_connection()
        assert state is not None
        self.assertEqual((state.connection_id, state.is_enabled, state.can_reply, state.owner_user_id),
                         (CONN, True, True, OWNER))

    async def test_update_existing_connection(self) -> None:
        self.clock.advance(seconds=1)
        await self.db.save_business_connection(CONN, False, False, OWNER)
        state = await self.db.get_business_connection()
        assert state is not None
        self.assertEqual((state.is_enabled, state.can_reply), (False, False))

    async def test_prefers_enabled_connection_over_newer_disabled_one(self) -> None:
        self.clock.advance(seconds=1)
        await self.db.save_business_connection("old-conn", False, True, OWNER)
        state = await self.db.get_business_connection()
        assert state is not None
        self.assertEqual(state.connection_id, CONN)

    async def test_empty_connection_id_rejected(self) -> None:
        with self.assertRaises(ValueError):
            await self.db.save_business_connection("", True, True, OWNER)


class InboundEventTests(StorageTestCase):
    async def test_duplicate_event_is_rejected(self) -> None:
        self.assertTrue(await self.db.claim_inbound_event(CONN, CHAT, 1))
        self.assertFalse(await self.db.claim_inbound_event(CONN, CHAT, 1))

    async def test_same_message_id_in_other_chat_or_message_is_distinct(self) -> None:
        self.assertTrue(await self.db.claim_inbound_event(CONN, CHAT, 1))
        self.assertTrue(await self.db.claim_inbound_event(CONN, CHAT + 1, 1))
        self.assertTrue(await self.db.claim_inbound_event(CONN, CHAT, 2))

    async def test_duplicates_survive_restart(self) -> None:
        self.assertTrue(await self.db.claim_inbound_event(CONN, CHAT, 7))
        await self.db.close()
        reopened = await self.open_db()
        self.assertFalse(await reopened.claim_inbound_event(CONN, CHAT, 7))

    async def test_concurrent_duplicates_yield_exactly_one_claim(self) -> None:
        results = await asyncio.gather(*(self.db.claim_inbound_event(CONN, CHAT, 99) for _ in range(50)))
        self.assertEqual(sum(results), 1)

    async def test_unknown_connection_raises(self) -> None:
        with self.assertRaises(UnknownConnectionError):
            await self.db.claim_inbound_event("never-saved", CHAT, 1)

    async def test_prune_removes_only_old_records(self) -> None:
        self.assertTrue(await self.db.claim_inbound_event(CONN, CHAT, 1))
        self.clock.advance(days=31)
        self.assertTrue(await self.db.claim_inbound_event(CONN, CHAT, 2))
        self.assertEqual(await self.db.prune_processed_messages(30), 1)
        self.assertFalse(await self.db.claim_inbound_event(CONN, CHAT, 2))  # recent one kept
        self.assertTrue(await self.db.claim_inbound_event(CONN, CHAT, 1))  # old one forgotten


SENT = DeliveryOutcome.SENT
REJECTED = DeliveryOutcome.REJECTED
UNCERTAIN = DeliveryOutcome.UNCERTAIN


class DeliveryTestCase(StorageTestCase):
    """Helpers for claim/complete tests."""

    async def claim(self, session: str, chat: int = CHAT, db: Database | None = None) -> str:
        token = await (db or self.db).claim_reply(CONN, chat, session)
        assert token is not None, "expected claim_reply to succeed"
        return token

    async def complete(
        self,
        session: str,
        token: str,
        outcome: DeliveryOutcome = SENT,
        chat: int = CHAT,
        db: Database | None = None,
        **kwargs: Any,
    ) -> bool:
        return await (db or self.db).complete_reply(CONN, chat, session, token, outcome, **kwargs)

    def delivery_row(self, chat: int = CHAT) -> tuple[Any, ...]:
        (row,) = self.raw_query(
            "SELECT state, attempts, claim_token, telegram_message_id, last_error "
            "FROM reply_deliveries WHERE chat_id = ?",
            (chat,),
        )
        return row


class ReplyDeliveryTests(DeliveryTestCase):
    async def test_first_claim_wins_second_is_rejected_while_pending(self) -> None:
        session = await self.activate()
        self.assertTrue(await self.claim(session))
        self.assertIsNone(await self.db.claim_reply(CONN, CHAT, session))

    async def test_claim_tokens_are_unique_non_empty_strings(self) -> None:
        session = await self.activate()
        tokens = {await self.claim(session, chat=CHAT + i) for i in range(20)}
        self.assertEqual(len(tokens), 20)
        self.assertTrue(all(isinstance(t, str) and len(t) >= 32 for t in tokens))

    async def test_success_is_final(self) -> None:
        session = await self.activate()
        token = await self.claim(session)
        self.assertTrue(await self.complete(session, token, SENT, telegram_message_id=42))
        self.assertIsNone(await self.db.claim_reply(CONN, CHAT, session))
        state, attempts, _, message_id, _ = self.delivery_row()
        self.assertEqual((state, attempts, message_id), ("sent", 1, 42))

    async def test_rejected_attempt_can_be_retried_then_succeeds_once(self) -> None:
        session = await self.activate()
        first = await self.claim(session)
        self.assertTrue(await self.complete(session, first, REJECTED, error="Bad Request: nope"))

        second = await self.claim(session)  # retry allowed after a confirmed rejection
        self.assertNotEqual(first, second)
        self.assertIsNone(await self.db.claim_reply(CONN, CHAT, session))  # not concurrently
        self.assertTrue(await self.complete(session, second, SENT, telegram_message_id=7))

        self.assertIsNone(await self.db.claim_reply(CONN, CHAT, session))  # never a duplicate
        state, attempts, _, _, last_error = self.delivery_row()
        self.assertEqual((state, attempts, last_error), ("sent", 2, None))

    async def test_failure_details_are_stored_without_token_and_truncated(self) -> None:
        session = await self.activate()
        token = await self.claim(session)
        await self.complete(
            session, token, REJECTED,
            error=f"POST https://api.telegram.org/bot{FAKE_TOKEN}/sendMessage failed " + "y" * 2000,
        )
        stored = self.delivery_row()[4]
        self.assertNotIn(FAKE_TOKEN, stored)
        self.assertIn("<redacted>", stored)
        self.assertLessEqual(len(stored), 500)

    async def test_concurrent_claims_yield_exactly_one_reservation(self) -> None:
        session = await self.activate()
        results = await asyncio.gather(*(self.db.claim_reply(CONN, CHAT, session) for _ in range(50)))
        self.assertEqual(len([r for r in results if r]), 1)

    async def test_concurrent_claims_across_two_database_instances(self) -> None:
        """Two connections to the same file (as with two processes) still yield one winner."""
        session = await self.activate()
        other = await self.open_db()
        calls = [(self.db if i % 2 else other).claim_reply(CONN, CHAT, session) for i in range(40)]
        self.assertEqual(len([r for r in await asyncio.gather(*calls) if r]), 1)

    async def test_concurrent_retry_after_rejection_yields_one_reservation(self) -> None:
        session = await self.activate()
        token = await self.claim(session)
        await self.complete(session, token, REJECTED, error="boom")
        results = await asyncio.gather(*(self.db.claim_reply(CONN, CHAT, session) for _ in range(30)))
        self.assertEqual(len([r for r in results if r]), 1)

    async def test_different_chats_are_independent(self) -> None:
        session = await self.activate()
        self.assertTrue(await self.claim(session))
        self.assertTrue(await self.claim(session, chat=CHAT + 1))

    async def test_new_session_allows_another_reply_to_same_chat(self) -> None:
        first = await self.activate("sleeping")
        await self.complete(first, await self.claim(first), SENT, telegram_message_id=1)

        second = await self.activate("cooking")  # different status -> new session
        await self.complete(second, await self.claim(second), SENT, telegram_message_id=2)

        await self.db.disable_auto_reply()
        await self.db.enable_auto_reply()  # disabled -> enabled -> new session
        third = (await self.db.get_app_state()).session_id
        assert third is not None
        self.assertTrue(await self.claim(third))

    async def test_same_status_reselected_does_not_allow_second_reply(self) -> None:
        session = await self.activate("sleeping")
        await self.complete(session, await self.claim(session), SENT, telegram_message_id=1)
        await self.db.activate_status("sleeping")  # keeps session
        self.assertIsNone(await self.db.claim_reply(CONN, CHAT, session))

    async def test_default_off_state_never_allows_a_claim(self) -> None:
        self.assertIsNone(await self.db.claim_reply(CONN, CHAT, str(uuid.uuid4())))

    async def test_claim_refused_when_disabled_or_session_is_stale(self) -> None:
        stale = await self.activate("sleeping")
        await self.activate("cooking")
        self.assertIsNone(await self.db.claim_reply(CONN, CHAT, stale))  # status switched

        current = (await self.db.get_app_state()).session_id
        assert current is not None
        await self.db.disable_auto_reply()
        self.assertIsNone(await self.db.claim_reply(CONN, CHAT, current))  # auto-reply off
        self.assertEqual(self.raw_query("SELECT COUNT(*) FROM reply_deliveries")[0][0], 0)

    async def test_default_policy_never_reclaims_an_abandoned_pending_reservation(self) -> None:
        """Production default: an expired lease means 'outcome unknown', never 'send again'."""
        session = await self.activate()
        token = await self.claim(session)  # worker "crashes" without completing
        self.clock.advance(seconds=100)
        self.assertIsNone(await self.db.claim_reply(CONN, CHAT, session))  # still within lease
        self.assertEqual(self.delivery_row()[0], "pending")
        self.clock.advance(seconds=201)  # 301s total > 300s default lease
        self.assertIsNone(await self.db.claim_reply(CONN, CHAT, session))
        self.assertEqual(self.delivery_row()[0], "uncertain")
        self.clock.advance(days=5)
        self.assertIsNone(await self.db.claim_reply(CONN, CHAT, session))
        self.assertFalse(await self.complete(session, token, SENT, telegram_message_id=1))
        self.assertEqual(self.delivery_row()[1], 1)  # attempts: never re-attempted

    async def test_reclaim_policy_is_an_explicit_opt_in(self) -> None:
        db = await self.open_db(expired_lease_policy="reclaim")
        session = await self.activate()
        first = await self.claim(session, db=db)  # worker "crashes" without completing
        self.clock.advance(seconds=100)
        self.assertIsNone(await db.claim_reply(CONN, CHAT, session))  # still within lease
        self.clock.advance(seconds=201)  # 301s total > 300s default lease
        second = await self.claim(session, db=db)
        self.assertNotEqual(first, second)
        self.assertEqual(self.delivery_row()[1], 2)

    async def test_pending_claim_survives_restart_within_lease(self) -> None:
        session = await self.activate()
        await self.claim(session)
        await self.db.close()
        reopened = await self.open_db()
        self.assertIsNone(await reopened.claim_reply(CONN, CHAT, session))

    async def test_sent_reply_survives_restart(self) -> None:
        session = await self.activate()
        await self.complete(session, await self.claim(session), SENT, telegram_message_id=5)
        self.clock.advance(days=2)
        await self.db.close()
        reopened = await self.open_db()
        self.assertIsNone(await reopened.claim_reply(CONN, CHAT, session))

    async def test_complete_without_claim_changes_nothing(self) -> None:
        session = await self.activate()
        self.assertFalse(await self.complete(session, "f" * 32, SENT, telegram_message_id=1))
        self.assertFalse(await self.complete(session, "f" * 32, REJECTED, error="x"))
        self.assertTrue(await self.claim(session))

    async def test_complete_twice_is_ignored(self) -> None:
        session = await self.activate()
        token = await self.claim(session)
        self.assertTrue(await self.complete(session, token, SENT, telegram_message_id=1))
        self.assertFalse(await self.complete(session, token, REJECTED, error="late failure"))
        self.assertFalse(await self.complete(session, token, UNCERTAIN, error="late"))
        self.assertEqual(self.delivery_row()[0], "sent")

    async def test_claim_for_unknown_connection_raises(self) -> None:
        session = await self.activate()
        with self.assertRaises(UnknownConnectionError):
            await self.db.claim_reply("never-saved", CHAT, session)

    async def test_invalid_complete_arguments_raise(self) -> None:
        session = await self.activate()
        token = await self.claim(session)
        with self.assertRaises(TypeError):
            await self.db.complete_reply(CONN, CHAT, session, token, True)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            await self.db.complete_reply(CONN, CHAT, session, "", SENT)
        with self.assertRaises(ValueError):
            await self.db.complete_reply(CONN, CHAT, session, None, SENT)  # type: ignore[arg-type]
        self.assertEqual(self.delivery_row()[0], "pending")  # nothing changed


class ClaimTokenOwnershipTests(DeliveryTestCase):
    """A worker can only ever finish its own reservation.

    These tests need an expired lease to be handed to a second worker, which only happens under
    the explicit ``"reclaim"`` opt-in, so every Database opened here defaults to that policy.
    """

    async def open_db(self, **kwargs: Any) -> Database:
        kwargs.setdefault("expired_lease_policy", "reclaim")
        return await super().open_db(**kwargs)

    async def reclaim_after_expiry(self, session: str) -> tuple[str, str]:
        worker_a = await self.claim(session)
        self.clock.advance(seconds=301)  # worker A hangs past the 300s lease
        worker_b = await self.claim(session)
        return worker_a, worker_b

    async def test_reclaim_invalidates_the_previous_workers_token(self) -> None:
        session = await self.activate()
        token_a, token_b = await self.reclaim_after_expiry(session)
        self.assertNotEqual(token_a, token_b)
        self.assertEqual(self.delivery_row()[2], token_b)

    async def test_stale_worker_cannot_complete_with_any_outcome(self) -> None:
        session = await self.activate()
        token_a, token_b = await self.reclaim_after_expiry(session)
        for outcome in (SENT, REJECTED, UNCERTAIN):
            with self.subTest(outcome=outcome):
                self.assertFalse(await self.complete(session, token_a, outcome, telegram_message_id=1))
        state, _, stored_token, message_id, _ = self.delivery_row()
        self.assertEqual((state, stored_token, message_id), ("pending", token_b, None))  # B untouched

        self.assertTrue(await self.complete(session, token_b, SENT, telegram_message_id=2))
        self.assertEqual(self.delivery_row()[3], 2)

    async def test_stale_worker_cannot_release_the_new_owners_reservation(self) -> None:
        session = await self.activate()
        token_a, token_b = await self.reclaim_after_expiry(session)
        self.assertFalse(await self.db.release_reply(CONN, CHAT, session, token_a))
        self.assertEqual(self.delivery_row()[0], "pending")
        # B's reservation is still live and exclusive: nobody else can claim it.
        self.assertIsNone(await self.db.claim_reply(CONN, CHAT, session))
        self.assertTrue(await self.db.release_reply(CONN, CHAT, session, token_b))

    async def test_token_is_bound_to_connection_chat_and_session(self) -> None:
        session = await self.activate("sleeping")
        token = await self.claim(session)
        other_chat_token = await self.claim(session, chat=CHAT + 1)

        self.assertFalse(await self.complete(session, token, SENT, chat=CHAT + 1))  # wrong chat
        self.assertFalse(await self.complete("other-session", token, SENT))  # wrong session
        self.assertFalse(await self.complete(session, other_chat_token, SENT))  # swapped tokens
        await self.db.save_business_connection("conn-2", True, True, OWNER)
        self.assertFalse(await self.db.complete_reply("conn-2", CHAT, session, token, SENT))
        self.assertEqual(self.delivery_row(CHAT)[0], "pending")
        self.assertEqual(self.delivery_row(CHAT + 1)[0], "pending")

    async def test_release_makes_delivery_retryable_and_is_one_shot(self) -> None:
        session = await self.activate()
        token = await self.claim(session)
        self.assertTrue(await self.db.release_reply(CONN, CHAT, session, token))
        self.assertFalse(await self.db.release_reply(CONN, CHAT, session, token))
        self.assertEqual(self.delivery_row()[0], "failed")
        self.assertTrue(await self.claim(session))

    async def test_concurrent_completion_with_same_token_has_exactly_one_winner(self) -> None:
        session = await self.activate()
        token = await self.claim(session)
        results = await asyncio.gather(
            *(self.complete(session, token, SENT, telegram_message_id=i) for i in range(20))
        )
        self.assertEqual(sum(results), 1)
        self.assertEqual(self.delivery_row()[0], "sent")

    async def test_concurrent_conflicting_outcomes_across_two_instances(self) -> None:
        session = await self.activate()
        other = await self.open_db()
        token = await self.claim(session)
        outcomes = [SENT, REJECTED, UNCERTAIN] * 10
        calls = [
            self.complete(session, token, outcome, db=(self.db if i % 2 else other),
                          telegram_message_id=1, error="e")
            for i, outcome in enumerate(outcomes)
        ]
        results = await asyncio.gather(*calls)
        self.assertEqual(sum(results), 1)
        winner = outcomes[results.index(True)]
        expected = {SENT: "sent", REJECTED: "failed", UNCERTAIN: "uncertain"}[winner]
        self.assertEqual(self.delivery_row()[0], expected)

    async def test_claim_on_one_instance_complete_on_another(self) -> None:
        session = await self.activate()
        other = await self.open_db()
        token = await self.claim(session)
        self.assertTrue(await self.complete(session, token, SENT, db=other, telegram_message_id=3))
        self.assertIsNone(await self.db.claim_reply(CONN, CHAT, session))

    async def test_concurrent_reclaim_of_expired_lease_has_one_winner_and_revokes_old_token(self) -> None:
        session = await self.activate()
        other = await self.open_db()
        old_token = await self.claim(session)
        self.clock.advance(seconds=301)
        claims = await asyncio.gather(
            *((self.db if i % 2 else other).claim_reply(CONN, CHAT, session) for i in range(30))
        )
        winners = [t for t in claims if t]
        self.assertEqual(len(winners), 1)
        self.assertNotEqual(winners[0], old_token)
        self.assertFalse(await self.complete(session, old_token, SENT, db=other))
        self.assertTrue(await self.complete(session, winners[0], SENT, db=other))

    async def test_completion_racing_with_reclaim_never_double_owns(self) -> None:
        """Old owner completes while another worker reclaims: exactly one of them ends up owning it."""
        session = await self.activate()
        other = await self.open_db()
        old_token = await self.claim(session)
        self.clock.advance(seconds=301)
        completed, reclaimed = await asyncio.gather(
            self.complete(session, old_token, SENT, telegram_message_id=1),
            other.claim_reply(CONN, CHAT, session),
        )
        state = self.delivery_row()[0]
        if completed:  # old owner finished first -> reservation is final, reclaim refused
            self.assertEqual(state, "sent")
            self.assertIsNone(reclaimed)
        else:  # reclaim first -> old token revoked, new owner holds a pending reservation
            self.assertEqual(state, "pending")
            self.assertIsNotNone(reclaimed)


class UncertainDeliveryTests(DeliveryTestCase):
    async def test_uncertain_outcome_is_never_retried_automatically(self) -> None:
        session = await self.activate()
        token = await self.claim(session)
        self.assertTrue(await self.complete(session, token, UNCERTAIN, error="ReadTimeout"))
        self.assertEqual(self.delivery_row()[0], "uncertain")
        for _ in range(3):  # later incoming messages in the same session are not answered
            self.assertIsNone(await self.db.claim_reply(CONN, CHAT, session))
        self.clock.advance(days=3)  # not even much later
        self.assertIsNone(await self.db.claim_reply(CONN, CHAT, session))
        self.assertEqual(self.delivery_row()[1], 1)  # attempts unchanged

    async def test_uncertain_survives_restart(self) -> None:
        session = await self.activate()
        await self.complete(session, await self.claim(session), UNCERTAIN, error="reset")
        await self.db.close()
        reopened = await self.open_db()
        self.assertIsNone(await reopened.claim_reply(CONN, CHAT, session))

    async def test_uncertain_only_blocks_that_contact_in_that_session(self) -> None:
        session = await self.activate("sleeping")
        await self.complete(session, await self.claim(session), UNCERTAIN)
        self.assertTrue(await self.claim(session, chat=CHAT + 1))  # other contacts unaffected
        new_session = await self.activate("cooking")  # new session starts fresh
        self.assertTrue(await self.claim(new_session))

    async def test_uncertain_error_text_is_redacted(self) -> None:
        session = await self.activate()
        await self.complete(
            session, await self.claim(session), UNCERTAIN,
            error=f"timeout calling bot{FAKE_TOKEN}/sendMessage",
        )
        self.assertNotIn(FAKE_TOKEN, self.delivery_row()[4])

    async def test_default_policy_turns_expired_pending_into_uncertain(self) -> None:
        db = await self.open_db()  # default policy
        session = await self.activate()
        token = await self.claim(session, db=db)
        self.clock.advance(seconds=301)
        self.assertIsNone(await db.claim_reply(CONN, CHAT, session))  # not handed to anyone
        self.assertEqual(self.delivery_row()[0], "uncertain")
        self.assertFalse(await self.complete(session, token, SENT, db=db))  # token is dead
        self.assertIsNone(await db.claim_reply(CONN, CHAT, session))

    async def test_default_policy_still_blocks_within_lease(self) -> None:
        db = await self.open_db()  # default policy
        session = await self.activate()
        token = await self.claim(session, db=db)
        self.clock.advance(seconds=100)
        self.assertIsNone(await db.claim_reply(CONN, CHAT, session))
        self.assertEqual(self.delivery_row()[0], "pending")
        self.assertTrue(await self.complete(session, token, SENT, db=db))

    async def test_explicit_uncertain_policy_matches_the_default(self) -> None:
        db = await self.open_db(expired_lease_policy="uncertain")
        session = await self.activate()
        await self.claim(session, db=db)
        self.clock.advance(seconds=301)
        self.assertIsNone(await db.claim_reply(CONN, CHAT, session))
        self.assertEqual(self.delivery_row()[0], "uncertain")

    async def test_ambiguous_outcome_is_never_retried_under_either_policy(self) -> None:
        for offset, policy in enumerate(("uncertain", "reclaim")):
            with self.subTest(policy=policy):
                chat = CHAT + 100 + offset
                db = await self.open_db(expired_lease_policy=policy)  # type: ignore[arg-type]
                session = await self.activate()
                token = await self.claim(session, chat=chat, db=db)
                self.assertTrue(await self.complete(session, token, UNCERTAIN, chat=chat, db=db))
                self.clock.advance(days=2)
                self.assertIsNone(await db.claim_reply(CONN, chat, session))
                self.assertEqual(self.delivery_row(chat)[0], "uncertain")

    async def test_confirmed_rejection_stays_retryable_under_either_policy(self) -> None:
        for offset, policy in enumerate(("uncertain", "reclaim")):
            with self.subTest(policy=policy):
                chat = CHAT + 200 + offset
                db = await self.open_db(expired_lease_policy=policy)  # type: ignore[arg-type]
                session = await self.activate()
                first = await self.claim(session, chat=chat, db=db)
                self.assertTrue(await self.complete(session, first, REJECTED, chat=chat, db=db, error="403"))
                self.clock.advance(days=2)
                second = await self.claim(session, chat=chat, db=db)  # retry after a confirmed rejection
                self.assertNotEqual(first, second)

    async def test_invalid_policy_and_lease_rejected(self) -> None:
        with self.assertRaises(ValueError):
            Database(self.path, expired_lease_policy="bogus")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            Database(self.path, pending_lease_seconds=0)


class SchemaMigrationTests(StorageTestCase):
    async def test_version_1_database_is_upgraded_and_rows_are_kept(self) -> None:
        session = await self.activate()
        await self.db.close()
        # Rebuild reply_deliveries exactly as it was in schema version 1 (no claim_token).
        with closing(sqlite3.connect(self.path)) as raw:
            raw.executescript(
                """
                DROP TABLE reply_deliveries;
                CREATE TABLE reply_deliveries (
                    connection_id TEXT NOT NULL REFERENCES business_connections (connection_id)
                        ON UPDATE CASCADE ON DELETE CASCADE,
                    chat_id INTEGER NOT NULL,
                    session_id TEXT NOT NULL CHECK (length(session_id) > 0),
                    state TEXT NOT NULL CHECK (state IN ('pending', 'sent', 'failed')),
                    attempts INTEGER NOT NULL DEFAULT 1 CHECK (attempts >= 1),
                    telegram_message_id INTEGER, last_error TEXT,
                    claimed_at TEXT NOT NULL, completed_at TEXT, updated_at TEXT NOT NULL,
                    PRIMARY KEY (connection_id, chat_id, session_id),
                    CHECK (state <> 'sent' OR completed_at IS NOT NULL)
                ) WITHOUT ROWID;
                PRAGMA user_version = 1;
                """
            )
            raw.execute(
                "INSERT INTO reply_deliveries VALUES (?, 1, ?, 'sent', 1, 77, NULL, 't0', 't0', 't0')",
                (CONN, session),
            )
            raw.execute(
                "INSERT INTO reply_deliveries VALUES (?, 2, ?, 'failed', 3, NULL, 'x', 't0', 't0', 't0')",
                (CONN, session),
            )
            raw.commit()

        db = await self.open_db()
        self.assertEqual(self.raw_query("PRAGMA user_version")[0][0], SCHEMA_VERSION)
        rows = self.raw_query(
            "SELECT chat_id, state, attempts, telegram_message_id, length(claim_token) "
            "FROM reply_deliveries ORDER BY chat_id"
        )
        self.assertEqual(rows, [(1, "sent", 1, 77, 32), (2, "failed", 3, None, 32)])
        self.assertIsNone(await db.claim_reply(CONN, 1, session))  # migrated 'sent' stays final
        token = await db.claim_reply(CONN, 2, session)  # migrated 'failed' is retryable
        assert token is not None
        self.assertTrue(await db.complete_reply(CONN, 2, session, token, UNCERTAIN, error="e"))
        self.assertEqual(self.raw_query("SELECT state FROM reply_deliveries WHERE chat_id = 2"),
                         [("uncertain",)])


class CancellationSafetyTests(DeliveryTestCase):
    async def test_cancelled_callers_do_not_wedge_the_connection(self) -> None:
        session = await self.activate()
        tasks = [asyncio.create_task(self.db.claim_reply(CONN, CHAT + i, session)) for i in range(40)]
        await asyncio.sleep(0)
        for task in tasks[::2]:
            task.cancel()
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=15)

        # The connection must be usable and not stuck inside an open transaction.
        self.assertTrue((await asyncio.wait_for(self.db.get_app_state(), 5)).enabled)
        self.assertTrue(await asyncio.wait_for(self.claim(session, chat=10_000), 5))
        await asyncio.wait_for(self.db.save_status_text("gaming", "متن"), 5)


if __name__ == "__main__":
    unittest.main()

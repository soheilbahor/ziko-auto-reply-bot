"""Business message engine tests (Module 4). Telegram is always mocked."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from conftest import (
    ADMIN_ID, BOT_TOKEN, CONN_ID, CONTACT_ID, OWNER_ID, PRIVATE_BODY, STRANGER_ID,
    ambiguous_failure, begin_session, claim, complete, confirmed_rejection,
    delivery_outcome, dump_database_text, make_connection_update, make_message,
)

pytestmark = pytest.mark.asyncio


# ----------------------------------------------------------------------------
# Positive path
# ----------------------------------------------------------------------------
async def test_valid_private_message_triggers_active_reply(armed: Any) -> None:
    state = await armed.db.get_app_state()
    status = await armed.db.get_status(state.active_status_key)
    await armed.deliver(make_message(message_id=1))
    assert len(armed.sent) == 1
    assert armed.sent[0]["chat_id"] == CONTACT_ID
    assert armed.sent[0]["text"] == status.reply_text


async def test_business_connection_id_is_passed_to_send_message(armed: Any) -> None:
    await armed.deliver(make_message(message_id=1))
    assert armed.sent[0].get("business_connection_id") == CONN_ID


async def test_non_contact_private_chat_is_served_too(armed: Any) -> None:
    await armed.deliver(make_message(message_id=1, chat_id=CONTACT_ID + 500))
    assert len(armed.sent) == 1


async def test_duplicate_update_replies_once(armed: Any) -> None:
    message = make_message(message_id=1)
    await armed.deliver(message)
    await armed.deliver(message)
    assert len(armed.sent) == 1


async def test_concurrent_handlers_send_a_single_reply(armed: Any) -> None:
    async def slow_send(*args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(0.05)  # widen the race window
        return armed.sender.send_message.return_value

    armed.sender.send_message.side_effect = slow_send
    await asyncio.gather(*(armed.deliver(make_message(message_id=i)) for i in range(1, 16)))
    assert len(armed.sent) == 1


async def test_separate_contacts_each_get_one_reply(armed: Any) -> None:
    await armed.deliver(make_message(message_id=1, chat_id=CONTACT_ID))
    await armed.deliver(make_message(message_id=1, chat_id=CONTACT_ID + 1))
    assert sorted(m["chat_id"] for m in armed.sent) == [CONTACT_ID, CONTACT_ID + 1]


# ----------------------------------------------------------------------------
# Gates: global switch, connection state, rights
# ----------------------------------------------------------------------------
async def test_default_off_state_never_replies(harness: Any) -> None:
    await harness.db.save_business_connection(CONN_ID, True, True, OWNER_ID)
    await harness.deliver(make_message(message_id=1))
    assert harness.sent == []


async def test_no_reply_when_global_automation_disabled(armed: Any) -> None:
    await armed.db.disable_auto_reply()
    await armed.deliver(make_message(message_id=1))
    assert armed.sent == []


async def test_no_reply_when_business_connection_disabled(armed: Any) -> None:
    await armed.db.save_business_connection(CONN_ID, False, True, OWNER_ID)
    await armed.deliver(make_message(message_id=1))
    assert armed.sent == []


async def test_no_reply_when_can_reply_is_false(armed: Any) -> None:
    await armed.db.save_business_connection(CONN_ID, True, False, OWNER_ID)
    await armed.deliver(make_message(message_id=1))
    assert armed.sent == []


async def test_no_reply_when_no_connection_is_stored(harness: Any) -> None:
    await begin_session(harness.db)
    await harness.deliver(make_message(message_id=1))
    assert harness.sent == []


async def test_no_reply_for_unknown_connection_id(armed: Any) -> None:
    """QA expectation: a message tagged with a connection we never stored is ignored."""
    await armed.deliver(make_message(message_id=1, connection_id="some-other-connection"))
    assert armed.sent == []


async def test_connection_update_with_missing_rights_means_cannot_reply(harness: Any) -> None:
    await harness.connection_update(make_connection_update(can_reply=None))
    stored = await harness.db.get_business_connection()
    assert stored is not None and stored.can_reply is False
    await begin_session(harness.db)
    await harness.deliver(make_message(message_id=1))
    assert harness.sent == []


async def test_connection_update_persists_enabled_and_reply_rights(harness: Any) -> None:
    await harness.connection_update(make_connection_update(can_reply=True, is_enabled=True))
    stored = await harness.db.get_business_connection()
    assert (stored.connection_id, stored.is_enabled, stored.can_reply, stored.owner_user_id) == (
        CONN_ID, True, True, OWNER_ID)


# ----------------------------------------------------------------------------
# Ignored senders / chats / events
# ----------------------------------------------------------------------------
@pytest.mark.parametrize("chat_type", ["group", "supergroup", "channel"])
async def test_non_private_chats_are_ignored(armed: Any, chat_type: str) -> None:
    await armed.deliver(make_message(message_id=1, chat_id=-100123, chat_type=chat_type,
                                     from_id=CONTACT_ID))
    assert armed.sent == []


async def test_bot_senders_are_ignored(armed: Any) -> None:
    await armed.deliver(make_message(message_id=1, is_bot=True))
    assert armed.sent == []


async def test_owner_messages_are_ignored(armed: Any) -> None:
    # Outgoing message typed by the account owner inside a customer's chat.
    await armed.deliver(make_message(message_id=1, chat_id=CONTACT_ID, from_id=OWNER_ID))
    assert armed.sent == []


async def test_messages_in_admin_chat_are_ignored(armed: Any) -> None:
    await armed.deliver(make_message(message_id=1, chat_id=ADMIN_ID, from_id=ADMIN_ID))
    assert armed.sent == []


async def test_edited_events_do_not_trigger_reply(armed: Any) -> None:
    await armed.deliver(make_message(message_id=1, edited=True))
    assert armed.sent == []


async def test_edit_of_already_answered_message_does_not_double_reply(armed: Any) -> None:
    await armed.deliver(make_message(message_id=1))
    await armed.deliver(make_message(message_id=1, edited=True))
    assert len(armed.sent) == 1


async def test_ignored_events_do_not_consume_the_reply_slot(armed: Any) -> None:
    await armed.deliver(make_message(message_id=1, is_bot=True))
    await armed.deliver(make_message(message_id=2, chat_type="group", chat_id=-5))
    await armed.deliver(make_message(message_id=3))  # first legitimate message
    assert len(armed.sent) == 1


# ----------------------------------------------------------------------------
# Delivery bookkeeping: claim token -> SENT / REJECTED / UNCERTAIN / release
# ----------------------------------------------------------------------------
async def test_successful_send_completes_claim_with_sent_and_message_id(armed: Any) -> None:
    await armed.deliver(make_message(message_id=1))
    spy = armed.db
    assert len(spy.granted) == 1
    assert spy.outcome_names() == ["SENT"]
    assert 9001 in spy.settlements[0]["values"], "Telegram message id must be recorded"
    spy.assert_every_claim_settled_once()


async def test_claim_token_is_passed_to_the_matching_completion(armed: Any) -> None:
    await armed.deliver(make_message(message_id=1))
    await armed.deliver(make_message(message_id=2, chat_id=CONTACT_ID + 1))
    spy = armed.db
    assert len(spy.granted) == 2
    assert [s["token"] for s in spy.settlements] == spy.granted
    spy.assert_every_claim_settled_once()


@pytest.mark.parametrize("kind", ["bad_request", "forbidden", "retry_after"])
async def test_confirmed_api_rejection_is_recorded_as_rejected_and_retried_later(
    armed: Any, kind: str
) -> None:
    armed.sender.send_message.side_effect = confirmed_rejection(kind)
    await armed.deliver(make_message(message_id=1))          # must not raise
    assert len(armed.sent) == 1
    assert armed.db.outcome_names() == ["REJECTED"]

    armed.sender.send_message.side_effect = None
    await armed.deliver(make_message(message_id=2))          # later message retries
    assert len(armed.sent) == 2
    assert armed.db.outcome_names() == ["REJECTED", "SENT"]

    await armed.deliver(make_message(message_id=3))          # delivered -> no more
    assert len(armed.sent) == 2
    armed.db.assert_every_claim_settled_once()


@pytest.mark.parametrize("kind", ["timeout", "reset", "aiogram_network"])
async def test_ambiguous_network_outcome_is_recorded_as_uncertain_and_not_resent(
    armed: Any, kind: str
) -> None:
    armed.sender.send_message.side_effect = ambiguous_failure(kind)
    await armed.deliver(make_message(message_id=1))          # must not raise
    assert armed.db.outcome_names() == ["UNCERTAIN"]

    armed.sender.send_message.side_effect = None
    await armed.deliver(make_message(message_id=2))
    await armed.deliver(make_message(message_id=3))
    assert len(armed.sent) == 1, "an UNCERTAIN delivery must never be retried in the same session"
    armed.db.assert_every_claim_settled_once()


async def test_unexpected_exception_during_send_is_treated_as_uncertain(armed: Any) -> None:
    """QA expectation (A14): if we cannot tell whether Telegram processed it, assume it might have."""
    armed.sender.send_message.side_effect = ambiguous_failure("unexpected")
    await armed.deliver(make_message(message_id=1))
    assert armed.db.outcome_names() == ["UNCERTAIN"]
    armed.db.assert_every_claim_settled_once()


async def test_rejected_is_never_used_for_ambiguous_errors(armed: Any) -> None:
    for index, kind in enumerate(("timeout", "reset", "unexpected"), start=1):
        await begin_session(armed.db, index=index)           # fresh session per case
        armed.sender.send_message.side_effect = ambiguous_failure(kind)
        await armed.deliver(make_message(message_id=index))
    assert "REJECTED" not in armed.db.outcome_names()
    assert "SENT" not in armed.db.outcome_names()


async def test_failure_before_sending_releases_the_claim_and_allows_retry(armed: Any) -> None:
    armed.db.fail_status_lookup = True
    try:
        await armed.deliver(make_message(message_id=1))
    except Exception:  # propagating an internal error is acceptable; leaking a claim is not
        pass
    assert armed.sent == [], "nothing may be sent when the reply text could not be loaded"
    assert "SENT" not in armed.db.outcome_names()
    armed.db.assert_every_claim_settled_once()
    if armed.db.granted:
        kinds = [s["kind"] for s in armed.db.settlements]
        assert kinds == ["release"], (
            "a claim taken before Telegram was contacted must be released, not completed")

    armed.db.fail_status_lookup = False
    await armed.deliver(make_message(message_id=2))          # recovered: one reply goes out
    assert len(armed.sent) == 1


async def test_ignored_events_never_take_a_claim(armed: Any) -> None:
    await armed.deliver(make_message(message_id=1, is_bot=True))
    await armed.deliver(make_message(message_id=2, chat_type="group", chat_id=-5))
    await armed.deliver(make_message(message_id=3, edited=True))
    assert armed.db.granted == []
    armed.db.assert_every_claim_settled_once()


async def test_gated_messages_never_take_a_claim(armed: Any) -> None:
    await armed.db.save_business_connection(CONN_ID, True, False, OWNER_ID)
    await armed.deliver(make_message(message_id=1))
    assert armed.db.granted == []


async def test_concurrent_handlers_with_uncertain_outcome_send_once_and_settle_once(
    armed: Any
) -> None:
    async def slow_timeout(*args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(0.05)
        raise ambiguous_failure("timeout")

    armed.sender.send_message.side_effect = slow_timeout
    await asyncio.gather(*(armed.deliver(make_message(message_id=i)) for i in range(1, 12)))
    assert len(armed.sent) == 1
    assert armed.db.outcome_names() == ["UNCERTAIN"]
    armed.db.assert_every_claim_settled_once()


async def test_engine_does_not_complete_a_reservation_it_does_not_hold(armed: Any) -> None:
    """Another worker already holds the slot: the engine must not send or settle anything."""
    state = await armed.db.get_app_state()
    foreign_token = await claim(armed.db._inner, CONTACT_ID, state.session_id)
    assert foreign_token
    await armed.deliver(make_message(message_id=1))
    assert armed.sent == []
    assert armed.db.settlements == []
    outcome = delivery_outcome()
    await complete(armed.db._inner, CONTACT_ID, state.session_id, foreign_token,
                   outcome.SENT, message_id=77)           # the real holder still completes fine


@pytest.mark.parametrize("kind", ["rejected", "uncertain"])
async def test_failure_details_do_not_leak_token_or_body(
    armed: Any, db_path: Any, caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str], kind: str
) -> None:
    caplog.set_level(logging.DEBUG)
    leaky = (f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage failed for "
             f"text={PRIVATE_BODY}")
    armed.sender.send_message.side_effect = (
        confirmed_rejection("bad_request", leaky) if kind == "rejected"
        else ambiguous_failure("reset", leaky)
    )
    await armed.deliver(make_message(message_id=1))
    captured = capsys.readouterr()
    haystacks = {
        "log records": caplog.text + "\n".join(r.getMessage() for r in caplog.records),
        "stdout/stderr": captured.out + captured.err,
        "database": dump_database_text(db_path),
    }
    for where, blob in haystacks.items():
        assert BOT_TOKEN not in blob, f"bot token leaked into {where}"
        assert BOT_TOKEN.split(":")[1] not in blob, f"token secret half leaked into {where}"
        assert PRIVATE_BODY not in blob, f"private message body leaked into {where}"


# ----------------------------------------------------------------------------
# Privacy: no message content persistence / no logging of bodies
# ----------------------------------------------------------------------------
async def test_private_message_body_is_never_persisted(armed: Any, db_path: Any) -> None:
    await armed.deliver(make_message(message_id=1))
    await armed.deliver(make_message(message_id=2, chat_id=CONTACT_ID + 1))
    await armed.deliver(make_message(message_id=3, is_bot=True))
    assert PRIVATE_BODY not in dump_database_text(db_path)


async def test_private_message_body_is_never_logged(
    armed: Any, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    caplog.set_level(logging.DEBUG)
    await armed.deliver(make_message(message_id=1))
    await armed.deliver(make_message(message_id=2, chat_type="group", chat_id=-9))
    captured = capsys.readouterr()
    blob = caplog.text + captured.out + captured.err
    assert PRIVATE_BODY not in blob


async def test_bot_token_is_never_logged_on_success_path(
    armed: Any, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    caplog.set_level(logging.DEBUG)
    await armed.deliver(make_message(message_id=1))
    captured = capsys.readouterr()
    assert BOT_TOKEN not in caplog.text + captured.out + captured.err


async def test_stranger_private_message_is_handled_like_any_contact(armed: Any) -> None:
    await armed.deliver(make_message(message_id=1, chat_id=STRANGER_ID))
    assert len(armed.sent) == 1

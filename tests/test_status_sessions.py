"""Status and session semantics (spec requirements 3-8) — Module 4."""

from __future__ import annotations

from typing import Any

import pytest
from conftest import (
    CONTACT_2_ID,
    CONTACT_ID,
    begin_session,
    claim,
    complete,
    delivery_outcome,
    make_message,
)

pytestmark = pytest.mark.asyncio

EXPECTED_EMOJIS = {"😴", "🍳", "🎮", "🚶", "📚", "🛠"}


def _norm(emoji: str) -> str:
    return emoji.replace("\ufe0f", "")


async def test_default_state_is_off(db: Any) -> None:
    state = await db.get_app_state()
    assert state.enabled is False
    assert state.active_status_key is None
    assert state.session_id is None


async def test_initial_statuses_match_spec(db: Any) -> None:
    statuses = await db.list_statuses()
    assert {_norm(s.emoji) for s in statuses} >= {_norm(e) for e in EXPECTED_EMOJIS}
    assert len({s.key for s in statuses}) == len(statuses), "status keys must be unique"


async def test_each_status_activation_starts_a_distinct_session(db: Any) -> None:
    sessions = []
    for status in await db.list_statuses():
        sessions.append((await begin_session(db, status.key)).session_id)
    assert len(set(sessions)) == len(sessions)


async def test_switching_back_to_earlier_status_is_a_new_session(db: Any) -> None:
    a = await begin_session(db, index=0)
    b = await begin_session(db, index=1)
    a_again = await begin_session(db, a.active_status_key)
    assert len({a.session_id, b.session_id, a_again.session_id}) == 3


async def test_contact_gets_one_reply_per_session_across_three_sessions(db: Any) -> None:
    outcome = delivery_outcome()
    delivered = 0
    for step in range(3):
        if step == 2:
            await db.disable_auto_reply()
            await db.enable_auto_reply()
            state = await db.get_app_state()
        else:
            state = await begin_session(db, index=step)
        for _attempt in range(4):  # repeated incoming messages inside one session
            token = await claim(db, CONTACT_ID, state.session_id)
            if token:
                await complete(db, CONTACT_ID, state.session_id, token, outcome.SENT,
                               message_id=100 + step)
                delivered += 1
    assert delivered == 3


async def test_reselecting_active_status_repeatedly_never_resets(db: Any) -> None:
    state = await begin_session(db)
    for _ in range(5):
        await db.activate_status(state.active_status_key)
    assert (await db.get_app_state()).session_id == state.session_id


async def test_enable_when_already_enabled_does_not_reset_session(db: Any) -> None:
    """QA expectation (not explicit in spec): enabling an enabled app is a no-op."""
    state = await begin_session(db)
    await db.enable_auto_reply()
    assert (await db.get_app_state()).session_id == state.session_id


async def test_disable_turns_auto_reply_off(db: Any) -> None:
    await begin_session(db)
    await db.disable_auto_reply()
    assert (await db.get_app_state()).enabled is False


async def test_editing_reply_text_mid_session_does_not_grant_second_reply(db: Any) -> None:
    outcome = delivery_outcome()
    state = await begin_session(db)
    token = await claim(db, CONTACT_ID, state.session_id)
    await complete(db, CONTACT_ID, state.session_id, token, outcome.SENT, message_id=1)
    await db.save_status_text(state.active_status_key, "متن ویرایش‌شده")
    assert await claim(db, CONTACT_ID, state.session_id) is None


async def test_other_contact_unaffected_by_first_contacts_delivery(db: Any) -> None:
    outcome = delivery_outcome()
    state = await begin_session(db)
    token = await claim(db, CONTACT_ID, state.session_id)
    await complete(db, CONTACT_ID, state.session_id, token, outcome.SENT, message_id=1)
    assert await claim(db, CONTACT_2_ID, state.session_id)


async def test_uncertain_delivery_in_old_session_does_not_block_new_session(db: Any) -> None:
    outcome = delivery_outcome()
    first = await begin_session(db, index=0)
    token = await claim(db, CONTACT_ID, first.session_id)
    await complete(db, CONTACT_ID, first.session_id, token, outcome.UNCERTAIN, error="timeout")
    second = await begin_session(db, index=1)
    assert await claim(db, CONTACT_ID, second.session_id)


# ----------------------------------------------------------------------------
# Engine-level session behaviour (skipped until the business engine is present)
# ----------------------------------------------------------------------------
async def test_engine_replies_once_per_session_then_again_after_status_switch(armed: Any) -> None:
    await armed.deliver(make_message(message_id=1))
    await armed.deliver(make_message(message_id=2))
    assert len(armed.sent) == 1

    await begin_session(armed.db, index=1)
    await armed.deliver(make_message(message_id=3))
    await armed.deliver(make_message(message_id=4))
    assert len(armed.sent) == 2
    armed.db.assert_every_claim_settled_once()


async def test_engine_reselecting_same_status_does_not_allow_second_reply(armed: Any) -> None:
    await armed.deliver(make_message(message_id=1))
    state = await armed.db.get_app_state()
    await armed.db.activate_status(state.active_status_key)
    await armed.deliver(make_message(message_id=2))
    assert len(armed.sent) == 1


async def test_engine_reply_after_disable_enable_cycle(armed: Any) -> None:
    await armed.deliver(make_message(message_id=1))
    await armed.db.disable_auto_reply()
    await armed.deliver(make_message(message_id=2))       # OFF -> silent
    assert len(armed.sent) == 1
    await armed.db.enable_auto_reply()
    await armed.deliver(make_message(message_id=3))       # fresh session -> one reply
    assert len(armed.sent) == 2


async def test_engine_uncertain_in_one_session_still_replies_in_next(armed: Any) -> None:
    from conftest import ambiguous_failure

    armed.sender.send_message.side_effect = ambiguous_failure("timeout")
    await armed.deliver(make_message(message_id=1))
    armed.sender.send_message.side_effect = None
    await armed.deliver(make_message(message_id=2))       # same session: no duplicate risk taken
    assert len(armed.sent) == 1
    await begin_session(armed.db, index=1)
    await armed.deliver(make_message(message_id=3))       # new session: one fresh reply
    assert len(armed.sent) == 2


async def test_engine_sends_the_active_status_text(armed: Any) -> None:
    state = await armed.db.get_app_state()
    status = await armed.db.get_status(state.active_status_key)
    await armed.deliver(make_message(message_id=1))
    assert armed.sent[0]["text"] == status.reply_text

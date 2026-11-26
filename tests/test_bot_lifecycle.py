"""Child-bot lifecycle: watchdog, states, classification (app/bot_lifecycle.py).

These run without aiogram. ``test_tenant_lifecycle_aiogram.py`` covers the real poller.
"""
from __future__ import annotations

import re

import pytest

from app import bot_lifecycle as bl


# Stand-ins named exactly like aiogram's classes: classify() matches by class name.
class TelegramUnauthorizedError(Exception):
    pass


class TelegramConflictError(Exception):
    pass


class TelegramNotFound(Exception):
    pass


class TelegramNetworkError(Exception):
    pass


class TelegramServerError(Exception):
    pass


class RestartingTelegram(TelegramServerError):  # subclass must map like its parent
    pass


@pytest.mark.parametrize("exc,code,retryable,disable", [
    (TelegramUnauthorizedError(), "token_rejected", False, True),
    (TelegramConflictError(), "polling_conflict", False, False),
    (TelegramNotFound(), "bot_not_found", False, True),
    (TelegramNetworkError(), "network", True, False),
    (RestartingTelegram(), "telegram_unavailable", True, False),
    (TimeoutError(), "timeout", True, False),
    (ValueError(), "unknown", True, False),
])
def test_classification(exc, code, retryable, disable):
    d = bl.classify(exc)
    assert (d.code, d.retryable, d.disable) == (code, retryable, disable)


def test_operator_text_is_english_and_owner_text_is_persian():
    persian = re.compile(r"[\u0600-\u06FF]")
    for name in ["TelegramUnauthorizedError", "TelegramConflictError", "TelegramNotFound",
                 "TelegramRetryAfter", "TelegramServerError", "TelegramNetworkError",
                 "TimeoutError", "StorageError", "nonexistent"]:
        d = bl.classify(name)
        assert not persian.search(d.operator), name
        assert persian.search(d.owner_title) and persian.search(d.owner_action), name


def test_watchdog_first_poll_marks_running():
    wd = bl.PollingWatchdog()
    assert not wd.first_poll
    wd.ok(now=100.0)
    assert wd.first_poll and wd.first_poll_at == 100.0 and wd.consecutive == 0


def test_revoked_token_trips_quickly_instead_of_retrying_forever():
    wd = bl.PollingWatchdog(fatal_after=2)
    trips = []
    wd.on_trip(trips.append)
    assert wd.failed(TelegramUnauthorizedError()) is None      # one could be a race
    trip = wd.failed(TelegramUnauthorizedError())
    assert trip is not None and trip.error_name == "TelegramUnauthorizedError"
    assert isinstance(trip.error, TelegramUnauthorizedError)
    assert trips == [trip]
    assert wd.failed(TelegramUnauthorizedError()) is trip      # stays tripped, no new callback
    assert trips == [trip]


def test_single_conflict_during_restart_is_tolerated():
    wd = bl.PollingWatchdog(fatal_after=2)
    assert wd.failed(TelegramConflictError()) is None
    wd.ok()
    assert wd.failed(TelegramConflictError()) is None
    assert wd.tripped is None


def test_transient_errors_trip_only_after_many_in_a_row():
    wd = bl.PollingWatchdog(max_consecutive=8)
    for _ in range(7):
        assert wd.failed(TelegramNetworkError()) is None
    assert wd.failed(TelegramNetworkError()) is not None


def test_success_resets_and_different_errors_do_not_accumulate():
    wd = bl.PollingWatchdog(max_consecutive=3)
    wd.failed(TelegramNetworkError())
    wd.failed(TelegramNetworkError())
    wd.ok()
    wd.failed(TelegramNetworkError())
    wd.failed(TimeoutError())
    wd.failed(TelegramNetworkError())
    assert wd.tripped is None


def test_status_transitions_count_restarts_and_clear_running():
    st = bl.BotStatus(bot_id=1)
    st.move(bl.State.STARTING, now=1)
    st.move(bl.State.RUNNING, now=2)
    assert st.running_since == 2 and st.starts == 1 and st.restarts == 0
    st.move(bl.State.BACKOFF, error=bl.classify(TelegramNetworkError()), now=3)
    assert st.running_since is None and st.last_error_code == "network"
    st.move(bl.State.STARTING, now=4)
    assert st.starts == 2 and st.restarts == 1
    assert st.as_dict()["state"] == "starting"

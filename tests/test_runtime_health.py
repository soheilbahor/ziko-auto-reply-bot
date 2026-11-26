"""Runtime-health rules for child bots: "running" means Telegram polling is confirmed.

These drive the real TenantBotManager. Only the Telegram side is replaced: the polling stub
feeds the same PollingWatchdog the aiogram session middleware feeds in production.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.bot_lifecycle import State
from app.tenant_bots import TenantBotManager
from app.tenant_registry import Tenant


class TelegramUnauthorizedError(Exception):
    """Same class name as aiogram's: classify() maps it to token_rejected (fatal)."""


class TelegramNetworkError(Exception):
    """Same class name as aiogram's: classify() maps it to network (retryable)."""


class FakeRegistry:
    async def set_tenant_enabled(self, bot_id: int, enabled: bool) -> bool:
        return True


class FakeMasterBot:
    async def send_message(self, *, chat_id: int, text: str):
        return None


def _tenant(n: int) -> Tenant:
    return Tenant(user_id=1000 + n, bot_id=2000 + n, username=f"bot{n}",
                  encrypted_token="encrypted", created_at="2026-01-01T00:00:00Z",
                  enabled=True, bot_token=f"token-{n}")


def _manager(tmp_path: Path, slots: int = 1) -> TenantBotManager:
    settings = SimpleNamespace(database_path=tmp_path / "ziko.sqlite3", admin_ids=frozenset())
    return TenantBotManager(settings=settings, registry=FakeRegistry(),
                            master_bot=FakeMasterBot(), max_concurrent_pollers=slots)


async def _wait_for(predicate, timeout: float = 1.0) -> None:
    for _ in range(int(timeout / 0.01)):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached")


@pytest.mark.asyncio
async def test_slot_holder_without_confirmed_polling_is_not_running(tmp_path: Path):
    manager = _manager(tmp_path, slots=1)
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold_without_telegram(tenant):
        manager._attach_watchdog(tenant.bot_id)  # getUpdates never answers
        entered.set()
        await release.wait()

    manager._run_tenant_once = hold_without_telegram
    try:
        await manager.start_tenant(_tenant(1))
        await asyncio.wait_for(entered.wait(), timeout=1)
        await manager.start_tenant(_tenant(2))
        await _wait_for(lambda: manager.runtime_state(2002) == "waiting")
        assert manager.runtime_state(2001) == "starting"
        assert manager.runtime_state(2002) == "waiting"
        assert manager.runtime_state(2002) != "running"
        assert manager.runtime_status(2001) != "در حال اجرا"
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_degraded_then_recovers_when_telegram_answers_again(tmp_path: Path):
    manager = _manager(tmp_path)
    holder: dict = {}
    entered, release = asyncio.Event(), asyncio.Event()

    async def poll(tenant):
        watchdog = manager._attach_watchdog(tenant.bot_id)
        watchdog.ok()
        assert manager._confirm_polling(tenant.bot_id, watchdog)
        holder["wd"] = watchdog
        entered.set()
        await release.wait()

    manager._run_tenant_once = poll
    try:
        await manager.start_tenant(_tenant(1))
        await asyncio.wait_for(entered.wait(), timeout=1)
        assert manager.runtime_state(2001) == "running"

        assert holder["wd"].failed(TelegramNetworkError()) is None  # retryable, not tripped
        assert manager.runtime_state(2001) == "degraded"
        assert manager.runtime_status(2001) == "در حال اجرا، ارتباط با تلگرام قطع است"

        holder["wd"].ok()  # Telegram responsive again
        assert manager.runtime_state(2001) == "running"
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_rejected_token_never_becomes_running(tmp_path: Path):
    manager = _manager(tmp_path)
    outcome: dict = {}
    entered, release = asyncio.Event(), asyncio.Event()

    async def poll_with_revoked_token(tenant):
        watchdog = manager._attach_watchdog(tenant.bot_id)
        watchdog.failed(TelegramUnauthorizedError())
        outcome["tripped"] = watchdog.failed(TelegramUnauthorizedError())
        outcome["confirmed"] = manager._confirm_polling(tenant.bot_id, watchdog)
        # Even a late successful poll on a tripped watchdog must not confirm it.
        watchdog.ok()
        outcome["confirmed_after_ok"] = manager._confirm_polling(tenant.bot_id, watchdog)
        entered.set()
        await release.wait()

    manager._run_tenant_once = poll_with_revoked_token
    try:
        await manager.start_tenant(_tenant(1))
        await asyncio.wait_for(entered.wait(), timeout=1)
        assert outcome["tripped"] is not None
        assert outcome["confirmed"] is False
        assert outcome["confirmed_after_ok"] is False
        assert manager.runtime_state(2001) != "running"
        status = manager.status(2001)
        assert status is None or status.state is not State.RUNNING
    finally:
        await manager.close()


def test_confirmation_requires_a_successful_poll_and_the_current_watchdog(tmp_path: Path):
    manager = _manager(tmp_path)
    old = manager._attach_watchdog(2001)
    assert manager._confirm_polling(2001, old) is False  # no getUpdates answer yet
    old.ok()
    manager._attach_watchdog(2001)  # poller restarted
    assert manager._confirm_polling(2001, old) is False  # stale run cannot confirm
    assert manager.status(2001) is None or manager.status(2001).state is not State.RUNNING

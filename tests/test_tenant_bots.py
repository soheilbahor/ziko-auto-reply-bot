from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import app.tenant_bots as tenant_bots
from app.tenant_bots import TenantBotManager
from app.tenant_registry import Tenant


class FakeRegistry:
    def __init__(self):
        self.disabled: list[int] = []

    async def set_tenant_enabled(self, bot_id: int, enabled: bool) -> bool:
        if not enabled:
            self.disabled.append(bot_id)
        return True


class FakeMasterBot:
    def __init__(self):
        self.messages: list[tuple[int, str]] = []

    async def send_message(self, *, chat_id: int, text: str):
        self.messages.append((chat_id, text))


@pytest.mark.asyncio
async def test_unauthorized_child_token_disables_tenant_and_notifies_admins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    class FakeUnauthorized(Exception):
        pass

    monkeypatch.setattr(tenant_bots, "TelegramUnauthorizedError", FakeUnauthorized)
    registry = FakeRegistry()
    master_bot = FakeMasterBot()
    settings = SimpleNamespace(
        database_path=tmp_path / "ziko.sqlite3",
        admin_ids=frozenset({11, 22}),
    )
    manager = TenantBotManager(
        settings=settings, registry=registry, master_bot=master_bot,
        max_concurrent_pollers=2,
    )

    async def fail_as_unauthorized(_tenant):
        raise FakeUnauthorized()

    manager._run_tenant_once = fail_as_unauthorized
    tenant = Tenant(
        user_id=1001,
        bot_id=2001,
        username="revoked_bot",
        encrypted_token="encrypted",
        created_at="2026-01-01T00:00:00Z",
        enabled=True,
        bot_token="not-a-real-token",
    )

    await manager._supervise_tenant(tenant)

    assert registry.disabled == [2001]
    assert [chat_id for chat_id, _ in master_bot.messages] == [11, 22]
    assert all("2001" in message and "1001" in message for _, message in master_bot.messages)
    assert all("not-a-real-token" not in message for _, message in master_bot.messages)


def test_tenant_poller_limit_must_be_positive(tmp_path: Path):
    with pytest.raises(ValueError, match="positive integer"):
        TenantBotManager(
            settings=SimpleNamespace(
                database_path=tmp_path / "ziko.sqlite3",
                admin_ids=frozenset(),
            ),
            registry=FakeRegistry(),
            master_bot=FakeMasterBot(),
            max_concurrent_pollers=0,
        )


def test_tenant_poller_limit_is_configurable(tmp_path: Path):
    manager = TenantBotManager(
        settings=SimpleNamespace(
            database_path=tmp_path / "ziko.sqlite3",
            admin_ids=frozenset(),
        ),
        registry=FakeRegistry(),
        master_bot=FakeMasterBot(),
        max_concurrent_pollers=3,
    )
    assert manager.max_concurrent_pollers == 3


@pytest.mark.asyncio
async def test_poller_slots_report_running_and_waiting_states(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import asyncio

    registry = FakeRegistry()
    settings = SimpleNamespace(
        database_path=tmp_path / "ziko.sqlite3",
        admin_ids=frozenset(),
    )
    manager = TenantBotManager(
        settings=settings, registry=registry, master_bot=FakeMasterBot(),
        max_concurrent_pollers=1,
    )
    first = Tenant(
        user_id=1001, bot_id=2001, username="first_bot",
        encrypted_token="encrypted", created_at="2026-01-01T00:00:00Z",
        enabled=True, bot_token="token-one",
    )
    second = Tenant(
        user_id=1002, bot_id=2002, username="second_bot",
        encrypted_token="encrypted", created_at="2026-01-01T00:00:00Z",
        enabled=True, bot_token="token-two",
    )
    entered = {2001: asyncio.Event(), 2002: asyncio.Event()}
    release = asyncio.Event()

    async def hold_polling(tenant):
        entered[tenant.bot_id].set()
        await release.wait()

    manager._run_tenant_once = hold_polling
    try:
        await manager.start_tenant(first)
        await asyncio.wait_for(entered[2001].wait(), timeout=1)
        await manager.start_tenant(second)
        for _ in range(100):
            if manager.runtime_status(2002) == "در صف انتظار":
                break
            await asyncio.sleep(0.01)
        assert manager.runtime_status(2001) == "در حال اجرا"
        assert manager.runtime_status(2002) == "در صف انتظار"
        assert manager.running_count() == 1
        assert manager.waiting_count() == 1
    finally:
        await manager.close()


def test_default_poller_limit_matches_default_tenant_capacity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("ZIKO_TENANT_MAX_CONCURRENT_POLLERS", raising=False)
    monkeypatch.delenv("ZIKO_TENANT_MAX_TOTAL", raising=False)
    manager = TenantBotManager(
        settings=SimpleNamespace(
            database_path=tmp_path / "ziko.sqlite3",
            admin_ids=frozenset(),
        ),
        registry=FakeRegistry(),
        master_bot=FakeMasterBot(),
    )
    assert manager.max_concurrent_pollers == 50


@pytest.mark.asyncio
async def test_close_stops_pollers_and_prevents_new_supervisors(
    tmp_path: Path,
):
    import asyncio

    registry = FakeRegistry()
    manager = TenantBotManager(
        settings=SimpleNamespace(
            database_path=tmp_path / "ziko.sqlite3",
            admin_ids=frozenset(),
        ),
        registry=registry,
        master_bot=FakeMasterBot(),
        max_concurrent_pollers=1,
    )
    tenant = Tenant(
        user_id=1001, bot_id=2001, username="shutdown_bot",
        encrypted_token="encrypted", created_at="2026-01-01T00:00:00Z",
        enabled=True, bot_token="token",
    )
    entered = asyncio.Event()

    async def hold_polling(_tenant):
        entered.set()
        await asyncio.Event().wait()

    manager._run_tenant_once = hold_polling
    await manager.start_tenant(tenant)
    await asyncio.wait_for(entered.wait(), timeout=1)
    await manager.close()

    assert manager._closing is True
    assert manager.running_count() == 0
    assert manager.waiting_count() == 0
    assert manager.runtime_status(tenant.bot_id) == "متوقف"
    await manager.start_tenant(tenant)
    assert manager._tasks == {}

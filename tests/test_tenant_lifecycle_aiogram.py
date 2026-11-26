"""The real TenantBotManager and aiogram Dispatcher, with Telegram faked at the HTTP-session level.

Proves the core requirement: a newly registered child bot actually receives /start and
answers it, and is reported RUNNING only after Telegram answered getUpdates.
Also proves the root-cause fix: a revoked token or a polling conflict ends the poller
instead of being retried forever by aiogram.

Needs aiogram + aiosqlite; skipped (and reported as skipped) otherwise.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

aiogram = pytest.importorskip("aiogram")
pytest.importorskip("aiosqlite")

from aiogram.client.session.base import BaseSession  # noqa: E402
from aiogram.exceptions import TelegramConflictError, TelegramUnauthorizedError  # noqa: E402
from aiogram.methods import (  # noqa: E402
    DeleteWebhook, GetMe, GetUpdates, GetUserProfilePhotos, SendMessage, SetMyCommands,
)

import app.tenant_bots as tenant_bots  # noqa: E402
from app.bot_lifecycle import State  # noqa: E402
from app.config import Settings  # noqa: E402
from app.tenant_bots import TenantBotManager  # noqa: E402
from app.tenant_registry import Tenant  # noqa: E402

BOT_ID, OWNER = 7001, 9001
TOKEN = f"{BOT_ID}:AAH_fake_token_for_lifecycle_tests_0001"


class FakeTelegram(BaseSession):
    """Answers Bot API methods in-process. ``script`` decides what getUpdates returns."""

    def __init__(self, script: list[Any]) -> None:
        super().__init__()
        self.script = list(script)
        self.calls: list[str] = []
        self.sent: list[dict[str, Any]] = []

    async def close(self) -> None:
        pass

    async def stream_content(self, *a: Any, **k: Any):  # pragma: no cover - unused
        yield b""

    async def make_request(self, bot: Any, method: Any, timeout: int | None = None) -> Any:
        self.calls.append(type(method).__name__)
        if isinstance(method, GetMe):
            return aiogram.types.User(id=BOT_ID, is_bot=True, first_name="Child", username="child_bot")
        if isinstance(method, (DeleteWebhook, SetMyCommands)):
            return True
        if isinstance(method, GetUserProfilePhotos):
            return aiogram.types.UserProfilePhotos(total_count=0, photos=[])
        if isinstance(method, SendMessage):
            self.sent.append({"chat_id": method.chat_id, "text": method.text})
            return aiogram.types.Message(
                message_id=len(self.sent) + 100, date=0,
                chat=aiogram.types.Chat(id=method.chat_id, type="private"), text=method.text)
        if isinstance(method, GetUpdates):
            await asyncio.sleep(0.01)
            step = self.script.pop(0) if self.script else []
            if isinstance(step, BaseException):
                raise step
            return [aiogram.types.Update.model_validate(u, context={"bot": bot}) for u in step]
        return True


def start_update(update_id: int = 1) -> dict[str, Any]:
    user = {"id": OWNER, "is_bot": False, "first_name": "Owner"}
    return {"update_id": update_id, "message": {
        "message_id": 10, "date": 0, "chat": {"id": OWNER, "type": "private", "first_name": "Owner"},
        "from": user, "text": "/start",
        "entities": [{"type": "bot_command", "offset": 0, "length": 6}]}}


class Registry:
    def __init__(self) -> None:
        self.disabled: list[int] = []

    async def set_tenant_enabled(self, bot_id: int, enabled: bool) -> bool:
        if not enabled:
            self.disabled.append(bot_id)
        return True

    async def get_tenant(self, user_id: int):
        return None


class MasterBot:
    id = 1

    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send_message(self, *, chat_id: int, text: str) -> None:
        self.messages.append(text)

    async def get_user_profile_photos(self, **_k: Any):
        return SimpleNamespace(total_count=0, photos=[])


@pytest.fixture
def make_manager(tmp_path, monkeypatch):
    sessions: list[FakeTelegram] = []

    def build(script: list[Any]):
        session = FakeTelegram(script)
        sessions.append(session)
        real_bot = tenant_bots.Bot

        def bot_factory(token: str):
            return real_bot(token=token, session=session)

        monkeypatch.setattr(tenant_bots, "Bot", bot_factory)
        monkeypatch.setattr(tenant_bots, "STARTUP_TIMEOUT", 5)
        monkeypatch.setattr(tenant_bots, "INITIAL_RESTART_DELAY", 0)
        # The real (frozen) Settings dataclass: the supervisor derives per-tenant settings
        # with dataclasses.replace(), which a SimpleNamespace does not support.
        settings = Settings(database_path=tmp_path / "ziko.sqlite3", admin_ids=frozenset({1}),
                            bot_token="1:x", log_level="INFO", business_owner_id=None)
        registry, master = Registry(), MasterBot()
        manager = TenantBotManager(settings=settings, registry=registry, master_bot=master,
                                   max_concurrent_pollers=5, log_dir=tmp_path / "logs")
        return manager, session, registry, master

    return build


def tenant() -> Tenant:
    return Tenant(user_id=OWNER, bot_id=BOT_ID, username="child_bot", encrypted_token="x",
                  created_at="2026-01-01T00:00:00Z", enabled=True, bot_token=TOKEN)


async def wait_for(predicate, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.02)


async def test_new_child_bot_receives_and_answers_start(make_manager):
    manager, session, _registry, _master = make_manager([[], [start_update()]] + [[]] * 200)
    await manager.start_tenant(tenant())
    try:
        await wait_for(lambda: manager.runtime_state(BOT_ID) == "running")
        await wait_for(lambda: any(m["chat_id"] == OWNER for m in session.sent))
    finally:
        await manager.close()
    assert session.calls[:2] == ["GetMe", "DeleteWebhook"]           # verified before polling
    replies = [m for m in session.sent if m["chat_id"] == OWNER]
    assert len(replies) == 1, replies                                 # exactly one answer to /start
    assert replies[0]["text"]                                         # the admin panel answered
    assert TOKEN not in json.dumps(session.sent)


async def test_not_running_until_first_getupdates_returns(make_manager):
    slow = asyncio.Event()
    manager, session, _r, _m = make_manager([])

    original = session.make_request

    async def hold_first_poll(bot, method, timeout=None):
        if isinstance(method, GetUpdates) and not slow.is_set():
            await slow.wait()
        return await original(bot, method, timeout)

    session.make_request = hold_first_poll  # type: ignore[method-assign]
    await manager.start_tenant(tenant())
    try:
        await wait_for(lambda: "GetUpdates" in session.calls or manager.runtime_state(BOT_ID) == "starting")
        await asyncio.sleep(0.2)
        assert manager.runtime_state(BOT_ID) == "starting"            # slot held, NOT running
        slow.set()
        await wait_for(lambda: manager.runtime_state(BOT_ID) == "running")
    finally:
        await manager.close()


async def test_revoked_token_during_polling_disables_and_notifies(make_manager):
    revoked = [TelegramUnauthorizedError(method=GetUpdates(), message="Unauthorized")] * 50
    manager, session, registry, master = make_manager([[]] + revoked)
    await manager.start_tenant(tenant())
    try:
        await wait_for(lambda: registry.disabled == [BOT_ID], timeout=10)
    finally:
        await manager.close()
    assert manager.status(BOT_ID).state in (State.FAILED, State.STOPPED)
    assert manager.status(BOT_ID).last_error_code == "token_rejected"
    assert registry.disabled == [BOT_ID]               # disabled exactly once
    assert len(master.messages) == 1 and TOKEN not in master.messages[0]   # one notice, no token
    assert session.calls.count("GetUpdates") < 10     # stopped quickly; aiogram alone retries forever


async def test_polling_conflict_backs_off_instead_of_spinning(make_manager, monkeypatch):
    monkeypatch.setattr(tenant_bots, "CONFLICT_RETRY_DELAY", 0)
    conflicts = [TelegramConflictError(method=GetUpdates(), message="Conflict: terminated by other getUpdates")] * 4
    manager, session, registry, _m = make_manager([[]] + conflicts + [[]] * 200)
    await manager.start_tenant(tenant())
    try:
        await wait_for(lambda: (manager.status(BOT_ID) is not None
                                and manager.status(BOT_ID).last_error_code == "polling_conflict"), timeout=10)
        await wait_for(lambda: manager.runtime_state(BOT_ID) == "running", timeout=10)  # recovered
    finally:
        await manager.close()
    assert registry.disabled == []                     # a conflict never disables the tenant
    assert manager.status(BOT_ID).restarts >= 1


async def test_unexpected_error_is_logged_with_type_and_backs_off(make_manager, monkeypatch, caplog):
    """An exception the table does not know is reported by class (never only "unknown"),
    without the token, and restarts are rate-limited even with a zero configured delay."""
    import logging

    manager, _session, registry, _m = make_manager([])

    async def boom(_tenant):
        raise RuntimeError(f"exploded while calling https://api.telegram.org/bot{TOKEN}/getMe")

    monkeypatch.setattr(manager, "_run_tenant_polling", boom)
    caplog.set_level(logging.ERROR, logger="ziko.tenant_bots")
    await manager.start_tenant(tenant())
    await asyncio.sleep(1.0)
    await manager.close()
    stopped = [r for r in caplog.records if "Child bot stopped" in r.getMessage()]
    assert stopped
    first = stopped[0].getMessage()
    assert "error_type=RuntimeError" in first and "exploded" in first
    assert TOKEN not in first and TOKEN.split(":", 1)[1] not in first
    assert stopped[0].exc_info is not None                       # traceback kept for the log file
    assert len(stopped) <= 6                                     # 0.25s floor + doubling, not a spin
    assert registry.disabled == []                               # unexpected errors never disable
    assert manager.runtime_state(BOT_ID) != "running"


async def test_close_cancels_a_backing_off_supervisor_promptly(make_manager, monkeypatch):
    manager, _session, _r, _m = make_manager([])
    monkeypatch.setattr(tenant_bots, "INITIAL_RESTART_DELAY", 30)

    async def fail(_tenant):
        raise RuntimeError("x")

    monkeypatch.setattr(manager, "_run_tenant_polling", fail)
    await manager.start_tenant(tenant())
    await asyncio.sleep(0.2)
    loop = asyncio.get_running_loop()
    started = loop.time()
    await manager.close()
    assert loop.time() - started < 2
    assert manager.runtime_state(BOT_ID) == "stopped"

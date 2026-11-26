"""A managed_bot update must reach the handler and register the new child bot.

Drives the real aiogram Dispatcher with the real tenant router; Telegram is faked at the
session level. Regression for 1.4.x-1.5.1, where the update was logged "is not handled".
"""
from __future__ import annotations

from typing import Any

import pytest

aiogram = pytest.importorskip("aiogram")

from aiogram import Bot, Dispatcher  # noqa: E402
from aiogram.client.session.base import BaseSession  # noqa: E402
from aiogram.dispatcher.event.bases import UNHANDLED  # noqa: E402
from aiogram.types import Update  # noqa: E402

from app.handlers.tenants import create_tenant_router  # noqa: E402
from app.tenant_registry import UserAlreadyHasBot  # noqa: E402

MASTER_ID, OWNER, CHILD = 9152411071, 9217139291, 9027262952
MASTER_TOKEN = f"{MASTER_ID}:AAFakeMasterTokenForTestsOnly_0123456789"
CHILD_TOKEN = f"{CHILD}:AAFakeChildTokenForTestsOnly_01234567890"


class FakeTelegram(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []
        self.sent: list[dict[str, Any]] = []

    async def close(self) -> None:
        pass

    async def stream_content(self, *a: Any, **k: Any):  # pragma: no cover - unused
        yield b""

    async def make_request(self, bot: Any, method: Any, timeout: int | None = None) -> Any:
        name = type(method).__name__
        self.calls.append(name)
        if name == "GetManagedBotToken":
            return CHILD_TOKEN
        if name == "SendMessage":
            self.sent.append({"chat_id": method.chat_id, "text": method.text})
            return aiogram.types.Message(message_id=len(self.sent), date=0, text=method.text,
                                         chat=aiogram.types.Chat(id=method.chat_id, type="private"))
        return True


class Registry:
    def __init__(self, refuse: Exception | None = None) -> None:
        self.added: list[dict[str, Any]] = []
        self.refuse = refuse

    async def get_tenant_by_bot_id(self, bot_id: int, reveal_token: bool = False):
        return None

    async def add_tenant(self, **kw: Any):
        if self.refuse is not None:
            raise self.refuse
        self.added.append(kw)
        from types import SimpleNamespace
        return SimpleNamespace(user_id=kw["user_id"], bot_id=kw["bot_id"], username=kw["username"],
                               enabled=True, bot_token=kw["token"])


class Manager:
    def __init__(self) -> None:
        self.started: list[int] = []

    async def start_tenant(self, tenant: Any) -> None:
        self.started.append(tenant.bot_id)

    async def stop_tenant(self, bot_id: int) -> None:
        pass


def managed_update(update_id: int = 1) -> dict[str, Any]:
    return {"update_id": update_id, "managed_bot": {
        "user": {"id": OWNER, "is_bot": False, "first_name": "Owner"},
        "bot_user": {"id": CHILD, "is_bot": True, "first_name": "New", "username": "NewZikoBot"},
    }}


async def feed(registry: Registry) -> tuple[Any, Manager, FakeTelegram]:
    session = FakeTelegram()
    bot = Bot(token=MASTER_TOKEN, session=session)
    dp = Dispatcher()
    manager = Manager()
    dp.include_router(create_tenant_router(dispatcher=dp, registry=registry, manager=manager,
                                           master_bot_id=MASTER_ID, admin_ids=frozenset({1})))
    result = await dp.feed_update(bot, Update.model_validate(managed_update(), context={"bot": bot}))
    return result, manager, session


@pytest.mark.asyncio
async def test_managed_bot_update_is_handled_and_registers_the_bot():
    registry = Registry()
    result, manager, session = await feed(registry)
    assert result is not UNHANDLED
    assert "GetManagedBotToken" in session.calls
    assert len(registry.added) == 1
    added = registry.added[0]
    assert (added["user_id"], added["bot_id"], added["username"]) == (OWNER, CHILD, "NewZikoBot")
    assert manager.started == [CHILD]
    notices = [m for m in session.sent if m["chat_id"] == OWNER]
    assert len(notices) == 1 and "@NewZikoBot" in notices[0]["text"]
    assert CHILD_TOKEN not in notices[0]["text"]


@pytest.mark.asyncio
async def test_owner_with_a_bot_already_is_told_and_logged(caplog):
    import logging
    caplog.set_level(logging.WARNING, logger="ziko.tenants")
    registry = Registry(refuse=UserAlreadyHasBot())
    _result, manager, session = await feed(registry)
    assert manager.started == []
    assert len([m for m in session.sent if m["chat_id"] == OWNER]) == 1
    assert any("reason=UserAlreadyHasBot" in r.getMessage() for r in caplog.records)


def test_master_polls_managed_bot_updates():
    from pathlib import Path
    main = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text()
    assert '"managed_bot"' in main.split("allowed_updates=", 1)[1].split("]", 1)[0]

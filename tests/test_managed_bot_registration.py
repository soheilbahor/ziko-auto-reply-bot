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
            self.sent.append({"chat_id": method.chat_id, "text": method.text,
                              "markup": method.reply_markup})
            return aiogram.types.Message(message_id=len(self.sent), date=0, text=method.text,
                                         chat=aiogram.types.Chat(id=method.chat_id, type="private"))
        return True


class Registry:
    def __init__(self, refuse: Exception | None = None, existing: list[Any] | None = None) -> None:
        self.added: list[dict[str, Any]] = []
        self.refuse = refuse
        self.existing = existing or []

    async def list_tenants(self, *, reveal_tokens: bool = False, user_id: int | None = None):
        return [t for t in self.existing if user_id is None or t.user_id == user_id]

    async def get_tenant_by_bot_id(self, bot_id: int, reveal_token: bool = False):
        return next((t for t in self.existing if t.bot_id == bot_id), None)

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


async def feed(registry: Registry, monkeypatch: Any = None, rejected: set[str] | None = None
               ) -> tuple[Any, Manager, FakeTelegram]:
    if monkeypatch is not None:
        import app.handlers.tenants as tenants_module
        from aiogram.exceptions import TelegramUnauthorizedError
        from aiogram.methods import GetMe

        class Probe(BaseSession):
            def __init__(self, token: str) -> None:
                super().__init__()
                self.token = token

            async def close(self) -> None:
                pass

            async def stream_content(self, *a: Any, **k: Any):  # pragma: no cover
                yield b""

            async def make_request(self, bot: Any, method: Any, timeout: int | None = None) -> Any:
                if self.token in (rejected or set()):
                    raise TelegramUnauthorizedError(method=GetMe(), message="Unauthorized")
                return aiogram.types.User(id=1, is_bot=True, first_name="alive")

        real_bot = tenants_module.Bot
        monkeypatch.setattr(tenants_module, "Bot", lambda token, **kw: real_bot(token=token, session=Probe(token)))
    session = FakeTelegram()
    bot = Bot(token=MASTER_TOKEN, session=session)
    dp = Dispatcher()
    manager = Manager()
    dp.include_router(create_tenant_router(dispatcher=dp, registry=registry, manager=manager,
                                           master_bot_id=MASTER_ID, admin_ids=frozenset({1})))
    result = await dp.feed_update(bot, Update.model_validate(managed_update(), context={"bot": bot}))
    return result, manager, session


@pytest.mark.asyncio
async def test_managed_bot_update_is_handled_and_registers_the_bot(monkeypatch):
    registry = Registry()
    result, manager, session = await feed(registry, monkeypatch)   # no real network
    assert result is not UNHANDLED
    assert "GetManagedBotToken" in session.calls
    assert len(registry.added) == 1
    added = registry.added[0]
    assert (added["user_id"], added["bot_id"], added["username"]) == (OWNER, CHILD, "NewZikoBot")
    assert manager.started == [CHILD]
    notices = [m for m in session.sent if m["chat_id"] == OWNER]
    assert len(notices) == 1 and "@NewZikoBot" in notices[0]["text"]
    assert CHILD_TOKEN not in notices[0]["text"]
    urls = [b.url for row in notices[0]["markup"].inline_keyboard for b in row]   # announce + links
    assert urls[-1] == "https://t.me/NewZikoBot" and "/start" in notices[0]["text"]


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



def test_welcome_and_connected_texts_need_no_commands_or_premium():
    from app.handlers import tenants
    kb = tenants._welcome_keyboard()
    assert [b.callback_data for row in kb.inline_keyboard for b in row] == [tenants.CREATE_CB, tenants.CONNECT_CB]
    text = tenants.connected_text("NewZikoBot")
    assert "@NewZikoBot" in text and "Premium" not in text and "Chatbots" in text



def _old_bot(enabled: bool, token: str):
    from types import SimpleNamespace
    return SimpleNamespace(user_id=OWNER, bot_id=8111111111, username="OldDeletedBot", enabled=enabled,
                           bot_token=token)


OLD_TOKEN = "8111111111:AAFakeOldTokenForTestsOnly_0123456789ab"


@pytest.mark.asyncio
async def test_owner_whose_bot_was_deleted_can_register_a_new_one(monkeypatch):
    """Regression: a bot deleted in @BotFather stayed in the registry (disabled) and blocked
    the owner forever: no new bot, no re-creation."""
    registry = Registry(existing=[_old_bot(False, OLD_TOKEN)])
    calls = []
    real_add = registry.add_tenant

    async def add(**kw):
        calls.append(kw.get("replace_bot_ids"))
        return await real_add(**kw)
    registry.add_tenant = add
    _r, manager, _s = await feed(registry, monkeypatch, rejected={OLD_TOKEN})
    assert manager.started == [CHILD]
    assert calls == [[8111111111]]               # the dead bot is replaced, nothing else


@pytest.mark.asyncio
async def test_disabled_but_alive_bot_is_not_replaced(monkeypatch):
    """A bot disabled by an admin (token still valid) must not be bypassed."""
    registry = Registry(existing=[_old_bot(False, OLD_TOKEN)])
    calls = []
    real_add = registry.add_tenant

    async def add(**kw):
        calls.append(kw.get("replace_bot_ids"))
        return await real_add(**kw)
    registry.add_tenant = add
    await feed(registry, monkeypatch, rejected=set())
    assert calls == [[]]



def test_connected_text_shows_botfather_steps_only_when_needed():
    from app.handlers.tenants import connected_text
    off = connected_text("NewZikoBot", business_mode=False)
    on = connected_text("NewZikoBot", business_mode=True)
    unknown = connected_text("NewZikoBot", business_mode=None)
    assert "Business Mode" in off and "خاموش" in off and "@BotFather" in off
    assert "@BotFather" not in on and "Chatbots" in on
    assert "@BotFather" in unknown and "اگر هنوز روشن نیست" in unknown
    for text in (off, on, unknown):
        assert "Premium" not in text and "/start" in text



def test_connected_keyboard_offers_botfather_unless_business_mode_is_on():
    from app.handlers.tenants import connected_keyboard
    def urls(mode):
        return [b.url for row in connected_keyboard("NewZikoBot", mode).inline_keyboard for b in row]
    assert urls(False) == ["https://t.me/BotFather", "https://t.me/NewZikoBot"]
    assert urls(None) == ["https://t.me/BotFather", "https://t.me/NewZikoBot"]
    assert urls(True) == ["https://t.me/NewZikoBot"]

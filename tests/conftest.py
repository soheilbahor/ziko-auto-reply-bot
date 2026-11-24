"""Shared fixtures for the ZIKO QA suite (Module 4).

Design rules
------------
* No real Telegram traffic: every send operation goes through mocks, and the
  admin harness uses an aiogram session subclass that records API calls and
  never touches the network.
* Production modules are owned by other modules. This file never edits them.
  It *locates* them (see ``MODULE_CANDIDATES``) and, when a required symbol is
  missing, skips with a message starting with ``CONTRACT MISMATCH:`` so the
  gap is reported instead of silently worked around.
* Set ``ZIKO_STRICT=1`` to turn every such skip into a hard failure (use this
  in CI once all modules are integrated).
* Module locations can be overridden per area with environment variables:
  ``ZIKO_DATABASE_MODULE``, ``ZIKO_CONFIG_MODULE``, ``ZIKO_BUSINESS_MODULE``,
  ``ZIKO_ADMIN_MODULE`` (dotted module path). Factories can be overridden with
  ``ZIKO_ENGINE_FACTORY`` and ``ZIKO_ADMIN_FACTORY`` ("package.module:callable").

Assumptions NOT fixed by the shared spec (reported in QA_REPORT_TEMPLATE.md)
---------------------------------------------------------------------------
A1. ``Database(database_path)`` constructor (positional or ``database_path=``).
A2. ``claim_inbound_event`` returns a truthy value when the event is new and a
    falsy value for a duplicate. ``claim_reply`` returns a ``claim_token``
    (truthy, str-like) or ``None``.
A3. Status keys are discovered via ``list_statuses()``; none are hardcoded.
A4. Business engine: factory called as ``factory(db=, sender=, settings=)``
    exposing ``handle_business_message(message)``; the sender mock records
    calls named ``send_message`` with ``business_connection_id`` as a kwarg.
A5. Admin router factory called as ``factory(db=, settings=)`` returning an
    aiogram ``Router`` or ``Dispatcher``; the panel is opened by ``/start``
    and is driven through inline keyboards (callback data is discovered, not
    hardcoded).
A6. ``load_settings()`` reads BOT_TOKEN, ADMIN_IDS (comma separated),
    DATABASE_PATH and LOG_LEVEL from the environment.

Delivery API (Module 1 update; MODULE1_CONTRACT.md was NOT available to QA, so
the exact signatures below are inferred and must be confirmed):
A10. ``complete_reply`` and ``release_reply`` are called with keyword arguments
    ``connection_id, chat_id, session_id, claim_token`` (+ ``outcome``,
    ``telegram_message_id``, ``error`` for ``complete_reply``).
A11. ``DeliveryOutcome`` (SENT / REJECTED / UNCERTAIN) is importable from the
    database module. SENT and UNCERTAIN are terminal for the session (no new
    claim); REJECTED and a release free the reservation for a retry.
A12. A completion/release with a stale token (already completed, released or
    superseded) either raises or returns falsy, and never changes the current
    holder's reservation.
A13. The database accepts a lease-length keyword (one of LEASE_KWARGS below)
    and ``expired_lease_policy``; the production default policy is "uncertain",
    i.e. an expired un-completed reservation is NOT re-granted.
A14. Engine error mapping: confirmed Telegram API rejections (aiogram
    TelegramBadRequest / TelegramForbiddenError / TelegramRetryAfter) ->
    REJECTED; network timeouts/resets and any other unexpected exception ->
    UNCERTAIN (QA expectation: unknown outcomes are ambiguous).
"""

from __future__ import annotations

import importlib
import os
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

PROJECT_ROOT = Path(__file__).resolve().parents[1]
for _p in (PROJECT_ROOT, PROJECT_ROOT / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

# ----------------------------------------------------------------------------
# Test constants (all fake)
# ----------------------------------------------------------------------------
BOT_TOKEN = "123456789:AAFakeTokenForTestsOnly_DoNotUse_0123456"
CONN_ID = "test-business-connection-1"
OWNER_ID = 1001          # the personal account that connected the bot
ADMIN_ID = 2001          # an admin who is NOT the owner (keeps cases separate)
CONTACT_ID = 3001        # an ordinary private contact
CONTACT_2_ID = 3002
STRANGER_ID = 4001       # not an admin, not the owner
PRIVATE_BODY = "SENTINEL-PRIVATE-BODY-7f3a9c-do-not-persist"
MSG_TS = 1_700_000_000

STRICT = os.environ.get("ZIKO_STRICT") == "1"

MODULE_CANDIDATES: dict[str, tuple[str, ...]] = {
    "database": ("app.database", "app.storage", "app.db", "ziko_assistant.database",
                 "ziko.database", "src.database", "database"),
    "config": ("app.config", "app.settings", "ziko_assistant.config",
               "ziko.config", "src.config", "config"),
    "business": ("app.business", "app.business_handler", "app.handlers.business",
                 "app.services.business", "ziko_assistant.business", "business"),
    "admin": ("app.admin", "app.handlers.admin", "app.admin_panel",
              "ziko_assistant.admin", "admin"),
}


# ----------------------------------------------------------------------------
# Contract resolution helpers
# ----------------------------------------------------------------------------
def mismatch(reason: str) -> None:
    """Report a contract mismatch: skip (default) or fail (ZIKO_STRICT=1)."""
    message = f"CONTRACT MISMATCH: {reason}"
    if STRICT:
        pytest.fail(message)
    pytest.skip(message)


def import_area(area: str) -> Any | None:
    """Import the first existing candidate module for ``area`` (or None)."""
    override = os.environ.get(f"ZIKO_{area.upper()}_MODULE")
    candidates = (override,) if override else MODULE_CANDIDATES[area]
    for name in candidates:
        try:
            return importlib.import_module(name)
        except ModuleNotFoundError as exc:
            # Only swallow "this candidate does not exist"; a missing
            # *dependency inside* an existing module is a real error.
            if exc.name and (name == exc.name or name.startswith(exc.name + ".")):
                continue
            raise
    return None


def require_symbol(area: str, *names: str) -> Any:
    module = import_area(area)
    if module is None:
        mismatch(f"no '{area}' module found (tried {MODULE_CANDIDATES[area]}; "
                 f"override with ZIKO_{area.upper()}_MODULE)")
    for name in names:
        if hasattr(module, name):
            return getattr(module, name)
    mismatch(f"module '{module.__name__}' has none of {names}")


def load_factory(env_var: str, area: str, names: Iterable[str]) -> Any:
    spec = os.environ.get(env_var)
    if spec:
        mod_name, _, attr = spec.partition(":")
        return getattr(importlib.import_module(mod_name), attr)
    return require_symbol(area, *tuple(names))


def is_granted(result: Any) -> bool:
    """Interpret a claim_* result: a claim token / True means granted, None / False rejected (A2)."""
    if isinstance(result, bool):
        return result
    if result is None:
        return False
    for attr in ("claimed", "reserved", "acquired", "granted", "ok", "success"):
        if hasattr(result, attr):
            return bool(getattr(result, attr))
    return bool(result)


# ----------------------------------------------------------------------------
# Database fixtures
# ----------------------------------------------------------------------------
def make_database(path: Path, **kwargs: Any) -> Any:
    database_cls = require_symbol("database", "Database")
    try:
        return database_cls(path, **kwargs)
    except TypeError:
        try:
            return database_cls(database_path=path, **kwargs)
        except TypeError as exc:
            mismatch(f"cannot construct Database(path, **{sorted(kwargs)}): {exc}")


LEASE_KWARGS = ("lease_seconds", "claim_lease_seconds", "lease_timeout_seconds",
                "lease_ttl_seconds")


def make_leased_database(path: Path, lease: float, **extra: Any) -> Any:
    """Database with a short reservation lease (keyword name is an assumption, A13)."""
    database_cls = require_symbol("database", "Database")
    for name in LEASE_KWARGS:
        for path_kw in ({}, {"database_path": None}):
            try:
                if path_kw:
                    return database_cls(database_path=path, **{name: lease}, **extra)
                return database_cls(path, **{name: lease}, **extra)
            except TypeError:
                continue
    mismatch(f"Database accepts none of the lease keywords {LEASE_KWARGS} "
             f"(extra={sorted(extra)})")


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    return data_dir / "ziko-test.db"


@pytest_asyncio.fixture
async def db(db_path: Path):
    database = make_database(db_path)
    await database.initialize()
    try:
        yield database
    finally:
        await database.close()


async def begin_session(database: Any, key: str | None = None, index: int = 0) -> Any:
    """Activate a status and make sure auto-reply is enabled; return AppState."""
    if key is None:
        statuses = await database.list_statuses()
        assert len(statuses) > index, "expected at least %d seeded statuses" % (index + 1)
        key = statuses[index].key
    await database.activate_status(key)
    state = await database.get_app_state()
    if not state.enabled:
        await database.enable_auto_reply()
        state = await database.get_app_state()
    assert state.enabled, "auto-reply should be enabled after explicit activation"
    assert state.active_status_key == key
    assert state.session_id, "an active session must have a session_id"
    return state


def delivery_outcome() -> Any:
    return require_symbol("database", "DeliveryOutcome")


async def attempt(awaitable: Any) -> Any:
    """Await a call that is expected to be refused (stale token).

    Returns the raised exception or the returned value. A TypeError means the
    call signature differs from assumption A10, which is reported as a contract
    mismatch rather than being mistaken for a refusal.
    """
    try:
        return await awaitable
    except TypeError as exc:
        mismatch(f"delivery API signature differs from assumption A10: {exc}")
    except Exception as exc:  # noqa: BLE001 - any refusal style is acceptable
        return exc


async def claim(database: Any, chat: int, session_id: str, conn: str = "test-business-connection-1") -> Any:
    return await database.claim_reply(conn, chat, session_id)


def _completion_kwargs(chat: int, session_id: str, token: Any, conn: str) -> dict[str, Any]:
    return dict(connection_id=conn, chat_id=chat, session_id=session_id, claim_token=token)


async def complete(database: Any, chat: int, session_id: str, token: Any, outcome: Any, *,
                   conn: str = "test-business-connection-1",
                   message_id: int | None = None, error: str | None = None) -> Any:
    kwargs = _completion_kwargs(chat, session_id, token, conn)
    kwargs["outcome"] = outcome
    if message_id is not None:
        kwargs["telegram_message_id"] = message_id
    if error is not None:
        kwargs["error"] = error
    try:
        return await database.complete_reply(**kwargs)
    except TypeError as exc:
        mismatch(f"complete_reply signature differs from assumption A10: {exc}")


async def release(database: Any, chat: int, session_id: str, token: Any,
                  conn: str = "test-business-connection-1") -> Any:
    try:
        return await database.release_reply(**_completion_kwargs(chat, session_id, token, conn))
    except TypeError as exc:
        mismatch(f"release_reply signature differs from assumption A10: {exc}")


def dump_database_text(path: Path) -> str:
    """Return every table's rows as text plus the raw file bytes (and WAL)."""
    chunks: list[str] = []
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        for line in conn.iterdump():
            chunks.append(line)
    finally:
        conn.close()
    for suffix in ("", "-wal", "-journal"):
        candidate = Path(str(path) + suffix)
        if candidate.exists():
            chunks.append(candidate.read_bytes().decode("latin-1"))
    return "\n".join(chunks)


# ----------------------------------------------------------------------------
# Settings fixture
# ----------------------------------------------------------------------------
@pytest.fixture
def settings(tmp_path: Path) -> Any:
    values = dict(
        bot_token=BOT_TOKEN,
        admin_ids=frozenset({ADMIN_ID, OWNER_ID}),
        database_path=tmp_path / "data" / "ziko-test.db",
        log_level="DEBUG",
    )
    module = import_area("config")
    settings_cls = getattr(module, "Settings", None) if module else None
    if settings_cls is not None:
        try:
            return settings_cls(**values)
        except TypeError:
            pass  # fall through to a duck-typed stand-in with the contract fields
    return SimpleNamespace(**values)


# ----------------------------------------------------------------------------
# Fake Telegram objects
# ----------------------------------------------------------------------------
class FakeTelegramError(Exception):
    """Generic stand-in error (unknown kind => treated as ambiguous by the engine)."""


def confirmed_rejection(kind: str = "bad_request", text: str = "Bad Request: chat not found") -> Exception:
    """A real aiogram exception that Telegram's API actually returned (REJECTED)."""
    exceptions = pytest.importorskip("aiogram.exceptions")
    from aiogram.methods import SendMessage

    method = SendMessage(chat_id=CONTACT_ID, text="x")
    if kind == "bad_request":
        return exceptions.TelegramBadRequest(method=method, message=text)
    if kind == "forbidden":
        return exceptions.TelegramForbiddenError(method=method, message=text)
    if kind == "retry_after":
        return exceptions.TelegramRetryAfter(method=method, message=text, retry_after=1)
    raise ValueError(kind)


def ambiguous_failure(kind: str = "timeout", text: str = "timed out") -> Exception:
    """A failure after which we cannot know whether Telegram delivered (UNCERTAIN)."""
    if kind == "timeout":
        return TimeoutError(text)
    if kind == "reset":
        return ConnectionResetError(text)
    if kind == "aiogram_network":
        exceptions = pytest.importorskip("aiogram.exceptions")
        from aiogram.methods import SendMessage

        return exceptions.TelegramNetworkError(
            method=SendMessage(chat_id=CONTACT_ID, text="x"), message=text)
    if kind == "unexpected":
        return RuntimeError(text)
    raise ValueError(kind)


def make_message(
    *,
    message_id: int,
    chat_id: int = CONTACT_ID,
    chat_type: str = "private",
    from_id: int | None = None,
    is_bot: bool = False,
    text: str = PRIVATE_BODY,
    connection_id: str | None = CONN_ID,
    edited: bool = False,
) -> Any:
    """Build an incoming business message (real aiogram Message when possible)."""
    sender_id = chat_id if from_id is None else from_id
    chat: dict[str, Any] = {"id": chat_id, "type": chat_type}
    if chat_type == "private":
        chat["first_name"] = "Contact"
    else:
        chat["title"] = "Some chat"
    payload: dict[str, Any] = {
        "message_id": message_id,
        "date": MSG_TS,
        "chat": chat,
        "text": text,
    }
    if chat_type != "channel":
        payload["from"] = {"id": sender_id, "is_bot": is_bot, "first_name": "Sender"}
    if connection_id is not None:
        payload["business_connection_id"] = connection_id
    if edited:
        payload["edit_date"] = MSG_TS + 60
    try:
        from aiogram.types import Message

        return Message.model_validate(payload)
    except Exception:  # aiogram missing or field unsupported -> duck-typed fallback
        return SimpleNamespace(
            message_id=message_id,
            date=MSG_TS,
            chat=SimpleNamespace(id=chat_id, type=chat_type),
            from_user=None if chat_type == "channel" else SimpleNamespace(
                id=sender_id, is_bot=is_bot),
            text=text,
            business_connection_id=connection_id,
            edit_date=(MSG_TS + 60) if edited else None,
        )


def make_connection_update(*, connection_id: str = CONN_ID, owner_id: int = OWNER_ID,
                           is_enabled: bool = True, can_reply: bool | None = True) -> Any:
    rights = None if can_reply is None else SimpleNamespace(can_reply=can_reply)
    return SimpleNamespace(
        id=connection_id,
        user=SimpleNamespace(id=owner_id, is_bot=False),
        user_chat_id=owner_id,
        date=MSG_TS,
        is_enabled=is_enabled,
        rights=rights,
        can_reply=can_reply,  # older field name, harmless for duck typing
    )


def sent_messages(sender: Any) -> list[dict[str, Any]]:
    """Normalise every send-like call recorded on the sender mock."""
    calls: list[dict[str, Any]] = []
    for name, args, kwargs in sender.mock_calls:
        base = name.split(".")[-1]
        if "(" in name or not base.lower().startswith("send"):
            continue
        record = dict(kwargs)
        for key, value in zip(("chat_id", "text"), args):
            record.setdefault(key, value)
        calls.append(record)
    return calls


@pytest.fixture
def sender() -> AsyncMock:
    """Mocked outbound Telegram client. Nothing here can reach the network."""
    mock = AsyncMock()
    result = SimpleNamespace(message_id=9001)
    mock.send_message.return_value = result
    mock.send.return_value = result
    return mock


class SpyDatabase:
    """Delegating wrapper recording the delivery-API calls the engine makes."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.granted: list[Any] = []
        self.rejected_claims = 0
        self.settlements: list[dict[str, Any]] = []
        self.fail_status_lookup = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def get_status(self, *args: Any, **kwargs: Any) -> Any:
        if self.fail_status_lookup:
            raise RuntimeError("injected status lookup failure")
        return await self._inner.get_status(*args, **kwargs)

    async def list_statuses(self, *args: Any, **kwargs: Any) -> Any:
        if self.fail_status_lookup:
            raise RuntimeError("injected status lookup failure")
        return await self._inner.list_statuses(*args, **kwargs)

    async def claim_reply(self, *args: Any, **kwargs: Any) -> Any:
        token = await self._inner.claim_reply(*args, **kwargs)
        if token:
            self.granted.append(token)
        else:
            self.rejected_claims += 1
        return token

    def _record(self, kind: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
        values = list(args) + list(kwargs.values())
        token = next((v for v in values if isinstance(v, str) and v in self.granted), None)
        enum_cls = delivery_outcome()
        outcome = next((v for v in values if isinstance(v, enum_cls)), None)
        self.settlements.append({
            "kind": kind, "token": token, "outcome": outcome,
            "message_id": kwargs.get("telegram_message_id"),
            "values": values,
        })

    async def complete_reply(self, *args: Any, **kwargs: Any) -> Any:
        self._record("complete", args, kwargs)
        return await self._inner.complete_reply(*args, **kwargs)

    async def release_reply(self, *args: Any, **kwargs: Any) -> Any:
        self._record("release", args, kwargs)
        return await self._inner.release_reply(*args, **kwargs)

    # -- assertions used by the engine tests ---------------------------------
    def outcome_names(self) -> list[str]:
        return [s["outcome"].name for s in self.settlements if s["kind"] == "complete"
                and s["outcome"] is not None]

    def assert_every_claim_settled_once(self) -> None:
        """Each granted token must reach exactly one complete_reply/release_reply."""
        for token in self.granted:
            matching = [s for s in self.settlements if s["token"] == token]
            assert len(matching) == 1, (
                f"claim token {token!r} was settled {len(matching)} times (expected exactly 1)")
        stray = [s for s in self.settlements if s["token"] is None]
        assert not stray, "a completion/release was made without a granted claim token"


@dataclass
class EngineHarness:
    engine: Any
    sender: AsyncMock
    db: SpyDatabase
    settings: Any

    async def deliver(self, message: Any) -> Any:
        for name in ("handle_business_message", "handle_message", "process_business_message"):
            method = getattr(self.engine, name, None)
            if method is not None:
                return await method(message)
        mismatch("engine exposes none of handle_business_message / handle_message / "
                 "process_business_message")

    async def connection_update(self, connection: Any) -> Any:
        for name in ("handle_business_connection", "on_business_connection",
                     "process_business_connection"):
            method = getattr(self.engine, name, None)
            if method is not None:
                return await method(connection)
        mismatch("engine exposes no business_connection handler")

    @property
    def sent(self) -> list[dict[str, Any]]:
        return sent_messages(self.sender)


@pytest_asyncio.fixture
async def harness(db: Any, sender: AsyncMock, settings: Any) -> EngineHarness:
    factory = load_factory("ZIKO_ENGINE_FACTORY", "business",
                           ("BusinessMessageEngine", "BusinessEngine", "BusinessHandler",
                            "create_engine", "build_engine"))
    spy = SpyDatabase(db)
    try:
        engine = factory(db=spy, sender=sender, settings=settings)
    except TypeError as exc:
        mismatch(f"engine factory rejects (db=, sender=, settings=): {exc}")
    return EngineHarness(engine=engine, sender=sender, db=spy, settings=settings)


@pytest_asyncio.fixture
async def armed(harness: EngineHarness) -> EngineHarness:
    """Enabled connection with reply rights + active status + auto-reply ON."""
    await harness.db.save_business_connection(CONN_ID, True, True, OWNER_ID)
    await begin_session(harness.db, index=0)
    return harness


# ----------------------------------------------------------------------------
# Admin panel harness (aiogram, recorded session, zero network)
# ----------------------------------------------------------------------------
@dataclass
class AdminHarness:
    dispatcher: Any
    bot: Any
    session: Any
    db: Any
    _update_id: int = 0
    _message_id: int = 100
    outgoing_start: int = 0

    def _next_update(self) -> int:
        self._update_id += 1
        return self._update_id

    def _next_message(self) -> int:
        self._message_id += 1
        return self._message_id

    @staticmethod
    def _user(user_id: int, *, bot: bool = False) -> dict[str, Any]:
        return {"id": user_id, "is_bot": bot, "first_name": "U%d" % user_id}

    async def send_text(self, user_id: int, text: str, chat_id: int | None = None) -> None:
        from aiogram.types import Update

        chat = chat_id or user_id
        message: dict[str, Any] = {
            "message_id": self._next_message(), "date": MSG_TS,
            "chat": {"id": chat, "type": "private", "first_name": "U"},
            "from": self._user(user_id), "text": text,
        }
        if text.startswith("/"):
            command = text.split()[0]
            message["entities"] = [{"type": "bot_command", "offset": 0, "length": len(command)}]
        update = Update.model_validate({"update_id": self._next_update(), "message": message})
        await self.dispatcher.feed_update(self.bot, update)

    async def press(self, user_id: int, data: str, chat_id: int | None = None) -> None:
        from aiogram.types import Update

        chat = chat_id or user_id
        callback = {
            "id": "cb-%d" % self._next_update(),
            "from": self._user(user_id),
            "chat_instance": "test-instance",
            "data": data,
            "message": {
                "message_id": self._next_message(), "date": MSG_TS,
                "chat": {"id": chat, "type": "private", "first_name": "U"},
                "from": self._user(999_000_001, bot=True),
                "text": "panel",
            },
        }
        update = Update.model_validate({"update_id": self._update_id + 1,
                                        "callback_query": callback})
        self._update_id += 1
        await self.dispatcher.feed_update(self.bot, update)

    # -- observation helpers -------------------------------------------------
    @property
    def calls(self) -> list[tuple[str, dict[str, Any]]]:
        return self.session.calls

    def mark(self) -> int:
        return len(self.session.calls)

    def calls_since(self, mark: int) -> list[tuple[str, dict[str, Any]]]:
        return self.session.calls[mark:]

    @staticmethod
    def callback_data_in(calls: Iterable[tuple[str, dict[str, Any]]]) -> list[str]:
        found: list[str] = []
        for _name, payload in calls:
            markup = payload.get("reply_markup") or {}
            for row in markup.get("inline_keyboard", []) if isinstance(markup, dict) else []:
                for button in row:
                    if button.get("callback_data"):
                        found.append(button["callback_data"])
        return found

    async def snapshot(self) -> dict[str, Any]:
        """Comparable picture of everything an admin action could change."""
        state = await self.db.get_app_state()
        statuses = await self.db.list_statuses()
        return {
            "enabled": state.enabled,
            "active": state.active_status_key,
            "session": state.session_id,
            "texts": {s.key: s.reply_text for s in statuses},
        }


def _build_recording_session() -> Any:
    from aiogram.client.session.base import BaseSession
    from aiogram.types import Chat, Message, User

    class RecordingSession(BaseSession):
        def __init__(self) -> None:
            super().__init__()
            self.calls: list[tuple[str, dict[str, Any]]] = []

        async def close(self) -> None:  # pragma: no cover - nothing to close
            return None

        async def stream_content(self, *args: Any, **kwargs: Any):  # pragma: no cover
            if False:
                yield b""

        async def make_request(self, bot: Any, method: Any, timeout: Any = None) -> Any:
            self.calls.append((type(method).__name__, method.model_dump(exclude_none=True)))
            returning = str(getattr(method, "__returning__", bool))
            if "Message" in returning and "Union" not in returning and "bool" not in returning:
                return Message(message_id=5000 + len(self.calls), date=MSG_TS,
                               chat=Chat(id=1, type="private"),
                               from_user=User(id=999_000_001, is_bot=True, first_name="bot"),
                               text="stub")
            return True

    return RecordingSession()


@pytest_asyncio.fixture
async def admin(db: Any, settings: Any) -> AdminHarness:
    pytest.importorskip("aiogram", reason="aiogram is required for admin-panel tests")
    from aiogram import Bot, Dispatcher, Router
    from aiogram.fsm.storage.memory import MemoryStorage

    factory = load_factory("ZIKO_ADMIN_FACTORY", "admin",
                           ("create_admin_router", "build_admin_router", "setup_admin_router",
                            "create_router", "get_router", "create_dispatcher"))
    try:
        built = factory(db=db, settings=settings)
    except TypeError as exc:
        mismatch(f"admin factory rejects (db=, settings=): {exc}")
    if isinstance(built, Dispatcher):
        dispatcher = built
    elif isinstance(built, Router):
        dispatcher = Dispatcher(storage=MemoryStorage())
        dispatcher.include_router(built)
    else:
        mismatch(f"admin factory returned {type(built).__name__}, expected Router/Dispatcher")
    session = _build_recording_session()
    bot = Bot(token=BOT_TOKEN, session=session)
    return AdminHarness(dispatcher=dispatcher, bot=bot, session=session, db=db)

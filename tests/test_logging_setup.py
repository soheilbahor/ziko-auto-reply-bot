"""Phase 3 logging tests: redaction, dedupe, rotation, per-bot routing, correlation, alerts.

Real files are written under pytest's tmp_path. Telegram is never contacted.
"""
from __future__ import annotations

import asyncio
import io
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from conftest import ADMIN_ID, CONN_ID, OWNER_ID, begin_session, make_message

from app.handlers.admin import format_problem_report
from app.logging_setup import (
    BOT_ID,
    CORRELATION_ID,
    AlertSink,
    CorrelationMiddleware,
    Deduplicator,
    LogConfig,
    TenantLogRouter,
    correlation_scope,
    load_log_config,
    log_file_for_bot,
    operation,
    recent_problems,
    redact,
    setup_logging,
)
from app.services.auto_reply import BusinessMessageEngine
from app.storage import Database

# A well-formed but fake token. It is never used for any request.
TOKEN = "123456789:AAFakeTokenFakeTokenFakeToken0123456"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


@pytest.fixture
def install(tmp_path: Path) -> Any:
    """Install the ZIKO pipeline on the root logger for one test, then restore pytest's handlers."""
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    installed: list[Any] = []

    def _install(**overrides: Any) -> SimpleNamespace:
        config = LogConfig(directory=tmp_path / "logs", **overrides)
        stream = io.StringIO()
        pipeline = setup_logging(config, level="INFO", stream=stream)
        installed.append(pipeline)
        return SimpleNamespace(pipeline=pipeline, config=config, console=stream)

    yield _install
    for pipeline in installed:
        pipeline.close()
    root.handlers[:] = saved_handlers
    root.setLevel(saved_level)


# --------------------------------------------------------------------------- #
# Redaction
# --------------------------------------------------------------------------- #
def test_redact_removes_tokens_passwords_bearer_and_keys() -> None:
    fernet_like = "a" * 43 + "="
    text = (
        f"GET https://api.telegram.org/bot{TOKEN}/getMe password=hunter2 "
        f"secret: s3cr3t bot_token={TOKEN} Authorization: Bearer abc.def-ghi key={fernet_like}"
    )
    out = redact(text)
    for secret in (TOKEN, "hunter2", "s3cr3t", "abc.def-ghi", fernet_like):
        assert secret not in out
    assert "password=[REDACTED]" in out


def test_redact_leaves_operational_text_readable() -> None:
    line = "Child bot polling failed user_id=1001 bot_id=123456789 error=TelegramBadRequest retry=2"
    assert redact(line) == line
    assert redact("Admin action user_id=1 action=save_status_text key=status_a") == (
        "Admin action user_id=1 action=save_status_text key=status_a"
    )


def test_secrets_never_reach_files_or_console(install: Any) -> None:
    env = install(dedup_seconds=0)
    log = logging.getLogger("ziko.test")
    try:
        raise RuntimeError(f"request failed for https://api.telegram.org/bot{TOKEN}/getMe")
    except RuntimeError:
        log.exception("Probe failed token=%s", TOKEN)
    log.warning("Settings loaded password=%s", "hunter2")

    master = _read(env.config.directory / "master.log")
    assert "Probe failed" in master
    assert "Traceback" in master  # the traceback is kept, only redacted
    assert TOKEN not in master and "hunter2" not in master
    assert TOKEN not in env.console.getvalue()


# --------------------------------------------------------------------------- #
# Duplicate suppression
# --------------------------------------------------------------------------- #
def test_hundred_identical_errors_write_one_line(install: Any) -> None:
    env = install()  # default window: 60 seconds
    log = logging.getLogger("ziko.test")
    for _ in range(100):
        log.error("Child bot polling failed error=%s", "TelegramNetworkError")
    env.pipeline.flush_summaries()
    master = _read(env.config.directory / "master.log")
    error_lines = [line for line in master.splitlines()
                   if " ERROR " in line and "Child bot polling failed" in line]
    assert len(error_lines) == 1
    assert "Suppressed 99 repeats of: Child bot polling failed error=%s" in master


def test_deduplicator_reports_repeats_when_window_reopens() -> None:
    now = [0.0]
    dedup = Deduplicator(60, clock=lambda: now[0])
    key = ("k",)
    assert dedup.admit(key) == 0
    for _ in range(99):
        assert dedup.admit(key) is None
    now[0] = 61.0
    assert dedup.admit(key) == 99
    assert dedup.admit(key) is None


# --------------------------------------------------------------------------- #
# Rotation
# --------------------------------------------------------------------------- #
def test_master_log_rotates_and_stays_within_limit(install: Any) -> None:
    env = install(max_bytes=2000, backup_count=2, dedup_seconds=0)
    log = logging.getLogger("ziko.test")
    for index in range(300):
        log.info("routine event number %d with some padding text", index)
    directory = env.config.directory
    assert (directory / "master.log").stat().st_size <= 2000
    assert (directory / "master.log.1").exists()
    assert not (directory / "master.log.3").exists()  # backup_count bounds the history


# --------------------------------------------------------------------------- #
# Per-bot routing
# --------------------------------------------------------------------------- #
def test_child_errors_stay_in_their_own_bot_file(install: Any) -> None:
    env = install(dedup_seconds=0)
    log = logging.getLogger("ziko.tenant_bots")
    directory = env.config.directory
    with correlation_scope(bot_id=111):
        log.error("Child bot polling failed user_id=1 error=TelegramNetworkError retry=1")
    with correlation_scope(bot_id=222):
        log.error("Child bot polling failed user_id=2 error=TimeoutError retry=1")
    log.error("explicit scope user_id=3", extra={"bot_id": 333})
    log.warning("platform-wide event")

    bot_a = _read(log_file_for_bot(directory, 111))
    bot_b = _read(log_file_for_bot(directory, 222))
    bot_c = _read(log_file_for_bot(directory, 333))
    master = _read(directory / "master.log")
    assert "user_id=1 " in bot_a and "user_id=2" not in bot_a
    assert "user_id=2" in bot_b and "user_id=1 " not in bot_b
    assert "user_id=3" in bot_c
    assert "platform-wide" in master
    assert "user_id=" not in master  # child records never leak into the master file


def test_tasks_created_inside_a_child_scope_inherit_its_file(install: Any) -> None:
    env = install(dedup_seconds=0)

    async def child_task() -> None:
        await asyncio.sleep(0)
        logging.getLogger("aiogram.event").error("inherited by a task")

    async def main() -> None:
        with correlation_scope(bot_id=444):
            await asyncio.create_task(child_task())

    asyncio.run(main())
    assert "inherited by a task" in _read(log_file_for_bot(env.config.directory, 444))
    assert "inherited" not in _read(env.config.directory / "master.log")


def test_tenant_router_keeps_few_files_open(tmp_path: Path) -> None:
    router = TenantLogRouter(
        tmp_path, max_bytes=10_000, backup_count=1,
        formatter=logging.Formatter("%(message)s"), max_open=2,
    )
    for bot in ("1", "2", "3"):
        record = logging.LogRecord("t", logging.ERROR, __file__, 1, "x", (), None)
        record.bot_id = bot
        router.handle(record)
    assert len(router._handlers) == 2  # bounded open files is the contract
    assert (tmp_path / "tenants" / "bot-1.log").exists()
    router.close()


# --------------------------------------------------------------------------- #
# Correlation
# --------------------------------------------------------------------------- #
def test_middleware_scopes_child_updates_and_not_master_updates() -> None:
    seen: dict[str, Any] = {}

    async def handler(event: object, data: dict[str, Any]) -> None:
        seen["bot"] = BOT_ID.get()
        seen["cid"] = CORRELATION_ID.get()

    async def run_both() -> None:
        await CorrelationMiddleware(tenant=True)(handler, object(), {"bot": SimpleNamespace(id=555)})
        assert seen["bot"] == 555 and seen["cid"]
        first = seen["cid"]
        await CorrelationMiddleware(tenant=False)(handler, object(), {"bot": SimpleNamespace(id=555)})
        assert seen["bot"] is None
        assert seen["cid"] and seen["cid"] != first

    asyncio.run(run_both())


async def _failing_business_reply(tmp_path: Path) -> None:
    """A real engine whose final DB write fails after the message was sent."""
    db = Database(tmp_path / "ziko.sqlite3")
    await db.initialize()
    await db.save_business_connection(CONN_ID, True, True, OWNER_ID)
    await begin_session(db, index=0)

    class Sender:
        async def send_message(self, **kwargs: Any) -> SimpleNamespace:
            return SimpleNamespace(message_id=9)

    async def broken_complete_reply(**kwargs: Any) -> None:
        raise RuntimeError("database is locked")

    db.complete_reply = broken_complete_reply  # type: ignore[method-assign]
    settings = SimpleNamespace(admin_ids=frozenset({ADMIN_ID}), business_owner_id=OWNER_ID)
    engine = BusinessMessageEngine(db=db, sender=Sender(), settings=settings)
    business_log = logging.getLogger("ziko.business")
    with correlation_scope(), pytest.raises(RuntimeError), operation("BUSINESS_MESSAGE", business_log):
        business_log.info("business update received")
        await engine.handle_business_message(make_message(message_id=1))
    await db.close()


def test_failed_business_write_reports_stage_under_one_correlation_id(install: Any, tmp_path: Path) -> None:
    env = install(dedup_seconds=0)
    asyncio.run(_failing_business_reply(tmp_path))
    lines = [line for line in _read(env.config.directory / "master.log").splitlines()
             if "business update received" in line or "operation=BUSINESS_MESSAGE failed" in line]
    assert len(lines) == 2
    cids = {line.split(" cid=")[1].split(" ")[0] for line in lines}
    assert len(cids) == 1 and "-" not in cids
    failed = next(line for line in lines if "operation=BUSINESS_MESSAGE failed" in line)
    assert "stage=DATABASE_SAVE" in failed
    assert "RuntimeError" in _read(env.config.directory / "master.log")


# --------------------------------------------------------------------------- #
# Critical alerts
# --------------------------------------------------------------------------- #
def test_critical_alert_goes_to_admins_once_and_without_secrets(install: Any) -> None:
    env = install(dedup_seconds=0)
    sent: list[tuple[int, str]] = []

    async def send(admin_id: int, text: str) -> None:
        sent.append((admin_id, text))

    sink = AlertSink(send=send, admin_ids=[ADMIN_ID], min_interval_seconds=300)
    env.pipeline.set_alert_sink(sink)

    async def main() -> None:
        log = logging.getLogger("ziko.main")
        for _ in range(3):
            log.critical("Master polling stopped with an unexpected error token=%s", TOKEN)
        await sink.drain()

    asyncio.run(main())
    assert len(sent) == 1
    admin_id, text = sent[0]
    assert admin_id == ADMIN_ID
    assert "Master polling stopped" in text
    assert TOKEN not in text


def test_alert_pipeline_never_alerts_about_itself(install: Any) -> None:
    env = install(dedup_seconds=0)
    sent: list[tuple[int, str]] = []

    async def send(admin_id: int, text: str) -> None:
        sent.append((admin_id, text))

    sink = AlertSink(send=send, admin_ids=[ADMIN_ID], min_interval_seconds=0)
    env.pipeline.set_alert_sink(sink)

    async def main() -> None:
        logging.getLogger("ziko.alerts").critical("alert delivery itself failed")
        await sink.drain()

    asyncio.run(main())
    assert sent == []


def test_failed_alert_delivery_is_logged_not_raised(install: Any) -> None:
    env = install(dedup_seconds=0)

    async def send(admin_id: int, text: str) -> None:
        raise RuntimeError("telegram unreachable")

    sink = AlertSink(send=send, admin_ids=[ADMIN_ID], min_interval_seconds=0)
    env.pipeline.set_alert_sink(sink)

    async def main() -> None:
        logging.getLogger("ziko.main").critical("boom")
        await sink.drain()

    asyncio.run(main())
    master = _read(env.config.directory / "master.log")
    assert "Could not deliver critical alert" in master
    assert "RuntimeError" in master


# --------------------------------------------------------------------------- #
# Configuration and admin views
# --------------------------------------------------------------------------- #
def test_log_config_defaults_follow_database_directory(tmp_path: Path) -> None:
    config = load_log_config(tmp_path / "ziko.sqlite3", {})
    assert config.directory == tmp_path / "logs"
    assert config.max_bytes == 5 * 1024 * 1024
    assert config.backup_count == 5
    assert config.dedup_seconds == 60
    assert config.alerts_enabled is True
    assert config.alert_interval_seconds == 300


def test_log_config_rejects_out_of_range_values(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="ZIKO_LOG_BACKUP_COUNT must be"):
        load_log_config(tmp_path / "ziko.sqlite3", {"ZIKO_LOG_BACKUP_COUNT": "99"})
    with pytest.raises(ValueError, match="ZIKO_LOG_ALERTS must be"):
        load_log_config(tmp_path / "ziko.sqlite3", {"ZIKO_LOG_ALERTS": "maybe"})


def test_recent_problems_returns_only_warnings_and_errors(install: Any, tmp_path: Path) -> None:
    env = install(dedup_seconds=0)
    log = logging.getLogger("ziko.test")
    log.info("routine")
    log.warning("first warning")
    log.error("failure password=hunter2")
    path = env.config.directory / "master.log"
    problems = recent_problems(path)
    assert len(problems) == 2
    assert "first warning" in problems[0]
    assert "hunter2" not in "\n".join(problems)
    assert len(recent_problems(path, limit=1)) == 1
    assert recent_problems(tmp_path / "missing.log") == []


def test_problem_report_escapes_html_and_handles_empty() -> None:
    report = format_problem_report(["2026 ERROR <script> & more"], "T")
    assert "<script>" not in report
    assert "&lt;script&gt;" in report and "<pre>" in report
    assert "هیچ خطا" in format_problem_report([], "T")


# --------------------------------------------------------------------------- #
# /logs through a real aiogram dispatcher (recording session, no network)
# --------------------------------------------------------------------------- #
async def _child_logs_command(tmp_path: Path, log_path: Path | None) -> list[str]:
    from aiogram import Bot, Dispatcher
    from aiogram.fsm.storage.memory import MemoryStorage
    from conftest import AdminHarness, _build_recording_session

    from app.config import load_settings
    from app.handlers.admin import create_admin_router

    settings = load_settings({
        "BOT_TOKEN": TOKEN, "ADMIN_IDS": str(ADMIN_ID),
        "DATABASE_PATH": str(tmp_path / "child.sqlite3"),
    })
    db = Database(tmp_path / "child.sqlite3")
    await db.initialize()
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(CorrelationMiddleware(tenant=True))
    dp.include_router(create_admin_router(db, settings, log_path=log_path))
    session = _build_recording_session()
    bot = Bot(token=TOKEN, session=session)
    harness = AdminHarness(dispatcher=dp, bot=bot, session=session, db=db)
    try:
        await harness.send_text(ADMIN_ID, "/logs")
    finally:
        await db.close()
    return [str(payload.get("text", "")) for name, payload in session.calls if name == "SendMessage"]


def test_child_logs_command_shows_only_that_bots_problems(install: Any, tmp_path: Path) -> None:
    env = install(dedup_seconds=0)
    log = logging.getLogger("ziko.tenant_bots")
    with correlation_scope(bot_id=111):
        log.error("own bot problem user_id=1 error=TimeoutError")
    with correlation_scope(bot_id=222):
        log.error("other bot problem user_id=2 error=TimeoutError")

    replies = asyncio.run(_child_logs_command(
        tmp_path, log_path=log_file_for_bot(env.config.directory, 111),
    ))
    text = "\n".join(replies)
    assert "own bot problem" in text
    assert "other bot problem" not in text


def test_child_logs_command_without_a_log_file_says_so(tmp_path: Path) -> None:
    replies = asyncio.run(_child_logs_command(tmp_path, log_path=None))
    assert any("در دسترس نیست" in reply for reply in replies)


async def _master_logs_commands(tmp_path: Path, log_dir: Path, texts: list[str]) -> list[str]:
    from aiogram import Bot, Dispatcher
    from aiogram.fsm.storage.memory import MemoryStorage
    from conftest import AdminHarness, _build_recording_session

    from app.handlers.master import create_master_router
    from app.tenant_bots import TenantBotManager
    from app.tenant_registry import TenantRegistry

    settings = SimpleNamespace(database_path=tmp_path / "ziko.sqlite3", admin_ids=frozenset({ADMIN_ID}))
    registry = TenantRegistry(tmp_path / "tenants.sqlite3")
    session = _build_recording_session()
    bot = Bot(token=TOKEN, session=session)
    manager = TenantBotManager(settings=settings, registry=registry, master_bot=bot)
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(CorrelationMiddleware(tenant=False))
    dp.include_router(create_master_router(
        registry=registry, manager=manager, admin_ids=frozenset({ADMIN_ID}), log_dir=log_dir,
    ))
    harness = AdminHarness(dispatcher=dp, bot=bot, session=session, db=None)
    for text in texts:
        await harness.send_text(ADMIN_ID, text)
    return [str(payload.get("text", "")) for name, payload in session.calls if name == "SendMessage"]


def test_master_logs_command_shows_platform_and_per_bot_problems(install: Any, tmp_path: Path) -> None:
    env = install(dedup_seconds=0)
    logging.getLogger("ziko.main").error("platform problem")
    with correlation_scope(bot_id=222):
        logging.getLogger("ziko.tenant_bots").error("child 222 problem")

    replies = asyncio.run(_master_logs_commands(
        tmp_path, env.config.directory, ["/logs", "/logs 222", "/logs abc"],
    ))
    assert "platform problem" in replies[0] and "child 222" not in replies[0]
    assert "child 222 problem" in replies[1] and "platform problem" not in replies[1]
    assert "/logs BOT_ID" in replies[2]


async def _reply_with_sender(tmp_path: Path, sender_error: BaseException | None) -> None:
    db = Database(tmp_path / "ziko.sqlite3")
    await db.initialize()
    await db.save_business_connection(CONN_ID, True, True, OWNER_ID)
    await begin_session(db, index=0)

    class Sender:
        async def send_message(self, **kwargs: Any) -> SimpleNamespace:
            if sender_error is not None:
                raise sender_error
            return SimpleNamespace(message_id=9)

    settings = SimpleNamespace(admin_ids=frozenset({ADMIN_ID}), business_owner_id=OWNER_ID)
    engine = BusinessMessageEngine(db=db, sender=Sender(), settings=settings)
    await engine.handle_business_message(make_message(message_id=1))
    await db.close()


def test_successful_reply_is_logged_as_sent(install: Any, tmp_path: Path) -> None:
    env = install(dedup_seconds=0)
    asyncio.run(_reply_with_sender(tmp_path, sender_error=None))
    master = _read(env.config.directory / "master.log")
    assert "Business auto-reply finished outcome=sent" in master
    assert " ERROR " not in master


def test_telegram_rejection_logs_type_without_message_text(install: Any, tmp_path: Path) -> None:
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import SendMessage

    env = install(dedup_seconds=0)
    rejected = TelegramBadRequest(
        method=SendMessage(chat_id=1, text="x"),
        message="Bad Request: can't parse SENTINEL-PRIVATE-BODY-7f3a9c",
    )
    asyncio.run(_reply_with_sender(tmp_path, sender_error=rejected))
    master = _read(env.config.directory / "master.log")
    assert "Auto-reply rejected by Telegram TelegramBadRequest retries=0" in master
    assert "SENTINEL-PRIVATE-BODY" not in master

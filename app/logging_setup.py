"""Structured, redacted, rotated logging for the master process and child bots.

Files (under ``log_directory``, default ``<database directory>/logs``):

* ``master.log``            platform events (master bot, Business engine, registry).
* ``tenants/bot-<id>.log``  events of one child bot. Its owner reads it with ``/logs``
                            inside the child bot; platform admins can read any of them.
* stderr                    everything, for the systemd journal.

Every record passes through one pipeline (``LogPipeline``) which, in order:

1. fills ``correlation_id``, ``bot_id`` and ``stage`` from the current context;
2. redacts secrets (bot tokens, ``password=``/``secret=``/``token=`` values, PEM and
   Fernet keys, bearer tokens) in the message, arguments, traceback and stack;
3. collapses repeats of the same warning or error inside a time window;
4. sends CRITICAL records, and records flagged ``alert=True``, to platform admins;
5. routes the record to ``master.log`` or to the file of its child bot.

Message bodies, contact identities and raw tokens are never logged on purpose.
Rotation is size based and bounded: each file keeps ``backup_count`` old copies.
"""
from __future__ import annotations

import asyncio
import contextvars
import logging
import os
import re
import secrets
import sys
import time
import traceback
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

MASTER_FILE = "master.log"
TENANT_DIR = "tenants"
ALERT_LOGGER = "ziko.alerts"
NO_VALUE = "-"
DEFAULT_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_BACKUP_COUNT = 5
DEFAULT_DEDUP_SECONDS = 60
DEFAULT_ALERT_INTERVAL = 300

CORRELATION_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "ziko_correlation_id", default=None
)
STAGE: contextvars.ContextVar[str | None] = contextvars.ContextVar("ziko_stage", default=None)
BOT_ID: contextvars.ContextVar[int | None] = contextvars.ContextVar("ziko_bot_id", default=None)

LOG_FORMAT = (
    "%(asctime)s %(levelname)-8s %(name)s cid=%(correlation_id)s bot=%(bot_id)s "
    "stage=%(stage)s %(message)s"
)

# --------------------------------------------------------------------------- #
# Redaction
# --------------------------------------------------------------------------- #
_REDACTION_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
                re.DOTALL), "[REDACTED_PRIVATE_KEY]"),
    # Telegram bot tokens, including the ones embedded in API URLs such as /bot<token>/.
    (re.compile(r"\d{5,}:[A-Za-z0-9_-]{20,}"), "[REDACTED_TOKEN]"),
    # Fernet keys (tenant-token.key) are 32 bytes in urlsafe base64: 43 characters and "=".
    (re.compile(r"[A-Za-z0-9_-]{43}="), "[REDACTED_KEY]"),
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/-]+=*"), r"\1 [REDACTED]"),
    # Named secrets keep their name and lose their value: bot_token=..., password: ...
    (re.compile(r"(?i)\b(\w*(?:password|passwd|secret|token|api_?key|private_?key)\w*)"
                r"(\s*[=:]\s*)[^\s,;&'\"]+"), r"\1\2[REDACTED]"),
)


def redact(text: str) -> str:
    """Return ``text`` with every recognised secret replaced by a placeholder."""
    for pattern, replacement in _REDACTION_RULES:
        text = pattern.sub(replacement, text)
    return text


# --------------------------------------------------------------------------- #
# Correlation context
# --------------------------------------------------------------------------- #
def new_correlation_id() -> str:
    return secrets.token_hex(6)


@contextmanager
def correlation_scope(*, bot_id: int | None = None, correlation_id: str | None = None) -> Iterator[str]:
    """Give the enclosed work its own correlation ID (and child bot scope, if any)."""
    cid = correlation_id or new_correlation_id()
    cid_token = CORRELATION_ID.set(cid)
    stage_token = STAGE.set(None)
    bot_token = BOT_ID.set(bot_id) if bot_id is not None else None
    try:
        yield cid
    finally:
        if bot_token is not None:
            BOT_ID.reset(bot_token)
        STAGE.reset(stage_token)
        CORRELATION_ID.reset(cid_token)


def set_stage(name: str) -> None:
    """Name the step that is running now, e.g. ``DATABASE_SAVE``."""
    STAGE.set(name)


@contextmanager
def operation(name: str, logger: logging.Logger) -> Iterator[None]:
    """Log a failure of ``name`` together with the stage that was running when it failed."""
    try:
        yield
    except Exception:
        logger.exception("operation=%s failed", name)
        raise


class CorrelationMiddleware(BaseMiddleware):
    """Gives each Telegram update its own correlation ID.

    ``tenant=True`` marks the child-bot dispatcher, whose updates are scoped to that
    bot's log file. The master dispatcher must not scope, or its updates would be
    written into whichever child bot happens to share the master's ID.
    """

    def __init__(self, *, tenant: bool) -> None:
        self._tenant = tenant

    async def __call__(
        self, handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject, data: dict[str, Any],
    ) -> Any:
        bot_id = getattr(data.get("bot"), "id", None) if self._tenant else None
        with correlation_scope(bot_id=bot_id):
            return await handler(event, data)


# --------------------------------------------------------------------------- #
# Duplicate suppression
# --------------------------------------------------------------------------- #
class Deduplicator:
    """Admits the first of identical warnings/errors per window and counts the rest."""

    MAX_KEYS = 2048

    def __init__(self, window_seconds: float, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._window = float(window_seconds)
        self._clock = clock
        # key -> [window start, repeats suppressed in this window]
        self._entries: OrderedDict[tuple[Any, ...], list[Any]] = OrderedDict()

    def admit(self, key: tuple[Any, ...]) -> int | None:
        """``None`` drops the record; otherwise the repeats suppressed before this one."""
        now = self._clock()
        entry = self._entries.get(key)
        if entry is not None and now - entry[0] < self._window:
            entry[1] += 1
            return None
        suppressed = entry[1] if entry is not None else 0
        self._entries[key] = [now, 0]
        self._entries.move_to_end(key)
        if len(self._entries) > self.MAX_KEYS:
            self._prune(now)
        return suppressed

    def _prune(self, now: float) -> None:
        for key in [k for k, e in self._entries.items() if now - e[0] >= self._window]:
            del self._entries[key]
        while len(self._entries) > self.MAX_KEYS:
            self._entries.popitem(last=False)

    def pending_summaries(self) -> list[tuple[str, int]]:
        """Repeat counts that were never shown, for a final summary at shutdown."""
        summaries = []
        for key, entry in self._entries.items():
            if entry[1]:
                summaries.append((str(key[2]), int(entry[1])))
                entry[1] = 0
        return summaries


def _dedup_key(record: logging.LogRecord) -> tuple[Any, ...]:
    bot = getattr(record, "bot_id", None) or BOT_ID.get()
    stage = getattr(record, "stage", None) or STAGE.get()
    # Correlation IDs are deliberately absent: each update has its own, and the
    # point is to group repeats of the same failure.
    return (record.levelno, record.name, str(record.msg), bot, stage, repr(record.args))


# --------------------------------------------------------------------------- #
# Formatting and routing
# --------------------------------------------------------------------------- #
class _Formatter(logging.Formatter):
    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        return datetime.fromtimestamp(record.created).astimezone().isoformat(timespec="milliseconds")


def _enrich(record: logging.LogRecord) -> None:
    if getattr(record, "correlation_id", None) is None:
        record.correlation_id = CORRELATION_ID.get() or NO_VALUE
    bot = getattr(record, "bot_id", None)
    if bot is None:
        bot = BOT_ID.get()
    record.bot_id = NO_VALUE if bot is None else str(bot)
    record.stage = getattr(record, "stage", None) or STAGE.get() or NO_VALUE


def _scrub(record: logging.LogRecord) -> None:
    record.msg = redact(record.getMessage())
    record.args = ()
    if record.exc_info and not record.exc_text:
        record.exc_text = redact("".join(traceback.format_exception(*record.exc_info)))
    elif record.exc_text:
        record.exc_text = redact(record.exc_text)
    if record.stack_info:
        record.stack_info = redact(record.stack_info)


class TenantLogRouter(logging.Handler):
    """Writes each child bot's records to its own rotating file.

    Only a bounded number of files stay open; the least recently used one is closed.
    """

    def __init__(
        self, directory: Path, *, max_bytes: int, backup_count: int,
        formatter: logging.Formatter, max_open: int = 64,
    ) -> None:
        super().__init__(level=logging.NOTSET)
        self.setFormatter(formatter)
        self._directory = directory / TENANT_DIR
        self._max_bytes = max_bytes
        self._backup_count = backup_count
        self._max_open = max_open
        self._handlers: OrderedDict[str, RotatingFileHandler] = OrderedDict()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._handler_for(str(record.bot_id)).handle(record)
        except Exception:
            self.handleError(record)

    def _handler_for(self, bot_key: str) -> RotatingFileHandler:
        if not bot_key.isdigit():
            bot_key = "unknown"  # only numeric Telegram IDs may name a file
        handler = self._handlers.get(bot_key)
        if handler is not None:
            self._handlers.move_to_end(bot_key)
            return handler
        self._directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        handler = RotatingFileHandler(
            self._directory / f"bot-{bot_key}.log",
            maxBytes=self._max_bytes, backupCount=self._backup_count,
            encoding="utf-8", delay=True,
        )
        handler.setFormatter(self.formatter)
        self._handlers[bot_key] = handler
        while len(self._handlers) > self._max_open:
            _, stale = self._handlers.popitem(last=False)
            stale.close()
        return handler

    def close(self) -> None:
        for handler in self._handlers.values():
            handler.close()
        self._handlers.clear()
        super().close()


class LogPipeline(logging.Handler):
    """The single root handler: enrich, redact, dedupe, alert, then route."""

    def __init__(
        self, *, master: logging.Handler, tenants: TenantLogRouter,
        console: logging.Handler, dedup: Deduplicator | None,
    ) -> None:
        super().__init__(level=logging.NOTSET)
        self._master = master
        self._tenants = tenants
        self._console = console
        self._dedup = dedup
        self._alert: Callable[[logging.LogRecord], None] | None = None

    def set_alert_sink(self, sink: Callable[[logging.LogRecord], None] | None) -> None:
        self._alert = sink

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._dispatch(record)
        except Exception:
            self.handleError(record)

    def _dispatch(self, record: logging.LogRecord) -> None:
        suppressed: int | None = 0
        if self._dedup is not None and record.levelno >= logging.WARNING:
            suppressed = self._dedup.admit(_dedup_key(record))
            if suppressed is None:
                return
        _enrich(record)
        _scrub(record)
        if suppressed:
            record.msg = f"{record.msg} [{suppressed} similar repeats suppressed]"
        if self._alert is not None and (
            record.levelno >= logging.CRITICAL or getattr(record, "alert", False)
        ):
            self._alert(record)
        if record.bot_id == NO_VALUE:
            self._master.handle(record)
        else:
            self._tenants.handle(record)
        self._console.handle(record)

    def flush_summaries(self) -> None:
        """Write one line per suppressed repeat group that was never shown."""
        if self._dedup is None:
            return
        logger = logging.getLogger("ziko.logging")
        for template, count in self._dedup.pending_summaries():
            logger.warning("Suppressed %d repeats of: %s", count, template)

    def close(self) -> None:
        self.flush_summaries()
        for handler in (self._master, self._tenants, self._console):
            handler.close()
        super().close()


# --------------------------------------------------------------------------- #
# Critical alerts to platform admins
# --------------------------------------------------------------------------- #
ALERT_MAX_PER_HOUR = 20
ALERT_MAX_CHARS = 600


class AlertSink:
    """Sends CRITICAL records to platform admins, rate limited per message and per hour."""

    def __init__(
        self, *, send: Callable[[int, str], Awaitable[object]], admin_ids: Iterable[int],
        min_interval_seconds: float, clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._send = send
        self._admins = sorted(int(admin) for admin in admin_ids)
        self._min_interval = float(min_interval_seconds)
        self._clock = clock
        self._last_sent: dict[tuple[str, str, str], float] = {}
        self._hour: deque[float] = deque()
        self._tasks: set[asyncio.Task[None]] = set()

    def __call__(self, record: logging.LogRecord) -> None:
        if record.name.startswith(ALERT_LOGGER) or not self._admins:
            return  # never alert about alert delivery itself
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no running loop: the record still reaches stderr
        now = self._clock()
        key = (record.name, str(getattr(record, "stage", NO_VALUE)), str(record.msg)[:120])
        last = self._last_sent.get(key)
        if last is not None and now - last < self._min_interval:
            return
        while self._hour and now - self._hour[0] >= 3600:
            self._hour.popleft()
        if len(self._hour) >= ALERT_MAX_PER_HOUR:
            return
        self._last_sent[key] = now
        self._hour.append(now)
        task = loop.create_task(self._deliver(_alert_text(record)))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _deliver(self, text: str) -> None:
        for admin_id in self._admins:
            try:
                await self._send(admin_id, text)
            except Exception as exc:
                logging.getLogger(ALERT_LOGGER).warning(
                    "Could not deliver critical alert admin_id=%s error=%s",
                    admin_id, type(exc).__name__,
                )

    async def drain(self, timeout: float = 10.0) -> None:
        """Wait briefly for alerts still in flight, e.g. during shutdown."""
        if self._tasks:
            await asyncio.wait(set(self._tasks), timeout=timeout)


def _alert_text(record: logging.LogRecord) -> str:
    message = record.getMessage()
    if len(message) > ALERT_MAX_CHARS:
        message = message[:ALERT_MAX_CHARS].rstrip() + "…"
    when = datetime.fromtimestamp(record.created).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    return (
        "🚨 هشدار بحرانی ZIKO\n"
        f"زمان: {when}\n"
        f"مرحله: {getattr(record, 'stage', NO_VALUE)}\n"
        f"ربات: {getattr(record, 'bot_id', NO_VALUE)}\n"
        f"شناسه پیگیری: {getattr(record, 'correlation_id', NO_VALUE)}\n\n"
        f"{message}\n\n"
        "جزئیات کامل در گزارش لاگ سرور ثبت شده است."
    )


# --------------------------------------------------------------------------- #
# Configuration and setup
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class LogConfig:
    directory: Path
    max_bytes: int = DEFAULT_MAX_BYTES
    backup_count: int = DEFAULT_BACKUP_COUNT
    dedup_seconds: int = DEFAULT_DEDUP_SECONDS
    alerts_enabled: bool = True
    alert_interval_seconds: int = DEFAULT_ALERT_INTERVAL


def _int_setting(environ: Mapping[str, str], name: str, default: int, low: int, high: int) -> int:
    raw = environ.get(name, "").strip()
    if not raw:
        return default
    if not (raw.isascii() and raw.isdigit()) or not low <= int(raw) <= high:
        raise ValueError(f"{name} must be a whole number between {low} and {high}")
    return int(raw)


def load_log_config(database_path: Path, environ: Mapping[str, str]) -> LogConfig:
    """Read the ``ZIKO_LOG_*`` settings. Error messages never include values."""
    custom_dir = environ.get("ZIKO_LOG_DIR", "").strip()
    directory = Path(custom_dir) if custom_dir else database_path.parent / "logs"
    alerts_raw = environ.get("ZIKO_LOG_ALERTS", "1").strip().lower()
    if alerts_raw not in {"0", "1", "true", "false", "yes", "no", "on", "off"}:
        raise ValueError("ZIKO_LOG_ALERTS must be 0 or 1")
    return LogConfig(
        directory=directory,
        max_bytes=_int_setting(environ, "ZIKO_LOG_MAX_BYTES", DEFAULT_MAX_BYTES, 64 * 1024, 100 * 1024 * 1024),
        backup_count=_int_setting(environ, "ZIKO_LOG_BACKUP_COUNT", DEFAULT_BACKUP_COUNT, 1, 20),
        dedup_seconds=_int_setting(environ, "ZIKO_LOG_DEDUP_SECONDS", DEFAULT_DEDUP_SECONDS, 0, 3600),
        alerts_enabled=alerts_raw in {"1", "true", "yes", "on"},
        alert_interval_seconds=_int_setting(
            environ, "ZIKO_LOG_ALERT_INTERVAL_SECONDS", DEFAULT_ALERT_INTERVAL, 0, 86400
        ),
    )


def log_file_for_bot(directory: Path, bot_id: int) -> Path:
    return directory / TENANT_DIR / f"bot-{int(bot_id)}.log"


def master_log_file(directory: Path) -> Path:
    return directory / MASTER_FILE


def setup_logging(config: LogConfig, *, level: str, stream: Any = None) -> LogPipeline:
    """Replace the root logging configuration with the ZIKO pipeline."""
    config.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(config.directory, 0o700)
    formatter = _Formatter(LOG_FORMAT)
    master = RotatingFileHandler(
        master_log_file(config.directory), maxBytes=config.max_bytes,
        backupCount=config.backup_count, encoding="utf-8", delay=True,
    )
    master.setFormatter(formatter)
    tenants = TenantLogRouter(
        config.directory, max_bytes=config.max_bytes,
        backup_count=config.backup_count, formatter=formatter,
    )
    console = logging.StreamHandler(stream if stream is not None else sys.stderr)
    console.setFormatter(formatter)
    dedup = Deduplicator(config.dedup_seconds) if config.dedup_seconds > 0 else None
    pipeline = LogPipeline(master=master, tenants=tenants, console=console, dedup=dedup)
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.addHandler(pipeline)
    root.setLevel(level)
    return pipeline


# --------------------------------------------------------------------------- #
# Reading logs for administrators
# --------------------------------------------------------------------------- #
_PROBLEM_LINE = re.compile(r" (WARNING|ERROR|CRITICAL) ")


def recent_problems(path: Path, *, limit: int = 15, max_bytes: int = 256 * 1024) -> list[str]:
    """Return the last ``limit`` WARNING/ERROR/CRITICAL header lines, redacted again."""
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            handle.seek(max(0, size - max_bytes))
            data = handle.read()
    except FileNotFoundError:
        return []
    lines = data.decode("utf-8", errors="replace").splitlines()
    if size > max_bytes and lines:
        lines = lines[1:]  # the first line may start mid-record
    problems = [redact(line) for line in lines if _PROBLEM_LINE.search(line)]
    return problems[-max(1, limit):]

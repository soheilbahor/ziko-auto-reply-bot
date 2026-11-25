"""SQLite persistence layer (aiosqlite) for ZIKO Personal Assistant.

Concurrency model
-----------------
* One aiosqlite connection per ``Database`` instance, used in autocommit mode
  (``isolation_level=None``) with explicit ``BEGIN IMMEDIATE`` transactions.
* An ``asyncio.Lock`` serialises all access to that connection, so coroutines
  can never interleave statements inside one another's transaction.
* The critical reservations (``claim_inbound_event``, ``claim_reply``) are each
  a single atomic SQL statement, so they are also safe across several
  ``Database`` instances or processes sharing the same file.

Ownership: every successful claim_reply returns a unique claim_token; complete_reply and
release_reply only act for the current token holder (see MODULE1_CONTRACT.md).

Privacy: only identifiers and delivery metadata are stored, never message text.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import string
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import Any, Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiosqlite

from app.models import (
    DEFAULT_STATUSES,
    AppState,
    BusinessConnectionState,
    DeliveryOutcome,
    OwnerSettings,
    ReplyStep,
    Status,
)

__all__ = [
    "SCHEMA_VERSION",
    "Database",
    "DeliveryOutcome",
    "InvalidReplyTextError",
    "SchemaVersionError",
    "StorageError",
    "UnknownConnectionError",
    "UnknownStatusError",
]

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 3
SCHEMA_PATH = Path(__file__).with_name("database_schema.sql")

MAX_REPLY_TEXT_LENGTH = 4096  # Telegram's sendMessage text limit
MAX_ERROR_LENGTH = 500
DEFAULT_PENDING_LEASE_SECONDS = 300

_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"  # fixed width, so strings sort chronologically
_STATUS_COLUMNS = (
    "key, title, emoji, reply_text, is_active, reply_mode, min_interval_seconds, "
    "active_from_hour, active_to_hour"
)
_BOT_TOKEN_RE = re.compile(r"\d{6,}:[A-Za-z0-9_-]{20,}")

# Version 1 -> 2: add reply_deliveries.claim_token and the 'uncertain' state. SQLite cannot
# alter a CHECK constraint, so the table is rebuilt. Rows left over from version 1 receive a
# random token; a leftover 'pending' row follows expired_lease_policy once its lease expires.
_MIGRATION_2 = """
CREATE TABLE reply_deliveries_v2 (
    connection_id       TEXT    NOT NULL
                        REFERENCES business_connections (connection_id)
                        ON UPDATE CASCADE ON DELETE CASCADE,
    chat_id             INTEGER NOT NULL,
    session_id          TEXT    NOT NULL CHECK (length(session_id) > 0),
    state               TEXT    NOT NULL
                        CHECK (state IN ('pending', 'sent', 'failed', 'uncertain')),
    attempts            INTEGER NOT NULL DEFAULT 1 CHECK (attempts >= 1),
    telegram_message_id INTEGER,
    last_error          TEXT,
    claim_token         TEXT    NOT NULL CHECK (length(claim_token) > 0),
    claimed_at          TEXT    NOT NULL,
    completed_at        TEXT,
    updated_at          TEXT    NOT NULL,
    PRIMARY KEY (connection_id, chat_id, session_id),
    CHECK (state <> 'sent' OR completed_at IS NOT NULL)
) WITHOUT ROWID;
INSERT INTO reply_deliveries_v2
    (connection_id, chat_id, session_id, state, attempts, telegram_message_id, last_error,
     claim_token, claimed_at, completed_at, updated_at)
SELECT connection_id, chat_id, session_id, state, attempts, telegram_message_id, last_error,
       lower(hex(randomblob(16))), claimed_at, completed_at, updated_at
FROM reply_deliveries;
DROP TABLE reply_deliveries;
ALTER TABLE reply_deliveries_v2 RENAME TO reply_deliveries;
CREATE INDEX IF NOT EXISTS idx_reply_deliveries_state_claimed
    ON reply_deliveries (state, claimed_at);
CREATE INDEX IF NOT EXISTS idx_reply_deliveries_session
    ON reply_deliveries (session_id);
"""

# Version 2 -> 3: ordered reply steps, owner settings, reply exceptions, status policy
# (soft delete, reply mode, interval, hour window) and the 'partial' delivery state.
# The first text of every existing status becomes its single step, so behaviour is unchanged
# until an owner edits it. reply_deliveries is rebuilt for the new state and parts_sent column.
_MIGRATION_3 = """
ALTER TABLE statuses ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1));
ALTER TABLE statuses ADD COLUMN deleted_at TEXT;
ALTER TABLE statuses ADD COLUMN reply_mode TEXT NOT NULL DEFAULT 'reply'
    CHECK (reply_mode IN ('plain', 'reply'));
ALTER TABLE statuses ADD COLUMN min_interval_seconds INTEGER NOT NULL DEFAULT 0
    CHECK (min_interval_seconds BETWEEN 0 AND 86400);
ALTER TABLE statuses ADD COLUMN active_from_hour INTEGER CHECK (active_from_hour BETWEEN 0 AND 23);
ALTER TABLE statuses ADD COLUMN active_to_hour INTEGER CHECK (active_to_hour BETWEEN 0 AND 23);
CREATE TABLE IF NOT EXISTS reply_steps (
    status_key TEXT    NOT NULL REFERENCES statuses (key) ON UPDATE CASCADE ON DELETE CASCADE,
    position   INTEGER NOT NULL CHECK (position BETWEEN 1 AND 10),
    kind       TEXT    NOT NULL CHECK (kind IN ('text', 'emoji', 'sticker')),
    content    TEXT    NOT NULL CHECK (length(content) BETWEEN 1 AND 4096),
    created_at TEXT    NOT NULL,
    PRIMARY KEY (status_key, position)
) WITHOUT ROWID;
INSERT INTO reply_steps (status_key, position, kind, content, created_at)
SELECT key, 1, 'text', reply_text, updated_at FROM statuses;
CREATE TABLE IF NOT EXISTS owner_settings (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    timezone   TEXT NOT NULL DEFAULT 'Asia/Tehran' CHECK (length(timezone) BETWEEN 1 AND 64),
    language   TEXT NOT NULL DEFAULT 'fa' CHECK (language IN ('fa', 'en')),
    owner_name TEXT NOT NULL DEFAULT '' CHECK (length(owner_name) <= 64),
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reply_exceptions (
    chat_id    INTEGER PRIMARY KEY,
    created_at TEXT    NOT NULL
) WITHOUT ROWID;
CREATE TABLE reply_deliveries_v3 (
    connection_id       TEXT    NOT NULL
                        REFERENCES business_connections (connection_id)
                        ON UPDATE CASCADE ON DELETE CASCADE,
    chat_id             INTEGER NOT NULL,
    session_id          TEXT    NOT NULL CHECK (length(session_id) > 0),
    state               TEXT    NOT NULL
                        CHECK (state IN ('pending', 'sent', 'failed', 'uncertain', 'partial')),
    attempts            INTEGER NOT NULL DEFAULT 1 CHECK (attempts >= 1),
    parts_sent          INTEGER NOT NULL DEFAULT 0 CHECK (parts_sent >= 0),
    telegram_message_id INTEGER,
    last_error          TEXT,
    claim_token         TEXT    NOT NULL CHECK (length(claim_token) > 0),
    claimed_at          TEXT    NOT NULL,
    completed_at        TEXT,
    updated_at          TEXT    NOT NULL,
    PRIMARY KEY (connection_id, chat_id, session_id),
    CHECK (state NOT IN ('sent', 'partial') OR completed_at IS NOT NULL)
) WITHOUT ROWID;
INSERT INTO reply_deliveries_v3
    (connection_id, chat_id, session_id, state, attempts, parts_sent, telegram_message_id,
     last_error, claim_token, claimed_at, completed_at, updated_at)
SELECT connection_id, chat_id, session_id, state, attempts,
       CASE WHEN state = 'sent' THEN 1 ELSE 0 END,
       telegram_message_id, last_error, claim_token, claimed_at, completed_at, updated_at
FROM reply_deliveries;
DROP TABLE reply_deliveries;
ALTER TABLE reply_deliveries_v3 RENAME TO reply_deliveries;
CREATE INDEX IF NOT EXISTS idx_reply_deliveries_state_claimed
    ON reply_deliveries (state, claimed_at);
CREATE INDEX IF NOT EXISTS idx_reply_deliveries_session
    ON reply_deliveries (session_id);
CREATE INDEX IF NOT EXISTS idx_reply_deliveries_contact
    ON reply_deliveries (connection_id, chat_id, state, completed_at);
"""

# {target_version: SQL script migrating from target_version - 1}.
_MIGRATIONS: dict[int, str] = {2: _MIGRATION_2, 3: _MIGRATION_3}


class StorageError(Exception):
    """Base class for storage-layer errors."""


class SchemaVersionError(StorageError):
    """The database schema is newer than, or cannot be upgraded by, this code."""


class UnknownStatusError(StorageError):
    """The requested status key does not exist."""


class UnknownConnectionError(StorageError):
    """The business connection has not been saved yet (call save_business_connection first)."""


class InvalidReplyTextError(StorageError, ValueError):
    """A status reply text is empty or too long."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _new_uuid() -> str:
    return str(uuid.uuid4())


def _format_timestamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime(_TIMESTAMP_FORMAT)


def _sanitize_error(error: str | None) -> str | None:
    """Truncate an error description and strip anything that looks like a bot token."""
    if error is None:
        return None
    cleaned = _BOT_TOKEN_RE.sub("<redacted>", error).strip()
    return cleaned[:MAX_ERROR_LENGTH] or None


def _validate_reply_text(reply_text: str) -> str:
    if not isinstance(reply_text, str):
        raise InvalidReplyTextError("reply_text must be a string")
    cleaned = reply_text.strip()
    if not cleaned:
        raise InvalidReplyTextError("reply_text must not be empty")
    if len(cleaned) > MAX_REPLY_TEXT_LENGTH:
        raise InvalidReplyTextError(f"reply_text must be at most {MAX_REPLY_TEXT_LENGTH} characters")
    return cleaned


# --- Low-level helpers -----------------------------------------------------


async def _execute(conn: aiosqlite.Connection, sql: str, params: Sequence[Any] = ()) -> int:
    """Run one statement and return its rowcount."""
    async with conn.execute(sql, params) as cursor:
        return cursor.rowcount


async def _fetch_one(
    conn: aiosqlite.Connection, sql: str, params: Sequence[Any] = ()
) -> aiosqlite.Row | None:
    async with conn.execute(sql, params) as cursor:
        return await cursor.fetchone()


async def _fetch_all(
    conn: aiosqlite.Connection, sql: str, params: Sequence[Any] = ()
) -> list[aiosqlite.Row]:
    async with conn.execute(sql, params) as cursor:
        return list(await cursor.fetchall())


@asynccontextmanager
async def _begin(conn: aiosqlite.Connection) -> AsyncIterator[None]:
    """Explicit write transaction: BEGIN IMMEDIATE, COMMIT on success, ROLLBACK otherwise.

    BEGIN is inside the try block on purpose: if the task is cancelled while BEGIN is
    still queued on the connection's worker thread, the ROLLBACK queued here runs right
    after it, so a cancelled caller can never leave a transaction open on the connection.
    """
    try:
        await _execute(conn, "BEGIN IMMEDIATE")
        yield
        await _execute(conn, "COMMIT")
    except BaseException:
        with suppress(Exception):
            await _execute(conn, "ROLLBACK")
        raise


def _app_state_from_row(row: aiosqlite.Row) -> AppState:
    return AppState(
        enabled=bool(row["enabled"]),
        active_status_key=row["active_status_key"],
        session_id=row["session_id"],
        updated_at=row["updated_at"],
    )


def _status_from_row(row: aiosqlite.Row) -> Status:
    return Status(
        key=row["key"],
        title=row["title"],
        emoji=row["emoji"],
        reply_text=row["reply_text"],
        is_active=bool(row["is_active"]),
        reply_mode=row["reply_mode"],
        min_interval_seconds=int(row["min_interval_seconds"]),
        active_from_hour=row["active_from_hour"],
        active_to_hour=row["active_to_hour"],
    )


_MAX_STEPS = 10
_STEP_KINDS = frozenset({"text", "emoji", "sticker"})


def _validate_label(title: str, emoji: str) -> tuple[str, str]:
    clean_title = (title or "").strip()
    clean_emoji = (emoji or "").strip()
    if not 1 <= len(clean_title) <= 40:
        raise InvalidReplyTextError("status title must be 1-40 characters")
    if not 1 <= len(clean_emoji) <= 16:
        raise InvalidReplyTextError("status emoji must be 1-16 characters")
    return clean_title, clean_emoji


def _validate_steps(steps: Sequence[tuple[str, str]]) -> list[tuple[str, str]]:
    if not 1 <= len(steps) <= _MAX_STEPS:
        raise InvalidReplyTextError(f"a status needs 1-{_MAX_STEPS} reply steps")
    cleaned: list[tuple[str, str]] = []
    for kind, content in steps:
        if kind not in _STEP_KINDS:
            raise InvalidReplyTextError(f"unknown reply step kind: {kind!r}")
        value = (content or "").strip()
        if kind == "sticker":
            if not 1 <= len(value) <= 200 or any(ch.isspace() for ch in value):
                raise InvalidReplyTextError("sticker file id is invalid")
        elif kind == "emoji":
            if not 1 <= len(value) <= 16:
                raise InvalidReplyTextError("emoji step must be 1-16 characters")
        else:
            value = _validate_reply_text(value)
        cleaned.append((kind, value))
    return cleaned


def _validate_policy(
    reply_mode: str, min_interval_seconds: int,
    active_from_hour: int | None, active_to_hour: int | None,
) -> None:
    if reply_mode not in ("plain", "reply"):
        raise InvalidReplyTextError("reply_mode must be 'plain' or 'reply'")
    if not 0 <= int(min_interval_seconds) <= 86400:
        raise InvalidReplyTextError("min_interval_seconds must be between 0 and 86400")
    if (active_from_hour is None) != (active_to_hour is None):
        raise InvalidReplyTextError("set both hour bounds or neither")
    if active_from_hour is not None:
        if not (0 <= active_from_hour <= 23 and 0 <= active_to_hour <= 23):
            raise InvalidReplyTextError("hour bounds must be between 0 and 23")
        if active_from_hour == active_to_hour:
            raise InvalidReplyTextError("the start and end hour must differ")


async def _write_steps(conn: aiosqlite.Connection, status_key: str, steps: list[tuple[str, str]], now: str) -> None:
    await _execute(conn, "DELETE FROM reply_steps WHERE status_key = ?", (status_key,))
    for position, (kind, content) in enumerate(steps, start=1):
        await _execute(
            conn,
            "INSERT INTO reply_steps (status_key, position, kind, content, created_at) VALUES (?, ?, ?, ?, ?)",
            (status_key, position, kind, content, now),
        )
    first_text = next((content for kind, content in steps if kind == "text"), None)
    if first_text is not None:
        await _execute(
            conn, "UPDATE statuses SET reply_text = ? WHERE key = ?", (first_text, status_key),
        )


async def _fetch_app_state(conn: aiosqlite.Connection) -> AppState:
    row = await _fetch_one(
        conn,
        "SELECT enabled, active_status_key, session_id, updated_at FROM app_state WHERE id = 1",
    )
    if row is None:
        raise StorageError("app_state row is missing; database was not initialised correctly")
    return _app_state_from_row(row)


async def _require_status(conn: aiosqlite.Connection, status_key: str) -> None:
    if await _fetch_one(conn, "SELECT 1 FROM statuses WHERE key = ? AND deleted_at IS NULL", (status_key,)) is None:
        raise UnknownStatusError(f"Unknown status key: {status_key!r}")


async def _require_connection(conn: aiosqlite.Connection, connection_id: str) -> None:
    row = await _fetch_one(
        conn, "SELECT 1 FROM business_connections WHERE connection_id = ?", (connection_id,)
    )
    if row is None:
        raise UnknownConnectionError("Business connection is not known; save it before claiming events")


# --- Database --------------------------------------------------------------


class Database:
    """Asynchronous SQLite storage. Call ``initialize()`` before use and ``close()`` after."""

    def __init__(
        self,
        path: Path,
        *,
        clock: Callable[[], datetime] | None = None,
        session_id_factory: Callable[[], str] | None = None,
        pending_lease_seconds: int = DEFAULT_PENDING_LEASE_SECONDS,
        expired_lease_policy: Literal["reclaim", "uncertain"] = "uncertain",
    ) -> None:
        """
        Args:
            path: SQLite file path (``":memory:"`` is accepted for throwaway use).
            clock: Injectable UTC clock (tests).
            session_id_factory: Injectable session id generator (defaults to uuid4).
            pending_lease_seconds: How long a ``pending`` reservation blocks other
                workers before its lease is considered expired. Must be longer than the
                worst-case duration of one send attempt (HTTP timeout included).
            expired_lease_policy: What ``claim_reply`` does with a ``pending`` reservation whose
                lease expired (its worker may have crashed mid-send, or may be hung and
                still about to send).
                ``"uncertain"`` (default, production) marks it ``uncertain`` and never
                retries it: strict at-most-once, so one contact may stay unanswered for
                the rest of that status session, but a personal message is never
                duplicated. ``"reclaim"`` (explicit opt-in) hands the reservation to the
                new caller under a fresh claim token and invalidates the old token; this
                favours availability but can duplicate a message the old worker did send.
                Confirmed Telegram rejections (``REJECTED``) are retryable under both.
        """
        if pending_lease_seconds <= 0:
            raise ValueError("pending_lease_seconds must be positive")
        if expired_lease_policy not in ("reclaim", "uncertain"):
            raise ValueError("expired_lease_policy must be 'reclaim' or 'uncertain'")
        self._expired_lease_policy = expired_lease_policy
        self._path = Path(path)
        self._is_memory = str(path) == ":memory:"
        self._clock = clock or _utc_now
        self._new_session_id = session_id_factory or _new_uuid
        self._pending_lease = timedelta(seconds=pending_lease_seconds)
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> Self:
        await self.initialize()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.close()

    # -- lifecycle ----------------------------------------------------------

    async def initialize(self) -> None:
        """Open the database, apply the schema/migrations and seed default rows. Idempotent."""
        if self._conn is not None:
            return
        if not self._is_memory:
            await asyncio.to_thread(self._prepare_file)
        conn = await aiosqlite.connect(str(self._path), isolation_level=None)
        try:
            conn.row_factory = aiosqlite.Row
            await self._configure(conn)
            await self._migrate(conn)
            await self._seed(conn)
        except BaseException:
            with suppress(Exception):
                await conn.close()
            raise
        self._conn = conn

    async def close(self) -> None:
        """Close the connection. Safe to call more than once."""
        async with self._lock:
            conn, self._conn = self._conn, None
            if conn is not None:
                await conn.close()

    def _prepare_file(self) -> None:
        """Create the parent directory and the file with owner-only permissions (0600)."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Creating the file ourselves means SQLite's -wal/-shm files inherit 0600 too.
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        os.close(fd)

    @staticmethod
    async def _configure(conn: aiosqlite.Connection) -> None:
        await _execute(conn, "PRAGMA foreign_keys = ON")
        await _execute(conn, "PRAGMA busy_timeout = 5000")
        await _execute(conn, "PRAGMA synchronous = NORMAL")
        row = await _fetch_one(conn, "PRAGMA foreign_keys")
        if row is None or row[0] != 1:
            raise StorageError("SQLite foreign key enforcement could not be enabled")
        mode_row = await _fetch_one(conn, "PRAGMA journal_mode = WAL")
        logger.debug("sqlite journal_mode=%s", mode_row[0] if mode_row else "unknown")

    @staticmethod
    async def _migrate(conn: aiosqlite.Connection) -> None:
        row = await _fetch_one(conn, "PRAGMA user_version")
        version = int(row[0]) if row else 0
        if version > SCHEMA_VERSION:
            raise SchemaVersionError(
                f"Database schema version {version} is newer than supported version {SCHEMA_VERSION}"
            )
        if version == SCHEMA_VERSION:
            return

        if version == 0:
            schema_sql = await asyncio.to_thread(SCHEMA_PATH.read_text, "utf-8")
            scripts = [schema_sql]
        else:
            missing = [v for v in range(version + 1, SCHEMA_VERSION + 1) if v not in _MIGRATIONS]
            if missing:
                raise SchemaVersionError(f"No migration available for schema version(s) {missing}")
            scripts = [_MIGRATIONS[v] for v in range(version + 1, SCHEMA_VERSION + 1)]

        # One transaction for schema + version: a crash cannot leave a half-built schema.
        body = "\n".join(scripts)
        await conn.executescript(
            f"BEGIN IMMEDIATE;\n{body}\nPRAGMA user_version = {SCHEMA_VERSION};\nCOMMIT;"
        )
        logger.info("database schema ready (version %d)", SCHEMA_VERSION)

    async def _seed(self, conn: aiosqlite.Connection) -> None:
        now = self._now()
        async with _begin(conn):
            await _execute(
                conn,
                "INSERT OR IGNORE INTO app_state (id, enabled, active_status_key, session_id, updated_at) "
                "VALUES (1, 0, NULL, NULL, ?)",
                (now,),
            )
            await _execute(
                conn,
                "INSERT OR IGNORE INTO owner_settings (id, timezone, language, owner_name, updated_at) "
                "VALUES (1, 'Asia/Tehran', 'fa', '', ?)",
                (now,),
            )
            for order, status in enumerate(DEFAULT_STATUSES):
                await _execute(
                    conn,
                    "INSERT OR IGNORE INTO statuses (key, title, emoji, reply_text, sort_order, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (status.key, status.title, status.emoji, status.reply_text, order, now),
                )

    # -- transaction helpers --------------------------------------------------

    def _require_conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise StorageError("Database is not initialised; call initialize() first")
        return self._conn

    def _now(self) -> str:
        return _format_timestamp(self._clock())

    @asynccontextmanager
    async def _read(self) -> AsyncIterator[aiosqlite.Connection]:
        conn = self._require_conn()
        async with self._lock:
            yield conn

    @asynccontextmanager
    async def _write(self) -> AsyncIterator[aiosqlite.Connection]:
        conn = self._require_conn()
        async with self._lock, _begin(conn):
            yield conn

    # -- app state ------------------------------------------------------------

    async def get_app_state(self) -> AppState:
        async with self._read() as conn:
            return await _fetch_app_state(conn)

    async def activate_status(self, status_key: str) -> AppState:
        """Turn auto-reply on with ``status_key``.

        Selecting the status that is already active keeps the existing session.
        Any other transition (different status, or disabled -> enabled) starts a new one.
        """
        async with self._write() as conn:
            await _require_status(conn, status_key)
            state = await _fetch_app_state(conn)
            if state.enabled and state.active_status_key == status_key:
                return state
            session_id = self._new_session_id()
            now = self._now()
            await _execute(
                conn,
                "UPDATE app_state SET enabled = 1, active_status_key = ?, session_id = ?, updated_at = ? "
                "WHERE id = 1",
                (status_key, session_id, now),
            )
        logger.info("status activated: %s", status_key)
        return AppState(True, status_key, session_id, now)

    async def enable_auto_reply(self) -> AppState:
        """Re-enable auto-reply with the last selected status, starting a new session.

        No-op if already enabled. If no status was ever selected the state stays
        disabled; callers should check ``state.enabled`` on the returned value.
        """
        async with self._write() as conn:
            state = await _fetch_app_state(conn)
            if state.enabled or state.active_status_key is None:
                return state
            session_id = self._new_session_id()
            now = self._now()
            await _execute(
                conn,
                "UPDATE app_state SET enabled = 1, session_id = ?, updated_at = ? WHERE id = 1",
                (session_id, now),
            )
        logger.info("auto-reply enabled: %s", state.active_status_key)
        return AppState(True, state.active_status_key, session_id, now)

    async def disable_auto_reply(self) -> AppState:
        """Turn auto-reply off. The selected status is remembered; the session is discarded."""
        async with self._write() as conn:
            state = await _fetch_app_state(conn)
            if not state.enabled:
                return state
            now = self._now()
            await _execute(
                conn,
                "UPDATE app_state SET enabled = 0, session_id = NULL, updated_at = ? WHERE id = 1",
                (now,),
            )
        logger.info("auto-reply disabled")
        return AppState(False, state.active_status_key, None, now)

    # -- statuses -------------------------------------------------------------

    async def list_statuses(self) -> list[Status]:
        async with self._read() as conn:
            rows = await _fetch_all(
                conn,
                "SELECT " + _STATUS_COLUMNS + " FROM statuses WHERE deleted_at IS NULL "
                "ORDER BY sort_order, key",
            )
        return [_status_from_row(row) for row in rows]

    async def get_status(self, status_key: str) -> Status | None:
        async with self._read() as conn:
            row = await _fetch_one(
                conn,
                "SELECT " + _STATUS_COLUMNS + " FROM statuses WHERE key = ?",
                (status_key,),
            )
        return _status_from_row(row) if row is not None else None

    async def save_status_text(self, status_key: str, reply_text: str) -> Status:
        """Replace a status's reply text. Does not affect the current session."""
        cleaned = _validate_reply_text(reply_text)
        async with self._write() as conn:
            changed = await _execute(
                conn,
                "UPDATE statuses SET reply_text = ?, updated_at = ? WHERE key = ? AND deleted_at IS NULL",
                (cleaned, self._now(), status_key),
            )
            if changed != 1:
                raise UnknownStatusError(f"Unknown status key: {status_key!r}")
            row = await _fetch_one(
                conn,
                "SELECT " + _STATUS_COLUMNS + " FROM statuses WHERE key = ?",
                (status_key,),
            )
        if row is None:  # pragma: no cover - the UPDATE above just matched this row
            raise UnknownStatusError(f"Unknown status key: {status_key!r}")
        return _status_from_row(row)

    # -- statuses, reply steps, owner settings and exceptions (phase 4) --------

    async def create_status(
        self, *, title: str, emoji: str, steps: Sequence[tuple[str, str]],
        reply_mode: str = "reply", min_interval_seconds: int = 0,
        active_from_hour: int | None = None, active_to_hour: int | None = None,
    ) -> Status:
        """Add a status with a generated key (``custom_`` + 8 letters). Never reuses a key."""
        clean_title, clean_emoji = _validate_label(title, emoji)
        clean_steps = _validate_steps(steps)
        _validate_policy(reply_mode, min_interval_seconds, active_from_hour, active_to_hour)
        now = self._now()
        async with self._write() as conn:
            key = ""
            for _ in range(10):
                candidate = "custom_" + "".join(secrets.choice(string.ascii_lowercase) for _ in range(8))
                if await _fetch_one(conn, "SELECT 1 FROM statuses WHERE key = ?", (candidate,)) is None:
                    key = candidate
                    break
            if not key:
                raise StorageError("could not allocate a unique status key")
            order_row = await _fetch_one(conn, "SELECT COALESCE(MAX(sort_order), 0) + 1 FROM statuses")
            order = int(order_row[0]) if order_row else 1
            await _execute(
                conn,
                "INSERT INTO statuses (key, title, emoji, reply_text, sort_order, updated_at, "
                "reply_mode, min_interval_seconds, active_from_hour, active_to_hour) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (key, clean_title, clean_emoji, clean_steps[0][1], order, now, reply_mode,
                 int(min_interval_seconds), active_from_hour, active_to_hour),
            )
            await _write_steps(conn, key, clean_steps, now)
            row = await _fetch_one(
                conn, "SELECT " + _STATUS_COLUMNS + " FROM statuses WHERE key = ?", (key,),
            )
        if row is None:  # pragma: no cover - inserted above
            raise UnknownStatusError(f"Unknown status key: {key!r}")
        return _status_from_row(row)

    async def update_status_settings(
        self, status_key: str, *, title: str, emoji: str, reply_mode: str,
        min_interval_seconds: int, active_from_hour: int | None, active_to_hour: int | None,
        is_active: bool,
    ) -> Status:
        """Replace a status's label and policy. Its reply steps are untouched."""
        clean_title, clean_emoji = _validate_label(title, emoji)
        _validate_policy(reply_mode, min_interval_seconds, active_from_hour, active_to_hour)
        async with self._write() as conn:
            await _require_status(conn, status_key)
            await _execute(
                conn,
                "UPDATE statuses SET title = ?, emoji = ?, reply_mode = ?, min_interval_seconds = ?, "
                "active_from_hour = ?, active_to_hour = ?, is_active = ?, updated_at = ? "
                "WHERE key = ? AND deleted_at IS NULL",
                (clean_title, clean_emoji, reply_mode, int(min_interval_seconds), active_from_hour,
                 active_to_hour, 1 if is_active else 0, self._now(), status_key),
            )
            row = await _fetch_one(
                conn, "SELECT " + _STATUS_COLUMNS + " FROM statuses WHERE key = ?", (status_key,),
            )
        if row is None:  # pragma: no cover - checked above
            raise UnknownStatusError(f"Unknown status key: {status_key!r}")
        return _status_from_row(row)

    async def delete_status(self, status_key: str) -> None:
        """Hide a status (soft delete). The status currently in use cannot be deleted."""
        async with self._write() as conn:
            await _require_status(conn, status_key)
            state = await _fetch_app_state(conn)
            if state.active_status_key == status_key:
                raise StorageError("switch to another status before deleting this one")
            now = self._now()
            await _execute(
                conn,
                "UPDATE statuses SET deleted_at = ?, is_active = 0, updated_at = ? WHERE key = ?",
                (now, now, status_key),
            )

    async def list_reply_steps(self, status_key: str) -> list[ReplyStep]:
        async with self._read() as conn:
            rows = await _fetch_all(
                conn,
                "SELECT position, kind, content FROM reply_steps WHERE status_key = ? ORDER BY position",
                (status_key,),
            )
        return [ReplyStep(int(r["position"]), r["kind"], r["content"]) for r in rows]

    async def replace_reply_steps(self, status_key: str, steps: Sequence[tuple[str, str]]) -> None:
        """Replace all steps of a status. The first text step also becomes its ``reply_text``."""
        clean_steps = _validate_steps(steps)
        async with self._write() as conn:
            await _require_status(conn, status_key)
            await _write_steps(conn, status_key, clean_steps, self._now())

    async def get_owner_settings(self) -> OwnerSettings:
        async with self._read() as conn:
            row = await _fetch_one(
                conn, "SELECT timezone, language, owner_name, updated_at FROM owner_settings WHERE id = 1",
            )
        if row is None:  # pragma: no cover - seeded on initialize
            raise StorageError("owner settings are missing")
        return OwnerSettings(row[0], row[1], row[2], row[3])

    async def save_owner_settings(self, *, timezone: str, language: str, owner_name: str) -> OwnerSettings:
        zone = (timezone or "").strip()
        try:
            ZoneInfo(zone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise InvalidReplyTextError("unknown timezone") from exc
        if language not in ("fa", "en"):
            raise InvalidReplyTextError("language must be 'fa' or 'en'")
        name = (owner_name or "").strip()
        if len(name) > 64 or any(ch in name for ch in "\r\n\t"):
            raise InvalidReplyTextError("owner name must be one line of at most 64 characters")
        async with self._write() as conn:
            await _execute(
                conn,
                "UPDATE owner_settings SET timezone = ?, language = ?, owner_name = ?, updated_at = ? WHERE id = 1",
                (zone, language, name, self._now()),
            )
        return await self.get_owner_settings()

    async def add_reply_exception(self, chat_id: int) -> None:
        async with self._write() as conn:
            await _execute(
                conn,
                "INSERT OR IGNORE INTO reply_exceptions (chat_id, created_at) VALUES (?, ?)",
                (int(chat_id), self._now()),
            )

    async def remove_reply_exception(self, chat_id: int) -> bool:
        async with self._write() as conn:
            removed = await _execute(conn, "DELETE FROM reply_exceptions WHERE chat_id = ?", (int(chat_id),))
        return removed == 1

    async def is_reply_exception(self, chat_id: int) -> bool:
        async with self._read() as conn:
            row = await _fetch_one(conn, "SELECT 1 FROM reply_exceptions WHERE chat_id = ?", (int(chat_id),))
        return row is not None

    async def list_reply_exceptions(self) -> list[int]:
        async with self._read() as conn:
            rows = await _fetch_all(conn, "SELECT chat_id FROM reply_exceptions ORDER BY chat_id")
        return [int(row[0]) for row in rows]

    async def last_reply_sent_at(self, connection_id: str, chat_id: int) -> datetime | None:
        """Completion time of the latest reply (sent or partial) to this contact, any session."""
        async with self._read() as conn:
            row = await _fetch_one(
                conn,
                "SELECT MAX(completed_at) FROM reply_deliveries "
                "WHERE connection_id = ? AND chat_id = ? AND state IN ('sent', 'partial')",
                (connection_id, int(chat_id)),
            )
        if row is None or row[0] is None:
            return None
        return datetime.strptime(row[0], _TIMESTAMP_FORMAT).replace(tzinfo=UTC)

    # -- business connection ------------------------------------------------------

    async def save_business_connection(
        self,
        connection_id: str,
        is_enabled: bool,
        can_reply: bool,
        owner_user_id: int,
    ) -> BusinessConnectionState:
        if not isinstance(connection_id, str) or not connection_id:
            raise ValueError("connection_id must be a non-empty string")
        now = self._now()
        async with self._write() as conn:
            await _execute(
                conn,
                "INSERT INTO business_connections "
                "(connection_id, is_enabled, can_reply, owner_user_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (connection_id) DO UPDATE SET "
                "is_enabled = excluded.is_enabled, can_reply = excluded.can_reply, "
                "owner_user_id = excluded.owner_user_id, updated_at = excluded.updated_at",
                (connection_id, int(is_enabled), int(can_reply), owner_user_id, now, now),
            )
        return BusinessConnectionState(connection_id, bool(is_enabled), bool(can_reply), owner_user_id)

    async def get_business_connection(self) -> BusinessConnectionState | None:
        """Return the current connection: an enabled one if any, otherwise the latest known."""
        async with self._read() as conn:
            row = await _fetch_one(
                conn,
                "SELECT connection_id, is_enabled, can_reply, owner_user_id FROM business_connections "
                "ORDER BY is_enabled DESC, updated_at DESC, rowid DESC LIMIT 1",
            )
        if row is None:
            return None
        return BusinessConnectionState(
            connection_id=row["connection_id"],
            is_enabled=bool(row["is_enabled"]),
            can_reply=bool(row["can_reply"]),
            owner_user_id=row["owner_user_id"],
        )

    async def get_statistics(self) -> dict[str, int]:
        """Return privacy-preserving delivery counters for this bot's private database."""
        async with self._read() as conn:
            rows = await _fetch_all(
                conn,
                "SELECT state, COUNT(*) AS total FROM reply_deliveries GROUP BY state",
            )
            inbound = await _fetch_one(conn, "SELECT COUNT(*) AS total FROM processed_messages")
            connections = await _fetch_one(
                conn, "SELECT COUNT(*) AS total FROM business_connections WHERE is_enabled=1"
            )
        counts = {str(row["state"]): int(row["total"]) for row in rows}
        return {
            "sent": counts.get("sent", 0),
            "failed": counts.get("failed", 0),
            "uncertain": counts.get("uncertain", 0),
            "pending": counts.get("pending", 0),
            "inbound_events": int(inbound["total"]) if inbound else 0,
            "active_connections": int(connections["total"]) if connections else 0,
        }

    # -- deduplication & delivery -------------------------------------------------

    async def claim_inbound_event(self, connection_id: str, chat_id: int, message_id: int) -> bool:
        """Record an incoming message event. Returns True only the first time it is seen.

        Raises UnknownConnectionError if ``save_business_connection`` was never called
        for ``connection_id``.
        """
        async with self._write() as conn:
            await _require_connection(conn, connection_id)
            inserted = await _execute(
                conn,
                "INSERT OR IGNORE INTO processed_messages (connection_id, chat_id, message_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (connection_id, chat_id, message_id, self._now()),
            )
        return inserted == 1

    async def claim_reply(self, connection_id: str, chat_id: int, session_id: str) -> str | None:
        """Atomically reserve the single auto-reply for ``(connection_id, chat_id, session_id)``.

        Returns a unique ``claim_token`` if the caller now owns the reservation. The caller
        must send and then finish with ``complete_reply`` (or ``release_reply`` if it never
        attempted the send), passing the same token. Returns ``None`` (nothing may be sent)
        if the reply is already ``sent``, ``uncertain`` or being sent by another worker, if
        ``session_id`` is not the active session, or if auto-reply is off.

        A ``failed`` delivery is re-reserved. A ``pending`` one whose lease expired is handled
        per ``expired_lease_policy``; when reclaimed, the previous token stops working.

        Raises UnknownConnectionError if the connection was never saved.
        """
        now = self._clock()
        now_text = _format_timestamp(now)
        stale_before = _format_timestamp(now - self._pending_lease)
        token = uuid.uuid4().hex
        async with self._write() as conn:
            await _require_connection(conn, connection_id)
            if self._expired_lease_policy == "uncertain":
                # Strict at-most-once: an abandoned reservation may have been delivered.
                await _execute(
                    conn,
                    "UPDATE reply_deliveries SET state = 'uncertain', completed_at = ?, updated_at = ?, "
                    "last_error = 'lease expired while sending; outcome unknown' "
                    "WHERE connection_id = ? AND chat_id = ? AND session_id = ? "
                    "AND state = 'pending' AND claimed_at <= ?",
                    (now_text, now_text, connection_id, chat_id, session_id, stale_before),
                )
            # Single statement: insert a new reservation or re-reserve a failed/expired one.
            # The WHERE clause on app_state ties the claim to the live session, so a handler
            # holding a stale session id (status switched or auto-reply disabled) is refused.
            # claim_token is overwritten on re-reservation, which revokes the previous owner.
            claimed = await _execute(
                conn,
                "INSERT INTO reply_deliveries "
                "(connection_id, chat_id, session_id, state, attempts, claim_token, claimed_at, updated_at) "
                "SELECT ?, ?, ?, 'pending', 1, ?, ?, ? FROM app_state "
                "WHERE id = 1 AND enabled = 1 AND session_id = ? "
                "ON CONFLICT (connection_id, chat_id, session_id) DO UPDATE SET "
                "state = 'pending', attempts = reply_deliveries.attempts + 1, "
                "claim_token = excluded.claim_token, last_error = NULL, completed_at = NULL, "
                "claimed_at = excluded.claimed_at, updated_at = excluded.updated_at "
                "WHERE reply_deliveries.state = 'failed' "
                "OR (reply_deliveries.state = 'pending' AND reply_deliveries.claimed_at <= ?)",
                (connection_id, chat_id, session_id, token, now_text, now_text, session_id, stale_before),
            )
        return token if claimed == 1 else None

    async def complete_reply(
        self,
        connection_id: str,
        chat_id: int,
        session_id: str,
        claim_token: str,
        outcome: DeliveryOutcome,
        telegram_message_id: int | None = None,
        error: str | None = None,
        parts_sent: int = 0,
    ) -> bool:
        """Record the outcome of a reservation. Only the current token holder can do this.

        One atomic statement that matches the key, ``claim_token`` AND ``state = 'pending'``:
        a worker whose lease was reclaimed, a second completion of the same reservation, or
        a token from another chat/session all update nothing and return False.

        ``SENT`` -> final ``sent``. ``REJECTED`` -> ``failed`` (a later message may retry).
        ``UNCERTAIN`` -> ``uncertain`` (never retried automatically). ``telegram_message_id``
        is stored for ``SENT`` only; ``error`` for the other two (token-redacted, truncated).
        Returns True only if this call performed the transition.
        """
        if not isinstance(outcome, DeliveryOutcome):
            raise TypeError("outcome must be a DeliveryOutcome")
        if not isinstance(claim_token, str) or not claim_token:
            raise ValueError("claim_token must be a non-empty string (claim_reply returned None?)")
        new_state = {
            DeliveryOutcome.SENT: "sent",
            DeliveryOutcome.REJECTED: "failed",
            DeliveryOutcome.UNCERTAIN: "uncertain",
            DeliveryOutcome.PARTIAL: "partial",
        }[outcome]
        sent = outcome is DeliveryOutcome.SENT
        now = self._now()
        async with self._write() as conn:
            updated = await _execute(
                conn,
                "UPDATE reply_deliveries SET state = ?, parts_sent = ?, telegram_message_id = ?, "
                "last_error = ?, completed_at = ?, updated_at = ? "
                "WHERE connection_id = ? AND chat_id = ? AND session_id = ? "
                "AND claim_token = ? AND state = 'pending'",
                (
                    new_state,
                    max(0, int(parts_sent)),
                    telegram_message_id if sent else None,
                    None if sent else _sanitize_error(error),
                    now,
                    now,
                    connection_id,
                    chat_id,
                    session_id,
                    claim_token,
                ),
            )
        return updated == 1

    async def release_reply(
        self, connection_id: str, chat_id: int, session_id: str, claim_token: str
    ) -> bool:
        """Give up a reservation for which Telegram was never called (e.g. shutdown, or the
        connection lost its reply right between claim and send). The delivery becomes
        retryable (``failed``). Token-protected exactly like ``complete_reply``. Never call
        this after a send attempt started; use ``complete_reply`` with UNCERTAIN instead.
        """
        return await self.complete_reply(
            connection_id,
            chat_id,
            session_id,
            claim_token,
            DeliveryOutcome.REJECTED,
            error="released without sending",
        )

    # -- maintenance --------------------------------------------------------------

    async def prune_processed_messages(self, older_than_days: int = 30) -> int:
        """Delete dedup records older than ``older_than_days``. Returns the number removed."""
        if older_than_days < 1:
            raise ValueError("older_than_days must be at least 1")
        cutoff = _format_timestamp(self._clock() - timedelta(days=older_than_days))
        async with self._write() as conn:
            return await _execute(
                conn, "DELETE FROM processed_messages WHERE created_at < ?", (cutoff,)
            )

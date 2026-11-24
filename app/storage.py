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
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import TracebackType
from typing import Any, Literal, Self

import aiosqlite

from app.models import (
    DEFAULT_STATUSES,
    AppState,
    BusinessConnectionState,
    DeliveryOutcome,
    Status,
)

__all__ = [
    "SCHEMA_VERSION",
    "Database",
    "InvalidReplyTextError",
    "SchemaVersionError",
    "StorageError",
    "UnknownConnectionError",
    "UnknownStatusError",
]

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 2
SCHEMA_PATH = Path(__file__).with_name("database_schema.sql")

MAX_REPLY_TEXT_LENGTH = 4096  # Telegram's sendMessage text limit
MAX_ERROR_LENGTH = 500
DEFAULT_PENDING_LEASE_SECONDS = 300

_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"  # fixed width, so strings sort chronologically
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

# {target_version: SQL script migrating from target_version - 1}.
_MIGRATIONS: dict[int, str] = {2: _MIGRATION_2}


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
    return datetime.now(timezone.utc)


def _new_uuid() -> str:
    return str(uuid.uuid4())


def _format_timestamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime(_TIMESTAMP_FORMAT)


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
    if await _fetch_one(conn, "SELECT 1 FROM statuses WHERE key = ?", (status_key,)) is None:
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
        async with self._lock:
            async with _begin(conn):
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
                "SELECT key, title, emoji, reply_text FROM statuses ORDER BY sort_order, key",
            )
        return [_status_from_row(row) for row in rows]

    async def get_status(self, status_key: str) -> Status | None:
        async with self._read() as conn:
            row = await _fetch_one(
                conn,
                "SELECT key, title, emoji, reply_text FROM statuses WHERE key = ?",
                (status_key,),
            )
        return _status_from_row(row) if row is not None else None

    async def save_status_text(self, status_key: str, reply_text: str) -> Status:
        """Replace a status's reply text. Does not affect the current session."""
        cleaned = _validate_reply_text(reply_text)
        async with self._write() as conn:
            changed = await _execute(
                conn,
                "UPDATE statuses SET reply_text = ?, updated_at = ? WHERE key = ?",
                (cleaned, self._now(), status_key),
            )
            if changed != 1:
                raise UnknownStatusError(f"Unknown status key: {status_key!r}")
            row = await _fetch_one(
                conn,
                "SELECT key, title, emoji, reply_text FROM statuses WHERE key = ?",
                (status_key,),
            )
        if row is None:  # pragma: no cover - the UPDATE above just matched this row
            raise UnknownStatusError(f"Unknown status key: {status_key!r}")
        return _status_from_row(row)

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
        }[outcome]
        sent = outcome is DeliveryOutcome.SENT
        now = self._now()
        async with self._write() as conn:
            updated = await _execute(
                conn,
                "UPDATE reply_deliveries SET state = ?, telegram_message_id = ?, last_error = ?, "
                "completed_at = ?, updated_at = ? "
                "WHERE connection_id = ? AND chat_id = ? AND session_id = ? "
                "AND claim_token = ? AND state = 'pending'",
                (
                    new_state,
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

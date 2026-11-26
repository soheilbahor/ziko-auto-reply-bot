"""Encrypted registry for user-owned ZIKO child bots.

Regular Telegram users may register one child bot; platform administrators may
register multiple child bots. Tokens are encrypted with a local Fernet key.
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from pathlib import Path

import aiosqlite
from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from app import key_location


class TenantRegistryError(RuntimeError):
    """Base class for tenant registry errors."""


class UserAlreadyHasBot(TenantRegistryError):
    pass


class BotAlreadyRegistered(TenantRegistryError):
    pass


class TokenDecryptionError(TenantRegistryError):
    pass


class TenantLimitReached(TenantRegistryError):
    """Registration is blocked by a configured platform or user quota."""


@dataclass(frozen=True, slots=True)
class Tenant:
    user_id: int
    bot_id: int
    username: str
    encrypted_token: str = field(repr=False)
    created_at: str
    enabled: bool = True
    bot_token: str = field(default="", repr=False)


class TenantRegistry:
    """Small SQLite control-plane registry; tenant data lives in separate DB files."""

    def __init__(
        self, database_path: Path, *, max_total: int | None = None,
        per_user_per_hour: int | None = None,
    ) -> None:
        self.path = Path(database_path)
        self.max_total = max_total if max_total is not None else int(os.environ.get("ZIKO_TENANT_MAX_TOTAL", "50"))
        self.per_user_per_hour = (
            per_user_per_hour if per_user_per_hour is not None
            else int(os.environ.get("ZIKO_TENANT_PER_USER_PER_HOUR", "3"))
        )
        if self.max_total < 1 or self.per_user_per_hour < 1:
            raise ValueError("Tenant registration limits must be positive integers.")
        # Legacy default: next to the registry. ZIKO_TENANT_KEY_FILE moves it out of the
        # data directory (see app/key_location.py); then it is never created here.
        self.key_path, self.key_external = key_location.resolve(self.path.parent)
        self._conn: aiosqlite.Connection | None = None
        self._fernet: MultiFernet | None = None
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        self._load_or_create_key()
        conn = await aiosqlite.connect(str(self.path), isolation_level=None)
        os.chmod(self.path, 0o600)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.execute("PRAGMA busy_timeout=5000")
        await conn.execute(
            """CREATE TABLE IF NOT EXISTS tenants (
                tenant_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL CHECK(user_id > 0),
                bot_id INTEGER NOT NULL UNIQUE CHECK(bot_id > 0),
                username TEXT NOT NULL,
                encrypted_token TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
                created_at TEXT NOT NULL
            )"""
        )
        # Migrate the original one-bot-per-user schema without losing any row.
        async with conn.execute("PRAGMA table_info(tenants)") as cur:
            columns = await cur.fetchall()
        user_id_is_primary = any(row["name"] == "user_id" and int(row["pk"]) > 0 for row in columns)
        if user_id_is_primary:
            await conn.execute("BEGIN IMMEDIATE")
            try:
                await conn.execute(
                    """CREATE TABLE tenants_v2 (
                        tenant_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER NOT NULL CHECK(user_id > 0),
                        bot_id INTEGER NOT NULL UNIQUE CHECK(bot_id > 0),
                        username TEXT NOT NULL,
                        encrypted_token TEXT NOT NULL,
                        enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
                        created_at TEXT NOT NULL
                    )"""
                )
                await conn.execute(
                    """INSERT INTO tenants_v2
                       (user_id, bot_id, username, encrypted_token, enabled, created_at)
                       SELECT user_id, bot_id, username, encrypted_token, enabled, created_at
                       FROM tenants ORDER BY created_at"""
                )
                await conn.execute("DROP TABLE tenants")
                await conn.execute("ALTER TABLE tenants_v2 RENAME TO tenants")
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_tenants_user_id ON tenants(user_id)")
        await conn.commit()
        self._conn = conn

    def _load_or_create_key(self) -> None:
        if self.key_external:
            # Owned by root and read-only for the service: never create or chmod it here.
            try:
                key_location.check_external_file(self.key_path)
            except key_location.KeyLocationError as exc:
                raise TenantRegistryError(str(exc)) from None
            self._fernet = self._parse_key(self.key_path.read_bytes())
            return
        try:
            fd = os.open(self.key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "wb") as stream:
                stream.write(Fernet.generate_key())
                stream.flush()
                os.fsync(stream.fileno())
        os.chmod(self.key_path, 0o600)
        self._fernet = self._parse_key(self.key_path.read_bytes())

    @staticmethod
    def _parse_key(raw: bytes) -> MultiFernet:
        # One key per line, newest first (see app/key_rotation.py). MultiFernet encrypts
        # with the first key and decrypts with any, so an interrupted rotation never
        # locks the service out of its own tokens.
        lines = [line.strip() for line in raw.splitlines() if line.strip()]
        try:
            if not lines:
                raise ValueError("empty key file")
            return MultiFernet([Fernet(line) for line in lines])
        except (ValueError, TypeError):
            raise TenantRegistryError("Tenant encryption key is invalid") from None

    def _require_conn(self) -> aiosqlite.Connection:
        if self._conn is None or self._fernet is None:
            raise TenantRegistryError("Tenant registry is not initialized")
        return self._conn

    async def add_tenant(
        self, *, user_id: int, bot_id: int, username: str, token: str,
        allow_multiple: bool = False,
    ) -> Tenant:
        conn = self._require_conn()
        assert self._fernet is not None
        encrypted = self._fernet.encrypt(token.encode("utf-8")).decode("ascii")
        async with self._lock:
            await conn.execute("BEGIN IMMEDIATE")
            try:
                async with conn.execute("SELECT COUNT(*) FROM tenants") as cur:
                    total = int((await cur.fetchone())[0])
                if total >= self.max_total:
                    raise TenantLimitReached("The platform tenant capacity has been reached")
                async with conn.execute(
                    "SELECT COUNT(*) FROM tenants WHERE user_id=? "
                    "AND created_at >= strftime('%Y-%m-%dT%H:%M:%fZ','now','-1 hour')",
                    (user_id,),
                ) as cur:
                    recent = int((await cur.fetchone())[0])
                if recent >= self.per_user_per_hour:
                    raise TenantLimitReached("The user registration rate limit has been reached")
                if not allow_multiple:
                    async with conn.execute("SELECT 1 FROM tenants WHERE user_id=? LIMIT 1", (user_id,)) as cur:
                        if await cur.fetchone():
                            raise UserAlreadyHasBot("This user already has a registered bot")
                try:
                    await conn.execute(
                        "INSERT INTO tenants(user_id,bot_id,username,encrypted_token,created_at) "
                        "VALUES(?,?,?,?,strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
                        (user_id, bot_id, username, encrypted),
                    )
                except aiosqlite.IntegrityError:
                    raise BotAlreadyRegistered("This bot is already registered") from None
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise
        return Tenant(user_id, bot_id, username, encrypted, "", True, token)

    def _tenant(self, row: aiosqlite.Row, *, reveal_token: bool) -> Tenant:
        token = ""
        if reveal_token:
            assert self._fernet is not None
            try:
                token = self._fernet.decrypt(row["encrypted_token"].encode("ascii")).decode("utf-8")
            except (InvalidToken, UnicodeDecodeError):
                raise TokenDecryptionError("A stored bot token cannot be decrypted; check the key backup") from None
        return Tenant(
            user_id=int(row["user_id"]), bot_id=int(row["bot_id"]),
            username=str(row["username"]), encrypted_token=str(row["encrypted_token"]),
            created_at=str(row["created_at"]), enabled=bool(row["enabled"]), bot_token=token,
        )

    async def update_tenant_credentials(
        self, *, user_id: int, bot_id: int, username: str, token: str
    ) -> Tenant | None:
        """Update a managed bot token after Telegram reports a token/owner update.

        The existing owner binding is immutable here: ownership changes require
        an explicit administrative transfer workflow.
        """
        conn = self._require_conn()
        assert self._fernet is not None
        encrypted = self._fernet.encrypt(token.encode("utf-8")).decode("ascii")
        async with self._lock:
            await conn.execute("BEGIN IMMEDIATE")
            try:
                async with conn.execute(
                    "SELECT * FROM tenants WHERE bot_id=?", (bot_id,)
                ) as cur:
                    row = await cur.fetchone()
                if row is None or int(row["user_id"]) != user_id:
                    await conn.rollback()
                    return None
                await conn.execute(
                    "UPDATE tenants SET username=?, encrypted_token=? WHERE bot_id=? AND user_id=?",
                    (username, encrypted, bot_id, user_id),
                )
                async with conn.execute(
                    "SELECT * FROM tenants WHERE bot_id=?", (bot_id,)
                ) as cur:
                    updated = await cur.fetchone()
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise
        return self._tenant(updated, reveal_token=True) if updated else None

    async def get_tenant(self, user_id: int, *, reveal_token: bool = False) -> Tenant | None:
        """Return the oldest registered bot for a user (legacy one-bot API)."""
        conn = self._require_conn()
        async with self._lock:
            async with conn.execute(
                "SELECT * FROM tenants WHERE user_id=? ORDER BY tenant_id ASC LIMIT 1",
                (user_id,),
            ) as cur:
                row = await cur.fetchone()
        return self._tenant(row, reveal_token=reveal_token) if row else None

    async def get_tenant_by_bot_id(self, bot_id: int, *, reveal_token: bool = False) -> Tenant | None:
        conn = self._require_conn()
        async with self._lock:
            async with conn.execute("SELECT * FROM tenants WHERE bot_id=?", (bot_id,)) as cur:
                row = await cur.fetchone()
        return self._tenant(row, reveal_token=reveal_token) if row else None

    async def list_tenants(
        self, *, reveal_tokens: bool = False, user_id: int | None = None
    ) -> list[Tenant]:
        conn = self._require_conn()
        async with self._lock:
            if user_id is None:
                query = "SELECT * FROM tenants ORDER BY tenant_id ASC"
                params: tuple[object, ...] = ()
            else:
                query = "SELECT * FROM tenants WHERE user_id=? ORDER BY tenant_id ASC"
                params = (user_id,)
            async with conn.execute(query, params) as cur:
                rows = await cur.fetchall()
        return [self._tenant(row, reveal_token=reveal_tokens) for row in rows]


    async def set_enabled(self, user_id: int, enabled: bool) -> bool:
        """Compatibility method: enable/disable every bot owned by a user."""
        conn = self._require_conn()
        async with self._lock:
            async with conn.execute(
                "UPDATE tenants SET enabled=? WHERE user_id=?", (int(enabled), user_id)
            ) as cur:
                changed = cur.rowcount > 0
            await conn.commit()
        return changed

    async def set_tenant_enabled(self, bot_id: int, enabled: bool) -> bool:
        conn = self._require_conn()
        async with self._lock:
            async with conn.execute(
                "UPDATE tenants SET enabled=? WHERE bot_id=?", (int(enabled), bot_id)
            ) as cur:
                changed = cur.rowcount > 0
            await conn.commit()
        return changed

    async def close(self) -> None:
        async with self._lock:
            conn, self._conn = self._conn, None
            if conn is not None:
                await conn.close()

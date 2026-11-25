"""Runtime supervisor for isolated child-bot pollers."""
from __future__ import annotations

import asyncio
import io
import logging
import os
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, BufferedInputFile, InputProfilePhotoStatic

from app.handlers.admin import create_admin_router
from app.handlers.business import create_business_router
from app.logging_setup import CorrelationMiddleware, correlation_scope, log_file_for_bot
from app.storage import Database
from app.tenant_registry import Tenant, TenantRegistry

logger = logging.getLogger("ziko.tenant_bots")

# A poller that stayed up at least this long is considered healthy: its restart backoff resets.
STABLE_RUN_SECONDS = 60.0
INITIAL_RESTART_DELAY = 2
MAX_RESTART_DELAY = 60


class TenantBotManager:
    def __init__(
        self, *, settings: Any, registry: TenantRegistry, master_bot: Bot,
        max_concurrent_pollers: int | None = None, log_dir: Path | None = None,
    ) -> None:
        self.settings = settings
        self.log_dir = log_dir if log_dir is not None else settings.database_path.parent / "logs"
        self.registry = registry
        self.master_bot = master_bot
        configured_limit = (
            max_concurrent_pollers
            if max_concurrent_pollers is not None
            else int(os.environ.get("ZIKO_TENANT_MAX_CONCURRENT_POLLERS", "50"))
        )
        if configured_limit < 1:
            raise ValueError("ZIKO_TENANT_MAX_CONCURRENT_POLLERS must be a positive integer.")
        self.max_concurrent_pollers = configured_limit
        self._poller_slots = asyncio.Semaphore(configured_limit)
        self._tasks: dict[int, asyncio.Task[None]] = {}  # keyed by Telegram bot_id
        self._running: set[int] = set()
        self._waiting: set[int] = set()
        # Profile photo sync: the master photo is read once per process and
        # pushed to each child bot once per process lifetime (best effort).
        self._photo_loaded = False
        self._photo_bytes: bytes | None = None
        self._photo_synced: set[int] = set()
        self._closing = False
        self._lifecycle_lock = asyncio.Lock()

    @property
    def tenant_db_dir(self) -> Path:
        return self.settings.database_path.parent / "tenants"

    async def start_all(self) -> None:
        for tenant in await self.registry.list_tenants(reveal_tokens=True):
            if tenant.enabled:
                await self.start_tenant(tenant)

    async def start_tenant(self, tenant: Tenant) -> None:
        async with self._lifecycle_lock:
            if self._closing or not tenant.enabled:
                return
            current = self._tasks.get(tenant.bot_id)
            if current is not None and not current.done():
                return
            task = asyncio.create_task(
                self._supervise_tenant(tenant), name=f"ziko-tenant-{tenant.bot_id}"
            )
            self._tasks[tenant.bot_id] = task

    async def restart_tenant(self, bot_id: int) -> None:
        """Stop the old credential/session before starting the latest registry record."""
        await self.stop_tenant(bot_id)
        if self._closing:
            return
        tenant = await self.registry.get_tenant_by_bot_id(bot_id, reveal_token=True)
        if tenant is not None and tenant.enabled:
            await self.start_tenant(tenant)

    async def stop_tenant(self, bot_id: int) -> None:
        async with self._lifecycle_lock:
            task = self._tasks.pop(bot_id, None)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._running.discard(bot_id)
        self._waiting.discard(bot_id)

    async def _supervise_tenant(self, tenant: Tenant) -> None:
        delay = INITIAL_RESTART_DELAY
        bot_id = tenant.bot_id
        scope = {"bot_id": bot_id}
        failures = 0  # consecutive runs that did not reach STABLE_RUN_SECONDS
        while not self._closing:
            attempt_started = time.monotonic()
            self._waiting.add(bot_id)
            logger.info(
                "Child bot waiting for polling slot user_id=%s active=%s waiting=%s limit=%s",
                tenant.user_id, len(self._running), len(self._waiting),
                self.max_concurrent_pollers, extra=scope,
            )
            try:
                async with self._poller_slots:
                    self._waiting.discard(bot_id)
                    self._running.add(bot_id)
                    logger.info(
                        "Child bot acquired polling slot user_id=%s active=%s waiting=%s",
                        tenant.user_id, len(self._running), len(self._waiting), extra=scope,
                    )
                    try:
                        await self._run_tenant_once(tenant)
                    finally:
                        self._running.discard(bot_id)
                failures += 1
                logger.warning(
                    "Child bot polling ended user_id=%s retry=%s; restarting",
                    tenant.user_id, failures, extra=scope,
                )
            except asyncio.CancelledError:
                raise
            except TelegramUnauthorizedError:
                await self.registry.set_tenant_enabled(bot_id, False)
                logger.error(
                    "Child bot token was rejected; tenant disabled user_id=%s",
                    tenant.user_id, extra={**scope, "stage": "POLLING"},
                )
                await self._notify_admins_token_revoked(tenant)
                return
            except Exception as exc:
                # The error type only: exception text may contain Telegram URLs with the token.
                failures += 1
                logger.error(
                    "Child bot polling failed user_id=%s error=%s retry=%s",
                    tenant.user_id, type(exc).__name__, failures,
                    extra={**scope, "stage": "POLLING"},
                )
            finally:
                self._waiting.discard(bot_id)
                self._running.discard(bot_id)
            if time.monotonic() - attempt_started >= STABLE_RUN_SECONDS:
                delay = INITIAL_RESTART_DELAY
                failures = 0
            if self._closing:
                break
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                raise
            delay = min(delay * 2, MAX_RESTART_DELAY)

    async def _notify_admins_token_revoked(self, tenant: Tenant) -> None:
        """Notify platform administrators without exposing the revoked token."""
        text = (
            "⚠️ ربات زیرمجموعه به دلیل رد شدن توکن توسط تلگرام غیرفعال شد.\n"
            f"ربات: @{tenant.username}\n"
            f"Bot ID: {tenant.bot_id}\n"
            f"مالک: {tenant.user_id}\n"
            "برای فعال‌سازی مجدد، مالک باید توکن معتبر را دوباره متصل کند."
        )
        for admin_id in sorted(getattr(self.settings, "admin_ids", ())):
            try:
                await self.master_bot.send_message(chat_id=admin_id, text=text)
            except Exception as exc:
                logger.warning(
                    "Could not notify platform admin about revoked tenant bot_id=%s error=%s",
                    tenant.bot_id, type(exc).__name__,
                )

    async def _master_photo_bytes(self) -> bytes | None:
        """Return the master bot's profile photo, or the configured fallback file.

        Telegram returns the master's own photos through getUserProfilePhotos;
        when that is unavailable, ZIKO_TENANT_PROFILE_PHOTO (a JPEG path) is used.
        """
        if self._photo_loaded:
            return self._photo_bytes
        self._photo_loaded = True
        data: bytes | None = None
        try:
            photos = await self.master_bot.get_user_profile_photos(
                user_id=self.master_bot.id, limit=1
            )
            if photos.total_count and photos.photos:
                largest = photos.photos[0][-1]
                buffer = io.BytesIO()
                await self.master_bot.download(largest, destination=buffer)
                data = buffer.getvalue() or None
        except Exception as exc:
            logger.warning("Could not read master profile photo error=%s", type(exc).__name__)
        if data is None:
            fallback = os.environ.get("ZIKO_TENANT_PROFILE_PHOTO", "").strip()
            if fallback:
                try:
                    data = Path(fallback).read_bytes() or None
                except OSError:
                    logger.warning("Configured ZIKO_TENANT_PROFILE_PHOTO could not be read")
        self._photo_bytes = data
        return data

    async def _sync_profile_photo(self, bot: Bot, tenant: Tenant) -> None:
        """Copy the master photo to a child bot. Never blocks or stops polling."""
        if tenant.bot_id in self._photo_synced:
            return
        try:
            data = await self._master_photo_bytes()
            if not data:
                logger.info("No master profile photo available; child photo not synced bot_id=%s", tenant.bot_id)
                return
            await bot.set_my_profile_photo(
                photo=InputProfilePhotoStatic(
                    photo=BufferedInputFile(data, filename="ziko-profile.jpg")
                )
            )
            self._photo_synced.add(tenant.bot_id)
            logger.info("Child bot profile photo synced bot_id=%s", tenant.bot_id)
        except Exception as exc:
            logger.warning(
                "Child bot profile photo sync failed bot_id=%s error=%s",
                tenant.bot_id, type(exc).__name__,
            )

    async def _run_tenant_once(self, tenant: Tenant) -> None:
        # Records logged while this child polls go to its own file. Tasks created
        # below inherit this scope, so aiogram's records are routed correctly too.
        with correlation_scope(bot_id=tenant.bot_id):
            await self._run_tenant_polling(tenant)

    async def _run_tenant_polling(self, tenant: Tenant) -> None:
        bot = Bot(token=tenant.bot_token)
        self.tenant_db_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.tenant_db_dir, 0o700)
        # Keep the original owner's database path for their oldest registered bot.
        # This is intentionally non-destructive so rollback to the previous release
        # can still find existing per-user databases.
        legacy_path = self.tenant_db_dir / f"{tenant.user_id}.sqlite3"
        primary = await self.registry.get_tenant(tenant.user_id)
        if primary is not None and primary.bot_id == tenant.bot_id and legacy_path.is_file():
            db_path = legacy_path
        else:
            db_path = self.tenant_db_dir / f"bot-{tenant.bot_id}.sqlite3"
        db = Database(db_path)
        tenant_settings = replace(
            self.settings,
            bot_token=tenant.bot_token,
            admin_ids=frozenset({tenant.user_id}),
            database_path=db_path,
            business_owner_id=tenant.user_id,
        )
        dp = Dispatcher(storage=MemoryStorage())
        dp.update.outer_middleware(CorrelationMiddleware(tenant=True))
        try:
            await db.initialize()
            await bot.set_my_commands([
                BotCommand(command="start", description="پنل مدیریت ربات"),
                BotCommand(command="menu", description="نمایش منو"),
                BotCommand(command="cancel", description="لغو عملیات"),
            ])
            await self._sync_profile_photo(bot, tenant)
            dp.include_router(create_admin_router(
                db, tenant_settings, log_path=log_file_for_bot(self.log_dir, tenant.bot_id),
            ))
            dp.include_router(create_business_router(db, tenant_settings))

            logger.info("Child bot polling started user_id=%s bot_id=%s", tenant.user_id, tenant.bot_id)
            await dp.start_polling(
                bot,
                allowed_updates=["message", "callback_query", "business_connection", "business_message"],
                close_bot_session=False,
                handle_signals=False,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            # Do not log exception text: Telegram URLs and HTTP errors can contain bot tokens.
            raise
        finally:
            await bot.session.close()
            await db.close()

    async def close(self) -> None:
        # Set this before cancelling: no supervisor may enter its restart path
        # while systemd is stopping the master process.
        async with self._lifecycle_lock:
            self._closing = True
            tasks = list(self._tasks.values())
            self._tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True), timeout=20
                )
            except asyncio.TimeoutError:
                logger.error("Timed out while stopping child bot pollers")
        self._running.clear()
        self._waiting.clear()

    def running_count(self) -> int:
        """Number of child bots that currently hold a polling slot."""
        return len(self._running)

    def waiting_count(self) -> int:
        """Number of enabled child bots waiting for a polling slot."""
        return len(self._waiting)

    def runtime_status(self, bot_id: int) -> str:
        """Human-readable runtime state for platform administration."""
        if bot_id in self._running:
            return "در حال اجرا"
        if bot_id in self._waiting:
            return "در صف انتظار"
        task = self._tasks.get(bot_id)
        if task is not None and not task.done():
            return "در حال راه‌اندازی"
        return "متوقف"

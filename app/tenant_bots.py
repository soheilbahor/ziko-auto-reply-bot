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
from aiogram.exceptions import TelegramUnauthorizedError  # noqa: F401  (tests patch this name)
from aiogram.methods import GetUpdates
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, BufferedInputFile, InputProfilePhotoStatic

from app import daily_report
from app.bot_lifecycle import (
    BotStatus, PollingWatchdog, State, TokenMismatch, WatchdogTripped, classify,
)
from app.handlers.admin import create_admin_router
from app.handlers.business import create_business_router
from app.logging_setup import CorrelationMiddleware, correlation_scope, log_file_for_bot, redact
from app.storage import Database
from app.telegram_stats import STATS, TelemetryRequestMiddleware
from app.tenant_registry import Tenant, TenantRegistry

logger = logging.getLogger("ziko.tenant_bots")

# A poller that stayed up at least this long is considered healthy: its restart backoff resets.
STABLE_RUN_SECONDS = 60.0
INITIAL_RESTART_DELAY = 2
MAX_RESTART_DELAY = 60
# Retry a 409 Conflict at most this long: after a restart, the previous long-poll of the
# same token can take up to one polling timeout to expire on Telegram's side.
CONFLICT_RETRY_DELAY = 30
STARTUP_TIMEOUT = 60  # getMe + deleteWebhook + first getUpdates must finish within this
# Floor for every restart wait, even when a test (or a bad config) sets the delays to 0:
# without it a poller that fails instantly restarts thousands of times per second.
MIN_RESTART_DELAY = 0.25


def _safe_error_text(exc: BaseException, token: str | None, limit: int = 300) -> str:
    """Exception message for logs: the tenant's own token removed exactly, then the
    pipeline-wide redaction rules (tokens, keys, bearer/authorization values)."""
    text = str(exc)
    if token:
        text = text.replace(token, "[REDACTED_TOKEN]")
    text = redact(text).replace("\n", " ")
    return text[:limit] + ("…" if len(text) > limit else "")


class _WatchdogMiddleware:
    """Session middleware feeding getUpdates outcomes to a PollingWatchdog.

    aiogram retries failed getUpdates forever and never raises (see app/bot_lifecycle.py),
    so this is the only reliable place to see that a poller is not actually receiving.
    It never changes requests or results; it re-raises every exception.
    """

    def __init__(self, watchdog: PollingWatchdog) -> None:
        self.watchdog = watchdog

    async def __call__(self, make_request: Any, bot: Any, method: Any) -> Any:
        if not isinstance(method, GetUpdates):
            return await make_request(bot, method)
        try:
            result = await make_request(bot, method)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.watchdog.failed(exc)
            raise
        self.watchdog.ok()
        return result


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
        self._status: dict[int, BotStatus] = {}
        self._watchdogs: dict[int, PollingWatchdog] = {}

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
        self._set_state(bot_id, State.STOPPED)
        STATS.forget(bot_id)

    def _set_state(self, bot_id: int, state: State, error: Any = None) -> None:
        status = self._status.setdefault(bot_id, BotStatus(bot_id))
        previous = status.state
        status.move(state, error=error)
        if previous is not state:
            logger.info("Child bot state %s -> %s%s", previous.value, state.value,
                        f" error={error.code}" if error is not None else "",
                        extra={"bot_id": bot_id})

    def _attach_watchdog(self, bot_id: int) -> PollingWatchdog:
        """Create the getUpdates watchdog for one poller run and make it visible to runtime_state."""
        watchdog = PollingWatchdog()
        self._watchdogs[bot_id] = watchdog
        return watchdog

    def _confirm_polling(self, bot_id: int, watchdog: PollingWatchdog) -> bool:
        """Mark a bot RUNNING. Refused unless Telegram has answered getUpdates and the watchdog
        has not tripped: this is the only place a child bot becomes RUNNING."""
        if self._watchdogs.get(bot_id) is not watchdog or not watchdog.first_poll or watchdog.tripped:
            return False
        self._set_state(bot_id, State.RUNNING)
        return True

    def status(self, bot_id: int) -> BotStatus | None:
        return self._status.get(bot_id)

    def statuses(self) -> list[dict[str, Any]]:
        return [s.as_dict() for s in self._status.values()]

    async def _supervise_tenant(self, tenant: Tenant) -> None:
        delay = INITIAL_RESTART_DELAY
        bot_id = tenant.bot_id
        scope = {"bot_id": bot_id}
        failures = 0  # consecutive runs that did not reach STABLE_RUN_SECONDS
        while not self._closing:
            attempt_started = time.monotonic()
            self._waiting.add(bot_id)
            self._set_state(bot_id, State.QUEUED)
            logger.info(
                "Child bot waiting for polling slot user_id=%s active=%s waiting=%s limit=%s",
                tenant.user_id, len(self._running), len(self._waiting),
                self.max_concurrent_pollers, extra=scope,
            )
            diagnosis = None
            error: BaseException | None = None
            try:
                async with self._poller_slots:
                    self._waiting.discard(bot_id)
                    self._running.add(bot_id)
                    self._set_state(bot_id, State.STARTING)
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
            except WatchdogTripped as trip:
                # aiogram swallowed these errors and would have retried forever.
                diagnosis = classify(trip.error or trip.error_name)
                error = trip.error or trip
                failures += 1
            except Exception as exc:
                diagnosis = (classify("TelegramUnauthorizedError")
                             if isinstance(exc, TelegramUnauthorizedError) else classify(exc))
                error = exc
                failures += 1
            finally:
                self._waiting.discard(bot_id)
                self._running.discard(bot_id)
            if diagnosis is not None:
                # The exception class is always logged so "unknown" never hides the cause.
                # Its message is logged only after the tenant token is removed and the
                # pipeline redaction rules ran; the traceback (also redacted by the log
                # pipeline) is attached for unexpected errors and goes to the per-bot log
                # file, which is readable only by the service user.
                error_type = type(error).__name__ if error is not None else "?"
                unexpected = diagnosis.code == "unknown"
                logger.error(
                    "Child bot stopped code=%s error_type=%s user_id=%s retry=%s: %s | %s",
                    diagnosis.code, error_type, tenant.user_id, failures, diagnosis.operator,
                    _safe_error_text(error, tenant.bot_token) if error is not None else "",
                    extra={**scope, "stage": "POLLING"},
                    exc_info=(type(error), error, error.__traceback__) if unexpected and error else None,
                )
                if diagnosis.disable:
                    await self.registry.set_tenant_enabled(bot_id, False)
                    self._set_state(bot_id, State.FAILED, diagnosis)
                    await self._notify_admins_token_revoked(tenant, diagnosis)
                    return
                self._set_state(bot_id, State.BACKOFF, diagnosis)
                if diagnosis.code == "polling_conflict":
                    delay = max(delay, CONFLICT_RETRY_DELAY)
            else:
                self._set_state(bot_id, State.BACKOFF)
            if time.monotonic() - attempt_started >= STABLE_RUN_SECONDS:
                delay = INITIAL_RESTART_DELAY
                failures = 0
            if self._closing:
                break
            delay = min(max(delay, MIN_RESTART_DELAY), MAX_RESTART_DELAY)
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                raise
            delay = min(delay * 2, MAX_RESTART_DELAY)
        self._set_state(bot_id, State.STOPPED)

    async def _notify_admins_token_revoked(self, tenant: Tenant, diagnosis: Any = None) -> None:
        """Notify platform administrators (Telegram, Persian) without exposing the token."""
        diagnosis = diagnosis or classify("TelegramUnauthorizedError")
        text = (
            f"⚠️ ربات زیرمجموعه غیرفعال شد: {diagnosis.owner_title}\n"
            f"ربات: @{tenant.username}\n"
            f"Bot ID: {tenant.bot_id}\n"
            f"مالک: {tenant.user_id}\n"
            f"کد: {diagnosis.code}\n"
            f"{diagnosis.owner_action}"
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
        bot.session.middleware(TelemetryRequestMiddleware(tenant.bot_id, "child"))
        watchdog = self._attach_watchdog(tenant.bot_id)
        bot.session.middleware(_WatchdogMiddleware(watchdog))
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
            # 1. Prove the credential before anything else. TelegramUnauthorizedError here
            #    is classified and handled by the supervisor (tenant disabled, admins told).
            me = await asyncio.wait_for(bot.get_me(), timeout=STARTUP_TIMEOUT)
            if me.id != tenant.bot_id:
                raise TokenMismatch()
            # 2. A webhook makes every getUpdates fail with 409; polling would never receive.
            #    Pending updates are kept: the owner's first /start must not be dropped.
            await bot.delete_webhook(drop_pending_updates=False)
            await db.initialize()
            await bot.set_my_commands([
                BotCommand(command="start", description="پنل مدیریت ربات"),
                BotCommand(command="menu", description="نمایش منو"),
                BotCommand(command="diagnose", description="چرا پاسخ ارسال نمی‌شود؟"),
                BotCommand(command="help", description="راهنمای اتصال به حساب تلگرام"),
                BotCommand(command="logs", description="خطاهای اخیر"),
                BotCommand(command="cancel", description="لغو عملیات"),
            ])
            await self._sync_profile_photo(bot, tenant)
            dp.include_router(create_admin_router(
                db, tenant_settings, log_path=log_file_for_bot(self.log_dir, tenant.bot_id),
                onboarding=True,
            ))
            dp.include_router(create_business_router(db, tenant_settings))

            polling = asyncio.create_task(dp.start_polling(
                bot,
                allowed_updates=["message", "callback_query", "business_connection", "business_message"],
                close_bot_session=False,
                handle_signals=False,
            ), name=f"ziko-poll-{tenant.bot_id}")
            # Daily owner report (off unless the owner enabled it). Lives exactly as long as
            # this poller, so a restarted or stopped bot never leaves a second reporter.
            reporter = asyncio.create_task(daily_report.report_loop(db, bot, tenant.user_id),
                                           name=f"ziko-report-{tenant.bot_id}")
            tripped = asyncio.get_running_loop().create_future()
            watchdog.on_trip(lambda trip: tripped.done() or tripped.set_result(trip))
            try:
                # 3. RUNNING only once Telegram has actually answered getUpdates.
                started = time.monotonic()
                while not watchdog.first_poll and not polling.done() and not tripped.done():
                    if time.monotonic() - started > STARTUP_TIMEOUT:
                        break
                    await asyncio.sleep(0.05)
                if not tripped.done() and self._confirm_polling(tenant.bot_id, watchdog):
                    logger.info("Child bot polling verified user_id=%s bot_id=%s @%s",
                                tenant.user_id, tenant.bot_id, me.username)
                done, _ = await asyncio.wait({polling, tripped}, return_when=asyncio.FIRST_COMPLETED)
                if tripped in done:
                    raise tripped.result()
                polling.result()  # re-raise anything start_polling raised itself
            finally:
                # Always runs, including on cancellation (stop/restart/shutdown): a poller left
                # behind would keep polling the same token and cause 409 conflicts.
                if not tripped.done():
                    tripped.cancel()
                reporter.cancel()
                await asyncio.gather(reporter, return_exceptions=True)
                if not polling.done():
                    try:
                        await dp.stop_polling()
                    except RuntimeError:
                        pass  # polling had not started yet
                    polling.cancel()
                await asyncio.gather(polling, return_exceptions=True)
                self._watchdogs.pop(tenant.bot_id, None)
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

    def runtime_state(self, bot_id: int) -> str:
        """Machine-readable runtime state: running / waiting / starting / stopped.

        "running" now means Telegram has answered getUpdates (verified), not merely that a
        polling slot is held: a poller stuck in aiogram's retry loop reports "starting".
        """
        status = self._status.get(bot_id)
        watchdog = self._watchdogs.get(bot_id)
        if (status is not None and status.state is State.RUNNING and bot_id in self._running
                and (watchdog is None or watchdog.healthy)):
            return "running"
        if bot_id in self._running:
            return "degraded" if status is not None and status.state is State.RUNNING else "starting"
        if bot_id in self._waiting:
            return "waiting"
        task = self._tasks.get(bot_id)
        if task is not None and not task.done():
            return "starting"
        return "stopped"

    def runtime_status(self, bot_id: int) -> str:
        """Persian runtime label for the Telegram /tenants list (not used by the CLI)."""
        return {"running": "در حال اجرا", "degraded": "در حال اجرا، ارتباط با تلگرام قطع است",
                "waiting": "در صف انتظار", "starting": "در حال راه‌اندازی",
                "stopped": "متوقف"}[self.runtime_state(bot_id)]

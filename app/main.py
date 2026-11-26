"""ZIKO entry point: Telegram Bot API long polling only."""
from __future__ import annotations

import asyncio
import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, BotCommandScopeChat, InlineKeyboardButton

from app.config import load_settings
from app.handlers.admin import create_admin_router
from app.handlers.business import create_business_router
from app.handlers.tenants import CREATE_CB, create_tenant_router
from app.handlers.master import create_master_router, make_overview
from app import platform_overview
from app.overview_cache import OverviewCache
from app.platform_alerts import AlertConfig, AlertEngine, run_alert_loop
from app.logging_setup import AlertSink, CorrelationMiddleware, load_log_config, setup_logging
from app.telegram_stats import STATUS_FILE, TelemetryRequestMiddleware, status_writer
from app.tenant_bots import TenantBotManager
from app.tenant_registry import TenantRegistry
from app.storage import Database


async def run() -> None:
    settings = load_settings()
    os.umask(0o077)
    log_config = load_log_config(settings.database_path, os.environ)
    pipeline = setup_logging(log_config, level=settings.log_level)
    logger = logging.getLogger("ziko.main")
    db = Database(settings.database_path)
    registry = TenantRegistry(settings.database_path.with_name("tenants.sqlite3"))
    bot = Bot(token=settings.bot_token)
    # Counts/latency only; never alters requests (see app/telegram_stats.py).
    bot.session.middleware(TelemetryRequestMiddleware(bot.id, "master"))
    status_task: asyncio.Task[None] | None = None
    alert_task: asyncio.Task[None] | None = None
    cache_task: asyncio.Task[None] | None = None
    alerts: AlertSink | None = None
    if log_config.alerts_enabled:
        alerts = AlertSink(
            send=lambda admin_id, text: bot.send_message(chat_id=admin_id, text=text),
            admin_ids=settings.admin_ids,
            min_interval_seconds=log_config.alert_interval_seconds,
        )
        pipeline.set_alert_sink(alerts)
    manager = TenantBotManager(
        settings=settings, registry=registry, master_bot=bot, log_dir=log_config.directory,
    )
    try:
        logger.info("Master process starting log_dir=%s", log_config.directory)
        await db.initialize()
        await registry.initialize()
        # Keep the inbound-event idempotency table bounded without storing message bodies.
        retention_days = int(os.environ.get("ZIKO_EVENT_RETENTION_DAYS", "30"))
        if not 1 <= retention_days <= 365:
            raise ValueError("ZIKO_EVENT_RETENTION_DAYS must be between 1 and 365")
        pruned = await db.prune_processed_messages(older_than_days=retention_days)
        if pruned:
            logging.getLogger("ziko.storage").info(
                "Pruned %s old inbound event records", pruned
            )
        master_bot_id = (await bot.get_me()).id
        dp = Dispatcher(storage=MemoryStorage())
        dp.update.outer_middleware(CorrelationMiddleware(tenant=False))
        # Proactive platform alerts are OFF by default; ZIKO_PLATFORM_ALERTS=1 turns them on.
        platform_alerts: AlertEngine | None = None
        alerts_mute_path = settings.database_path.parent / "run" / "alerts.json"
        if os.environ.get("ZIKO_PLATFORM_ALERTS", "0").strip() == "1":
            platform_alerts = AlertEngine(AlertConfig.from_env(os.environ))
        # One overview snapshot shared by /platform and the alert loop, kept warm in the
        # background (ZIKO_OVERVIEW_REFRESH_SECONDS, default 30, range 5..600).
        refresh = float(os.environ.get("ZIKO_OVERVIEW_REFRESH_SECONDS", "30") or 30)
        if not 5 <= refresh <= 600:
            raise ValueError("ZIKO_OVERVIEW_REFRESH_SECONDS must be between 5 and 600")
        snapshot_path = settings.database_path.parent / "run" / platform_overview.SNAPSHOT_FILE
        overview_cache = OverviewCache(
            make_overview(registry, manager), max_age=refresh, interval=refresh,
            # Exported for the CLI (separate process); written off the event loop.
            on_refresh=lambda ov: asyncio.to_thread(platform_overview.write_snapshot, snapshot_path, ov),
        )
        dp.include_router(create_master_router(
            registry=registry, manager=manager, admin_ids=settings.admin_ids,
            log_dir=log_config.directory, alerts=platform_alerts,
            alerts_mute_path=alerts_mute_path, overview_cache=overview_cache,
        ))
        # Onboarding must precede the admin router's catch-all, while its filters
        # deliberately let admin /start and non-onboarding /cancel reach that router.
        dp.include_router(create_tenant_router(
            dispatcher=dp, registry=registry, manager=manager, master_bot_id=master_bot_id,
            admin_ids=settings.admin_ids,
        ))
        dp.include_router(create_admin_router(db, settings, extra_menu_rows=[
            [InlineKeyboardButton(text="✨ ساخت ربات زیرمجموعه", callback_data=CREATE_CB)],
        ]))
        dp.include_router(create_business_router(db, settings))
        status_task = asyncio.create_task(
            status_writer(settings.database_path.parent / "run" / STATUS_FILE),
            name="ziko-telegram-status",
        )
        await manager.start_all()
        cache_task = asyncio.create_task(overview_cache.run(), name="ziko-overview-cache")
        if platform_alerts is not None:
            # Alerts must judge current state, so each check forces a scan; concurrent
            # /platform taps share that same scan instead of starting their own.
            alert_task = asyncio.create_task(run_alert_loop(
                engine=platform_alerts, overview=lambda: overview_cache.get(fresh=True),
                send=lambda admin_id, text: bot.send_message(
                    chat_id=admin_id, text=text, parse_mode="HTML",
                    disable_web_page_preview=True),
                admin_ids=settings.admin_ids, mute_path=alerts_mute_path,
            ), name="ziko-platform-alerts")
        await bot.set_my_commands([
            BotCommand(command="start", description="باز کردن پنل مدیریت"),
            BotCommand(command="menu", description="نمایش منوی مدیریت"),
            BotCommand(command="create_bot", description="ساخت یا اتصال ربات اختصاصی"),
            BotCommand(command="connect_bot", description="اتصال ربات موجود با توکن"),
            BotCommand(command="cancel", description="لغو عملیات جاری"),
        ])
        # Platform admins additionally see the platform tools in their own chat menu.
        for admin_id in settings.admin_ids:
            try:
                await bot.set_my_commands([
                    BotCommand(command="platform", description="داشبورد همه‌ی ربات‌ها"),
                    BotCommand(command="start", description="باز کردن پنل مدیریت"),
                    BotCommand(command="alerts", description="اعلان‌ها: وضعیت / بی‌صدا"),
                    BotCommand(command="tenants", description="فهرست متنی ربات‌ها"),
                    BotCommand(command="logs", description="خطاهای اخیر (/logs BOT_ID)"),
                    BotCommand(command="create_bot", description="ساخت یا اتصال ربات اختصاصی"),
                    BotCommand(command="cancel", description="لغو عملیات جاری"),
                ], scope=BotCommandScopeChat(chat_id=admin_id))
            except Exception as exc:  # admin never started the bot: chat not found
                logging.getLogger("ziko.main").info(
                    "Admin command menu not set admin_id=%s error=%s", admin_id, type(exc).__name__)
        await dp.start_polling(
            bot,
            allowed_updates=["message", "callback_query", "business_connection", "business_message", "managed_bot"],
            close_bot_session=False,
        )
    except Exception:
        logger.critical("Master polling stopped with an unexpected error", exc_info=True)
        raise
    finally:
        for task in (status_task, alert_task, cache_task):
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        await manager.close()
        if alerts is not None:
            await alerts.drain()
        await registry.close()
        await bot.session.close()
        await db.close()
        pipeline.close()


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

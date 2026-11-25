"""ZIKO entry point: Telegram Bot API long polling only."""
from __future__ import annotations

import asyncio
import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

from app.config import load_settings
from app.handlers.admin import create_admin_router
from app.handlers.business import create_business_router
from app.handlers.tenants import create_tenant_router
from app.handlers.master import create_master_router
from app.logging_setup import AlertSink, CorrelationMiddleware, load_log_config, setup_logging
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
        dp.include_router(create_master_router(
            registry=registry, manager=manager, admin_ids=settings.admin_ids,
            log_dir=log_config.directory,
        ))
        # Onboarding must precede the admin router's catch-all, while its filters
        # deliberately let admin /start and non-onboarding /cancel reach that router.
        dp.include_router(create_tenant_router(
            dispatcher=dp, registry=registry, manager=manager, master_bot_id=master_bot_id,
            admin_ids=settings.admin_ids,
        ))
        dp.include_router(create_admin_router(db, settings))
        dp.include_router(create_business_router(db, settings))
        await manager.start_all()
        await bot.set_my_commands([
            BotCommand(command="start", description="باز کردن پنل مدیریت"),
            BotCommand(command="menu", description="نمایش منوی مدیریت"),
            BotCommand(command="create_bot", description="ساخت یا اتصال ربات اختصاصی"),
            BotCommand(command="connect_bot", description="اتصال ربات موجود با توکن"),
            BotCommand(command="cancel", description="لغو عملیات جاری"),
        ])
        await dp.start_polling(
            bot,
            allowed_updates=["message", "callback_query", "business_connection", "business_message", "managed_bot"],
            close_bot_session=False,
        )
    except Exception:
        logger.critical("Master polling stopped with an unexpected error", exc_info=True)
        raise
    finally:
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

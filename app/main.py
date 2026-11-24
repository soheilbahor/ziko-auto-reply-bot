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
from app.tenant_bots import TenantBotManager
from app.tenant_registry import TenantRegistry
from app.storage import Database


async def run() -> None:
    settings = load_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    os.umask(0o077)
    db = Database(settings.database_path)
    registry = TenantRegistry(settings.database_path.with_name("tenants.sqlite3"))
    bot = Bot(token=settings.bot_token)
    manager = TenantBotManager(settings=settings, registry=registry)
    try:
        await db.initialize()
        await registry.initialize()
        master_bot_id = (await bot.get_me()).id
        dp = Dispatcher(storage=MemoryStorage())
        dp.include_router(create_master_router(
            registry=registry, manager=manager, admin_ids=settings.admin_ids
        ))
        # Onboarding must precede the admin router's catch-all, while its filters
        # deliberately let admin /start and non-onboarding /cancel reach that router.
        dp.include_router(create_tenant_router(
            registry=registry, manager=manager, master_bot_id=master_bot_id,
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
    finally:
        await manager.close()
        await registry.close()
        await bot.session.close()
        await db.close()


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

"""ZIKO entry point: Telegram Bot API long polling only."""
from __future__ import annotations

import asyncio
import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

from app.config import load_settings
from app.storage import Database
from app.handlers.admin import create_admin_router
from app.handlers.business import create_business_router


async def run() -> None:
    settings = load_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    os.umask(0o077)
    db = Database(settings.database_path)
    await db.initialize()
    bot = Bot(token=settings.bot_token)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(create_admin_router(db, settings))
    dp.include_router(create_business_router(db, settings))
    try:
        await bot.set_my_commands([
            BotCommand(command="start", description="باز کردن پنل مدیریت"),
            BotCommand(command="menu", description="نمایش منوی مدیریت"),
            BotCommand(command="cancel", description="لغو عملیات جاری"),
        ])
        await dp.start_polling(
            bot,
            allowed_updates=["message", "callback_query", "business_connection", "business_message"],
            close_bot_session=False,
        )
    finally:
        await bot.session.close()
        await db.close()


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

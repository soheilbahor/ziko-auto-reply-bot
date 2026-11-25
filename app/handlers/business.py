"""aiogram Telegram Business update router."""
from __future__ import annotations

import logging
from typing import Any

from aiogram import Bot, Router
from aiogram.types import BusinessConnection, Message

from app.logging_setup import operation
from app.services.auto_reply import BusinessMessageEngine

logger = logging.getLogger("ziko.business")


def create_business_router(db: Any, settings: Any, *, service: Any = None) -> Router:
    engine = service or BusinessMessageEngine(db=db, sender=None, settings=settings)
    router = Router(name="business")

    @router.business_connection()
    async def on_business_connection(connection: BusinessConnection) -> None:
        with operation("BUSINESS_CONNECT", logger):
            await engine.handle_business_connection(connection)

    @router.business_message()
    async def on_business_message(message: Message, bot: Bot) -> None:
        if getattr(engine, "sender", None) is None:
            engine.sender = bot
        with operation("BUSINESS_MESSAGE", logger):
            await engine.handle_business_message(message)

    return router

"""aiogram Telegram Business update router."""
from __future__ import annotations

from typing import Any

from aiogram import Bot, Router
from aiogram.types import BusinessConnection, Message

from app.services.auto_reply import BusinessMessageEngine


def create_business_router(db: Any, settings: Any, *, service: Any = None) -> Router:
    engine = service or BusinessMessageEngine(db=db, sender=None, settings=settings)
    router = Router(name="business")

    @router.business_connection()
    async def on_business_connection(connection: BusinessConnection) -> None:
        await engine.handle_business_connection(connection)

    @router.business_message()
    async def on_business_message(message: Message, bot: Bot) -> None:
        if getattr(engine, "sender", None) is None:
            engine.sender = bot
        await engine.handle_business_message(message)

    return router

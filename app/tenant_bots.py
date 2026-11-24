"""Runtime supervisor for isolated child-bot pollers."""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import replace
from pathlib import Path
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

from app.handlers.admin import create_admin_router
from app.handlers.business import create_business_router
from app.storage import Database
from app.tenant_registry import Tenant, TenantRegistry

logger = logging.getLogger("ziko.tenant_bots")


class TenantBotManager:
    def __init__(self, *, settings: Any, registry: TenantRegistry) -> None:
        self.settings = settings
        self.registry = registry
        self._tasks: dict[int, asyncio.Task[None]] = {}

    @property
    def tenant_db_dir(self) -> Path:
        return self.settings.database_path.parent / "tenants"

    async def start_all(self) -> None:
        for tenant in await self.registry.list_tenants(reveal_tokens=True):
            if tenant.enabled:
                await self.start_tenant(tenant)

    async def start_tenant(self, tenant: Tenant) -> None:
        if not tenant.enabled:
            return
        current = self._tasks.get(tenant.user_id)
        if current is not None and not current.done():
            return
        task = asyncio.create_task(self._supervise_tenant(tenant), name=f"ziko-tenant-{tenant.user_id}")
        self._tasks[tenant.user_id] = task

    async def stop_tenant(self, user_id: int) -> None:
        task = self._tasks.pop(user_id, None)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _supervise_tenant(self, tenant: Tenant) -> None:
        delay = 2
        while True:
            try:
                await self._run_tenant_once(tenant)
                logger.warning("Child bot polling ended user_id=%s bot_id=%s; restarting", tenant.user_id, tenant.bot_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("Child bot polling failed user_id=%s bot_id=%s error=%s", tenant.user_id, tenant.bot_id, type(exc).__name__)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)

    async def _run_tenant_once(self, tenant: Tenant) -> None:
        bot = Bot(token=tenant.bot_token)
        db_path = self.tenant_db_dir / f"{tenant.user_id}.sqlite3"
        self.tenant_db_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.tenant_db_dir, 0o700)
        db = Database(db_path)
        tenant_settings = replace(
            self.settings,
            bot_token=tenant.bot_token,
            admin_ids=frozenset({tenant.user_id}),
            database_path=db_path,
            business_owner_id=tenant.user_id,
        )
        dp = Dispatcher(storage=MemoryStorage())
        try:
            await db.initialize()
            await bot.set_my_commands([
                BotCommand(command="start", description="پنل مدیریت ربات"),
                BotCommand(command="menu", description="نمایش منو"),
                BotCommand(command="cancel", description="لغو عملیات"),
            ])
            dp.include_router(create_admin_router(db, tenant_settings))
            dp.include_router(create_business_router(db, tenant_settings))
            logger.info("Child bot polling started user_id=%s bot_id=%s", tenant.user_id, tenant.bot_id)
            await dp.start_polling(
                bot,
                allowed_updates=["message", "callback_query", "business_connection", "business_message"],
                close_bot_session=False,
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
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    def running_count(self) -> int:
        return sum(not task.done() for task in self._tasks.values())

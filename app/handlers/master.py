"""Master-owner commands for platform-wide tenant visibility."""
from __future__ import annotations

from aiogram import Router
from aiogram.enums import ChatType
from aiogram.filters import BaseFilter, Command
from aiogram.types import Message

from app.tenant_registry import TenantRegistry
from app.tenant_bots import TenantBotManager


class MasterAdminFilter(BaseFilter):
    def __init__(self, admin_ids: frozenset[int]) -> None:
        self.admin_ids = admin_ids

    async def __call__(self, message: Message) -> bool:
        return bool(message.from_user and message.from_user.id in self.admin_ids
                    and message.chat.type == ChatType.PRIVATE)


def create_master_router(*, registry: TenantRegistry, manager: TenantBotManager, admin_ids: frozenset[int]) -> Router:
    router = Router(name="master_platform")
    router.message.filter(MasterAdminFilter(admin_ids))

    @router.message(Command("tenants"))
    async def tenants(message: Message) -> None:
        items = await registry.list_tenants()
        if not items:
            await message.answer("هنوز هیچ ربات زیرمجموعه‌ای ثبت نشده است.")
            return
        lines = [f"کاربران و ربات‌های ثبت‌شده: {len(items)}", ""]
        for item in items[:100]:
            lines.append(f"• {item.user_id} — @{item.username} — {'فعال' if item.enabled else 'غیرفعال'}")
        if len(items) > 100:
            lines.append(f"\nو {len(items) - 100} مورد دیگر...")
        await message.answer("\n".join(lines))

    @router.message(Command("tenant_disable"))
    async def tenant_disable(message: Message) -> None:
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) != 2 or not parts[1].isascii() or not parts[1].isdigit():
            await message.answer("استفاده: /tenant_disable شناسه_تلگرام_کاربر")
            return
        user_id = int(parts[1])
        if not await registry.set_enabled(user_id, False):
            await message.answer("کاربری با این شناسه ثبت نشده است.")
            return
        await manager.stop_tenant(user_id)
        await message.answer(f"ربات زیرمجموعه کاربر {user_id} غیرفعال شد.")

    @router.message(Command("tenant_enable"))
    async def tenant_enable(message: Message) -> None:
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) != 2 or not parts[1].isascii() or not parts[1].isdigit():
            await message.answer("استفاده: /tenant_enable شناسه_تلگرام_کاربر")
            return
        user_id = int(parts[1])
        if not await registry.set_enabled(user_id, True):
            await message.answer("کاربری با این شناسه ثبت نشده است.")
            return
        tenant = await registry.get_tenant(user_id, reveal_token=True)
        if tenant is not None:
            await manager.start_tenant(tenant)
        await message.answer(f"ربات زیرمجموعه کاربر {user_id} فعال شد.")

    return router

"""Private-chat onboarding for one child bot per Telegram user."""
from __future__ import annotations

import logging
import re

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup
from aiogram.types import Message

from app.tenant_bots import TenantBotManager
from app.tenant_registry import BotAlreadyRegistered, TenantRegistry, UserAlreadyHasBot

logger = logging.getLogger("ziko.tenants")
_TOKEN_RE = re.compile(r"^[0-9]{6,}:[A-Za-z0-9_-]{20,}$")


class BotSetup(StatesGroup):
    waiting_for_token = State()


def create_tenant_router(*, registry: TenantRegistry, manager: TenantBotManager,
                         master_bot_id: int, admin_ids: frozenset[int]) -> Router:
    router = Router(name="tenant_onboarding")
    router.message.filter(F.chat.type == ChatType.PRIVATE)

    @router.message(Command("start"))
    async def start(message: Message, state: FSMContext) -> None:
        await state.clear()
        user_id = message.from_user.id if message.from_user else 0
        if user_id in admin_ids:
            return
        tenant = await registry.get_tenant(user_id)
        if tenant:
            await message.answer(
                f"🤖 ربات اختصاصی شما فعال است: @{tenant.username}\n\n"
                "برای مدیریت، وارد ربات خودتان شوید.\n"
                "برای اتصال حساب Telegram Business، از تنظیمات Business تلگرام، ربات خودتان را انتخاب کنید."
            )
        else:
            await message.answer(
                "سلام! به پلتفرم ZIKO خوش آمدید.\n\n"
                "هر کاربر می‌تواند فقط یک ربات اختصاصی داشته باشد.\n"
                "برای شروع، ابتدا در @BotFather ربات خودتان را بسازید؛ سپس /create_bot را بزنید و توکن را ارسال کنید.\n\n"
                "توکن را فقط در همین گفت‌وگوی خصوصی بفرستید."
            )

    @router.message(Command("create_bot"))
    async def create_bot(message: Message, state: FSMContext) -> None:
        user_id = message.from_user.id if message.from_user else 0
        if user_id in admin_ids:
            return
        existing = await registry.get_tenant(user_id)
        if existing:
            await message.answer(f"شما قبلاً ربات @{existing.username} را ثبت کرده‌اید؛ هر کاربر فقط یک ربات می‌تواند داشته باشد.")
            return
        await state.set_state(BotSetup.waiting_for_token)
        await message.answer(
            "توکن رباتی را که در @BotFather ساخته‌اید ارسال کنید.\n"
            "توکن پس از اعتبارسنجی رمزنگاری و ذخیره می‌شود. برای انصراف /cancel را بفرستید."
        )

    @router.message(Command("cancel"))
    async def cancel(message: Message, state: FSMContext) -> None:
        if await state.get_state() == BotSetup.waiting_for_token.state:
            await state.clear()
            await message.answer("عملیات ساخت ربات لغو شد.")

    @router.message(BotSetup.waiting_for_token, F.text)
    async def receive_token(message: Message, state: FSMContext) -> None:
        user_id = message.from_user.id if message.from_user else 0
        token = (message.text or "").strip()
        # Best-effort removal of the credential from chat history before validation.
        token_message_deleted = True
        try:
            await message.delete()
        except Exception:
            token_message_deleted = False
        if not token_message_deleted:
            await message.answer("⚠️ پیام حاوی توکن حذف نشد؛ لطفاً پس از ثبت یا رد توکن، آن پیام را دستی پاک کنید.")
        if not _TOKEN_RE.fullmatch(token):
            await message.answer("قالب توکن معتبر نیست. توکن BotFather را بررسی کنید یا /cancel بزنید.")
            return
        probe = None
        try:
            probe = Bot(token=token)
            me = await probe.get_me()
        except Exception:
            await message.answer("تلگرام توکن را تأیید نکرد. توکن BotFather را بررسی و دوباره ارسال کنید.")
            return
        finally:
            if probe is not None:
                await probe.session.close()
        if not me.is_bot or me.id == master_bot_id:
            await message.answer("این توکن برای ثبت ربات زیرمجموعه قابل استفاده نیست.")
            return
        try:
            tenant = await registry.add_tenant(
                user_id=user_id, bot_id=me.id, username=me.username or str(me.id), token=token
            )
        except UserAlreadyHasBot:
            await message.answer("شما قبلاً یک ربات ثبت کرده‌اید؛ هر کاربر فقط یک ربات می‌تواند داشته باشد.")
            await state.clear()
            return
        except BotAlreadyRegistered:
            await message.answer("این ربات قبلاً توسط یک حساب دیگر ثبت شده است.")
            return
        await state.clear()
        await manager.start_tenant(tenant)
        logger.info("Tenant registered user_id=%s bot_id=%s", user_id, tenant.bot_id)
        token_warning = "" if token_message_deleted else "\n⚠️ پیام حاوی توکن حذف نشد؛ لطفاً آن را از تاریخچه گفتگو پاک کنید.\n"
        await message.answer(
            f"✅ ربات شما ثبت شد: @{tenant.username}\n\n"
            f"{token_warning}"
            "هسته ZIKO در حال راه‌اندازی ربات شماست.\n"
            "برای پاسخ‌گویی به پیام‌های حساب شخصی‌تان، تلگرام را باز کنید: Settings → Telegram Business → Chatbots، "
            "ربات خودتان را انتخاب و مجوز پاسخ‌دادن را فعال کنید.\n\n"
            "سپس وارد ربات اختصاصی‌تان شوید و پاسخ خودکار را تنظیم کنید."
        )

    @router.message(BotSetup.waiting_for_token)
    async def invalid_token_message(message: Message) -> None:
        await message.answer("لطفاً توکن را به‌صورت متن ارسال کنید؛ برای انصراف /cancel را بفرستید.")

    return router

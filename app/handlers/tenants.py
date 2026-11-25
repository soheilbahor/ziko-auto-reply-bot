"""Private-chat onboarding for user-owned and platform-admin child bots."""
from __future__ import annotations

import asyncio
import logging
import re
import secrets
from urllib.parse import quote

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
)

from app.tenant_bots import TenantBotManager
from app.tenant_registry import (
    BotAlreadyRegistered,
    TenantRegistry,
    TenantLimitReached,
    UserAlreadyHasBot,
)

logger = logging.getLogger("ziko.tenants")
_TOKEN_RE = re.compile(r"^[0-9]{6,}:[A-Za-z0-9_-]{20,}$")


class BotSetup(StatesGroup):
    waiting_for_token = State()


def create_tenant_router(*, dispatcher: Dispatcher, registry: TenantRegistry,
                         manager: TenantBotManager, master_bot_id: int,
                         admin_ids: frozenset[int]) -> Router:
    router = Router(name="tenant_onboarding")
    router.message.filter(F.chat.type == ChatType.PRIVATE)

    @router.message(Command("start"), ~F.from_user.id.in_(admin_ids))
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
                "از بخش ساخت ربات می‌توانید یک ربات اختصاصی را مستقیماً از طریق تلگرام ایجاد کنید؛ "
                "یا اگر از قبل ربات دارید، توکن آن را به‌صورت خصوصی متصل کنید.\n\n"
                "برای شروع دستور /create_bot را بزنید."
            )

    @router.message(Command("create_bot"))
    async def create_bot(message: Message, state: FSMContext) -> None:
        user_id = message.from_user.id if message.from_user else 0
        existing = await registry.get_tenant(user_id)
        if existing:
            await message.answer(
                f"شما قبلاً ربات @{existing.username} را ثبت کرده‌اید. "
                "در حال حاضر هر حساب می‌تواند یک ربات فعال در ZIKO داشته باشد."
            )
            return

        me = await message.bot.get_me()
        rows = []
        if getattr(me, "can_manage_bots", False) and me.username:
            # Telegram validates the suggested username during the official creation flow.
            suffix = secrets.token_hex(3)
            suggested_username = f"Ziko{user_id % 100000}{suffix}Bot"
            display_name = f"{(message.from_user.first_name if message.from_user else 'My')} ZIKO Bot"[:64]
            creation_url = (
                f"https://t.me/newbot/{me.username}/{suggested_username}"
                f"?name={quote(display_name, safe='')}"
            )
            rows.append([InlineKeyboardButton(text="🤖 ساخت خودکار ربات جدید", url=creation_url)])
        else:
            logger.warning("Managed bot creation is not enabled for master bot_id=%s", master_bot_id)

        rows.append([
            InlineKeyboardButton(
                text="🔗 اتصال رباتی که از قبل دارم",
                callback_data="tenant_connect_existing",
            )
        ])
        await message.answer(
            "روش موردنظرتان را انتخاب کنید:\n\n"
            "• ساخت خودکار: صفحه رسمی تلگرام باز می‌شود؛ نام و شناسه پیشنهادی را می‌توانید تغییر دهید و ساخت را تأیید کنید. "
            "پس از تأیید، ZIKO توکن را مستقیماً از تلگرام دریافت و ربات را راه‌اندازی می‌کند.\n"
            "• اتصال ربات موجود: توکن رباتی را که قبلاً ساخته‌اید، در همین گفت‌وگوی خصوصی ارسال می‌کنید.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )

    @router.callback_query(F.data == "tenant_connect_existing")
    async def connect_existing(callback: CallbackQuery, state: FSMContext) -> None:
        user_id = callback.from_user.id
        existing = await registry.get_tenant(user_id)
        if existing:
            await callback.answer("شما قبلاً یک ربات ثبت کرده‌اید.", show_alert=True)
            return
        await state.set_state(BotSetup.waiting_for_token)
        if isinstance(callback.message, Message):
            await callback.message.answer(
                "توکن ربات موجود را ارسال کنید. توکن پس از اعتبارسنجی رمزنگاری می‌شود. "
                "برای انصراف /cancel را بفرستید."
            )
        await callback.answer()

    @router.message(Command("connect_bot"))
    async def connect_bot(message: Message, state: FSMContext) -> None:
        user_id = message.from_user.id if message.from_user else 0
        existing = await registry.get_tenant(user_id)
        if existing:
            await message.answer(f"شما قبلاً ربات @{existing.username} را ثبت کرده‌اید.")
            return
        await state.set_state(BotSetup.waiting_for_token)
        await message.answer(
            "توکن ربات موجود را ارسال کنید. توکن پس از اعتبارسنجی رمزنگاری می‌شود. "
            "برای انصراف /cancel را بفرستید."
        )

    @router.message(Command("cancel"), BotSetup.waiting_for_token)
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
                user_id=user_id, bot_id=me.id, username=me.username or str(me.id),
                token=token, allow_multiple=user_id in admin_ids,
            )
        except UserAlreadyHasBot:
            await message.answer("شما قبلاً یک ربات ثبت کرده‌اید؛ هر کاربر فقط یک ربات می‌تواند داشته باشد.")
            await state.clear()
            return
        except TenantLimitReached:
            await message.answer(
                "ظرفیت ثبت ربات در حال حاضر تکمیل است یا محدودیت ثبت‌نام ساعتی شما به پایان رسیده است. "
                "کمی بعد دوباره تلاش کنید."
            )
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

    # ``update`` is a root-Dispatcher observer in aiogram; child Routers do not expose it.
    @dispatcher.update(F.managed_bot)
    async def managed_bot_updated(update: Update, bot: Bot) -> None:
        event = update.managed_bot
        if event is None:
            return
        owner_id = event.user.id
        child = event.bot_user
        if child.id == master_bot_id or not child.is_bot:
            logger.warning("Ignored invalid managed bot update bot_id=%s", child.id)
            return

        # Fetch the credential only from Telegram's managed-bot API. Never log or send it.
        token = None
        last_error_type = "UnknownError"
        for attempt in range(3):
            try:
                token = await bot.get_managed_bot_token(user_id=child.id)
                break
            except Exception as exc:
                last_error_type = type(exc).__name__
                if attempt < 2:
                    await asyncio.sleep(attempt + 1)
        if not token:
            logger.error(
                "Could not fetch managed bot token owner_id=%s bot_id=%s error=%s",
                owner_id, child.id, last_error_type,
            )
            try:
                await bot.send_message(
                    owner_id,
                    "تلگرام ربات را ساخت، اما دریافت امن توکن ناموفق بود. لطفاً با پشتیبانی ZIKO تماس بگیرید.",
                )
            except Exception:
                pass
            return

        try:
            existing_bot = await registry.get_tenant_by_bot_id(child.id)
            if existing_bot is not None:
                if existing_bot.user_id != owner_id:
                    logger.warning("Managed bot owner mismatch bot_id=%s", child.id)
                    return
                tenant = await registry.update_tenant_credentials(
                    user_id=owner_id, bot_id=child.id,
                    username=child.username or str(child.id), token=token,
                )
                if tenant is None:
                    return
                await manager.stop_tenant(child.id)
            else:
                tenant = await registry.add_tenant(
                    user_id=owner_id, bot_id=child.id,
                    username=child.username or str(child.id), token=token,
                )
        except UserAlreadyHasBot:
            try:
                await bot.send_message(
                    owner_id,
                    "ربات جدید در تلگرام ساخته شد، اما شما از قبل یک ربات ZIKO دارید. "
                    "برای جلوگیری از تداخل، ربات جدید به حساب ZIKO متصل نشد.",
                )
            except Exception:
                pass
            return
        except BotAlreadyRegistered:
            logger.warning("Managed bot already registered bot_id=%s", child.id)
            return
        except Exception as exc:
            logger.error(
                "Managed bot registration failed owner_id=%s bot_id=%s error=%s",
                owner_id, child.id, type(exc).__name__,
            )
            return

        await manager.start_tenant(tenant)
        logger.info("Managed bot registered owner_id=%s bot_id=%s", owner_id, child.id)
        try:
            await bot.send_message(
                owner_id,
                f"✅ ربات شما با موفقیت ساخته و به ZIKO متصل شد: @{tenant.username}\n\n"
                "توکن به‌صورت خودکار و رمزنگاری‌شده ذخیره شد؛ نیازی به ارسال یا کپی توکن نبود.\n"
                "برای تنظیم پاسخ‌گویی، وارد ربات اختصاصی‌تان شوید. برای استفاده از قابلیت Telegram Business، "
                "ربات را از Settings → Telegram Business → Chatbots متصل و مجوز پاسخ‌گویی را فعال کنید.",
            )
        except Exception:
            logger.warning("Could not notify managed bot owner owner_id=%s bot_id=%s", owner_id, child.id)

    return router

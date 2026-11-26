"""Private-chat onboarding for user-owned and platform-admin child bots."""
from __future__ import annotations

import asyncio
import logging
import re
import secrets
from typing import Any
from urllib.parse import quote

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramUnauthorizedError
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


CREATE_CB = "tenant_create"
CONNECT_CB = "tenant_connect_existing"


def _open_bot_row(username: str) -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(text=f"🤖 باز کردن @{username}", url=f"https://t.me/{username}")]


def _welcome_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✨ ساخت ربات جدید", callback_data=CREATE_CB)],
        [InlineKeyboardButton(text="🔗 اتصال رباتی که از قبل دارم", callback_data=CONNECT_CB)],
    ])


def connected_text(username: str, *, token_note: str = "") -> str:
    """Sent by the master bot right after a child bot is registered."""
    return (
        f"✅ <b>ربات شما وصل شد:</b> @{username}\n\n"
        f"{token_note}"
        "قدم بعدی، فقط دو کار:\n"
        "۱. ربات خودتان را باز کنید و /start بزنید؛ راهنمای کامل همان‌جاست.\n"
        "۲. در تلگرام: <b>Settings ← Telegram Business ← Chatbots</b>، ربات را انتخاب و "
        "«Reply to messages» را روشن کنید.\n\n"
        "توکن به‌صورت رمزنگاری‌شده ذخیره شد و هیچ‌جا نمایش داده نمی‌شود."
    )


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
        tenant, dead = await ownership(user_id)
        if tenant is not None and tenant.enabled:
            await message.answer(
                f"🤖 ربات اختصاصی شما فعال است: @{tenant.username}\n\n"
                "برای مدیریت و دیدن راهنما، ربات خودتان را باز کنید و /start بزنید.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[_open_bot_row(tenant.username)]),
            )
        elif tenant is not None:
            await refuse_if_blocked(message.answer, user_id)
        else:
            gone = (f"ربات قبلی شما (@{dead[0].username}) در تلگرام حذف شده یا توکنش باطل شده است؛ "
                    "می‌توانید یک ربات تازه بسازید یا همان را دوباره وصل کنید.\n\n") if dead else ""
            await message.answer(
                gone +
                "سلام! به <b>ZIKO</b> خوش آمدید 👋\n\n"
                "با ZIKO یک ربات اختصاصی می‌سازید که وقتی در دسترس نیستید، "
                "از طرف حساب تلگرام شما به پیام‌ها پاسخ می‌دهد.\n\n"
                "یکی را انتخاب کنید:",
                parse_mode="HTML", reply_markup=_welcome_keyboard(),
            )

    async def _token_rejected(token: str) -> bool:
        """True only when Telegram itself rejects the token (bot deleted or token revoked).
        A network problem is "unknown", never "dead": the bot is then not replaced."""
        if not token:
            return False
        probe = None
        try:
            probe = Bot(token=token)
            await probe.get_me()
            return False
        except TelegramUnauthorizedError:
            return True
        except Exception:
            return False
        finally:
            if probe is not None:
                await probe.session.close()

    async def ownership(user_id: int) -> tuple[Any, list[Any]]:
        """(blocking bot or None, dead bots of this user that may be replaced).

        Blocking: an enabled bot, or a disabled bot whose token Telegram still accepts
        (disabled on purpose, e.g. by an admin). Dead: disabled and rejected by Telegram.
        """
        bots = await registry.list_tenants(user_id=user_id)
        active = [t for t in bots if t.enabled]
        if active:
            return active[0], []
        blocking, dead = None, []
        for t in bots:
            try:
                full = await registry.get_tenant_by_bot_id(t.bot_id, reveal_token=True)
            except Exception:
                full = None
            if full is not None and await _token_rejected(full.bot_token):
                dead.append(t)
            elif blocking is None:
                blocking = t
        return blocking, dead

    async def refuse_if_blocked(answer: Any, user_id: int) -> bool:
        """Tell the user why a new bot is not possible. True when refused."""
        if user_id in admin_ids:
            return False
        blocking, _dead = await ownership(user_id)
        if blocking is None:
            return False
        if blocking.enabled:
            await answer(
                f"شما قبلاً ربات @{blocking.username} را ثبت کرده‌اید. "
                "در حال حاضر هر حساب می‌تواند یک ربات فعال در ZIKO داشته باشد.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[_open_bot_row(blocking.username)]),
            )
        else:
            await answer(
                f"ربات @{blocking.username} شما در ZIKO غیرفعال شده است، اما هنوز در تلگرام وجود دارد.\n"
                "برای فعال‌سازی دوباره با پشتیبانی ZIKO تماس بگیرید."
            )
        return True

    async def offer_creation(message: Message, user: Any) -> None:
        """The creation choices. Shared by /create_bot and the ✨ button."""
        user_id = user.id if user else 0
        if await refuse_if_blocked(message.answer, user_id):
            return

        me = await message.bot.get_me()
        rows = []
        if getattr(me, "can_manage_bots", False) and me.username:
            # Telegram validates the suggested username during the official creation flow.
            suffix = secrets.token_hex(3)
            suggested_username = f"Ziko{user_id % 100000}{suffix}Bot"
            display_name = f"{(user.first_name if user else 'My')} ZIKO Bot"[:64]
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
                callback_data=CONNECT_CB,
            )
        ])
        await message.answer(
            "روش موردنظرتان را انتخاب کنید:\n\n"
            "• ساخت خودکار: صفحه رسمی تلگرام باز می‌شود؛ نام و شناسه پیشنهادی را می‌توانید تغییر دهید و ساخت را تأیید کنید. "
            "پس از تأیید، ZIKO توکن را مستقیماً از تلگرام دریافت و ربات را راه‌اندازی می‌کند.\n"
            "• اتصال ربات موجود: توکن رباتی را که قبلاً ساخته‌اید، در همین گفت‌وگوی خصوصی ارسال می‌کنید.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )

    @router.message(Command("create_bot"))
    async def create_bot(message: Message, state: FSMContext) -> None:
        await offer_creation(message, message.from_user)

    @router.callback_query(F.data == CREATE_CB)
    async def create_bot_button(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        if isinstance(callback.message, Message):
            await offer_creation(callback.message, callback.from_user)
        await callback.answer()

    @router.callback_query(F.data == CONNECT_CB)
    async def connect_existing(callback: CallbackQuery, state: FSMContext) -> None:
        user_id = callback.from_user.id
        if isinstance(callback.message, Message) and await refuse_if_blocked(callback.message.answer, user_id):
            await callback.answer()
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
        if await refuse_if_blocked(message.answer, user_id):
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
        except Exception as exc:
            logger.warning("Token probe rejected by Telegram error=%s", type(exc).__name__)
            await message.answer("تلگرام توکن را تأیید نکرد. توکن BotFather را بررسی و دوباره ارسال کنید.")
            return
        finally:
            if probe is not None:
                await probe.session.close()
        if not me.is_bot or me.id == master_bot_id:
            await message.answer("این توکن برای ثبت ربات زیرمجموعه قابل استفاده نیست.")
            return
        _blocking, dead = await ownership(user_id)
        dead_ids = [t.bot_id for t in dead]
        try:
            same = await registry.get_tenant_by_bot_id(me.id)
            if same is not None and same.user_id == user_id:
                # The same bot again (e.g. its token was revoked and a new one issued).
                await manager.stop_tenant(me.id)
                tenant = await registry.update_tenant_credentials(
                    user_id=user_id, bot_id=me.id, username=me.username or str(me.id), token=token,
                    enable=me.id in dead_ids,
                )
                if tenant is None:
                    raise BotAlreadyRegistered()
            else:
                tenant = await registry.add_tenant(
                    user_id=user_id, bot_id=me.id, username=me.username or str(me.id),
                    token=token, allow_multiple=user_id in admin_ids, replace_bot_ids=dead_ids,
                )
            if dead_ids:
                logger.info("Replaced dead child bots user_id=%s old=%s new=%s", user_id, dead_ids, me.id)
        except UserAlreadyHasBot:
            logger.warning("Tenant registration refused reason=UserAlreadyHasBot")
            await message.answer("شما قبلاً یک ربات ثبت کرده‌اید؛ هر کاربر فقط یک ربات می‌تواند داشته باشد.")
            await state.clear()
            return
        except TenantLimitReached:
            logger.warning("Tenant registration refused reason=TenantLimitReached")
            await message.answer(
                "ظرفیت ثبت ربات در حال حاضر تکمیل است یا محدودیت ثبت‌نام ساعتی شما به پایان رسیده است. "
                "کمی بعد دوباره تلاش کنید."
            )
            await state.clear()
            return
        except BotAlreadyRegistered:
            logger.warning("Tenant registration refused reason=BotAlreadyRegistered")
            await message.answer("این ربات قبلاً توسط یک حساب دیگر ثبت شده است.")
            return
        await state.clear()
        await manager.start_tenant(tenant)
        logger.info("Tenant registered user_id=%s bot_id=%s", user_id, tenant.bot_id)
        token_warning = "" if token_message_deleted else "⚠️ پیام حاوی توکن حذف نشد؛ لطفاً آن را از تاریخچه گفتگو پاک کنید.\n\n"
        await message.answer(
            connected_text(tenant.username, token_note=token_warning), parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[_open_bot_row(tenant.username)]),
        )

    @router.message(BotSetup.waiting_for_token)
    async def invalid_token_message(message: Message) -> None:
        await message.answer("لطفاً توکن را به‌صورت متن ارسال کنید؛ برای انصراف /cancel را بفرستید.")

    async def _restart_previous_poller(bot_id: int) -> None:
        """After a failed credential update, put the still-valid previous poller back.

        Without this, a refused update leaves an enabled bot stopped until a manual fix.
        """
        previous = await registry.get_tenant_by_bot_id(bot_id, reveal_token=True)
        if previous is not None and previous.enabled:
            await manager.start_tenant(previous)

    async def _log_managed_bot_arrival(handler, event: Update, data: dict) -> Any:
        # Runs for every update before routing: proves whether Telegram's managed_bot
        # update reached this process, independently of the handler below.
        managed = getattr(event, "managed_bot", None)
        if managed is not None:
            logger.info(
                "Managed bot update received owner_id=%s bot_id=%s",
                managed.user.id, managed.bot_user.id,
            )
        return await handler(event, data)

    dispatcher.update.outer_middleware(_log_managed_bot_arrival)

    # Must be the typed ``managed_bot`` observer. A handler on ``dispatcher.update`` never
    # runs: the Dispatcher registers its own catch-all ``update`` handler first, and an
    # observer stops at the first matching handler, so every managed_bot update was logged
    # "is not handled" and the new bot was never registered (bug in 1.4.x-1.5.1).
    @router.managed_bot()
    async def managed_bot_updated(event: Any, bot: Bot) -> None:
        if event is None or getattr(event, "bot_user", None) is None:
            logger.warning("Managed bot handler received an event without a bot_user")
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
                # Stop the old token first. Otherwise its pending Unauthorized
                # response can race with the credential update and disable the new token.
                await manager.stop_tenant(child.id)
                tenant = await registry.update_tenant_credentials(
                    user_id=owner_id, bot_id=child.id,
                    username=child.username or str(child.id), token=token,
                )
                if tenant is None:
                    logger.warning("Managed bot credential update refused bot_id=%s", child.id)
                    await _restart_previous_poller(child.id)
                    return
            else:
                _blocking, dead = await ownership(owner_id)
                tenant = await registry.add_tenant(
                    user_id=owner_id, bot_id=child.id,
                    username=child.username or str(child.id), token=token,
                    allow_multiple=owner_id in admin_ids, replace_bot_ids=[t.bot_id for t in dead],
                )
                if dead:
                    logger.info("Replaced dead child bots owner_id=%s old=%s new=%s",
                                owner_id, [t.bot_id for t in dead], child.id)
        except UserAlreadyHasBot:
            # Was silent before 1.5.2: the new bot then looked "lost" in the logs.
            logger.warning("Managed bot not registered reason=UserAlreadyHasBot owner_id=%s bot_id=%s",
                           owner_id, child.id)
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
            await _restart_previous_poller(child.id)
            return

        logger.info("Managed bot registered owner_id=%s bot_id=%s", owner_id, child.id)
        await manager.start_tenant(tenant)
        try:
            await bot.send_message(
                owner_id, connected_text(tenant.username), parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[_open_bot_row(tenant.username)]),
            )
        except Exception:
            logger.warning("Could not notify managed bot owner owner_id=%s bot_id=%s", owner_id, child.id)

    return router

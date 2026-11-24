"""Persian admin interface: commands, callbacks and FSM text editing.

Security model:
  * A router-level filter checks `from_user.id` against `settings.admin_ids` for
    EVERY message and callback query, including FSM-state handlers.
  * Non-admins are silently ignored: no reply, no menu, no hint.
  * Only private chats with the bot are served; business messages are never handled.
  * No handler reveals the token, settings values, connection IDs or message contents.
"""

from __future__ import annotations

import asyncio
import html
import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from aiogram import F, Router
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import BaseFilter, Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from app.keyboards.admin import (
    Action,
    AdminCB,
    back_keyboard,
    cancel_edit_keyboard,
    edit_status_keyboard,
    main_menu_keyboard,
    persian_title,
)

if TYPE_CHECKING:  # Module 1 types; annotation-only, no runtime import.
    from app.config import Settings
    from app.database import AppState, Database, Status

logger = logging.getLogger(__name__)

MAX_REPLY_LENGTH = 4096  # Telegram sendMessage text limit
PREVIEW_LIMIT = 800  # keeps escaped previews safely under the 4096 message limit
CARD_PREVIEW_LIMIT = 300

# Optional statistics: only these keys of `db.get_statistics()` are rendered.
_STAT_LABELS: dict[str, str] = {
    "total_inbound": "پیام‌های دریافتی پردازش‌شده",
    "replies_sent": "پاسخ‌های ارسال‌شده",
    "replies_failed": "ارسال‌های ناموفق",
    "current_session_replies": "پاسخ‌های نشست فعلی",
}


class EditReply(StatesGroup):
    """FSM for editing a status reply text."""

    waiting_text = State()
    confirming = State()


# --------------------------------------------------------------------------- #
# Security
# --------------------------------------------------------------------------- #
class AdminFilter(BaseFilter):
    """Passes only for numeric IDs listed in ADMIN_IDS, in a private bot chat."""

    def __init__(self, admin_ids: frozenset[int]) -> None:
        self._admin_ids = admin_ids

    async def __call__(self, event: Message | CallbackQuery) -> bool:
        user = event.from_user
        if user is None or user.is_bot or user.id not in self._admin_ids:
            # Log only the numeric ID, never any content.
            logger.warning(
                "Unauthorized admin interface access ignored user_id=%s",
                user.id if user else None,
            )
            return False

        if isinstance(event, Message):
            if getattr(event, "business_connection_id", None):
                return False  # business messages are never admin input
            return event.chat.type == ChatType.PRIVATE

        message = event.message
        return isinstance(message, Message) and message.chat.type == ChatType.PRIVATE


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
def _esc(value: object) -> str:
    return html.escape(str(value), quote=False)


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit].rstrip() + "…", True


def _utf16_length(text: str) -> int:
    """Length in UTF-16 code units (conservative match for Telegram's counting)."""
    return len(text.encode("utf-16-le")) // 2


def validate_reply_text(raw: str | None) -> tuple[str | None, str | None]:
    """Return (clean_text, error_message). Exactly one of them is None."""
    if raw is None:
        return None, "⚠️ فقط پیام متنی پذیرفته می‌شود. لطفاً متن پاسخ را به‌صورت متن ارسال کنید."
    text = raw.strip()
    if not text:
        return None, "⚠️ متن پاسخ نمی‌تواند خالی باشد."
    if _utf16_length(text) > MAX_REPLY_LENGTH:
        return None, (
            f"⚠️ متن بیش از حد طولانی است. حداکثر {MAX_REPLY_LENGTH} نویسه مجاز است."
        )
    return text, None


def _find_status(statuses: list[Status], key: str | None) -> Status | None:
    return next((s for s in statuses if s.key == key), None)


def render_main_card(state: AppState, statuses: list[Status]) -> str:
    active = _find_status(statuses, state.active_status_key)
    power = "🟢 فعال" if state.enabled else "🔴 غیرفعال"
    lines = ["🤖 <b>دستیار شخصی ZIKO</b>", "", f"پاسخ خودکار: {power}"]

    if active is None:
        lines.append("وضعیت فعلی: انتخاب نشده")
    else:
        paused = "" if state.enabled else " (متوقف)"
        lines.append(
            f"وضعیت فعلی: {_esc(active.emoji)} {_esc(persian_title(active.title))}{paused}"
        )
        preview, _ = _truncate(active.reply_text, CARD_PREVIEW_LIMIT)
        lines += ["", "متن پاسخ:", f"<blockquote>{_esc(preview)}</blockquote>"]

    lines += ["", f"آخرین تغییر: <code>{_esc(state.updated_at)}</code>"]
    if not state.enabled:
        lines.append("\nبرای شروع، یکی از وضعیت‌ها را انتخاب کنید.")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Router factory
# --------------------------------------------------------------------------- #
def create_admin_router(db: Database, settings: Settings) -> Router:
    """Build the admin router. Requires an FSM storage on the Dispatcher."""
    router = Router(name="admin")
    admin_filter = AdminFilter(settings.admin_ids)
    router.message.filter(admin_filter)
    router.callback_query.filter(admin_filter)

    # Serializes check-then-act state transitions triggered from this interface.
    transition_lock = asyncio.Lock()

    # ------------------------------ rendering ------------------------------ #
    async def build_menu() -> tuple[str, InlineKeyboardMarkup]:
        state = await db.get_app_state()
        statuses = await db.list_statuses()
        return render_main_card(state, statuses), main_menu_keyboard(statuses, state)

    async def edit_callback_message(
        callback: CallbackQuery, text: str, markup: InlineKeyboardMarkup | None
    ) -> None:
        message = callback.message
        if not isinstance(message, Message):
            return
        try:
            await message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
        except TelegramBadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                raise

    async def show_menu_in_callback(callback: CallbackQuery) -> None:
        text, markup = await build_menu()
        await edit_callback_message(callback, text, markup)

    async def send_menu(message: Message, prefix: str = "") -> None:
        text, markup = await build_menu()
        await message.answer(
            f"{prefix}{text}", parse_mode=ParseMode.HTML, reply_markup=markup
        )

    def log_action(user_id: int, action: str, key: str = "") -> None:
        logger.info("Admin action user_id=%s action=%s key=%s", user_id, action, key)

    # ------------------------------- commands ------------------------------ #
    @router.message(Command("start", "menu"))
    async def cmd_start(message: Message, state: FSMContext) -> None:
        await state.clear()
        await send_menu(message)

    @router.message(Command("cancel"))
    async def cmd_cancel(message: Message, state: FSMContext) -> None:
        await state.clear()
        await send_menu(message, prefix="❌ عملیات لغو شد.\n\n")

    # ------------------------- menu / navigation --------------------------- #
    @router.callback_query(AdminCB.filter(F.action.in_({Action.MENU, Action.CANCEL})))
    async def cb_menu(
        callback: CallbackQuery, callback_data: AdminCB, state: FSMContext
    ) -> None:
        await state.clear()
        await show_menu_in_callback(callback)
        await callback.answer("عملیات لغو شد." if callback_data.action == Action.CANCEL else None)

    # ----------------------------- status select --------------------------- #
    @router.callback_query(AdminCB.filter(F.action == Action.STATUS))
    async def cb_status(
        callback: CallbackQuery, callback_data: AdminCB, state: FSMContext
    ) -> None:
        await state.clear()
        async with transition_lock:
            status = await db.get_status(callback_data.key)
            if status is None:
                await callback.answer("وضعیت نامعتبر است.", show_alert=True)
                return

            current = await db.get_app_state()
            if current.enabled and current.active_status_key == status.key:
                # Selecting the already-active status must not reset the session.
                toast = "این وضعیت از قبل فعال است؛ نشست تغییری نکرد."
            else:
                await db.activate_status(status.key)
                fresh = await db.get_app_state()
                if not fresh.enabled:
                    # Selecting a status is an explicit activation by the owner.
                    await db.enable_auto_reply()
                toast = f"{status.emoji} وضعیت فعال شد؛ نشست جدید آغاز شد."
                log_action(callback.from_user.id, "activate_status", status.key)

        await show_menu_in_callback(callback)
        await callback.answer(toast)

    # ---------------------------- enable / disable ------------------------- #
    @router.callback_query(AdminCB.filter(F.action == Action.ENABLE))
    async def cb_enable(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        async with transition_lock:
            current = await db.get_app_state()
            if current.active_status_key is None:
                await callback.answer(
                    "ابتدا یکی از وضعیت‌ها را انتخاب کنید.", show_alert=True
                )
                return
            if current.enabled:
                toast = "پاسخ خودکار از قبل فعال است."
            else:
                await db.enable_auto_reply()  # starts a new session
                toast = "پاسخ خودکار فعال شد؛ نشست جدید آغاز شد."
                log_action(callback.from_user.id, "enable")
        await show_menu_in_callback(callback)
        await callback.answer(toast)

    @router.callback_query(AdminCB.filter(F.action == Action.DISABLE))
    async def cb_disable(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        async with transition_lock:
            await db.disable_auto_reply()
            log_action(callback.from_user.id, "disable")
        await show_menu_in_callback(callback)
        await callback.answer("پاسخ خودکار غیرفعال شد.")

    # ------------------------------ text editing --------------------------- #
    @router.callback_query(AdminCB.filter(F.action == Action.EDIT_MENU))
    async def cb_edit_menu(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        statuses = await db.list_statuses()
        await edit_callback_message(
            callback,
            "✏️ <b>ویرایش متن پاسخ</b>\n\nمتن کدام وضعیت را می‌خواهید تغییر دهید؟",
            edit_status_keyboard(statuses),
        )
        await callback.answer()

    @router.callback_query(AdminCB.filter(F.action == Action.EDIT))
    async def cb_edit_pick(
        callback: CallbackQuery, callback_data: AdminCB, state: FSMContext
    ) -> None:
        status = await db.get_status(callback_data.key)
        if status is None:
            await state.clear()
            await callback.answer("وضعیت نامعتبر است.", show_alert=True)
            return

        await state.clear()
        await state.set_state(EditReply.waiting_text)
        await state.update_data(status_key=status.key)

        current, truncated = _truncate(status.reply_text, PREVIEW_LIMIT)
        note = "\n<i>(پیش‌نمایش کوتاه‌شده)</i>" if truncated else ""
        await edit_callback_message(
            callback,
            f"✏️ <b>{_esc(status.emoji)} {_esc(persian_title(status.title))}</b>\n\n"
            f"متن فعلی:\n<blockquote>{_esc(current)}</blockquote>{note}\n\n"
            f"متن جدید را ارسال کنید (حداکثر {MAX_REPLY_LENGTH} نویسه).\n"
            "برای انصراف /cancel را بزنید.",
            cancel_edit_keyboard(),
        )
        await callback.answer()

    @router.message(EditReply.waiting_text)
    async def msg_new_text(message: Message, state: FSMContext) -> None:
        clean, error = validate_reply_text(message.text)
        if error is not None or clean is None:
            await message.answer(
                error or "⚠️ متن نامعتبر است.",
                reply_markup=cancel_edit_keyboard(),
            )
            return

        # The selected key is captured when the admin presses the status button.
        # Read it from this user's FSM context; never infer it from current app state.
        data: dict[str, Any] = await state.get_data()
        key = data.get("status_key")
        status = await db.get_status(str(key or ""))
        if status is None:
            await state.clear()
            await message.answer("⚠️ وضعیت نامعتبر است. دوباره تلاش کنید.")
            await send_menu(message)
            return

        # Persist the validated text immediately. Editing reply text must not create
        # a new delivery session or alter any existing claim/delivery record.
        await db.save_status_text(status.key, clean)
        await state.clear()
        log_action(message.from_user.id, "save_status_text", status.key)
        await send_menu(message, prefix="✅ متن پاسخ ذخیره شد.\n\n")

    # ---------------------------- connection / stats ----------------------- #
    @router.callback_query(AdminCB.filter(F.action == Action.CONNECTION))
    async def cb_connection(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        connection = await db.get_business_connection()
        app_state = await db.get_app_state()

        if connection is None:
            text = (
                "🔌 <b>وضعیت اتصال</b>\n\n🔴 ربات هنوز به حساب شما متصل نشده است.\n\n"
                "از مسیر «تنظیمات ← Chat Automation» ربات را انتخاب کنید و "
                "دسترسی پاسخ‌دادن به پیام‌ها را بدهید."
            )
        else:
            lines = [
                "🔌 <b>وضعیت اتصال</b>",
                "",
                f"اتصال: {'🟢 فعال' if connection.is_enabled else '🔴 غیرفعال'}",
                f"مجوز پاسخ‌دادن: {'🟢 دارد' if connection.can_reply else '🔴 ندارد'}",
            ]
            if not connection.is_enabled or not connection.can_reply:
                lines.append(
                    "\n⚠️ تا زمانی که اتصال فعال نباشد و مجوز پاسخ‌دادن داده نشود، "
                    "هیچ پاسخی ارسال نمی‌شود. تنظیمات Chat Automation را بررسی کنید."
                )
            elif not app_state.enabled:
                lines.append("\nاتصال سالم است؛ پاسخ خودکار فعلاً خاموش است.")
            text = "\n".join(lines)

        await edit_callback_message(callback, text, back_keyboard())
        await callback.answer()

    @router.callback_query(AdminCB.filter(F.action == Action.STATS))
    async def cb_stats(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        text = "📊 <b>آمار</b>\n\nآمار در دسترس نیست."
        getter = getattr(db, "get_statistics", None)  # optional database capability
        if getter is not None:
            try:
                stats: Mapping[str, int] = await getter()
                rows = [
                    f"{label}: <b>{_esc(stats[key])}</b>"
                    for key, label in _STAT_LABELS.items()
                    if key in stats
                ]
                if rows:
                    text = "📊 <b>آمار</b>\n\n" + "\n".join(rows)
            except Exception:
                logger.exception("Failed to load statistics")
        await edit_callback_message(callback, text, back_keyboard())
        await callback.answer()

    # ------------------------------- fallbacks ----------------------------- #
    # Registered last so specific handlers (including FSM ones) win.
    @router.callback_query(AdminCB.filter())
    async def cb_stale(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await callback.answer("این دکمه منقضی شده است.", show_alert=True)
        await show_menu_in_callback(callback)

    @router.message(StateFilter(None))
    async def msg_fallback(message: Message) -> None:
        await send_menu(message)

    return router

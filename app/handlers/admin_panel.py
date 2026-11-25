"""Phase 4 admin panel: custom statuses, reply steps, policies, owner settings, exceptions.

Registered from ``create_admin_router``, so the router-level ``AdminFilter`` already
guards every handler in this module (callbacks and FSM-state messages included).

Security and data rules:
  * Contact message text is never read or stored here. Only admin input is handled.
  * Sticker ``file_id`` values are taken from the admin's own private messages.
  * Logs record the action name and status key only, never message content.
  * Destructive actions (soft delete, replacing all steps) are explicit in the UI.
"""

from __future__ import annotations

import html
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from aiogram import F, Router
from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from app.keyboards.admin import (
    Action,
    AdminCB,
    cancel_edit_keyboard,
    delete_confirm_keyboard,
    exceptions_keyboard,
    owner_keyboard,
    panel_menu_keyboard,
    persian_title,
    status_list_keyboard,
    status_policy_keyboard,
    status_view_keyboard,
    steps_wizard_keyboard,
)
from app.storage import InvalidReplyTextError, StorageError

if TYPE_CHECKING:
    from app.models import OwnerSettings, Status
    from app.storage import Database

logger = logging.getLogger(__name__)

MAX_STEPS = 10  # matches storage._MAX_STEPS
MAX_TEXT_STEP = 4096  # Telegram sendMessage text limit
MAX_EMOJI_STEP = 16
TITLE_MAX = 40
ALLOWED_INTERVALS = (0, 30, 60, 300, 900)
PREVIEW_CHARS = 60

# Purposes of the generic text-input state. Stored in FSM data under "purpose".
P_LABEL_TITLE = "label_title"
P_LABEL_EMOJI = "label_emoji"
P_NEW_TITLE = "new_title"
P_NEW_EMOJI = "new_emoji"
P_STEPS = "steps"
P_HOURS = "hours"
P_OWNER_TZ = "owner_tz"
P_OWNER_NAME = "owner_name"
P_EXC_ADD = "exc_add"

_HOURS_RE = re.compile(r"^\s*(\d{1,2})\s*[-–]\s*(\d{1,2})\s*$")
_CHAT_ID_RE = re.compile(r"^-?\d{1,20}$")
_OFF_WORDS = frozenset({"off", "خاموش", "همیشه"})


class PanelInput(StatesGroup):
    """One FSM state for every text or sticker input of this panel."""

    waiting = State()


@dataclass(frozen=True)
class PanelDeps:
    """Helpers owned by the admin router, injected to avoid a circular import."""

    db: Database
    edit: Callable[[CallbackQuery, str, InlineKeyboardMarkup | None], Awaitable[None]]
    answer: Callable[..., Awaitable[None]]
    log_action: Callable[[int, str, str], None]


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def _esc(value: object) -> str:
    return html.escape(str(value), quote=False)


def _step_label(kind: str, content: str) -> str:
    if kind == "sticker":
        return "🎞 استیکر"
    if kind == "emoji":
        return f"😀 ایموجی: {_esc(content)}"
    preview = content if len(content) <= PREVIEW_CHARS else content[:PREVIEW_CHARS] + "…"
    return f"💬 متن: {_esc(preview)}"


def _hours_label(status: Status, owner: OwnerSettings) -> str:
    if status.active_from_hour is None or status.active_to_hour is None:
        return "همیشه"
    return (
        f"{status.active_from_hour}:00 تا {status.active_to_hour}:00 "
        f"({_esc(owner.timezone)})"
    )


async def _render_status(db: Database, key: str) -> tuple[str, InlineKeyboardMarkup] | None:
    status = await db.get_status(key)
    if status is None:
        return None
    owner = await db.get_owner_settings()
    steps = await db.list_reply_steps(key)
    mode = "ریپلای (پاسخ به پیام مخاطب)" if status.reply_mode == "reply" else "ساده (بدون ریپلای)"
    interval = (
        "بدون محدودیت"
        if status.min_interval_seconds == 0
        else f"{status.min_interval_seconds} ثانیه"
    )
    step_lines = "\n".join(
        f"{i}. {_step_label(s.kind, s.content)}" for i, s in enumerate(steps, start=1)
    ) or "— بدون بخش —"
    text = (
        f"{_esc(status.emoji)} <b>{_esc(persian_title(status.title))}</b>\n\n"
        f"وضعیت: {'فعال' if status.is_active else 'غیرفعال'}\n"
        f"حالت پاسخ: {mode}\n"
        f"فاصله حداقلی پاسخ: {interval}\n"
        f"بازه ساعتی: {_hours_label(status, owner)}\n\n"
        f"<b>بخش‌های پاسخ</b>\n{step_lines}"
    )
    return text, status_view_keyboard(key)


async def _render_policy(db: Database, key: str) -> tuple[str, InlineKeyboardMarkup] | None:
    status = await db.get_status(key)
    if status is None:
        return None
    owner = await db.get_owner_settings()
    text = (
        f"⚙️ <b>سیاست پاسخ: {_esc(status.emoji)} {_esc(persian_title(status.title))}</b>\n\n"
        f"حالت: {'ریپلای' if status.reply_mode == 'reply' else 'ساده'}\n"
        f"فاصله حداقلی: {status.min_interval_seconds} ثانیه\n"
        f"بازه ساعتی: {_hours_label(status, owner)}\n"
        f"وضعیت: {'فعال' if status.is_active else 'غیرفعال'}"
    )
    return text, status_policy_keyboard(status, ALLOWED_INTERVALS)


async def _render_owner(db: Database) -> tuple[str, InlineKeyboardMarkup]:
    owner = await db.get_owner_settings()
    lang = "فارسی" if owner.language == "fa" else "English"
    text = (
        "👤 <b>تنظیمات مالک</b>\n\n"
        f"منطقه زمانی: {_esc(owner.timezone)}\n"
        f"زبان: {lang}\n"
        f"نام مالک: {_esc(owner.owner_name) or '—'}"
    )
    return text, owner_keyboard()


async def _render_exceptions(db: Database) -> tuple[str, InlineKeyboardMarkup]:
    ids = await db.list_reply_exceptions()
    body = "\n".join(f"• <code>{chat_id}</code>" for chat_id in ids) or "— فهرست خالی است —"
    text = (
        "🚫 <b>استثناهای پاسخ خودکار</b>\n\n"
        "به شناسه‌های عددی زیر پاسخ خودکار داده نمی‌شود:\n\n"
        f"{body}"
    )
    return text, exceptions_keyboard(ids)


# --------------------------------------------------------------------------- #
# Input helpers
# --------------------------------------------------------------------------- #
def _looks_like_emoji(text: str) -> bool:
    """Short text without letters or digits is treated as an emoji step."""
    return 0 < len(text) <= MAX_EMOJI_STEP and not any(ch.isalnum() for ch in text)


def _parse_hours(raw: str) -> tuple[int | None, int | None] | None:
    """Return (from, to), (None, None) for 'always', or None if the input is invalid."""
    value = raw.strip().lower()
    if value in _OFF_WORDS:
        return None, None
    match = _HOURS_RE.match(raw)
    if match is None:
        return None
    start, end = int(match.group(1)), int(match.group(2))
    if not (0 <= start <= 23 and 0 <= end <= 23) or start == end:
        return None
    return start, end


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #
def register_panel_handlers(router: Router, deps: PanelDeps) -> None:
    """Attach all panel handlers. Must be called before the router's catch-all handlers."""
    db = deps.db

    async def show(callback: CallbackQuery, rendered: tuple[str, InlineKeyboardMarkup] | None) -> None:
        if rendered is None:
            await deps.answer(callback, "این وضعیت دیگر وجود ندارد.", show_alert=True)
            return
        await deps.edit(callback, rendered[0], rendered[1])
        await deps.answer(callback)

    async def begin_input(
        callback: CallbackQuery, state: FSMContext, purpose: str, prompt: str, **extra: Any
    ) -> None:
        await state.clear()
        await state.set_state(PanelInput.waiting)
        await state.update_data(purpose=purpose, **extra)
        await deps.edit(callback, prompt, cancel_edit_keyboard())
        await deps.answer(callback)

    def parse_key(callback_data: AdminCB) -> str | None:
        return callback_data.key or None

    # ------------------------------ panel menu ----------------------------- #
    @router.callback_query(AdminCB.filter(F.action == Action.PANEL))
    async def cb_panel(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await deps.edit(callback, "🧰 <b>امکانات پیشرفته</b>", panel_menu_keyboard())
        await deps.answer(callback)

    # ------------------------------ statuses ------------------------------- #
    @router.callback_query(AdminCB.filter(F.action == Action.ST_LIST))
    async def cb_status_list(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        statuses = await db.list_statuses()
        await deps.edit(
            callback,
            "🧩 <b>مدیریت وضعیت‌ها</b>\n\nوضعیت موردنظر را انتخاب کنید.",
            status_list_keyboard(statuses),
        )
        await deps.answer(callback)

    @router.callback_query(AdminCB.filter(F.action == Action.ST_VIEW))
    async def cb_status_view(callback: CallbackQuery, callback_data: AdminCB, state: FSMContext) -> None:
        await state.clear()
        key = parse_key(callback_data)
        await show(callback, await _render_status(db, key) if key else None)

    @router.callback_query(AdminCB.filter(F.action == Action.ST_TITLE))
    async def cb_status_title(callback: CallbackQuery, callback_data: AdminCB, state: FSMContext) -> None:
        key = parse_key(callback_data)
        if not key or await db.get_status(key) is None:
            await deps.answer(callback, "این وضعیت دیگر وجود ندارد.", show_alert=True)
            return
        await begin_input(
            callback, state, P_LABEL_TITLE,
            f"عنوان جدید را بفرستید (حداکثر {TITLE_MAX} نویسه).\nبرای انصراف /cancel.",
            status_key=key,
        )

    @router.callback_query(AdminCB.filter(F.action == Action.ST_EMOJI))
    async def cb_status_emoji(callback: CallbackQuery, callback_data: AdminCB, state: FSMContext) -> None:
        key = parse_key(callback_data)
        if not key or await db.get_status(key) is None:
            await deps.answer(callback, "این وضعیت دیگر وجود ندارد.", show_alert=True)
            return
        await begin_input(
            callback, state, P_LABEL_EMOJI,
            f"ایموجی جدید را بفرستید (۱ تا {MAX_EMOJI_STEP} نویسه).",
            status_key=key,
        )

    @router.callback_query(AdminCB.filter(F.action == Action.ST_STEPS))
    async def cb_status_steps(callback: CallbackQuery, callback_data: AdminCB, state: FSMContext) -> None:
        key = parse_key(callback_data)
        if not key or await db.get_status(key) is None:
            await deps.answer(callback, "این وضعیت دیگر وجود ندارد.", show_alert=True)
            return
        await begin_input(
            callback, state, P_STEPS,
            "🧩 <b>بخش‌های جدید را بفرستید</b>\n\n"
            "هر پیام یک بخش می‌شود: متن، ایموجی کوتاه یا استیکر.\n"
            f"حداکثر {MAX_STEPS} بخش. با «پایان» ذخیره کنید.\n\n"
            "<b>توجه:</b> بخش‌های فعلی با این فهرست جایگزین می‌شوند.",
            status_key=key, steps=[],
        )

    @router.callback_query(AdminCB.filter(F.action == Action.ST_POLICY))
    async def cb_status_policy(callback: CallbackQuery, callback_data: AdminCB, state: FSMContext) -> None:
        await state.clear()
        key = parse_key(callback_data)
        await show(callback, await _render_policy(db, key) if key else None)

    @router.callback_query(AdminCB.filter(F.action == Action.ST_SET))
    async def cb_status_set(callback: CallbackQuery, callback_data: AdminCB, state: FSMContext) -> None:
        await state.clear()
        parts = (callback_data.key or "").split("|")
        if len(parts) != 3:
            await deps.answer(callback, "دکمه نامعتبر است.", show_alert=True)
            return
        key, field, value = parts
        status = await db.get_status(key)
        if status is None:
            await deps.answer(callback, "این وضعیت دیگر وجود ندارد.", show_alert=True)
            return
        mode, interval, active = status.reply_mode, status.min_interval_seconds, status.is_active
        if field == "mode" and value in ("plain", "reply"):
            mode = value
        elif field == "int" and value.isdigit() and int(value) in ALLOWED_INTERVALS:
            interval = int(value)
        elif field == "act" and value in ("0", "1"):
            active = value == "1"
        else:
            await deps.answer(callback, "دکمه نامعتبر است.", show_alert=True)
            return
        try:
            await db.update_status_settings(
                key,
                title=status.title,
                emoji=status.emoji,
                reply_mode=mode,
                min_interval_seconds=interval,
                active_from_hour=status.active_from_hour,
                active_to_hour=status.active_to_hour,
                is_active=active,
            )
        except (InvalidReplyTextError, StorageError) as exc:
            logger.warning("Status policy rejected action=status_policy key=%s error=%s",
                           key, type(exc).__name__)
            await deps.answer(callback, "تنظیم ذخیره نشد.", show_alert=True)
            return
        deps.log_action(callback.from_user.id, "status_policy", key)
        await show(callback, await _render_policy(db, key))

    @router.callback_query(AdminCB.filter(F.action == Action.ST_HOURS))
    async def cb_status_hours(callback: CallbackQuery, callback_data: AdminCB, state: FSMContext) -> None:
        key = parse_key(callback_data)
        if not key or await db.get_status(key) is None:
            await deps.answer(callback, "این وضعیت دیگر وجود ندارد.", show_alert=True)
            return
        await begin_input(
            callback, state, P_HOURS,
            "🕒 بازه ساعتی را به شکل <code>8-20</code> بفرستید.\n"
            "ساعت‌ها بر اساس منطقه زمانی مالک هستند.\n"
            "برای فعال بودن همیشه، «خاموش» را بفرستید.",
            status_key=key,
        )

    @router.callback_query(AdminCB.filter(F.action == Action.ST_DEL))
    async def cb_status_delete_ask(callback: CallbackQuery, callback_data: AdminCB, state: FSMContext) -> None:
        await state.clear()
        key = parse_key(callback_data)
        status = await db.get_status(key) if key else None
        if status is None:
            await deps.answer(callback, "این وضعیت دیگر وجود ندارد.", show_alert=True)
            return
        await deps.edit(
            callback,
            f"🗑 وضعیت <b>{_esc(status.emoji)} {_esc(persian_title(status.title))}</b> حذف شود؟\n\n"
            "این حذف نرم است و از فهرست پنهان می‌شود. متن‌ها در پایگاه داده باقی می‌مانند.",
            delete_confirm_keyboard(key),
        )
        await deps.answer(callback)

    @router.callback_query(AdminCB.filter(F.action == Action.ST_DEL_OK))
    async def cb_status_delete(callback: CallbackQuery, callback_data: AdminCB, state: FSMContext) -> None:
        await state.clear()
        key = parse_key(callback_data)
        if not key:
            await deps.answer(callback, "دکمه نامعتبر است.", show_alert=True)
            return
        try:
            await db.delete_status(key)
        except StorageError as exc:
            logger.warning("Status delete refused key=%s error=%s", key, type(exc).__name__)
            await deps.answer(
                callback,
                "این وضعیت در حال استفاده است. ابتدا وضعیت دیگری را فعال کنید.",
                show_alert=True,
            )
            return
        deps.log_action(callback.from_user.id, "status_delete", key)
        statuses = await db.list_statuses()
        await deps.edit(
            callback,
            "🧩 <b>مدیریت وضعیت‌ها</b>\n\nوضعیت حذف شد.",
            status_list_keyboard(statuses),
        )
        await deps.answer(callback)

    @router.callback_query(AdminCB.filter(F.action == Action.ST_NEW))
    async def cb_status_new(callback: CallbackQuery, state: FSMContext) -> None:
        await begin_input(
            callback, state, P_NEW_TITLE,
            f"➕ <b>وضعیت جدید</b>\n\nعنوان را بفرستید (حداکثر {TITLE_MAX} نویسه).",
        )

    @router.callback_query(AdminCB.filter(F.action == Action.STEPS_DONE))
    async def cb_steps_done(callback: CallbackQuery, state: FSMContext) -> None:
        data = await state.get_data()
        if data.get("purpose") != P_STEPS:
            await deps.answer(callback, "این مرحله دیگر فعال نیست.", show_alert=True)
            return
        steps: list[list[str]] = list(data.get("steps", []))
        if not steps:
            await deps.answer(callback, "حداقل یک بخش لازم است.", show_alert=True)
            return
        pairs = [(kind, content) for kind, content in steps]
        key = str(data.get("status_key") or "")
        try:
            if key:
                await db.replace_reply_steps(key, pairs)
                action = "status_steps"
            else:
                created = await db.create_status(
                    title=str(data.get("title", "")),
                    emoji=str(data.get("emoji", "")),
                    steps=pairs,
                )
                key = created.key
                action = "status_create"
        except (InvalidReplyTextError, StorageError) as exc:
            logger.warning("Status steps rejected error=%s", type(exc).__name__)
            await deps.answer(callback, "بخش‌ها معتبر نیستند. دوباره بفرستید.", show_alert=True)
            return
        await state.clear()
        deps.log_action(callback.from_user.id, action, key)
        await show(callback, await _render_status(db, key))

    # -------------------------------- owner -------------------------------- #
    @router.callback_query(AdminCB.filter(F.action == Action.OWNER))
    async def cb_owner(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        text, markup = await _render_owner(db)
        await deps.edit(callback, text, markup)
        await deps.answer(callback)

    @router.callback_query(AdminCB.filter(F.action == Action.OWNER_TZ))
    async def cb_owner_tz(callback: CallbackQuery, state: FSMContext) -> None:
        await begin_input(
            callback, state, P_OWNER_TZ,
            "🕒 منطقه زمانی را بفرستید، مثلاً <code>Asia/Tehran</code>.",
        )

    @router.callback_query(AdminCB.filter(F.action == Action.OWNER_NAME))
    async def cb_owner_name(callback: CallbackQuery, state: FSMContext) -> None:
        await begin_input(callback, state, P_OWNER_NAME, "👤 نام مالک را بفرستید (حداکثر ۶۴ نویسه).")

    @router.callback_query(AdminCB.filter(F.action == Action.OWNER_LANG))
    async def cb_owner_lang(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        owner = await db.get_owner_settings()
        new_lang = "en" if owner.language == "fa" else "fa"
        try:
            await db.save_owner_settings(
                timezone=owner.timezone, language=new_lang, owner_name=owner.owner_name,
            )
        except (InvalidReplyTextError, StorageError) as exc:
            logger.warning("Owner language change rejected error=%s", type(exc).__name__)
            await deps.answer(callback, "زبان ذخیره نشد.", show_alert=True)
            return
        deps.log_action(callback.from_user.id, "owner_language", new_lang)
        text, markup = await _render_owner(db)
        await deps.edit(callback, text, markup)
        await deps.answer(callback)

    # ----------------------------- exceptions ------------------------------ #
    @router.callback_query(AdminCB.filter(F.action == Action.EXC))
    async def cb_exceptions(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        text, markup = await _render_exceptions(db)
        await deps.edit(callback, text, markup)
        await deps.answer(callback)

    @router.callback_query(AdminCB.filter(F.action == Action.EXC_ADD))
    async def cb_exception_add(callback: CallbackQuery, state: FSMContext) -> None:
        await begin_input(
            callback, state, P_EXC_ADD,
            "🚫 شناسه عددی تلگرام مخاطب را بفرستید.\nبرای انصراف /cancel.",
        )

    @router.callback_query(AdminCB.filter(F.action == Action.EXC_DEL))
    async def cb_exception_del(callback: CallbackQuery, callback_data: AdminCB, state: FSMContext) -> None:
        await state.clear()
        raw = callback_data.key or ""
        if not _CHAT_ID_RE.match(raw):
            await deps.answer(callback, "دکمه نامعتبر است.", show_alert=True)
            return
        removed = await db.remove_reply_exception(int(raw))
        if not removed:
            await deps.answer(callback, "این شناسه در فهرست نیست.", show_alert=True)
        else:
            deps.log_action(callback.from_user.id, "exception_remove", "")
        text, markup = await _render_exceptions(db)
        await deps.edit(callback, text, markup)
        await deps.answer(callback)

    # ----------------------------- text input ------------------------------ #
    @router.message(PanelInput.waiting)
    async def msg_panel_input(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        purpose = data.get("purpose")
        if purpose == P_STEPS:
            await _collect_step(message, state, data)
            return

        raw = message.text
        if raw is None or not raw.strip():
            await message.answer(
                "⚠️ فقط متن پذیرفته می‌شود. دوباره بفرستید یا /cancel را بزنید.",
                reply_markup=cancel_edit_keyboard(),
            )
            return
        text = raw.strip()
        try:
            await _handle_text(message, state, data, purpose, text)
        except (InvalidReplyTextError, StorageError) as exc:
            logger.warning("Panel input rejected purpose=%s error=%s", purpose, type(exc).__name__)
            await message.answer(
                "⚠️ مقدار نامعتبر است. دوباره بفرستید یا /cancel را بزنید.",
                reply_markup=cancel_edit_keyboard(),
            )

    async def _collect_step(message: Message, state: FSMContext, data: dict[str, Any]) -> None:
        steps: list[list[str]] = list(data.get("steps", []))
        if len(steps) >= MAX_STEPS:
            await message.answer(
                f"⚠️ حداکثر {MAX_STEPS} بخش مجاز است. با «پایان» ذخیره کنید.",
                reply_markup=steps_wizard_keyboard(len(steps)),
            )
            return
        if message.sticker is not None:
            kind, content = "sticker", message.sticker.file_id
        elif message.text is not None and message.text.strip():
            text = message.text.strip()
            if _looks_like_emoji(text):
                kind, content = "emoji", text
            elif len(text) <= MAX_TEXT_STEP:
                kind, content = "text", text
            else:
                await message.answer(
                    f"⚠️ متن هر بخش حداکثر {MAX_TEXT_STEP} نویسه است.",
                    reply_markup=steps_wizard_keyboard(len(steps)),
                )
                return
        else:
            await message.answer(
                "⚠️ فقط متن، ایموجی کوتاه یا استیکر پذیرفته می‌شود.",
                reply_markup=steps_wizard_keyboard(len(steps)),
            )
            return
        steps.append([kind, content])
        await state.update_data(steps=steps)
        label = {"text": "متن", "emoji": "ایموجی", "sticker": "استیکر"}[kind]
        await message.answer(
            f"✅ بخش {len(steps)} ({label}) ثبت شد. بخش بعدی را بفرستید یا «پایان» را بزنید.",
            reply_markup=steps_wizard_keyboard(len(steps)),
        )

    async def _handle_text(
        message: Message, state: FSMContext, data: dict[str, Any], purpose: str | None, text: str
    ) -> None:
        key = str(data.get("status_key") or "")

        if purpose == P_LABEL_TITLE or purpose == P_LABEL_EMOJI:
            status = await db.get_status(key)
            if status is None:
                await state.clear()
                await message.answer("⚠️ این وضعیت دیگر وجود ندارد.")
                return
            title = text if purpose == P_LABEL_TITLE else status.title
            emoji = text if purpose == P_LABEL_EMOJI else status.emoji
            if purpose == P_LABEL_TITLE and len(text) > TITLE_MAX:
                raise InvalidReplyTextError("title too long")
            await db.update_status_settings(
                key,
                title=title,
                emoji=emoji,
                reply_mode=status.reply_mode,
                min_interval_seconds=status.min_interval_seconds,
                active_from_hour=status.active_from_hour,
                active_to_hour=status.active_to_hour,
                is_active=status.is_active,
            )
            await state.clear()
            deps.log_action(message.from_user.id, "status_label", key)
            rendered = await _render_status(db, key)
            if rendered is not None:
                await message.answer(rendered[0], parse_mode=ParseMode.HTML, reply_markup=rendered[1])
            return

        if purpose == P_NEW_TITLE:
            if len(text) > TITLE_MAX:
                raise InvalidReplyTextError("title too long")
            await state.update_data(purpose=P_NEW_EMOJI, title=text)
            await message.answer(f"ایموجی وضعیت را بفرستید (۱ تا {MAX_EMOJI_STEP} نویسه).",
                                 reply_markup=cancel_edit_keyboard())
            return

        if purpose == P_NEW_EMOJI:
            if len(text) > MAX_EMOJI_STEP:
                raise InvalidReplyTextError("emoji too long")
            await state.update_data(purpose=P_STEPS, emoji=text, status_key="", steps=[])
            await message.answer(
                "🧩 بخش‌های پاسخ را بفرستید؛ هر پیام یک بخش (متن، ایموجی کوتاه یا استیکر).\n"
                f"حداکثر {MAX_STEPS} بخش. سپس «پایان» را بزنید.",
                reply_markup=cancel_edit_keyboard(),
            )
            return

        if purpose == P_HOURS:
            parsed = _parse_hours(text)
            if parsed is None:
                raise InvalidReplyTextError("bad hours")
            status = await db.get_status(key)
            if status is None:
                await state.clear()
                await message.answer("⚠️ این وضعیت دیگر وجود ندارد.")
                return
            await db.update_status_settings(
                key,
                title=status.title,
                emoji=status.emoji,
                reply_mode=status.reply_mode,
                min_interval_seconds=status.min_interval_seconds,
                active_from_hour=parsed[0],
                active_to_hour=parsed[1],
                is_active=status.is_active,
            )
            await state.clear()
            deps.log_action(message.from_user.id, "status_hours", key)
            rendered = await _render_policy(db, key)
            if rendered is not None:
                await message.answer(rendered[0], parse_mode=ParseMode.HTML, reply_markup=rendered[1])
            return

        if purpose == P_OWNER_TZ or purpose == P_OWNER_NAME:
            owner = await db.get_owner_settings()
            zone = text if purpose == P_OWNER_TZ else owner.timezone
            name = text if purpose == P_OWNER_NAME else owner.owner_name
            await db.save_owner_settings(timezone=zone, language=owner.language, owner_name=name)
            await state.clear()
            deps.log_action(message.from_user.id, "owner_settings", "")
            owner_text, markup = await _render_owner(db)
            await message.answer(owner_text, parse_mode=ParseMode.HTML, reply_markup=markup)
            return

        if purpose == P_EXC_ADD:
            if not _CHAT_ID_RE.match(text):
                raise InvalidReplyTextError("chat id must be numeric")
            await db.add_reply_exception(int(text))
            await state.clear()
            deps.log_action(message.from_user.id, "exception_add", "")
            exc_text, markup = await _render_exceptions(db)
            await message.answer(exc_text, parse_mode=ParseMode.HTML, reply_markup=markup)
            return

        await state.clear()
        await message.answer("⚠️ این مرحله دیگر فعال نیست.")

"""Owner panel: add a sticker to the reply that is sent right now (Telegram, Persian).

Flow: main menu "🎭 استیکر پاسخ" -> owner sends a sticker -> it is appended as the last
step of the ACTIVE status, so the existing reply engine sends it after the text (it
already sends sticker steps through the Business connection). "🗑" removes the sticker
steps again; the text steps are never touched.

Registered from create_admin_router, so the router-level AdminFilter (owner only,
private chat) guards every handler here, including the FSM-state message handler.
Only the sticker ``file_id`` from the owner's own message is stored; nothing is logged
except the action name.
"""
from __future__ import annotations

import html
import logging
from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from app.keyboards.admin import Action, AdminCB, _btn
from app.storage import InvalidReplyTextError, StorageError

logger = logging.getLogger(__name__)

STICKER = "stk"          # open: ask for a sticker
STICKER_CLEAR = "stkx"   # remove the sticker steps of the active status
MAX_STEPS = 10           # matches storage._MAX_STEPS


class StickerInput(StatesGroup):
    waiting = State()


def waiting_keyboard(has_sticker: bool) -> InlineKeyboardMarkup:
    rows = []
    if has_sticker:
        rows.append([_btn("🗑 حذف استیکر از پاسخ", STICKER_CLEAR)])
    rows.append([_btn("❌ انصراف", Action.MENU)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def done_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn("🎭 افزودن استیکر دیگر", STICKER), _btn("🗑 حذف استیکرها", STICKER_CLEAR)],
        [_btn("🏠 منوی اصلی", Action.MENU)],
    ])


def _as_pairs(steps: Any) -> list[tuple[str, str]]:
    return [(s.kind, s.content) for s in steps]


async def _effective_steps(db: Any, status: Any) -> list[tuple[str, str]]:
    """The parts the engine really sends. A text-only reply has no reply_steps rows: the
    engine falls back to ``reply_text``. Before this fix, adding a sticker to such a reply
    stored [sticker] alone, so the customer got the sticker and lost the text."""
    steps = _as_pairs(await db.list_reply_steps(status.key))
    if not steps and (status.reply_text or "").strip():
        steps = [("text", status.reply_text)]
    return steps


def register_sticker_handlers(router: Router, deps: Any) -> None:
    db = deps.db

    async def active_status() -> Any:
        state = await db.get_app_state()
        if not state.active_status_key:
            return None
        return await db.get_status(state.active_status_key)

    @router.callback_query(AdminCB.filter(F.action == STICKER))
    async def cb_sticker(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        status = await active_status()
        if status is None:
            await deps.answer(callback, "اول از منوی اصلی یک پاسخ را انتخاب و فعال کنید.", show_alert=True)
            return
        steps = await db.list_reply_steps(status.key)
        stickers = sum(1 for s in steps if s.kind == "sticker")
        if len(steps) >= MAX_STEPS:
            await deps.answer(callback, f"این پاسخ حداکثر {MAX_STEPS} بخش دارد؛ اول یک استیکر را حذف کنید.",
                              show_alert=True)
            await deps.edit(callback, "🎭 <b>استیکر پاسخ</b>\n\nاین پاسخ پر است.", waiting_keyboard(stickers > 0))
            return
        await state.set_state(StickerInput.waiting)
        await state.update_data(status_key=status.key)
        await deps.edit(
            callback,
            "🎭 <b>استیکر پاسخ</b>\n\n"
            f"پاسخ فعال: {html.escape(status.emoji)} {html.escape(status.title)}\n"
            f"استیکرهای فعلی این پاسخ: {stickers}\n\n"
            "یک <b>استیکر</b> برای همین ربات بفرستید. بعد از متن پاسخ، برای مخاطب ارسال می‌شود.",
            waiting_keyboard(stickers > 0),
        )
        await deps.answer(callback)

    @router.callback_query(AdminCB.filter(F.action == STICKER_CLEAR))
    async def cb_clear(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        status = await active_status()
        if status is None:
            await deps.answer(callback, "پاسخ فعالی وجود ندارد.", show_alert=True)
            return
        steps = await _effective_steps(db, status)
        kept = [s for s in steps if s[0] != "sticker"]
        if len(kept) == len(steps):
            await deps.answer(callback, "این پاسخ استیکری ندارد.", show_alert=True)
            return
        if not kept:
            await deps.answer(callback, "این پاسخ فقط استیکر دارد؛ از «✏️ ویرایش متن پاسخ‌ها» متن اضافه کنید.",
                              show_alert=True)
            return
        try:
            await db.replace_reply_steps(status.key, kept)
        except (InvalidReplyTextError, StorageError):
            await deps.answer(callback, "ذخیره ناموفق بود؛ دوباره امتحان کنید.", show_alert=True)
            return
        deps.log_action(callback.from_user.id, "sticker_clear", status.key)
        await deps.edit(callback, "🗑 استیکرها از پاسخ حذف شدند.", done_keyboard())
        await deps.answer(callback, "حذف شد")

    @router.message(StickerInput.waiting, F.sticker)
    async def on_sticker(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        key = str(data.get("status_key") or "")
        await state.clear()
        status = await db.get_status(key) if key else None
        if status is None:
            await message.answer("این پاسخ دیگر وجود ندارد. از منوی اصلی دوباره انتخاب کنید.")
            return
        steps = await _effective_steps(db, status)
        if len(steps) >= MAX_STEPS:
            await message.answer(f"این پاسخ حداکثر {MAX_STEPS} بخش دارد.", reply_markup=done_keyboard())
            return
        try:
            await db.replace_reply_steps(key, [*steps, ("sticker", message.sticker.file_id)])
        except (InvalidReplyTextError, StorageError):
            logger.warning("Sticker step rejected status=%s", key)
            await message.answer("این استیکر ذخیره نشد؛ استیکر دیگری بفرستید.", reply_markup=done_keyboard())
            return
        deps.log_action(message.from_user.id if message.from_user else 0, "sticker_add", key)
        await message.answer(
            f"✅ استیکر به پاسخ «{html.escape(status.title)}» اضافه شد.\n"
            "از این به بعد، بعد از متن پاسخ برای مخاطب ارسال می‌شود.",
            parse_mode="HTML", reply_markup=done_keyboard(),
        )

    @router.message(StickerInput.waiting)
    async def on_other(message: Message) -> None:
        await message.answer("لطفاً یک استیکر بفرستید، یا «❌ انصراف» را بزنید.",
                             reply_markup=waiting_keyboard(False))

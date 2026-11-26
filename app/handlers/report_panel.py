"""Owner panel "📊 گزارش روزانه": on/off, send hour, and a preview of the report now.

Registered from create_admin_router (router-level AdminFilter: owner only, private chat).
Actions: rep (screen) · rept (toggle) · reph <hour> · repn (preview now, does not count
as today's report).
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from aiogram import F, Router
from aiogram.enums import ParseMode
from aiogram.types import CallbackQuery, InlineKeyboardMarkup

from app import daily_report
from app.keyboards.admin import Action, AdminCB, _btn

MAIN, TOGGLE, HOUR, PREVIEW = "rep", "rept", "reph", "repn"


def view(settings: dict[str, Any]) -> tuple[str, InlineKeyboardMarkup]:
    on, hour = settings["enabled"], settings["hour"]
    text = (
        "📊 <b>گزارش روزانه</b>\n\n"
        f"وضعیت: <b>{'روشن' if on else 'خاموش'}</b>\n"
        f"ساعت ارسال: <b>{daily_report.fa(hour)}:۰۰</b> (به وقت شما)\n\n"
        "هر روز یک پیام برایتان می\u200cفرستم: چند پیام از مشتری\u200cها رسید، به چند نفر پاسخ "
        "خودکار رفت، و آیا ارسالی ناموفق بود. متن پیام\u200cها و نام مشتری\u200cها در گزارش نمی\u200cآید."
    )
    rows = [
        [_btn("🔴 خاموش کن" if on else "🟢 روشن کن", TOGGLE)],
        [_btn(("✅ " if h == hour else "") + daily_report.fa(h), HOUR, str(h)) for h in daily_report.HOURS],
        [_btn("👁 پیش\u200cنمایش گزارش همین حالا", PREVIEW)],
        [_btn("🔙 بازگشت به منو", Action.MENU)],
    ]
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def register_report_handlers(router: Router, deps: Any) -> None:
    db = deps.db

    async def show(callback: CallbackQuery, note: str | None = None) -> None:
        text, markup = view(await db.get_report_settings())
        await deps.edit(callback, text, markup)
        await deps.answer(callback, note)

    @router.callback_query(AdminCB.filter(F.action == MAIN))
    async def cb_main(callback: CallbackQuery) -> None:
        await show(callback)

    @router.callback_query(AdminCB.filter(F.action == TOGGLE))
    async def cb_toggle(callback: CallbackQuery) -> None:
        on = not (await db.get_report_settings())["enabled"]
        await db.set_report_settings(enabled=on)
        deps.log_action(callback.from_user.id, "daily_report", "on" if on else "off")
        await show(callback, "روشن شد" if on else "خاموش شد")

    @router.callback_query(AdminCB.filter(F.action == HOUR))
    async def cb_hour(callback: CallbackQuery, callback_data: AdminCB) -> None:
        raw = callback_data.key or ""
        if not raw.isdigit() or int(raw) not in daily_report.HOURS:
            await deps.answer(callback, "دکمه نامعتبر است.", show_alert=True)
            return
        await db.set_report_settings(hour=int(raw))
        deps.log_action(callback.from_user.id, "daily_report_hour", raw)
        await show(callback, "ذخیره شد")

    @router.callback_query(AdminCB.filter(F.action == PREVIEW))
    async def cb_preview(callback: CallbackQuery) -> None:
        text = await daily_report.compose(db, datetime.now(UTC), preview=True)
        if callback.message is not None:
            await callback.message.answer(text, parse_mode=ParseMode.HTML)
        await deps.answer(callback)

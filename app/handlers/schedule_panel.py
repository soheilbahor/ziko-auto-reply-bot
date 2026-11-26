"""Owner panel "⏰ زمان‌بندی و تایپ": typing pause, reply mode, weekly hours, holidays.

Registered from create_admin_router, so the router-level AdminFilter (owner only,
private chat) guards every handler here, including the FSM-state message handler.
Only the owner's own settings are stored; logs record the action name only.

Callback actions (AdminCB, key after the action):
  sch                  main screen
  scht  <seconds>      typing pause (0 = off)
  schm  <mode>         always | outside | inside
  schd                 weekly hours list         schday <weekday>  edit one day (text input)
  schp                 preset: Sat-Wed 9-17, Thu 9-13, Fri off
  schh                 holidays list             schha             add (text input)
  schhx <start|end>    remove one holiday (ISO dates)
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any

from aiogram import F, Router
from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from app import schedule
from app.keyboards.admin import Action, AdminCB, _btn
from app.storage import InvalidReplyTextError, StorageError

logger = logging.getLogger(__name__)

MAIN, TYPING, MODE = "sch", "scht", "schm"
DAYS, DAY, PRESET = "schd", "schday", "schp"
HOLIDAYS, HOLIDAY_ADD, HOLIDAY_DEL = "schh", "schha", "schhx"
_FA = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


class ScheduleInput(StatesGroup):
    waiting = State()


def _typing_label(seconds: int) -> str:
    return "خاموش" if not seconds else f"{seconds} ثانیه".translate(_FA)


def main_view(b: schedule.Behaviour, tz: str, now: Any) -> tuple[str, InlineKeyboardMarkup]:
    days = ("\n" + "\n".join(f"   {schedule.WEEKDAYS[d]}: {schedule.format_hours(b.hours.get(d))}" for d in range(7))
            if b.has_hours else "تنظیم نشده")
    today = schedule.local_now(now, tz).date()
    upcoming = [h for h in b.holidays if h[1] >= today]
    holiday_line = (f"{len(upcoming)} مورد (نزدیک\u200cترین: {schedule.format_jalali(upcoming[0][0])})".translate(_FA)
                    if upcoming else "ندارد")
    text = (
        "⏰ <b>زمان\u200cبندی و رفتار پاسخ</b>\n\n"
        f"⌨️ «در حال نوشتن…» قبل از پاسخ: <b>{_typing_label(b.typing_delay)}</b>\n"
        f"📅 ارسال پاسخ خودکار: <b>{schedule.MODE_LABELS[b.mode]}</b>\n"
        f"🕘 ساعت کاری: {days}\n"
        f"🏖 تعطیلات: {holiday_line}\n\n"
        f"{schedule.explain(b, now, tz)}\n\n"
        "<i>«در حال نوشتن…» پاسخ را طبیعی\u200cتر می\u200cکند. «فقط خارج از ساعت کاری» یعنی وقتی "
        "خودتان سر کار هستید ربات ساکت می\u200cماند و بعد از ساعت کاری و در تعطیلات پاسخ می\u200cدهد.</i>"
    )
    typing_row = [_btn(("✅ " if b.typing_delay == s else "") + ("⌨️ خاموش" if not s else f"{s}s"), TYPING, str(s))
                  for s in schedule.TYPING_CHOICES]
    mode_rows = [[_btn(("✅ " if b.mode == m else "") + schedule.MODE_LABELS[m], MODE, m)] for m in schedule.MODES]
    rows = [typing_row, *mode_rows,
            [_btn("🕘 ساعت کاری روزها", DAYS), _btn("🏖 تعطیلات", HOLIDAYS)],
            [_btn("🔙 بازگشت به منو", Action.MENU)]]
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def days_view(b: schedule.Behaviour) -> tuple[str, InlineKeyboardMarkup]:
    rows = [[_btn(f"{schedule.WEEKDAYS[d]}: {schedule.format_hours(b.hours.get(d))}", DAY, str(d))] for d in range(7)]
    rows.append([_btn("⚡ شنبه تا چهارشنبه ۹–۱۷، پنج\u200cشنبه ۹–۱۳، جمعه تعطیل", PRESET)])
    rows.append([_btn("🔙 بازگشت", MAIN)])
    text = ("🕘 <b>ساعت کاری روزهای هفته</b>\n\n"
            "روی هر روز بزنید و ساعتش را بفرستید. روزی که ساعت ندارد، تعطیل حساب می\u200cشود.")
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def holidays_view(b: schedule.Behaviour, today: date) -> tuple[str, InlineKeyboardMarkup]:
    upcoming = [h for h in b.holidays if h[1] >= today]
    rows = []
    for start, end in upcoming[:12]:
        label = schedule.format_jalali(start) + ("" if start == end else f" تا {schedule.format_jalali(end)}")
        rows.append([_btn(f"🗑 {label}", HOLIDAY_DEL, f"{start.isoformat()}|{end.isoformat()}")])
    rows.append([_btn("➕ افزودن تعطیلی", HOLIDAY_ADD)])
    rows.append([_btn("🔙 بازگشت", MAIN)])
    text = ("🏖 <b>تعطیلات</b>\n\n"
            + ("روزهای تعطیل پیش\u200cرو؛ برای حذف روی هر مورد بزنید." if upcoming else "تعطیلی ثبت نشده است.")
            + "\nتعطیلات مثل روز غیرکاری حساب می\u200cشوند.")
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def _cancel_keyboard(back: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[_btn("❌ انصراف", back)]])


def register_schedule_handlers(router: Router, deps: Any) -> None:
    db = deps.db

    async def state_now() -> tuple[schedule.Behaviour, str]:
        owner = await db.get_owner_settings()
        return await db.get_reply_behaviour(), owner.timezone

    async def show_main(callback: CallbackQuery, note: str | None = None) -> None:
        b, tz = await state_now()
        text, markup = main_view(b, tz, schedule.now_utc())
        await deps.edit(callback, text, markup)
        await deps.answer(callback, note)

    @router.callback_query(AdminCB.filter(F.action == MAIN))
    async def cb_main(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await show_main(callback)

    @router.callback_query(AdminCB.filter(F.action == TYPING))
    async def cb_typing(callback: CallbackQuery, callback_data: AdminCB, state: FSMContext) -> None:
        await state.clear()
        raw = callback_data.key or ""
        if not raw.isdigit() or int(raw) not in schedule.TYPING_CHOICES:
            await deps.answer(callback, "دکمه نامعتبر است.", show_alert=True)
            return
        await db.set_typing_delay(int(raw))
        deps.log_action(callback.from_user.id, "typing_delay", raw)
        await show_main(callback, "ذخیره شد")

    @router.callback_query(AdminCB.filter(F.action == MODE))
    async def cb_mode(callback: CallbackQuery, callback_data: AdminCB, state: FSMContext) -> None:
        await state.clear()
        mode = callback_data.key or ""
        if mode not in schedule.MODES:
            await deps.answer(callback, "دکمه نامعتبر است.", show_alert=True)
            return
        try:
            await db.set_schedule_mode(mode)
        except InvalidReplyTextError:
            await deps.answer(callback, "اول از «🕘 ساعت کاری روزها» ساعت کاری را تنظیم کنید.", show_alert=True)
            return
        deps.log_action(callback.from_user.id, "schedule_mode", mode)
        await show_main(callback, "ذخیره شد")

    @router.callback_query(AdminCB.filter(F.action == DAYS))
    async def cb_days(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        b, _tz = await state_now()
        text, markup = days_view(b)
        await deps.edit(callback, text, markup)
        await deps.answer(callback)

    @router.callback_query(AdminCB.filter(F.action == PRESET))
    async def cb_preset(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await db.set_working_hours(dict(schedule.PRESET_HOURS))
        deps.log_action(callback.from_user.id, "working_hours_preset", "")
        b, _tz = await state_now()
        text, markup = days_view(b)
        await deps.edit(callback, text, markup)
        await deps.answer(callback, "ذخیره شد")

    @router.callback_query(AdminCB.filter(F.action == DAY))
    async def cb_day(callback: CallbackQuery, callback_data: AdminCB, state: FSMContext) -> None:
        raw = callback_data.key or ""
        if not raw.isdigit() or not 0 <= int(raw) <= 6:
            await deps.answer(callback, "دکمه نامعتبر است.", show_alert=True)
            return
        await state.set_state(ScheduleInput.waiting)
        await state.update_data(purpose="day", weekday=int(raw))
        await deps.edit(
            callback,
            f"🕘 ساعت کاری <b>{schedule.WEEKDAYS[int(raw)]}</b> را بفرستید، مثلاً:\n"
            "<code>9-17</code> یا <code>9:30-18</code> یا <code>22-2</code> (تا بعد از نیمه\u200cشب)\n"
            "یا «تعطیل»، یا «24» برای تمام روز.",
            _cancel_keyboard(DAYS),
        )
        await deps.answer(callback)

    @router.callback_query(AdminCB.filter(F.action == HOLIDAYS))
    async def cb_holidays(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        b, tz = await state_now()
        text, markup = holidays_view(b, schedule.local_now(schedule.now_utc(), tz).date())
        await deps.edit(callback, text, markup)
        await deps.answer(callback)

    @router.callback_query(AdminCB.filter(F.action == HOLIDAY_ADD))
    async def cb_holiday_add(callback: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(ScheduleInput.waiting)
        await state.update_data(purpose="holiday")
        await deps.edit(
            callback,
            "🏖 تاریخ تعطیلی را بفرستید (شمسی):\n"
            "یک روز: <code>۱۴۰۵/۱۲/۲۵</code>\n"
            "یک بازه: <code>۱۴۰۵/۱۲/۲۵ تا ۱۴۰۶/۰۱/۱۳</code>",
            _cancel_keyboard(HOLIDAYS),
        )
        await deps.answer(callback)

    @router.callback_query(AdminCB.filter(F.action == HOLIDAY_DEL))
    async def cb_holiday_del(callback: CallbackQuery, callback_data: AdminCB, state: FSMContext) -> None:
        await state.clear()
        try:
            start_s, end_s = (callback_data.key or "").split("|")
            start, end = date.fromisoformat(start_s), date.fromisoformat(end_s)
        except ValueError:
            await deps.answer(callback, "دکمه نامعتبر است.", show_alert=True)
            return
        await db.remove_holiday(start, end)
        deps.log_action(callback.from_user.id, "holiday_remove", "")
        b, tz = await state_now()
        text, markup = holidays_view(b, schedule.local_now(schedule.now_utc(), tz).date())
        await deps.edit(callback, text, markup)
        await deps.answer(callback, "حذف شد")

    @router.message(ScheduleInput.waiting, F.text)
    async def on_text(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        purpose = data.get("purpose")
        try:
            if purpose == "day":
                weekday = int(data.get("weekday", -1))
                hours = schedule.parse_hours(message.text or "")
                await db.set_working_hours({weekday: hours})
                done = f"✅ {schedule.WEEKDAYS[weekday]}: {schedule.format_hours(hours)}"
                back = days_view
            elif purpose == "holiday":
                start, end = schedule.parse_holiday(message.text or "")
                _b, tz = await state_now()
                today = schedule.local_now(schedule.now_utc(), tz).date()
                if end < today:
                    raise schedule.ScheduleError("past", "این تاریخ گذشته است.")
                await db.add_holiday(start, end, today=today)
                done = "✅ تعطیلی ثبت شد: " + schedule.format_jalali(start) + (
                    "" if start == end else f" تا {schedule.format_jalali(end)}")
                back = None
            else:
                await state.clear()
                return
        except schedule.ScheduleError as exc:
            await message.answer(f"⚠️ {exc.persian}\n\nدوباره بفرستید یا «❌ انصراف» را بزنید.",
                                 reply_markup=_cancel_keyboard(DAYS if purpose == "day" else HOLIDAYS))
            return
        except (InvalidReplyTextError, StorageError):
            await message.answer("ذخیره نشد (حداکثر ۳۰ تعطیلی). دوباره امتحان کنید.",
                                 reply_markup=_cancel_keyboard(MAIN))
            return
        await state.clear()
        deps.log_action(message.from_user.id if message.from_user else 0, f"schedule_{purpose}", "")
        b, tz = await state_now()
        if back is days_view:
            text, markup = days_view(b)
        else:
            text, markup = holidays_view(b, schedule.local_now(schedule.now_utc(), tz).date())
        await message.answer(f"{done}\n\n{text}", parse_mode=ParseMode.HTML, reply_markup=markup)

    @router.message(ScheduleInput.waiting)
    async def on_other(message: Message) -> None:
        await message.answer("لطفاً به\u200cصورت متن بفرستید، یا «❌ انصراف» را بزنید.",
                             reply_markup=_cancel_keyboard(MAIN))

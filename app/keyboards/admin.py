"""Inline keyboards for the Persian admin interface (pure functions, no I/O)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

if TYPE_CHECKING:  # Module 1 types; only needed for annotations.
    from app.database import AppState, Status


class Action:
    """Callback action identifiers."""

    MENU = "menu"
    STATUS = "status"
    ENABLE = "enable"
    DISABLE = "disable"
    EDIT_MENU = "editmenu"
    EDIT = "edit"
    SAVE = "save"
    CANCEL = "cancel"
    CONNECTION = "conn"
    STATS = "stats"
    # Phase 4 panel (see app/handlers/admin_panel.py)
    PANEL = "panel"
    ST_LIST = "stlist"
    ST_VIEW = "stview"
    ST_TITLE = "sttitle"
    ST_EMOJI = "stemoji"
    ST_STEPS = "ststeps"
    ST_POLICY = "stpolicy"
    ST_SET = "stset"
    ST_HOURS = "sthours"
    ST_DEL = "stdel"
    ST_DEL_OK = "stdelok"
    ST_NEW = "stnew"
    STEPS_DONE = "stdone"
    OWNER = "owner"
    OWNER_TZ = "owtz"
    OWNER_LANG = "owlang"
    OWNER_NAME = "ownname"
    EXC = "exc"
    EXC_ADD = "excadd"
    EXC_DEL = "excdel"


class AdminCB(CallbackData, prefix="adm"):
    """Callback payload. `key` is only ever a status key read from the database."""

    action: str
    key: str = ""


def persian_title(title: str) -> str:
    """Return the Persian part of a bilingual title such as 'Sleeping / در حال خواب'."""
    parts = [part.strip() for part in title.split("/")]
    for part in parts:
        if any("\u0600" <= ch <= "\u06ff" for ch in part):
            return part
    return title.strip()


def _btn(text: str, action: str, key: str = "") -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=text, callback_data=AdminCB(action=action, key=key).pack()
    )


def main_menu_keyboard(
    statuses: Sequence[Status], state: AppState
) -> InlineKeyboardMarkup:
    """Main menu: six status buttons, power toggle, edit, connection, stats."""
    rows: list[list[InlineKeyboardButton]] = []

    status_buttons: list[InlineKeyboardButton] = []
    for status in statuses:
        is_active = state.enabled and state.active_status_key == status.key
        mark = "✅ " if is_active else ""
        label = f"{mark}{status.emoji} {persian_title(status.title)}"
        status_buttons.append(_btn(label, Action.STATUS, status.key))
    for i in range(0, len(status_buttons), 2):
        rows.append(status_buttons[i : i + 2])

    if state.enabled:
        rows.append([_btn("⏸ غیرفعال‌سازی پاسخ خودکار", Action.DISABLE)])
    else:
        rows.append([_btn("▶️ فعال‌سازی پاسخ خودکار", Action.ENABLE)])

    rows.append([_btn("✏️ ویرایش متن پاسخ‌ها", Action.EDIT_MENU)])
    rows.append(
        [
            _btn("🔌 وضعیت اتصال", Action.CONNECTION),
            _btn("📊 آمار", Action.STATS),
        ]
    )
    rows.append([_btn("🧰 امکانات پیشرفته", Action.PANEL)])
    rows.append([_btn("🔄 بروزرسانی", Action.MENU)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def edit_status_keyboard(statuses: Sequence[Status]) -> InlineKeyboardMarkup:
    """Pick which status' reply text to edit."""
    rows = [
        [_btn(f"✏️ {s.emoji} {persian_title(s.title)}", Action.EDIT, s.key)]
        for s in statuses
    ]
    rows.append([_btn("🔙 بازگشت", Action.MENU)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def cancel_edit_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[_btn("❌ انصراف", Action.CANCEL)]])


def confirm_text_keyboard() -> InlineKeyboardMarkup:
    """Confirmation before overwriting a stored reply text."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _btn("✅ ذخیره", Action.SAVE),
                _btn("❌ انصراف", Action.CANCEL),
            ]
        ]
    )


def back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[_btn("🔙 بازگشت به منو", Action.MENU)]])


# --------------------------- Phase 4 panel -------------------------------- #
def _markup(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def panel_menu_keyboard() -> InlineKeyboardMarkup:
    return _markup(
        [
            [_btn("🧩 مدیریت وضعیت‌ها", Action.ST_LIST)],
            [_btn("➕ افزودن وضعیت", Action.ST_NEW)],
            [_btn("👤 تنظیمات مالک", Action.OWNER)],
            [_btn("🚫 استثناهای پاسخ", Action.EXC)],
            [_btn("🔙 بازگشت", Action.MENU)],
        ]
    )


def status_list_keyboard(statuses: Sequence[Status]) -> InlineKeyboardMarkup:
    rows = [
        [_btn(f"{s.emoji} {persian_title(s.title)}", Action.ST_VIEW, s.key)]
        for s in statuses
    ]
    rows.append([_btn("🔙 بازگشت", Action.PANEL)])
    return _markup(rows)


def status_view_keyboard(key: str) -> InlineKeyboardMarkup:
    return _markup(
        [
            [_btn("✏️ ویرایش عنوان", Action.ST_TITLE, key), _btn("🔤 ویرایش ایموجی", Action.ST_EMOJI, key)],
            [_btn("🧩 ویرایش بخش‌های پاسخ", Action.ST_STEPS, key)],
            [_btn("⚙️ سیاست پاسخ", Action.ST_POLICY, key)],
            [_btn("🗑 حذف وضعیت", Action.ST_DEL, key)],
            [_btn("🔙 بازگشت", Action.ST_LIST)],
        ]
    )


def status_policy_keyboard(status: Status, intervals: Sequence[int]) -> InlineKeyboardMarkup:
    """Policy controls. Callback key format: '<status_key>|<field>|<value>'."""
    key = status.key
    rows: list[list[InlineKeyboardButton]] = []
    if status.reply_mode == "reply":
        rows.append([_btn("💬 تغییر به حالت ساده (بدون ریپلای)", Action.ST_SET, f"{key}|mode|plain")])
    else:
        rows.append([_btn("↩️ تغییر به حالت ریپلای", Action.ST_SET, f"{key}|mode|reply")])

    interval_buttons = []
    for seconds in intervals:
        mark = "✅ " if seconds == status.min_interval_seconds else ""
        interval_buttons.append(_btn(f"{mark}⏱ {seconds}s", Action.ST_SET, f"{key}|int|{seconds}"))
    for i in range(0, len(interval_buttons), 3):
        rows.append(interval_buttons[i : i + 3])

    rows.append([_btn("🕒 تنظیم بازه ساعتی", Action.ST_HOURS, key)])
    if status.is_active:
        rows.append([_btn("⏸ غیرفعال‌سازی این وضعیت", Action.ST_SET, f"{key}|act|0")])
    else:
        rows.append([_btn("▶️ فعال‌سازی این وضعیت", Action.ST_SET, f"{key}|act|1")])
    rows.append([_btn("🔙 بازگشت", Action.ST_VIEW, key)])
    return _markup(rows)


def delete_confirm_keyboard(key: str) -> InlineKeyboardMarkup:
    return _markup(
        [
            [_btn("✅ بله، حذف شود", Action.ST_DEL_OK, key)],
            [_btn("❌ انصراف", Action.ST_VIEW, key)],
        ]
    )


def steps_wizard_keyboard(count: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if count > 0:
        rows.append([_btn("✅ پایان و ذخیره بخش‌ها", Action.STEPS_DONE)])
    rows.append([_btn("❌ انصراف", Action.CANCEL)])
    return _markup(rows)


def owner_keyboard() -> InlineKeyboardMarkup:
    return _markup(
        [
            [_btn("🕒 منطقه زمانی", Action.OWNER_TZ)],
            [_btn("🗣 تغییر زبان (فارسی / English)", Action.OWNER_LANG)],
            [_btn("👤 نام مالک", Action.OWNER_NAME)],
            [_btn("🔙 بازگشت", Action.PANEL)],
        ]
    )


def exceptions_keyboard(chat_ids: Sequence[int]) -> InlineKeyboardMarkup:
    rows = [[_btn(f"🗑 حذف {chat_id}", Action.EXC_DEL, str(chat_id))] for chat_id in chat_ids]
    rows.append([_btn("➕ افزودن شناسه", Action.EXC_ADD)])
    rows.append([_btn("🔙 بازگشت", Action.PANEL)])
    return _markup(rows)

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

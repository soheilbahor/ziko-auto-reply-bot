"""Owner panel: ready-made reply library (Telegram, Persian).

Flow:  📚 کتابخانه پیام  ->  group  ->  category (paged messages with preview)
       -> message  -> [✅ استفاده و فعال‌سازی] [⭐ علاقه‌مندی] [✏️ ویرایش پیش از ذخیره]

"Use" installs the message as a normal ZIKO status (create_status) and activates it,
so the existing reply engine sends it. Registered from create_admin_router, so the
router-level AdminFilter (owner only, private chat) guards every handler here.

Callback data: a dedicated LibCB ("zl") with typed fields, so an id never shares a
field with a page number and ":" (aiogram's separator) can never appear inside a value.
Ids are validated at library load (``[a-z0-9_.]{1,32}``), so the longest payload is
"zl:libc:" + 32 + ":" + page, far below Telegram's 64-byte limit (checked by tests).
The main-menu entry still uses AdminCB(action="lib").
"""
from __future__ import annotations

import html
import logging
from typing import Any

from aiogram import F, Router
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app import reply_library as rl
from app import templates
from app.keyboards.admin import Action, AdminCB

logger = logging.getLogger(__name__)
PER_PAGE = 3

MAX_PAGE = 999        # a forged page number is clamped, never trusted

LIB = "lib"           # item: ""            -> groups (also the AdminCB main-menu entry)
LIB_GROUP = "libg"    # item: group id
LIB_CAT = "libc"      # item: category id, page: page index
LIB_MSG = "libm"      # item: message id
LIB_USE = "libu"      # item: message id
LIB_FAV = "libf"      # item: message id
LIB_FAVS = "libs"     # item: ""            -> favourites list
LIB_EDIT = "libe"     # item: message id   -> edit text before saving
ACTIONS = frozenset({LIB, LIB_GROUP, LIB_CAT, LIB_MSG, LIB_USE, LIB_FAV, LIB_FAVS, LIB_EDIT})
PREFIX = "zl"


class LibCB(CallbackData, prefix=PREFIX):
    """Reply-library navigation. Typed: ``page`` is an int, parsed by aiogram."""

    action: str
    item: str = ""
    page: int = 0


class LibraryInput(StatesGroup):
    editing = State()


def _btn(text: str, action: str, item: str = "", page: int = 0) -> InlineKeyboardButton:
    if action in ACTIONS:
        data = LibCB(action=action, item=item, page=page).pack()
    else:  # links back into the main panel (Action.PANEL, DIAG, MENU)
        data = AdminCB(action=action, key=item).pack()
    return InlineKeyboardButton(text=text, callback_data=data)


def _e(text: object) -> str:
    return html.escape(str(text), quote=False)


def groups_view(lib: rl.Library) -> tuple[str, InlineKeyboardMarkup]:
    rows = [[_btn(f"{g.emoji} {g.title}", LIB_GROUP, g.id)] for g in lib.groups]
    rows.append([_btn("⭐ علاقه‌مندی‌های من", LIB_FAVS)])
    # The library is opened from the main menu (and from Advanced). "Back" returns to the
    # main menu; before 1.5.2 it opened the Advanced panel, which owners did not expect.
    rows.append([_btn("🔙 بازگشت به منوی اصلی", Action.MENU)])
    text = ("📚 <b>کتابخانه پیام‌های آماده</b>\n\n"
            f"{lib.count} پیام حرفه‌ای در {sum(len(g.categories) for g in lib.groups)} دسته.\n"
            "یک گروه را انتخاب کنید؛ هر پیام را می‌توانید ببینید، ویرایش کنید و با یک لمس فعال کنید.")
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def group_view(lib: rl.Library, group_id: str) -> tuple[str, InlineKeyboardMarkup] | None:
    g = lib.group(group_id)
    if g is None:
        return None
    rows = [[_btn(f"{c.emoji} {c.title} ({len(c.messages)})", LIB_CAT, c.id)] for c in g.categories]
    rows.append([_btn("🔙 بازگشت", LIB)])
    return f"{g.emoji} <b>{_e(g.title)}</b>\n\nدسته‌بندی را انتخاب کنید:", InlineKeyboardMarkup(inline_keyboard=rows)


def category_view(lib: rl.Library, category_id: str, favourites: set[str],
                  page: int = 0) -> tuple[str, InlineKeyboardMarkup] | None:
    cid = category_id
    c = lib.category(cid)
    if c is None:
        return None
    pages = max(1, -(-len(c.messages) // PER_PAGE))
    page = min(max(page, 0), pages - 1)
    shown = c.messages[page * PER_PAGE:(page + 1) * PER_PAGE]
    lines = [f"{c.emoji} <b>{_e(c.title)}</b>  (صفحه {page + 1}/{pages})", ""]
    rows = []
    for n, m in enumerate(shown, page * PER_PAGE + 1):
        star = "⭐ " if m.id in favourites else ""
        lines += [f"<b>{n}.</b> {star}<i>{rl.TONES[m.tone]}</i>",
                  f"<blockquote>{_e(templates.preview(m.text))}</blockquote>", ""]
        rows.append([_btn(f"{n}. انتخاب {star}".strip(), LIB_MSG, m.id)])
    nav = []
    if page > 0:
        nav.append(_btn("◀️ قبلی", LIB_CAT, cid, page - 1))
    if page < pages - 1:
        nav.append(_btn("بعدی ▶️", LIB_CAT, cid, page + 1))
    if nav:
        rows.append(nav)
    rows.append([_btn("🔙 بازگشت", LIB_GROUP, c.group_id)])
    lines.append("<i>‹نام مخاطب› در پیام واقعی با نام خود هر مخاطب جایگزین می‌شود؛ "
                 "اگر نامش معلوم نباشد، حذف می‌شود.</i>")
    return _fit("\n".join(lines)), InlineKeyboardMarkup(inline_keyboard=rows)


def message_view(lib: rl.Library, message_id: str, is_favourite: bool) -> tuple[str, InlineKeyboardMarkup] | None:
    m = lib.message(message_id)
    if m is None:
        return None
    c = lib.category(m.category_id)
    used = templates.variables_in(m.text)
    var_line = ("متغیرها: " + "، ".join(f"{{{v}}} = {templates.VARIABLES[v][1]}" for v in dict.fromkeys(used))
                if used else "بدون متغیر")
    text = (f"{c.emoji} <b>{_e(c.title)}</b> · <i>{rl.TONES[m.tone]}</i>\n\n"
            f"<b>پیش‌نمایش برای مشتری:</b>\n<blockquote>{_e(templates.preview(m.text))}</blockquote>\n"
            f"<b>متن قالب:</b>\n<code>{_e(m.text)}</code>\n\n{_e(var_line)}")
    rows = [
        [_btn("✅ استفاده و فعال‌سازی", LIB_USE, m.id)],
        [_btn("✏️ ویرایش و سپس ذخیره", LIB_EDIT, m.id)],
        [_btn("💔 حذف از علاقه‌مندی" if is_favourite else "⭐ افزودن به علاقه‌مندی", LIB_FAV, m.id)],
        [_btn("🔙 بازگشت", LIB_CAT, m.category_id)],
    ]
    return _fit(text), InlineKeyboardMarkup(inline_keyboard=rows)


def favourites_view(lib: rl.Library, ids: list[str]) -> tuple[str, InlineKeyboardMarkup]:
    items = [lib.message(i) for i in ids]
    items = [m for m in items if m is not None]  # a removed library message just disappears
    if not items:
        text = "⭐ <b>علاقه‌مندی‌ها</b>\n\nهنوز پیامی را نشان نکرده‌اید. در هر پیام دکمهٔ ⭐ را بزنید."
        rows = []
    else:
        text = "⭐ <b>علاقه‌مندی‌ها</b>\n\n" + "\n".join(
            f"• {_e(templates.preview(m.text))[:70]}…" for m in items[:20])
        rows = [[_btn(f"{lib.category(m.category_id).emoji} {templates.preview(m.text)[:28]}", LIB_MSG, m.id)]
                for m in items[:20]]
    rows.append([_btn("🔙 بازگشت", LIB)])
    return _fit(text), InlineKeyboardMarkup(inline_keyboard=rows)


def _fit(text: str) -> str:
    return text if len(text) <= 4000 else text[:3990] + "…"


def register_library_handlers(router: Router, deps: Any) -> None:
    db = deps.db
    lib = rl.load()

    async def favourite_ids() -> list[str]:
        return await db.list_favourites()

    async def show(callback: CallbackQuery, rendered: tuple[str, InlineKeyboardMarkup] | None) -> None:
        if rendered is None:
            await deps.answer(callback, "این مورد دیگر در کتابخانه نیست.", show_alert=True)
            return
        await deps.edit(callback, rendered[0], rendered[1])
        await deps.answer(callback)

    @router.callback_query(AdminCB.filter(F.action == LIB))
    @router.callback_query(LibCB.filter(F.action == LIB))
    async def cb_groups(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await show(callback, groups_view(lib))

    @router.callback_query(LibCB.filter(F.action == LIB_GROUP))
    async def cb_group(callback: CallbackQuery, callback_data: LibCB) -> None:
        await show(callback, group_view(lib, callback_data.item))

    @router.callback_query(LibCB.filter(F.action == LIB_CAT))
    async def cb_category(callback: CallbackQuery, callback_data: LibCB) -> None:
        await show(callback, category_view(lib, callback_data.item, set(await favourite_ids()),
                                         page=min(callback_data.page, MAX_PAGE)))

    @router.callback_query(LibCB.filter(F.action == LIB_MSG))
    async def cb_message(callback: CallbackQuery, callback_data: LibCB) -> None:
        favs = set(await favourite_ids())
        await show(callback, message_view(lib, callback_data.item, callback_data.item in favs))

    @router.callback_query(LibCB.filter(F.action == LIB_FAVS))
    async def cb_favourites(callback: CallbackQuery) -> None:
        await show(callback, favourites_view(lib, await favourite_ids()))

    @router.callback_query(LibCB.filter(F.action == LIB_FAV))
    async def cb_toggle_favourite(callback: CallbackQuery, callback_data: LibCB) -> None:
        if lib.message(callback_data.item) is None:
            await deps.answer(callback, "این پیام دیگر در کتابخانه نیست.", show_alert=True)
            return
        try:
            now_fav = await db.toggle_favourite(callback_data.item, limit=rl.MAX_FAVOURITES)
        except (KeyError, ValueError):
            await deps.answer(callback, "این پیام دیگر در کتابخانه نیست.", show_alert=True)
            return
        except OverflowError:
            await deps.answer(callback, f"حداکثر {rl.MAX_FAVOURITES} علاقه‌مندی مجاز است.", show_alert=True)
            return
        rendered = message_view(lib, callback_data.item, now_fav)
        if rendered:
            await deps.edit(callback, rendered[0], rendered[1])
        await deps.answer(callback, "⭐ به علاقه‌مندی‌ها اضافه شد." if now_fav else "از علاقه‌مندی‌ها حذف شد.")

    async def install(user_id: int, message_id: str, text: str | None = None) -> str:
        args = rl.status_from_message(lib, message_id)
        if text is not None:
            args["steps"] = [("text", text)]
        status = await db.create_status(**args)
        await db.activate_status(status.key)   # also switches auto-reply on (new session)
        deps.log_action(user_id, "library_install", message_id)
        return status.key

    @router.callback_query(LibCB.filter(F.action == LIB_USE))
    async def cb_use(callback: CallbackQuery, callback_data: LibCB, state: FSMContext) -> None:
        await state.clear()
        if lib.message(callback_data.item) is None:
            await deps.answer(callback, "این پیام دیگر در کتابخانه نیست.", show_alert=True)
            return
        try:
            await install(callback.from_user.id, callback_data.item)
        except Exception:
            logger.warning("Library install failed message_id=%s", callback_data.item, exc_info=True)
            await deps.answer(callback, "ذخیره ناموفق بود؛ دوباره امتحان کنید.", show_alert=True)
            return
        await deps.edit(callback,
                        "✅ <b>پیام فعال شد.</b>\n\nاز این لحظه به هر مخاطب جدید همین پیام ارسال می‌شود "
                        "(هر مخاطب یک بار در هر نشست).\n\nبرای اطمینان از آماده بودن، 🩺 عیب‌یابی را بزنید.",
                        InlineKeyboardMarkup(inline_keyboard=[
                            [_btn("🩺 عیب‌یابی", Action.DIAG)],
                            [_btn("📚 کتابخانه", LIB), _btn("🏠 منوی اصلی", Action.MENU)]]))
        await deps.answer(callback, "فعال شد ✅")

    @router.callback_query(LibCB.filter(F.action == LIB_EDIT))
    async def cb_edit(callback: CallbackQuery, callback_data: LibCB, state: FSMContext) -> None:
        m = lib.message(callback_data.item)
        if m is None:
            await deps.answer(callback, "این پیام دیگر در کتابخانه نیست.", show_alert=True)
            return
        await state.set_state(LibraryInput.editing)
        await state.update_data(message_id=m.id)
        allowed = " ".join("{" + v + "}" for v in templates.VARIABLES)
        await deps.edit(callback,
                        "✏️ متن جدید را بفرستید. می‌توانید از متغیرها استفاده کنید:\n"
                        f"<code>{_e(allowed)}</code>\n\nمتن فعلی:\n<code>{_e(m.text)}</code>",
                        InlineKeyboardMarkup(inline_keyboard=[[_btn("❌ انصراف", LIB_MSG, m.id)]]))
        await deps.answer(callback)

    @router.message(LibraryInput.editing, F.text)
    async def on_edited_text(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        message_id = data.get("message_id", "")
        try:
            text = templates.validate(message.text or "")
        except templates.TemplateError as exc:
            await message.answer(f"⚠️ {exc.persian}\n\nدوباره بفرستید یا /cancel بزنید.")
            return
        if lib.message(message_id) is None:
            await state.clear()
            await message.answer("این پیام دیگر در کتابخانه نیست.")
            return
        await state.clear()
        try:
            await install(message.from_user.id if message.from_user else 0, message_id, text)
        except Exception:
            logger.warning("Library install (edited) failed message_id=%s", message_id, exc_info=True)
            await message.answer("ذخیره ناموفق بود؛ دوباره امتحان کنید.")
            return
        await message.answer("✅ پیام ویرایش‌شده ذخیره و فعال شد.\n\nپیش‌نمایش:\n" + templates.preview(text))

    @router.message(LibraryInput.editing)
    async def on_edited_other(message: Message) -> None:
        await message.answer("لطفاً متن پیام را به‌صورت متن بفرستید، یا /cancel بزنید.")

    # Malformed or forged "zl:" payloads (bad page, unknown action, wrong field count) do
    # not match any LibCB filter above. Answer them so the button does not spin, change
    # nothing, and log only the action-free fact (callback data is user-controlled).
    @router.callback_query(F.data.startswith(PREFIX + ":"))
    async def cb_malformed(callback: CallbackQuery) -> None:
        logger.warning("Rejected malformed library callback user_id=%s",
                       callback.from_user.id if callback.from_user else 0)
        await deps.answer(callback, "این دکمه قدیمی است؛ دوباره از منو وارد شوید.", show_alert=True)

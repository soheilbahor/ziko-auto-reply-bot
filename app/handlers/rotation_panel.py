"""Owner panel: reply rotation per status and the online/offline switch (Telegram, Persian).

* Main menu shows "🟢 آنلاینم / 🌙 آفلاینم" once both statuses are linked; one tap switches.
* "🔁 چرخش پیام" on a status: add variants (one message each), choose random/sequence,
  preview, or turn rotation off.
* "🟢🌙 حالت آنلاین/آفلاین" in advanced features links the two statuses.

Every handler is attached to the admin router, so AdminFilter (owner only) applies.
"""
from __future__ import annotations

import html
import logging
from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app import rotation, templates
from app.keyboards.admin import Action, AdminCB, persian_title

logger = logging.getLogger(__name__)

ROT = "rot"          # key: status key            -> rotation screen
ROT_ADD = "rota"     # key: status key            -> wait for a variant message
ROT_MODE = "rotm"    # key: "<status>|<mode>"
ROT_OFF = "roto"     # key: status key            -> clear variants
AV = "av"            # key: ""                    -> availability settings
AV_PICK = "avp"      # key: "online" | "offline"  -> choose a status for that state
AV_SET = "avs"       # key: "<state>|<status>"
AV_GO = "avg"        # key: "online" | "offline"  -> switch now


class RotationInput(StatesGroup):
    adding = State()


def _btn(text: str, action: str, key: str = "") -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=AdminCB(action=action, key=key).pack())


def _e(v: object) -> str:
    return html.escape(str(v), quote=False)


def availability_row(av: dict[str, Any]) -> list[InlineKeyboardButton] | None:
    """Main-menu switch, shown only when both statuses are linked."""
    if not (av.get("online_key") and av.get("offline_key")):
        return None
    if av["state"] == "online":
        return [_btn("🟢 آنلاینم  ←  🌙 رفتن به آفلاین", AV_GO, "offline")]
    return [_btn("🌙 آفلاینم  ←  🟢 رفتن به آنلاین", AV_GO, "online")]


def rotation_view(status: Any, mode: str | None, variants: list[str]) -> tuple[str, InlineKeyboardMarkup]:
    title = f"{_e(status.emoji)} {_e(persian_title(status.title))}"
    if mode is None:
        text = (f"🔁 <b>چرخش پیام: {title}</b>\n\nچرخش خاموش است؛ همیشه همان یک متن ارسال میشود.\n\n"
                "با افزودن حداقل ۲ متن، هر مخاطب یکی از آنها را دریافت میکند.")
        lines = []
    else:
        text = (f"🔁 <b>چرخش پیام: {title}</b>\n\nحالت: <b>{rotation.MODE_LABELS[mode]}</b> · "
                f"{len(variants)} متن\n")
        lines = [f"\n<b>{i}.</b> <blockquote>{_e(templates.preview(v))[:300]}</blockquote>"
                 for i, v in enumerate(variants, 1)]
    rows = [[_btn("➕ افزودن متن", ROT_ADD, status.key)]]
    if mode is not None:
        other = "sequence" if mode == "random" else "random"
        rows.append([_btn(f"🔀 تغییر به «{rotation.MODE_LABELS[other]}»", ROT_MODE, f"{status.key}|{other}")])
        rows.append([_btn("⛔ خاموش کردن چرخش", ROT_OFF, status.key)])
    rows.append([_btn("🔙 بازگشت", Action.ST_VIEW, status.key)])
    body = text + "".join(lines)
    return (body if len(body) <= 4000 else body[:3990] + "…"), InlineKeyboardMarkup(inline_keyboard=rows)


def availability_view(av: dict[str, Any], statuses: list[Any]) -> tuple[str, InlineKeyboardMarkup]:
    names = {s.key: f"{s.emoji} {persian_title(s.title)}" for s in statuses}
    on = names.get(av.get("online_key"), "— انتخاب نشده —")
    off = names.get(av.get("offline_key"), "— انتخاب نشده —")
    state = "🟢 آنلاین" if av.get("state") == "online" else "🌙 آفلاین"
    text = ("🟢🌙 <b>حالت آنلاین / آفلاین</b>\n\n"
            "دو پیام جدا: یکی وقتی پشتیبان آنلاین است، یکی وقتی نیست. با یک لمس از منوی اصلی جابهجا کنید.\n"
            "با هر جابهجایی، هر مخاطب پیام جدید را یک بار دریافت میکند.\n\n"
            f"وقتی آنلاینم: <b>{_e(on)}</b>\nوقتی آفلاینم: <b>{_e(off)}</b>\nالان: {state}")
    rows = [[_btn("🟢 انتخاب پیام حالت آنلاین", AV_PICK, "online")],
            [_btn("🌙 انتخاب پیام حالت آفلاین", AV_PICK, "offline")]]
    if av.get("online_key") and av.get("offline_key"):
        rows.append([_btn("🟢 الان آنلاینم", AV_GO, "online"), _btn("🌙 الان آفلاینم", AV_GO, "offline")])
    rows.append([_btn("🔙 بازگشت", Action.PANEL)])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def pick_view(state: str, statuses: list[Any]) -> tuple[str, InlineKeyboardMarkup]:
    label = "آنلاین" if state == "online" else "آفلاین"
    rows = [[_btn(f"{s.emoji} {persian_title(s.title)}", AV_SET, f"{state}|{s.key}")] for s in statuses[:30]]
    rows.append([_btn("🔙 بازگشت", AV)])
    hint = "پیشنهاد: از 📚 پیامهای آماده، «ادمین آنلاین است» و «ادمین آفلاین» را فعال کنید."
    return f"کدام پیام برای حالت <b>{label}</b> ارسال شود؟\n\n{hint}", InlineKeyboardMarkup(inline_keyboard=rows)


def register_rotation_handlers(router: Router, deps: Any) -> None:
    db = deps.db

    async def show_rotation(callback: CallbackQuery, key: str, toast: str | None = None) -> None:
        status = await db.get_status(key)
        if status is None:
            await deps.answer(callback, "این وضعیت دیگر وجود ندارد.", show_alert=True)
            return
        mode, variants = await db.get_reply_variants(key)
        text, kb = rotation_view(status, mode, variants)
        await deps.edit(callback, text, kb)
        await deps.answer(callback, toast)

    @router.callback_query(AdminCB.filter(F.action == ROT))
    async def cb_rotation(callback: CallbackQuery, callback_data: AdminCB, state: FSMContext) -> None:
        await state.clear()
        await show_rotation(callback, callback_data.key)

    @router.callback_query(AdminCB.filter(F.action == ROT_ADD))
    async def cb_add(callback: CallbackQuery, callback_data: AdminCB, state: FSMContext) -> None:
        status = await db.get_status(callback_data.key)
        if status is None:
            await deps.answer(callback, "این وضعیت دیگر وجود ندارد.", show_alert=True)
            return
        mode, variants = await db.get_reply_variants(status.key)
        if len(variants) >= rotation.MAX_VARIANTS:
            await deps.answer(callback, f"حداکثر {rotation.MAX_VARIANTS} متن مجاز است.", show_alert=True)
            return
        await state.set_state(RotationInput.adding)
        await state.update_data(status_key=status.key)
        await deps.edit(callback, "✏️ متن جدید را بفرستید (هر پیام = یک متن).\n"
                                  "اولین بار، متن فعلی این وضعیت هم بهعنوان متن ۱ نگه داشته میشود.",
                        InlineKeyboardMarkup(inline_keyboard=[[_btn("❌ انصراف", ROT, status.key)]]))
        await deps.answer(callback)

    @router.message(RotationInput.adding, F.text)
    async def on_variant(message: Message, state: FSMContext) -> None:
        key = (await state.get_data()).get("status_key", "")
        try:
            text = templates.validate(message.text or "")
        except templates.TemplateError as exc:
            await message.answer(f"⚠️ {exc.persian}\n\nدوباره بفرستید یا /cancel بزنید.")
            return
        status = await db.get_status(key)
        if status is None:
            await state.clear()
            await message.answer("این وضعیت دیگر وجود ندارد.")
            return
        mode, variants = await db.get_reply_variants(key)
        if not variants:
            variants = [status.reply_text]          # keep the current text as variant 1
        variants.append(text)
        await db.set_reply_variants(key, variants[:rotation.MAX_VARIANTS], mode or "random")
        await state.clear()
        deps.log_action(message.from_user.id if message.from_user else 0, "rotation_add", key)
        await message.answer(f"✅ متن اضافه شد. این وضعیت حالا {len(variants)} متن دارد "
                             f"({rotation.MODE_LABELS[mode or 'random']}).\n\nپیشنمایش:\n{templates.preview(text)}",
                             reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_btn("🔁 مشاهده چرخش", ROT, key)]]))

    @router.message(RotationInput.adding)
    async def on_variant_other(message: Message) -> None:
        await message.answer("لطفاً متن را بهصورت متن بفرستید، یا /cancel بزنید.")

    @router.callback_query(AdminCB.filter(F.action == ROT_MODE))
    async def cb_mode(callback: CallbackQuery, callback_data: AdminCB) -> None:
        key, _, mode = callback_data.key.partition("|")
        if mode not in rotation.MODES:
            await deps.answer(callback, "این دکمه منقضی شده است.", show_alert=True)
            return
        _old, variants = await db.get_reply_variants(key)
        if len(variants) >= 2:
            await db.set_reply_variants(key, variants, mode)
        await show_rotation(callback, key, f"حالت: {rotation.MODE_LABELS[mode]}")

    @router.callback_query(AdminCB.filter(F.action == ROT_OFF))
    async def cb_off(callback: CallbackQuery, callback_data: AdminCB) -> None:
        if await db.get_status(callback_data.key) is not None:
            await db.set_reply_variants(callback_data.key, [], "random")
            deps.log_action(callback.from_user.id, "rotation_off", callback_data.key)
        await show_rotation(callback, callback_data.key, "چرخش خاموش شد.")

    # ------------------------------ availability ------------------------------ #
    async def show_availability(callback: CallbackQuery, toast: str | None = None) -> None:
        text, kb = availability_view(await db.get_availability(), await db.list_statuses())
        await deps.edit(callback, text, kb)
        await deps.answer(callback, toast)

    @router.callback_query(AdminCB.filter(F.action == AV))
    async def cb_av(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await show_availability(callback)

    @router.callback_query(AdminCB.filter(F.action == AV_PICK))
    async def cb_av_pick(callback: CallbackQuery, callback_data: AdminCB) -> None:
        if callback_data.key not in rotation.STATES:
            await deps.answer(callback, "این دکمه منقضی شده است.", show_alert=True)
            return
        text, kb = pick_view(callback_data.key, await db.list_statuses())
        await deps.edit(callback, text, kb)
        await deps.answer(callback)

    @router.callback_query(AdminCB.filter(F.action == AV_SET))
    async def cb_av_set(callback: CallbackQuery, callback_data: AdminCB) -> None:
        which, _, key = callback_data.key.partition("|")
        av = await db.get_availability()
        online, offline = av.get("online_key"), av.get("offline_key")
        if which == "online":
            online = key
        elif which == "offline":
            offline = key
        else:
            await deps.answer(callback, "این دکمه منقضی شده است.", show_alert=True)
            return
        if online and online == offline:
            await deps.answer(callback, "پیام آنلاین و آفلاین باید متفاوت باشند.", show_alert=True)
            return
        try:
            await db.set_availability_keys(online_key=online, offline_key=offline)
        except Exception:
            await deps.answer(callback, "این وضعیت دیگر وجود ندارد.", show_alert=True)
            return
        deps.log_action(callback.from_user.id, f"availability_{which}", key)
        await show_availability(callback, "ذخیره شد ✅")

    @router.callback_query(AdminCB.filter(F.action == AV_GO))
    async def cb_av_go(callback: CallbackQuery, callback_data: AdminCB) -> None:
        target = callback_data.key
        if target not in rotation.STATES:
            await deps.answer(callback, "این دکمه منقضی شده است.", show_alert=True)
            return
        try:
            await db.set_availability(target)
        except Exception:
            await deps.answer(callback, "ابتدا پیام هر دو حالت را در «حالت آنلاین/آفلاین» انتخاب کنید.",
                              show_alert=True)
            return
        deps.log_action(callback.from_user.id, f"availability_go_{target}", "")
        await deps.refresh_menu(callback)
        await deps.answer(callback, "🟢 حالت آنلاین فعال شد" if target == "online" else "🌙 حالت آفلاین فعال شد")

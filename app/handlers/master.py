"""Master-owner commands for platform-wide tenant visibility."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import asyncio
import logging

from aiogram import F, Router
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import BaseFilter, Command
from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app import platform_overview
from app.telegram_stats import STATS

from app.handlers.admin import format_problem_report
from app.logging_setup import log_file_for_bot, master_log_file, recent_problems
from app.tenant_registry import TenantRegistry
from app.tenant_bots import TenantBotManager


logger = logging.getLogger("ziko.master")


class MasterAdminFilter(BaseFilter):
    """Platform admins in a private chat only. Also guards callback queries."""

    def __init__(self, admin_ids: frozenset[int]) -> None:
        self.admin_ids = admin_ids

    async def __call__(self, event: Message | CallbackQuery) -> bool:
        user = event.from_user
        if not user or user.is_bot or user.id not in self.admin_ids:
            return False
        message = event if isinstance(event, Message) else event.message
        return isinstance(message, Message) and message.chat.type == ChatType.PRIVATE


class PlatformCB(CallbackData, prefix="plt"):
    """a: ov (overview) | bot (detail) | ask (confirm toggle) | set (apply toggle).
    f: filter all/attention. p: page. b: bot id. v: 1 enable / 0 disable."""

    a: str
    f: str = "all"
    p: int = 0
    b: int = 0
    v: int = 0
    r: int = 0  # 1 = refresh button: bypass the cached snapshot


def _btn(text: str, **kw: object) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=PlatformCB(**kw).pack())


def overview_keyboard(shown: list, page: int, pages: int, filt: str) -> InlineKeyboardMarkup:
    rows = [[_btn(f"{b.icon} @{b.username}"[:60], a="bot", b=b.bot_id, f=filt, p=page)] for b in shown]
    nav = []
    if page > 0:
        nav.append(_btn("◀️ قبلی", a="ov", f=filt, p=page - 1))
    if page < pages - 1:
        nav.append(_btn("بعدی ▶️", a="ov", f=filt, p=page + 1))
    if nav:
        rows.append(nav)
    other = ("attention", "⚠️ فقط نیازمند توجه") if filt == "all" else ("all", "📋 همه‌ی ربات‌ها")
    rows.append([_btn(other[1], a="ov", f=other[0]), _btn("🔄 بروزرسانی", a="ov", f=filt, p=page, r=1)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def bot_keyboard(view: platform_overview.BotView, filt: str, page: int) -> InlineKeyboardMarkup:
    toggle = (_btn("⏸ غیرفعال‌سازی", a="ask", b=view.bot_id, v=0, f=filt, p=page) if view.enabled
              else _btn("▶️ فعال‌سازی", a="ask", b=view.bot_id, v=1, f=filt, p=page))
    return InlineKeyboardMarkup(inline_keyboard=[
        [toggle, _btn("🔄 بروزرسانی", a="bot", b=view.bot_id, f=filt, p=page)],
        [_btn("🔙 بازگشت به فهرست", a="ov", f=filt, p=page)],
    ])


def confirm_keyboard(bot_id: int, enable: bool, filt: str, page: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn("✅ بله، " + ("فعال شود" if enable else "غیرفعال شود"), a="set", b=bot_id, v=int(enable), f=filt, p=page)],
        [_btn("❌ انصراف", a="bot", b=bot_id, f=filt, p=page)],
    ])


def make_overview(registry: TenantRegistry, manager: TenantBotManager):
    """Return an async callable producing a platform_overview.Overview (shared by /platform and alerts)."""
    data_dir = registry.path.parent

    async def overview() -> platform_overview.Overview:
        tenants = await registry.list_tenants()  # tokens are NOT revealed
        runtime = {t.bot_id: manager.runtime_state(t.bot_id) for t in tenants}
        telemetry = {b["bot_id"]: b for b in STATS.snapshot()["bots"] if b["role"] == "child"}
        # SQLite reads of N tenant DBs happen off the event loop.
        return await asyncio.to_thread(
            platform_overview.collect, tenants=tenants, data_dir=data_dir,
            runtime_of=runtime.__getitem__, telemetry=telemetry,
            running=manager.running_count(), waiting=manager.waiting_count(),
            limit=manager.max_concurrent_pollers)

    return overview


def create_master_router(
    *, registry: TenantRegistry, manager: TenantBotManager, admin_ids: frozenset[int],
    log_dir: Path | None = None, alerts: Any = None, alerts_mute_path: Path | None = None,
    overview_cache: Any = None,
) -> Router:
    router = Router(name="master_platform")
    admin_filter = MasterAdminFilter(admin_ids)
    router.message.filter(admin_filter)
    router.callback_query.filter(admin_filter)
    # One shared, background-warmed snapshot (app/overview_cache.py). A private cache is
    # created when none is injected, so the router also works standalone (tests).
    if overview_cache is None:
        from app.overview_cache import OverviewCache
        overview_cache = OverviewCache(make_overview(registry, manager))

    async def overview(fresh: bool = False) -> platform_overview.Overview:
        return await overview_cache.get(fresh=fresh)

    async def edit(callback: CallbackQuery, text: str, markup: InlineKeyboardMarkup) -> None:
        if not isinstance(callback.message, Message):
            return
        try:
            await callback.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
        except TelegramBadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                raise

    async def answer(callback: CallbackQuery, text: str | None = None, alert: bool = False) -> None:
        try:
            await callback.answer(text, show_alert=alert)
        except TelegramBadRequest:
            pass  # expired query: nothing to acknowledge

    @router.message(Command("platform", "dashboard"))
    async def platform(message: Message) -> None:
        ov = await overview()
        text, shown, pages = platform_overview.render_overview(ov)
        await message.answer(text, parse_mode=ParseMode.HTML,
                             reply_markup=overview_keyboard(shown, 0, pages, "all"))

    @router.callback_query(PlatformCB.filter(F.a == "ov"))
    async def cb_overview(callback: CallbackQuery, callback_data: PlatformCB) -> None:
        # Paging / filter switches use the shared snapshot; 🔄 (r=1) forces a new scan.
        ov = await overview(fresh=bool(callback_data.r))
        text, shown, pages = platform_overview.render_overview(ov, callback_data.p, callback_data.f)
        page = min(callback_data.p, pages - 1)
        await edit(callback, text, overview_keyboard(shown, page, pages, callback_data.f))
        await answer(callback)

    async def show_bot(callback: CallbackQuery, bot_id: int, filt: str, page: int,
                       toast: str | None = None) -> None:
        # A single bot's detail drives an admin decision: always current.
        ov = await overview(fresh=True)
        view = next((b for b in ov.bots if b.bot_id == bot_id), None)
        if view is None:
            await answer(callback, "این ربات دیگر ثبت نیست.", alert=True)
            text, shown, pages = platform_overview.render_overview(ov, page, filt)
            await edit(callback, text, overview_keyboard(shown, min(page, pages - 1), pages, filt))
            return
        await edit(callback, platform_overview.render_bot(view, ov.taken_at), bot_keyboard(view, filt, page))
        await answer(callback, toast)

    @router.callback_query(PlatformCB.filter(F.a == "bot"))
    async def cb_bot(callback: CallbackQuery, callback_data: PlatformCB) -> None:
        await show_bot(callback, callback_data.b, callback_data.f, callback_data.p)

    @router.callback_query(PlatformCB.filter(F.a == "ask"))
    async def cb_ask(callback: CallbackQuery, callback_data: PlatformCB) -> None:
        tenant = await registry.get_tenant_by_bot_id(callback_data.b)
        if tenant is None:
            await answer(callback, "این ربات دیگر ثبت نیست.", alert=True)
            return
        enable = bool(callback_data.v)
        effect = ("پولینگ ربات شروع می‌شود و پاسخ خودکار طبق تنظیمات مالک ادامه پیدا می‌کند."
                  if enable else
                  "پولینگ ربات فوراً متوقف می‌شود و هیچ پاسخ خودکاری ارسال نمی‌شود تا دوباره فعال شود. "
                  "داده‌ها و تنظیمات مالک حذف نمی‌شوند.")
        await edit(callback, f"{'▶️' if enable else '⏸'} <b>@{platform_overview._e(tenant.username)}</b>\n\n"
                   f"{effect}\n\nادامه می‌دهید؟",
                   confirm_keyboard(tenant.bot_id, enable, callback_data.f, callback_data.p))
        await answer(callback)

    @router.callback_query(PlatformCB.filter(F.a == "set"))
    async def cb_set(callback: CallbackQuery, callback_data: PlatformCB) -> None:
        bot_id, enable = callback_data.b, bool(callback_data.v)
        current = await registry.get_tenant_by_bot_id(bot_id)
        if current is None:
            await answer(callback, "این ربات دیگر ثبت نیست.", alert=True)
            return
        if current.enabled == enable:  # double tap / stale button: idempotent, no restart
            await show_bot(callback, bot_id, callback_data.f, callback_data.p,
                           "وضعیت از قبل همین بود.")
            return
        await registry.set_tenant_enabled(bot_id, enable)
        if enable:
            tenant = await registry.get_tenant_by_bot_id(bot_id, reveal_token=True)
            if tenant is not None:
                await manager.start_tenant(tenant)
        else:
            await manager.stop_tenant(bot_id)
        overview_cache.invalidate()
        logger.info("Platform admin action user_id=%s action=%s bot_id=%s",
                    callback.from_user.id, "enable" if enable else "disable", bot_id)
        await show_bot(callback, bot_id, callback_data.f, callback_data.p,
                       "فعال شد." if enable else "غیرفعال شد.")

    @router.message(Command("alerts"))
    async def alerts_cmd(message: Message) -> None:
        """/alerts | /alerts mute 2h | /alerts unmute"""
        if alerts is None:
            await message.answer(
                "اعلان‌های پلتفرم خاموش است (پیش‌فرض).\n"
                "برای فعال‌سازی در ziko.env بنویسید ZIKO_PLATFORM_ALERTS=1 و سرویس را ری‌استارت کنید.")
            return
        from app import platform_alerts
        parts = (message.text or "").split()
        if len(parts) >= 2 and parts[1] == "mute":
            seconds = platform_alerts.parse_duration(parts[2] if len(parts) > 2 else "1h")
            if seconds is None:
                await message.answer("استفاده: /alerts mute 30m | 2h | 1d (حداکثر ۷ روز)")
                return
            alerts.mute(seconds)
            if alerts_mute_path is not None:
                await asyncio.to_thread(platform_alerts.save_mute, alerts_mute_path, alerts.muted_until)
            logger.info("Platform alerts muted user_id=%s seconds=%s", message.from_user.id, int(seconds))
            await message.answer(f"🔕 اعلان‌ها به مدت {int(seconds // 60)} دقیقه بی‌صدا شد. "
                                 "مشکلات همچنان در /platform دیده می‌شوند.")
            return
        if len(parts) >= 2 and parts[1] == "unmute":
            alerts.unmute()
            if alerts_mute_path is not None:
                await asyncio.to_thread(platform_alerts.save_mute, alerts_mute_path, 0.0)
            await message.answer("🔔 اعلان‌ها دوباره فعال شد.")
            return
        if len(parts) >= 2:
            await message.answer("استفاده: /alerts · /alerts mute 2h · /alerts unmute")
            return
        st = alerts.status()
        cfg = alerts.config
        mute = f"🔕 بی‌صدا تا {st['muted_for'] // 60} دقیقه‌ی دیگر" if st["muted_for"] else "🔔 فعال"
        alerting = "، ".join(f"@{platform_overview._e(u)}" for u in st["alerting"][:20]) or "—"
        await message.answer(
            "🔔 <b>اعلان‌های پلتفرم</b>\n\n"
            f"وضعیت: {mute}\n"
            f"بررسی هر {int(cfg.interval)} ثانیه · تأیید پس از {cfg.confirm_checks} بررسی پیاپی\n"
            f"وضعیت‌های اعلان‌شونده: {'، '.join(sorted(cfg.states))}\n"
            f"سقف: {cfg.max_per_hour} پیام در ساعت · فاصله‌ی تکرار برای هر ربات: {int(cfg.cooldown // 60)} دقیقه\n"
            f"ارسال‌شده در ساعت اخیر: {st['sent_last_hour']}\n"
            f"مشکلات اعلان‌شده‌ی باز: {alerting}\n\n"
            "/alerts mute 2h · /alerts unmute",
            parse_mode=ParseMode.HTML)

    @router.message(Command("tenants"))
    async def tenants(message: Message) -> None:
        items = await registry.list_tenants()
        if not items:
            await message.answer("هنوز هیچ ربات زیرمجموعه‌ای ثبت نشده است.")
            return
        lines = [f"ربات‌های ثبت‌شده: {len(items)}", ""]
        for item in items[:100]:
            enabled_state = "فعال" if item.enabled else "غیرفعال"
            runtime_state = manager.runtime_status(item.bot_id)
            lines.append(
                f"• @{item.username} — Bot ID: {item.bot_id} — Owner: {item.user_id} "
                f"— {enabled_state} — Polling: {runtime_state}"
            )
        lines.extend([
            "",
            f"Polling فعال: {manager.running_count()} / {manager.max_concurrent_pollers}",
            f"در صف انتظار: {manager.waiting_count()}",
        ])
        if len(items) > 100:
            lines.append(f"\nو {len(items) - 100} مورد دیگر...")
        await message.answer("\n".join(lines))

    @router.message(Command("logs"))
    async def logs(message: Message) -> None:
        """/logs shows platform problems; /logs BOT_ID shows one child bot's problems."""
        if log_dir is None:
            await message.answer("گزارش لاگ در دسترس نیست.")
            return
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) == 1:
            path = master_log_file(log_dir)
            title = "🧾 <b>خطاهای اخیر مستر</b>"
        else:
            arg = parts[1].strip()
            if not (arg.isascii() and arg.isdigit()):
                await message.answer("استفاده: /logs یا /logs BOT_ID")
                return
            path = log_file_for_bot(log_dir, int(arg))
            title = f"🧾 <b>خطاهای اخیر ربات {arg}</b>"
        await message.answer(
            format_problem_report(recent_problems(path), title), parse_mode=ParseMode.HTML,
        )

    @router.message(Command("tenant_disable"))
    async def tenant_disable(message: Message) -> None:
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) != 2 or not parts[1].isascii() or not parts[1].isdigit():
            await message.answer("استفاده: /tenant_disable BOT_ID")
            return
        bot_id = int(parts[1])
        tenant = await registry.get_tenant_by_bot_id(bot_id)
        if tenant is None or not await registry.set_tenant_enabled(bot_id, False):
            await message.answer("رباتی با این شناسه ثبت نشده است.")
            return
        await manager.stop_tenant(bot_id)
        await message.answer(f"ربات @{tenant.username} (شناسه {bot_id}) غیرفعال شد.")

    @router.message(Command("tenant_enable"))
    async def tenant_enable(message: Message) -> None:
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) != 2 or not parts[1].isascii() or not parts[1].isdigit():
            await message.answer("استفاده: /tenant_enable BOT_ID")
            return
        bot_id = int(parts[1])
        if not await registry.set_tenant_enabled(bot_id, True):
            await message.answer("رباتی با این شناسه ثبت نشده است.")
            return
        tenant = await registry.get_tenant_by_bot_id(bot_id, reveal_token=True)
        if tenant is None:
            await message.answer("اطلاعات ربات پیدا نشد؛ وضعیت را دوباره بررسی کنید.")
            return
        await manager.start_tenant(tenant)
        await message.answer(f"ربات @{tenant.username} (شناسه {bot_id}) فعال شد.")

    return router

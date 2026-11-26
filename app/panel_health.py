"""Readiness diagnosis for the child-bot owner panel (pure logic, no aiogram, no I/O).

Answers the owner's most common question -- "why is nobody getting my auto-reply?" --
by evaluating, in the same order as ``BusinessMessageEngine``, every gate a reply
must pass. Each failed gate comes with a concrete Persian fix.

The checks mirror app/services/auto_reply.py exactly; if a gate is added there,
add it here too (tests/test_panel_health.py pins the order).
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

OK, WARN, BLOCK, INFO = "ok", "warn", "block", "info"
_ICON = {OK: "✅", WARN: "⚠️", BLOCK: "⛔", INFO: "ℹ️"}

CONNECT_GUIDE = (
    "📘 <b>اتصال ربات به حساب تلگرام (Telegram Business)</b>\n\n"
    "۱. تلگرام را به‌روز کنید. اتصال ربات فقط با <b>Telegram Premium</b> ممکن است.\n"
    "۲. بروید به: <b>Settings ← Telegram Business ← Chatbots</b>\n"
    "   (در بعضی نسخه‌ها: <b>Settings ← Business ← Chat Automation</b>)\n"
    "۳. نام کاربری همین ربات را وارد و انتخاب کنید.\n"
    "۴. گزینهٔ <b>«Reply to messages / پاسخ به پیام‌ها»</b> را روشن کنید.\n"
    "۵. مشخص کنید ربات به کدام چت‌ها دسترسی داشته باشد (همه یا انتخابی).\n"
    "۶. به همین‌جا برگردید و «🔄 بررسی دوباره» را بزنید.\n\n"
    "ℹ️ محدودیت‌های تلگرام: ربات فقط به چت‌های خصوصی پاسخ می‌دهد، به گروه‌ها نه؛ "
    "و فقط به پیام‌هایی که <b>بعد</b> از اتصال می‌رسند."
)


@dataclass(frozen=True, slots=True)
class Finding:
    level: str
    title: str          # Persian, shown to the bot owner in Telegram
    fix: str = ""       # Persian
    code: str = ""      # stable English identifier for logs, CLI and JSON


@dataclass(frozen=True, slots=True)
class Diagnosis:
    findings: tuple[Finding, ...]

    @property
    def ready(self) -> bool:
        return not any(f.level == BLOCK for f in self.findings)

    @property
    def headline(self) -> str:
        if not self.ready:
            return "⛔ پاسخ خودکار الان ارسال <b>نمی‌شود</b>"
        if any(f.level == WARN for f in self.findings):
            return "⚠️ پاسخ خودکار کار می‌کند، اما نکته‌ای هست"
        return "✅ همه‌چیز آماده است؛ پاسخ خودکار ارسال می‌شود"

    @property
    def badge(self) -> str:
        """One line for the main card."""
        if not self.ready:
            first = next(f for f in self.findings if f.level == BLOCK)
            return f"⛔ {first.title}"
        if any(f.level == WARN for f in self.findings):
            return "⚠️ آماده، با هشدار (🩺 عیب‌یابی)"
        return "✅ آمادهٔ پاسخ‌گویی"


def _display_title(title: str) -> str:
    """Same rule as keyboards.admin.persian_title (copied: this module must not import aiogram)."""
    parts = [part.strip() for part in title.split("/")]
    for part in parts:
        if any("\u0600" <= ch <= "\u06ff" for ch in part):
            return part
    return title.strip()


def _hour_allowed(now_utc: datetime, tz: str, start: int | None, end: int | None) -> bool:
    if start is None or end is None:
        return True
    hour = now_utc.astimezone(ZoneInfo(tz)).hour
    return start <= hour < end if start < end else (hour >= start or hour < end)


def diagnose(*, connection: Any, app_state: Any, status: Any, owner: Any,
             stats: dict[str, int] | None = None, problems: list[str] | None = None,
             exceptions_count: int = 0, now: datetime | None = None) -> Diagnosis:
    """All arguments are the objects the storage layer already returns (or None)."""
    now = now or datetime.now(UTC)
    f: list[Finding] = []

    # Gate 1 -- Business connection exists, is enabled, and may reply.
    if connection is None:
        f.append(Finding(BLOCK, "ربات هنوز به حساب تلگرام شما وصل نشده", code="business_not_connected", fix=
                         "دکمهٔ «📘 راهنمای اتصال» را بزنید و مراحل را انجام دهید."))
    elif not connection.is_enabled:
        f.append(Finding(BLOCK, "اتصال Business قطع شده است", code="business_disconnected", fix=
                         "در Settings ← Telegram Business ← Chatbots دوباره همین ربات را "
                         "انتخاب کنید. اگر عمداً قطع کرده‌اید، این پیام را نادیده بگیرید."))
    elif not connection.can_reply:
        f.append(Finding(BLOCK, "ربات وصل است ولی اجازهٔ پاسخ‌دادن ندارد", code="no_reply_permission", fix=
                         "در تنظیمات Chatbots گزینهٔ «Reply to messages» را روشن کنید."))
    else:
        f.append(Finding(OK, "اتصال Business فعال و دارای مجوز پاسخ", code="business_ok"))

    # Gate 2 -- auto-reply switched on with a selected status.
    if not app_state.enabled:
        f.append(Finding(BLOCK, "پاسخ خودکار خاموش است", code="auto_reply_off", fix=
                         "در منوی اصلی یکی از وضعیت‌ها (مثلاً 😴 در حال خواب) را انتخاب کنید."))
    elif status is None:
        f.append(Finding(BLOCK, "وضعیت فعال پیدا نشد", code="status_missing", fix=
                         "در منوی اصلی یک وضعیت دیگر انتخاب کنید."))
    else:
        f.append(Finding(OK, f"وضعیت فعال: {html.escape(status.emoji)} "
                             f"{html.escape(_display_title(status.title))}"))
        # Gate 3 -- the selected status itself is enabled and has text.
        if not status.is_active:
            f.append(Finding(BLOCK, "این وضعیت در «سیاست پاسخ» غیرفعال شده", code="status_inactive", fix=
                             "🧰 امکانات پیشرفته ← مدیریت وضعیت‌ها ← سیاست پاسخ ← فعال‌سازی"))
        if not (status.reply_text or "").strip():
            f.append(Finding(BLOCK, "متن پاسخ این وضعیت خالی است", code="reply_text_empty", fix=
                             "«✏️ ویرایش متن پاسخ‌ها» را بزنید."))
        # Gate 4 -- active-hours window (same rule as the engine).
        start, end = status.active_from_hour, status.active_to_hour
        if start is not None and end is not None:
            try:
                inside = _hour_allowed(now, owner.timezone, start, end)
            except (ZoneInfoNotFoundError, ValueError):
                f.append(Finding(BLOCK, f"منطقهٔ زمانی نامعتبر است: {html.escape(owner.timezone)}", code="invalid_timezone", fix=
                                 "👤 تنظیمات مالک ← منطقه زمانی (مثلاً Asia/Tehran)"))
            else:
                local = now.astimezone(ZoneInfo(owner.timezone)).strftime("%H:%M")
                window = f"{start:02d}:00 تا {end:02d}:00"
                if inside:
                    f.append(Finding(OK, f"داخل بازهٔ ساعتی {window} (اکنون {local})", code="inside_active_hours"))
                else:
                    f.append(Finding(BLOCK, f"الان خارج از بازهٔ ساعتی {window} است (اکنون {local})", code="outside_active_hours", fix=
                                     "اگر می‌خواهید همیشه پاسخ داده شود، در «سیاست پاسخ» بازه را "
                                     "«همیشه» کنید."))
        # Informational policies -- never block, but often surprise owners.
        if status.min_interval_seconds:
            f.append(Finding(INFO, f"به هر مخاطب حداکثر هر {status.min_interval_seconds} ثانیه یک‌بار پاسخ داده می‌شود"))
        f.append(Finding(INFO, "به هر مخاطب در هر نشست فقط یک‌بار پاسخ داده می‌شود؛ "
                               "با انتخاب دوبارهٔ وضعیت، نشست جدید شروع می‌شود"))
    if exceptions_count:
        f.append(Finding(INFO, f"{exceptions_count} مخاطب در فهرست استثنا هستند و پاسخ نمی‌گیرند"))

    # Delivery health from counters (privacy-safe aggregates only).
    if stats:
        sent, failed, unsure = stats.get("sent", 0), stats.get("failed", 0), stats.get("uncertain", 0)
        total = sent + failed + unsure
        if total >= 5 and (failed + unsure) / total > 0.2:
            f.append(Finding(WARN, f"{failed + unsure} از {total} ارسال موفق نبوده", code="high_failure_rate", fix=
                             "/logs را بزنید تا علت را ببینید."))
    for explanation in explain_problems(problems or []):
        f.append(explanation)
    return Diagnosis(tuple(f))


# Log line pattern -> owner-facing explanation. Order matters: first match wins per line.
_EXPLAIN: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (re.compile(r"Too Many Requests|retry after|Flood", re.I),
     "تلگرام موقتاً سرعت ارسال را محدود کرده (Flood)",
     "نیازی به کاری نیست؛ ربات خودش صبر می‌کند. اگر زیاد تکرار شد «فاصلهٔ پاسخ» را بیشتر کنید."),
    (re.compile(r"BUSINESS_PEER_USAGE_MISSING|peer.*usage", re.I),
     "تلگرام اجازهٔ پاسخ به یک چت را نداد",
     "معمولاً یعنی مخاطب تازه است یا در چت‌های مجاز ربات نیست. فهرست چت‌های مجاز را بررسی کنید."),
    (re.compile(r"Forbidden|blocked", re.I),
     "تلگرام ارسال به برخی مخاطبان را رد کرد",
     "احتمالاً آن چت در فهرست چت‌های مجاز ربات نیست یا مخاطب محدودیت گذاشته."),
    (re.compile(r"Unauthorized|token was rejected", re.I),
     "توکن ربات نامعتبر شده است",
     "اگر توکن را در @BotFather عوض کرده‌اید، در ربات اصلی /connect_bot را بزنید و توکن جدید را بدهید."),
    (re.compile(r"Network|Timeout|Cannot connect|Server ?Error|Bad Gateway", re.I),
     "مشکل موقت ارتباط با سرورهای تلگرام",
     "معمولاً خودبه‌خود رفع می‌شود. پیام‌هایی که وضعیتشان نامشخص است برای جلوگیری از ارسال تکراری دوباره ارسال نمی‌شوند."),
)


_EXPLAIN_CODES = dict(zip([t for _p, t, _f in _EXPLAIN],
                          ["flood_wait", "peer_not_allowed", "forbidden", "token_rejected", "network"]))


def explain_problems(lines: list[str]) -> list[Finding]:
    seen: dict[str, Finding] = {}
    counts: dict[str, int] = {}
    for line in lines:
        for pattern, title, fix in _EXPLAIN:
            if pattern.search(line):
                counts[title] = counts.get(title, 0) + 1
                seen.setdefault(title, Finding(WARN, title, fix, code="log_" + _EXPLAIN_CODES[title]))
                break
    return [Finding(WARN, f"{x.title} (×{counts[x.title]})", x.fix, code=x.code) for x in seen.values()]


def render(d: Diagnosis) -> str:
    lines = ["🩺 <b>عیب‌یابی پاسخ خودکار</b>", "", d.headline, ""]
    for x in d.findings:
        lines.append(f"{_ICON[x.level]} {x.title}")
        if x.fix and x.level in (BLOCK, WARN):
            lines.append(f"   ↳ <i>{x.fix}</i>")
    lines += ["", "تست: از یک حساب دیگر به خودتان پیام خصوصی بدهید."]
    text = "\n".join(lines)
    return text if len(text) <= 4000 else text[:3990] + "…"


def render_stats(stats: dict[str, int]) -> str:
    sent, failed, unsure = stats.get("sent", 0), stats.get("failed", 0), stats.get("uncertain", 0)
    partial = stats.get("partial", 0)
    total = sent + failed + unsure + partial
    rate = f"{100 * sent / total:.0f}٪" if total else "—"
    return (
        "📊 <b>آمار پاسخ‌گویی</b>\n\n"
        f"📥 پیام‌های دریافتی: <b>{stats.get('inbound_events', 0)}</b>\n"
        f"📤 پاسخ‌های ارسال‌شده: <b>{sent}</b>\n"
        f"🎯 نرخ موفقیت: <b>{rate}</b>\n"
        f"❌ ناموفق (قابل تلاش مجدد): {failed}\n"
        f"❔ نامشخص (برای جلوگیری از تکرار، دوباره ارسال نمی‌شود): {unsure}\n"
        f"⏳ در حال ارسال: {stats.get('pending', 0)}\n"
        f"🔌 اتصال‌های فعال: {stats.get('active_connections', 0)}\n\n"
        "<i>متن پیام‌های مخاطبان هرگز ذخیره نمی‌شود؛ فقط شمارنده‌ها.</i>"
    )

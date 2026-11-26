"""Daily report to the owner of a child bot (v1.6).

Once a day, at the hour the owner picks (owner's timezone), the child bot sends its
owner one Persian message with privacy-safe counts for the last 24 hours: incoming
messages, distinct contacts, replies sent, failed and uncertain deliveries, and the
change against the 24 hours before. No message text, name or chat id is included.

Off by default. At most one report per local day: the day is claimed in the database
before sending (a crash between claim and send loses that day's report rather than
sending it twice); a failed send releases the claim so the next check retries, except
when the owner blocked the bot (Forbidden), which is not retried that day.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any

from app import schedule

logger = logging.getLogger("ziko.daily_report")

HOURS = (9, 13, 18, 21, 23)          # choices offered in the panel
DEFAULT_HOUR = 21
CHECK_EVERY = 60.0                   # seconds between checks
_FA = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


def fa(value: Any) -> str:
    return str(value).translate(_FA)


def is_due(*, enabled: bool, hour: int, last_sent: date | None, now_local: datetime) -> bool:
    """Due once per local day, from ``hour`` until midnight (late start still sends)."""
    return bool(enabled) and now_local.hour >= hour and last_sent != now_local.date()


def _trend(now: int, before: int) -> str:
    if before == 0:
        return ""
    change = round((now - before) * 100 / before)
    if change == 0:
        return " (مثل دیروز)"
    return f" ({'▲' if change > 0 else '▼'} {fa(abs(change))}٪ نسبت به دیروز)"


def build_text(counts: dict[str, int], previous: dict[str, int], day: date, *,
               auto_reply_on: bool, preview: bool = False) -> str:
    inbound = int(counts.get("inbound", 0))
    contacts = int(counts.get("contacts", 0))
    sent = int(counts.get("sent", 0))
    failed = int(counts.get("failed", 0))
    unsure = int(counts.get("uncertain", 0))
    title = "📊 <b>گزارش روزانه</b>" + (" (پیش\u200cنمایش)" if preview else "")
    lines = [f"{title} — {schedule.format_jalali(day)}", "<i>۲۴ ساعت گذشته</i>", ""]
    if inbound == 0 and sent == 0 and failed + unsure == 0:
        lines.append("📭 در ۲۴ ساعت گذشته پیامی از مشتری نرسید.")
    else:
        lines.append(f"💬 پیام دریافتی: <b>{fa(inbound)}</b>{_trend(inbound, int(previous.get('inbound', 0)))}")
        lines.append(f"👥 مخاطب: <b>{fa(contacts)}</b>")
        lines.append(f"✅ پاسخ خودکار ارسال\u200cشده: <b>{fa(sent)}</b>{_trend(sent, int(previous.get('sent', 0)))}")
        if failed:
            lines.append(f"❌ ارسال ناموفق: <b>{fa(failed)}</b>")
        if unsure:
            lines.append(f"❔ نامشخص (مثلاً قطع اینترنت سرور): <b>{fa(unsure)}</b>")
        not_replied = max(0, contacts - sent - failed - unsure)
        if not_replied:
            lines.append(f"⏸ بدون پاسخ خودکار: <b>{fa(not_replied)}</b> مخاطب "
                         "(قبلاً پاسخ گرفته\u200cاند، خارج از زمان\u200cبندی، یا در استثنا)")
    lines.append("")
    if failed + unsure:
        lines.append("⚠️ برای دیدن علت، /logs یا /diagnose را بزنید.")
    lines.append("🟢 پاسخ خودکار روشن است." if auto_reply_on else "🔴 پاسخ خودکار الان خاموش است.")
    return "\n".join(lines)


async def compose(db: Any, now_utc: datetime, *, preview: bool = False) -> str:
    owner = await db.get_owner_settings()
    day = schedule.local_now(now_utc, owner.timezone).date()
    counts = await db.report_counts(now_utc - timedelta(hours=24), now_utc)
    previous = await db.report_counts(now_utc - timedelta(hours=48), now_utc - timedelta(hours=24))
    state = await db.get_app_state()
    return build_text(counts, previous, day, auto_reply_on=bool(state.enabled), preview=preview)


async def run_once(db: Any, bot: Any, owner_id: int, now_utc: datetime | None = None) -> bool:
    """Send today's report if it is due. Returns True when one was sent."""
    from aiogram.exceptions import TelegramForbiddenError

    now_utc = now_utc or datetime.now(UTC)
    settings = await db.get_report_settings()
    owner = await db.get_owner_settings()
    local = schedule.local_now(now_utc, owner.timezone)
    if not is_due(enabled=settings["enabled"], hour=settings["hour"],
                  last_sent=settings["last_sent"], now_local=local):
        return False
    if not await db.claim_report_day(local.date()):
        return False                               # another check already claimed today
    try:
        text = await compose(db, now_utc)
        await bot.send_message(chat_id=owner_id, text=text, parse_mode="HTML")
    except asyncio.CancelledError:
        await asyncio.shield(db.release_report_day(local.date()))
        raise
    except TelegramForbiddenError:
        logger.info("Daily report not delivered: owner blocked the bot")
        return False
    except Exception as exc:
        await db.release_report_day(local.date())
        logger.warning("Daily report failed %s; will retry", type(exc).__name__)
        return False
    logger.info("Daily report sent")
    return True


async def report_loop(db: Any, bot: Any, owner_id: int, *, interval: float = CHECK_EVERY,
                      sleep: Any = asyncio.sleep) -> None:
    """Runs for the lifetime of one child poller; never raises except on cancellation."""
    while True:
        try:
            await run_once(db, bot, owner_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Daily report check failed %s", type(exc).__name__)
        await sleep(interval)

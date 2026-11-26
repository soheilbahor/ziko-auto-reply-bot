"""Weekly working hours, holidays and the "typing…" pause (pure logic, no I/O).

The owner describes WHEN they work; the reply mode decides when auto-replies go out:

* ``always``   every time (default; the behaviour of every earlier release)
* ``outside``  only outside working hours and on holidays ("we are closed")
* ``inside``   only during working hours

Weekdays use the Persian week: 0 = Saturday … 6 = Friday. Times are minutes from
midnight in the owner's timezone. A day may span midnight (22:00-02:00). A weekday
with no hours set is a day off. Holidays are inclusive local-date ranges and count as
closed all day. Dates are stored in ISO (Gregorian) and shown to owners in the Solar
Hijri (Jalali) calendar, which is what Iranian owners type.

All texts here are Persian (they appear in Telegram); codes are English.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MODES = ("always", "outside", "inside")
TYPING_CHOICES = (0, 2, 4, 7)       # seconds offered in the panel
MAX_TYPING = 10
TYPING_REFRESH = 4.5                # Telegram shows "typing…" for about 5 s
MAX_HOLIDAYS = 30
WEEKDAYS = ("شنبه", "یکشنبه", "دوشنبه", "سه\u200cشنبه", "چهارشنبه", "پنج\u200cشنبه", "جمعه")
MODE_LABELS = {
    "always": "همیشه",
    "outside": "فقط خارج از ساعت کاری",
    "inside": "فقط در ساعت کاری",
}
_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
_FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
_CLOSED_WORDS = {"تعطیل", "بسته", "off", "closed", "-", "نه"}
_ALL_DAY_WORDS = {"24", "۲۴", "24h", "تمام روز", "تمام\u200cروز", "۲۴ ساعته", "24 ساعته", "همه", "شبانه\u200cروزی"}
_TIME = r"(\d{1,2})(?:[:.](\d{2}))?"
_RANGE_RE = re.compile(rf"^\s*{_TIME}\s*(?:-|–|تا|الی|to)\s*{_TIME}\s*$")
_DATE_RE = re.compile(r"^\s*(\d{4})[/\-.](\d{1,2})[/\-.](\d{1,2})\s*$")


class ScheduleError(ValueError):
    """Bad owner input. ``persian`` is shown in Telegram."""

    def __init__(self, message: str, persian: str) -> None:
        super().__init__(message)
        self.persian = persian


Hours = tuple[int, int] | None          # (open_minute, close_minute); None = day off


@dataclass(frozen=True, slots=True)
class Behaviour:
    typing_delay: int = 0
    mode: str = "always"
    hours: dict[int, Hours] = field(default_factory=dict)
    holidays: tuple[tuple[date, date], ...] = ()

    @property
    def has_hours(self) -> bool:
        return any(v is not None for v in self.hours.values())


# --------------------------------------------------------------------------- Jalali calendar
# Borkowski's algorithm as used by jalaali-js; valid for Jalali years -61 … 3177.
_BREAKS = (-61, 9, 38, 199, 426, 686, 756, 818, 1111, 1181, 1210, 1635, 2060, 2097, 2192,
           2262, 2324, 2394, 2456, 3178)


def _div(a: int, b: int) -> int:     # JavaScript ~~(a / b): truncates toward zero
    return int(a / b)


def _mod(a: int, b: int) -> int:
    return a - _div(a, b) * b


def _jal_cal(jy: int) -> tuple[int, int, int]:
    """(leap, gregorian year, March day of Nowruz) for a Jalali year."""
    if jy < _BREAKS[0] or jy >= _BREAKS[-1]:
        raise ValueError(f"Jalali year out of range: {jy}")
    gy, leap_j, jp, jump = jy + 621, -14, _BREAKS[0], 0
    for jm in _BREAKS[1:]:
        jump = jm - jp
        if jy < jm:
            break
        leap_j += _div(jump, 33) * 8 + _div(_mod(jump, 33), 4)
        jp = jm
    n = jy - jp
    leap_j += _div(n, 33) * 8 + _div(_mod(n, 33) + 3, 4)
    if _mod(jump, 33) == 4 and jump - n == 4:
        leap_j += 1
    leap_g = _div(gy, 4) - _div((_div(gy, 100) + 1) * 3, 4) - 150
    march = 20 + leap_j - leap_g
    if jump - n < 6:
        n = n - jump + _div(jump + 4, 33) * 33
    leap = _mod(_mod(n + 1, 33) - 1, 4)
    if leap == -1:
        leap = 4
    return leap, gy, march


def jalali_to_date(jy: int, jm: int, jd: int) -> date:
    if not 1 <= jm <= 12 or not 1 <= jd <= (31 if jm <= 6 else 30):
        raise ValueError("invalid Jalali month/day")
    leap, gy, march = _jal_cal(jy)
    if jm == 12 and jd == 30 and leap != 0:
        raise ValueError("Esfand 30 exists only in leap years")
    nowruz = date(gy, 3, march)
    offset = (jm - 1) * 31 - _div(jm, 7) * (jm - 7) + jd - 1
    return nowruz + timedelta(days=offset)


def date_to_jalali(d: date) -> tuple[int, int, int]:
    jy = d.year - 621
    leap, gy, march = _jal_cal(jy)
    k = (d - date(gy, 3, march)).days
    if k >= 0:
        if k <= 185:
            return jy, 1 + k // 31, k % 31 + 1
        k -= 186
    else:
        jy -= 1
        k += 179
        if leap == 1:
            k += 1
    return jy, 7 + k // 30, k % 30 + 1


def format_jalali(d: date) -> str:
    jy, jm, jd = date_to_jalali(d)
    return f"{jy:04d}/{jm:02d}/{jd:02d}".translate(_FA_DIGITS)


# --------------------------------------------------------------------------- parsing
def _normal(text: str) -> str:
    return " ".join((text or "").translate(_DIGITS).replace("\u200c", " ").split()).strip()


def parse_hours(text: str) -> Hours:
    """Owner input for one day: "9-17", "9:30-18", "۹ تا ۱۷", "22-2", "تعطیل", "24"."""
    raw = _normal(text).lower()
    if raw in {w.replace("\u200c", " ") for w in _CLOSED_WORDS}:
        return None
    if raw in {w.replace("\u200c", " ").translate(_DIGITS) for w in _ALL_DAY_WORDS}:
        return (0, 1440)
    m = _RANGE_RE.match(raw)
    if not m:
        raise ScheduleError("bad hours", "قالب درست نیست. مثل 9-17 یا 9:30-18، یا «تعطیل» یا «24» بفرستید.")
    h1, m1, h2, m2 = int(m[1]), int(m[2] or 0), int(m[3]), int(m[4] or 0)
    if not (0 <= h1 <= 23 and 0 <= h2 <= 24 and m1 < 60 and m2 < 60) or (h2 == 24 and m2):
        raise ScheduleError("hour out of range", "ساعت باید بین ۰ تا ۲۴ و دقیقه بین ۰ تا ۵۹ باشد.")
    start, end = h1 * 60 + m1, h2 * 60 + m2
    if start == end:
        raise ScheduleError("empty range", "ساعت شروع و پایان یکی است.")
    return (start, end)


def parse_date(text: str) -> date:
    """"1405/12/25" (Jalali) or "2027-03-16" (Gregorian, year >= 1700)."""
    m = _DATE_RE.match(_normal(text))
    if not m:
        raise ScheduleError("bad date", "تاریخ را مثل ۱۴۰۵/۱۲/۲۵ بفرستید.")
    y, mo, d = int(m[1]), int(m[2]), int(m[3])
    try:
        return date(y, mo, d) if y >= 1700 else jalali_to_date(y, mo, d)
    except ValueError:
        raise ScheduleError("invalid date", "این تاریخ وجود ندارد.") from None


_ANY_DATE = re.compile(r"\d{4}[/.\-]\d{1,2}[/.\-]\d{1,2}")


def parse_holiday(text: str) -> tuple[date, date]:
    """"1405/12/25", "1405/12/25-1406/01/13", "۱۴۰۵/۱۲/۲۵ تا ۱۴۰۶/۰۱/۱۳" or ISO dates."""
    raw = _normal(text)
    found = _ANY_DATE.findall(raw)
    rest = _ANY_DATE.sub(" ", raw)
    rest = re.sub(r"(?:تا|الی|to|[-–\s])", "", rest)
    if len(found) not in (1, 2) or rest:
        raise ScheduleError("bad range", "یک تاریخ یا یک بازه بفرستید، مثل ۱۴۰۵/۱۲/۲۵ تا ۱۴۰۶/۰۱/۱۳.")
    start = parse_date(found[0])
    end = parse_date(found[-1])
    if end < start:
        raise ScheduleError("reversed range", "تاریخ پایان قبل از تاریخ شروع است.")
    if (end - start).days > 92:
        raise ScheduleError("too long", "هر تعطیلی حداکثر ۹۳ روز می\u200cتواند باشد.")
    return start, end


# --------------------------------------------------------------------------- deciding
def local_now(now_utc: datetime, tz: str) -> datetime:
    try:
        zone = ZoneInfo(tz or "Asia/Tehran")
    except (ZoneInfoNotFoundError, ValueError):
        zone = ZoneInfo("Asia/Tehran")
    return now_utc.astimezone(zone)


def persian_weekday(d: date) -> int:
    return (d.weekday() + 2) % 7          # Monday=0 -> 2; Saturday=5 -> 0


def _in_range(minute: int, hours: tuple[int, int]) -> bool:
    start, end = hours
    return start <= minute < end if start < end else (minute >= start or minute < end)


def holiday_on(day: date, holidays: tuple[tuple[date, date], ...]) -> tuple[date, date] | None:
    return next((h for h in holidays if h[0] <= day <= h[1]), None)


def is_open(b: Behaviour, now_utc: datetime, tz: str) -> tuple[bool, str]:
    """(working now?, reason code: holiday | day_off | open | closed_now)."""
    now = local_now(now_utc, tz)
    if holiday_on(now.date(), b.holidays):
        return False, "holiday"
    minute = now.hour * 60 + now.minute
    today = b.hours.get(persian_weekday(now.date()))
    # An overnight shift that started yesterday still counts after midnight.
    yesterday = b.hours.get(persian_weekday(now.date() - timedelta(days=1)))
    if yesterday and yesterday[0] >= yesterday[1] and minute < yesterday[1] \
            and not holiday_on(now.date() - timedelta(days=1), b.holidays):
        return True, "open"
    if today is None:
        return False, "day_off"
    if today[0] >= today[1]:
        return (minute >= today[0], "open" if minute >= today[0] else "closed_now")
    return (_in_range(minute, today), "open" if _in_range(minute, today) else "closed_now")


def reply_allowed(b: Behaviour, now_utc: datetime, tz: str) -> tuple[bool, str]:
    """(send an auto-reply now?, reason code). ``always`` never blocks."""
    if b.mode == "always" or b.mode not in MODES:
        return True, "always"
    working, reason = is_open(b, now_utc, tz)
    if b.mode == "outside":
        return (not working, "working_hours" if working else reason)
    return (working, reason)


def explain(b: Behaviour, now_utc: datetime, tz: str) -> str:
    """One Persian line: whether a reply goes out now, and why."""
    ok, reason = reply_allowed(b, now_utc, tz)
    if b.mode == "always":
        return "✅ پاسخ خودکار همیشه ارسال می\u200cشود."
    now = local_now(now_utc, tz)
    day = WEEKDAYS[persian_weekday(now.date())]
    why = {
        "holiday": "امروز تعطیل است",
        "day_off": f"{day} روز کاری نیست",
        "closed_now": f"الان ({now:%H:%M}) خارج از ساعت کاری {day} است".translate(_FA_DIGITS),
        "open": f"الان ({now:%H:%M}) ساعت کاری {day} است".translate(_FA_DIGITS),
        "working_hours": f"الان ({now:%H:%M}) ساعت کاری {day} است".translate(_FA_DIGITS),
    }.get(reason, "")
    return (f"✅ الان پاسخ ارسال می\u200cشود ({why})." if ok
            else f"⏸ الان پاسخ ارسال نمی\u200cشود ({why}).")


def format_hours(hours: Hours) -> str:
    if hours is None:
        return "تعطیل"
    if hours == (0, 1440):
        return "۲۴ ساعته"
    start, end = hours

    def hm(m: int) -> str:
        return f"{m // 60}:{m % 60:02d}"
    return f"{hm(start)}–{hm(end)}".translate(_FA_DIGITS)


PRESET_HOURS: dict[int, Hours] = {0: (540, 1020), 1: (540, 1020), 2: (540, 1020), 3: (540, 1020),
                                  4: (540, 1020), 5: (540, 780), 6: None}
"""Saturday-Wednesday 9-17, Thursday 9-13, Friday off: the common Iranian office week."""


def typing_seconds(delay: int, text_length: int) -> float:
    """Natural pause before a text part: the base delay, a little longer for long texts."""
    if delay <= 0:
        return 0.0
    return float(min(MAX_TYPING, delay + min(3.0, text_length / 120)))


def now_utc() -> datetime:
    return datetime.now(UTC)

# Working hours, holidays, typing pause and daily report (v1.6.0)

Owner panel → **⏰ زمان‌بندی و تایپ**.

| Setting | Values | Default |
|---|---|---|
| Typing pause | off, 2 s, 4 s, 7 s (+ up to 3 s for long texts, max 10 s) | off |
| Reply mode | always / only outside working hours / only during working hours | always |
| Working hours | per weekday: `9-17`, `9:30-18`, `22-2`, `تعطیل`, `24` | not set |
| Holidays | `1405/12/25` or `1405/12/25 تا 1406/01/13` (Jalali; ISO also accepted) | none |

Rules
- Weekdays without hours are days off. Holidays count as closed all day.
- An overnight shift (22-2) still counts after midnight, unless that previous day was a holiday.
- Times use the owner's timezone (owner settings, default Asia/Tehran).
- "Only outside working hours" needs hours set first; the panel refuses otherwise.
- The once-per-contact-per-session rule and the minimum interval still apply on top.

Server test (staging)
1. Child bot → /menu → ⏰ → 4s. Message the business account from another account: "typing…"
   appears ~4 s, then the reply.
2. Preset hours, mode "only outside working hours". During hours: no reply; `/diagnose` shows
   the reason. Add today as a holiday: replies resume.
3. Switch auto-reply off during the pause (7 s): nothing is sent.
4. Rollback check (optional): `update.sh --rollback-to v1.5.6` → bot replies as before.

Daily report (owner panel → 📊 گزارش روزانه)
5. Turn it on, press "👁 پیشنمایش": the report arrives immediately (does not count as today's).
6. Set the hour to the next one coming up; within a minute after it, one report arrives. Restart
   the service after it: no second report that day.

Store and support names (owner panel → 👤 تنظیمات مالک)
7. Set "🏪 نام فروشگاه" to a test name; put `{store_name}` in the active reply; message the
   business account: the reply shows the name. Send `-` to clear: the owner name is used again.

# ZIKO Personal Assistant

[![Latest Stable Release](https://img.shields.io/github/v/release/soheilbahor/ziko-auto-reply-bot?label=stable)](https://github.com/soheilbahor/ziko-auto-reply-bot/releases/latest)
[![CI](https://github.com/soheilbahor/ziko-auto-reply-bot/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/soheilbahor/ziko-auto-reply-bot/actions/workflows/ci.yml)
[![Release](https://github.com/soheilbahor/ziko-auto-reply-bot/actions/workflows/release.yml/badge.svg)](https://github.com/soheilbahor/ziko-auto-reply-bot/actions/workflows/release.yml)

**Repository:** [soheilbahor/ziko-auto-reply-bot](https://github.com/soheilbahor/ziko-auto-reply-bot) Â· [Releases](https://github.com/soheilbahor/ziko-auto-reply-bot/releases) Â· [Issues](https://github.com/soheilbahor/ziko-auto-reply-bot/issues)

[English](README.en.md) · **فارسی**

> دستیار شخصی پاسخ‌خودکار برای تلگرام، بر پایهٔ **Telegram Business** و **Bot API رسمی**.
> بدون Userbot، بدون Telethon/Pyrogram/MTProto و بدون ورود به حساب شخصی.

زیکو یک ربات «متصل به حساب کسب‌وکار» (Connected Business Bot) است. وقتی وضعیت خود را (مثلاً «در حال خواب») انتخاب می‌کنید، به پیام‌های خصوصی تازه‌ای که به حساب شما می‌رسد، **یک‌بار در هر نشست و برای هر چت**، پاسخ خودکارِ همان وضعیت را از طرف شما می‌فرستد.

> ⚠️ وضعیت پروژه: نسخهٔ ۱.۲.۰ در حال آماده‌سازی است. گزارش آزمون‌ها در [`docs/reports/QA_REPORT.md`](docs/reports/QA_REPORT.md) و گزارش فاز امنیت در [`docs/PHASE1_SECURITY_REPORT.md`](docs/PHASE1_SECURITY_REPORT.md) قرار دارد. تا پایان CI و آزمون روی Ubuntu 24.04، این نسخه Production تأییدشده نیست.

گزارش ادغام و آزمون‌های قبلی: [`docs/reports/MERGE_TEST_REPORT.md`](docs/reports/MERGE_TEST_REPORT.md).

## فهرست

- [ویژگی‌ها](#ویژگیها)
- [معماری](#معماری)
- [پیش‌نیازها](#پیشنیازها)
- [نصب](#نصب)
- [اتصال به Telegram Business](#اتصال-به-telegram-business)
- [دستورات و CLI](#دستورات-و-cli)
- [به‌روزرسانی](#بهروزرسانی)
- [پشتیبان‌گیری و بازیابی](#پشتیبانگیری-و-بازیابی)
- [عیب‌یابی](#عیبیابی)
- [امنیت و حریم خصوصی](#امنیت-و-حریم-خصوصی)
- [توسعه و آزمون](#توسعه-و-آزمون)
- [مجوز](#مجوز)

## ویژگی‌ها

- شش وضعیت آماده: 😴 در حال خواب، 🍳 در حال آشپزی، 🎮 در حال بازی، 🚶 بیرون از خانه، 📚 در حال مطالعه، 🛠 مشغول کار.
- متن پاسخ هر وضعیت از داخل ربات قابل ویرایش است.
- پنل مدیریت با دکمه‌های شیشه‌ای (Inline) فقط برای شناسه‌های عددیِ `ADMIN_IDS`.
- **وضعیت پیش‌فرض خاموش است**؛ تا مدیر یک وضعیت را فعال نکند چیزی ارسال نمی‌شود.
- پاسخ حداکثر یک‌بار در هر «نشست» و هر چت؛ فعال‌کردن دوبارهٔ همان وضعیت یا ویرایش متن، نشست را ریست نمی‌کند، اما خاموش‌ و روشن‌کردن نشست تازه می‌سازد.
- رعایت مجوزهای اتصال (`is_enabled` و `rights.can_reply`).
- ارسال با تضمین «حداکثر یک‌بار» (at-most-once): در حالت نامطمئن (مثلاً قطع شبکه حین ارسال) پیام دوباره ارسال نمی‌شود.
- ذخیره‌نکردن متن پیام مخاطبان؛ فقط شناسه‌ها و فراداده‌های تحویل نگه‌داری می‌شود.
- Long polling؛ هیچ پورت ورودی باز نمی‌شود.
- اجرا به‌صورت سرویس systemd با محدودیت‌های امنیتی.

## معماری

```
Telegram Bot API (HTTPS، long polling)
        │
   app/main.py ── Dispatcher (aiogram 3)
        │
        ├── handlers/admin.py      پنل مدیریت (فقط ADMIN_IDS)   ← keyboards/admin.py
        └── handlers/business.py   business_connection / business_message
                 │
                 └── services/auto_reply.py   موتور پاسخ خودکار
                          │
                     app/storage.py (Database)  ←  database_schema.sql
                          │
                   SQLite (aiosqlite)  /var/lib/ziko-assistant/ziko.sqlite3
```

| بخش | نقش |
|---|---|
| `app/config.py` | خواندن و اعتبارسنجی `.env` (بدون نمایش توکن در خطاها) |
| `app/storage.py`, `app/models.py` | لایهٔ SQLite و قرارداد داده (رجوع به [`docs/specs/MODULE1_CONTRACT.md`](docs/specs/MODULE1_CONTRACT.md)) |
| `app/services/auto_reply.py` | منطق نشست، یک‌بار پاسخ‌دادن و تصفیهٔ نتیجهٔ ارسال |
| `app/handlers/` | روترهای aiogram برای مدیریت و پیام‌های Business |
| `deploy/ziko.service` | واحد systemd |
| `scripts/` | `backup.sh`، `restore.sh`، `update.sh` |

جزئیات بیشتر: [`docs/reports/INTEGRATION_REPORT.md`](docs/reports/INTEGRATION_REPORT.md)، [`SECURITY.md`](SECURITY.md)، [`DEPLOYMENT.md`](DEPLOYMENT.md).

## پیش‌نیازها

- Ubuntu 24.04 و Python 3.12
- یک ربات تلگرام (از [@BotFather](https://t.me/BotFather)) با فعال‌بودن **Business Mode**
- حساب شخصی تلگرام با اشتراک **Telegram Premium** (شرط خود تلگرام برای قابلیت Business)
- شناسهٔ عددی تلگرام شما (مثلاً با ربات‌های نمایش‌دهندهٔ ID)

## نصب

### نصب یک‌خطی از GitHub

مخزن رسمی ZIKO برابر `soheilbahor/ziko-auto-reply-bot` است. این دستور
پیش‌نیازهای Ubuntu 24.04 را نصب می‌کند، آخرین Release را از GitHub دریافت می‌کند،
SHA-256 فایل انتشار را بررسی می‌کند و سپس نصب‌کنندهٔ تعاملی را اجرا می‌کند:

```bash
sudo apt-get update && sudo apt-get install -y python3.12 python3.12-venv curl ca-certificates tar age gh && bash -o pipefail -c 'curl -fsSL https://raw.githubusercontent.com/soheilbahor/ziko-auto-reply-bot/main/scripts/install-from-github.sh | sudo bash -s -- soheilbahor/ziko-auto-reply-bot'
```

نصب‌کننده توکن ربات را با ورودی مخفی می‌گیرد؛ توکن در خط فرمان، لاگ یا فایل systemd
قرار نمی‌گیرد. اگر فایل تنظیمات یا دیتابیس قبلاً وجود داشته باشد، بدون تأیید صریح
جایگزین نمی‌شود. اجرای نصب از GitHub به انتشار یک Release معتبر نیاز دارد. تا زمان انتشار Release نسخهٔ ۱.۲.۰، از بستهٔ ZIP آزمایشی و فقط در محیط تست استفاده کنید.

برای نصب از فایل Release دانلودشده نیز می‌توانید `install.sh` را از پوشهٔ استخراج‌شده
با `sudo ./install.sh` اجرا کنید. جزئیات مسیرها و rollback در [`DEPLOYMENT.md`](DEPLOYMENT.md) آمده است.

### مسیرهای اصلی

| مورد | مسیر |
|---|---|
| برنامه و محیط مجازی | `/opt/ziko-assistant` |
| فرمان CLI | `/usr/local/bin/ziko` |
| تنظیمات و توکن | `/etc/ziko-assistant/ziko.env` |
| پایگاه داده SQLite | `/var/lib/ziko-assistant/ziko.sqlite3` |
| پشتیبان‌ها | `/var/backups/ziko-assistant` |
| سرویس systemd | `/etc/systemd/system/ziko.service` |

## اتصال به Telegram Business

1. در BotFather: `/mybots` ← ربات شما ← **Bot Settings** ← **Business Mode** را فعال کنید.
2. ربات را با `python -m app.main` یا سرویس systemd اجرا کنید.
3. در حساب شخصی (Premium) در تلگرام: **Settings ← Telegram Business ← Chatbots** را باز کنید.
4. نام کاربری ربات را وارد کنید و مجوز **پاسخ‌دادن به پیام‌ها** (Reply to messages) را بدهید؛ چت‌هایی که ربات می‌تواند به آن‌ها پاسخ دهد را مشخص کنید.
5. از همان حساب شخصی به ربات پیام `/start` بدهید (شناسهٔ این حساب باید در `ADMIN_IDS` باشد).
6. در پنل، گزینهٔ «🔌 وضعیت اتصال» را بزنید؛ باید اتصال 🟢 فعال و مجوز پاسخ‌دادن نمایش داده شود.
7. یک وضعیت را انتخاب کنید تا پاسخ خودکار روشن و نشست جدید آغاز شود.

نام دقیق منوها ممکن است با نسخهٔ تلگرام کمی فرق کند؛ مرجع رسمی: <https://core.telegram.org/bots/api>.

## دستورات و CLI
پس از نصب، دستور `ziko` برای مدیریت سرویس در دسترس است؛ `ziko --help` فهرست فرمان‌ها را نشان می‌دهد.


### دستورات ربات

| دستور | کار |
|---|---|
| `/start` , `/menu` | باز کردن پنل مدیریت |
| `/cancel` | لغو عملیات جاری (مثلاً ویرایش متن) |

### مدیریت سرویس و اسکریپت‌ها

| دستور | کار |
|---|---|
| `sudo systemctl status ziko` | وضعیت سرویس |
| `sudo systemctl restart ziko` | راه‌اندازی مجدد |
| `journalctl -u ziko -f` | مشاهدهٔ زندهٔ لاگ |
| `sudo ZIKO_AGE_RECIPIENT=age1... ziko backup` | پشتیبان‌گیری رمزنگاری‌شده |
| `sudo ziko restore فایل.sqlite3.age` یا `فایل.tar.gz.age` | بازیابی دیتابیس یا کل پلتفرم |
| `sudo ziko update --repo soheilbahor/ziko-auto-reply-bot [--version vX.Y.Z]` | به‌روزرسانی از Release |
| `pytest -ra` | اجرای آزمون‌ها |

## به‌روزرسانی

```bash
# کانال پایدار؛ مخزن از deploy/release.conf خوانده می‌شود
sudo ziko update --channel stable

# نسخه مشخص
sudo ziko update --channel stable --version v1.2.1

# بررسی بدون اعمال تغییر
sudo ziko update --channel stable --check

# override اختیاری مخزن
sudo ziko update --repo soheilbahor/ziko-auto-reply-bot
```

Updater برای Release پایدار، SHA-256 و GitHub Artifact Attestation را بررسی می‌کند.
برای اجرای Update باید `gh` نصب باشد. پیش از Update، آرشیو کامل پلتفرم با `age`
رمزنگاری می‌شود؛ بنابراین `age` و متغیر `ZIKO_AGE_RECIPIENT` باید در محیط اجرای
فرمان در دسترس باشند. کلید خصوصی age باید جدا از محل Backup نگهداری شود.

در صورت خطا، مسیر Rollback کد، وابستگی‌ها و داده‌های پلتفرم را بازیابی می‌کند.
جزئیات تنظیم age و مسیرهای بازیابی در [`DEPLOYMENT.md`](DEPLOYMENT.md) آمده است.

## پشتیبان‌گیری و بازیابی

هم بکاپ مستقل دیتابیس (`.sqlite3.age`) و هم آرشیو کامل پلتفرم (`.tar.gz.age`)
با `age` رمزنگاری می‌شوند. آرشیو کامل شامل دیتابیس اصلی، رجیستری ربات‌های زیرمجموعه،
کلید رمزنگاری و دیتابیس‌های مستقل آن‌هاست.

```bash
# ایجاد بکاپ‌ها با کلید عمومی age
sudo ZIKO_AGE_RECIPIENT='age1...' ziko backup

# بازیابی دیتابیس مستقل
sudo ZIKO_AGE_RECIPIENT='age1...' ZIKO_AGE_IDENTITY=/etc/ziko-assistant/backup-age-identity ziko restore /var/backups/ziko-assistant/ziko-....sqlite3.age

# بازیابی کامل پلتفرم
sudo ZIKO_AGE_RECIPIENT='age1...' ZIKO_AGE_IDENTITY=/etc/ziko-assistant/backup-age-identity ziko restore /var/backups/ziko-assistant/ziko-platform-....tar.gz.age
```

کلید خصوصی age باید root-owned با مجوز `0400` یا `0600` باشد و در همان محل Backup
نگهداری نشود. فایل `.age` و فایل `.sha256` باید با هم نگهداری شوند. برای بازیابی
آرشیوهای قدیمی و بدون رمزنگاری، فقط در صورت اعتماد کامل به فایل، متغیر
`ZIKO_ALLOW_LEGACY_PLAINTEXT_BACKUP=1` را صریحاً تنظیم کنید. جزئیات در
[`DEPLOYMENT.md`](DEPLOYMENT.md) آمده است.



### Child-bot polling capacity

The default concurrent child-bot polling limit is 50, matching the default
global tenant registration capacity. Operators may set
`ZIKO_TENANT_MAX_CONCURRENT_POLLERS` to a lower positive integer to constrain
resource use. Enabled bots that cannot acquire a slot remain queued; `/tenants`
shows each bot's runtime polling state and reports active and waiting counts.
The application logs queue entry and slot acquisition without logging bot tokens.

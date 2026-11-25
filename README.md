# ZIKO — پلتفرم پاسخ‌گویی خودکار تلگرام

[![آخرین نسخه پایدار](https://img.shields.io/github/v/release/ziko-freedom/ziko-auto-reply-bot?label=stable)](https://github.com/ziko-freedom/ziko-auto-reply-bot/releases/latest)
[![CI](https://github.com/ziko-freedom/ziko-auto-reply-bot/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/ziko-freedom/ziko-auto-reply-bot/actions/workflows/ci.yml)
[![Release](https://github.com/ziko-freedom/ziko-auto-reply-bot/actions/workflows/release.yml/badge.svg)](https://github.com/ziko-freedom/ziko-auto-reply-bot/actions/workflows/release.yml)

**مخزن رسمی:** [ziko-freedom/ziko-auto-reply-bot](https://github.com/ziko-freedom/ziko-auto-reply-bot) · [نسخه‌ها](https://github.com/ziko-freedom/ziko-auto-reply-bot/releases) · [گزارش مشکل](https://github.com/ziko-freedom/ziko-auto-reply-bot/issues)

**فارسی** · [English](README.en.md)

ZIKO یک پلتفرم پاسخ‌گویی خودکار بر پایه **Telegram Bot API** و **Telegram Business** است. این پروژه Userbot نیست و برای کارکردن به ورود به حساب شخصی تلگرام یا MTProto نیاز ندارد.

## نصب سریع — آخرین نسخه پایدار

<badge color="success">Ubuntu 24.04</badge>

دستور زیر پیش‌نیازها را نصب می‌کند، GitHub CLI را از مخزن رسمی آن نصب می‌کند و نصب‌کننده ZIKO را اجرا می‌کند. نصب‌کننده آخرین Release پایدار (غیر prerelease) مخزن رسمی را از GitHub پیدا می‌کند؛ بنابراین لازم نیست شماره نسخه را دستی وارد کنید.

```bash
sudo apt-get update && sudo apt-get install -y ca-certificates curl gnupg python3.12 python3.12-venv tar age && sudo install -d -m 755 /etc/apt/keyrings && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | sudo tee /etc/apt/keyrings/githubcli-archive-keyring.gpg >/dev/null && sudo chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | sudo tee /etc/apt/sources.list.d/github-cli.list >/dev/null && sudo apt-get update && sudo apt-get install -y gh && bash -o pipefail -c 'curl -fsSL --retry 3 --proto "=https" --tlsv1.2 https://raw.githubusercontent.com/ziko-freedom/ziko-auto-reply-bot/main/scripts/install-from-github.sh | sudo bash -s -- ziko-freedom/ziko-auto-reply-bot'
```

**مهم:** این دستور برای نصب تازه است. اگر ZIKO از قبل نصب است، برای حفظ اطلاعات کاربران از دستور Update استفاده کنید و نصب تازه را اجرا نکنید.

نصب‌کننده:
- آخرین Release پایدار را از GitHub API پیدا می‌کند.
- Asset واقعی Release را از API انتخاب می‌کند؛ نام فایل را حدس نمی‌زند.
- وجود `SHA256SUMS` و تطابق SHA-256 را بررسی می‌کند.
- ساختار آرشیو و شماره نسخه داخلی را اعتبارسنجی می‌کند.
- GitHub Artifact Attestation را بررسی می‌کند.
- فقط پس از موفقیت بررسی‌ها، نصب‌کننده تعاملی را اجرا می‌کند.

برای انتشارهای استاندارد، نام Asset باید `ziko-assistant-vX.Y.Z.tar.gz` یا `ziko-assistant-vX.Y.Z.zip` باشد. نام ZIP فعلی `ziko-assistant-1.4.1-version-fixed.zip` نیز به‌عنوان نام سازگاری پذیرفته می‌شود؛ اما همچنان به `SHA256SUMS` و Attestation معتبر نیاز دارد. فایل ZIP به‌تنهایی برای نصب قابل اعتماد کافی نیست.

## قابلیت‌ها

- اتصال ربات به Telegram Business با مجوزهایی که مالک حساب داده است.
- شش وضعیت آماده: خواب، آشپزی، بازی، بیرون از خانه، مطالعه و مشغول کار.
- ویرایش متن پاسخ هر وضعیت از داخل ربات.
- فعال و غیرفعال کردن پاسخ خودکار از پنل مدیریت.
- جلوگیری از پاسخ تکراری؛ ارسال‌های نامطمئن به‌صورت خودکار تکرار نمی‌شوند.
- ساخت ربات فرزند یا اتصال ربات موجود از طریق ربات مستر.
- دیتابیس و تنظیمات مستقل برای هر ربات فرزند.
- اجرای سرویس با systemd و ثبت لاگ عملیاتی.

**توجه:** پاسخ‌ها بر اساس وضعیت و متن تنظیم‌شده ارسال می‌شوند؛ این قابلیت به‌معنی پاسخ‌گویی هوشمند یا تولید متن با هوش مصنوعی نیست.

## پیش‌نیازها

- Ubuntu 24.04
- Python 3.12
- ربات ساخته‌شده با [@BotFather](https://t.me/BotFather) و Business Mode فعال
- حساب تلگرام واجد شرایط Telegram Business
- شناسه عددی مدیر تلگرام

## اتصال Telegram Business

1. در BotFather از مسیر `/mybots` ربات را انتخاب و Business Mode را فعال کنید.
2. سرویس ZIKO را اجرا کنید.
3. در تلگرام مالک به **Settings → Telegram Business → Chatbots** بروید.
4. ربات را اضافه کنید و مجوز پاسخ‌گویی را فقط برای چت‌های موردنظر بدهید.
5. از حساب مالک به ربات `/start` بفرستید؛ شناسه مالک باید در `ADMIN_IDS` تنظیم شده باشد.
6. وضعیت اتصال و مجوز پاسخ‌گویی را در پنل بررسی کنید.
7. یک وضعیت را فعال و متن پاسخ آن را ویرایش کنید.

مرجع رسمی: [Telegram Bot API](https://core.telegram.org/bots/api).

## ربات‌های فرزند

- از ربات مستر برای ساخت ربات یا اتصال توکن ربات موجود استفاده کنید.
- مالک هر ربات فرزند باید همان ربات را در تنظیمات Telegram Business حساب خود متصل کند و مجوز پاسخ‌گویی بدهد.
- دیتابیس و تنظیمات هر ربات فرزند مستقل است.
- `/tenants` وضعیت ربات‌های ثبت‌شده را برای مدیر پلتفرم نمایش می‌دهد.
- غیرفعال کردن ربات، اطلاعات آن را حذف نمی‌کند.

## دستورات پرکاربرد

### داخل تلگرام

| دستور | کاربرد |
|---|---|
| `/start` یا `/menu` | بازکردن پنل |
| `/cancel` | لغو عملیات جاری |
| `/create_bot` | ساخت ربات فرزند |
| `/connect_bot` | اتصال ربات موجود |

### روی سرور

| دستور | کاربرد |
|---|---|
| `sudo ziko` | بازکردن منوی CLI |
| `sudo ziko status` | وضعیت سرویس |
| `sudo ziko doctor` | بررسی سلامت و عیب‌یابی |
| `sudo ziko bots list` | فهرست ربات‌های فرزند |
| `sudo systemctl status ziko` | وضعیت سرویس systemd |
| `sudo systemctl restart ziko` | راه‌اندازی مجدد |
| `sudo journalctl -u ziko -f` | مشاهده زنده لاگ |

## به‌روزرسانی به آخرین نسخه پایدار

بررسی نسخه جدید بدون نصب:

```bash
sudo ziko update --channel stable --check
```

به‌روزرسانی:

```bash
sudo ZIKO_AGE_RECIPIENT='age1...' ziko update --channel stable
```

`age1...` را با کلید عمومی واقعی age جایگزین کنید. Updater قبل از تغییر فایل‌های برنامه، بکاپ رمزنگاری‌شده پلتفرم می‌گیرد، Asset را از GitHub API پیدا می‌کند و checksum، ساختار آرشیو، نسخه داخلی و Attestation را بررسی می‌کند.

برای نصب نسخه مشخص:

```bash
sudo ZIKO_AGE_RECIPIENT='age1...' ziko update --channel stable --version vX.Y.Z
```

کلید خصوصی age را خارج از سرور و جدا از محل بکاپ نگه دارید. راهنمای کامل: [راهنمای بکاپ و آپدیت](docs/UPDATE_AND_BACKUP.md) و [استقرار](DEPLOYMENT.md).

## بکاپ و بازیابی

ساخت بکاپ رمزنگاری‌شده:

```bash
sudo ZIKO_AGE_RECIPIENT='age1...' ziko backup
```

فایل بکاپ و فایل checksum آن را با هم نگه دارید. برای بازیابی، کلید خصوصی age متناظر لازم است. پیش از ارتقا یا بازیابی، یک نسخه بکاپ خارج از سرور نیز نگه دارید.

## مسیرهای مهم

| مورد | مسیر |
|---|---|
| برنامه | `/opt/ziko-assistant` |
| تنظیمات و توکن | `/etc/ziko-assistant/ziko.env` |
| دیتابیس اصلی | `/var/lib/ziko-assistant/ziko.sqlite3` |
| دیتابیس ربات‌های فرزند | `/var/lib/ziko-assistant/tenants/` |
| بکاپ‌ها | `/var/backups/ziko-assistant/` |
| سرویس systemd | `/etc/systemd/system/ziko.service` |

## عیب‌یابی

```bash
sudo ziko doctor
sudo systemctl status ziko --no-pager
sudo journalctl -u ziko -n 200 --no-pager
```

در صورت شکست نصب یا آپدیت، متن کامل خطا و لاگ را نگه دارید. برای رفع خطا، پوشه داده‌ها یا دیتابیس‌ها را دستی حذف نکنید.

## توسعه و تست

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
python -m compileall -q app tests
ruff check .
pytest -ra
```

گزارش‌های فنی در پوشه [`docs/`](docs/) قرار دارند. تست‌هایی که به توکن واقعی یا حساب Telegram Business نیاز دارند باید جداگانه در محیط آزمایشی اجرا شوند.

## امنیت

- توکن ربات و کلیدهای خصوصی را در Git یا لاگ عمومی قرار ندهید.
- فایل تنظیمات و دیتابیس را منتشر نکنید.
- مجوز Telegram Business را فقط به چت‌های لازم بدهید.
- کلید خصوصی age را جدا از بکاپ‌ها نگه دارید.
- برای گزارش آسیب‌پذیری، [SECURITY.md](SECURITY.md) را ببینید.

---

[راهنمای نصب](DEPLOYMENT.md) · [بکاپ و آپدیت](docs/UPDATE_AND_BACKUP.md) · [لاگ‌ها](docs/LOGGING.md) · [تغییرات نسخه](CHANGELOG.md)

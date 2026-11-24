"""ZIKO command line manager (``ziko``).

Service control uses systemctl; backup/restore/update/uninstall are delegated to the
scripts in ``<ZIKO_HOME>/scripts/``. This module never touches the Telegram engine or the
database contract, and it never prints the bot token: secrets are masked in ``config`` and
redacted from every command output that passes through it.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path

HOME = Path(os.environ.get("ZIKO_HOME", "/opt/ziko-assistant"))
SERVICE = os.environ.get("ZIKO_SERVICE", "ziko")
SERVICE_USER = "ziko"

_TOKEN_RE = re.compile(r"\d{6,}:[A-Za-z0-9_-]{20,}")
_TOKEN_FORMAT = re.compile(r"[0-9]+:[A-Za-z0-9_-]{20,}")
_SQLITE_MAGIC = b"SQLite format 3\x00"
_LOG_LEVELS = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}
_UI = None
_known_secrets: list[str] | None = None


# --------------------------------------------------------------------------- helpers
def _version() -> str:
    try:
        from app import __version__

        return __version__
    except Exception:  # noqa: BLE001 - version must never break the CLI
        return "unknown"


def _ui():
    """Import the Rich layer lazily so ``--help`` works even without Rich."""
    global _UI
    if _UI is None:
        try:
            from app import cli_menu
        except ModuleNotFoundError as exc:
            if (exc.name or "").split(".")[0] != "rich":
                raise
            print("Rich is not installed. Run: "
                  f"{HOME}/.venv/bin/pip install 'rich>=13.7'", file=sys.stderr)
            raise SystemExit(1) from None
        _UI = cli_menu
    return _UI


def _env_path() -> Path:
    return Path(os.environ.get("ZIKO_ENV_FILE", "/etc/ziko-assistant/ziko.env"))


def _scripts_dir() -> Path:
    return HOME / "scripts"


def _read_env() -> tuple[dict[str, str] | None, str | None]:
    """Return (values, problem). problem is None, 'missing' or 'denied'."""
    path = _env_path()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, "missing"
    except (PermissionError, OSError, UnicodeDecodeError):
        return None, "denied"
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, sep, value = line.partition("=")
        if not sep or not key.strip():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values, None


def redact(text: str) -> str:
    """Remove anything that looks like (or is) the bot token."""
    global _known_secrets
    if _known_secrets is None:
        values, _ = _read_env()
        token = (values or {}).get("BOT_TOKEN", "")
        _known_secrets = [token] if len(token) >= 8 else []
    for secret in _known_secrets:
        text = text.replace(secret, "[REDACTED]")
    return _TOKEN_RE.sub("[REDACTED]", text)


def _privileged(cmd: list[str]) -> list[str]:
    if os.geteuid() == 0 or not shutil.which("sudo"):
        return cmd
    return ["sudo", *cmd]


def _as_service_user(cmd: list[str]) -> list[str]:
    import pwd

    if pwd.getpwuid(os.geteuid()).pw_name == SERVICE_USER:
        return cmd
    if os.geteuid() == 0:
        return ["runuser", "-u", SERVICE_USER, "--", *cmd]
    return ["sudo", "-u", SERVICE_USER, *cmd] if shutil.which("sudo") else cmd


def _capture(cmd: list[str], timeout: float = 15) -> tuple[int, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                              timeout=timeout, check=False)
    except FileNotFoundError:
        return 127, ""
    except subprocess.TimeoutExpired:
        return 124, ""
    return proc.returncode, redact((proc.stdout or "") + (proc.stderr or "")).strip()


def _stream(cmd: list[str]) -> int:
    """Run a command and print its output line by line with secrets redacted."""
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, errors="replace", bufsize=1)
    except FileNotFoundError:
        _ui().error(f"دستور پیدا نشد: {cmd[0]}")
        return 127
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            sys.stdout.write(redact(line))
            sys.stdout.flush()
        return proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        proc.wait()
        return 130


def _inherit(cmd: list[str]) -> int:
    try:
        return subprocess.run(cmd, check=False).returncode
    except FileNotFoundError:
        _ui().error(f"دستور پیدا نشد: {cmd[0]}")
        return 127


def _script(name: str) -> Path | None:
    path = _scripts_dir() / name
    if path.is_file():
        return path
    _ui().error(f"اسکریپت پیدا نشد: {path}")
    return None


def _human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


def _human_duration(seconds: float) -> str:
    s = int(seconds)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m = s // 60
    parts = [f"{d} روز" if d else "", f"{h} ساعت" if h else "", f"{m} دقیقه" if m and not d else ""]
    parts = [p for p in parts if p]
    return " و ".join(parts[:2]) or "کمتر از یک دقیقه"


# ----------------------------------------------------------------------- systemd info
def service_info() -> dict[str, str]:
    rc, out = _capture(["systemctl", "show", SERVICE, "-p",
                        "LoadState,ActiveState,SubState,UnitFileState,MainPID,"
                        "ActiveEnterTimestampMonotonic,NRestarts,MemoryCurrent"])
    info: dict[str, str] = {}
    if rc == 0:
        for line in out.splitlines():
            key, _, value = line.partition("=")
            info[key] = value
    return info


def service_state(info: dict[str, str] | None = None) -> str:
    info = service_info() if info is None else info
    if not info:
        return "unknown"
    if info.get("LoadState") == "not-found":
        return "not-found"
    return info.get("ActiveState") or "unknown"


def _db_path(values: dict[str, str] | None) -> Path:
    raw = ((values or {}).get("DATABASE_PATH") or "/var/lib/ziko-assistant/ziko.sqlite3").strip()
    path = Path(raw).expanduser()
    return path if path.is_absolute() else HOME / path


def _last_backup() -> tuple[float | None, str | None]:
    """Return (mtime, problem). problem: None, 'none' or 'denied'."""
    try:
        files = list(Path(os.environ.get("ZIKO_BACKUP_DIR", "/var/backups/ziko-assistant")).glob("ziko-*.sqlite3"))
        times = [f.stat().st_mtime for f in files]
    except PermissionError:
        return None, "denied"
    except OSError:
        return None, "none"
    return (max(times), None) if times else (None, "none")


# ------------------------------------------------------------------------- commands
def cmd_status() -> int:
    ui = _ui()
    info = service_info()
    state = service_state(info)
    values, env_problem = _read_env()
    rows: list[tuple[str, object]] = [("سرویس", ui.indicator(state))]

    boot = info.get("UnitFileState", "")
    if boot:
        rows.append(("اجرا هنگام بوت", "فعال" if boot == "enabled" else boot))
    mono = info.get("ActiveEnterTimestampMonotonic", "0")
    if state == "active" and mono.isdigit() and int(mono) > 0:
        rows.append(("مدت فعالیت", _human_duration(time.monotonic() - int(mono) / 1e6)))
    if info.get("MainPID", "0") not in {"", "0"}:
        rows.append(("PID", info["MainPID"]))
    mem = info.get("MemoryCurrent", "")
    if mem.isdigit() and int(mem) < 2**63:
        rows.append(("حافظه", _human_size(int(mem))))
    if info.get("NRestarts", "0") not in {"", "0"}:
        rows.append(("تعداد ری‌استارت", info["NRestarts"]))

    if env_problem == "missing":
        rows.append(("تنظیمات", "فایل .env پیدا نشد"))
    elif env_problem == "denied":
        rows.append(("تنظیمات", "نیاز به دسترسی (با sudo اجرا کنید)"))
    else:
        token = (values or {}).get("BOT_TOKEN", "")
        good = bool(_TOKEN_FORMAT.fullmatch(token))
        rows.append(("توکن ربات", "تنظیم شده" if good else "تنظیم نشده یا نامعتبر"))
    try:
        size = _db_path(values).stat().st_size
        rows.append(("پایگاه داده", _human_size(size)))
    except FileNotFoundError:
        rows.append(("پایگاه داده", "هنوز ساخته نشده"))
    except PermissionError:
        rows.append(("پایگاه داده", "نیاز به دسترسی"))
    mtime, problem = _last_backup()
    if mtime:
        rows.append(("آخرین پشتیبان", f"{_human_duration(time.time() - mtime)} پیش"))
    else:
        rows.append(("آخرین پشتیبان", "نیاز به دسترسی" if problem == "denied" else "ندارد"))
    rows.append(("نسخه", _version()))
    ui.render_status(rows)  # type: ignore[arg-type]
    return 0 if state == "active" else 3


def _wait_for(target: str, timeout: float = 12.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if service_state() == target:
            if target != "active":
                return True
            time.sleep(1.5)  # guard against an immediate crash loop
            return service_state() == "active"
        time.sleep(0.5)
    return False


def _systemctl_action(action: str) -> int:
    ui = _ui()
    if not shutil.which("systemctl"):
        ui.error("systemctl روی این سیستم در دسترس نیست.")
        return 1
    labels = {"start": "شروع", "stop": "توقف", "restart": "راه‌اندازی مجدد"}
    state = service_state()
    if state == "not-found":
        ui.error("سرویس ziko نصب نشده است.")
        return 1
    if action == "start" and state == "active":
        ui.info("سرویس از قبل فعال است.")
        return 0
    if action == "stop" and state in {"inactive", "failed"}:
        ui.info("سرویس از قبل متوقف است.")
        return 0
    rc = _inherit(_privileged(["systemctl", action, SERVICE]))
    if rc != 0:
        ui.error(f"{labels[action]} سرویس ناموفق بود.")
        return rc
    target = "inactive" if action == "stop" else "active"
    with ui.console.status(f"{labels[action]} سرویس..."):
        reached = _wait_for(target)
    if reached:
        ui.ok(f"سرویس {'متوقف شد' if action == 'stop' else 'فعال است'}.")
        return 0
    ui.error("سرویس به وضعیت مورد انتظار نرسید. برای بررسی: ziko logs")
    return 1


def cmd_start() -> int:
    return _systemctl_action("start")


def cmd_stop() -> int:
    return _systemctl_action("stop")


def cmd_restart() -> int:
    return _systemctl_action("restart")


def _can_read_journal() -> bool:
    if os.geteuid() == 0:
        return True
    import grp

    names = set()
    for gid in os.getgroups():
        try:
            names.add(grp.getgrgid(gid).gr_name)
        except KeyError:
            pass
    return bool(names & {"adm", "systemd-journal"})


def cmd_logs(lines: int = 100, follow: bool = False) -> int:
    cmd = ["journalctl", "-u", SERVICE, "-n", str(max(1, lines)), "--no-pager", "-o", "short-iso"]
    if follow:
        cmd.append("-f")
    if not _can_read_journal():
        cmd = _privileged(cmd)
    rc = _stream(cmd)
    return 0 if rc == 130 else rc


def cmd_backup() -> int:
    ui = _ui()
    script = _script("backup.sh")
    if not script:
        return 1
    rc = _stream(_as_service_user(["bash", str(script)]))
    if rc == 0:
        ui.ok("پشتیبان‌گیری انجام شد.")
    else:
        ui.error("پشتیبان‌گیری ناموفق بود.")
    return rc


def cmd_restore(file: str, yes: bool = False) -> int:
    ui = _ui()
    path = Path(file).expanduser().resolve()
    if not path.is_file():
        ui.error(f"فایل پشتیبان پیدا نشد: {path}")
        return 2
    try:
        with path.open("rb") as fh:
            if fh.read(16) != _SQLITE_MAGIC:
                ui.error("این فایل یک پایگاه‌داده SQLite معتبر نیست.")
                return 2
    except PermissionError:
        ui.warn("امکان بررسی فایل نیست (دسترسی ندارید)؛ اسکریپت بازیابی بررسی می‌کند.")
    script = _script("restore.sh")
    if not script:
        return 1
    ui.warn("پایگاه داده فعلی جایگزین می‌شود (یک نسخه .pre-restore ذخیره می‌ماند) "
            "و سرویس چند ثانیه متوقف خواهد شد.")
    if not yes and not ui.confirm(f"بازیابی از {path.name} انجام شود؟"):
        ui.info("لغو شد.")
        return 1
    rc = _stream(_privileged(["bash", str(script), *(["--yes"] if yes else []), str(path)]))
    if rc != 0:
        ui.error("بازیابی ناموفق بود.")
        return rc
    ui.ok("بازیابی انجام شد.")
    return 0 if _wait_for("active") else 1


def cmd_update(yes: bool = False, repo: str | None = None, version: str | None = None) -> int:
    ui = _ui()
    script = _script("update.sh")
    if not script:
        return 1
    if not yes and not ui.confirm("ZIKO به‌روزرسانی شود؟", default=True):
        ui.info("لغو شد.")
        return 1
    command = ["bash", str(script)]
    if repo:
        command += ["--repo", repo]
    if version:
        command += ["--version", version]
    rc = _inherit(_privileged(command))
    if rc != 0:
        ui.error("به‌روزرسانی ناموفق بود.")
        return rc
    ui.ok("به‌روزرسانی انجام شد.")
    return cmd_status()


def cmd_uninstall(yes: bool = False) -> int:
    ui = _ui()
    script = _script("uninstall.sh")
    if not script:
        return 1
    ui.warn("با حذف ZIKO سرویس متوقف می‌شود و داده‌ها طبق اسکریپت حذف پاک می‌شوند.")
    if not yes:
        if ui.confirm("پیش از حذف، پشتیبان‌گیری شود؟", default=True) and cmd_backup() != 0:
            ui.error("حذف متوقف شد زیرا پشتیبان‌گیری ناموفق بود.")
            return 1
        if ui.ask("برای تأیید کلمه uninstall را تایپ کنید") != "uninstall":
            ui.info("لغو شد.")
            return 1
    return _inherit(_privileged(["bash", str(script), *(["--yes"] if yes else [])]))


def _config_rows(values: dict[str, str]) -> list[tuple[str, object]]:
    ui = _ui()
    token = values.get("BOT_TOKEN", "")
    if not token:
        token_cell = "تنظیم نشده"
    else:
        good = bool(_TOKEN_FORMAT.fullmatch(token))
        token_cell = "•••••••• (تنظیم شده" + ("، فرمت معتبر)" if good else "، فرمت نامعتبر!)")
    ids = [x for x in re.split(r"[,;\s]+", values.get("ADMIN_IDS", "")) if x]
    rows: list[tuple[str, object]] = [
        ("BOT_TOKEN", token_cell),
        ("ADMIN_IDS", f"{len(ids)} مدیر" if ids else "تنظیم نشده"),
        ("DATABASE_PATH", values.get("DATABASE_PATH") or "/var/lib/ziko-assistant/ziko.sqlite3 (پیش‌فرض)"),
        ("LOG_LEVEL", values.get("LOG_LEVEL") or "INFO (پیش‌فرض)"),
    ]
    for key in values:
        if key not in {"BOT_TOKEN", "ADMIN_IDS", "DATABASE_PATH", "LOG_LEVEL"}:
            rows.append((key, "تنظیم شده (مقدار مخفی)"))
    return rows


def cmd_config(edit: bool = False) -> int:
    ui = _ui()
    if edit:
        if os.geteuid() == 0:
            editor = os.environ.get("EDITOR") or ("nano" if shutil.which("nano") else "vi")
            rc = _inherit([editor, str(_env_path())])
        else:
            rc = _inherit(["sudoedit", str(_env_path())])
        if rc == 0:
            ui.ok("ذخیره شد. برای اعمال تغییرات: ziko restart")
        return rc
    values, problem = _read_env()
    if problem == "missing":
        ui.error(f"فایل تنظیمات پیدا نشد: {_env_path()}")
        return 1
    if problem == "denied":
        ui.error("اجازه خواندن .env را ندارید. با sudo اجرا کنید: sudo ziko config")
        return 1
    ui.render_config(str(_env_path()), _config_rows(values or {}))  # type: ignore[arg-type]
    try:
        mode = _env_path().stat().st_mode & 0o777
        if mode & 0o027:
            ui.warn(f"دسترسی فایل تنظیمات برابر {mode:o} است؛ باید root:ziko با حالت 640 باشد.")
    except OSError:
        pass
    ui.info("برای ویرایش: ziko config --edit  (توکن هیچ‌وقت نمایش داده نمی‌شود)")
    return 0


def run_checks() -> list[tuple[str, str, str]]:
    checks: list[tuple[str, str, str]] = []

    def add(level: str, title: str, detail: str) -> None:
        checks.append((level, title, detail))

    v = sys.version_info
    add("ok" if v[:2] == (3, 12) else "fail", "نسخه Python",
        f"{v.major}.{v.minor}.{v.micro}" + ("" if v[:2] == (3, 12) else " (نیاز به Python 3.12)"))
    add("ok" if HOME.is_dir() else "fail", "پوشه برنامه", str(HOME))
    venv_py = HOME / ".venv" / "bin" / "python"
    add("ok" if os.access(venv_py, os.X_OK) else "fail", "محیط مجازی (venv)", str(venv_py))

    values, problem = _read_env()
    if problem == "missing":
        add("fail", "فایل .env", "پیدا نشد")
    elif problem == "denied":
        add("warn", "فایل .env", "دسترسی خواندن ندارید؛ با sudo اجرا کنید")
    else:
        mode = _env_path().stat().st_mode & 0o777
        add("ok" if not mode & 0o027 else "warn", "دسترسی تنظیمات",
            f"{mode:o}" + ("" if not mode & 0o027 else " (باید root:ziko با حالت 640 باشد)"))
        values = values or {}
        token = values.get("BOT_TOKEN", "")
        if not token or token.startswith("000000000:REPLACE"):
            add("fail", "توکن ربات", "تنظیم نشده یا هنوز مقدار نمونه است")
        elif not _TOKEN_FORMAT.fullmatch(token):
            add("fail", "توکن ربات", "فرمت نامعتبر")
        else:
            add("ok", "توکن ربات", "تنظیم شده (مخفی)")
        ids = [x for x in re.split(r"[,;\s]+", values.get("ADMIN_IDS", "")) if x]
        good_ids = bool(ids) and all(x.isascii() and x.isdigit() and int(x) > 0 for x in ids)
        add("ok" if good_ids else "fail", "ADMIN_IDS",
            f"{len(ids)} مدیر" if good_ids else "باید حداقل یک شناسه عددی معتبر باشد")
        level = (values.get("LOG_LEVEL") or "INFO").upper()
        add("ok" if level in _LOG_LEVELS else "fail", "LOG_LEVEL", level)

    db = _db_path(values)
    try:
        with db.open("rb") as fh:
            header_ok = fh.read(16) == _SQLITE_MAGIC
        mode = db.stat().st_mode & 0o777
        if not header_ok:
            add("fail", "پایگاه داده", "هدر SQLite معتبر نیست")
        elif mode & 0o077:
            add("warn", "پایگاه داده", f"دسترسی {mode:o} است؛ باید 600 باشد")
        else:
            add("ok", "پایگاه داده", _human_size(db.stat().st_size))
    except FileNotFoundError:
        add("warn", "پایگاه داده", "هنوز ساخته نشده (با اولین اجرا ساخته می‌شود)")
    except PermissionError:
        add("warn", "پایگاه داده", "دسترسی ندارید؛ با sudo اجرا کنید")

    if not shutil.which("systemctl"):
        add("fail", "systemd", "systemctl پیدا نشد")
    else:
        info = service_info()
        state = service_state(info)
        if state == "not-found":
            add("fail", "سرویس ziko", "نصب نشده")
        else:
            add("ok" if state == "active" else "fail", "سرویس ziko", state)
            enabled = info.get("UnitFileState") == "enabled"
            add("ok" if enabled else "warn", "اجرا هنگام بوت", info.get("UnitFileState", "?"))
            if info.get("NRestarts", "0") not in {"", "0"}:
                add("warn", "ری‌استارت‌ها", f"{info['NRestarts']} بار از آخرین شروع")

    for name, level in (("backup.sh", "fail"), ("restore.sh", "fail"),
                        ("update.sh", "warn"), ("uninstall.sh", "warn")):
        found = (_scripts_dir() / name).is_file()
        add("ok" if found else level, f"اسکریپت {name}", "موجود" if found else "پیدا نشد")

    try:
        socket.create_connection(("api.telegram.org", 443), timeout=5).close()
        add("ok", "اتصال به تلگرام", "api.telegram.org:443")
    except OSError:
        add("warn", "اتصال به تلگرام", "اتصال برقرار نشد (شبکه/فایروال را بررسی کنید)")

    try:
        free = shutil.disk_usage(HOME if HOME.is_dir() else "/").free
        level = "fail" if free < 100 * 2**20 else "warn" if free < 500 * 2**20 else "ok"
        add(level, "فضای دیسک", f"{_human_size(free)} آزاد")
    except OSError:
        add("warn", "فضای دیسک", "نامشخص")

    mtime, problem = _last_backup()
    if mtime is None:
        add("warn", "پشتیبان‌گیری", "دسترسی ندارید" if problem == "denied" else "هنوز پشتیبانی گرفته نشده")
    else:
        age = time.time() - mtime
        add("ok" if age < 7 * 86400 else "warn", "پشتیبان‌گیری", f"{_human_duration(age)} پیش")
    return checks


def cmd_doctor() -> int:
    ui = _ui()
    with ui.console.status("در حال بررسی..."):
        checks = run_checks()
    ui.render_checks(checks)
    return 1 if any(c[0] == "fail" for c in checks) else 0


def cmd_version() -> int:
    try:
        ui = _ui()
    except SystemExit:
        print(f"ZIKO {_version()}")
        return 0
    ui.print_logo(_version())
    ui.console.print(f"Python {sys.version.split()[0]}  ·  {HOME}")
    return 0


# ---------------------------------------------------------------------------- parser
def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ziko",
        description="ZIKO - مدیریت دستیار شخصی تلگرام. بدون دستور، منوی تعاملی باز می‌شود.",
    )
    p.add_argument("-V", "--version", action="store_true", help="نمایش نسخه")
    sub = p.add_subparsers(dest="command", metavar="COMMAND")
    sub.add_parser("status", help="وضعیت سرویس و سلامت کلی")
    sub.add_parser("start", help="شروع سرویس")
    sub.add_parser("stop", help="توقف سرویس")
    sub.add_parser("restart", help="راه‌اندازی مجدد سرویس")
    logs = sub.add_parser("logs", help="مشاهده لاگ‌ها (توکن مخفی می‌شود)")
    logs.add_argument("-n", "--lines", type=int, default=100, help="تعداد خطوط (پیش‌فرض ۱۰۰)")
    logs.add_argument("-f", "--follow", action="store_true", help="دنبال کردن لاگ زنده")
    upd = sub.add_parser("update", help="به‌روزرسانی (scripts/update.sh)")
    upd.add_argument("-y", "--yes", action="store_true", help="بدون پرسش تأیید")
    upd.add_argument("--repo", help="مخزن GitHub به‌شکل OWNER/REPOSITORY")
    upd.add_argument("--version", help="برچسب نسخه مانند v1.2.3")
    sub.add_parser("backup", help="پشتیبان‌گیری از پایگاه داده (scripts/backup.sh)")
    res = sub.add_parser("restore", help="بازیابی از فایل پشتیبان (scripts/restore.sh)")
    res.add_argument("file", metavar="FILE", help="مسیر فایل پشتیبان .sqlite3")
    res.add_argument("-y", "--yes", action="store_true", help="بدون پرسش تأیید")
    cfg = sub.add_parser("config", help="نمایش تنظیمات (توکن هرگز نمایش داده نمی‌شود)")
    cfg.add_argument("--edit", action="store_true", help="ویرایش فایل .env")
    sub.add_parser("doctor", help="عیب‌یابی و بررسی سلامت نصب")
    sub.add_parser("version", help="نمایش نسخه")
    un = sub.add_parser("uninstall", help="حذف ZIKO (scripts/uninstall.sh)")
    un.add_argument("-y", "--yes", action="store_true", help="بدون پرسش تأیید")
    return p


def _interactive() -> int:
    ui = _ui()

    def restore() -> int:
        file = ui.ask("مسیر فایل پشتیبان (خالی = لغو)")
        return cmd_restore(file) if file else 0

    def config() -> int:
        rc = cmd_config()
        if rc == 0 and ui.confirm("فایل تنظیمات ویرایش شود؟"):
            return cmd_config(edit=True)
        return rc

    actions = {
        "status": cmd_status, "start": cmd_start, "stop": cmd_stop, "restart": cmd_restart,
        "logs": lambda: cmd_logs(50, follow=True), "backup": cmd_backup, "restore": restore,
        "update": cmd_update, "config": config, "doctor": cmd_doctor,
        "version": cmd_version, "uninstall": cmd_uninstall,
    }
    return ui.run_menu(actions, service_state, _version())


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.version or args.command == "version":
            return cmd_version()
        if args.command is None:
            if sys.stdin.isatty() and sys.stdout.isatty():
                return _interactive()
            parser.print_help()
            return 0
        c = args.command
        if c == "logs":
            return cmd_logs(args.lines, args.follow)
        if c == "restore":
            return cmd_restore(args.file, args.yes)
        if c == "config":
            return cmd_config(args.edit)
        if c == "update":
            return cmd_update(args.yes, args.repo, args.version)
        if c == "uninstall":
            return cmd_uninstall(args.yes)
        return {"status": cmd_status, "start": cmd_start, "stop": cmd_stop,
                "restart": cmd_restart, "backup": cmd_backup, "doctor": cmd_doctor}[c]()
    except KeyboardInterrupt:
        print()
        return 130


if __name__ == "__main__":
    sys.exit(main())

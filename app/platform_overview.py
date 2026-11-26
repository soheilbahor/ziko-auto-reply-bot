"""Platform overview for the master bot's admin panel (pure logic: no aiogram).

For every registered child bot it combines three read-only sources:

* registry row   -- enabled flag, owner, username (tokens are never read)
* tenant DB      -- the same readiness gates the owner sees in /diagnose
                    (app.panel_health), opened ``mode=ro`` so a live poller is never blocked
* live telemetry -- in-process request counters (app.telegram_stats.STATS)

and classifies each bot into one state that an administrator can act on.
Everything here is synchronous and file-bound; handlers call ``collect`` through
``asyncio.to_thread`` so the event loop never blocks on SQLite.
"""
from __future__ import annotations

import html
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app import panel_health

PAGE_SIZE = 8
SILENT_AFTER = 180  # seconds without a successful API call while polling

# State -> (sort priority, icon, Persian label). Lower priority is shown first.
STATES: dict[str, tuple[int, str, str]] = {
    "crashed":   (0, "🟥", "پولینگ متوقف یا خطادار"),
    "token":     (1, "🔑", "توکن رد شده"),
    "blocked":   (2, "⛔", "پاسخ ارسال نمی‌شود"),
    "warning":   (3, "⚠️", "کار می‌کند، با هشدار"),
    "queued":    (4, "⏳", "در صف پولینگ"),
    "ready":     (5, "✅", "آماده"),
    "disabled":  (6, "⏸", "غیرفعال توسط مدیر"),
}


@dataclass(frozen=True, slots=True)
class _Row:
    """Duck-typed stand-ins for the storage models that panel_health expects."""
    values: dict[str, Any]

    def __getattr__(self, name: str) -> Any:
        try:
            return self.values[name]
        except KeyError:
            raise AttributeError(name) from None


@dataclass(slots=True)
class BotView:
    bot_id: int
    user_id: int
    username: str
    enabled: bool
    runtime: str  # running / waiting / starting / stopped
    state: str = "ready"
    reasons: list[str] = field(default_factory=list)        # Persian, Telegram only
    reason_codes: list[str] = field(default_factory=list)   # English, CLI/logs/JSON
    diagnosis: panel_health.Diagnosis | None = None
    telemetry: dict[str, Any] | None = None
    stats: dict[str, int] = field(default_factory=dict)
    db_error: str | None = None

    @property
    def icon(self) -> str:
        return STATES[self.state][1]


def tenant_db_path(data_dir: Path, user_id: int, bot_id: int, is_primary: bool) -> Path:
    """Same rule as TenantBotManager._run_tenant_polling."""
    legacy = data_dir / "tenants" / f"{user_id}.sqlite3"
    if is_primary and legacy.is_file():
        return legacy
    return data_dir / "tenants" / f"bot-{bot_id}.sqlite3"


def read_tenant_db(path: Path) -> dict[str, Any]:
    """Read only what readiness needs. Never selects message or contact content."""
    if not path.is_file():
        return {"error": "database not created yet"}
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
    except sqlite3.Error:
        return {"error": "database cannot be opened"}
    conn.row_factory = sqlite3.Row
    try:
        st = conn.execute("SELECT enabled, active_status_key, session_id, updated_at "
                          "FROM app_state WHERE id=1").fetchone()
        status = None
        if st is not None and st["active_status_key"]:
            status = conn.execute(
                "SELECT key, title, emoji, reply_text, is_active, reply_mode, min_interval_seconds, "
                "active_from_hour, active_to_hour FROM statuses WHERE key=? AND deleted_at IS NULL",
                (st["active_status_key"],)).fetchone()
        connection = conn.execute(
            "SELECT connection_id, is_enabled, can_reply, owner_user_id FROM business_connections "
            "ORDER BY is_enabled DESC, updated_at DESC, rowid DESC LIMIT 1").fetchone()
        owner = conn.execute("SELECT timezone, language, owner_name, updated_at "
                             "FROM owner_settings WHERE id=1").fetchone()
        states = dict(conn.execute("SELECT state, COUNT(*) FROM reply_deliveries GROUP BY state").fetchall())
        exceptions = conn.execute("SELECT COUNT(*) FROM reply_exceptions").fetchone()[0]
    except sqlite3.Error as exc:
        return {"error": f"database unreadable ({type(exc).__name__})"}
    finally:
        conn.close()
    return {
        "app_state": _Row({"enabled": bool(st["enabled"]) if st else False,
                           "active_status_key": st["active_status_key"] if st else None,
                           "session_id": st["session_id"] if st else None,
                           "updated_at": st["updated_at"] if st else ""}),
        "status": _Row({**dict(status), "is_active": bool(status["is_active"])}) if status else None,
        "connection": _Row({**dict(connection), "is_enabled": bool(connection["is_enabled"]),
                            "can_reply": bool(connection["can_reply"])}) if connection else None,
        "owner": _Row(dict(owner)) if owner else _Row({"timezone": "Asia/Tehran", "language": "fa",
                                                        "owner_name": "", "updated_at": ""}),
        "stats": {k: int(v) for k, v in states.items()},
        "exceptions": int(exceptions),
    }


def classify(view: BotView, db: dict[str, Any], *, now: float | None = None) -> None:
    now = now or time.time()
    tel = view.telemetry or {}
    if not view.enabled:
        view.state = "disabled"
        view.reasons, view.reason_codes = ["توسط مدیر غیرفعال شده"], ["disabled_by_admin"]
        return
    if tel.get("errors", {}).get("unauthorized"):
        view.state = "token"
        view.reasons, view.reason_codes = ["تلگرام توکن را رد کرد؛ مالک باید توکن جدید بدهد (/connect_bot)"], ["token_rejected"]
        return
    if view.runtime == "waiting":
        view.state = "queued"
        view.reasons, view.reason_codes = ["منتظر جای خالی پولینگ (سقف ZIKO_TENANT_MAX_CONCURRENT_POLLERS)"], ["waiting_for_polling_slot"]
    elif view.runtime == "stopped":
        view.state = "crashed"
        view.reasons, view.reason_codes = ["فعال است اما پولینگ اجرا نمی‌شود؛ /logs را ببینید"], ["polling_not_running"]
        return
    elif view.runtime == "starting":
        # Slot held but Telegram has not answered getUpdates yet (or a restart is pending).
        view.state = "warning"
        view.reasons, view.reason_codes = ["در حال راه‌اندازی؛ هنوز پیامی از تلگرام دریافت نشده"], ["starting_no_updates_yet"]
    elif view.runtime == "degraded":
        view.state = "crashed"
        view.reasons, view.reason_codes = ["پولینگ در حال تلاش مجدد است؛ دریافت پیام از تلگرام قطع شده"], ["polling_degraded"]
        return
    elif view.runtime == "running" and tel:
        last_ok = tel.get("last_success_at")
        if last_ok is None or now - last_ok > SILENT_AFTER:
            view.state = "crashed"
            silent = "هیچ" if last_ok is None else f"{int(now - last_ok)} ثانیه"
            view.reasons = [f"تماس موفق با تلگرام: {silent}"]
            view.reason_codes = ["no_successful_call" if last_ok is None else f"silent_{int(now - last_ok)}s"]
            return
    if "error" in db:
        view.db_error = db["error"]
        if view.state != "queued":
            view.state = "warning"
        view.reasons.append(db["error"])
        view.reason_codes.append("database_" + db["error"].split(" (")[0].replace(" ", "_"))
        return
    view.stats = db["stats"]
    view.diagnosis = panel_health.diagnose(
        connection=db["connection"], app_state=db["app_state"], status=db["status"],
        owner=db["owner"], stats=db["stats"], exceptions_count=db["exceptions"],
        now=datetime.fromtimestamp(now, UTC))
    if view.state == "queued" or view.runtime == "starting":
        return  # a runtime problem outranks owner settings; the diagnosis is still attached
    if not view.diagnosis.ready:
        view.state = "blocked"
        view.reasons = [f.title for f in view.diagnosis.findings if f.level == panel_health.BLOCK]
        view.reason_codes = [f.code for f in view.diagnosis.findings if f.level == panel_health.BLOCK]
    elif any(f.level == panel_health.WARN for f in view.diagnosis.findings):
        view.state = "warning"
        view.reasons = [f.title for f in view.diagnosis.findings if f.level == panel_health.WARN]
        view.reason_codes = [f.code for f in view.diagnosis.findings if f.level == panel_health.WARN]
    else:
        view.state = "ready"


@dataclass(frozen=True, slots=True)
class Overview:
    bots: list[BotView]
    running: int
    waiting: int
    limit: int
    taken_at: float

    def count(self, state: str) -> int:
        return sum(1 for b in self.bots if b.state == state)

    @property
    def needs_attention(self) -> list[BotView]:
        return [b for b in self.bots if STATES[b.state][0] <= 3]


def collect(*, tenants: list[Any], data_dir: Path, runtime_of: Any, telemetry: dict[int, dict[str, Any]],
            running: int, waiting: int, limit: int, now: float | None = None) -> Overview:
    """``tenants`` are registry Tenant objects (tokens not revealed), oldest first."""
    first_bot_of: dict[int, int] = {}
    for t in tenants:
        first_bot_of.setdefault(t.user_id, t.bot_id)
    views: list[BotView] = []
    for t in tenants:
        view = BotView(bot_id=t.bot_id, user_id=t.user_id, username=t.username,
                       enabled=bool(t.enabled), runtime=runtime_of(t.bot_id),
                       telemetry=telemetry.get(t.bot_id))
        path = tenant_db_path(data_dir, t.user_id, t.bot_id, first_bot_of[t.user_id] == t.bot_id)
        classify(view, read_tenant_db(path) if t.enabled else {}, now=now)
        views.append(view)
    views.sort(key=lambda v: (STATES[v.state][0], v.username.lower()))
    return Overview(views, running, waiting, limit, now or time.time())


# --------------------------------------------------------------------------- #
# rendering (HTML, every dynamic value escaped)
# --------------------------------------------------------------------------- #
def _e(v: object) -> str:
    return html.escape(str(v), quote=False)


def render_overview(ov: Overview, page: int = 0, filt: str = "all", *,
                    tz: str = "Asia/Tehran", now: float | None = None) -> tuple[str, list[BotView], int]:
    """Return (text, bots on this page, page count)."""
    pool = ov.needs_attention if filt == "attention" else ov.bots
    pages = max(1, -(-len(pool) // PAGE_SIZE))
    page = min(max(page, 0), pages - 1)
    shown = pool[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    total = len(ov.bots)
    lines = ["🛰 <b>داشبورد پلتفرم ZIKO</b>", ""]
    if not total:
        lines += ["هنوز هیچ ربات زیرمجموعه‌ای ثبت نشده است.",
                  "کاربران با /create_bot در همین ربات، ربات خود را می‌سازند."]
        return "\n".join(lines), [], 1
    bar_used = min(10, round(10 * ov.running / ov.limit)) if ov.limit else 0
    lines += [
        f"🤖 ربات‌ها: <b>{total}</b>   ✅ آماده: <b>{ov.count('ready')}</b>   "
        f"⚠️ نیازمند توجه: <b>{len(ov.needs_attention)}</b>",
        f"📡 پولینگ: {'▰' * bar_used}{'▱' * (10 - bar_used)} {ov.running}/{ov.limit}"
        + (f"   ⏳ صف: {ov.waiting}" if ov.waiting else ""),
    ]
    breakdown = [f"{STATES[s][1]} {ov.count(s)}" for s in STATES if ov.count(s)]
    lines += ["   ".join(breakdown), ""]
    if ov.waiting and ov.running >= ov.limit:
        lines += ["💡 همه‌ی جای‌های پولینگ پر است؛ برای ربات‌های در صف، "
                  "ZIKO_TENANT_MAX_CONCURRENT_POLLERS را افزایش دهید.", ""]
    title = "ربات‌های نیازمند توجه" if filt == "attention" else "همه‌ی ربات‌ها"
    lines.append(f"<b>{title}</b> (صفحه {page + 1}/{pages}):")
    if not shown:
        lines.append("🎉 هیچ رباتی مشکل ندارد.")
    for b in shown:
        reason = f" — {_e(b.reasons[0])}" if b.reasons and b.state != "ready" else ""
        lines.append(f"{b.icon} @{_e(b.username)}{reason}")
    from zoneinfo import ZoneInfo
    try:
        zone = ZoneInfo(tz)
    except (KeyError, ValueError):
        zone, tz = ZoneInfo("UTC"), "UTC"
    stamp = datetime.fromtimestamp(ov.taken_at, zone).strftime("%H:%M:%S")
    age = max(0, int((now or time.time()) - ov.taken_at))
    lines += ["", f"<i>به‌روزرسانی: {stamp} ({tz})"
              + (f" · {age} ثانیه پیش" if age >= 5 else "") + "</i>"]
    return "\n".join(lines), shown, pages


def render_bot(b: BotView, now: float | None = None) -> str:
    lines = [f"{b.icon} <b>@{_e(b.username)}</b>", "",
             f"وضعیت: <b>{STATES[b.state][2]}</b>",
             f"Bot ID: <code>{b.bot_id}</code>   مالک: <code>{b.user_id}</code>",
             f"پولینگ: {_e({'running': 'در حال اجرا', 'waiting': 'در صف', 'starting': 'در حال راه‌اندازی', 'stopped': 'متوقف'}.get(b.runtime, b.runtime))}"]
    tel = b.telemetry
    if tel:
        lat = tel.get("latency_ms", {})
        last = tel.get("last_success_at")
        # Measured against the snapshot time, never a later clock: no negative ages.
        ago = f"{max(0, int((now or time.time()) - last))} ثانیه پیش" if last else "هرگز"
        lines.append(f"تلگرام: p50 {lat.get('p50')} / p95 {lat.get('p95')} ms · آخرین موفق: {ago}")
        if tel.get("errors"):
            lines.append("خطاها: " + "، ".join(f"{_e(k)}×{v}" for k, v in tel["errors"].items()))
        if tel.get("rate_limited"):
            lines.append(f"محدودیت سرعت (429): {tel['rate_limited']} بار")
    if b.stats:
        sent = b.stats.get("sent", 0)
        bad = b.stats.get("failed", 0) + b.stats.get("uncertain", 0)
        lines.append(f"پاسخ‌ها: ✅ {sent}   ❌ {bad}   ⏳ {b.stats.get('pending', 0)}")
    if b.diagnosis is not None:
        lines += ["", "<b>بررسی آمادگی پاسخ:</b>"]
        for f in b.diagnosis.findings:
            if f.level != panel_health.INFO:
                lines.append(f"{panel_health._ICON[f.level]} {f.title}")
    elif b.reasons:
        lines += [""] + [f"• {_e(r)}" for r in b.reasons]
    if b.state in {"blocked", "warning"} and b.diagnosis is not None and not b.diagnosis.ready:
        lines += ["", "💡 مشکل از سمت تنظیمات مالک است؛ مالک می‌تواند در ربات خود /diagnose را بزند."]
    text = "\n".join(lines)
    return text if len(text) <= 4000 else text[:3990] + "…"


# --------------------------------------------------------------------------- #
# snapshot file for the CLI (separate process): <data>/run/overview.json, 0600
# --------------------------------------------------------------------------- #
SNAPSHOT_FILE = "overview.json"
SNAPSHOT_SCHEMA = 1


def to_snapshot(ov: Overview) -> dict[str, Any]:
    """Serialisable summary for the English-only CLI: English reason codes, no Persian text.

    Contains no token, message, contact or chat data."""
    return {
        "schema": SNAPSHOT_SCHEMA, "taken_at": ov.taken_at,
        "running": ov.running, "waiting": ov.waiting, "limit": ov.limit,
        "counts": {s: ov.count(s) for s in STATES},
        "bots": [{"bot_id": b.bot_id, "user_id": b.user_id, "username": b.username,
                  "enabled": b.enabled, "runtime": b.runtime, "state": b.state,
                  "reason_codes": b.reason_codes[:3]} for b in ov.bots],
    }


def write_snapshot(path: Path, ov: Overview) -> None:
    import json
    import os
    import secrets
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    try:
        tmp.write_text(json.dumps(to_snapshot(ov), ensure_ascii=False, separators=(",", ":")),
                       encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def read_snapshot(path: Path) -> dict[str, Any] | None:
    import json
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("schema") != SNAPSHOT_SCHEMA:
        return None
    if not isinstance(data.get("bots"), list) or not isinstance(data.get("taken_at"), (int, float)):
        return None
    return data

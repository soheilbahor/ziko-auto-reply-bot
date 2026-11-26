"""ZIKO terminal home screen: Telegram connectivity, system resources and bots at a glance.

Everything shown is measured, never estimated:

* Telegram ping   TCP connect time to api.telegram.org:443 from this server (no token,
                  no HTTP request). Bot API state and per-bot latency come from the
                  service's own telemetry file (``run/telegram-status.json``).
* Child bots      the service's overview snapshot (``run/overview.json``).
* System          psutil (CPU, RAM, disk, network, the service process).

A value that cannot be read is shown as "n/a" with no number. History sparklines cover
this CLI session only (one sample per screen refresh); they are labelled as such.

English only (CLI rule). Collection is separated from rendering so both are testable.
"""
from __future__ import annotations

import socket
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rich import box
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

try:  # psutil is a runtime dependency since 1.5.0; degrade if it is missing
    import psutil  # type: ignore[import-untyped]
except Exception:  # pragma: no cover - exercised only on broken installs
    psutil = None

API_HOST = "api.telegram.org"
HISTORY = 30                 # samples kept per sparkline
PING_GOOD_MS = 300.0
PING_SLOW_MS = 800.0
TELEMETRY_STALE_S = 60.0     # the service writes telemetry every 10 s
BOT_API_QUIET_S = 120.0      # no successful Bot API call for this long = not connected
SPARK = "▁▂▃▄▅▆▇█"

C_OK, C_WARN, C_BAD = "#34d399", "#fbbf24", "#f87171"
C_TEXT, C_DIM, C_EDGE, C_TRACK = "#c0caf5", "#5b6b8c", "#2b3a67", "#1f2a44"
C_CYAN, C_BLUE, C_VIOLET = "#22d3ee", "#7aa2ff", "#a78bfa"


# --------------------------------------------------------------------------- model
@dataclass(slots=True)
class BotLine:
    username: str
    role: str                 # master / child
    state: str                # running / degraded / waiting / starting / stopped / failed / unknown
    ping_ms: float | None
    note: str = ""            # English reason or last error category
    note_level: str = "dim"   # dim / warn / bad


@dataclass(slots=True)
class HomeModel:
    version: str = ""
    update_text: str = ""
    update_level: str = "ok"          # ok / warn / info
    service_state: str = "unknown"
    service_uptime_s: float | None = None
    host: str = ""
    last_backup_at: float | None = None
    # Telegram
    ping_ms: float | None = None
    ping_error: str | None = None
    ping_history: list[float] = field(default_factory=list)
    bot_api: str = "unknown"          # connected / quiet / no-data
    telemetry_age_s: float | None = None
    last_success_age_s: float | None = None
    rate_limited: int | None = None
    master_username: str | None = None
    child_counts: dict[str, int] = field(default_factory=dict)
    snapshot_problem: str | None = None
    bots: list[BotLine] = field(default_factory=list)
    # System
    cpu_percent: float | None = None
    cpu_history: list[float] = field(default_factory=list)
    cores: int | None = None
    load_avg: tuple[float, float, float] | None = None
    mem_percent: float | None = None
    mem_history: list[float] = field(default_factory=list)
    mem_used: int | None = None
    mem_total: int | None = None
    disk_percent: float | None = None
    disk_used: int | None = None
    disk_total: int | None = None
    db_size: int | None = None
    net_rx_s: float | None = None
    net_tx_s: float | None = None
    service_rss: int | None = None
    service_cpu: float | None = None
    taken_at: float = field(default_factory=time.time)


# --------------------------------------------------------------------------- measuring
def tcp_ping(host: str = API_HOST, port: int = 443, timeout: float = 2.0) -> tuple[float | None, str | None]:
    """(milliseconds, error class). One TCP handshake; nothing is sent."""
    t0 = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return round((time.perf_counter() - t0) * 1000, 1), None
    except OSError as exc:
        return None, type(exc).__name__


def ping_level(ms: float | None) -> str:
    if ms is None:
        return "bad"
    return "ok" if ms < PING_GOOD_MS else "warn" if ms < PING_SLOW_MS else "bad"


def _read_json(path: Path) -> dict[str, Any] | None:
    import json
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


_SNAPSHOT_STATE = {"ready": "running", "queued": "waiting", "crashed": "failed", "token": "failed",
                   "disabled": "stopped", "blocked": "running", "warning": "running"}


class Collector:
    """Keeps session history (sparklines, network deltas). One instance per CLI session.

    Every source is injectable, so tests never touch the network or the host.
    """

    def __init__(self, *, data_dir: Path, db_path: Path,
                 ping: Callable[[], tuple[float | None, str | None]] = tcp_ping,
                 clock: Callable[[], float] = time.time,
                 master_name: Callable[[], str | None] | None = None,
                 psutil_mod: Any = psutil) -> None:
        self.data_dir, self.db_path = data_dir, db_path
        self._ping, self._clock, self._ps = ping, clock, psutil_mod
        self._master_name_fn = master_name
        self._master_name: str | None = None
        self._master_name_tried = False
        self.ping_hist: deque[float] = deque(maxlen=HISTORY)
        self.cpu_hist: deque[float] = deque(maxlen=HISTORY)
        self.mem_hist: deque[float] = deque(maxlen=HISTORY)
        self._net_prev: tuple[float, int, int] | None = None
        self._proc: Any = None

    # -- Telegram ---------------------------------------------------------------------
    def _telegram(self, m: HomeModel, now: float) -> dict[int, dict[str, Any]]:
        m.ping_ms, m.ping_error = self._ping()
        if m.ping_ms is not None:
            self.ping_hist.append(m.ping_ms)
        m.ping_history = list(self.ping_hist)
        tel = _read_json(self.data_dir / "run" / "telegram-status.json")
        per_bot: dict[int, dict[str, Any]] = {}
        if tel is None or not isinstance(tel.get("bots"), list):
            m.bot_api = "no-data"
            return per_bot
        m.telemetry_age_s = max(0.0, now - float(tel.get("written_at") or 0))
        for b in tel["bots"]:
            if isinstance(b, dict) and isinstance(b.get("bot_id"), int):
                per_bot[b["bot_id"]] = b
        m.rate_limited = sum(int(b.get("rate_limited") or 0) for b in per_bot.values())
        masters = [b for b in per_bot.values() if b.get("role") == "master"]
        last = max((float(b.get("last_success_at") or 0) for b in masters), default=0.0)
        if m.telemetry_age_s > TELEMETRY_STALE_S:
            m.bot_api = "no-data"
        elif last:
            m.last_success_age_s = max(0.0, now - last)
            m.bot_api = "connected" if m.last_success_age_s <= BOT_API_QUIET_S else "quiet"
        else:
            m.bot_api = "quiet"
        return per_bot

    def _master(self) -> str | None:
        if not self._master_name_tried and self._master_name_fn is not None:
            self._master_name_tried = True  # one getMe per session at most
            try:
                self._master_name = self._master_name_fn()
            except Exception:
                self._master_name = None
        return self._master_name

    def _bots(self, m: HomeModel, per_bot: Mapping[int, dict[str, Any]], now: float) -> None:
        from app import cli_bots
        m.master_username = self._master()
        masters = [b for b in per_bot.values() if b.get("role") == "master"]
        for b in masters[:1]:
            lat = (b.get("latency_ms") or {}).get("p50")
            err = b.get("last_error")
            m.bots.append(BotLine(
                username=m.master_username or f"id {b['bot_id']}", role="master",
                state="running" if m.bot_api == "connected" else "unknown",
                ping_ms=lat, note=f"last error: {err}" if err else "",
                note_level="warn" if err else "dim"))
        snap, _age, problem = cli_bots.load(self.db_path, now)
        m.snapshot_problem = problem
        if snap is None:
            return
        counts = snap.get("counts") or {}
        m.child_counts = {
            "running": int(counts.get("ready", 0)) + int(counts.get("blocked", 0)) + int(counts.get("warning", 0)),
            "attention": int(counts.get("blocked", 0)) + int(counts.get("warning", 0)),
            "failed": int(counts.get("crashed", 0)) + int(counts.get("token", 0)),
            "waiting": int(counts.get("queued", 0)),
        }
        for b in snap.get("bots") or []:
            state = _SNAPSHOT_STATE.get(b.get("state", ""), "unknown")
            if b.get("runtime") == "degraded" and state == "running":
                state = "degraded"
            tel = per_bot.get(b.get("bot_id"), {})
            codes = ", ".join(str(c).replace("_", " ") for c in (b.get("reason_codes") or [])[:2])
            err = tel.get("last_error")
            if state in ("failed", "degraded") or b.get("state") in ("blocked", "warning"):
                note, level = (codes or err or b.get("state", "")), "bad" if state == "failed" else "warn"
            elif err:
                note, level = f"last error: {err}", "dim"
            else:
                note, level = "", "dim"
            m.bots.append(BotLine(username=str(b.get("username") or b.get("bot_id")), role="child",
                                  state=state, ping_ms=(tel.get("latency_ms") or {}).get("p50"),
                                  note=note, note_level=level))

    # -- system -----------------------------------------------------------------------
    def _system(self, m: HomeModel, main_pid: int | None, now: float) -> None:
        ps = self._ps
        if ps is None:
            return
        try:
            m.cpu_percent = float(ps.cpu_percent(interval=0.1))
            self.cpu_hist.append(m.cpu_percent)
            m.cores = ps.cpu_count(logical=True)
            try:
                m.load_avg = tuple(round(x, 2) for x in ps.getloadavg())  # type: ignore[assignment]
            except (AttributeError, OSError):
                m.load_avg = None
        except Exception:
            m.cpu_percent = None
        try:
            vm = ps.virtual_memory()
            m.mem_percent, m.mem_used, m.mem_total = float(vm.percent), int(vm.total - vm.available), int(vm.total)
            self.mem_hist.append(m.mem_percent)
        except Exception:
            pass
        try:
            du = ps.disk_usage(str(self.data_dir if self.data_dir.exists() else Path("/")))
            m.disk_percent, m.disk_used, m.disk_total = float(du.percent), int(du.used), int(du.total)
        except Exception:
            pass
        try:
            io = ps.net_io_counters()
            if self._net_prev is not None and now > self._net_prev[0]:
                dt = now - self._net_prev[0]
                m.net_rx_s = max(0.0, (io.bytes_recv - self._net_prev[1]) / dt)
                m.net_tx_s = max(0.0, (io.bytes_sent - self._net_prev[2]) / dt)
            self._net_prev = (now, int(io.bytes_recv), int(io.bytes_sent))
        except Exception:
            pass
        if main_pid:
            try:
                if self._proc is None or self._proc.pid != main_pid:
                    self._proc = ps.Process(main_pid)
                    self._proc.cpu_percent(None)  # first call primes the counter
                m.service_rss = int(self._proc.memory_info().rss)
                m.service_cpu = float(self._proc.cpu_percent(None))
            except Exception:
                self._proc = None
        m.cpu_history, m.mem_history = list(self.cpu_hist), list(self.mem_hist)
        try:
            m.db_size = self.db_path.stat().st_size
        except OSError:
            m.db_size = None

    def collect(self, *, service: Mapping[str, str], version: str, update: Mapping[str, object] | None,
                host: str = "", last_backup_at: float | None = None) -> HomeModel:
        now = self._clock()
        m = HomeModel(version=version, host=host, last_backup_at=last_backup_at, taken_at=now)
        m.service_state = service.get("ActiveState") or "unknown"
        if service.get("LoadState") == "not-found":
            m.service_state = "not-found"
        mono = service.get("ActiveEnterTimestampMonotonic", "0")
        if m.service_state == "active" and str(mono).isdigit() and int(mono) > 0:
            m.service_uptime_s = max(0.0, time.monotonic() - int(mono) / 1e6)
        m.update_text, m.update_level = _update_line(update)
        per_bot = self._telegram(m, now)
        self._bots(m, per_bot, now)
        pid = service.get("MainPID", "0")
        self._system(m, int(pid) if str(pid).isdigit() and int(pid) > 0 else None, now)
        return m


def _update_line(update: Mapping[str, object] | None) -> tuple[str, str]:
    if not update:
        return "update status not checked", "info"
    if update.get("reachable") and update.get("available"):
        return f"update available · v{str(update.get('stable', '')).removeprefix('v')}", "warn"
    if update.get("reachable"):
        return "up to date", "ok"
    return "update status unavailable", "info"


# --------------------------------------------------------------------------- formatting
def human_bytes(n: float | int | None) -> str:
    if n is None:
        return "n/a"
    value = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(value) < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit in ("B", "KB") or value >= 100 else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"  # pragma: no cover


def human_duration(seconds: float | None) -> str:
    if seconds is None:
        return "n/a"
    s = int(seconds)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    mi, s = divmod(s, 60)
    if d:
        return f"{d}d {h:02d}:{mi:02d}"
    if h:
        return f"{h}h {mi:02d}m"
    return f"{mi}m {s:02d}s" if mi else f"{s}s"


def sparkline(values: list[float], lo: float, hi: float, style: str) -> Text:
    out = Text()
    if hi <= lo:
        hi = lo + 1
    for v in values[-HISTORY:]:
        i = int((min(max(v, lo), hi) - lo) / (hi - lo) * (len(SPARK) - 1))
        out.append(SPARK[i], style=style)
    return out


def level_style(level: str) -> str:
    return {"ok": C_OK, "warn": C_WARN, "bad": C_BAD, "info": C_BLUE, "dim": C_DIM}.get(level, C_TEXT)


def usage_level(pct: float | None) -> str:
    if pct is None:
        return "dim"
    return "ok" if pct < 70 else "warn" if pct < 90 else "bad"


def bar(pct: float | None, width: int = 22, ascii_only: bool = False) -> Text:
    if pct is None:
        return Text("n/a", style=C_DIM)
    style = level_style(usage_level(pct))
    filled = max(0, min(width, round(pct / 100 * width)))
    full, empty = ("#", "-") if ascii_only else ("━", "━")
    return Text.assemble((full * filled, style), (empty * (width - filled), C_DIM if ascii_only else C_TRACK),
                         (f" {pct:5.1f}%", f"bold {style}"))


# --------------------------------------------------------------------------- rendering
_STATE_VIEW = {
    "running": ("🟢", "[ok]", "running", "ok"),
    "degraded": ("🟡", "[!]", "degraded", "warn"),
    "waiting": ("🔵", "[..]", "queued", "info"),
    "starting": ("🔵", "[..]", "starting", "info"),
    "stopped": ("⚪", "[-]", "stopped", "dim"),
    "failed": ("🔴", "[x]", "failed", "bad"),
    "unknown": ("⚪", "[?]", "unknown", "dim"),
}
TAGLINE = "Telegram Business Auto-Reply Assistant"
WIDE = 110          # side-by-side Telegram / System panels from this width
C_PILL = "#0b2233"

# 3-row block lettering; every glyph row has the same width.
_LOGO = {
    "Z": ("▀▀▀█", " ▄▀ ", "█▄▄▄"),
    "I": ("█", "█", "█"),
    "K": ("█ ▄▀", "██  ", "█ ▀▄"),
    "O": ("█▀▀█", "█  █", "█▄▄█"),
}
_GRADIENT = ((34, 211, 238), (167, 139, 250))


def _e(ascii_only: bool, fancy: str, plain: str = "") -> str:
    return plain if ascii_only else fancy


def _panel(body: RenderableType, title: str, colour: str, height: int | None = None) -> Panel:
    return Panel(body, title=f"[bold {colour}]{title}", title_align="left", border_style=C_EDGE,
                 box=box.ROUNDED, padding=(0, 1), height=height)


def _height(renderable: RenderableType, width: int) -> int:
    """Rendered line count at ``width`` (used to give side-by-side panels equal height)."""
    import io

    from rich.console import Console
    probe = Console(width=max(width, 10), file=io.StringIO(), color_system=None)
    return len(probe.render_lines(renderable, probe.options, pad=False))


def logo(ascii_only: bool = False) -> Text:
    if ascii_only:
        return Text("ZIKO", style=f"bold {C_CYAN}")
    rows = [" ".join(_LOGO[ch][i] for ch in "ZIKO") for i in range(3)]
    span = max(len(r) for r in rows) - 1
    out = Text()
    for n, row in enumerate(rows):
        for x, ch in enumerate(row):
            if ch == " ":
                out.append(" ")
                continue
            r, g, b = (round(a + (c - a) * x / span) for a, c in zip(*_GRADIENT))
            out.append(ch, style=f"bold rgb({r},{g},{b})")
        if n < len(rows) - 1:
            out.append("\n")
    return out


def version_pill(m: HomeModel, ascii_only: bool = False) -> Text:
    ver = f"v{m.version.removeprefix('v')}" if m.version else "version unknown"
    mark = {"ok": ("✓", "OK"), "warn": ("↑", "!"), "info": ("·", "-")}.get(m.update_level, ("·", "-"))
    pill = Text.assemble((f" {ver} ", f"bold {C_VIOLET}"),
                         (f" {_e(ascii_only, mark[0], mark[1])} {m.update_text} ", level_style(m.update_level)))
    if not ascii_only:
        pill.stylize(f"on {C_PILL}")
    return pill


def _status_grid(m: HomeModel, ascii_only: bool, two_columns: bool) -> Table:
    svc_level = "ok" if m.service_state == "active" else "bad"
    service = Text(f"{_e(ascii_only, '●', '*')} {m.service_state}", style=f"bold {level_style(svc_level)}")
    uptime = Text(human_duration(m.service_uptime_s), style=f"bold {C_TEXT}")
    host = Text(m.host or "n/a", style=C_TEXT)
    if m.last_backup_at:
        age = max(0.0, m.taken_at - m.last_backup_at)
        backup = Text(f"{_e(ascii_only, '🔐 ')}{human_duration(age)} ago",
                      style=f"bold {level_style('ok' if age < 86400 * 2 else 'warn')}")
    else:
        backup = Text("none found", style=f"bold {level_style('warn')}")
    g = Table.grid(padding=(0, 1))
    pairs = [("Service:", service), ("Uptime:", uptime), ("Host:", host), ("Last backup:", backup)]
    if two_columns:
        for _ in range(2):
            g.add_column(style=C_DIM, no_wrap=True)
            g.add_column(no_wrap=True, overflow="ellipsis")
        g.add_row(pairs[0][0], pairs[0][1], " " + pairs[1][0], pairs[1][1])
        g.add_row(pairs[2][0], pairs[2][1], " " + pairs[3][0], pairs[3][1])
    else:
        g.add_column(style=C_DIM, no_wrap=True)
        g.add_column(no_wrap=True, overflow="ellipsis")
        for label, value in pairs:
            g.add_row(label, value)
    return g


def header(m: HomeModel, ascii_only: bool = False, width: int = 120) -> RenderableType:
    """Logo, product line, version pill, and service / uptime / host / last backup."""
    brand = Table.grid(padding=(0, 0))
    brand.add_column(no_wrap=True, overflow="ellipsis")
    brand.add_row(Text(TAGLINE, style=f"bold {C_TEXT}"))
    brand.add_row(version_pill(m, ascii_only))
    if width >= WIDE:
        g = Table.grid(expand=True, padding=(0, 3))
        g.add_column(no_wrap=True)
        g.add_column(no_wrap=True, overflow="ellipsis")
        g.add_column(justify="right", no_wrap=True)
        g.add_row(logo(ascii_only), brand, _status_grid(m, ascii_only, two_columns=True))
        return g
    top = Table.grid(padding=(0, 3))
    top.add_column(no_wrap=True)
    top.add_column(no_wrap=True, overflow="ellipsis")
    top.add_row(logo(ascii_only), brand)
    return Group(top, _status_grid(m, ascii_only, two_columns=width >= 72))


def telegram_panel(m: HomeModel, ascii_only: bool = False, height: int | None = None) -> Panel:
    return _panel(_telegram_body(m, ascii_only), _e(ascii_only, "📨 Telegram", "Telegram"), C_CYAN, height)


def _telegram_body(m: HomeModel, ascii_only: bool) -> Table:
    g = Table.grid(padding=(0, 1))
    g.add_column(style=C_DIM, no_wrap=True, width=15)
    g.add_column(no_wrap=True, overflow="ellipsis")
    api_view = {"connected": ("connected", "ok"), "quiet": ("no recent Bot API success", "warn"),
                "no-data": ("no telemetry (service stopped?)", "warn"), "unknown": ("unknown", "dim")}
    label, level = api_view.get(m.bot_api, api_view["unknown"])
    g.add_row(_e(ascii_only, "📡 Bot API", "Bot API"),
              Text.assemble((f"{_e(ascii_only, '●', '*')} {label}", f"bold {level_style(level)}"),
                            (f" ({API_HOST})", C_DIM)))
    lvl = ping_level(m.ping_ms)
    if m.ping_ms is None:
        ping = Text.assemble((_e(ascii_only, "🔴 ", "[x] ") + "unreachable", f"bold {C_BAD}"),
                             (f"   {m.ping_error or ''}", C_DIM))
    else:
        dot = {"ok": ("🟢", "good"), "warn": ("🟡", "slow"), "bad": ("🔴", "very slow")}[lvl]
        ping = Text.assemble((f"{m.ping_ms:.0f} ms ", f"bold {level_style(lvl)}"),
                             (_e(ascii_only, dot[0] + " ") + dot[1], level_style(lvl)))
        if len(m.ping_history) >= 2:
            ping.append(f"  (min {min(m.ping_history):.0f} · max {max(m.ping_history):.0f})", style=C_DIM)
    g.add_row(_e(ascii_only, "⚡ Ping (TCP)", "Ping (TCP)"), ping)
    if len(m.ping_history) >= 2 and not ascii_only:
        hi = max(max(m.ping_history), PING_GOOD_MS / 2)
        g.add_row("", Text.assemble(sparkline(m.ping_history, 0, hi, C_BLUE),
                                    (f"  {len(m.ping_history)} samples", C_DIM)))
    g.add_row(_e(ascii_only, "🤖 Master bot", "Master bot"),
              Text("@" + m.master_username, style=f"bold {C_TEXT}") if m.master_username
              else Text("n/a", style=C_DIM))
    c = m.child_counts
    if c:
        kids = Text.assemble((f"{c.get('running', 0)} running", f"bold {C_OK}"),
                             (f" · {c.get('failed', 0)} failed", C_BAD if c.get("failed") else C_TEXT),
                             (f" · {c.get('waiting', 0)} queued", C_TEXT))
        if c.get("attention"):
            kids.append(f" · {c['attention']} need attention", style=C_WARN)
    else:
        kids = Text(m.snapshot_problem or "none registered", style=C_DIM)
    g.add_row(_e(ascii_only, "👶 Child bots", "Child bots"), kids)
    g.add_row(_e(ascii_only, "🕒 Last API ok", "Last API ok"),
              Text(f"{_e(ascii_only, '✓ ')}{human_duration(m.last_success_age_s)} ago", style=C_OK)
              if m.last_success_age_s is not None else Text("n/a", style=C_DIM))
    g.add_row(_e(ascii_only, "🚦 Rate limits", "Rate limits"),
              Text(f"{m.rate_limited} × 429 since service start",
                   style=level_style("ok" if not m.rate_limited else "warn"))
              if m.rate_limited is not None else Text("n/a", style=C_DIM))
    return g


GAUGE_WIDTH = 11


def gauge(label: str, pct: float | None, ascii_only: bool = False) -> Table:
    """Compact meter card: label, big value, bar (terminal stand-in for a ring gauge)."""
    style = level_style(usage_level(pct))
    g = Table.grid()
    g.add_column(justify="center", width=GAUGE_WIDTH, no_wrap=True)
    g.add_row(Text(label, style=f"bold {C_TEXT}"))
    g.add_row(Text("n/a" if pct is None else f"{pct:.1f}%", style=f"bold {style}" if pct is not None else C_DIM))
    inner = GAUGE_WIDTH - 2
    filled = 0 if pct is None else max(0, min(inner, round(pct / 100 * inner)))
    full, empty = ("#", "-") if ascii_only else ("█", "░")
    g.add_row(Text.assemble((full * filled, style), (empty * (inner - filled), C_DIM if ascii_only else C_EDGE)))
    return g


def _system_details(m: HomeModel, ascii_only: bool) -> Table:
    g = Table.grid(padding=(0, 1))
    g.add_column(style=C_DIM, no_wrap=True)
    g.add_column(no_wrap=True, overflow="ellipsis")
    extra = [f"{m.cores} cores" if m.cores else "cores n/a"]
    if m.load_avg:
        extra.append(f"load {m.load_avg[0]:.2f}")
    g.add_row(_e(ascii_only, "🧠", "CPU "), Text(" · ".join(extra), style=C_TEXT))
    g.add_row(_e(ascii_only, "💾", "Mem "), Text(f"{human_bytes(m.mem_used)} / {human_bytes(m.mem_total)}", style=C_TEXT))
    g.add_row(_e(ascii_only, "💽", "Disk"),
              Text(f"{human_bytes(m.disk_used)} / {human_bytes(m.disk_total)} · DB {human_bytes(m.db_size)}",
                   style=C_TEXT))
    if m.net_rx_s is None:
        net = Text("Net measuring… (next refresh)", style=C_DIM)
    else:
        down, up = ("rx", "tx") if ascii_only else ("↓", "↑")
        net = Text.assemble(("Net ", C_DIM), (f"{down} {human_bytes(m.net_rx_s)}/s", C_TEXT),
                            (f" · {up} {human_bytes(m.net_tx_s)}/s", C_TEXT))
    g.add_row(_e(ascii_only, "🌐", "Net "), net)
    if m.service_rss is not None:
        g.add_row(_e(ascii_only, "⚡", "ZIKO"),
                  Text.assemble(("ZIKO: ", C_DIM), (f"{human_bytes(m.service_rss)} RAM", f"bold {C_TEXT}"),
                                (f" · {m.service_cpu or 0:.1f}% CPU", C_TEXT)))
    if len(m.cpu_history) >= 2 and not ascii_only:
        g.add_row("📈", Text.assemble(sparkline(m.cpu_history, 0, 100, C_VIOLET), ("  CPU trend", C_DIM)))
    return g


def _system_body(m: HomeModel, ascii_only: bool, inner_width: int) -> RenderableType:
    meters = Table.grid(padding=(0, 1))
    for _ in range(3):
        meters.add_column()
    meters.add_row(gauge("CPU", m.cpu_percent, ascii_only), gauge("RAM", m.mem_percent, ascii_only),
                   gauge("Disk", m.disk_percent, ascii_only))
    details = _system_details(m, ascii_only)
    meters_width = 3 * GAUGE_WIDTH + 2
    if inner_width >= meters_width + 30:
        body = Table.grid(expand=True, padding=(0, 2))
        body.add_column(width=meters_width, no_wrap=True)
        body.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
        body.add_row(meters, details)
        return body
    return Group(meters, Text(""), details)


def system_panel(m: HomeModel, ascii_only: bool = False, width: int = 60, height: int | None = None) -> Panel:
    return _panel(_system_body(m, ascii_only, max(10, width - 4)),
                  _e(ascii_only, "💻 System", "System"), C_VIOLET, height)


BOT_NAME_MAX = 32


def bots_panel(m: HomeModel, ascii_only: bool = False, limit: int = 8, width: int = 120) -> Panel | None:
    if not m.bots:
        return None
    t = Table(box=box.SIMPLE_HEAD, expand=True, header_style=f"bold {C_DIM}", border_style=C_EDGE,
              pad_edge=False, show_edge=False)
    t.add_column("#", justify="right", no_wrap=True, style=C_DIM, width=2)
    t.add_column("Bot", no_wrap=True, overflow="ellipsis", max_width=BOT_NAME_MAX, min_width=12, ratio=4)
    if width >= 72:
        t.add_column("Role", no_wrap=True, overflow="ellipsis", width=6)
    t.add_column("State", no_wrap=True, overflow="ellipsis", width=12)
    t.add_column("API p50", justify="right", no_wrap=True, width=8)
    t.add_column("Note", no_wrap=True, overflow="ellipsis", ratio=3)
    for n, b in enumerate(m.bots[:limit], 1):
        icon, plain, label, level = _STATE_VIEW.get(b.state, _STATE_VIEW["unknown"])
        name = Text("@" + b.username, style=f"bold {C_CYAN}")
        name.truncate(BOT_NAME_MAX, overflow="ellipsis")
        cells: list[RenderableType] = [str(n), name]
        if width >= 72:
            cells.append(Text(b.role, style=C_VIOLET if b.role == "master" else C_BLUE))
        cells += [Text(f"{_e(ascii_only, icon, plain)} {label}", style=level_style(level)),
                  Text("n/a" if b.ping_ms is None else f"{b.ping_ms:.0f} ms", style=f"bold {C_TEXT}"),
                  Text(b.note or "—" if not ascii_only else (b.note or "-"),
                       style=C_WARN if b.note and b.note_level == "dim" else level_style(b.note_level))]
        t.add_row(*cells)
    more = len(m.bots) - limit
    title = _e(ascii_only, "👥 ", "") + f"Bots ({len(m.bots)})"
    if more > 0:
        title += f"  +{more} more: ziko bots status"
    return _panel(t, title, C_BLUE)


def render(m: HomeModel, *, width: int = 120, ascii_only: bool = False) -> Group:
    """The whole home dashboard (header and panels, without the menu)."""
    parts: list[RenderableType] = [header(m, ascii_only, width), Text("")]
    if width >= WIDE:
        half = (width - 1) // 2
        tg_body = _telegram_body(m, ascii_only)
        sys_body = _system_body(m, ascii_only, half - 4)
        height = max(_height(tg_body, half - 4), _height(sys_body, half - 4)) + 2
        row = Table.grid(expand=True, padding=(0, 1))
        row.add_column(ratio=1)
        row.add_column(ratio=1)
        row.add_row(_panel(tg_body, _e(ascii_only, "📨 Telegram", "Telegram"), C_CYAN, height),
                    _panel(sys_body, _e(ascii_only, "💻 System", "System"), C_VIOLET, height))
        parts.append(row)
    else:
        parts += [telegram_panel(m, ascii_only), system_panel(m, ascii_only, width)]
    bots = bots_panel(m, ascii_only, width=width)
    if bots is not None:
        parts.append(bots)
    return Group(*parts)


def live(collect: Callable[[], HomeModel], *, console: Any, refresh: float = 2.0,
         frames: int | None = None, sleep: Callable[[float], None] = time.sleep) -> int:
    """Full-screen view refreshed every ``refresh`` s until Ctrl+C. ``frames`` is for tests."""
    from rich.live import Live

    def frame() -> RenderableType:
        model = collect()
        foot = Text.assemble(("  live · refresh every ", C_DIM), (f"{refresh:g}s", f"bold {C_VIOLET}"),
                             ("  ·  Ctrl+C to return to the menu", C_DIM))
        return Group(render(model, width=console.width, ascii_only=console.options.ascii_only), foot)

    shown = 0
    try:
        with Live(frame(), console=console, screen=console.is_terminal, refresh_per_second=4) as view:
            shown = 1
            while frames is None or shown < frames:
                sleep(refresh)
                view.update(frame())
                shown += 1
    except KeyboardInterrupt:
        pass
    return 0

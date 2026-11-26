"""Home dashboard (app/cli_home.py): measured values only, safe fallbacks, English only."""
from __future__ import annotations

import json
import re
import time
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console

from app import cli_home, cli_menu, cli_nav

NOW = 1_800_000_000.0
_PERSIAN = re.compile(r"[\u0600-\u06ff]")


class FakePsutil:
    """Deterministic psutil stand-in; counters advance between calls."""

    def __init__(self) -> None:
        self.rx, self.tx = 1_000_000, 500_000

    def cpu_percent(self, interval=None):
        return 14.2

    def cpu_count(self, logical=True):
        return 2

    def getloadavg(self):
        return (0.21, 0.18, 0.15)

    def virtual_memory(self):
        return SimpleNamespace(percent=44.1, total=934 * 2**20, available=522 * 2**20)

    def disk_usage(self, _path):
        return SimpleNamespace(percent=7.6, used=int(2.9 * 2**30), total=38 * 2**30)

    def net_io_counters(self):
        self.rx += 36_800
        self.tx += 12_200
        return SimpleNamespace(bytes_recv=self.rx, bytes_sent=self.tx)

    def Process(self, pid):  # noqa: N802 - psutil API name
        return SimpleNamespace(pid=pid, cpu_percent=lambda _i=None: 0.8,
                               memory_info=lambda: SimpleNamespace(rss=154 * 2**20))


def write_run_files(data: Path, *, telemetry_age: float = 3.0, master_ok_age: float = 2.0,
                    child_state: str = "ready", runtime: str = "running") -> None:
    run = data / "run"
    run.mkdir(parents=True, exist_ok=True)
    (run / "telegram-status.json").write_text(json.dumps({
        "schema": 1, "written_at": NOW - telemetry_age, "bots": [
            {"bot_id": 9152411071, "role": "master", "rate_limited": 0,
             "last_success_at": NOW - master_ok_age, "last_error": None,
             "latency_ms": {"p50": 142.0}},
            {"bot_id": 9126874011, "role": "child", "rate_limited": 1,
             "last_success_at": NOW - 1, "last_error": "network",
             "latency_ms": {"p50": 139.0}},
        ]}), encoding="utf-8")
    (run / "overview.json").write_text(json.dumps({
        "schema": 1, "taken_at": NOW - 5, "running": 1, "waiting": 0, "limit": 50,
        "counts": {"ready": 1 if child_state == "ready" else 0, "crashed": 1 if child_state == "crashed" else 0},
        "bots": [{"bot_id": 9126874011, "user_id": 9202600054, "username": "LiliYourBot", "enabled": True,
                  "runtime": runtime, "state": child_state,
                  "reason_codes": ["polling_stopped"] if child_state == "crashed" else []}],
    }), encoding="utf-8")


def collector(tmp_path: Path, ping=(42.0, None), **kw) -> cli_home.Collector:
    db = tmp_path / "ziko.sqlite3"
    db.write_bytes(b"x" * 1234)
    return cli_home.Collector(data_dir=tmp_path, db_path=db, ping=lambda: ping, clock=lambda: NOW,
                              master_name=lambda: "ZikoAssistantBot", psutil_mod=FakePsutil(), **kw)


SERVICE = {"ActiveState": "active", "LoadState": "loaded", "MainPID": "4242",
           "ActiveEnterTimestampMonotonic": "1"}


def text_of(model: cli_home.HomeModel, *, width: int = 124, ascii_only: bool = False) -> str:
    out = StringIO()
    Console(file=out, width=width, force_terminal=False, color_system=None).print(
        cli_home.render(model, width=width, ascii_only=ascii_only))
    return out.getvalue()


# --------------------------------------------------------------------------- collection
def test_collects_measured_telegram_and_system_values(tmp_path: Path) -> None:
    write_run_files(tmp_path)
    m = collector(tmp_path).collect(service=SERVICE, version="1.5.1",
                                    update={"reachable": True, "available": False}, host="srv")
    assert m.ping_ms == 42.0 and m.bot_api == "connected"
    assert m.master_username == "ZikoAssistantBot" and m.rate_limited == 1
    assert m.child_counts == {"running": 1, "attention": 0, "failed": 0, "waiting": 0}
    assert [b.username for b in m.bots] == ["ZikoAssistantBot", "LiliYourBot"]
    assert m.bots[1].ping_ms == 139.0 and m.bots[1].state == "running"
    assert m.cpu_percent == 14.2 and m.mem_percent == 44.1 and m.disk_percent == 7.6
    assert m.service_rss == 154 * 2**20 and m.db_size == 1234
    assert m.update_text == "up to date" and m.service_state == "active"
    assert m.net_rx_s is None  # the first sample has no delta: never invented


def test_network_rate_is_a_real_delta_between_refreshes(tmp_path: Path) -> None:
    clock = iter([NOW, NOW + 2.0])
    c = collector(tmp_path)
    c._clock = lambda: next(clock)
    c.collect(service=SERVICE, version="1.5.1", update=None)
    m = c.collect(service=SERVICE, version="1.5.1", update=None)
    assert m.net_rx_s == pytest.approx(36_800 / 2) and m.net_tx_s == pytest.approx(12_200 / 2)


def test_history_grows_per_refresh_and_is_bounded(tmp_path: Path) -> None:
    c = collector(tmp_path)
    for _ in range(cli_home.HISTORY + 5):
        m = c.collect(service=SERVICE, version="1.5.1", update=None)
    assert len(m.ping_history) == cli_home.HISTORY and len(m.cpu_history) == cli_home.HISTORY


def test_master_name_is_fetched_at_most_once_per_session(tmp_path: Path) -> None:
    calls = []
    c = collector(tmp_path)
    c._master_name_fn = lambda: calls.append(1) or "ZikoAssistantBot"
    for _ in range(3):
        c.collect(service=SERVICE, version="1.5.1", update=None)
    assert calls == [1]


def test_master_name_failure_is_not_fatal(tmp_path: Path) -> None:
    c = collector(tmp_path)

    def boom():
        raise OSError("no network")
    c._master_name_fn = boom
    m = c.collect(service=SERVICE, version="1.5.1", update=None)
    assert m.master_username is None


def test_stale_or_missing_telemetry_is_reported_not_presented_as_live(tmp_path: Path) -> None:
    m = collector(tmp_path).collect(service=SERVICE, version="1.5.1", update=None)
    assert m.bot_api == "no-data" and m.rate_limited is None
    write_run_files(tmp_path, telemetry_age=cli_home.TELEMETRY_STALE_S + 5)
    m = collector(tmp_path).collect(service=SERVICE, version="1.5.1", update=None)
    assert m.bot_api == "no-data"
    write_run_files(tmp_path, master_ok_age=cli_home.BOT_API_QUIET_S + 5)
    m = collector(tmp_path).collect(service=SERVICE, version="1.5.1", update=None)
    assert m.bot_api == "quiet"


def test_unreachable_telegram_and_failed_child_bot(tmp_path: Path) -> None:
    write_run_files(tmp_path, child_state="crashed")
    m = collector(tmp_path, ping=(None, "TimeoutError")).collect(service=SERVICE, version="1.5.1", update=None)
    assert m.ping_ms is None and m.ping_error == "TimeoutError" and m.ping_history == []
    child = m.bots[-1]
    assert child.state == "failed" and child.note == "polling stopped" and child.note_level == "bad"
    assert m.child_counts["failed"] == 1


def test_degraded_runtime_is_shown_as_degraded(tmp_path: Path) -> None:
    write_run_files(tmp_path, runtime="degraded")
    m = collector(tmp_path).collect(service=SERVICE, version="1.5.1", update=None)
    assert m.bots[-1].state == "degraded"


def test_without_psutil_system_values_are_absent_not_zero(tmp_path: Path) -> None:
    c = collector(tmp_path)
    c._ps = None
    m = c.collect(service=SERVICE, version="1.5.1", update=None)
    assert m.cpu_percent is None and m.mem_percent is None
    assert "n/a" in text_of(m)


def test_inactive_service_has_no_uptime(tmp_path: Path) -> None:
    m = collector(tmp_path).collect(service={"ActiveState": "failed"}, version="1.5.1", update=None)
    assert m.service_state == "failed" and m.service_uptime_s is None


# --------------------------------------------------------------------------- rendering
def test_render_shows_every_section_in_english(tmp_path: Path) -> None:
    write_run_files(tmp_path)
    c = collector(tmp_path)
    c.collect(service=SERVICE, version="1.5.1", update=None)
    m = c.collect(service=SERVICE, version="1.5.1", update={"reachable": True, "available": False},
                  host="srv", last_backup_at=NOW - 600)
    out = text_of(m)
    for needle in ("Telegram", "System", "Bots", "Ping (TCP)", "42 ms", "good", "connected",
                   "@ZikoAssistantBot", "@LiliYourBot", "CPU", "RAM", "Disk", "Net", "v1.5.1",
                   "up to date", "Last backup", "10m 00s ago", "2 samples"):
        assert needle in out, needle
    assert not _PERSIAN.search(out)


@pytest.mark.parametrize("ms,word", [(80.0, "good"), (450.0, "slow"), (1200.0, "very slow")])
def test_ping_thresholds(tmp_path: Path, ms: float, word: str) -> None:
    m = collector(tmp_path, ping=(ms, None)).collect(service=SERVICE, version="1.5.1", update=None)
    assert f"{ms:.0f} ms {word}" in text_of(m, ascii_only=True)


def test_ascii_terminals_get_no_emoji(tmp_path: Path) -> None:
    write_run_files(tmp_path)
    m = collector(tmp_path).collect(service=SERVICE, version="1.5.1", update=None)
    out = text_of(m, ascii_only=True)
    assert all(ord(ch) < 0x2190 or ch in "─│╭╮╯╰━┃·…—" for ch in out), [ch for ch in out if ord(ch) >= 0x2190][:5]


def test_narrow_terminal_stacks_panels(tmp_path: Path) -> None:
    m = collector(tmp_path).collect(service=SERVICE, version="1.5.1", update=None)
    out = text_of(m, width=70)
    assert out.index("Telegram") < out.index("System")
    assert max(len(line) for line in out.splitlines()) <= 70


def test_update_available_is_highlighted() -> None:
    text, level = cli_home._update_line({"reachable": True, "available": True, "stable": "v1.6.0"})
    assert text == "update available · v1.6.0" and level == "warn"


def test_formatters() -> None:
    assert cli_home.human_bytes(None) == "n/a" and cli_home.human_bytes(512) == "512 B"
    assert cli_home.human_bytes(154 * 2**20) == "154 MB"
    assert cli_home.human_duration(3 * 86400 + 4 * 3600 + 17 * 60) == "3d 04:17"
    assert cli_home.sparkline([0, 50, 100], 0, 100, "x").plain == "▁▄█"
    assert cli_home.ping_level(None) == "bad"


def test_tcp_ping_reports_error_class_never_raises() -> None:
    ms, err = cli_home.tcp_ping("127.0.0.1", 9, timeout=0.5)
    assert (ms is None and err) or (ms is not None and err is None)


# --------------------------------------------------------------------------- live view
def test_live_view_refreshes_and_returns_to_menu(tmp_path: Path) -> None:
    write_run_files(tmp_path)
    c = collector(tmp_path)
    calls = []

    def collect():
        calls.append(1)
        return c.collect(service=SERVICE, version="1.5.1", update=None)
    console = Console(file=StringIO(), width=120, force_terminal=False)
    assert cli_home.live(collect, console=console, frames=3, sleep=lambda _s: None) == 0
    assert len(calls) == 3


def test_live_view_ctrl_c_returns_zero(tmp_path: Path) -> None:
    def collect():
        raise KeyboardInterrupt
    console = Console(file=StringIO(), width=120, force_terminal=False)
    assert cli_home.live(collect, console=console, frames=2, sleep=lambda _s: None) == 0


# --------------------------------------------------------------------------- menu wiring
def test_home_screen_and_grid_menu_render_in_run_menu(tmp_path: Path, monkeypatch) -> None:
    write_run_files(tmp_path)
    c = collector(tmp_path)
    out = StringIO()
    monkeypatch.setattr(cli_menu, "console", Console(file=out, theme=cli_menu.THEME, width=124))
    monkeypatch.setattr(cli_menu.console, "input", lambda prompt="": "q")
    rc = cli_menu.run_menu({}, lambda: "active", version="1.5.1",
                           home_fn=lambda: c.collect(service=SERVICE, version="1.5.1", update=None))
    text = out.getvalue()
    assert rc == 0
    for needle in ("Telegram", "System", "Live Monitor", "Security", "r Refresh", "m Live monitor", "q Quit"):
        assert needle in text, needle


def test_home_failure_never_blocks_the_menu(monkeypatch) -> None:
    out = StringIO()
    monkeypatch.setattr(cli_menu, "console", Console(file=out, theme=cli_menu.THEME, width=124))
    monkeypatch.setattr(cli_menu.console, "input", lambda prompt="": "q")

    def broken():
        raise RuntimeError("boom")
    assert cli_menu.run_menu({}, lambda: "active", version="1.5.1", home_fn=broken) == 0
    assert "Dashboard data unavailable (RuntimeError)" in out.getvalue()
    assert "ZIKO Menu" in out.getvalue()


def test_r_and_empty_input_only_redraw() -> None:
    shows, runs = [], []
    stream = iter(["r", "", "q"])
    cli_nav.run_menus(cli_nav.MENUS, {"status": lambda: runs.append(1)},
                      show=lambda menu, stack: shows.append(tuple(stack)),
                      read_choice=lambda _m: next(stream), invalid=lambda raw: runs.append(("bad", raw)),
                      announce=lambda item: None, pause=lambda: None, interrupted=lambda: None)
    assert shows == [("main",)] * 3 and runs == []


def test_m_opens_live_monitor_and_security_submenu_exists() -> None:
    item = cli_nav.find_item(cli_nav.MENUS["main"], "m")
    assert item is not None and item.target == "monitor_live" and item.key == "11"
    sec = cli_nav.MENUS["security"]
    assert {i.target for i in sec.items if i.kind == cli_nav.ACTION} == {
        "security_audit", "security_rotate", "security_move"}
    assert all(i.danger for i in sec.items if i.target in {"security_rotate", "security_move"})


def test_existing_shortcuts_keep_their_numbers() -> None:
    main = {i.key: i for i in cli_nav.MENUS["main"].items}
    assert main["2"].target == "bot" and main["6"].target == "update"
    assert main["8"].target == "backup" and main["9"].target == "logs"
    assert main["0"].kind == cli_nav.EXIT


def test_menu_labels_are_english_only() -> None:
    for menu in cli_nav.MENUS.values():
        for item in menu.items:
            assert not _PERSIAN.search(item.label + item.hint), item.label


def test_last_backup_finds_encrypted_backups(tmp_path: Path, monkeypatch) -> None:
    from app import cli
    (tmp_path / "ziko-20261126T074355Z.60yMPO.sqlite3.age").write_bytes(b"x")
    monkeypatch.setenv("ZIKO_BACKUP_DIR", str(tmp_path))
    mtime, problem = cli._last_backup()
    assert problem is None and mtime is not None and abs(mtime - time.time()) < 120


def test_no_variation_selector_emoji_that_break_terminal_columns() -> None:
    """U+FE0F emoji (e.g. a gear or shield) are drawn 1 or 2 cells wide depending on the
    terminal and shift every column after them. Only single-codepoint emoji are used."""
    import inspect
    for source in (inspect.getsource(cli_home), inspect.getsource(cli_nav)):
        assert "\ufe0f" not in source



# --------------------------------------------------------------------------- 1.5.2 layout
def _menu_text(width: int, monkeypatch, *, plain: bool = False) -> str:
    out = StringIO()
    con = Console(file=out, theme=cli_menu.THEME, width=width, color_system=None if plain else "truecolor",
                  force_terminal=not plain, legacy_windows=False)
    monkeypatch.setattr(cli_menu, "console", con)
    con.print(cli_menu._main_grid(cli_nav.MENUS["main"]))
    con.print(cli_menu._footer(["main"]))
    return re.sub(r"\x1b\[[0-9;]*m", "", out.getvalue())


@pytest.mark.parametrize("width", [40, 60, 80, 100, 124, 160, 200])
def test_dashboard_never_overflows_the_terminal(tmp_path: Path, width: int) -> None:
    write_run_files(tmp_path)
    c = collector(tmp_path)
    c.collect(service=SERVICE, version="1.5.2", update=None)
    m = c.collect(service=SERVICE, version="1.5.2", update={"reachable": True, "available": False},
                  host="ip-172-31-1-185", last_backup_at=NOW - 60)
    out = StringIO()
    Console(file=out, width=width, color_system=None, legacy_windows=False).print(
        cli_home.render(m, width=width))
    lines = out.getvalue().splitlines()
    from rich.cells import cell_len
    assert max(cell_len(line) for line in lines) <= width
    assert any("Telegram" in line for line in lines) and any("System" in line for line in lines)


@pytest.mark.parametrize("width", [40, 70, 100, 124, 200])
def test_menu_grid_shows_all_twelve_items_at_every_width(monkeypatch, width: int) -> None:
    from rich.cells import cell_len
    text = _menu_text(width, monkeypatch)
    for item in cli_nav.MENUS["main"].items:
        if item.kind != cli_nav.EXIT:
            assert item.label.split()[0] in text, (width, item.label)
            assert item.key in text
    assert max(cell_len(line) for line in text.splitlines()) <= width


def test_wide_menu_is_six_columns_by_two_rows() -> None:
    assert cli_menu._menu_columns(124) == 6 and cli_menu._menu_columns(100) == 4
    assert cli_menu._menu_columns(80) == 3 and cli_menu._menu_columns(30) == 1
    entries = [i for i in cli_nav.MENUS["main"].items if i.kind != cli_nav.EXIT]
    assert len(entries) == 12 and [i.key for i in entries] == [str(n) for n in range(1, 13)]


def test_menu_without_colour_or_unicode_has_no_emoji(monkeypatch) -> None:
    text = _menu_text(124, monkeypatch, plain=True)
    assert not any(ord(ch) > 0x2FFF for ch in text)
    assert "1 Dashboard" in text and "12 Help / About" in text


def test_long_and_missing_values_are_safe(tmp_path: Path) -> None:
    from rich.cells import cell_len
    m = cli_home.HomeModel(version="", host="h" * 200, service_state="inactive")
    m.bots = [cli_home.BotLine(username="x" * 120, role="child", state="weird", ping_ms=None,
                               note="n" * 300, note_level="bad")]
    for width in (40, 80, 124):
        out = StringIO()
        Console(file=out, width=width, color_system=None).print(cli_home.render(m, width=width))
        text = out.getvalue()
        assert max(cell_len(line) for line in text.splitlines()) <= width
        assert "version unknown" in text and "n/a" in text and "…" in text


def test_main_prompt_shows_the_numbered_range(monkeypatch) -> None:
    prompts = []
    out = StringIO()
    monkeypatch.setattr(cli_menu, "console", Console(file=out, theme=cli_menu.THEME, width=124))
    monkeypatch.setattr(cli_menu.console, "input", lambda prompt="": prompts.append(str(prompt)) or "q")
    m = cli_home.HomeModel(version="1.5.2")
    assert cli_menu.run_menu({}, lambda: "active", version="1.5.2", home_fn=lambda: m) == 0
    assert prompts == ["Enter a number (1-12): "]


@pytest.mark.parametrize("key,target", [
    ("1", "status"), ("4", "autoreply"), ("5", "business"), ("6", "update"), ("7", "doctor"),
    ("11", "monitor_live"), ("m", "monitor_live"), ("12", "help")])
def test_numbered_actions_still_run(key: str, target: str) -> None:
    ran = []
    stream = iter([key, "q"])
    cli_nav.run_menus(cli_nav.MENUS, {target: lambda: ran.append(target)},
                      show=lambda menu, stack: None, read_choice=lambda _m: next(stream),
                      invalid=lambda raw: ran.append(("bad", raw)), announce=lambda item: None,
                      pause=lambda: None, interrupted=lambda: None)
    assert ran == [target]


@pytest.mark.parametrize("key,submenu", [("2", "bot"), ("3", "tenants"), ("8", "backup"), ("9", "logs"),
                                         ("10", "security")])
def test_numbered_submenus_still_open(key: str, submenu: str) -> None:
    shown = []
    stream = iter([key, "0", "q"])
    cli_nav.run_menus(cli_nav.MENUS, {}, show=lambda menu, stack: shown.append(tuple(stack)),
                      read_choice=lambda _m: next(stream), invalid=lambda raw: None,
                      announce=lambda item: None, pause=lambda: None, interrupted=lambda: None)
    assert ("main", submenu) in shown



@pytest.mark.parametrize("width", [44, 60, 80, 100, 119])
def test_narrow_menu_is_one_line_per_item_without_nested_boxes(monkeypatch, width: int) -> None:
    text = _menu_text(width, monkeypatch)
    body = [line for line in text.splitlines() if line.startswith("│")]
    assert len(body) <= 6                              # 12 items in at most 6 compact rows
    assert not any("╭" in line[1:] for line in body)   # no cards inside the panel

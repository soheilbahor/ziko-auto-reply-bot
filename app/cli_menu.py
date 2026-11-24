"""ZIKO CLI presentation layer: Rich theme, logo, tables and the interactive menu.

This module only renders and prompts. It holds no service logic and never
reads configuration, so it cannot leak secrets by itself.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import re

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

CYAN = (34, 211, 238)
VIOLET = (139, 92, 246)

THEME = Theme(
    {
        "ok": "bold green",
        "warn": "bold yellow",
        "err": "bold red",
        "info": "cyan",
        "muted": "dim",
        "accent": "bold #8b5cf6",
    }
)
console = Console(theme=THEME, highlight=False)
err_console = Console(theme=THEME, highlight=False, stderr=True)

# Original 5-row block lettering, 6 columns per glyph.
_GLYPHS = {
    "Z": ("██████", "   ██ ", "  ██  ", " ██   ", "██████"),
    "I": ("██████", "  ██  ", "  ██  ", "  ██  ", "██████"),
    "K": ("██  ██", "██ ██ ", "████  ", "██ ██ ", "██  ██"),
    "O": (" ████ ", "██  ██", "██  ██", "██  ██", " ████ "),
}
_TAGLINE = "Telegram Business auto-reply assistant"

# state -> (label, style)
_STATES = {
    "active": ("Active", "ok"),
    "activating": ("Starting", "warn"),
    "reloading": ("Reloading", "warn"),
    "deactivating": ("Stopping", "warn"),
    "inactive": ("Inactive", "err"),
    "failed": ("Failed", "err"),
    "not-found": ("Not installed", "err"),
    "unknown": ("Unknown", "warn"),
}
_LEVELS = {"ok": ("✔", "ok"), "warn": ("!", "warn"), "fail": ("✘", "err")}

MENU_ITEMS: Sequence[tuple[str, str, str]] = (
    ("1", "Dashboard / service status", "status"),
    ("2", "List registered child bots", "bots"),
    ("3", "Start service", "start"),
    ("4", "Stop service", "stop"),
    ("5", "Restart service", "restart"),
    ("6", "View logs", "logs"),
    ("7", "Create database backup", "backup"),
    ("8", "Restore database backup", "restore"),
    ("9", "Update Center", "update"),
    ("10", "Configuration", "config"),
    ("11", "Diagnostics", "doctor"),
    ("12", "Help & About", "help"),
    ("13", "Uninstall ZIKO", "uninstall"),
)
MENU_SECTIONS: Sequence[tuple[str, tuple[str, ...]]] = (
    ("OVERVIEW", ("status", "bots")),
    ("SERVICE CONTROL", ("start", "stop", "restart", "logs")),
    ("DATA & MAINTENANCE", ("backup", "restore", "update", "config", "doctor")),
    ("INFORMATION", ("help", "uninstall")),
)


def _lerp(t: float) -> str:
    r, g, b = (round(a + (c - a) * t) for a, c in zip(CYAN, VIOLET))
    return f"rgb({r},{g},{b})"


def _format_version_badge(version: str) -> str:
    value = version.strip()
    if not value:
        return ""
    if re.fullmatch(r"v?\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?", value):
        return f"v{value.removeprefix('v')}"
    if re.fullmatch(r"dev-[0-9A-Za-z.-]+", value):
        return value
    if re.fullmatch(r"[0-9a-fA-F]{7,40}", value):
        return f"commit {value.lower()}"
    return value


def logo(version: str = "") -> Text:
    """Cyan-to-violet logo with a normalized version badge."""
    badge = _format_version_badge(version)
    if console.options.ascii_only:
        return Text(" ".join(part for part in ("ZIKO", badge) if part), style="bold cyan")
    rows = ["  ".join(_GLYPHS[ch][i] for ch in "ZIKO") for i in range(5)]
    width = max(len(r) for r in rows)
    text = Text()
    for row in rows:
        for x, ch in enumerate(row):
            text.append(ch, style=_lerp(x / (width - 1)) if ch != " " else None)
        text.append("\n")
    text.append(_TAGLINE, style="cyan")
    if badge:
        text.append("  ·  ", style="muted")
        text.append(badge, style="accent")
    return text


def print_logo(version: str = "", version_error: str | None = None) -> None:
    console.print(logo(version))
    if version_error:
        err_console.print(Text.assemble(("Version unavailable: ", "err"), version_error))
    console.print()


def render_help_about(
    *, version: str, version_error: str | None,
    commands: Sequence[tuple[str, str]],
) -> None:
    rows: list[tuple[str, str | Text]] = [
        ("Product", "ZIKO Telegram Assistant Platform"),
        ("Version", _format_version_badge(version) or "Unavailable"),
        ("Runtime", "Python application managed by systemd"),
    ]
    if version_error:
        rows.append(("Version source", version_error))
    console.print(_grid("About ZIKO", rows, style="#8b5cf6"))
    table = Table(box=box.SIMPLE_HEAD, header_style="bold cyan")
    table.add_column("Command")
    table.add_column("Description", overflow="fold")
    for command, description in commands:
        table.add_row(command, description)
    console.print(Panel(table, title="CLI Help", border_style="cyan"))


def render_bot_list(rows: Sequence[tuple[object, ...]]) -> None:
    table = Table(box=box.SIMPLE_HEAD, header_style="bold cyan")
    table.add_column("Owner ID", justify="right")
    table.add_column("Bot ID", justify="right")
    table.add_column("Username")
    table.add_column("Registry")
    table.add_column("Created")
    for user_id, bot_id, username, enabled, created_at in rows:
        state = Text("Enabled", style="ok") if enabled else Text("Disabled", style="err")
        table.add_row(str(user_id), str(bot_id), f"@{username}", state, str(created_at))
    console.print(Panel(table, title=f"Registered Child Bots · {len(rows)}", border_style="cyan"))


def indicator(state: str) -> Text:
    label, style = _STATES.get(state, _STATES["unknown"])
    return Text.assemble(("● ", style), (label, style))


def ok(message: str) -> None:
    console.print(Text.assemble(("✔ ", "ok"), message))


def warn(message: str) -> None:
    console.print(Text.assemble(("! ", "warn"), message))


def error(message: str) -> None:
    err_console.print(Text.assemble(("✘ ", "err"), message))


def info(message: str) -> None:
    console.print(Text.assemble(("ℹ ", "info"), message))


def confirm(question: str, default: bool = False) -> bool:
    hint = "[Y/n]" if default else "[y/N]"
    try:
        answer = console.input(Text.assemble(("? ", "warn"), question, (f" {hint} ", "muted")))
    except EOFError:
        return False
    answer = answer.strip().lower()
    if not answer:
        return default
    return answer in {"y", "yes"}


def ask(question: str) -> str:
    try:
        return console.input(Text.assemble(("❯ ", "accent"), question, ": ")).strip()
    except EOFError:
        return ""


def _grid(title: str, rows: Sequence[tuple[str, str | Text]], style: str = "cyan") -> Panel:
    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold")
    table.add_column()
    for label, value in rows:
        table.add_row(label, value)
    return Panel(table, title=title, title_align="left", border_style=style, box=box.ROUNDED)


def render_status(rows: Sequence[tuple[str, str | Text]]) -> None:
    console.print(_grid("ZIKO Status", rows))


def render_config(path: str, rows: Sequence[tuple[str, str | Text]]) -> None:
    console.print(_grid(f"Configuration  ·  {path}", rows, style="#8b5cf6"))


def render_checks(checks: Sequence[tuple[str, str, str]]) -> None:
    """checks: (level ok|warn|fail, title, detail)."""
    table = Table(box=box.SIMPLE_HEAD, header_style="bold cyan", show_edge=False)
    table.add_column("", width=1, no_wrap=True)
    table.add_column("Check")
    table.add_column("Details", overflow="fold")
    for level, title, detail in checks:
        mark, style = _LEVELS[level]
        table.add_row(Text(mark, style=style), title, Text(detail, style="muted"))
    console.print(table)
    fails = sum(1 for c in checks if c[0] == "fail")
    warns = sum(1 for c in checks if c[0] == "warn")
    if fails:
        error(f"Found {fails} critical issue(s) and {warns} warning(s).")
    elif warns:
        warn(f"System is running, but {warns} warning(s) remain.")
    else:
        ok("All checks passed.")


def run_menu(
    actions: Mapping[str, Callable[[], int | None]],
    state_fn: Callable[[], str],
    version: str | Callable[[], str] = "",
    version_error_fn: Callable[[], str | None] | None = None,
) -> int:
    """Interactive menu; version is refreshed on every render."""
    while True:
        if console.is_terminal:
            console.clear()
        current_version = version() if callable(version) else version
        version_error = version_error_fn() if version_error_fn else None
        print_logo(current_version, version_error=version_error)
        console.print(Panel(
            Text.assemble(("Service: ", "bold"), indicator(state_fn())),
            title="Dashboard", border_style="cyan", box=box.ROUNDED,
        ))
        for section, action_names in MENU_SECTIONS:
            table = Table.grid(padding=(0, 2))
            table.add_column(justify="right", style="accent")
            table.add_column()
            for key, label, name in MENU_ITEMS:
                if name in action_names:
                    table.add_row(key, Text(label, style="err" if name == "uninstall" else ""))
            console.print(Panel(table, title=section, border_style="cyan", box=box.ROUNDED))
        console.print("[muted]0  Exit[/muted]")
        try:
            choice = console.input(Text.assemble(("❯ ", "accent"), "Select an option: ")).strip()
        except (EOFError, KeyboardInterrupt):
            console.print()
            return 0
        if choice in {"0", "q", "exit", "quit"}:
            return 0
        name = next((n for k, _, n in MENU_ITEMS if k == choice), None)
        if name is None or name not in actions:
            warn("Invalid option.")
            try:
                console.input(Text("Press Enter to continue", style="muted"))
            except (EOFError, KeyboardInterrupt):
                return 0
            continue
        console.print()
        try:
            actions[name]()
        except KeyboardInterrupt:
            console.print()
            info("Returning to menu.")
        try:
            console.input(Text("\nPress Enter to return to the menu ↵ ", style="muted"))
        except (EOFError, KeyboardInterrupt):
            console.print()
            return 0

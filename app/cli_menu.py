"""ZIKO CLI presentation layer: Rich theme, logo, tables and the interactive menu.

This module only renders and prompts. It holds no service logic and never
reads configuration, so it cannot leak secrets by itself.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

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
    ("1", "Service status", "status"),
    ("2", "Start service", "start"),
    ("3", "Stop service", "stop"),
    ("4", "Restart service", "restart"),
    ("5", "View logs", "logs"),
    ("6", "Backup", "backup"),
    ("7", "Restore backup", "restore"),
    ("8", "Update", "update"),
    ("9", "Configuration", "config"),
    ("10", "Diagnostics (doctor)", "doctor"),
    ("11", "Version", "version"),
    ("12", "Uninstall ZIKO", "uninstall"),
)


def _lerp(t: float) -> str:
    r, g, b = (round(a + (c - a) * t) for a, c in zip(CYAN, VIOLET))
    return f"rgb({r},{g},{b})"


def logo(version: str = "") -> Text:
    """Cyan -> violet gradient logo (plain text on non-UTF-8 terminals)."""
    if console.options.ascii_only:
        return Text(f"ZIKO {version}".strip(), style="bold cyan")
    rows = ["  ".join(_GLYPHS[ch][i] for ch in "ZIKO") for i in range(5)]
    width = max(len(r) for r in rows)
    text = Text()
    for row in rows:
        for x, ch in enumerate(row):
            text.append(ch, style=_lerp(x / (width - 1)) if ch != " " else None)
        text.append("\n")
    text.append(_TAGLINE, style="cyan")
    if version:
        text.append("  ·  ", style="muted")
        text.append(f"v{version}", style="accent")
    return text


def print_logo(version: str = "") -> None:
    console.print(logo(version))
    console.print()


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
    version: str = "",
) -> int:
    """Interactive loop. Ctrl-C inside an action returns to the menu; at the prompt it exits."""
    while True:
        if console.is_terminal:
            console.clear()
        print_logo(version)
        console.print(Text.assemble(("Service status: ", "bold"), indicator(state_fn())))
        console.print()
        table = Table.grid(padding=(0, 2))
        table.add_column(justify="right", style="accent")
        table.add_column()
        for key, label, name in MENU_ITEMS:
            table.add_row(key, Text(label, style="err" if name == "uninstall" else ""))
        table.add_row("0", Text("Exit", style="muted"))
        console.print(Panel(table, title="ZIKO Menu", title_align="left",
                            border_style="cyan", box=box.ROUNDED))
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
            console.input(Text("Enter ↵", style="muted"))
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

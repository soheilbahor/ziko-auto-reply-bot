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

from app import cli_nav

CYAN = (34, 211, 238)
VIOLET = (139, 92, 246)
NARROW = 60
MAX_WIDTH = 80

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
_LEVELS = {
    "ok": ("✔", "[OK]", "ok"),
    "warn": ("!", "[WARN]", "warn"),
    "fail": ("✘", "[FAIL]", "err"),
    "info": ("ℹ", "[INFO]", "info"),
}


def _ascii(con: Console | None = None) -> bool:
    return (con or console).options.ascii_only


def _plain(con: Console | None = None) -> bool:
    con = con or console
    return bool(con.no_color) or con.color_system is None or _ascii(con)


def _sym(fancy: str, plain: str) -> str:
    return plain if _ascii() else fancy


def _sep() -> str:
    return _sym("  ·  ", "  |  ")


def _marker(level: str, con: Console | None = None) -> tuple[str, str]:
    symbol, fallback, style = _LEVELS[level]
    return (fallback if _plain(con) else symbol), style


def _panel_width() -> int:
    return min(console.width, MAX_WIDTH)


def _narrow() -> bool:
    return console.width < NARROW


def _t(value: str | Text) -> Text:
    return Text(value) if isinstance(value, str) else value

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
    if _ascii() or _narrow():
        text = Text("ZIKO", style="bold cyan")
        if badge:
            text.append(_sep(), style="muted")
            text.append(badge, style="accent")
        return text
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
    return Text.assemble((_sym("●", "*") + " ", style), (label, style))


def _say(con: Console, level: str, message: str) -> None:
    mark, style = _marker(level, con)
    con.print(Text.assemble((mark + " ", style), message))


def ok(message: str) -> None:
    _say(console, "ok", message)


def warn(message: str) -> None:
    _say(console, "warn", message)


def error(message: str) -> None:
    _say(err_console, "fail", message)


def info(message: str) -> None:
    _say(console, "info", message)


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
    table = Table.grid(padding=(0, 1) if _narrow() else (0, 2))
    table.add_column(style="bold", no_wrap=True)
    table.add_column(overflow="fold")
    for label, value in rows:
        table.add_row(label, _t(value))
    return Panel(table, title=title, title_align="left", border_style=style,
                 box=box.ROUNDED, width=_panel_width(), padding=(0, 1) if _narrow() else (0, 2))


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


def render_update_center(status: Mapping[str, object]) -> None:
    current = str(status.get("current", "unknown"))
    reachable = bool(status.get("reachable"))
    stable = str(status.get("stable", "Unavailable")) or "Unavailable"
    beta = str(status.get("beta", "Unavailable")) or "Unavailable"
    available = bool(status.get("available"))
    state = Text("● Update available", style="ok") if reachable and available else (
        Text("● Up to date", style="ok") if reachable else Text("● Status unavailable", style="warn")
    )
    console.print(_grid("UPDATE", [
        ("Currently installed", _format_version_badge(current) or current),
        ("Latest stable", _format_version_badge(stable) if stable != "Unavailable" else stable),
        ("Latest main / beta", beta),
        ("Status", state),
    ], style="#8b5cf6"))
    console.print("[bold cyan]SELECT VERSION[/bold cyan]")
    console.print("[1] Automatic  (latest stable release)")
    console.print("[2] Choose a specific release version")
    console.print("[3] Beta       (latest main branch - may be unstable)")
    console.print("[0] Back to menu")


def render_release_choices(releases: Sequence[tuple[str, str]]) -> None:
    table = Table(box=box.SIMPLE_HEAD, header_style="bold cyan")
    table.add_column("#", justify="right", style="accent")
    table.add_column("Version")
    table.add_column("Release")
    for index, (tag, name) in enumerate(releases, 1):
        table.add_row(str(index), tag, name)
    console.print(Panel(table, title="Available Stable Releases", border_style="cyan"))


def section(title: str) -> None:
    console.print(Panel(Text(title, style="bold cyan"), border_style="cyan", box=box.SIMPLE))


_GUIDES = {
    "tenants": (
        "Tenant Management",
        "Each user can connect one dedicated bot. Registration and lifecycle are managed by ZIKO.",
        [
            "Users create a bot through Telegram's official managed-bot flow or connect an existing bot token privately.",
            "Bot tokens are validated and stored encrypted; never request or share tokens in support chats.",
            "Use 'ziko bots list' to inspect registered child bots without revealing credentials.",
            "Use the main service logs and diagnostics when a child bot fails to start.",
        ],
    ),
    "autoreply": (
        "Auto Reply Settings",
        "Auto-reply behavior is configured from the Telegram bot interface and Telegram Business connection.",
        [
            "Open your dedicated ZIKO bot and use /start to open its controls.",
            "Configure the reply text and the enabled behavior from the bot's settings.",
            "For personal-account replies, connect the bot under Telegram Settings → Telegram Business → Chatbots.",
        ],
    ),
    "business": (
        "Telegram Business",
        "Connect a bot to your Telegram Business account to allow it to reply to eligible chats.",
        [
            "Enable Business Mode for the bot in @BotFather if required.",
            "Open Telegram Settings → Telegram Business → Chatbots.",
            "Select the bot and grant the 'Reply to messages' permission for the chats it may answer.",
            "Return to ZIKO and check the connection status; confirm that reply permission is active.",
        ],
    ),
}


def show_guide(key: str) -> int:
    guide = _GUIDES.get(key)
    if guide is None:
        warn("Guide not found.")
        return 2
    title, intro, points = guide
    table = Table.grid(padding=(0, 1))
    table.add_column(justify="right", style="accent", no_wrap=True)
    table.add_column(overflow="fold")
    for index, point in enumerate(points, 1):
        table.add_row(f"{index}.", point)
    body = Table.grid()
    body.add_column(overflow="fold")
    body.add_row(intro)
    body.add_row("")
    body.add_row(table)
    console.print(Panel(body, title=title, border_style="cyan", box=box.ROUNDED))
    return 0


def _menu_panel(menu: cli_nav.Menu, stack: Sequence[str]) -> Panel:
    table = Table.grid(padding=(0, 2))
    table.add_column(justify="right", style="accent", no_wrap=True)
    table.add_column(no_wrap=not _narrow(), overflow="fold")
    if not _narrow():
        table.add_column(style="muted", overflow="ellipsis")
    for item in menu.items:
        quiet = item.kind in (cli_nav.BACK, cli_nav.EXIT)
        style = "err" if item.danger else "muted" if quiet else ""
        if _narrow():
            table.add_row(item.key, Text(item.label, style=style))
        else:
            table.add_row(item.key, Text(item.label, style=style), item.hint)
    title = _sym(" › ", " > ").join(cli_nav.breadcrumb(stack))
    return Panel(table, title=title, border_style="cyan", box=box.ROUNDED,
                 width=_panel_width(), padding=(0, 1) if _narrow() else (0, 2))


def _footer(stack: Sequence[str]) -> Text:
    if len(stack) == 1:
        return Text("Enter a number  |  q or 0 = quit", style="muted")
    return Text("Enter a number  |  b or 0 = back  |  q = quit", style="muted")


def run_menu(
    actions: Mapping[str, Callable[[], int | None]],
    state_fn: Callable[[], str],
    version: str | Callable[[], str] = "",
    version_error_fn: Callable[[], str | None] | None = None,
    update_status_fn: Callable[[], Mapping[str, object]] | None = None,
) -> int:
    """Nested CLI menu. All existing command handlers remain injected by app.cli."""
    def show(menu: cli_nav.Menu, stack: Sequence[str]) -> None:
        if console.is_terminal:
            console.clear()
        current = version() if callable(version) else version
        version_error = version_error_fn() if version_error_fn else None
        print_logo(current, version_error=version_error)
        console.print(Panel(
            Text.assemble(("Service: ", "bold"), indicator(state_fn())),
            title="Dashboard", border_style="cyan", box=box.ROUNDED,
        ))
        if len(stack) == 1 and update_status_fn:
            try:
                update = update_status_fn()
                if update.get("reachable") and update.get("available"):
                    update_badge = Text.assemble(("● ", "ok"), (f"Update available · v{update.get('stable')}", "ok"))
                elif update.get("reachable"):
                    update_badge = Text.assemble(("● ", "ok"), ("System up to date", "ok"))
                else:
                    update_badge = Text.assemble(("● ", "warn"), ("Update status unavailable", "warn"))
                console.print(Panel(update_badge, title="UPDATE", border_style="cyan", box=box.ROUNDED))
            except Exception:
                console.print(Panel(Text("Update status unavailable", style="warn"), title="UPDATE", border_style="cyan"))
        console.print(_menu_panel(menu, stack))
        console.print(_footer(stack))

    def read_choice(_menu: cli_nav.Menu) -> str:
        return console.input(Text.assemble(("❯ ", "accent"), "Select an option: "))

    def invalid(raw: str) -> None:
        warn(f"Invalid option: {raw.strip()[:20]!r}. Enter one of the numbers shown.")
        _wait_for_enter()

    def interrupted() -> None:
        info("Action interrupted. Returning to menu.")

    def pause() -> None:
        _wait_for_enter("Press Enter to return to the menu")

    try:
        return cli_nav.run_menus(
            cli_nav.MENUS, actions, show=show, read_choice=read_choice,
            invalid=invalid, announce=lambda item: section(item.label),
            pause=pause, interrupted=interrupted,
        )
    finally:
        console.print()


def _wait_for_enter(message: str = "Press Enter to continue") -> None:
    try:
        console.input(Text(message, style="muted"))
    except (EOFError, KeyboardInterrupt):
        pass

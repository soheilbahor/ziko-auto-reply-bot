"""ZIKO CLI menu structure and navigation (no Rich, no I/O of its own).

This module only describes the menus and runs the navigation loop. All printing and
prompting is injected by the caller (see ``cli_menu.run_menu``), which keeps the logic
easy to test and guarantees that navigation can never run a service command by itself:
an action only runs when the caller supplied a handler for it.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

# Words that always mean "go back one level" (or leave, from the main menu).
BACK_WORDS = frozenset({"0", "b", "back", "q", "quit", "exit"})
# Words that redraw the current screen (fresh measurements) without running anything.
REFRESH_WORDS = frozenset({"r", "refresh", ""})

ACTION, SUBMENU, BACK, EXIT = "action", "submenu", "back", "exit"


@dataclass(frozen=True)
class Item:
    key: str
    label: str
    hint: str
    kind: str = ACTION
    target: str = ""  # action name for ACTION, menu name for SUBMENU
    danger: bool = False
    icon: str = ""     # shown only on terminals that can display emoji
    aliases: tuple[str, ...] = ()  # extra single-word shortcuts, e.g. "m" for Live Monitor


@dataclass(frozen=True)
class Menu:
    name: str
    title: str
    items: Sequence[Item]
    parent: str | None = None


def _back() -> Item:
    return Item("0", "Back", "Return to the previous menu", BACK)


MENUS: Mapping[str, Menu] = {
    "main": Menu("main", "ZIKO Menu", (
        Item("1", "Dashboard", "Service, uptime, database and backups", ACTION, "status", icon="📊"),
        Item("2", "Bot Management", "Start, stop, restart, configuration", SUBMENU, "bot", icon="🤖"),
        Item("3", "Tenants", "Child bot status and inspection", SUBMENU, "tenants", icon="👥"),
        Item("4", "Auto Reply", "Where replies are configured", ACTION, "autoreply", icon="💬"),
        Item("5", "Telegram Business", "Connection setup checklist", ACTION, "business", icon="💼"),
        Item("6", "Update", "Update to a newer version", ACTION, "update", icon="🔼"),
        Item("7", "Diagnostics", "Full diagnostics (doctor)", ACTION, "doctor", icon="🩺"),
        Item("8", "Backup & Restore", "Back up or restore the database", SUBMENU, "backup", icon="🔐"),
        Item("9", "Logs", "Recent or live service logs", SUBMENU, "logs", icon="📜"),
        Item("10", "Security", "Audit, key rotation, key location", SUBMENU, "security", icon="🔒"),
        Item("11", "Live Monitor", "Full-screen live view, refreshed every 2 s", ACTION, "monitor_live",
             icon="📈", aliases=("m",)),
        Item("12", "Help / About", "Version, commands and keyboard help", ACTION, "help", icon="❓"),
        Item("0", "Exit", "Close the menu", EXIT, icon="🚪"),
    )),
    "tenants": Menu("tenants", "Tenants", (
        Item("1", "Child bot status", "Every child bot with an English reason", ACTION, "bots_status"),
        Item("2", "Inspect one bot", "Telemetry, database and log checks for a bot ID", ACTION, "bots_inspect"),
        Item("3", "Registered child bots", "Registry list (tokens are never shown)", ACTION, "bots"),
        Item("4", "How tenants work", "Read-only guide", ACTION, "tenants"),
        _back(),
    ), parent="main"),
    "security": Menu("security", "Security", (
        Item("1", "Security audit", "Permissions, hardening, key, backups (read-only)", ACTION, "security_audit"),
        Item("2", "Rotate token key", "Re-encrypt every child-bot token (service must be stopped)",
             ACTION, "security_rotate", True),
        Item("3", "Move token key", "Move the key out of the data directory or back",
             ACTION, "security_move", True),
        _back(),
    ), parent="main"),
    "bot": Menu("bot", "Bot Management", (
        Item("1", "Start service", "Start the ziko service", ACTION, "start"),
        Item("2", "Stop service", "Stop the ziko service", ACTION, "stop"),
        Item("3", "Restart service", "Restart the ziko service", ACTION, "restart"),
        Item("4", "Configuration", "View settings (token is never shown)", ACTION, "config"),
        Item("5", "List registered child bots", "Inspect child bots without exposing tokens", ACTION, "bots"),
        Item("6", "Uninstall ZIKO", "Remove ZIKO from this server", ACTION, "uninstall", True),
        _back(),
    ), parent="main"),
    "backup": Menu("backup", "Backup & Restore", (
        Item("1", "Back up now", "Create a database backup", ACTION, "backup"),
        Item("2", "Restore from file", "Replace the database from a backup", ACTION, "restore"),
        _back(),
    ), parent="main"),
    "logs": Menu("logs", "Logs", (
        Item("1", "Last 50 lines", "Show recent log lines", ACTION, "logs_tail"),
        Item("2", "Follow live", "Stream logs (Ctrl+C to stop)", ACTION, "logs_follow"),
        Item("3", "Choose line count", "Show a custom number of lines", ACTION, "logs_custom"),
        _back(),
    ), parent="main"),
}


def action_names(menus: Mapping[str, Menu] = MENUS) -> set[str]:
    """Every action name the menus can request (used to check handlers are wired)."""
    return {i.target for m in menus.values() for i in m.items if i.kind == ACTION}


def find_item(menu: Menu, choice: str) -> Item | None:
    choice = choice.strip().lower()
    for item in menu.items:
        if item.key == choice or choice in item.aliases:
            return item
    return None


def breadcrumb(stack: Sequence[str], menus: Mapping[str, Menu] = MENUS) -> list[str]:
    return [menus[name].title for name in stack]


def run_menus(
    menus: Mapping[str, Menu],
    actions: Mapping[str, Callable[[], object]],
    *,
    show: Callable[[Menu, Sequence[str]], None],
    read_choice: Callable[[Menu], str],
    invalid: Callable[[str], None],
    announce: Callable[[Item], None],
    pause: Callable[[], None],
    interrupted: Callable[[], None],
) -> int:
    """Run the menu loop. Returns the exit code (always 0 for a normal exit).

    * Ctrl-C / end-of-input at a menu prompt leaves the program.
    * Ctrl-C inside an action returns to the menu.
    * ``0``, ``b`` and ``q`` go back one level; from the main menu they exit.
    * ``r`` or an empty line redraws the screen (fresh measurements).
    * Unknown input, or an item without a handler, only shows ``invalid``.
    """
    stack = ["main"]
    while stack:
        menu = menus[stack[-1]]
        show(menu, list(stack))
        try:
            raw = read_choice(menu)
        except (EOFError, KeyboardInterrupt):
            return 0
        choice = raw.strip().lower()
        if choice in REFRESH_WORDS and find_item(menu, choice) is None:
            continue  # redraw with fresh measurements; nothing runs
        if choice in BACK_WORDS and find_item(menu, choice) is None:
            stack.pop()
            continue
        item = find_item(menu, choice)
        if item is None:
            invalid(raw)
            continue
        if item.kind in (BACK, EXIT):
            stack.pop()
            continue
        if item.kind == SUBMENU:
            if item.target in menus:
                stack.append(item.target)
            else:
                invalid(raw)
            continue
        handler = actions.get(item.target)
        if handler is None:
            invalid(raw)
            continue
        announce(item)
        try:
            handler()
        except KeyboardInterrupt:
            interrupted()
        try:
            pause()
        except (EOFError, KeyboardInterrupt):
            return 0
    return 0

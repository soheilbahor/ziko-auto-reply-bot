from __future__ import annotations

from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console

from app import cli, cli_menu


def test_version_marker_normalizes_stable_and_dev(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(cli, "HOME", tmp_path)
    marker = tmp_path / "VERSION"
    marker.write_text("v1.0.6\n", encoding="utf-8")
    assert cli.resolve_installed_version() == "1.0.6"
    marker.write_text("v1.0.7-dev.1\n", encoding="utf-8")
    assert cli.resolve_installed_version() == "1.0.7-dev.1"


def test_version_marker_rejects_missing_empty_and_malformed(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="not found"):
        cli.resolve_installed_version(tmp_path)
    marker = tmp_path / "VERSION"
    marker.write_text("  \n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="empty"):
        cli.resolve_installed_version(tmp_path)
    marker.write_text("not-a-version", encoding="utf-8")
    with pytest.raises(RuntimeError, match="Malformed"):
        cli.resolve_installed_version(tmp_path)


def test_menu_formats_versions_and_refreshes_after_update(tmp_path: Path, monkeypatch) -> None:
    marker = tmp_path / "VERSION"
    marker.write_text("1.0.6\n", encoding="utf-8")
    monkeypatch.setattr(cli, "HOME", tmp_path)
    output, errors = StringIO(), StringIO()
    monkeypatch.setattr(cli_menu, "console", Console(file=output, theme=cli_menu.THEME, width=100))
    monkeypatch.setattr(cli_menu, "err_console", Console(file=errors, theme=cli_menu.THEME, stderr=True, width=100))
    rendered: list[str] = []
    monkeypatch.setattr(cli_menu, "print_logo", lambda version="", version_error=None: rendered.append(version))
    inputs = iter(["9", "", "0"])
    monkeypatch.setattr(cli_menu.console, "input", lambda prompt="": next(inputs))

    def update() -> int:
        marker.write_text("1.0.7\n", encoding="utf-8")
        return 0

    actions = {name: (lambda: 0) for _, _, name in cli_menu.MENU_ITEMS}
    actions["update"] = update
    assert cli_menu.run_menu(actions, lambda: "active", version=cli._version,
                             version_error_fn=cli._version_error) == 0
    assert rendered[:2] == ["1.0.6", "1.0.7"]


def test_menu_has_help_bots_and_safe_exit() -> None:
    names = {name for _, _, name in cli_menu.MENU_ITEMS}
    assert {"status", "bots", "help", "uninstall"} <= names
    assert next(key for key, _, name in cli_menu.MENU_ITEMS if name == "uninstall") == "13"
    assert cli_menu._format_version_badge("1.0.6") == "v1.0.6"
    assert cli_menu._format_version_badge("a81f92c") == "commit a81f92c"


def test_cli_parser_supports_bot_inventory_and_update_channels() -> None:
    parser = cli._build_parser()
    bots = parser.parse_args(["bots", "list"])
    assert bots.command == "bots" and bots.bots_command == "list"
    update = parser.parse_args(["update", "--channel", "dev", "--check"])
    assert update.command == "update" and update.channel == "dev" and update.check is True

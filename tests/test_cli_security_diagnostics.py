from __future__ import annotations

from app import cli


def test_main_denies_non_root_before_parsing(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli.os, "geteuid", lambda: 1000)
    assert cli.main(["--help"]) == 13
    output = capsys.readouterr().err
    assert "root only" in output
    assert "sudo ziko" in output


def test_main_allows_root_to_parse_help(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)
    try:
        cli.main(["--help"])
    except SystemExit as exc:
        assert exc.code == 0
    assert "doctor" in capsys.readouterr().out


def test_telegram_api_rejects_bad_token_without_network() -> None:
    ok, detail = cli._telegram_api("not-a-token", "getMe")
    assert not ok
    assert "invalid" in str(detail).lower()


def test_dashboard_resource_helpers_return_safe_values(monkeypatch) -> None:
    monkeypatch.setattr(cli, "_system_cpu_percent", lambda: 12.5)
    rows = dict(cli._system_resources())
    assert rows["CPU usage"] == "12.5%"
    assert "RAM" in rows
    assert "Disk" in rows
    assert "Load average" in rows

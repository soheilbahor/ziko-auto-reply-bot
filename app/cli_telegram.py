"""CLI rendering for Telegram diagnostics. Exit codes: 0 ok, 1 warnings, 2 failures, 3 not found."""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app import dbdiag, key_location, telegram_stats, tgdiag

_COLOURS = {"ok": "green", "warn": "yellow", "fail": "red", "info": "cyan"}
_ICONS = {"ok": "✔", "warn": "!", "fail": "✘", "info": "·"}


def _paths(db_path: Path) -> tuple[Path, Path]:
    data = db_path.parent
    return data / "run" / telegram_stats.STATUS_FILE, data / "logs"


def gather(db_path: Path, token: str, api: Callable[..., Any], *, network: bool = True) -> dict[str, Any]:
    status_path, log_dir = _paths(db_path)
    sections: dict[str, list[tgdiag.Check]] = {}
    metrics: dict[str, Any] = {}
    if network:
        sections["network"], metrics = tgdiag.network_checks()
        if token:
            sections["master"] = tgdiag.master_checks(token, api)
        else:
            sections["master"] = [("fail", "Master bot auth", "BOT_TOKEN missing in configuration")]
    sections["telemetry"], _bots = tgdiag.telemetry_checks(status_path)
    sections["business"] = tgdiag.business_checks(dbdiag.discover(db_path)) or [
        ("info", "Business", "no databases with Business connections found")]
    sections["logs"] = tgdiag.log_checks(log_dir)
    every = [c for checks in sections.values() for c in checks]
    return {"status": tgdiag.overall(every), "metrics": metrics,
            "sections": {k: [{"level": lvl, "check": n, "detail": d} for lvl, n, d in v]
                         for k, v in sections.items()}}


def _render(report: dict[str, Any]) -> None:
    from rich.console import Console
    from rich.table import Table
    console = Console()
    for section, rows in report["sections"].items():
        t = Table(title=f"Telegram · {section}", show_header=False, expand=True, title_justify="left")
        t.add_column(width=2)
        t.add_column(ratio=1)
        t.add_column(ratio=3)
        for r in rows:
            c = _COLOURS[r["level"]]
            t.add_row(f"[{c}]{_ICONS[r['level']]}[/]", r["check"], r["detail"])
        console.print(t)
    label = ["healthy", "degraded", "failing"][report["status"]]
    console.print(f"[bold]overall: {label}[/]")


def cmd_monitor_telegram(*, db_path: Path, token: str, api: Callable[..., Any],
                         as_json: bool = False, watch: float | None = None) -> int:
    if as_json:
        report = gather(db_path, token, api)
        print(json.dumps(report, ensure_ascii=False))
        return report["status"]
    if not watch:
        report = gather(db_path, token, api)
        _render(report)
        return report["status"]
    try:
        while True:
            # Network probes every cycle would spam Telegram; only telemetry refreshes fast.
            report = gather(db_path, token, api, network=False)
            print("\033[2J\033[H", end="")
            _render(report)
            print(f"refresh {watch}s · network probes skipped in watch mode · Ctrl+C to exit")
            time.sleep(max(watch, 2.0))
    except KeyboardInterrupt:
        return 0


def _registry_row(registry: Path, bot_id: int) -> dict[str, Any] | None:
    if not registry.is_file():
        return None
    conn = sqlite3.connect(f"file:{registry}?mode=ro", uri=True, timeout=5)
    try:
        row = conn.execute("SELECT user_id, bot_id, username, enabled, created_at FROM tenants "
                           "WHERE bot_id=?", (bot_id,)).fetchone()
        primary = conn.execute("SELECT bot_id FROM tenants WHERE user_id=? ORDER BY created_at, "
                               "tenant_id LIMIT 1", (row[0],)).fetchone() if row else None
    finally:
        conn.close()
    if row is None:
        return None
    return {"user_id": row[0], "bot_id": row[1], "username": row[2], "enabled": bool(row[3]),
            "created_at": row[4], "is_primary": bool(primary and primary[0] == bot_id)}


def tenant_db_path(data_dir: Path, info: dict[str, Any]) -> Path:
    """Same rule as TenantBotManager._run_tenant_polling (legacy file for primary bot)."""
    legacy = data_dir / "tenants" / f"{info['user_id']}.sqlite3"
    if info["is_primary"] and legacy.is_file():
        return legacy
    return data_dir / "tenants" / f"bot-{info['bot_id']}.sqlite3"


def _decrypt_token(registry: Path, bot_id: int, key_path: Path | None = None) -> str | None:
    from cryptography.fernet import InvalidToken

    from app.key_rotation import KeyRotationError, load_multifernet
    conn = sqlite3.connect(f"file:{registry}?mode=ro", uri=True, timeout=5)
    try:
        row = conn.execute("SELECT encrypted_token FROM tenants WHERE bot_id=?", (bot_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    try:
        return load_multifernet(key_path or registry.with_name("tenant-token.key")).decrypt(
            row[0].encode("ascii")).decode("utf-8")
    except (OSError, InvalidToken, ValueError, KeyRotationError):
        return None


def inspect_bot(bot_id: int, db_path: Path, api: Callable[..., Any], probe: bool = False,
                values: dict[str, str] | None = None) -> dict[str, Any]:
    data_dir = db_path.parent
    registry = data_dir / "tenants.sqlite3"
    try:
        info = _registry_row(registry, bot_id)
    except sqlite3.Error:
        return {"status": 2, "error": "registry unreadable"}
    if info is None:
        return {"status": 3, "error": f"bot {bot_id} is not registered"}
    checks: list[tgdiag.Check] = [
        ("ok" if info["enabled"] else "warn", "registry",
         f"@{info['username']} owner={info['user_id']} {'enabled' if info['enabled'] else 'DISABLED'}"
         f" since {info['created_at']}")]
    tdb = tenant_db_path(data_dir, info)
    rep = dbdiag.inspect(tdb)
    checks.append(("ok" if rep.ok else ("fail" if rep.exists else "warn"), "database",
                   f"{tdb.name}: {rep.integrity or rep.error} · {sum(rep.tables.values())} rows"
                   + (f" · {rep.stuck_pending} stuck deliveries" if rep.stuck_pending else "")))
    checks += [c for c in tgdiag.business_checks({"tenant": tdb})] or [
        ("info", "Business", "no Business connection recorded yet")]
    status_path, log_dir = _paths(db_path)
    tel_checks, bots = tgdiag.telemetry_checks(status_path)
    mine = [c for c in tel_checks if c[1] == f"child {bot_id}"]
    if mine:
        checks += mine
    elif info["enabled"]:
        checks.append(("warn", "runtime", tel_checks[0][2] if tel_checks[0][0] != "ok"
                       else "not reporting — waiting for a polling slot or crashed (see logs)"))
    log_counts = tgdiag.scan_logs(log_dir).get(f"bot-{bot_id}.log")
    checks.append(("warn" if log_counts else "ok", "log errors",
                   ", ".join(f"{k}×{v}" for k, v in log_counts.most_common()) if log_counts else "none"))
    if probe:
        try:
            key_path, _ = key_location.resolve(data_dir, values)
        except key_location.KeyLocationError:
            key_path = None
        token = _decrypt_token(registry, bot_id, key_path)
        checks.append(tgdiag.probe_child(token, api) if token else
                      ("fail", "token", "could not decrypt (key missing or mismatched)"))
        token = None  # drop the reference promptly
    return {"status": tgdiag.overall(checks), "bot": info, "database_path": str(tdb),
            "checks": [{"level": lvl, "check": n, "detail": d} for lvl, n, d in checks]}


def cmd_bot_inspect(bot_id: int, *, db_path: Path, api: Callable[..., Any], probe: bool = False,
                    as_json: bool = False, values: dict[str, str] | None = None) -> int:
    report = inspect_bot(bot_id, db_path, api, probe, values)
    if as_json:
        print(json.dumps(report, ensure_ascii=False))
        return report["status"]
    if "error" in report:
        print(report["error"], file=sys.stderr)
        return report["status"]
    _render({"status": report["status"], "sections": {f"bot {bot_id}": report["checks"]}})
    return report["status"]

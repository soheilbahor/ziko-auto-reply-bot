"""`ziko monitor` and `ziko dashboard` commands (Rich rendering, read-only).

Exit codes: 0 = all OK, 1 = warnings, 2 = critical / database unhealthy, 64 = usage error.
`--json` prints machine-readable output for cron, Zabbix, Nagios or scripts.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from app import cli_bots, dbdiag, monitor

SECTIONS = ("cpu", "memory", "disk", "network", "processes", "database")


def _h(n: float | int | None) -> str:
    if n is None:
        return "n/a"
    n = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or unit == "TiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


def _bar(pct: float | None, width: int = 20) -> str:
    if pct is None:
        return "[dim]n/a[/]"
    fill = int(round(width * min(max(pct, 0), 100) / 100))
    color = "red" if pct >= 90 else "yellow" if pct >= 75 else "green"
    return f"[{color}]{'█' * fill}[/][dim]{'░' * (width - fill)}[/] {pct:5.1f}%"


def collect(section: str, data_dir: Path, db_path: Path) -> dict[str, Any]:
    if section == "cpu":
        return monitor.cpu().to_dict()
    if section == "memory":
        return monitor.memory().to_dict()
    if section == "disk":
        snap = monitor.disk([Path("/"), data_dir])
        snap.data["data_dir_bytes"] = monitor.dir_size(data_dir)
        snap.data["log_dir_bytes"] = monitor.dir_size(data_dir / "logs")
        snap.data["backup_dir_bytes"] = monitor.dir_size(
            Path(os.environ.get("ZIKO_BACKUP_DIR", "/var/backups/ziko-assistant")))
        return snap.to_dict()
    if section == "network":
        return monitor.network().to_dict()
    if section == "processes":
        return monitor.processes().to_dict()
    if section == "database":
        return {"section": "database", "taken_at": time.time(),
                "data": {k: v.to_dict() for k, v in dbdiag.inspect_all(db_path).items()},
                "unsupported": {"lock_contention": "SQLite exposes no lock statistics",
                                "connection_pool": "aiosqlite uses one connection per DB"}}
    raise ValueError(section)


def _render(console: Any, section: str, snap: dict[str, Any]) -> None:
    from rich.table import Table
    d, u = snap["data"], snap.get("unsupported", {})
    t = Table(title=f"ZIKO · {section}", expand=True, show_header=section in ("processes", "database"))
    if section == "cpu":
        t.add_column("metric")
        t.add_column("value")
        t.add_row("overall", _bar(d.get("percent")))
        for i, p in enumerate(d.get("per_core_percent") or []):
            t.add_row(f"core {i}", _bar(p))
        t.add_row("load avg 1/5/15", str(d.get("load_average") or "n/a"))
        t.add_row("cores (logical/physical)", f"{d.get('logical_cores')}/{d.get('physical_cores')}")
        t.add_row("frequency", f"{d['frequency_mhz']} MHz" if d.get("frequency_mhz") else "n/a")
        t.add_row("temperature", f"{d['temperature_c']} °C" if d.get("temperature_c") else "n/a")
        for p in d.get("top_processes") or []:
            t.add_row(f"  {p['name']} ({p['pid']})", f"{p['cpu_percent']:.1f}%")
    elif section == "memory":
        t.add_column("metric")
        t.add_column("value")
        t.add_row("RAM", _bar(d.get("percent")))
        t.add_row("used / total", f"{_h(d.get('used'))} / {_h(d.get('total'))}")
        t.add_row("available", _h(d.get("available")))
        t.add_row("cached", _h(d.get("cached")))
        t.add_row("swap", _bar(d.get("swap_percent")) if d.get("swap_total") else "no swap")
        pr = d.get("pressure")
        t.add_row("pressure avg10/60/300", f"{pr['avg10']}/{pr['avg60']}/{pr['avg300']} %" if pr else "n/a")
        for p in d.get("top_processes") or []:
            t.add_row(f"  {p['name']} ({p['pid']})", _h(p["memory_info"]))
    elif section == "disk":
        t.add_column("metric")
        t.add_column("value")
        for fs in d.get("filesystems", []):
            t.add_row(fs["path"], f"{_bar(fs['percent'])}  {_h(fs['free'])} free")
        t.add_row("read / write", f"{_h(d.get('read_bytes_per_s'))}/s / {_h(d.get('write_bytes_per_s'))}/s")
        t.add_row("IOPS", str(d.get("iops", "n/a")))
        t.add_row("avg latency", f"{d['avg_latency_ms']} ms" if "avg_latency_ms" in d else "n/a")
        for k in ("data_dir_bytes", "log_dir_bytes", "backup_dir_bytes"):
            t.add_row(k.replace("_bytes", "").replace("_", " "), _h(d.get(k)))
    elif section == "network":
        t.add_column("metric")
        t.add_column("value")
        for i in d.get("interfaces", []):
            t.add_row(i["name"], f"↓ {_h(i['rx_bytes_per_s'])}/s  ↑ {_h(i['tx_bytes_per_s'])}/s  "
                      f"err {i['errors_in']}/{i['errors_out']}  drop {i['drops_in']}/{i['drops_out']}")
        t.add_row("TCP states", str(d.get("tcp_states") or "n/a"))
        dns = d.get("dns", {})
        t.add_row(f"DNS {dns.get('host')}", f"[green]ok[/] {dns.get('ms')} ms" if dns.get("ok")
                  else f"[red]fail[/] {dns.get('error')}")
    elif section == "processes":
        t.add_column("pid")
        t.add_column("name")
        t.add_column("user")
        t.add_column("value")
        for p in d.get("ziko_processes", []):
            t.add_row(str(p["pid"]), "[bold]ziko[/]", str(p["user"]),
                      f"{_h(p['rss'])} · {p['threads']} thr · {p['open_files']} files · up {p['uptime_s']}s")
        for p in d.get("top_cpu", []):
            t.add_row(str(p["pid"]), p["name"], str(p["user"]), f"{p['cpu_percent']:.1f}% CPU")
    elif section == "database":
        for col in ("db", "ok", "size", "wal", "schema", "integrity", "rows", "stuck"):
            t.add_column(col)
        for label, r in d.items():
            if not r["exists"]:
                t.add_row(label, "[dim]—[/]", "missing", "", "", "", "", "")
                continue
            t.add_row(label, "[green]✔[/]" if r["ok"] else "[red]✘[/]", _h(r["size_bytes"]),
                      _h(r["wal_bytes"]), str(r["user_version"]), str(r["integrity"]),
                      str(sum(r["tables"].values())), str(r["stuck_pending"] or 0))
    console.print(t)
    for k, why in u.items():
        console.print(f"[dim]· {k}: not measured — {why}[/]")


def _exit_code(section: str, snap: dict[str, Any]) -> int:
    if section == "database":
        reports = [r for r in snap["data"].values() if r["exists"]]
        return 2 if any(not r["ok"] for r in reports) else 0
    return 0


def cmd_monitor(section: str, *, data_dir: Path, db_path: Path, as_json: bool = False,
                watch: float | None = None) -> int:
    if section not in SECTIONS:
        print(f"unknown section {section!r}; choose: {', '.join(SECTIONS)}", file=sys.stderr)
        return 64
    if as_json:
        snap = collect(section, data_dir, db_path)
        print(json.dumps(snap, default=str, ensure_ascii=False))
        return _exit_code(section, snap)
    from rich.console import Console
    console = Console()
    if not watch:
        snap = collect(section, data_dir, db_path)
        _render(console, section, snap)
        return _exit_code(section, snap)
    try:
        while True:
            snap = collect(section, data_dir, db_path)
            console.clear()
            _render(console, section, snap)
            console.print(f"[dim]refresh {watch}s · Ctrl+C to exit[/]")
            time.sleep(max(watch, 0.5))
    except KeyboardInterrupt:
        return 0


def health(data_dir: Path, db_path: Path) -> tuple[int, list[tuple[str, str, str]]]:
    c, m = monitor.cpu(sample_seconds=0.2, top=0), monitor.memory(top=0)
    dsk = monitor.disk([Path("/"), data_dir], sample_seconds=0.1)
    alerts = monitor.evaluate(c, m, dsk)
    main = dbdiag.inspect(db_path)
    if main.exists:
        alerts.append(("OK" if main.ok else "CRIT", "database.main", main.integrity or main.error or "?"))
        if main.stuck_pending:
            alerts.append(("WARN", "database.stuck_pending", f"{main.stuck_pending} deliveries"))
    else:
        alerts.append(("CRIT", "database.main", "not found"))
    # Child bots, from the service's snapshot (instant; no tenant DB scan here).
    alerts += cli_bots.health_checks(db_path)
    levels = {a[0] for a in alerts}
    return (2 if "CRIT" in levels else 1 if "WARN" in levels else 0), alerts


def cmd_dashboard(*, data_dir: Path, db_path: Path, version: str, service_state: Any,
                  refresh: float = 2.0, once: bool = False) -> int:
    from rich.console import Group
    from rich.live import Live
    from rich.panel import Panel
    from rich.table import Table

    def frame() -> Panel:
        rc, alerts = health(data_dir, db_path)
        m = monitor.memory(top=0).data
        c = monitor.cpu(sample_seconds=0.1, top=0).data
        up = monitor.uptime()
        head = Table.grid(expand=True)
        head.add_column()
        head.add_column(justify="right")
        state = service_state()
        colour = "green" if state == "active" else "red"
        head.add_row(f"[bold]ZIKO[/] v{version}", f"service [{colour}]{state}[/]  ·  "
                     f"host up {int(up // 3600) if up else '?'}h")
        res = Table.grid(padding=(0, 2))
        res.add_row("CPU", _bar(c.get("percent")), "load", str(c.get("load_average")))
        res.add_row("RAM", _bar(m.get("percent")), "swap",
                    _bar(m.get("swap_percent")) if m.get("swap_total") else "none")
        bots = Table.grid(padding=(0, 2))
        for label, value in cli_bots.dashboard_rows(db_path):
            bots.add_row(f"[bold]{label}[/]" if label else "", value)
        al = Table(show_header=False, box=None)
        for level, metric, msg in alerts:
            colour = {"OK": "green", "WARN": "yellow", "CRIT": "red"}[level]
            al.add_row(f"[{colour}]{level}[/]", metric, msg)
        status = ["healthy", "degraded", "critical"][rc]
        return Panel(Group(head, res, bots, al), title=f"health: {status}",
                     border_style=["green", "yellow", "red"][rc])

    if once or not sys.stdout.isatty():
        from rich.console import Console
        Console().print(frame())
        return health(data_dir, db_path)[0]
    try:
        with Live(frame(), refresh_per_second=4, screen=True) as live:
            while True:
                time.sleep(refresh)
                live.update(frame())
    except KeyboardInterrupt:
        return 0

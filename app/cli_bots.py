"""CLI view of the child-bot snapshot written by the running service.

The CLI never scans tenant databases itself for this view: the service already does it
(app/overview_cache.py) and exports ``<data>/run/overview.json``. Reading that file is
instant, and the CLI shows exactly what admins see in /platform.

Staleness is explicit: the snapshot is written every ZIKO_OVERVIEW_REFRESH_SECONDS
(default 30). Older than ``STALE_AFTER`` means the service is stopped, hung, or an old
version -- reported as such, never presented as current.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

from app import platform_overview as po

STALE_AFTER = 180.0
ATTENTION = ("crashed", "token", "blocked", "warning")
_COLOUR = {"crashed": "red", "token": "red", "blocked": "yellow", "warning": "yellow",
           "queued": "cyan", "ready": "green", "disabled": "dim"}
# English labels for the terminal (the Persian ones are for Telegram).
LABEL = {"crashed": "polling down", "token": "token rejected", "blocked": "replies blocked",
         "warning": "warning", "queued": "queued", "ready": "ready", "disabled": "disabled"}


def _reason(bot: dict[str, Any]) -> str:
    """English reason. Snapshots from older versions carried Persian text: never show it."""
    codes = bot.get("reason_codes") or []
    return ", ".join(code.replace("_", " ") for code in codes[:2])


def snapshot_path(db_path: Path) -> Path:
    return db_path.parent / "run" / po.SNAPSHOT_FILE


def load(db_path: Path, now: float | None = None) -> tuple[dict[str, Any] | None, float | None, str | None]:
    """Return (snapshot, age_seconds, problem). problem is None when fresh."""
    path = snapshot_path(db_path)
    if not path.exists():
        return None, None, "no snapshot yet: the service is stopped or older than this CLI"
    snap = po.read_snapshot(path)
    if snap is None:
        return None, None, "snapshot unreadable (permission, partial or older format)"
    age = (now or time.time()) - float(snap["taken_at"])
    if age > STALE_AFTER:
        return snap, age, f"STALE: taken {int(age)} s ago; the service may be stopped or hung"
    return snap, age, None


def health_checks(db_path: Path, now: float | None = None) -> list[tuple[str, str, str]]:
    """(OK|WARN|CRIT, metric, detail) lines for `ziko health`."""
    snap, age, problem = load(db_path, now)
    if snap is None:
        registry = db_path.parent / "tenants.sqlite3"
        # No child bots at all is not a problem; a missing snapshot with bots is.
        return [("WARN" if registry.exists() else "OK", "bots.snapshot",
                 problem if registry.exists() else "no child bots registered")]
    if problem:
        return [("WARN", "bots.snapshot", problem)]
    c = snap["counts"]
    out = [("OK", "bots.snapshot", f"{len(snap['bots'])} bots, {int(age)} s old")]
    down = c.get("crashed", 0) + c.get("token", 0)
    if down:
        names = ", ".join("@" + b["username"] for b in snap["bots"] if b["state"] in ("crashed", "token"))[:200]
        out.append(("CRIT", "bots.down", f"{down}: {names}"))
    owner_side = c.get("blocked", 0) + c.get("warning", 0)
    if owner_side:
        out.append(("WARN", "bots.attention", f"{owner_side} bot(s) blocked or warning (owner settings)"))
    if snap["waiting"] and snap["running"] >= snap["limit"]:
        out.append(("WARN", "bots.capacity",
                    f"polling full {snap['running']}/{snap['limit']}, {snap['waiting']} queued"))
    return out


def cmd_bots_status(*, db_path: Path, as_json: bool = False, attention: bool = False,
                    now: float | None = None, err: Any = None) -> int:
    """Exit: 0 all fine, 1 attention/stale, 2 a bot is down, 3 no snapshot."""
    snap, age, problem = load(db_path, now)
    if as_json:
        print(json.dumps({"snapshot": snap, "age_seconds": age, "problem": problem}, ensure_ascii=False))
    if snap is None:
        if not as_json:
            (err or sys.stderr).write(f"{problem}\n")
        return 3
    bots = [b for b in snap["bots"] if not attention or b["state"] in ATTENTION]
    c = snap["counts"]
    rc = 2 if c.get("crashed") or c.get("token") else 1 if (problem or c.get("blocked") or c.get("warning")) else 0
    if as_json:
        return rc
    from rich.console import Console
    from rich.table import Table
    console = Console()
    summary = "  ".join(f"[{_COLOUR[s]}]{LABEL[s]} {c[s]}[/]" for s in po.STATES if c.get(s))
    head = (f"[bold]{len(snap['bots'])} child bots[/]  polling {snap['running']}/{snap['limit']}"
            + (f"  queued {snap['waiting']}" if snap["waiting"] else ""))
    console.print(head)
    console.print(summary or "[dim]no bots[/]")
    t = Table(expand=True, show_lines=False)
    for col in ("state", "bot", "bot id", "owner", "polling", "reason"):
        t.add_column(col, overflow="fold")
    for b in bots:
        colour = _COLOUR.get(b["state"], "white")
        t.add_row(f"[{colour}]{LABEL.get(b['state'], b['state'])}[/]", "@" + b["username"],
                  str(b["bot_id"]), str(b["user_id"]), b["runtime"],
                  _reason(b) if b["state"] != "ready" else "")
    if bots:
        console.print(t)
    elif attention:
        console.print("[green]no bot needs attention[/]")
    stamp = time.strftime("%H:%M:%S", time.localtime(snap["taken_at"]))
    console.print(f"[dim]snapshot {stamp}, {int(age or 0)} s old[/]" if not problem else f"[yellow]{problem}[/]")
    console.print("[dim]details: ziko bots inspect BOT_ID[/]")
    return rc


def dashboard_rows(db_path: Path, now: float | None = None) -> list[tuple[str, str]]:
    """Compact (label, value) rows for the live dashboard panel."""
    snap, age, problem = load(db_path, now)
    if snap is None:
        return [("bots", f"[dim]{problem}[/]")]
    c = snap["counts"]
    parts = [f"[{_COLOUR[s]}]{LABEL[s]} {c[s]}[/]" for s in po.STATES if c.get(s)]
    rows = [("bots", f"{len(snap['bots'])} · " + ("  ".join(parts) or "none")),
            ("polling", f"{snap['running']}/{snap['limit']}" + (f" · queued {snap['waiting']}" if snap["waiting"] else ""))]
    worst = [b for b in snap["bots"] if b["state"] in ("crashed", "token")][:3]
    for b in worst:
        rows.append(("", f"[red]@{b['username']}[/] {_reason(b)[:60]}"))
    if problem:
        rows.append(("", f"[yellow]{problem}[/]"))
    return rows

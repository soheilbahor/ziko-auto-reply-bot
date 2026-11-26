"""CLI front-end for `ziko update <action>`. English output only.

  ziko update                 interactive: check, show notes, confirm, install (stable)
  ziko update check           installed vs latest stable, release notes, verifiability
  ziko update stable          install the latest stable release (verified)
  ziko update beta            install main-branch code (UNVERIFIED; explicit consent)
  ziko update history         past updates and rollbacks from the update journal
  ziko update rollback        restore the previous code snapshot (data untouched)
  ziko update verify          check the installed files against the install manifest
  ziko update logs            stages of the last (or a given) update run

Exit codes: 0 ok / up to date, 1 failure or refused, 2 integrity problem found,
10 an update is available (`check` only, for cron and monitoring).
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app import updates

EXIT_UPDATE_AVAILABLE = 10
_ICON = {"success": "[green]success[/]", "rolled_back": "[yellow]failed, rolled back[/]",
         "failed": "[red]failed[/]", "rollback": "[cyan]manual rollback[/]",
         "running": "[cyan]running[/]", "interrupted": "[red]interrupted[/]"}


def _console() -> Any:
    from rich.console import Console
    return Console(highlight=False)


def cmd_check(*, installed: str, repo: str, as_json: bool = False,
              fetch: Any = None, show_notes: bool = True, err: Any = None) -> int:
    try:
        release = updates.latest_release(repo, fetch=fetch)
    except updates.UpdateError as exc:
        if as_json:
            print(json.dumps({"error": str(exc), "installed": installed}))
        else:
            print(f"Update check failed: {exc}", file=err or sys.stderr)
        return 1
    result = updates.check(installed, release)
    if as_json:
        print(json.dumps({"installed": installed, "latest": release.tag,
                          "update_available": result.update_available, "verifiable": result.verifiable,
                          "problems": list(result.problems), "published_at": release.published_at,
                          "url": release.url}))
        return EXIT_UPDATE_AVAILABLE if result.update_available and result.verifiable else 0
    c = _console()
    c.print(f"Repository : {repo}")
    c.print(f"Installed  : [bold]{installed}[/]")
    c.print(f"Latest     : [bold]{release.tag.removeprefix('v')}[/]  ({release.published_at or 'date unknown'})")
    if not result.update_available:
        c.print("[green]ZIKO is up to date.[/]")
        return 0
    c.print("[yellow]An update is available.[/]")
    for problem in result.problems:
        c.print(f"[red]! {problem}[/]")
    if show_notes and release.notes:
        from rich.panel import Panel
        c.print(Panel(release.notes, title=f"Release notes {release.tag}", expand=False))
    c.print("Install with: [bold]sudo ziko update stable[/]")
    return EXIT_UPDATE_AVAILABLE if result.verifiable else 1


def cmd_interactive(*, installed: str, repo: str, run_update: Callable[..., int],
                    fetch: Any = None, yes: bool = False) -> int:
    """`ziko update` with no action: check first, then install only with consent."""
    rc = cmd_check(installed=installed, repo=repo, fetch=fetch)
    if rc != EXIT_UPDATE_AVAILABLE:
        return 0 if rc == 0 else rc
    if not yes:
        if not sys.stdin.isatty():
            print("Not a terminal: nothing installed. Use 'ziko update stable -y' in scripts.")
            return 1
        if input("Install now? A verified backup is made first. [y/N] ").strip().lower() not in {"y", "yes"}:
            print("Cancelled. Nothing was changed.")
            return 1
    return run_update(channel="stable")


def cmd_history(*, data_dir: Path, limit: int = 20, as_json: bool = False) -> int:
    history = updates.runs(updates.read_journal(updates.journal_path(data_dir)))[:limit]
    if as_json:
        print(json.dumps([asdict(r) for r in history], default=str))
        return 0
    c = _console()
    if not history:
        c.print("No update history yet. (The journal is written by updates made with v1.5.0 or later.)")
        return 0
    from rich.table import Table
    t = Table(title="Update history (newest first)")
    for col in ("started (UTC)", "from", "to", "channel", "result", "failed stage"):
        t.add_column(col)
    for r in history:
        t.add_row(r.started.replace("T", " ").rstrip("Z"), r.from_version, r.to_version, r.channel,
                  _ICON.get(r.outcome, r.outcome), r.failed_stage or "")
    c.print(t)
    c.print("[dim]Details of one run: ziko update logs [RUN_ID]   (newest: ziko update logs)[/]")
    return 0


def cmd_logs(*, data_dir: Path, run_id: str | None = None, as_json: bool = False) -> int:
    events = updates.read_journal(updates.journal_path(data_dir))
    if not events:
        print("No update journal yet.")
        return 0
    run_id = run_id or events[-1]["run"]
    selected = [e for e in events if e["run"] == run_id]
    if not selected:
        print(f"Unknown run id: {run_id}", file=sys.stderr)
        return 1
    if as_json:
        print(json.dumps(selected))
        return 0
    c = _console()
    c.print(f"[bold]Update run {run_id}[/]")
    for e in selected:
        extra = "  ".join(f"{k}={updates.clean_text(str(v), 200)}" for k, v in e.items()
                          if k not in {"ts", "run", "stage"})
        colour = "red" if e["stage"] in {"FAILED", "ROLLBACK"} else "green" if e["stage"].endswith("COMPLETE") else "white"
        c.print(f"{e.get('ts', '')}  [{colour}]{e['stage']:<22}[/] {extra}")
    return 0


def cmd_verify(*, root: Path, record: bool = False, as_json: bool = False, err: Any = None) -> int:
    err = err or sys.stderr
    if record:
        report = updates.verify(root)
        hard = [p for p in report.problems if not p.startswith(("unexpected file", "modified", "missing:"))]
        if hard:
            print("Refusing to record a manifest for a broken installation:", file=err)
            for p in hard:
                print(f"  - {p}", file=err)
            return 2
        path = updates.write_manifest(root, report.version)
        print(f"Install manifest recorded: {path} ({report.version}). "
              "Only do this right after a verified install or update.")
        return 0
    report = updates.verify(root)
    if as_json:
        print(json.dumps(asdict(report)))
        return 0 if report.ok else 2
    c = _console()
    c.print(f"Installed version: [bold]{report.version}[/]   files checked: {report.checked}")
    for w in report.warnings:
        c.print(f"[yellow]! {w}[/]")
    if report.ok:
        c.print("[green]Installation verified: no missing, modified or unexpected files.[/]")
        return 0
    for p in report.problems[:50]:
        c.print(f"[red]x {p}[/]")
    if len(report.problems) > 50:
        c.print(f"[red]... and {len(report.problems) - 50} more[/]")
    c.print("Repair: [bold]sudo ziko update stable[/] reinstalls the verified release "
            "(or [bold]ziko update rollback[/]).")
    return 2


def cmd_rollback(*, root: Path, backup_dir: Path, installed: str, target: str | None = None,
                 with_data: bool = False, yes: bool = False, runner: Callable[..., Any] | None = None,
                 service: str = "ziko.service", err: Any = None) -> int:
    """Restore a previous code snapshot by delegating to update.sh's own rollback path."""
    err = err or sys.stderr
    runner = runner or subprocess.run
    try:
        snap = updates.rollback_plan(backup_dir, installed, target)
    except updates.UpdateError as exc:
        print(f"Rollback not possible: {exc}", file=err)
        return 1
    problems = updates.check_snapshot(snap.path)
    if problems:
        print(f"Refusing: snapshot {snap.path.name} failed validation:", file=err)
        for p in problems[:10]:
            print(f"  - {p}", file=err)
        return 2
    c = _console()
    c.print(f"Roll back from [bold]{installed}[/] to [bold]{snap.version.removeprefix('v')}[/]")
    c.print(f"  code snapshot : {snap.path} ({snap.size // 1024} KiB, taken {snap.stamp})")
    if with_data:
        c.print("  [red]databases     : RESTORED from the matching platform backup "
                "(changes made since then are LOST)[/]")
    else:
        c.print("  databases     : kept as they are (use --with-data to restore them too)")
    c.print("  The service is stopped, the code and dependencies restored, then restarted and health-checked.")
    if not yes:
        if not sys.stdin.isatty() or input("Type ROLLBACK to continue: ").strip() != "ROLLBACK":
            print("Cancelled. Nothing was changed.")
            return 1
    script = root / "scripts" / "update.sh"
    if not script.is_file():
        print(f"Missing {script}; cannot roll back safely.", file=err)
        return 1
    cmd = ["bash", str(script), "--rollback-to", str(snap.path)]
    if with_data:
        cmd.append("--with-data")
    started = time.monotonic()
    result = runner(cmd, check=False)
    code = getattr(result, "returncode", 1)
    if code == 0:
        c.print(f"[green]Rolled back to {snap.version.removeprefix('v')} in {time.monotonic() - started:.0f}s.[/]")
    else:
        c.print(f"[red]Rollback failed (exit {code}). See: ziko update logs ; journalctl -u {service}[/]")
    return 0 if code == 0 else 1


def default_paths(db_path: Path) -> dict[str, Path]:
    return {"data_dir": db_path.parent,
            "backup_dir": Path(os.environ.get("ZIKO_BACKUP_DIR", "/var/backups/ziko-assistant"))}

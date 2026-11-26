"""CLI front-end for `ziko security audit` and `ziko security rotate-key`."""
from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from pathlib import Path

from app import key_location, key_rotation, security_audit

UNIT_PATH = Path("/etc/systemd/system/ziko.service")
_COLOUR = {"ok": "green", "warn": "yellow", "fail": "red", "info": "cyan"}


def cmd_audit(*, env_file: Path, db_path: Path, values: dict[str, str], as_json: bool = False) -> int:
    try:
        unit_text = UNIT_PATH.read_text(encoding="utf-8")
    except OSError:
        unit_text = None
    backup_dir = Path(os.environ.get("ZIKO_BACKUP_DIR", "/var/backups/ziko-assistant"))
    checks = security_audit.audit(env_file=env_file, data_dir=db_path.parent, backup_dir=backup_dir,
                                  unit_text=unit_text, env=values)
    rc = security_audit.exit_code(checks)
    if as_json:
        print(json.dumps({"status": rc, "checks": [dict(zip(("level", "area", "detail", "fix"), c))
                                                    for c in checks]}, ensure_ascii=False))
        return rc
    from rich.console import Console
    from rich.table import Table
    t = Table(title="ZIKO security audit (read-only)", expand=True)
    for col in ("", "area", "finding", "fix"):
        t.add_column(col)
    for level, area, detail, fix in checks:
        t.add_row(f"[{_COLOUR[level]}]{level.upper()}[/]", area, detail, f"[dim]{fix}[/]")
    console = Console()
    console.print(t)
    console.print(["[green]no issues[/]", "[yellow]warnings found[/]", "[red]failures found[/]"][rc])
    return rc


def cmd_rotate_key(*, db_path: Path, service_state: Callable[[], str], yes: bool = False,
                   values: dict[str, str] | None = None) -> int:
    data = db_path.parent
    registry = data / "tenants.sqlite3"
    key, _external = key_location.resolve(data, values)
    if not registry.is_file():
        print("No child-bot registry exists; nothing to rotate.")
        return 0
    state = service_state()
    if state in {"active", "activating", "reloading"}:
        print("Refusing: the service is running. Stop it first so no token is written during rotation:\n"
              "  sudo systemctl stop ziko && sudo ziko security rotate-key && sudo systemctl start ziko",
              file=sys.stderr)
        return 3
    try:
        n = key_rotation.verify(registry, key)
    except (key_rotation.KeyRotationError, OSError) as exc:
        print(f"Refusing: {exc}. Nothing was changed.", file=sys.stderr)
        return 2
    print(f"{n} child-bot token(s) will be re-encrypted with a new key.\n"
          "A backup of the registry and the current key is made first.\n"
          "After this, platform backups taken BEFORE the rotation still contain the old key "
          "(they remain restorable as a unit).")
    if not yes:
        if not sys.stdin.isatty() or input("Type ROTATE to continue: ").strip() != "ROTATE":
            print("Cancelled. Nothing was changed.")
            return 1
    # Pre-rotation copies hold the plaintext key: keep them beside the key (same
    # owner and protection), never in the backup directory that is copied off-host.
    backup_root = key.parent / "key-backups"
    try:
        res = key_rotation.rotate(registry, key, backup_root)
    except (key_rotation.KeyRotationError, OSError) as exc:
        print(f"Rotation failed: {exc}\nThe key file still decrypts all tokens "
              "(old and new keys are both accepted); re-run the command to finish.", file=sys.stderr)
        return 2
    try:
        os.chmod(backup_root, 0o700)
    except OSError:
        pass
    print(f"Done: {res.rows} token(s) re-encrypted and verified in {res.seconds}s.\n"
          f"Pre-rotation backup: {res.backup_dir}\nStart the service: sudo systemctl start ziko")
    return 0


def cmd_move_key(*, db_path: Path, env_file: Path, values: dict[str, str],
                 service_state: Callable[[], str], to_data: bool = False, yes: bool = False,
                 service_user: str = "ziko") -> int:
    """Move the token key out of the data directory (default) or back into it."""
    import grp
    import pwd

    data = db_path.parent
    registry = data / "tenants.sqlite3"
    current, external = key_location.resolve(data, values)
    if service_state() in {"active", "activating", "reloading"}:
        print("Refusing: stop the service first (it reads the key path only at start):\n"
              "  sudo systemctl stop ziko && sudo ziko security move-key && sudo systemctl start ziko",
              file=sys.stderr)
        return 3
    try:
        gid = grp.getgrnam(service_user).gr_gid
        uid = pwd.getpwnam(service_user).pw_uid
    except KeyError:
        print(f"Service user/group '{service_user}' not found.", file=sys.stderr)
        return 2
    dest = env_file.parent / key_location.KEY_NAME
    if to_data:
        plan = f"Move the key from {current} back to {key_location.legacy_path(data)} (legacy layout)."
    else:
        plan = (f"Move the key from {key_location.legacy_path(data)} to {dest} (root:{service_user} 0640).\n"
                "Afterwards platform backups no longer contain the key: keep an OFFLINE copy of\n"
                f"{dest} -- without it, restored child-bot tokens cannot be decrypted.")
    print(plan)
    if not yes and (not sys.stdin.isatty() or input("Type MOVE to continue: ").strip() != "MOVE"):
        print("Cancelled. Nothing was changed.")
        return 1
    try:
        if to_data:
            res = key_location.move_to_data(registry=registry, data_dir=data, current=current,
                                            env_file=env_file, uid=uid, gid=gid,
                                            verify=key_rotation.verify)
        else:
            if external and current != dest:
                print(f"The key is already external at {current}.", file=sys.stderr)
                return 0
            res = key_location.move_to_config(registry=registry, data_dir=data, dest=dest,
                                              env_file=env_file, gid=gid, verify=key_rotation.verify)
    except (key_location.KeyLocationError, key_rotation.KeyRotationError, OSError) as exc:
        print(f"Move stopped: {exc}\nRe-run the same command to finish; the service can still "
              "read the key at its current configured location.", file=sys.stderr)
        return 2
    note = " (finished an earlier interrupted move)" if res.already_done else ""
    print(f"Done{note}: key at {res.key_path}; {res.tokens} token(s) verified.\n"
          "Start the service: sudo systemctl start ziko")
    return 0

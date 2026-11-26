"""`ziko security audit`: read-only checks of permissions, hardening and secrets hygiene.

Each check returns (level, area, detail, fix). level: ok / warn / fail / info.
Exit code: 0 all ok, 1 warnings, 2 failures. Nothing is changed; no secret is printed.
"""
from __future__ import annotations

import os
import re
import stat
import time
from collections.abc import Iterable
from pathlib import Path

Check = tuple[str, str, str, str]

# Directives the shipped unit relies on. Missing ones are reported individually.
REQUIRED_UNIT = {
    "NoNewPrivileges": "true", "PrivateTmp": "true", "ProtectSystem": "strict",
    "ProtectHome": "true", "CapabilityBoundingSet": "", "UMask": "0077",
    "RestrictSUIDSGID": "true", "MemoryDenyWriteExecute": "true",
}
_TOKEN_RE = re.compile(rb"\d{5,}:[A-Za-z0-9_-]{30,}")


def _mode(p: Path) -> int:
    return stat.S_IMODE(p.lstat().st_mode)


def _owner(p: Path) -> str:
    try:
        import pwd
        return pwd.getpwuid(p.lstat().st_uid).pw_name
    except (ImportError, KeyError):
        return str(p.lstat().st_uid)


def check_path(p: Path, label: str, *, max_mode: int, kind: str = "file",
               owner: str | None = None, required: bool = True) -> Check:
    if p.is_symlink():
        return ("fail", label, f"{p} is a symlink", "replace it with a real file/directory")
    if not p.exists():
        return ("fail" if required else "info", label, f"{p} not found",
                "run the installer" if required else "")
    if kind == "dir" and not p.is_dir() or kind == "file" and not p.is_file():
        return ("fail", label, f"{p} is not a {kind}", "")
    mode = _mode(p)
    problems = []
    if mode & ~max_mode:
        problems.append(f"mode {mode:o} (max {max_mode:o})")
    if mode & 0o002:
        problems.append("WORLD-WRITABLE")
    who = _owner(p)
    if owner and who != owner:
        problems.append(f"owner {who} (expected {owner})")
    if problems:
        lvl = "fail" if (mode & 0o007 or "WORLD-WRITABLE" in problems) else "warn"
        return (lvl, label, f"{p}: " + ", ".join(problems),
                f"chmod {max_mode:o} {p}" + (f" && chown {owner} {p}" if owner else ""))
    return ("ok", label, f"{p}: {mode:o} {who}", "")


def parse_unit(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith(("#", ";", "[")) and "=" in line:
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    return out


def check_unit(unit_text: str | None) -> list[Check]:
    if unit_text is None:
        return [("warn", "systemd unit", "installed unit not found", "reinstall deploy/ziko.service")]
    unit = parse_unit(unit_text)
    out: list[Check] = []
    user = unit.get("User", "")
    if not user or user == "root":
        out.append(("fail", "service user", f"service runs as {user or 'root (no User=)'}",
                    "set User=ziko and Group=ziko"))
    else:
        out.append(("ok", "service user", f"runs as {user}", ""))
    missing = [k for k, v in REQUIRED_UNIT.items() if unit.get(k) != v]
    if missing:
        out.append(("warn", "systemd hardening", "missing/changed: " + ", ".join(missing),
                    "compare with deploy/ziko.service; also run: systemd-analyze security ziko"))
    else:
        out.append(("ok", "systemd hardening", f"{len(REQUIRED_UNIT)} key directives present", ""))
    return out


def check_key(key_path: Path) -> Check:
    if not key_path.exists():
        return ("info", "token key", "no child-bot key yet (created with the first child bot)", "")
    try:
        lines = [line for line in key_path.read_bytes().splitlines() if line.strip()]
    except PermissionError:
        return ("warn", "token key", "cannot read (run with sudo)", "")
    if len(lines) > 1:
        return ("warn", "token key", f"{len(lines)} keys present: an interrupted rotation",
                "run: sudo ziko security rotate-key (it completes safely)")
    age_days = (time.time() - key_path.stat().st_mtime) / 86400
    if age_days > 365:
        return ("warn", "token key", f"not rotated for {age_days:.0f} days",
                "sudo systemctl stop ziko && sudo ziko security rotate-key")
    return ("ok", "token key", f"1 key, age {age_days:.0f} days", "")


def scan_plaintext_tokens(paths: Iterable[Path], max_bytes: int = 4 << 20) -> list[Check]:
    """Look for raw bot tokens where none should exist (logs, backups dir, data dir)."""
    hits: list[str] = []
    for root in paths:
        if not root.is_dir():
            continue
        for p in root.rglob("*"):
            if not p.is_file() or p.is_symlink() or p.suffix in {".sqlite3", ".age", ".key"}:
                continue
            try:
                with p.open("rb") as fh:
                    if _TOKEN_RE.search(fh.read(max_bytes)):
                        hits.append(str(p))
            except OSError:
                continue
    if hits:
        return [("fail", "plaintext tokens", f"token-like strings in {len(hits)} file(s): "
                 + ", ".join(hits[:5]), "rotate the affected tokens in @BotFather, then delete the files")]
    return [("ok", "plaintext tokens", "none found in logs/data/backups", "")]


_CODE_SNAPSHOT = re.compile(r"^code-[0-9A-Za-z.+_-]+-\d{8}T?\d{6}Z?\.tar\.gz$")


def check_backups(backup_dir: Path, recipient: str | None) -> list[Check]:
    out: list[Check] = []
    out.append(("ok", "backup encryption", "ZIKO_AGE_RECIPIENT configured", "") if recipient
               else ("warn", "backup encryption", "ZIKO_AGE_RECIPIENT not set: platform backups will refuse to run",
                     "age-keygen -o /root/ziko-backup.agekey (store OFFLINE), set the public key"))
    try:
        files = [p for p in backup_dir.glob("*") if p.is_file()]
    except OSError:
        files = []
    # code-<version>-<stamp>.tar.gz are the updater's rollback snapshots of /opt/ziko-assistant:
    # program files only (no database, tokens or keys; config lives in /etc). Before 1.6 they
    # were reported as unencrypted backups, a false FAIL. Keep them: rollback needs them.
    snapshots = [p for p in files if _CODE_SNAPSHOT.match(p.name)]
    plain = [p.name for p in files if p.suffix in {".sqlite3", ".gz", ".tar", ".key"} and p not in snapshots]
    if plain:
        out.append(("fail", "unencrypted backups", f"{len(plain)} unencrypted file(s): {', '.join(plain[:3])}",
                    f"cd {backup_dir} && sudo rm -i -- " + " ".join(plain[:10])
                    + " (then: sudo ziko backup)"))
    if snapshots:
        out.append(("ok", "code snapshots", f"{len(snapshots)} updater rollback snapshot(s), program files only", ""))
    enc = sorted((p for p in files if p.suffix == ".age"), key=lambda p: p.stat().st_mtime)
    if not enc:
        out.append(("warn", "latest backup", "no encrypted backup found", "sudo ziko backup"))
    else:
        days = (time.time() - enc[-1].stat().st_mtime) / 86400
        out.append(("ok" if days <= 7 else "warn", "latest backup", f"{enc[-1].name}, {days:.1f} days old",
                    "" if days <= 7 else "sudo ziko backup (and schedule it)"))
    return out


def audit(*, env_file: Path, data_dir: Path, backup_dir: Path, unit_text: str | None,
          env: dict[str, str], service_user: str = "ziko") -> list[Check]:
    c: list[Check] = []
    c.append(check_path(env_file, "config file", max_mode=0o640, owner="root"))
    c.append(check_path(data_dir, "data directory", max_mode=0o700, kind="dir", owner=service_user))
    for name in ("ziko.sqlite3", "tenants.sqlite3"):
        c.append(check_path(data_dir / name, name, max_mode=0o600, owner=service_user,
                            required=name == "ziko.sqlite3"))
    key_raw = env.get("ZIKO_TENANT_KEY_FILE", "").strip()
    key_path = Path(key_raw) if key_raw else data_dir / "tenant-token.key"
    has_registry = (data_dir / "tenants.sqlite3").exists()
    if key_raw:
        c.append(check_path(key_path, "token key file", max_mode=0o640, owner="root"))
        if (data_dir / "tenant-token.key").exists():
            c.append(("warn", "token key location", "a second copy is still in the data directory",
                      "sudo ziko security move-key (re-run completes an interrupted move)"))
        else:
            c.append(("ok", "token key location", "outside the data directory; excluded from backups", ""))
    else:
        c.append(check_path(key_path, "token key file", max_mode=0o600, owner=service_user,
                            required=False))
        if has_registry:
            c.append(("warn", "token key location",
                      "key is in the data directory and inside every platform backup",
                      "sudo systemctl stop ziko && sudo ziko security move-key"))
    c.append(check_path(data_dir / "tenants", "tenant databases", max_mode=0o700, kind="dir",
                        owner=service_user, required=False))
    c.append(check_path(data_dir / "logs", "log directory", max_mode=0o700, kind="dir",
                        owner=service_user, required=False))
    c.append(check_path(backup_dir, "backup directory", max_mode=0o700, kind="dir", required=False))
    c += check_unit(unit_text)
    c.append(check_key(key_path))
    c += check_backups(backup_dir, env.get("ZIKO_AGE_RECIPIENT") or os.environ.get("ZIKO_AGE_RECIPIENT"))
    c += scan_plaintext_tokens([data_dir / "logs", backup_dir, data_dir / "run"])
    if env.get("ZIKO_UPDATE_CHANNEL", "stable") != "stable":
        c.append(("warn", "update channel", "non-stable channel configured (unverified code)",
                  "set ZIKO_UPDATE_CHANNEL=stable"))
    return c


def exit_code(checks: list[Check]) -> int:
    levels = {x[0] for x in checks}
    return 2 if "fail" in levels else 1 if "warn" in levels else 0

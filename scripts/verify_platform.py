#!/usr/bin/env python3
"""ZIKO platform verification used by backup, restore and the updater.

Subcommands:
  manifest DIR
      Verify DIR/manifest.json: every listed file exists with the recorded SHA-256 and
      size, and no unlisted file is present (manifest.json itself excepted).
  health --data-dir DIR --expected-schema N [--env-file F] [--telegram] [--strict-telegram]
      Check the live platform: SQLite integrity of the main database, the tenant registry
      and every tenant database; main schema version; that every registry token decrypts
      with the key; and, with --telegram, that the master and enabled child tokens are
      accepted by Telegram's getMe.

Secrets are never printed. Telegram errors are reported by exception type or HTTP status
only, because error text can contain the request URL, which contains the token.
Exit status is 0 when no check fails, 1 otherwise.
"""
from __future__ import annotations

import argparse
import hashlib
import json

import re
import sqlite3
import sys
import urllib.error
import urllib.request
from pathlib import Path

MANIFEST_NAME = "manifest.json"
MANIFEST_FORMAT = "ziko-platform-backup/1"
_TOKEN_RE = re.compile(r"[0-9]+:[A-Za-z0-9_-]{20,}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_manifest(directory: Path) -> list[str]:
    """Return a list of problems; an empty list means the directory matches its manifest."""
    problems: list[str] = []
    manifest_path = directory / MANIFEST_NAME
    if not manifest_path.is_file():
        return [f"{MANIFEST_NAME} is missing"]
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"{MANIFEST_NAME} is unreadable: {type(exc).__name__}"]
    tenant_key = manifest.get("tenant_key")
    if tenant_key is not None and (not isinstance(tenant_key, dict)
                                   or tenant_key.get("location") != "external"
                                   or not re.fullmatch(r"[0-9a-f]{64}", str(tenant_key.get("sha256", "")))):
        problems.append("malformed tenant_key entry in manifest")
    if tenant_key is not None and (directory / "tenant-token.key").exists():
        problems.append("archive claims an external key but also contains tenant-token.key")
    if manifest.get("format") != MANIFEST_FORMAT:
        problems.append(f"unsupported manifest format: {manifest.get('format')!r}")
    listed = manifest.get("files")
    if not isinstance(listed, dict) or not listed:
        return problems + ["manifest lists no files"]
    for rel, expected in sorted(listed.items()):
        path = directory / rel
        if path.is_symlink() or not path.is_file():
            problems.append(f"missing or non-regular file: {rel}")
            continue
        if path.stat().st_size != expected.get("bytes"):
            problems.append(f"size mismatch: {rel}")
        if _sha256(path) != expected.get("sha256"):
            problems.append(f"SHA-256 mismatch: {rel}")
    present = {
        p.relative_to(directory).as_posix()
        for p in directory.rglob("*") if p.is_file()
    } - {MANIFEST_NAME}
    for extra in sorted(present - set(listed)):
        problems.append(f"unlisted file present: {extra}")
    return problems


def _sqlite_problem(path: Path) -> str | None:
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    except sqlite3.Error as exc:
        return f"cannot open {path.name}: {type(exc).__name__}"
    try:
        result = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            return f"integrity_check failed for {path.name}"
        if conn.execute("PRAGMA foreign_key_check").fetchall():
            return f"foreign_key_check failed for {path.name}"
    except sqlite3.Error as exc:
        return f"cannot check {path.name}: {type(exc).__name__}"
    finally:
        conn.close()
    return None


def _user_version(path: Path) -> int:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()


def _read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip().removeprefix("export ").strip()] = value.strip().strip("\"'")
    return values


def _telegram_get_me(token: str) -> tuple[str, str]:
    """Return (status, detail). status is 'ok', 'rejected' or 'unreachable'."""
    url = f"https://api.telegram.org/bot{token}/getMe"
    try:
        with urllib.request.urlopen(url, timeout=15) as response:  # noqa: S310 - fixed HTTPS host
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 404):
            return "rejected", f"HTTP {exc.code}"
        return "unreachable", f"HTTP {exc.code}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return "unreachable", type(exc).__name__
    except ValueError:
        return "unreachable", "invalid JSON"
    if payload.get("ok") is True:
        return "ok", f"@{payload.get('result', {}).get('username', '?')}"
    return "rejected", "ok=false"


class Report:
    def __init__(self) -> None:
        self.failures = 0

    def ok(self, name: str, detail: str = "") -> None:
        print(f"OK    {name}{': ' + detail if detail else ''}")

    def warn(self, name: str, detail: str = "") -> None:
        print(f"WARN  {name}{': ' + detail if detail else ''}")

    def fail(self, name: str, detail: str = "") -> None:
        self.failures += 1
        print(f"FAIL  {name}{': ' + detail if detail else ''}")


def run_health(args: argparse.Namespace) -> int:
    report = Report()
    data = Path(args.data_dir)
    main_db = data / "ziko.sqlite3"
    registry_db = data / "tenants.sqlite3"
    key_file = data / "tenant-token.key"
    import os
    configured = os.environ.get("ZIKO_TENANT_KEY_FILE", "").strip()
    if not configured and getattr(args, "env_file", None):
        try:
            configured = _read_env(Path(args.env_file)).get("ZIKO_TENANT_KEY_FILE", "").strip()
        except OSError:
            configured = ""
    if configured:
        key_file = Path(configured)
    tenants_dir = data / "tenants"

    if not main_db.is_file():
        report.fail("main database", "not found")
    else:
        problem = _sqlite_problem(main_db)
        report.fail("main database integrity", problem) if problem else report.ok("main database integrity")
        version = _user_version(main_db)
        if version == args.expected_schema:
            report.ok("main schema", f"version {version}")
        elif version < args.expected_schema:
            report.fail("main schema", f"version {version} is older than expected {args.expected_schema}")
        else:
            report.fail("main schema", f"version {version} is newer than expected {args.expected_schema}")

    enabled_tokens: list[tuple[int, str]] = []
    if registry_db.is_file():
        problem = _sqlite_problem(registry_db)
        report.fail("tenant registry integrity", problem) if problem else report.ok("tenant registry integrity")
        if not key_file.is_file():
            report.fail("tenant key", "missing although the registry exists")
        else:
            try:
                from cryptography.fernet import Fernet, InvalidToken, MultiFernet
                fernet = MultiFernet([Fernet(k) for k in key_file.read_bytes().split() if k])
                conn = sqlite3.connect(f"file:{registry_db}?mode=ro", uri=True, timeout=10)
                try:
                    rows = conn.execute(
                        "SELECT bot_id, encrypted_token, enabled FROM tenants ORDER BY tenant_id"
                    ).fetchall()
                finally:
                    conn.close()
                bad = 0
                for bot_id, encrypted, enabled in rows:
                    try:
                        token = fernet.decrypt(str(encrypted).encode("ascii")).decode("utf-8")
                    except (InvalidToken, UnicodeError, ValueError):
                        bad += 1
                        continue
                    if enabled:
                        enabled_tokens.append((int(bot_id), token))
                if bad:
                    report.fail("tenant tokens", f"{bad} token(s) cannot be decrypted")
                else:
                    report.ok("tenant tokens", f"{len(rows)} decrypt, {len(enabled_tokens)} enabled")
            except ImportError:
                report.fail("tenant tokens", "cryptography is not installed")
    else:
        report.ok("tenant registry", "not in use")

    if tenants_dir.is_dir():
        for db in sorted(tenants_dir.glob("*.sqlite3")):
            problem = _sqlite_problem(db)
            if problem:
                report.warn("child database", problem)
        report.ok("child databases checked", str(len(list(tenants_dir.glob('*.sqlite3')))))

    if args.telegram:
        env_file = Path(args.env_file) if args.env_file else None
        master_token = ""
        if env_file and env_file.is_file():
            master_token = _read_env(env_file).get("BOT_TOKEN", "")
        if master_token and _TOKEN_RE.fullmatch(master_token):
            status, detail = _telegram_get_me(master_token)
            if status == "ok":
                report.ok("master bot getMe", detail)
            elif status == "rejected":
                # A revoked token is not caused by the release; rolling back would not fix it.
                (report.fail if args.strict_telegram else report.warn)(
                    "master bot getMe", f"token rejected ({detail})")
            else:
                (report.fail if args.strict_telegram else report.warn)(
                    "master bot getMe", f"unreachable ({detail})")
        else:
            report.warn("master bot getMe", "BOT_TOKEN not available")
        for bot_id, token in enabled_tokens:
            status, detail = _telegram_get_me(token)
            if status == "ok":
                report.ok(f"child bot {bot_id} getMe", detail)
            elif status == "rejected":
                # A revoked child token is an external event, not a reason to roll back.
                report.warn(f"child bot {bot_id} getMe", f"token rejected ({detail})")
            else:
                report.warn(f"child bot {bot_id} getMe", f"unreachable ({detail})")

    print(f"RESULT {'FAILED' if report.failures else 'PASSED'} ({report.failures} failure(s))")
    return 1 if report.failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    manifest = sub.add_parser("manifest")
    manifest.add_argument("directory")
    health = sub.add_parser("health")
    health.add_argument("--data-dir", required=True)
    health.add_argument("--expected-schema", type=int, required=True)
    health.add_argument("--env-file")
    health.add_argument("--telegram", action="store_true")
    health.add_argument("--strict-telegram", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "manifest":
        problems = verify_manifest(Path(args.directory))
        for problem in problems:
            print(f"FAIL  {problem}")
        print("RESULT PASSED" if not problems else f"RESULT FAILED ({len(problems)} problem(s))")
        return 1 if problems else 0
    return run_health(args)


if __name__ == "__main__":
    sys.exit(main())

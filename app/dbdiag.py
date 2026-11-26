"""Read-only SQLite diagnostics for ZIKO's main, registry and per-tenant databases.

Every connection is opened with ``mode=ro`` so diagnostics can never modify,
migrate or lock-escalate a live database. Token columns are never selected.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

EXPECTED_MAIN_TABLES = frozenset({
    "statuses", "reply_steps", "owner_settings", "reply_exceptions", "app_state",
    "business_connections", "processed_messages", "reply_deliveries",
})
# Tables whose rows are safe to count. Counting never reads row contents.
_IDENT_OK = frozenset("abcdefghijklmnopqrstuvwxyz_0123456789")


@dataclass(slots=True)
class DbReport:
    path: str
    exists: bool
    ok: bool = False
    size_bytes: int = 0
    wal_bytes: int = 0
    user_version: int | None = None
    journal_mode: str | None = None
    open_latency_ms: float | None = None
    query_latency_ms: float | None = None
    integrity: str | None = None
    foreign_key_violations: int | None = None
    tables: dict[str, int] = field(default_factory=dict)
    indexes: list[str] = field(default_factory=list)
    freelist_pages: int | None = None
    page_size: int | None = None
    missing_tables: list[str] = field(default_factory=list)
    delivery_states: dict[str, int] = field(default_factory=dict)
    stuck_pending: int | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def inspect(path: Path, *, full_integrity: bool = False, expect_main: bool = True,
            stuck_after_seconds: int = 600) -> DbReport:
    r = DbReport(path=str(path), exists=path.is_file())
    if not r.exists:
        r.error = "file not found"
        return r
    r.size_bytes = path.stat().st_size
    wal = path.with_name(path.name + "-wal")
    r.wal_bytes = wal.stat().st_size if wal.exists() else 0
    t0 = time.perf_counter()
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
    except sqlite3.Error as exc:
        r.error = f"open failed: {type(exc).__name__}"
        return r
    r.open_latency_ms = round((time.perf_counter() - t0) * 1000, 2)
    try:
        t1 = time.perf_counter()
        conn.execute("SELECT 1").fetchone()
        r.query_latency_ms = round((time.perf_counter() - t1) * 1000, 3)
        r.user_version = conn.execute("PRAGMA user_version").fetchone()[0]
        r.journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        r.page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        r.freelist_pages = conn.execute("PRAGMA freelist_count").fetchone()[0]
        pragma = "integrity_check" if full_integrity else "quick_check"
        r.integrity = conn.execute(f"PRAGMA {pragma}").fetchone()[0]
        r.foreign_key_violations = len(conn.execute("PRAGMA foreign_key_check").fetchall())
        names = [n for (n,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        for name in names:
            if set(name) <= _IDENT_OK:  # identifier comes from sqlite_master; still whitelisted
                r.tables[name] = conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
        r.indexes = [n for (n,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'")]
        if expect_main:
            r.missing_tables = sorted(EXPECTED_MAIN_TABLES - set(names))
            if "reply_deliveries" in names:
                r.delivery_states = dict(conn.execute(
                    "SELECT state, COUNT(*) FROM reply_deliveries GROUP BY state").fetchall())
                r.stuck_pending = conn.execute(
                    "SELECT COUNT(*) FROM reply_deliveries WHERE state='pending' "
                    "AND claimed_at < strftime('%Y-%m-%dT%H:%M:%fZ','now',?)",
                    (f"-{int(stuck_after_seconds)} seconds",)).fetchone()[0]
        r.ok = (r.integrity == "ok" and r.foreign_key_violations == 0 and not r.missing_tables)
    except sqlite3.Error as exc:
        r.error = f"{type(exc).__name__}: {exc}"
    finally:
        conn.close()
    return r


def discover(main_db: Path) -> dict[str, Path]:
    """Main DB, tenant registry and every tenant DB, using ZIKO's on-disk layout."""
    found = {"main": main_db, "registry": main_db.with_name("tenants.sqlite3")}
    tenant_dir = main_db.parent / "tenants"
    if tenant_dir.is_dir():
        for p in sorted(tenant_dir.glob("*.sqlite3")):
            if not p.is_symlink():
                found[f"tenant:{p.stem}"] = p
    return found


def inspect_all(main_db: Path, *, full_integrity: bool = False) -> dict[str, DbReport]:
    out = {}
    for label, path in discover(main_db).items():
        out[label] = inspect(path, full_integrity=full_integrity, expect_main=(label != "registry"))
    return out

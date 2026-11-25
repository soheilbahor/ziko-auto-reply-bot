#!/usr/bin/env python3
"""Dry-run the release's database migrations on a COPY of a database.

Usage (the updater runs this with PYTHONPATH pointing at the staged release):
    python scripts/migrate_check.py /path/to/copy.sqlite3

It opens the copy with the new app.storage.Database, which applies any pending migration
and seeds defaults, then prints the resulting schema version. Run it only on copies; it
writes to the file it is given. A non-zero exit means the release cannot migrate this
database, so the updater stops before touching the live installation.
"""
from __future__ import annotations

import asyncio
import sqlite3
import sys
from pathlib import Path


async def _migrate(path: Path) -> int:
    from app.storage import Database

    db = Database(path)
    await db.initialize()
    await db.close()
    conn = sqlite3.connect(path)
    try:
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise SystemExit("integrity_check failed after migration")
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: migrate_check.py COPY_OF_DATABASE", file=sys.stderr)
        return 2
    path = Path(argv[1])
    if not path.is_file():
        print(f"not a file: {path}", file=sys.stderr)
        return 2
    version = asyncio.run(_migrate(path))
    print(f"MIGRATION_OK schema_version={version}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

"""Crash-safe rotation of the Fernet key that encrypts child-bot tokens.

Key file format: one urlsafe-base64 Fernet key per line, NEWEST FIRST. Normally the
file has exactly one line. Two lines exist only during a rotation, so a crash at any
step leaves a state that every reader (MultiFernet) can still decrypt:

  step                          key file      registry rows
  1 verify all rows decrypt     [old]         old
  2 backup registry + key       [old]         old
  3 write key file              [new, old]    old        <- crash: old still decrypts
  4 re-encrypt in 1 transaction [new, old]    new        <- crash: rollback, old again
  5 write key file              [new]         new        <- crash: both lines still valid
  6 verify with new key only    [new]         new

The service must be stopped (the caller checks) so no row is written mid-rotation.
Tokens are decrypted only in memory and are never printed or logged.
"""
from __future__ import annotations

import os
import secrets
import shutil
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken, MultiFernet


class KeyRotationError(RuntimeError):
    """Rotation refused or failed. The message never contains key or token material."""


def parse_keys(raw: bytes) -> list[Fernet]:
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if not lines:
        raise KeyRotationError("key file is empty")
    try:
        return [Fernet(line) for line in lines]
    except (ValueError, TypeError):
        raise KeyRotationError("key file contains an invalid key") from None


def load_multifernet(key_path: Path) -> MultiFernet:
    return MultiFernet(parse_keys(key_path.read_bytes()))


def write_key_file(key_path: Path, keys: list[bytes]) -> None:
    """Atomic, fsynced, mode 0600. Keeps the original owner/group."""
    tmp = key_path.with_name(f".{key_path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with open(fd, "wb", closefd=True) as fh:
            fh.write(b"\n".join(keys) + b"\n")
            fh.flush()
            os.fsync(fh.fileno())
        mode = 0o600
        if key_path.exists():
            st = key_path.stat()
            # Keep owner, group and mode: an external key is root:ziko 0640 and the
            # service must still be able to read it after a rotation.
            mode = st.st_mode & 0o640
            try:
                os.chown(tmp, st.st_uid, st.st_gid)
            except PermissionError:
                pass
        os.chmod(tmp, mode)
        os.replace(tmp, key_path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:  # make the rename itself durable
        dfd = os.open(key_path.parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass


@dataclass(frozen=True, slots=True)
class RotationResult:
    rows: int
    backup_dir: Path
    seconds: float


def _rows(conn: sqlite3.Connection) -> list[tuple[int, str]]:
    return conn.execute("SELECT tenant_id, encrypted_token FROM tenants ORDER BY tenant_id").fetchall()


def verify(registry: Path, key_path: Path) -> int:
    """Return how many tokens decrypt. Raises if any does not."""
    mf = load_multifernet(key_path)
    conn = sqlite3.connect(f"file:{registry}?mode=ro", uri=True)
    try:
        rows = _rows(conn)
    finally:
        conn.close()
    bad = 0
    for _tid, enc in rows:
        try:
            mf.decrypt(enc.encode("ascii"))
        except (InvalidToken, ValueError):
            bad += 1
    if bad:
        raise KeyRotationError(f"{bad} of {len(rows)} stored tokens cannot be decrypted with the current key")
    return len(rows)


def rotate(registry: Path, key_path: Path, backup_root: Path, *,
           _crash_after: int | None = None) -> RotationResult:
    """Rotate the key. ``_crash_after`` exists only for crash-safety tests."""
    t0 = time.monotonic()
    if not registry.is_file() or not key_path.is_file():
        raise KeyRotationError("registry or key file not found")

    def crash(step: int) -> None:
        if _crash_after == step:
            raise KeyRotationError(f"simulated crash after step {step}")

    # 1. everything must decrypt before we touch anything
    rows = verify(registry, key_path)
    old_keys = [line.strip() for line in key_path.read_bytes().splitlines() if line.strip()]

    # 2. backup: consistent SQLite copy + key copy, private directory
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    # Random suffix: two rotations in the same second (e.g. re-run after a crash) never collide.
    backup = backup_root / f"key-rotation-{stamp}-{secrets.token_hex(4)}"
    backup.mkdir(parents=True, mode=0o700)
    src = sqlite3.connect(f"file:{registry}?mode=ro", uri=True)
    dst = sqlite3.connect(backup / "tenants.sqlite3")
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    shutil.copyfile(key_path, backup / "tenant-token.key")
    for f in backup.iterdir():
        os.chmod(f, 0o600)
    crash(2)

    # 3. new key first, old keys still accepted
    new_key = Fernet.generate_key()
    write_key_file(key_path, [new_key, *old_keys])
    crash(3)

    # 4. re-encrypt every row atomically
    mf = MultiFernet([Fernet(new_key), *(Fernet(k) for k in old_keys)])
    conn = sqlite3.connect(registry, isolation_level=None, timeout=10)
    try:
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("BEGIN IMMEDIATE")
        try:
            for tid, enc in _rows(conn):
                conn.execute("UPDATE tenants SET encrypted_token=? WHERE tenant_id=?",
                             (mf.rotate(enc.encode("ascii")).decode("ascii"), tid))
            crash(4)
            only_new = Fernet(new_key)
            for _tid, enc in _rows(conn):  # verify inside the transaction before commit
                only_new.decrypt(enc.encode("ascii"))
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()

    # 5. drop the old keys
    write_key_file(key_path, [new_key])
    crash(5)

    # 6. final proof
    if verify(registry, key_path) != rows:
        raise KeyRotationError("row count changed during rotation")
    return RotationResult(rows=rows, backup_dir=backup, seconds=round(time.monotonic() - t0, 3))

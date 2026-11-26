"""v1.6: updater code snapshots are not reported as unencrypted backups."""
from __future__ import annotations

from pathlib import Path

from app import security_audit as sa


def _names(checks, status):
    return [c for c in checks if c[0] == status]


def test_code_snapshots_are_ok_but_real_plaintext_still_fails(tmp_path: Path) -> None:
    for name in ("code-1.5.3-20261126T221500.tar.gz", "code-1.4.2-20261120T080000.tar.gz",
                 "ziko-20261126T221500Z.abc.sqlite3.age"):
        (tmp_path / name).write_bytes(b"x")
    checks = sa.check_backups(tmp_path, "age1example")
    assert not [c for c in checks if c[1] == "unencrypted backups"]
    assert any(c[1] == "code snapshots" and c[0] == "ok" and "2" in c[2] for c in checks)
    (tmp_path / "ziko-platform-20251125T000000Z.tar.gz").write_bytes(b"x")
    (tmp_path / "ziko-20251125.sqlite3").write_bytes(b"x")
    fail = [c for c in sa.check_backups(tmp_path, "age1example") if c[1] == "unencrypted backups"]
    assert fail and fail[0][0] == "fail" and "2 unencrypted" in fail[0][2]
    assert "code-1.5.3" not in fail[0][2] and "rm -i" in fail[0][3]


def test_lookalike_names_are_not_trusted(tmp_path: Path) -> None:
    for name in ("code-backup.tar.gz", "code-1.5.3.tar.gz", "xcode-1.5.3-20261126T221500.tar.gz"):
        (tmp_path / name).write_bytes(b"x")
    fail = [c for c in sa.check_backups(tmp_path, "age1example") if c[1] == "unencrypted backups"]
    assert fail and "3 unencrypted" in fail[0][2]

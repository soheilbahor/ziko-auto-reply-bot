"""Manifest verification used before any platform restore or update step trusts a backup."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("verify_platform", ROOT / "scripts" / "verify_platform.py")
verify_platform = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(verify_platform)


def _make_manifested_tree(root: Path) -> None:
    (root / "tenants").mkdir(parents=True)
    (root / "main.sqlite3").write_bytes(b"main-bytes")
    (root / "tenant-token.key").write_bytes(b"key-bytes")
    (root / "tenants" / "bot-1.sqlite3").write_bytes(b"child-bytes")
    files = {}
    for path in root.rglob("*"):
        if path.is_file():
            rel = path.relative_to(root).as_posix()
            files[rel] = {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                          "bytes": path.stat().st_size}
    manifest = {"format": "ziko-platform-backup/1", "files": files}
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def test_intact_tree_passes(tmp_path: Path):
    _make_manifested_tree(tmp_path)
    assert verify_platform.verify_manifest(tmp_path) == []


def test_modified_file_is_detected(tmp_path: Path):
    _make_manifested_tree(tmp_path)
    (tmp_path / "tenants" / "bot-1.sqlite3").write_bytes(b"tampered!!")
    problems = verify_platform.verify_manifest(tmp_path)
    assert any("bot-1.sqlite3" in p for p in problems)


def test_unlisted_file_is_detected(tmp_path: Path):
    _make_manifested_tree(tmp_path)
    (tmp_path / "injected.sqlite3").write_bytes(b"x")
    assert any("injected.sqlite3" in p for p in verify_platform.verify_manifest(tmp_path))


def test_missing_manifest_is_rejected(tmp_path: Path):
    _make_manifested_tree(tmp_path)
    (tmp_path / "manifest.json").unlink()
    assert verify_platform.verify_manifest(tmp_path) == ["manifest.json is missing"]

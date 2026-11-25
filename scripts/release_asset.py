#!/usr/bin/env python3
"""Resolve, verify and extract ZIKO release archives (standard library only).

Used by scripts/update.sh and by the release workflow. Every failure raises
ReleaseError with a message that names the cause; callers must stop on it.

Asset contract for a release tagged vX.Y.Z:
  * ziko-assistant-vX.Y.Z.tar.gz  (preferred) or ziko-assistant-vX.Y.Z.zip
  * SHA256SUMS listing that archive
  * archive top directory ziko-assistant-vX.Y.Z/ whose VERSION equals X.Y.Z
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tarfile
import urllib.error
import urllib.request
import zipfile
import zlib
from pathlib import Path, PurePosixPath

TAG_RE = re.compile(r"^v\d+\.\d+\.\d+(?:-[0-9A-Za-z.]+)?$")
FORMATS = (".tar.gz", ".zip")          # preference order when both exist
SUMS_NAME = "SHA256SUMS"
LEGACY_FIXED_SUFFIX = "-version-fixed.zip"
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
REQUIRED = (
    "VERSION", "app/main.py", "app/storage.py", "requirements.txt", "pyproject.toml",
    "scripts/update.sh", "scripts/backup.sh", "scripts/restore-platform.sh",
    "scripts/verify_platform.py", "scripts/migrate_check.py", "deploy/ziko.service",
)
FORBIDDEN_NAMES = re.compile(r"(^|/)\.env$|\.sqlite3?(-wal|-shm)?$|\.db$")


class ReleaseError(Exception):
    """A release cannot be installed. The message is shown to the operator."""


def asset_name(tag: str, fmt: str) -> str:
    return f"ziko-assistant-{tag}{fmt}"


def _check_tag(tag: str) -> None:
    if not TAG_RE.fullmatch(tag):
        raise ReleaseError(f"invalid release tag: {tag!r}")


def _api_get(url: str, timeout: int = 20) -> dict:
    request = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "ziko-updater",
    })
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise ReleaseError(f"release not found on GitHub: {url}") from None
        raise ReleaseError(f"GitHub API returned HTTP {exc.code} for the release") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ReleaseError("GitHub API is unreachable; retry later") from None
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ReleaseError("GitHub API returned an invalid response") from None


def resolve(api_url: str, repo: str, tag: str) -> dict:
    """Return the archive asset for ``tag`` as reported by the GitHub Release API."""
    _check_tag(tag)
    data = _api_get(f"{api_url.rstrip('/')}/repos/{repo}/releases/tags/{tag}")
    assets = [a for a in (data.get("assets") or []) if isinstance(a, dict)]
    if not assets:
        raise ReleaseError(
            f"release {tag} has no assets; publish the release archives before updating"
        )
    by_name = {a.get("name"): a for a in assets if a.get("state", "uploaded") == "uploaded"}
    chosen = next((fmt for fmt in FORMATS if asset_name(tag, fmt) in by_name), None)
    archive_name = asset_name(tag, chosen) if chosen else None
    # Compatibility for the manually named v1.4.1 package already uploaded by the
    # maintainer. It is accepted only with SHA256SUMS and a valid build attestation.
    if chosen is None:
        fixed_name = f"ziko-assistant-{tag.removeprefix('v')}{LEGACY_FIXED_SUFFIX}"
        if fixed_name in by_name:
            chosen = ".zip"
            archive_name = fixed_name
    if chosen is None or archive_name is None:
        expected = " or ".join(asset_name(tag, fmt) for fmt in FORMATS)
        found = ", ".join(sorted(str(n) for n in by_name)) or "none"
        raise ReleaseError(
            f"release {tag} has no supported archive ({expected}; "
            f"legacy fallback: ziko-assistant-{tag.removeprefix('v')}{LEGACY_FIXED_SUFFIX}); "
            f"found: {found}"
        )
    if SUMS_NAME not in by_name:
        raise ReleaseError(f"release {tag} has no {SUMS_NAME} asset; refusing an unverified archive")
    archive = by_name[archive_name]
    sums = by_name[SUMS_NAME]
    return {
        "tag": tag,
        "asset": archive_name,
        "format": chosen,
        "asset_url": archive["browser_download_url"],
        "sums_url": sums["browser_download_url"],
    }


def format_of(filename: str) -> str:
    for fmt in FORMATS:
        if filename.endswith(fmt):
            return fmt
    raise ReleaseError(f"unknown archive format: {filename!r} (expected .tar.gz or .zip)")


def download(url: str, dest: Path, timeout: int = 120) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "ziko-updater"})
    written = 0
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response, dest.open("wb") as out:
            while True:
                chunk = response.read(1 << 20)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_ARCHIVE_BYTES:
                    raise ReleaseError("download exceeds the maximum archive size")
                out.write(chunk)
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ReleaseError(f"download failed: {url}") from None


def verify_checksum(archive: Path, sums_file: Path, asset: str) -> str:
    """Check ``archive`` against the SHA256SUMS entry for ``asset``. Returns the digest."""
    entries = []
    for line in sums_file.read_text(encoding="utf-8").splitlines():
        parts = line.split(maxsplit=1)
        if len(parts) == 2 and parts[1].strip().lstrip("*") == asset:
            entries.append(parts[0].lower())
    if len(entries) != 1:
        raise ReleaseError(f"{SUMS_NAME} has no unique entry for {asset}")
    digest = hashlib.sha256()
    with archive.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    actual = digest.hexdigest()
    if actual != entries[0]:
        raise ReleaseError(f"SHA-256 mismatch for {asset} - refusing to install")
    return actual


def _safe_member(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if not name or path.is_absolute() or ".." in path.parts or "\\" in name:
        raise ReleaseError(f"archive contains an unsafe path: {name!r}")
    return path


def _members(archive: Path, fmt: str) -> dict[str, bytes | None]:
    """Return {relative_path: content-or-None} for every regular file, validating paths."""
    files: dict[str, bytes | None] = {}
    try:
        if fmt == ".tar.gz":
            with tarfile.open(archive, "r:gz") as tar:
                for member in tar:
                    path = _safe_member(member.name)
                    if member.issym() or member.islnk() or member.isdev():
                        raise ReleaseError(f"archive contains a link or device: {member.name!r}")
                    if member.isfile():
                        handle = tar.extractfile(member)
                        files[str(path)] = handle.read() if handle else b""
                    elif member.isdir():
                        files.setdefault(str(path) + "/", None)
                    else:
                        raise ReleaseError(f"archive contains an unsupported entry: {member.name!r}")
        else:
            with zipfile.ZipFile(archive) as zf:
                bad = zf.testzip()
                if bad is not None:
                    raise ReleaseError(f"corrupt zip archive (bad entry {bad!r})")
                for info in zf.infolist():
                    path = _safe_member(info.filename)
                    if info.is_dir():
                        files.setdefault(str(path) + "/", None)
                    else:
                        files[str(path)] = zf.read(info)
    except ReleaseError:
        raise
    except (tarfile.TarError, zipfile.BadZipFile, EOFError, OSError, zlib.error) as exc:
        raise ReleaseError(f"corrupt {fmt} archive: {type(exc).__name__}") from None
    return files


def _top_level(files: dict[str, bytes | None]) -> str:
    tops = {PurePosixPath(p).parts[0] for p in files if PurePosixPath(p).parts}
    if len(tops) != 1:
        raise ReleaseError("archive must contain exactly one top-level directory")
    return tops.pop()


def verify_structure(archive: Path, fmt: str, tag: str) -> dict:
    """Check layout, forbidden files, required files and the embedded VERSION."""
    files = _members(archive, fmt)
    top = _top_level(files)
    expected_tops = {f"ziko-assistant-{tag}"}
    if archive.name.endswith(LEGACY_FIXED_SUFFIX):
        expected_tops.add(f"ziko-assistant-{tag.removeprefix('v')}")
    if top not in expected_tops:
        expected = " or ".join(sorted(expected_tops))
        raise ReleaseError(f"archive top directory is {top!r}, expected {expected}")
    prefix = top + "/"
    for path in files:
        if FORBIDDEN_NAMES.search(path):
            raise ReleaseError(f"archive contains a forbidden file: {path}")
    for required in REQUIRED:
        if prefix + required not in files:
            raise ReleaseError(f"archive is missing required file: {required}")
    raw = files[prefix + "VERSION"] or b""
    embedded = raw.decode("utf-8", "replace").strip().removeprefix("v")
    if embedded != tag.removeprefix("v"):
        raise ReleaseError(
            f"archive VERSION ({embedded or 'empty'}) does not match release {tag}"
        )
    return {"top": top, "files": len(files)}


def extract(archive: Path, fmt: str, dest: Path) -> None:
    """Extract a verified archive into ``dest`` with the top directory removed."""
    files = _members(archive, fmt)
    top = _top_level(files)
    dest.mkdir(parents=True, exist_ok=True)
    for path, content in files.items():
        rel = PurePosixPath(path).relative_to(top) if path != top + "/" else None
        if rel is None or not rel.parts:
            continue
        target = dest.joinpath(*rel.parts)
        if path.endswith("/"):
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content or b"")


def verify_archive(archive: Path, sums_file: Path, asset: str, tag: str) -> dict:
    fmt = format_of(asset)
    if archive.name != asset:
        raise ReleaseError(f"downloaded file name {archive.name!r} does not match asset {asset!r}")
    digest = verify_checksum(archive, sums_file, asset)
    info = verify_structure(archive, fmt, tag)
    return {"asset": asset, "format": fmt, "sha256": digest, **info}


def _cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("resolve", help="print the archive asset of a release as JSON")
    p.add_argument("--api", required=True)
    p.add_argument("--repo", required=True)
    p.add_argument("--tag", required=True)

    p = sub.add_parser("verify", help="check SHA-256, layout and embedded VERSION")
    p.add_argument("--file", type=Path, required=True)
    p.add_argument("--sums", type=Path, required=True)
    p.add_argument("--asset", required=True)
    p.add_argument("--tag", required=True)

    p = sub.add_parser("extract", help="verify then extract into a directory")
    p.add_argument("--file", type=Path, required=True)
    p.add_argument("--sums", type=Path, required=True)
    p.add_argument("--asset", required=True)
    p.add_argument("--tag", required=True)
    p.add_argument("--dest", type=Path, required=True)

    p = sub.add_parser("verify-release", help="fetch a published release and verify it end to end")
    p.add_argument("--api", required=True)
    p.add_argument("--repo", required=True)
    p.add_argument("--tag", required=True)
    p.add_argument("--workdir", type=Path, required=True)

    args = parser.parse_args(argv)
    try:
        if args.command == "resolve":
            print(json.dumps(resolve(args.api, args.repo, args.tag)))
        elif args.command == "verify":
            info = verify_archive(args.file, args.sums, args.asset, args.tag)
            print(f"OK {info['asset']} sha256={info['sha256']} files={info['files']}")
        elif args.command == "extract":
            verify_archive(args.file, args.sums, args.asset, args.tag)
            extract(args.file, format_of(args.asset), args.dest)
            print(f"extracted {args.asset} to {args.dest}")
        elif args.command == "verify-release":
            info = resolve(args.api, args.repo, args.tag)
            args.workdir.mkdir(parents=True, exist_ok=True)
            archive = args.workdir / info["asset"]
            sums = args.workdir / SUMS_NAME
            download(info["asset_url"], archive)
            download(info["sums_url"], sums)
            result = verify_archive(archive, sums, info["asset"], args.tag)
            print(f"OK published release {args.tag}: {result['asset']} sha256={result['sha256']}")
    except ReleaseError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(_cli())

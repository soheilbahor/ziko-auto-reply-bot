"""`ziko update` subcommands: check, history, verify, rollback (and logs).

Design:
* update.sh stays the only thing that changes an installation. It now also appends one
  JSON line per stage to ``/var/lib/ziko-assistant/run/update-journal.jsonl`` (0600).
  This module only READS that journal; it never parses free-form log text.
* ``check`` asks the GitHub Releases API (read-only, no token) for the latest stable
  release and its notes. Release notes are untrusted text: control characters are
  stripped before printing (a crafted release cannot inject terminal escape sequences).
* ``verify`` re-checks the INSTALLED tree: VERSION marker, every file against the
  manifest written at install time, no unexpected files, Python byte-compile.
* ``rollback`` does not invent a new mechanism: it restores one of the pre-update code
  snapshots update.sh already keeps (``code-<version>-<stamp>.tar.gz``) and the matching
  dependencies, then health-checks. Databases are NOT rolled back by default (newer data
  would be lost); ``--with-data`` restores the matching encrypted platform backup through
  restore-platform.sh, which verifies it before touching anything.

All output is English. No secret is read or printed.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tarfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

JOURNAL_NAME = "update-journal.jsonl"
MANIFEST_NAME = ".ziko-install-manifest.json"
SNAPSHOT_RE = re.compile(r"^code-(?P<version>v?\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?|unknown)-"
                         r"(?P<stamp>\d{8}T\d{6})-(?P<pid>\d+)\.tar\.gz$")
TAG_RE = re.compile(r"^v?\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f\x9b]|\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07?")
# Files that must exist in any installed release (same list update.sh validates).
REQUIRED = ("VERSION", "app/main.py", "app/storage.py", "requirements.txt", "pyproject.toml",
            "scripts/update.sh", "scripts/backup.sh", "scripts/restore-platform.sh",
            "scripts/verify_platform.py", "deploy/ziko.service")
# Never part of the manifest: per-installation or generated content.
EXCLUDE_DIRS = frozenset({".venv", "data", "backups", "__pycache__", ".git"})
EXCLUDE_FILES = frozenset({".env", MANIFEST_NAME})


class UpdateError(RuntimeError):
    """Operator-facing failure; the message is safe to print."""


# --------------------------------------------------------------------------- versions
def parse_version(text: str) -> tuple[int, int, int, tuple[Any, ...]]:
    """SemVer-ish ordering: 1.5.0 > 1.5.0-rc2 > 1.5.0-rc1 > 1.4.9."""
    t = text.strip().removeprefix("v")
    m = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?", t)
    if not m:
        raise ValueError(f"not a version: {text!r}")
    pre = m.group(4)
    # A release (no pre-release tag) sorts after any of its pre-releases.
    pre_key: tuple[Any, ...] = (1,) if pre is None else (0, *(
        (0, int(p), "") if p.isdigit() else (1, 0, p) for p in pre.split(".")))
    return int(m.group(1)), int(m.group(2)), int(m.group(3)), pre_key


def is_newer(candidate: str, installed: str) -> bool:
    try:
        return parse_version(candidate) > parse_version(installed)
    except ValueError:
        return False


def clean_text(text: str, limit: int = 4000) -> str:
    """Make untrusted release-note text safe for a terminal."""
    text = _CONTROL.sub("", text.replace("\r\n", "\n"))
    return text if len(text) <= limit else text[:limit].rstrip() + "\n... (truncated)"


# --------------------------------------------------------------------------- GitHub
@dataclass(frozen=True, slots=True)
class Release:
    tag: str
    name: str
    published_at: str
    notes: str
    url: str
    prerelease: bool
    assets: tuple[str, ...]


def _api_get(url: str, timeout: float) -> Any:
    request = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "ziko-updater"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read(2_000_000).decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise UpdateError("no published stable release found (GitHub returned 404)") from None
        if exc.code == 403:
            raise UpdateError("GitHub API rate limit reached; try again later") from None
        raise UpdateError(f"GitHub API error HTTP {exc.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise UpdateError("cannot reach api.github.com (network, DNS or TLS problem)") from None
    except ValueError:
        raise UpdateError("GitHub returned an invalid response") from None


def latest_release(repo: str, *, api: str = "https://api.github.com", timeout: float = 8.0,
                   fetch: Any = None) -> Release:
    if not re.fullmatch(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", repo):
        raise UpdateError(f"invalid repository name: {repo!r}")
    data = (fetch or _api_get)(f"{api.rstrip('/')}/repos/{repo}/releases/latest", timeout)
    if not isinstance(data, dict) or not isinstance(data.get("tag_name"), str):
        raise UpdateError("GitHub returned an unexpected release format")
    tag = data["tag_name"]
    if not TAG_RE.fullmatch(tag):
        raise UpdateError(f"latest release has an invalid tag: {clean_text(tag, 40)!r}")
    assets = tuple(a.get("name", "") for a in data.get("assets") or [] if isinstance(a, dict))
    return Release(tag=tag, name=clean_text(str(data.get("name") or tag), 200),
                   published_at=str(data.get("published_at") or "")[:20],
                   notes=clean_text(str(data.get("body") or "")),
                   url=str(data.get("html_url") or ""), prerelease=bool(data.get("prerelease")),
                   assets=assets)


@dataclass(frozen=True, slots=True)
class CheckResult:
    installed: str
    latest: Release
    update_available: bool
    verifiable: bool          # release ships SHA256SUMS + an archive the updater accepts
    problems: tuple[str, ...]


def check(installed: str, release: Release) -> CheckResult:
    problems = []
    archive = f"ziko-assistant-{release.tag}.tar.gz"
    if "SHA256SUMS" not in release.assets:
        problems.append("release has no SHA256SUMS asset: the updater will refuse it")
    if archive not in release.assets and not any(a.endswith(".zip") for a in release.assets):
        problems.append(f"release has no {archive} asset: the updater will refuse it")
    if release.prerelease:
        problems.append("release is marked as a pre-release")
    return CheckResult(installed=installed, latest=release,
                       update_available=is_newer(release.tag, installed),
                       verifiable=not problems, problems=tuple(problems))


# --------------------------------------------------------------------------- journal
def journal_path(data_dir: Path) -> Path:
    return data_dir / "run" / JOURNAL_NAME


def read_journal(path: Path, max_bytes: int = 2_000_000) -> list[dict[str, Any]]:
    """Parse the JSON-lines journal; malformed lines are skipped, never fatal."""
    try:
        with path.open("rb") as fh:
            size = path.stat().st_size
            fh.seek(max(0, size - max_bytes))
            raw = fh.read().decode("utf-8", "replace")
    except OSError:
        return []
    events = []
    for line in raw.splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict) and isinstance(item.get("run"), str) and isinstance(item.get("stage"), str):
            events.append(item)
    return events


@dataclass(slots=True)
class Run:
    run: str
    started: str
    finished: str
    from_version: str
    to_version: str
    channel: str
    outcome: str              # success | failed | rolled_back | rollback | running | interrupted
    failed_stage: str | None
    message: str
    stages: list[str]
    platform_backup: str | None
    code_snapshot: str | None


def runs(events: list[dict[str, Any]], *, now: float | None = None) -> list[Run]:
    """Group journal events into update runs, newest first."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for e in events:
        grouped.setdefault(e["run"], []).append(e)
    out = []
    for run_id, items in grouped.items():
        stages = [e["stage"] for e in items]
        last = items[-1]
        first = items[0]
        meta: dict[str, Any] = {}
        for e in items:
            meta.update({k: v for k, v in e.items() if k in {"from", "to", "channel", "platform_backup",
                                                             "code_snapshot", "kind"} and v})
        if meta.get("kind") == "rollback" and "ROLLBACK_COMPLETE" in stages:
            outcome = "rollback"          # a manual `ziko update rollback` that succeeded
        elif "UPDATE_COMPLETE" in stages:
            outcome = "success"
        elif "ROLLBACK_DONE" in stages:   # an update failed and was rolled back automatically
            outcome = "rolled_back"
        elif last["stage"] in {"FAILED", "ERROR"}:
            outcome = "failed"
        else:
            # No terminal event: still running, or the process was killed.
            ts = _epoch(last.get("ts", ""))
            outcome = "running" if ts and (now or time.time()) - ts < 3600 else "interrupted"
        failed = next((e.get("failed_stage") or e.get("prev") for e in items
                       if e["stage"] in {"FAILED", "ERROR", "ROLLBACK"}), None)
        message = next((str(e.get("message", "")) for e in reversed(items) if e.get("message")), "")
        out.append(Run(run=run_id, started=first.get("ts", ""), finished=last.get("ts", ""),
                       from_version=str(meta.get("from", "?")), to_version=str(meta.get("to", "?")),
                       channel=str(meta.get("channel", "stable")), outcome=outcome,
                       failed_stage=failed, message=clean_text(message, 300), stages=stages,
                       platform_backup=meta.get("platform_backup"), code_snapshot=meta.get("code_snapshot")))
    out.sort(key=lambda r: r.started, reverse=True)
    return out


def _epoch(ts: str) -> float | None:
    try:
        return time.mktime(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone
    except (ValueError, OverflowError):
        return None


# --------------------------------------------------------------------------- verify
def _walk(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root)
        if rel_dir.parts and rel_dir.parts[0] in EXCLUDE_DIRS:
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
        for name in filenames:
            rel = (rel_dir / name).as_posix()
            if name in EXCLUDE_FILES or name.endswith((".pyc", ".pyo")):
                continue
            yield rel, Path(dirpath) / name


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_manifest(root: Path, version: str) -> dict[str, Any]:
    files = {}
    for rel, path in _walk(root):
        if path.is_symlink():
            raise UpdateError(f"refusing to record a symlink in the manifest: {rel}")
        files[rel] = {"sha256": _sha256(path), "bytes": path.stat().st_size}
    return {"format": "ziko-install-manifest/1", "version": version,
            "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "files": files}


def write_manifest(root: Path, version: str) -> Path:
    manifest = build_manifest(root, version)
    path = root / MANIFEST_NAME
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
    return path


@dataclass(slots=True)
class VerifyReport:
    ok: bool
    version: str
    checked: int
    problems: list[str]
    warnings: list[str]


def verify(root: Path) -> VerifyReport:
    problems: list[str] = []
    warnings: list[str] = []
    try:
        version = (root / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        version = "unknown"
        problems.append("VERSION file is missing or unreadable")
    else:
        if not TAG_RE.fullmatch(version):
            problems.append(f"VERSION marker is malformed: {version!r}")
    for rel in REQUIRED:
        if not (root / rel).is_file():
            problems.append(f"required file missing: {rel}")
    checked = 0
    manifest_path = root / MANIFEST_NAME
    if not manifest_path.is_file():
        warnings.append("no install manifest (installed before v1.5.0): file hashes not checked; "
                        "run 'ziko update verify --record' once to create it")
    else:
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            listed: dict[str, Any] = manifest["files"]
        except (OSError, ValueError, KeyError, TypeError):
            problems.append("install manifest is unreadable")
            listed = {}
        if manifest_path.is_file() and listed and manifest.get("version") != version:
            problems.append(f"manifest is for {manifest.get('version')!r} but VERSION says {version!r}")
        present = dict(_walk(root))
        for rel, expected in sorted(listed.items()):
            path = present.get(rel)
            if path is None:
                problems.append(f"missing: {rel}")
                continue
            checked += 1
            if path.is_symlink():
                problems.append(f"replaced by a symlink: {rel}")
            elif path.stat().st_size != expected.get("bytes") or _sha256(path) != expected.get("sha256"):
                problems.append(f"modified: {rel}")
        for rel in sorted(set(present) - set(listed)):
            problems.append(f"unexpected file: {rel}")
    for rel, path in _walk(root):
        if rel.endswith(".py") and (rel.startswith("app/") or rel.startswith("scripts/")):
            try:
                source = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                problems.append(f"unreadable or not UTF-8: {rel}")
                continue
            try:
                compile(source, rel, "exec")
            except (SyntaxError, ValueError):
                problems.append(f"does not compile: {rel}")
    return VerifyReport(ok=not problems, version=version, checked=checked,
                        problems=problems, warnings=warnings)


# --------------------------------------------------------------------------- rollback
@dataclass(frozen=True, slots=True)
class Snapshot:
    path: Path
    version: str
    stamp: str
    size: int


def snapshots(backup_dir: Path) -> list[Snapshot]:
    """Pre-update code snapshots written by update.sh, newest first."""
    found = []
    try:
        entries = list(backup_dir.iterdir())
    except OSError:
        return []
    for p in entries:
        m = SNAPSHOT_RE.match(p.name)
        if m and p.is_file() and not p.is_symlink():
            found.append(Snapshot(p, m.group("version"), m.group("stamp"), p.stat().st_size))
    found.sort(key=lambda s: s.stamp, reverse=True)
    return found


def check_snapshot(snapshot: Path) -> list[str]:
    """Validate a code snapshot before anything is touched. Returns problems."""
    problems = []
    try:
        with tarfile.open(snapshot, "r:gz") as tar:
            names = set()
            for member in tar:
                name = member.name.removeprefix("./")
                parts = Path(name).parts
                if member.name.startswith("/") or ".." in parts:
                    problems.append(f"unsafe path in snapshot: {member.name!r}")
                if member.issym() or member.islnk() or member.isdev():
                    problems.append(f"link or device in snapshot: {member.name!r}")
                if parts and (parts[0] in {".env", "data", ".venv", "backups"}):
                    problems.append(f"snapshot contains excluded entry: {member.name!r}")
                if member.isfile():
                    names.add(name)
    except (tarfile.TarError, OSError, EOFError):
        return ["snapshot cannot be read (corrupt or not a .tar.gz)"]
    for rel in REQUIRED:
        if rel not in names:
            problems.append(f"snapshot is missing {rel}")
    return problems


def rollback_plan(backup_dir: Path, installed: str, target: str | None = None) -> Snapshot:
    candidates = [s for s in snapshots(backup_dir) if s.version.removeprefix("v") != installed.removeprefix("v")]
    if target:
        wanted = target.removeprefix("v")
        candidates = [s for s in candidates if s.version.removeprefix("v") == wanted]
        if not candidates:
            raise UpdateError(f"no code snapshot of version {target} in {backup_dir}")
    if not candidates:
        raise UpdateError(f"no earlier version to roll back to: no code-*.tar.gz in {backup_dir}")
    return candidates[0]

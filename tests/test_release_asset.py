"""Release asset contract: resolution from the GitHub API, archive validation, checksum.

Each failure mode of an installable release has its own test. A test passes only
when the expected error is raised with the expected cause; nothing is skipped.
"""
from __future__ import annotations

import hashlib
import io
import json
import sys
import tarfile
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import release_asset as ra  # noqa: E402

TAG = "v1.4.1"
TOP = f"ziko-assistant-{TAG}"
REQUIRED_TEXT = {
    "app/main.py": "print('main')\n",
    "app/storage.py": "SCHEMA_VERSION = 3\n",
    "requirements.txt": "aiogram==3.34.0\n",
    "pyproject.toml": "[project]\n",
    "scripts/update.sh": "#!/usr/bin/env bash\n",
    "scripts/backup.sh": "#!/usr/bin/env bash\n",
    "scripts/restore-platform.sh": "#!/usr/bin/env bash\n",
    "scripts/verify_platform.py": "\n",
    "scripts/migrate_check.py": "\n",
    "deploy/ziko.service": "[Unit]\n",
}


def project_files(version: str = "1.4.1") -> dict[str, bytes]:
    files = {f"{TOP}/{k}": v.encode() for k, v in REQUIRED_TEXT.items()}
    files[f"{TOP}/VERSION"] = f"{version}\n".encode()
    return files


def make_tar(path: Path, files: dict[str, bytes]) -> Path:
    with tarfile.open(path, "w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return path


def make_zip(path: Path, files: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return path


def sums_for(*archives: Path) -> str:
    lines = []
    for archive in archives:
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        lines.append(f"{digest}  {archive.name}")
    return "\n".join(lines) + "\n"


@pytest.fixture
def api(tmp_path):
    """Local stand-in for api.github.com. ``routes`` maps a tag to a release JSON body."""
    state: dict = {"routes": {}}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # keep test output clean
            pass

        def do_GET(self):
            prefix = "/repos/owner/repo/releases/tags/"
            tag = self.path[len(prefix):] if self.path.startswith(prefix) else None
            body = state["routes"].get(tag)
            if body is None:
                self.send_response(404)
                self.end_headers()
                return
            if body == "__garbage__":
                payload = b"<html>not json</html>"
            else:
                payload = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield {"url": f"http://127.0.0.1:{server.server_port}", "state": state}
    server.shutdown()
    server.server_close()


def asset(name: str, url_base: str = "https://example.invalid/dl") -> dict:
    return {"name": name, "state": "uploaded", "browser_download_url": f"{url_base}/{name}", "size": 1}


# ---------------------------------------------------------------- resolution (API)

def test_release_without_assets_is_rejected_before_any_download(api):
    api["state"]["routes"][TAG] = {"tag_name": TAG, "assets": []}
    with pytest.raises(ra.ReleaseError, match="has no assets"):
        ra.resolve(api["url"], "owner/repo", TAG)


def test_wrong_asset_name_is_reported_with_what_was_found(api):
    api["state"]["routes"][TAG] = {"tag_name": TAG, "assets": [
        asset("ziko-assistant-1.4.1.zip"), asset("SHA256SUMS"),
    ]}
    with pytest.raises(ra.ReleaseError) as info:
        ra.resolve(api["url"], "owner/repo", TAG)
    message = str(info.value)
    assert "no supported archive" in message
    assert "ziko-assistant-1.4.1.zip" in message  # the wrong name is named


def test_missing_checksum_asset_is_rejected(api):
    api["state"]["routes"][TAG] = {"tag_name": TAG, "assets": [asset(f"ziko-assistant-{TAG}.zip")]}
    with pytest.raises(ra.ReleaseError, match="has no SHA256SUMS"):
        ra.resolve(api["url"], "owner/repo", TAG)


def test_unknown_release_is_reported_as_not_found(api):
    with pytest.raises(ra.ReleaseError, match="not found"):
        ra.resolve(api["url"], "owner/repo", "v9.9.9")


def test_unparseable_api_response_is_rejected(api):
    api["state"]["routes"][TAG] = "__garbage__"
    with pytest.raises(ra.ReleaseError, match="invalid response"):
        ra.resolve(api["url"], "owner/repo", TAG)


def test_resolution_prefers_tar_gz_and_accepts_zip_alone(api):
    api["state"]["routes"][TAG] = {"tag_name": TAG, "assets": [
        asset(f"{TOP}.zip"), asset(f"{TOP}.tar.gz"), asset("SHA256SUMS"),
    ]}
    chosen = ra.resolve(api["url"], "owner/repo", TAG)
    assert chosen["format"] == ".tar.gz"
    api["state"]["routes"][TAG] = {"tag_name": TAG, "assets": [asset(f"{TOP}.zip"), asset("SHA256SUMS")]}
    assert ra.resolve(api["url"], "owner/repo", TAG)["format"] == ".zip"


def test_invalid_tag_never_reaches_the_api(api):
    with pytest.raises(ra.ReleaseError, match="invalid release tag"):
        ra.resolve(api["url"], "owner/repo", "v1.4.1/../../x")




def test_resolution_accepts_the_published_version_fixed_zip_alias(api):
    alias = "ziko-assistant-1.4.1-version-fixed.zip"
    api["state"]["routes"][TAG] = {"tag_name": TAG, "assets": [
        asset(alias), asset("SHA256SUMS"),
    ]}
    chosen = ra.resolve(api["url"], "owner/repo", TAG)
    assert chosen["asset"] == alias
    assert chosen["format"] == ".zip"


def test_version_fixed_zip_accepts_archive_root_without_v_prefix(tmp_path):
    alias = "ziko-assistant-1.4.1-version-fixed.zip"
    alias_top = "ziko-assistant-1.4.1"
    content = {f"{alias_top}/{k}": v for k, v in REQUIRED_TEXT.items()}
    content[f"{alias_top}/VERSION"] = b"1.4.1\n"
    archive = make_zip(tmp_path / alias, content)
    sums = tmp_path / "SHA256SUMS"
    sums.write_text(sums_for(archive))
    info = ra.verify_archive(archive, sums, alias, TAG)
    assert info["top"] == alias_top
    dest = tmp_path / "stage"
    ra.extract(archive, ".zip", dest)
    assert (dest / "VERSION").read_text().strip() == "1.4.1"

# ---------------------------------------------------------------- archive validation

@pytest.mark.parametrize("maker,suffix", [(make_tar, ".tar.gz"), (make_zip, ".zip")])
def test_valid_archive_passes_checksum_structure_and_version(tmp_path, maker, suffix):
    archive = maker(tmp_path / f"{TOP}{suffix}", project_files())
    sums = tmp_path / "SHA256SUMS"
    sums.write_text(sums_for(archive))
    info = ra.verify_archive(archive, sums, archive.name, TAG)
    assert info["format"] == suffix
    assert info["top"] == TOP


@pytest.mark.parametrize("maker,suffix", [(make_tar, ".tar.gz"), (make_zip, ".zip")])
def test_corrupt_archive_is_rejected(tmp_path, maker, suffix):
    archive = maker(tmp_path / f"{TOP}{suffix}", project_files())
    archive.write_bytes(archive.read_bytes()[: len(archive.read_bytes()) // 2])  # truncated
    sums = tmp_path / "SHA256SUMS"
    sums.write_text(sums_for(archive))  # checksum of the broken file itself
    with pytest.raises(ra.ReleaseError, match="corrupt|mismatch|missing|unsafe"):
        ra.verify_archive(archive, sums, archive.name, TAG)


def test_zip_with_garbage_bytes_is_corrupt(tmp_path):
    archive = tmp_path / f"{TOP}.zip"
    archive.write_bytes(b"this is not a zip file at all" * 10)
    sums = tmp_path / "SHA256SUMS"
    sums.write_text(sums_for(archive))
    with pytest.raises(ra.ReleaseError, match="corrupt"):
        ra.verify_archive(archive, sums, archive.name, TAG)


def test_checksum_mismatch_refuses_install(tmp_path):
    archive = make_tar(tmp_path / f"{TOP}.tar.gz", project_files())
    sums = tmp_path / "SHA256SUMS"
    sums.write_text(f"{'0' * 64}  {archive.name}\n")
    with pytest.raises(ra.ReleaseError, match="SHA-256 mismatch"):
        ra.verify_archive(archive, sums, archive.name, TAG)


def test_checksum_entry_must_exist_exactly_once(tmp_path):
    archive = make_tar(tmp_path / f"{TOP}.tar.gz", project_files())
    sums = tmp_path / "SHA256SUMS"
    sums.write_text("")
    with pytest.raises(ra.ReleaseError, match="no unique entry"):
        ra.verify_archive(archive, sums, archive.name, TAG)


def test_unknown_format_stops_before_anything_is_read(tmp_path):
    odd = tmp_path / f"{TOP}.rar"
    odd.write_bytes(b"Rar!")
    sums = tmp_path / "SHA256SUMS"
    sums.write_text(sums_for(odd))
    with pytest.raises(ra.ReleaseError, match="unknown archive format"):
        ra.verify_archive(odd, sums, odd.name, TAG)


def test_embedded_version_must_match_the_tag(tmp_path):
    archive = make_zip(tmp_path / f"{TOP}.zip", project_files(version="1.2.2"))
    sums = tmp_path / "SHA256SUMS"
    sums.write_text(sums_for(archive))
    with pytest.raises(ra.ReleaseError, match="VERSION .* does not match release"):
        ra.verify_archive(archive, sums, archive.name, TAG)


def test_missing_required_file_is_rejected(tmp_path):
    files = project_files()
    del files[f"{TOP}/app/storage.py"]
    archive = make_tar(tmp_path / f"{TOP}.tar.gz", files)
    sums = tmp_path / "SHA256SUMS"
    sums.write_text(sums_for(archive))
    with pytest.raises(ra.ReleaseError, match="missing required file: app/storage.py"):
        ra.verify_archive(archive, sums, archive.name, TAG)


def test_path_traversal_in_zip_is_rejected(tmp_path):
    files = project_files()
    files["../escape.txt"] = b"x"
    archive = make_zip(tmp_path / f"{TOP}.zip", files)
    sums = tmp_path / "SHA256SUMS"
    sums.write_text(sums_for(archive))
    with pytest.raises(ra.ReleaseError, match="unsafe path"):
        ra.verify_archive(archive, sums, archive.name, TAG)


def test_database_or_env_file_inside_release_is_rejected(tmp_path):
    files = project_files()
    files[f"{TOP}/data/ziko.sqlite3"] = b"SQLite format 3"
    archive = make_tar(tmp_path / f"{TOP}.tar.gz", files)
    sums = tmp_path / "SHA256SUMS"
    sums.write_text(sums_for(archive))
    with pytest.raises(ra.ReleaseError, match="forbidden file"):
        ra.verify_archive(archive, sums, archive.name, TAG)


def test_wrong_top_directory_is_rejected(tmp_path):
    files = {k.replace(TOP, "something-else"): v for k, v in project_files().items()}
    archive = make_zip(tmp_path / f"{TOP}.zip", files)
    sums = tmp_path / "SHA256SUMS"
    sums.write_text(sums_for(archive))
    with pytest.raises(ra.ReleaseError, match="top directory"):
        ra.verify_archive(archive, sums, archive.name, TAG)


def test_extract_strips_top_directory_and_keeps_contents(tmp_path):
    archive = make_tar(tmp_path / f"{TOP}.tar.gz", project_files())
    dest = tmp_path / "stage"
    ra.extract(archive, ".tar.gz", dest)
    assert (dest / "VERSION").read_text().strip() == "1.4.1"
    assert (dest / "app" / "storage.py").read_text() == REQUIRED_TEXT["app/storage.py"]


# ---------------------------------------------------------------- end to end over HTTP

def test_resolve_then_download_then_verify_over_http(api, tmp_path):
    archive = make_zip(tmp_path / f"{TOP}.zip", project_files())
    sums = tmp_path / "SHA256SUMS"
    sums.write_text(sums_for(archive))
    base = api["url"] + "/files"
    api["state"]["routes"][TAG] = {"tag_name": TAG, "assets": [
        {"name": archive.name, "state": "uploaded", "browser_download_url": f"{base}/{archive.name}"},
        {"name": "SHA256SUMS", "state": "uploaded", "browser_download_url": f"{base}/SHA256SUMS"},
    ]}
    served = {archive.name: archive, "SHA256SUMS": sums}
    original = ra.download

    def local_download(url, dest, timeout=120):
        name = url.rsplit("/", 1)[-1]
        dest.write_bytes(served[name].read_bytes())

    ra.download = local_download
    try:
        chosen = ra.resolve(api["url"], "owner/repo", TAG)
        got = tmp_path / "got" / chosen["asset"]
        got.parent.mkdir()
        ra.download(chosen["asset_url"], got)
        ra.download(chosen["sums_url"], tmp_path / "got" / "SHA256SUMS")
        info = ra.verify_archive(got, tmp_path / "got" / "SHA256SUMS", chosen["asset"], TAG)
        assert info["format"] == ".zip"
    finally:
        ra.download = original

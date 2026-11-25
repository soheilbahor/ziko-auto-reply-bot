#!/usr/bin/env bash
# Install the latest stable ZIKO GitHub Release after checksum, archive and provenance checks.
set -Eeuo pipefail
umask 077

die(){ printf '[ziko-github-install] ERROR: %s\n' "$*" >&2; exit 1; }
log(){ printf '[ziko-github-install] %s\n' "$*"; }

[[ ${EUID:-$(id -u)} -eq 0 ]] || die "Run with sudo: bash install-from-github.sh [OWNER/REPOSITORY]"
REPO="${ZIKO_REPO:-ziko-freedom/ziko-auto-reply-bot}"
if [[ $# -gt 1 ]]; then die "Usage: install-from-github.sh [OWNER/REPOSITORY]"; fi
[[ $# -eq 0 ]] || REPO="$1"
[[ "$REPO" =~ ^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$ ]] || die "Invalid repository: $REPO"

for cmd in curl python3 tar sha256sum gh; do
  command -v "$cmd" >/dev/null 2>&1 || die "Missing prerequisite: $cmd"
done

WORK="$(mktemp -d)"
cleanup(){ rm -rf -- "$WORK"; }
trap cleanup EXIT

API="${ZIKO_API_URL:-https://api.github.com}"
curl -fsSL --retry 3 --proto '=https' --tlsv1.2 \
  -H "Accept: application/vnd.github+json" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  "$API/repos/$REPO/releases/latest" -o "$WORK/release.json" \
  || die "Cannot retrieve the latest stable release from $REPO"

# Select the actual uploaded asset from GitHub API. Do not guess a download URL.
if ! python3 - "$WORK/release.json" "$WORK/selection.json" <<'PY'
import json, re, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
tag = data.get("tag_name", "")
if not re.fullmatch(r"v\d+\.\d+\.\d+", tag):
    raise SystemExit("Latest stable release has an invalid tag")
assets = {
    a.get("name"): a for a in data.get("assets", [])
    if isinstance(a, dict) and a.get("state", "uploaded") == "uploaded"
}
names = [f"ziko-assistant-{tag}.tar.gz", f"ziko-assistant-{tag}.zip",
         f"ziko-assistant-{tag[1:]}-version-fixed.zip"]
asset = next((n for n in names if n in assets), None)
if not asset:
    raise SystemExit(
        "Release has no supported archive. Expected: " + ", ".join(names) +
        "; found: " + (", ".join(sorted(assets)) or "no uploaded assets")
    )
if "SHA256SUMS" not in assets:
    raise SystemExit("Release is missing SHA256SUMS; refusing to install an unverified archive")
json.dump({"tag": tag, "asset": asset,
           "asset_url": assets[asset]["browser_download_url"],
           "sums_url": assets["SHA256SUMS"]["browser_download_url"]},
          open(sys.argv[2], "w", encoding="utf-8"))
PY
then
  die "Release asset resolution failed"
fi

TAG="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["tag"])' "$WORK/selection.json")"
ASSET="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["asset"])' "$WORK/selection.json")"
ASSET_URL="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["asset_url"])' "$WORK/selection.json")"
SUMS_URL="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["sums_url"])' "$WORK/selection.json")"

log "latest stable release: $TAG"
log "downloading release asset: $ASSET"
curl -fsSL --retry 3 --proto '=https' --tlsv1.2 "$ASSET_URL" -o "$WORK/$ASSET" \
  || die "Release archive download failed"
curl -fsSL --retry 3 --proto '=https' --tlsv1.2 "$SUMS_URL" -o "$WORK/SHA256SUMS" \
  || die "SHA256SUMS download failed"

if ! python3 - "$WORK/$ASSET" "$WORK/SHA256SUMS" "$ASSET" "$TAG" "$WORK/project" <<'PY'
import hashlib, pathlib, re, sys, tarfile, zipfile
archive, sums = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
asset, tag, dest = sys.argv[3], sys.argv[4], pathlib.Path(sys.argv[5])
if archive.stat().st_size > 512 * 1024 * 1024:
    raise SystemExit("Release archive exceeds the 512 MiB safety limit")
matches = []
for line in sums.read_text(encoding="utf-8").splitlines():
    parts = line.split(maxsplit=1)
    if len(parts) == 2 and parts[1].strip().lstrip("*") == asset:
        matches.append(parts[0].lower())
if len(matches) != 1 or not re.fullmatch(r"[0-9a-f]{64}", matches[0]):
    raise SystemExit("SHA256SUMS has no unique valid entry for " + asset)
digest = hashlib.sha256(archive.read_bytes()).hexdigest()
if digest != matches[0]:
    raise SystemExit("SHA-256 mismatch; refusing to install")
expected = {f"ziko-assistant-{tag}"}
if asset.endswith("-version-fixed.zip"):
    expected.add(f"ziko-assistant-{tag[1:]}")
members = []
if asset.endswith(".zip"):
    with zipfile.ZipFile(archive) as zf:
        bad = zf.testzip()
        if bad: raise SystemExit("Corrupt ZIP member: " + bad)
        for info in zf.infolist():
            p = pathlib.PurePosixPath(info.filename)
            if p.is_absolute() or ".." in p.parts or "\\" in info.filename:
                raise SystemExit("Unsafe archive path: " + info.filename)
            if not info.is_dir(): members.append((info.filename, zf.read(info)))
else:
    with tarfile.open(archive, "r:gz") as tf:
        for m in tf:
            p = pathlib.PurePosixPath(m.name)
            if p.is_absolute() or ".." in p.parts or "\\" in m.name or not (m.isfile() or m.isdir()):
                raise SystemExit("Unsafe or unsupported archive entry: " + m.name)
            if m.isfile():
                f=tf.extractfile(m); members.append((m.name, f.read() if f else b""))
tops={pathlib.PurePosixPath(n).parts[0] for n,_ in members if pathlib.PurePosixPath(n).parts}
if len(tops)!=1 or next(iter(tops)) not in expected:
    raise SystemExit(f"Unexpected archive root: {sorted(tops)}; expected one of {sorted(expected)}")
top=next(iter(tops))
files={}
for name, content in members:
    rel=pathlib.PurePosixPath(name).relative_to(top)
    if not rel.parts: continue
    if rel.name == ".env" or rel.suffix in {".sqlite", ".sqlite3", ".db"} or ".sqlite3-" in rel.name:
        raise SystemExit("Release archive contains private runtime data: " + name)
    files[str(rel)]=content
required=["VERSION","install.sh","app/main.py","requirements.txt","pyproject.toml"]
missing=[n for n in required if n not in files]
if missing: raise SystemExit("Release archive is missing required files: "+", ".join(missing))
version=files["VERSION"].decode("utf-8").strip().removeprefix("v")
if version != tag.removeprefix("v"):
    raise SystemExit(f"Embedded VERSION {version!r} does not match tag {tag!r}")
dest.mkdir(mode=0o700)
for name,content in files.items():
    target=dest.joinpath(*pathlib.PurePosixPath(name).parts)
    target.parent.mkdir(parents=True,exist_ok=True)
    target.write_bytes(content)
print(f"Archive verified: {asset}; SHA256={digest}; files={len(files)}")
PY
then
  die "Release archive failed checksum or structure verification"
fi

gh attestation verify "$WORK/$ASSET" --repo "$REPO" \
  || die "GitHub Artifact Attestation verification failed; refusing to install"
log "GitHub build provenance verified for $ASSET"

[[ -f "$WORK/project/install.sh" ]] || die "Verified archive does not contain install.sh"
log "verified $TAG; starting the interactive installer"
bash "$WORK/project/install.sh"

#!/usr/bin/env bash
# Fetch the latest official GitHub Release, verify its SHA256SUMS entry, then run install.sh.
set -Eeuo pipefail
umask 077
die(){ printf '[ziko-github-install] ERROR: %s\n' "$*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || die "Root privileges are required. Run: sudo bash install-from-github.sh OWNER/REPOSITORY"
[[ $# -eq 1 ]] || die "Usage: install-from-github.sh OWNER/REPOSITORY"
REPO="$1"
[[ "$REPO" =~ ^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$ && "$REPO" != "OWNER/REPOSITORY" ]] \
  || die "Replace OWNER/REPOSITORY with the real public GitHub repository."
for cmd in curl python3 tar sha256sum grep; do command -v "$cmd" >/dev/null || die "Missing command: $cmd"; done
WORK="$(mktemp -d)"
cleanup(){ rm -rf -- "$WORK"; }
trap cleanup EXIT
curl -fsSL --retry 3 --proto '=https' --tlsv1.2 \
  "https://api.github.com/repos/${REPO}/releases/latest" -o "$WORK/release.json"
TAG="$(python3 - "$WORK/release.json" <<'PY'
import json,re,sys
data=json.load(open(sys.argv[1],encoding="utf-8"))
tag=data.get("tag_name","")
if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.]+)?",tag):
 raise SystemExit("Latest release tag is missing or invalid")
print(tag)
PY
)"
ASSET="ziko-assistant-${TAG}.tar.gz"
URL="https://github.com/${REPO}/releases/download/${TAG}"
curl -fsSL --retry 3 --proto '=https' --tlsv1.2 "$URL/$ASSET" -o "$WORK/$ASSET"
curl -fsSL --retry 3 --proto '=https' --tlsv1.2 "$URL/SHA256SUMS" -o "$WORK/SHA256SUMS"
[[ "$(grep -Ec "^[A-Fa-f0-9]{64}  ${ASSET}$" "$WORK/SHA256SUMS")" -eq 1 ]] \
  || die "SHA256SUMS does not contain exactly one checksum for $ASSET"
(cd "$WORK" && grep -E "^[A-Fa-f0-9]{64}  ${ASSET}$" SHA256SUMS | sha256sum -c --status -) \
  || die "Release asset checksum mismatch."
if tar -tzf "$WORK/$ASSET" | grep -Eq '(^/|(^|/)\.\.(/|$))'; then
  die "Release archive contains an unsafe path."
fi
mkdir "$WORK/project"
tar -xzf "$WORK/$ASSET" -C "$WORK/project" --strip-components=1
[[ -x "$WORK/project/install.sh" || -f "$WORK/project/install.sh" ]] \
  || die "Release archive does not contain install.sh."
printf '[ziko-github-install] Verified %s; starting the interactive installer.\n' "$TAG"
bash "$WORK/project/install.sh"

#!/usr/bin/env bash
# Restore a complete ZIKO platform archive (main + child-bot registry/key/databases).
set -Eeuo pipefail
umask 077
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="${ZIKO_APP_DIR:-/opt/ziko-assistant}"
ENV_FILE="${ZIKO_ENV_FILE:-/etc/ziko-assistant/ziko.env}"
BACKUP_DIR="${ZIKO_BACKUP_DIR:-/var/backups/ziko-assistant}"
SERVICE="${ZIKO_SERVICE:-ziko.service}"
APP_USER="${ZIKO_USER:-ziko}"
APP_GROUP="${ZIKO_GROUP:-ziko}"
die(){ echo "[ziko-platform-restore] ERROR: $*" >&2; exit 1; }
log(){ echo "[ziko-platform-restore] $*"; }
[[ $EUID -eq 0 ]] || die "Run as root."
[[ $# -eq 1 || ( $# -eq 2 && "$1" == "--yes" ) ]] || die "Usage: $0 [--yes] /path/to/ziko-platform-*.tar.gz.age"
if [[ $# -eq 2 ]]; then YES=1; ARCHIVE="$2"; else YES=0; ARCHIVE="$1"; fi
[[ -f "$ARCHIVE" && ! -L "$ARCHIVE" ]] || die "Archive must be a regular non-symlink file."
ARCHIVE="$(readlink -f -- "$ARCHIVE")"
[[ -f "$ARCHIVE.sha256" && ! -L "$ARCHIVE.sha256" ]] || die "SHA256 sidecar is required."
EXPECTED="$(awk 'NR==1 {print $1}' "$ARCHIVE.sha256")"
[[ "$EXPECTED" =~ ^[A-Fa-f0-9]{64}$ ]] || die "Malformed SHA256 sidecar."
ACTUAL="$(sha256sum "$ARCHIVE" | awk '{print $1}')"
[[ "${EXPECTED,,}" == "${ACTUAL,,}" ]] || die "SHA256 mismatch."
DB=""
if [[ -r "$ENV_FILE" ]]; then
  DB="$(sed -n 's/^[[:space:]]*\(export[[:space:]]\+\)\?DATABASE_PATH=\(.*\)$/\2/p' "$ENV_FILE" | tail -n1 | tr -d "\"'")"
fi
DB="${ZIKO_DB_PATH:-${DB:-/var/lib/ziko-assistant/ziko.sqlite3}}"
[[ "$DB" == /* ]] || DB="$APP_DIR/$DB"
DATA_DIR="$(dirname "$DB")"
REGISTRY_DB="$DATA_DIR/tenants.sqlite3"
TENANT_KEY="$DATA_DIR/tenant-token.key"
TENANT_DIR="$DATA_DIR/tenants"
PY="$APP_DIR/.venv/bin/python"
[[ -x "$PY" ]] || die "Application Python environment missing: $PY"
for cmd in systemctl install mv cp mkdir sha256sum find; do command -v "$cmd" >/dev/null || die "Missing command: $cmd"; done
id "$APP_USER" >/dev/null 2>&1 || die "Service user '$APP_USER' does not exist."
getent group "$APP_GROUP" >/dev/null 2>&1 || die "Service group '$APP_GROUP' does not exist."
install -d -o "$APP_USER" -g "$APP_GROUP" -m 700 "$DATA_DIR" "$BACKUP_DIR"
WORK="$(mktemp -d "$DATA_DIR/.ziko-platform-restore.XXXXXX")"
STAGE="$WORK/stage"
PREVIOUS="$WORK/previous"
mkdir -m 700 "$STAGE" "$PREVIOUS"
SWAPPED=0
WAS_ACTIVE=0
HAD_DB=0
HAD_REGISTRY=0
HAD_KEY=0
HAD_TENANTS=0
# shellcheck disable=SC2317
cleanup(){ rm -rf -- "$WORK"; }
# shellcheck disable=SC2317
rollback(){
  rc=$?
  trap - ERR INT TERM
  set +e
  if [[ "$SWAPPED" -eq 1 ]]; then
    systemctl stop "$SERVICE" >/dev/null 2>&1
    if [[ -e "$PREVIOUS/main.sqlite3" ]]; then rm -f -- "$DB"; mv -- "$PREVIOUS/main.sqlite3" "$DB"; elif [[ "$HAD_DB" -eq 0 ]]; then rm -f -- "$DB"; fi
    if [[ -e "$PREVIOUS/registry.sqlite3" ]]; then rm -f -- "$REGISTRY_DB"; mv -- "$PREVIOUS/registry.sqlite3" "$REGISTRY_DB"; elif [[ "$HAD_REGISTRY" -eq 0 ]]; then rm -f -- "$REGISTRY_DB"; fi
    if [[ -e "$PREVIOUS/tenant-token.key" ]]; then rm -f -- "$TENANT_KEY"; mv -- "$PREVIOUS/tenant-token.key" "$TENANT_KEY"; elif [[ "$HAD_KEY" -eq 0 ]]; then rm -f -- "$TENANT_KEY"; fi
    if [[ -e "$PREVIOUS/tenants" ]]; then rm -rf -- "$TENANT_DIR"; mv -- "$PREVIOUS/tenants" "$TENANT_DIR"; elif [[ "$HAD_TENANTS" -eq 0 ]]; then rm -rf -- "$TENANT_DIR"; fi
  fi
  if [[ "$WAS_ACTIVE" -eq 1 ]]; then systemctl start "$SERVICE" || echo "WARNING: previous platform data restored but service did not restart." >&2; fi
  cleanup
  exit "$rc"
}
trap rollback ERR INT TERM
trap cleanup EXIT

SOURCE_ARCHIVE="$ARCHIVE"
if [[ "$ARCHIVE" == *.age ]]; then
  command -v age >/dev/null 2>&1 || die "age is required to decrypt this platform backup."
  IDENTITY="${ZIKO_AGE_IDENTITY:-/etc/ziko-assistant/backup-age-identity}"
  [[ -f "$IDENTITY" && ! -L "$IDENTITY" ]] || die "age identity file not found or is a symlink: $IDENTITY"
  identity_owner_mode="$(stat -c '%u:%a' "$IDENTITY")"
  [[ "$identity_owner_mode" =~ ^0:(400|600)$ ]] || die "age identity must be root-owned with mode 0400 or 0600."
  DECRYPTED="$WORK/platform-backup.tar.gz"
  age -d -i "$IDENTITY" -o "$DECRYPTED" "$ARCHIVE" || die "Could not decrypt platform backup; check the age identity."
  chmod 600 "$DECRYPTED"
  SOURCE_ARCHIVE="$DECRYPTED"
else
  [[ "${ZIKO_ALLOW_LEGACY_PLAINTEXT_BACKUP:-0}" == "1" ]] || die "Plaintext platform backups are disabled. Set ZIKO_ALLOW_LEGACY_PLAINTEXT_BACKUP=1 only for a trusted legacy archive."
fi

# Extract only known regular files; never delegate untrusted archive paths to tar.
"$PY" - "$SOURCE_ARCHIVE" "$STAGE" <<'PYCODE'
import os, re, shutil, sys, tarfile
from pathlib import Path
archive, destination = sys.argv[1], Path(sys.argv[2])
allowed = re.compile(r"^(?:main\.sqlite3|registry\.sqlite3|tenant-token\.key|manifest\.json|tenants/(?:bot-)?[0-9]+\.sqlite3)$")
total = 0
with tarfile.open(archive, "r:gz") as tf:
    for member in tf.getmembers():
        name = member.name.removeprefix("./")
        if member.isdir(): name = name.rstrip("/")
        if name in {"", "."}: continue
        if member.isdir() and name == "tenants":
            (destination / "tenants").mkdir(mode=0o700, exist_ok=True)
            continue
        if not member.isfile() or not allowed.fullmatch(name):
            raise SystemExit(f"Unsafe or unsupported archive entry: {member.name}")
        total += member.size
        if total > 20 * 1024**3: raise SystemExit("Archive exceeds the 20 GiB safety limit")
        target = destination / name
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        source = tf.extractfile(member)
        if source is None: raise SystemExit(f"Cannot read archive entry: {name}")
        with source, target.open("xb") as output: shutil.copyfileobj(source, output)
        os.chmod(target, 0o600)
if not (destination / "main.sqlite3").is_file(): raise SystemExit("Platform archive does not contain main.sqlite3")
if not (destination / "manifest.json").is_file(): raise SystemExit("Platform archive has no manifest.json; refusing an unverifiable backup")
PYCODE
# Every extracted file must match the SHA-256 recorded when the archive was created.
"$PY" "$SCRIPT_DIR/verify_platform.py" manifest "$STAGE" || die "Platform archive failed manifest verification; nothing was changed."
"$PY" - "$STAGE" <<'PYCODE'
import sqlite3, sys
from pathlib import Path
from cryptography.fernet import Fernet, InvalidToken
stage = Path(sys.argv[1])
def check_db(path):
    with path.open("rb") as stream:
        if stream.read(16) != b"SQLite format 3\x00": raise SystemExit(f"Invalid SQLite header: {path.name}")
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok": raise SystemExit(f"SQLite integrity check failed: {path.name}")
        if db.execute("PRAGMA foreign_key_check").fetchall(): raise SystemExit(f"SQLite foreign-key check failed: {path.name}")
    finally: db.close()
for path in [stage / "main.sqlite3", *sorted((stage / "tenants").glob("*.sqlite3"))]: check_db(path)
registry, key_path = stage / "registry.sqlite3", stage / "tenant-token.key"
tenant_files = list((stage / "tenants").glob("*.sqlite3"))
if tenant_files and not registry.exists():
    raise SystemExit("Tenant databases are present but the tenant registry is missing")
if registry.exists():
    if not key_path.is_file(): raise SystemExit("Tenant registry exists but encryption key is missing")
    try: fernet = Fernet(key_path.read_bytes())
    except (ValueError, TypeError): raise SystemExit("Tenant encryption key is invalid") from None
    db = sqlite3.connect(f"file:{registry}?mode=ro", uri=True)
    try:
        for (encrypted,) in db.execute("SELECT encrypted_token FROM tenants").fetchall():
            try: fernet.decrypt(encrypted.encode("ascii"))
            except (InvalidToken, UnicodeEncodeError): raise SystemExit("Tenant token cannot be decrypted with the archived key") from None
    finally: db.close()
elif key_path.exists(): raise SystemExit("Archive contains a tenant key but no tenant registry")
PYCODE
if [[ "$YES" -eq 0 ]]; then
  echo "This replaces the main database and all child-bot registry/key/database files."
  read -r -p "Type RESTORE PLATFORM to continue: " answer </dev/tty
  [[ "$answer" == "RESTORE PLATFORM" ]] || die "Cancelled; live data unchanged."
fi
ZIKO_APP_DIR="$APP_DIR" ZIKO_ENV_FILE="$ENV_FILE" ZIKO_DB_PATH="$DB" ZIKO_BACKUP_DIR="$BACKUP_DIR" \
  bash "$APP_DIR/scripts/backup.sh" >/dev/null
if systemctl is-active --quiet "$SERVICE"; then WAS_ACTIVE=1; systemctl stop "$SERVICE"; fi
"$PY" - "$DB" "$REGISTRY_DB" <<'PYCODE'
import sqlite3, sys
from pathlib import Path
for raw in sys.argv[1:]:
    path = Path(raw)
    if not path.is_file(): continue
    db = sqlite3.connect(path, timeout=30)
    try:
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        db.execute("PRAGMA journal_mode=DELETE")
    finally: db.close()
PYCODE
[[ -e "$DB" ]] && HAD_DB=1
[[ -e "$REGISTRY_DB" ]] && HAD_REGISTRY=1
[[ -e "$TENANT_KEY" ]] && HAD_KEY=1
[[ -e "$TENANT_DIR" ]] && HAD_TENANTS=1
SWAPPED=1
[[ "$HAD_DB" -eq 0 ]] || mv -- "$DB" "$PREVIOUS/main.sqlite3"
rm -f -- "$DB-wal" "$DB-shm" "$DB-journal"
[[ "$HAD_REGISTRY" -eq 0 ]] || mv -- "$REGISTRY_DB" "$PREVIOUS/registry.sqlite3"
rm -f -- "$REGISTRY_DB-wal" "$REGISTRY_DB-shm" "$REGISTRY_DB-journal"
[[ "$HAD_KEY" -eq 0 ]] || mv -- "$TENANT_KEY" "$PREVIOUS/tenant-token.key"
[[ "$HAD_TENANTS" -eq 0 ]] || mv -- "$TENANT_DIR" "$PREVIOUS/tenants"
install -o "$APP_USER" -g "$APP_GROUP" -m 600 "$STAGE/main.sqlite3" "$DB"
if [[ -f "$STAGE/registry.sqlite3" ]]; then install -o "$APP_USER" -g "$APP_GROUP" -m 600 "$STAGE/registry.sqlite3" "$REGISTRY_DB"; fi
if [[ -f "$STAGE/tenant-token.key" ]]; then install -o "$APP_USER" -g "$APP_GROUP" -m 600 "$STAGE/tenant-token.key" "$TENANT_KEY"; fi
if [[ -d "$STAGE/tenants" ]]; then
  install -d -o "$APP_USER" -g "$APP_GROUP" -m 700 "$TENANT_DIR"
  while IFS= read -r -d '' file; do install -o "$APP_USER" -g "$APP_GROUP" -m 600 "$file" "$TENANT_DIR/$(basename "$file")"; done < <(find "$STAGE/tenants" -maxdepth 1 -type f -name '*.sqlite3' -print0)
fi
if [[ "$WAS_ACTIVE" -eq 1 ]]; then
  systemctl start "$SERVICE"
  for _ in {1..15}; do systemctl is-active --quiet "$SERVICE" && break; sleep 1; done
  systemctl is-active --quiet "$SERVICE" || die "Service failed to start after platform restore."
fi
SWAPPED=0
log "Platform restore completed. A verified pre-restore platform backup is retained in $BACKUP_DIR."
trap - ERR INT TERM
exit 0

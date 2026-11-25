#!/usr/bin/env bash
# SQLite online backup; consistent with WAL mode and integrity-checked before publication.
set -euo pipefail
umask 077
APP_DIR="${ZIKO_APP_DIR:-/opt/ziko-assistant}"
ENV_FILE="${ZIKO_ENV_FILE:-/etc/ziko-assistant/ziko.env}"
DEST="${1:-${ZIKO_BACKUP_DIR:-/var/backups/ziko-assistant}}"
PREFIX="${ZIKO_BACKUP_PREFIX:-ziko}"
die(){ echo "ERROR: $*" >&2; exit 1; }
[[ "$PREFIX" =~ ^[A-Za-z0-9._-]+$ ]] || die "Invalid backup prefix."
DB="${ZIKO_DB_PATH:-}"
if [[ -z "$DB" && -r "$ENV_FILE" ]]; then
 DB="$(sed -n 's/^[[:space:]]*\(export[[:space:]]\+\)\?DATABASE_PATH=\(.*\)$/\2/p' "$ENV_FILE" | tail -n1 | tr -d "\"'")"
fi
DB="${DB:-/var/lib/ziko-assistant/ziko.sqlite3}"
[[ "$DB" == /* ]] || DB="$APP_DIR/$DB"
[[ -f "$DB" ]] || die "Database not found: $DB"
PY="$APP_DIR/.venv/bin/python"
[[ -x "$PY" ]] || PY="$(command -v python3 || true)"
[[ -n "$PY" ]] || die "No Python interpreter found."
: "${ZIKO_AGE_RECIPIENT:?Set ZIKO_AGE_RECIPIENT before creating backups.}"
command -v age >/dev/null 2>&1 || die "age is required for encrypted backups (install the age package)."
install -d -m 700 "$DEST"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
# Reserve a unique basename; both the SQLite and full-platform artifacts are encrypted.
RESERVED="$(mktemp --suffix=.sqlite3 "$DEST/${PREFIX}-${STAMP}.XXXXXX")"
OUT="${RESERVED}.age"
rm -f -- "$RESERVED"
[[ ! -e "$OUT" && ! -L "$OUT" ]] || die "Encrypted backup destination already exists: $OUT"
BASE="$(basename "$OUT")"
TMP="$(mktemp "$DEST/.${BASE}.tmp.XXXXXX")"
SUMTMP="$(mktemp "$DEST/.${BASE}.sha256.XXXXXX")"
PUBLISHED=0
cleanup(){ rm -f -- "$TMP" "$SUMTMP" "$RESERVED"; [[ "$PUBLISHED" -eq 1 ]] || rm -f -- "$OUT"; }
trap cleanup EXIT
"$PY" - "$DB" "$TMP" <<'PY'
import sqlite3,sys
src=sqlite3.connect(f"file:{sys.argv[1]}?mode=rw",uri=True,timeout=30)
dst=sqlite3.connect(sys.argv[2])
try:
 src.backup(dst)
 dst.execute("PRAGMA journal_mode=DELETE")
 dst.commit()
finally:
 dst.close(); src.close()
check=sqlite3.connect(f"file:{sys.argv[2]}?mode=ro",uri=True)
try:
 if [r[0] for r in check.execute("PRAGMA integrity_check")] != ["ok"]:
  raise SystemExit("integrity_check failed")
 if check.execute("PRAGMA foreign_key_check").fetchall():
  raise SystemExit("foreign_key_check failed")
finally: check.close()
PY
chmod 600 "$TMP"
age -r "$ZIKO_AGE_RECIPIENT" -o "$OUT" "$TMP"
chmod 600 "$OUT"
rm -f -- "$TMP"
(cd "$DEST" && sha256sum "$BASE" > "$SUMTMP")
chmod 600 "$SUMTMP"
mv -- "$SUMTMP" "$OUT.sha256"
if [[ $EUID -eq 0 ]] && id -u ziko >/dev/null 2>&1; then
 chown ziko:ziko "$OUT" "$OUT.sha256"
fi
PUBLISHED=1
echo "Encrypted database backup created and verified: $OUT"
# Full platform snapshot: main DB, child-bot registry/key, and isolated tenant DBs.
DATA_DIR="$(dirname "$DB")"
REGISTRY_DB="$DATA_DIR/tenants.sqlite3"
TENANT_KEY="$DATA_DIR/tenant-token.key"
TENANT_DIR="$DATA_DIR/tenants"
PLATFORM_ARCHIVE="$DEST/ziko-platform-${STAMP}-$$.tar.gz.age"
[[ ! -e "$PLATFORM_ARCHIVE" && ! -L "$PLATFORM_ARCHIVE" && ! -e "$PLATFORM_ARCHIVE.sha256" ]] \
  || die "Platform backup destination already exists: $PLATFORM_ARCHIVE"
PLATFORM_TMP="$(mktemp -d "$DEST/.ziko-platform.XXXXXX")"
PLATFORM_PLAINTEXT="$(mktemp "$DEST/.ziko-platform-${STAMP}-$$.XXXXXX.tar.gz")"
PLATFORM_ENCRYPTED_TMP="${PLATFORM_ARCHIVE}.tmp"
PLATFORM_PUBLISHED=0
platform_cleanup() {
  rm -rf -- "$PLATFORM_TMP"
  rm -f -- "$PLATFORM_PLAINTEXT" "$PLATFORM_ENCRYPTED_TMP"
  [[ "$PLATFORM_PUBLISHED" -eq 1 ]] || rm -f -- "$PLATFORM_ARCHIVE" "$PLATFORM_ARCHIVE.sha256"
}
trap 'cleanup; platform_cleanup' EXIT
cp -- "$OUT" "$PLATFORM_TMP/main.sqlite3"
chmod 600 "$PLATFORM_TMP/main.sqlite3"
backup_sqlite() {
  local source="$1" target="$2"
  "$PY" - "$source" "$target" <<'PYCODE'
import sqlite3, sys
src = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True, timeout=30)
dst = sqlite3.connect(sys.argv[2])
try:
    src.backup(dst)
    dst.execute("PRAGMA journal_mode=DELETE")
    dst.commit()
    if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise SystemExit("SQLite integrity check failed")
    if dst.execute("PRAGMA foreign_key_check").fetchall():
        raise SystemExit("SQLite foreign-key check failed")
finally:
    dst.close()
    src.close()
PYCODE
  chmod 600 "$target"
}
if [[ -f "$REGISTRY_DB" && ! -L "$REGISTRY_DB" ]]; then
  backup_sqlite "$REGISTRY_DB" "$PLATFORM_TMP/registry.sqlite3"
fi
if [[ -f "$REGISTRY_DB" && -f "$TENANT_KEY" && ! -L "$TENANT_KEY" ]]; then
  install -m 600 "$TENANT_KEY" "$PLATFORM_TMP/tenant-token.key"
fi
if [[ -d "$TENANT_DIR" && ! -L "$TENANT_DIR" ]]; then
  [[ -f "$PLATFORM_TMP/registry.sqlite3" ]] || die "Tenant databases exist but the tenant registry is missing; platform backup refused."
  install -d -m 700 "$PLATFORM_TMP/tenants"
  while IFS= read -r -d '' tenant_db; do
    name="$(basename "$tenant_db")"
    [[ "$name" =~ ^(bot-)?[0-9]+\.sqlite3$ ]] || die "Unexpected tenant database filename: $name"
    backup_sqlite "$tenant_db" "$PLATFORM_TMP/tenants/$name"
  done < <(find "$TENANT_DIR" -maxdepth 1 -type f -name '*.sqlite3' -print0)
fi
if [[ -f "$PLATFORM_TMP/registry.sqlite3" && ! -f "$PLATFORM_TMP/tenant-token.key" ]]; then
  die "Tenant registry exists but its encryption key is missing; platform backup refused."
fi
: "${ZIKO_AGE_RECIPIENT:?Set ZIKO_AGE_RECIPIENT to an age public recipient before creating platform backups.}"
command -v age >/dev/null 2>&1 || die "age is required for encrypted platform backups (install the age package)."
tar -czf "$PLATFORM_PLAINTEXT" -C "$PLATFORM_TMP" .
chmod 600 "$PLATFORM_PLAINTEXT"
age -r "$ZIKO_AGE_RECIPIENT" -o "$PLATFORM_ENCRYPTED_TMP" "$PLATFORM_PLAINTEXT"
chmod 600 "$PLATFORM_ENCRYPTED_TMP"
mv -- "$PLATFORM_ENCRYPTED_TMP" "$PLATFORM_ARCHIVE"
rm -f -- "$PLATFORM_PLAINTEXT"
(cd "$DEST" && sha256sum "$(basename "$PLATFORM_ARCHIVE")" > "$PLATFORM_ARCHIVE.sha256")
chmod 600 "$PLATFORM_ARCHIVE.sha256"
if [[ $EUID -eq 0 ]] && id -u ziko >/dev/null 2>&1; then
  chown ziko:ziko "$PLATFORM_ARCHIVE" "$PLATFORM_ARCHIVE.sha256"
fi
PLATFORM_PUBLISHED=1
echo "Backup encryption: age"
echo "Full platform backup created and verified: $PLATFORM_ARCHIVE"

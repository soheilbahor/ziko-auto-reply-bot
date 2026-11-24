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
install -d -m 700 "$DEST"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
# mktemp atomically allocates a unique final name even for concurrent same-second runs.
RESERVED="$(mktemp --suffix=.sqlite3 "$DEST/${PREFIX}-${STAMP}.XXXXXX")"
OUT="$RESERVED"
BASE="$(basename "$OUT")"
TMP="$(mktemp "$DEST/.${BASE}.tmp.XXXXXX")"
SUMTMP="$(mktemp "$DEST/.${BASE}.sha256.XXXXXX")"
PUBLISHED=0
cleanup(){ rm -f -- "$TMP" "$SUMTMP"; [[ "$PUBLISHED" -eq 1 ]] || rm -f -- "$OUT"; }
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
mv -f -- "$TMP" "$OUT"
PUBLISHED=1
(cd "$DEST" && sha256sum "$BASE" > "$SUMTMP")
chmod 600 "$SUMTMP"
mv -- "$SUMTMP" "$OUT.sha256"
if [[ $EUID -eq 0 ]] && id -u ziko >/dev/null 2>&1; then
 chown ziko:ziko "$OUT" "$OUT.sha256"
fi
echo "Backup created and verified: $OUT"
# Full platform snapshot: main DB, child-bot registry/key, and isolated tenant DBs.
DATA_DIR="$(dirname "$DB")"
REGISTRY_DB="$DATA_DIR/tenants.sqlite3"
TENANT_KEY="$DATA_DIR/tenant-token.key"
TENANT_DIR="$DATA_DIR/tenants"
PLATFORM_TMP="$(mktemp -d "$DEST/.ziko-platform.XXXXXX")"
PLATFORM_ARCHIVE="$DEST/ziko-platform-${STAMP}-$$.tar.gz"
PLATFORM_PUBLISHED=0
platform_cleanup() {
  rm -rf -- "$PLATFORM_TMP"
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
tar -czf "$PLATFORM_ARCHIVE" -C "$PLATFORM_TMP" .
chmod 600 "$PLATFORM_ARCHIVE"
(cd "$DEST" && sha256sum "$(basename "$PLATFORM_ARCHIVE")" > "$PLATFORM_ARCHIVE.sha256")
chmod 600 "$PLATFORM_ARCHIVE.sha256"
if [[ $EUID -eq 0 ]] && id -u ziko >/dev/null 2>&1; then
  chown ziko:ziko "$PLATFORM_ARCHIVE" "$PLATFORM_ARCHIVE.sha256"
fi
PLATFORM_PUBLISHED=1
echo "Full platform backup created and verified: $PLATFORM_ARCHIVE"

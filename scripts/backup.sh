#!/usr/bin/env bash
# SQLite online backup; consistent with WAL mode and integrity-checked before publication.
set -euo pipefail
umask 077
APP_DIR="${ZIKO_APP_DIR:-/opt/ziko-assistant}"
ENV_FILE="${ZIKO_ENV_FILE:-/etc/ziko-assistant/ziko.env}"
DEST="${1:-/var/backups/ziko-assistant}"
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

#!/usr/bin/env bash
# Transactional restore. Valid SHA256 sidecar required; real application migrations run on a staged copy.
set -Eeuo pipefail
umask 077
APP_DIR="${ZIKO_APP_DIR:-/opt/ziko-assistant}"
ENV_FILE="${ZIKO_ENV_FILE:-/etc/ziko-assistant/ziko.env}"
BACKUP_DIR="${ZIKO_BACKUP_DIR:-/var/backups/ziko-assistant}"
SERVICE=ziko.service
APP_USER=ziko
APP_GROUP=ziko
die(){ echo "ERROR: $*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || die "Run as root."
YES=0
if [[ $# -eq 2 && "$1" == "--yes" ]]; then YES=1; SRC_ARG="$2"
elif [[ $# -eq 2 && "$2" == "--yes" ]]; then YES=1; SRC_ARG="$1"
elif [[ $# -eq 1 ]]; then SRC_ARG="$1"
else die "Usage: $0 [--yes] /path/to/backup.sqlite3"; fi
[[ -f "$SRC_ARG" && ! -L "$SRC_ARG" ]] || die "Backup must be a regular non-symlink file."
SRC="$(readlink -f -- "$SRC_ARG")"
id "$APP_USER" >/dev/null 2>&1 || die "Service user '$APP_USER' does not exist."
getent group "$APP_GROUP" >/dev/null 2>&1 || die "Service group '$APP_GROUP' does not exist."
[[ -f "$SRC.sha256" && ! -L "$SRC.sha256" ]] || die "SHA256 sidecar is required; refusing unmanaged/unchecked backup."
expected="$(awk 'NR==1 {print $1}' "$SRC.sha256")"
[[ "$expected" =~ ^[a-fA-F0-9]{64}$ ]] || die "Malformed SHA256 sidecar."
actual="$(sha256sum "$SRC" | awk '{print $1}')"
[[ "${expected,,}" == "${actual,,}" ]] || die "SHA256 mismatch."

DB="${ZIKO_DB_PATH:-}"
if [[ -z "$DB" && -r "$ENV_FILE" ]]; then
  DB="$(sed -n 's/^[[:space:]]*\(export[[:space:]]\+\)\?DATABASE_PATH=\(.*\)$/\2/p' "$ENV_FILE" | tail -n1 | tr -d "\"'")"
fi
DB="${DB:-/var/lib/ziko-assistant/ziko.sqlite3}"
[[ "$DB" == /* ]] || DB="$APP_DIR/$DB"
DB_DIR="$(dirname "$DB")"
install -d -o "$APP_USER" -g "$APP_GROUP" -m 700 "$DB_DIR"
PY="$APP_DIR/.venv/bin/python"
[[ -x "$PY" ]] || die "Application Python environment missing: $PY"
for c in systemctl install mv cp mktemp sha256sum; do command -v "$c" >/dev/null || die "Missing command: $c"; done

WORK="$(mktemp -d "$DB_DIR/.ziko-restore.XXXXXX")"
STAGE="$WORK/staged.sqlite3"
OLD=""
WAS_ACTIVE=0
SWAPPED=0
RESTORED=0
# shellcheck disable=SC2317
cleanup(){ rm -rf -- "$WORK"; }
# shellcheck disable=SC2317
rollback(){
 rc=$?
 trap - ERR INT TERM
 set +e
 ROLLBACK_OK=0
 if [[ "$SWAPPED" -eq 1 && "$RESTORED" -eq 0 ]]; then
   systemctl stop "$SERVICE" >/dev/null 2>&1
   rm -f -- "$DB-wal" "$DB-shm" "$DB-journal"
   if [[ -n "$OLD" && -f "$OLD" ]]; then
     local rollback_plain="$WORK/pre-restore-rollback.sqlite3"
     local identity="${ZIKO_AGE_IDENTITY:-/etc/ziko-assistant/backup-age-identity}"
     if [[ -f "$OLD.sha256" && -f "$identity" && ! -L "$identity" ]] &&
        [[ "$(stat -c '%u:%a' "$identity")" =~ ^0:(400|600)$ ]] &&
        (cd "$BACKUP_DIR" && sha256sum -c "$(basename "$OLD").sha256") &&
        age -d -i "$identity" -o "$rollback_plain" "$OLD" &&
        cp -p -- "$rollback_plain" "$DB.rollback.$$" &&
        chown "$APP_USER:$APP_GROUP" "$DB.rollback.$$" &&
        chmod 600 "$DB.rollback.$$" &&
        mv -f -- "$DB.rollback.$$" "$DB"; then
       if "$PY" - "$DB" <<'PY'
import sqlite3,sys
c=sqlite3.connect(f"file:{sys.argv[1]}?mode=ro",uri=True)
try:
 if c.execute("PRAGMA integrity_check").fetchone()[0] != "ok": raise SystemExit(1)
finally:c.close()
PY
       then
         ROLLBACK_OK=1
       fi
     else
       rm -f -- "$DB.rollback.$$" "$rollback_plain"
       echo "WARNING: encrypted pre-restore snapshot could not be verified/decrypted; service remains stopped." >&2
     fi
   else
     rm -f -- "$DB"
     ROLLBACK_OK=1
   fi
 fi
 if [[ "$SWAPPED" -eq 0 ]]; then ROLLBACK_OK=1; fi
 if [[ "$WAS_ACTIVE" -eq 1 && "$ROLLBACK_OK" -eq 1 ]]; then
   systemctl start "$SERVICE" || echo "WARNING: previous database restored but service could not be restarted." >&2
 elif [[ "$WAS_ACTIVE" -eq 1 ]]; then
   echo "WARNING: database rollback could not be verified; service left stopped." >&2
 fi
 cleanup
 exit "$rc"
}
trap rollback ERR INT TERM
trap cleanup EXIT

SOURCE_DB="$SRC"
if [[ "$SRC" == *.age ]]; then
  command -v age >/dev/null 2>&1 || die "age is required to decrypt this database backup."
  IDENTITY="${ZIKO_AGE_IDENTITY:-/etc/ziko-assistant/backup-age-identity}"
  [[ -f "$IDENTITY" && ! -L "$IDENTITY" ]] || die "age identity file not found or is a symlink: $IDENTITY"
  identity_owner_mode="$(stat -c '%u:%a' "$IDENTITY")"
  [[ "$identity_owner_mode" =~ ^0:(400|600)$ ]] || die "age identity must be root-owned with mode 0400 or 0600."
  DECRYPTED="$WORK/decrypted.sqlite3"
  age -d -i "$IDENTITY" -o "$DECRYPTED" "$SRC" || die "Could not decrypt database backup; check the age identity."
  chmod 600 "$DECRYPTED"
  SOURCE_DB="$DECRYPTED"
fi

# Validate source SQLite header, integrity and foreign keys before creating a live copy.
cp -- "$SOURCE_DB" "$STAGE"
"$PY" - "$STAGE" <<'PY'
import sqlite3,sys
p=sys.argv[1]
with open(p,"rb") as f:
 if f.read(16)!=b"SQLite format 3\x00": raise SystemExit("Invalid SQLite header")
c=sqlite3.connect(f"file:{p}?mode=rw",uri=True)
try:
 if [x[0] for x in c.execute("PRAGMA integrity_check")] != ["ok"]: raise SystemExit("integrity_check failed")
 if c.execute("PRAGMA foreign_key_check").fetchall(): raise SystemExit("foreign_key_check failed")
 required={"statuses","app_state","business_connections","processed_messages","reply_deliveries"}
 version=c.execute("PRAGMA user_version").fetchone()[0]
 tables={x[0] for x in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
 if version > 0 and required-tables:
  raise SystemExit("Missing ZIKO tables: "+", ".join(sorted(required-tables)))
finally: c.close()
PY

# Exercise the exact production migration implementation on the staged copy.
(cd "$APP_DIR" && "$PY" - "$STAGE" <<'PY'
import asyncio,sys
from pathlib import Path
from app.storage import Database
async def main():
 async with Database(Path(sys.argv[1])) as db:
  state=await db.get_app_state()
  if state.enabled and not state.session_id: raise RuntimeError("invalid enabled state")
asyncio.run(main())
import sqlite3
c=sqlite3.connect(sys.argv[1])
try:
 c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
 c.execute("PRAGMA journal_mode=DELETE")
finally: c.close()
PY
)
rm -f "$STAGE-wal" "$STAGE-shm" "$STAGE-journal"
"$PY" - "$STAGE" <<'PY'
import sqlite3,sys
c=sqlite3.connect(f"file:{sys.argv[1]}?mode=ro",uri=True)
try:
 if c.execute("PRAGMA integrity_check").fetchone()[0]!="ok": raise SystemExit("post-migration integrity failure")
 if c.execute("PRAGMA foreign_key_check").fetchall(): raise SystemExit("post-migration foreign key failure")
 required={"statuses","app_state","business_connections","processed_messages","reply_deliveries"}
 tables={x[0] for x in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
 if required-tables: raise SystemExit("Post-migration missing ZIKO tables: "+", ".join(sorted(required-tables)))
finally:c.close()
PY

echo "Validated backup and real project migration. Target: $DB"
if [[ "$YES" -eq 0 ]]; then
  read -r -p "Type RESTORE to replace the database: " answer </dev/tty
  [[ "$answer" == RESTORE ]] || die "Cancelled; live database unchanged."
else
  echo "Explicit --yes confirmation received."
fi

if systemctl is-active --quiet "$SERVICE"; then WAS_ACTIVE=1; systemctl stop "$SERVICE"; fi
# Keep a byte-for-byte rollback image of the original DB (including the online backup snapshot).
if [[ -f "$DB" ]]; then
  : "${ZIKO_AGE_RECIPIENT:?Set ZIKO_AGE_RECIPIENT before restoring; pre-restore backup is mandatory.}"
  command -v age >/dev/null 2>&1 || die "age is required for encrypted pre-restore backups."
  AGE_IDENTITY="${ZIKO_AGE_IDENTITY:-/etc/ziko-assistant/backup-age-identity}"
  [[ -f "$AGE_IDENTITY" && ! -L "$AGE_IDENTITY" ]] || die "Install the root-only age identity before restore: $AGE_IDENTITY"
  [[ "$(stat -c '%u:%a' "$AGE_IDENTITY")" =~ ^0:(400|600)$ ]] || die "age identity must be root-owned with mode 0400 or 0600."
  # Keep a durable online snapshot of the original DB; it includes committed WAL content.
  install -d -o "$APP_USER" -g "$APP_GROUP" -m 700 "$BACKUP_DIR"
  : "${ZIKO_AGE_RECIPIENT:?Set ZIKO_AGE_RECIPIENT before restoring; pre-restore backup is mandatory.}"
  command -v age >/dev/null 2>&1 || die "age is required for encrypted pre-restore backups."
  OLD="$BACKUP_DIR/ziko-pre-restore-$(date -u +%Y%m%dT%H%M%S)-$$.sqlite3.age"
  OLD_PLAIN="$WORK/pre-restore.sqlite3"
  OLD_CIPHER="$WORK/pre-restore.sqlite3.age"
  "$PY" - "$DB" "$OLD_PLAIN" <<'PY'
import sqlite3,sys
a=sqlite3.connect(f"file:{sys.argv[1]}?mode=rw",uri=True); b=sqlite3.connect(sys.argv[2])
try:
 a.backup(b)
 if b.execute("PRAGMA integrity_check").fetchone()[0]!="ok": raise SystemExit("pre-restore backup integrity failure")
finally:b.close();a.close()
PY
  chmod 600 "$OLD_PLAIN"
  age -r "$ZIKO_AGE_RECIPIENT" -o "$OLD_CIPHER" "$OLD_PLAIN"
  install -o "$APP_USER" -g "$APP_GROUP" -m 600 "$OLD_CIPHER" "$OLD"
  rm -f -- "$OLD_PLAIN" "$OLD_CIPHER"
  (cd "$BACKUP_DIR" && sha256sum "$(basename "$OLD")" > "$(basename "$OLD").sha256")
  chown "$APP_USER:$APP_GROUP" "$OLD.sha256"; chmod 600 "$OLD.sha256"
fi
chown "$APP_USER:$APP_GROUP" "$STAGE"; chmod 600 "$STAGE"
rm -f -- "$DB-wal" "$DB-shm" "$DB-journal"
mv -- "$STAGE" "$DB"
SWAPPED=1
if [[ "$WAS_ACTIVE" -eq 1 ]]; then
  systemctl start "$SERVICE"
  for _ in {1..15}; do systemctl is-active --quiet "$SERVICE" && break; sleep 1; done
  systemctl is-active --quiet "$SERVICE" || die "Service restart failed; automatic database rollback will run."
fi
RESTORED=1
echo "Restore successful. Pre-restore DB snapshot retained at ${OLD:-none}; restored backup: $SRC."
trap - ERR INT TERM
exit 0

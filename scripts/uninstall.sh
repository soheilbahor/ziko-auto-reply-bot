#!/usr/bin/env bash
# ZIKO uninstall.
#
# Always removed: the service unit, the program (/opt/ziko-assistant), CLI links,
# configuration and secrets (/etc/ziko-assistant), and backups (/var/backups/ziko-assistant).
#
# The database is deleted ONLY after two explicit typed confirmations:
#   1. the operator types the exact database path shown by this script
#   2. the operator types: DELETE DATABASE PERMANENTLY
# Deletion then removes everything ZIKO stored: the data directory (main database,
# child-bot databases, tenant registry and its key), the database file itself if it
# lives elsewhere, and the 'ziko' system user.
#
# Any other answer keeps the database. In that case the WHOLE data directory is kept,
# because the tenant registry cannot be read without its encryption key, and the
# 'ziko' user is kept because it owns that directory. Everything else is still removed.
#
# Without a terminal the database is always kept. --yes skips only the program
# confirmation; it never confirms database deletion.
#
# Usage: sudo uninstall.sh [-y]
set -euo pipefail

APP_DIR="${ZIKO_APP_DIR:-/opt/ziko-assistant}"
CONF_DIR="${ZIKO_CONF_DIR:-/etc/ziko-assistant}"
DATA_DIR="${ZIKO_DATA_DIR:-/var/lib/ziko-assistant}"
BACKUP_DIR="${ZIKO_BACKUP_DIR:-/var/backups/ziko-assistant}"
UNIT="${ZIKO_UNIT_FILE:-/etc/systemd/system/ziko.service}"
BIN_DIR="${ZIKO_BIN_DIR:-/usr/local/bin}"
SERVICE_USER="${ZIKO_USER:-ziko}"
# Input for the prompts; defaults to the terminal. Tests set this to a file.
PROMPT_IN="${ZIKO_UNINSTALL_INPUT:-/dev/tty}"
YES=0

die() { echo "ERROR: $*" >&2; exit 1; }
log() { echo "[ziko-uninstall] $*"; }

for arg in "$@"; do
  case "$arg" in
    -y|--yes) YES=1 ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) die "unknown option: $arg (see --help)" ;;
  esac
done
[[ "${EUID}" -eq 0 ]] || die "run as root (sudo)"

# Refuse to delete anything when a path is empty or the root directory.
safe_path() {
  local p="$1"
  [[ -n "$p" && "$p" != "/" && "$p" == /* ]] || die "refusing unsafe path: '$p'"
}
rm_tree() { safe_path "$1"; rm -rf -- "$1"; }

# One descriptor for the whole run, so successive answers are read in order.
HAVE_PROMPT=0
if { exec 3<"$PROMPT_IN"; } 2>/dev/null; then HAVE_PROMPT=1; fi
ask_tty() {  # ask_tty PROMPT -> answer on stdout; empty when there is no terminal
  local answer=""
  if [[ "$HAVE_PROMPT" -eq 1 ]]; then
    read -r -p "$1" -u 3 answer || answer=""
  fi
  printf '%s' "$answer"
}

# Database path from the configuration; the default matches the installer.
DB_FILE="$DATA_DIR/ziko.sqlite3"
if [[ -r "$CONF_DIR/ziko.env" ]]; then
  configured="$(grep -E '^DATABASE_PATH=' "$CONF_DIR/ziko.env" | tail -n1 | cut -d= -f2- | tr -d '\r' || true)"
  [[ -n "$configured" ]] && DB_FILE="$configured"
fi
[[ "$DB_FILE" == /* ]] || die "DATABASE_PATH must be an absolute path"

echo "ZIKO uninstall"
echo "  program, service and CLI links : $APP_DIR, $UNIT, $BIN_DIR/ziko*"
echo "  configuration and secrets      : $CONF_DIR"
echo "  backups                        : $BACKUP_DIR"
echo "  database                       : $DB_FILE"
echo "  data directory                 : $DATA_DIR"

if [[ "$YES" -ne 1 ]]; then
  answer="$(ask_tty 'Remove the program, configuration and backups? [y/N] ')"
  [[ "$answer" =~ ^[Yy]([Ee][Ss])?$ ]] || die "aborted; nothing changed"
fi

# ---- database decision -------------------------------------------------------
DB_DECISION="keep"
if [[ "$HAVE_PROMPT" -eq 1 ]]; then
  if [[ -e "$DB_FILE" ]]; then
    python3 - "$DB_FILE" "$DATA_DIR" <<'PY' || true
import glob, os, sqlite3, sys
db, data = sys.argv[1], sys.argv[2]
def describe(path):
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            tables = con.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
            version = con.execute("PRAGMA user_version").fetchone()[0]
        finally:
            con.close()
        return f"{tables} tables, schema version {version}"
    except sqlite3.Error:
        return "not a readable SQLite database"
print(f"  database file : {db}")
print(f"  size          : {os.path.getsize(db)} bytes")
print(f"  contents      : {describe(db)}")
children = sorted(glob.glob(os.path.join(data, "tenants", "*.sqlite3")))
print(f"  child-bot databases: {len(children)}")
registry = os.path.join(data, "tenants.sqlite3")
if os.path.exists(registry):
    try:
        con = sqlite3.connect(f"file:{registry}?mode=ro", uri=True)
        count = con.execute("SELECT COUNT(*) FROM tenants").fetchone()[0]
        con.close()
        print(f"  registered child bots: {count}")
    except sqlite3.Error:
        print("  registered child bots: registry unreadable")
PY
  else
    echo "  database file not found: $DB_FILE"
  fi
  echo
  echo "To DELETE the database, type its exact path shown above."
  echo "Press Enter to KEEP it."
  typed="$(ask_tty 'Database path: ')"
  if [[ -n "$typed" && "$typed" == "$DB_FILE" ]]; then
    echo "This permanently deletes all ZIKO data (databases, child-bot registry, keys) and the '$SERVICE_USER' user."
    confirm="$(ask_tty 'Type DELETE DATABASE PERMANENTLY to confirm, anything else keeps it: ')"
    [[ "$confirm" == "DELETE DATABASE PERMANENTLY" ]] && DB_DECISION="delete"
  fi
else
  echo "No terminal available: the database is kept."
fi

# ---- remove the program (always) ----------------------------------------------
if command -v systemctl >/dev/null 2>&1; then
  systemctl stop ziko.service 2>/dev/null || true
  systemctl disable ziko.service 2>/dev/null || true
fi
rm -f -- "$UNIT"
if command -v systemctl >/dev/null 2>&1; then
  systemctl daemon-reload 2>/dev/null || true
  systemctl reset-failed ziko.service 2>/dev/null || true
fi
if [[ -d "$BIN_DIR" ]]; then
  for link in "$BIN_DIR"/*; do
    [[ -L "$link" ]] || continue
    case "$(readlink "$link")" in "$APP_DIR"/.venv/bin/*) rm -f -- "$link" ;; esac
  done
fi
rm_tree "$APP_DIR"
# An external token key lives in $CONF_DIR. If the data is kept, the key must survive,
# otherwise every stored child-bot token becomes undecryptable: move it next to the data.
if [[ "$DB_DECISION" != "delete" && -f "$CONF_DIR/tenant-token.key" && ! -L "$CONF_DIR/tenant-token.key" ]]; then
  if [[ -e "$DATA_DIR/tenant-token.key" ]]; then
    if ! cmp -s "$CONF_DIR/tenant-token.key" "$DATA_DIR/tenant-token.key"; then
      kept="$DATA_DIR/tenant-token.key.from-config-$(date -u +%Y%m%dT%H%M%SZ)"
      install -m 600 "$CONF_DIR/tenant-token.key" "$kept"
      chown "$SERVICE_USER:$SERVICE_USER" "$kept" 2>/dev/null || true
      log "WARNING: two different token keys found; the external one was kept as $kept"
    fi
  else
    install -m 600 "$CONF_DIR/tenant-token.key" "$DATA_DIR/tenant-token.key"
    chown "$SERVICE_USER:$SERVICE_USER" "$DATA_DIR/tenant-token.key" 2>/dev/null || true
    log "token key moved to $DATA_DIR/tenant-token.key so the kept child-bot data stays usable"
  fi
fi
rm_tree "$CONF_DIR"
rm_tree "$BACKUP_DIR"
log "program, configuration and backups removed"

# ---- data ----------------------------------------------------------------------
if [[ "$DB_DECISION" == "delete" ]]; then
  rm_tree "$DATA_DIR"
  case "$DB_FILE" in
    "$DATA_DIR"/*) ;;   # already removed with the data directory
    *) for f in "$DB_FILE" "$DB_FILE-wal" "$DB_FILE-shm"; do rm -f -- "$f"; done ;;
  esac
  if id -u "$SERVICE_USER" >/dev/null 2>&1; then
    userdel "$SERVICE_USER" 2>/dev/null || die "could not remove user $SERVICE_USER"
  fi
  if getent group "$SERVICE_USER" >/dev/null 2>&1; then
    groupdel "$SERVICE_USER" 2>/dev/null || true
  fi
  log "database and all ZIKO data DELETED, user '$SERVICE_USER' removed"
else
  log "database KEPT: $DATA_DIR (main database, child-bot databases, registry and key)"
  log "on reinstall the key is used from the data directory; run 'ziko security move-key' to move it out again"
  log "the '$SERVICE_USER' user was kept because it owns that directory"
  log "reinstalling needs the configuration again (BOT_TOKEN, ADMIN_IDS, ...); it was removed"
fi
log "done"

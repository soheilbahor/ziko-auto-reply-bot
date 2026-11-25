#!/usr/bin/env bash
# Transactional ZIKO installer. Root only; never changes SSH/firewall or unrelated services.
set -Eeuo pipefail
umask 022

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  printf '[ziko-install] ERROR: Root privileges are required.\n' >&2
  printf '[ziko-install] Run: sudo bash install.sh\n' >&2
  exit 1
fi

APP_USER=ziko
APP_GROUP=ziko
APP_DIR=/opt/ziko-assistant
CONF_DIR=/etc/ziko-assistant
CONF_FILE="${CONF_DIR}/ziko.env"
DATA_DIR=/var/lib/ziko-assistant
DB_FILE="${DATA_DIR}/ziko.sqlite3"
BACKUP_DIR=/var/backups/ziko-assistant
UNIT_DST=/etc/systemd/system/ziko.service
CLI_LINK=/usr/local/bin/ziko
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STAMP="$(date -u +%Y%m%dT%H%M%S)-$$"
STAGE="${APP_DIR}.stage-${STAMP}"
PREVIOUS="${APP_DIR}.previous-${STAMP}"
DB_SNAPSHOT=""
DB_EXISTED=0
OLD_UNIT=""
OLD_LINK=""
HAD_UNIT=0
HAD_LINK=0
SWITCHED=0
STOPPED=0
WAS_ENABLED=unknown
START_SERVICE=1
NEW_TOKEN=""
NEW_ADMINS=""

log(){ printf '[ziko-install] %s\n' "$*"; }
die(){ printf '[ziko-install] ERROR: %s\n' "$*" >&2; exit 1; }
confirm(){ local a; read -r -p "$1 [y/N] " a </dev/tty || return 1; [[ "$a" =~ ^[Yy]([Ee][Ss])?$ ]]; }
# shellcheck disable=SC2317
cleanup(){
  [[ -n "$STAGE" && -d "$STAGE" ]] && rm -rf -- "$STAGE"
  if [[ -n "$DB_SNAPSHOT" && -f "$DB_SNAPSHOT" && ! -f "$DB_SNAPSHOT.sha256" ]]; then
    rm -f -- "$DB_SNAPSHOT"
  fi
}
# shellcheck disable=SC2317
rollback(){
  local rc=$?
  trap - ERR INT TERM
  set +e
  log "Installation failed; restoring previous state."
  if [[ "$SWITCHED" -eq 1 ]]; then
    systemctl stop ziko.service >/dev/null 2>&1
    [[ -d "$APP_DIR" ]] && mv -- "$APP_DIR" "${APP_DIR}.failed-${STAMP}"
    [[ -d "$PREVIOUS" ]] && mv -- "$PREVIOUS" "$APP_DIR"
    if [[ -n "$DB_SNAPSHOT" && -f "$DB_SNAPSHOT" ]]; then
      local rollback_plain="${DB_FILE}.rollback-${STAMP}.sqlite3"
      local age_identity="${ZIKO_AGE_IDENTITY:-/etc/ziko-assistant/backup-age-identity}"
      if [[ -f "$DB_SNAPSHOT.sha256" && -f "$age_identity" && ! -L "$age_identity" ]] &&
         [[ "$(stat -c '%u:%a' "$age_identity")" =~ ^0:(400|600)$ ]] &&
         (cd "$BACKUP_DIR" && sha256sum -c "$(basename "$DB_SNAPSHOT").sha256") &&
         age -d -i "$age_identity" -o "$rollback_plain" "$DB_SNAPSHOT"; then
        install -o "$APP_USER" -g "$APP_GROUP" -m 600 "$rollback_plain" "${DB_FILE}.rollback-${STAMP}"
        mv -f -- "${DB_FILE}.rollback-${STAMP}" "$DB_FILE"
        rm -f -- "$rollback_plain" "$DB_FILE-wal" "$DB_FILE-shm" "$DB_FILE-journal"
      else
        rm -f -- "$rollback_plain" "${DB_FILE}.rollback-${STAMP}"
        log "WARNING: encrypted pre-install database snapshot could not be verified/decrypted: $DB_SNAPSHOT"
      fi
    elif [[ "$DB_EXISTED" -eq 0 ]]; then
      rm -f -- "$DB_FILE" "$DB_FILE-wal" "$DB_FILE-shm" "$DB_FILE-journal"
    fi
    if [[ "$HAD_UNIT" -eq 1 && -n "$OLD_UNIT" && -f "$OLD_UNIT" ]]; then install -o root -g root -m 644 "$OLD_UNIT" "$UNIT_DST"; else rm -f "$UNIT_DST"; fi
    if [[ "$HAD_LINK" -eq 1 && -n "$OLD_LINK" && -L "$OLD_LINK" ]]; then ln -sfn "$(readlink "$OLD_LINK")" "$CLI_LINK"; else rm -f "$CLI_LINK"; fi
    systemctl daemon-reload
    if [[ "$WAS_ENABLED" == enabled ]]; then systemctl enable ziko.service; elif [[ "$WAS_ENABLED" == disabled ]]; then systemctl disable ziko.service; fi
  fi
  if [[ "$STOPPED" -eq 1 ]]; then systemctl start ziko.service; fi
  cleanup
  exit "$rc"
}
trap rollback ERR INT TERM
trap cleanup EXIT

for arg in "$@"; do
 case "$arg" in
  --no-start) START_SERVICE=0 ;;
  -h|--help) echo "Usage: sudo ./install.sh [--no-start]"; exit 0 ;;
  *) die "Unknown option: $arg" ;;
 esac
done
[[ -r /dev/tty ]] || die "Interactive terminal required for secure token/config confirmation."
for f in app/main.py app/storage.py requirements.txt pyproject.toml deploy/ziko.service scripts/backup.sh scripts/restore.sh scripts/restore-platform.sh; do
  [[ -f "$SRC/$f" ]] || die "Missing project file: $f"
done
. /etc/os-release
if [[ "${ID:-}" != ubuntu || "${VERSION_ID:-}" != 24.04 ]]; then
  [[ "${ZIKO_ALLOW_OTHER_OS:-0}" == 1 ]] || die "Ubuntu 24.04 required."
fi
command -v systemctl >/dev/null || die "systemd is required."
command -v python3.12 >/dev/null || die "python3.12 is required; install python3.12 and python3.12-venv first."
python3.12 -c 'import sys,venv,ensurepip; assert sys.version_info[:2] == (3,12)' || die "Python 3.12/venv unavailable."
python3.12 - <<'PY' "$SRC/pyproject.toml"
import sys,tomllib
p=tomllib.load(open(sys.argv[1],"rb"))
assert p.get("project",{}).get("scripts",{}).get("ziko") == "app.cli:main", "pyproject.toml must declare ziko = app.cli:main"
PY
[[ -f "$SRC/app/cli.py" && -f "$SRC/app/cli_menu.py" ]] || die "CLI implementation is missing."
if [[ -e "$CLI_LINK" && ! -L "$CLI_LINK" ]]; then die "$CLI_LINK exists and is not a symlink; refusing to replace it."; fi
if [[ -L "$CLI_LINK" ]]; then
  target="$(readlink "$CLI_LINK")"
  [[ "$target" == "$APP_DIR/.venv/bin/ziko" ]] || die "$CLI_LINK belongs to another installation; refusing to replace it."
fi

# Validate or create the dedicated service identity; do not repurpose an existing account.
if id "$APP_USER" >/dev/null 2>&1; then
  [[ "$(id -gn "$APP_USER")" == "$APP_GROUP" ]] || die "Existing ziko user's primary group is not ziko."
  [[ "$(id -u "$APP_USER")" -lt 1000 && "$(getent group "$APP_GROUP" | cut -d: -f3)" -lt 1000 ]] || die "Existing ziko account/group must be a system identity."
  [[ "$(getent passwd "$APP_USER" | cut -d: -f7)" == /usr/sbin/nologin ]] || die "Existing ziko user must use /usr/sbin/nologin."
else
  getent group "$APP_GROUP" >/dev/null 2>&1 && die "Group ziko exists without a ziko user; inspect it manually."
  groupadd --system "$APP_GROUP"
  useradd --system --gid "$APP_GROUP" --home-dir "$DATA_DIR" --no-create-home --shell /usr/sbin/nologin "$APP_USER"
fi

# Preserve configuration. A new token is requested only for a first installation.
KEEP_CONFIG=0
if [[ -f "$CONF_FILE" ]]; then
  confirm "Keep existing configuration at $CONF_FILE?" || die "Refusing to replace existing configuration. Edit it explicitly, then rerun."
  KEEP_CONFIG=1
else
  read -r -s -p "Telegram BOT_TOKEN (hidden): " NEW_TOKEN </dev/tty; echo
  [[ "$NEW_TOKEN" =~ ^[0-9]+:[A-Za-z0-9_-]{20,}$ ]] || die "Invalid BOT_TOKEN format."
  read -r -p "ADMIN_IDS (numeric IDs, comma/space separated): " raw_ids </dev/tty
  for idv in $(printf '%s' "$raw_ids" | tr ',;' '  '); do
    [[ "$idv" =~ ^[0-9]+$ && "$idv" -gt 0 ]] || die "Invalid ADMIN_IDS entry."
    NEW_ADMINS+="${NEW_ADMINS:+,}${idv}"
  done
  [[ -n "$NEW_ADMINS" ]] || die "ADMIN_IDS is required."
  read -r -p "BUSINESS_OWNER_ID (numeric Telegram account ID): " NEW_OWNER_ID </dev/tty
  [[ "$NEW_OWNER_ID" =~ ^[0-9]+$ && "$NEW_OWNER_ID" -gt 0 ]] || die "Invalid BUSINESS_OWNER_ID."
fi

install -d -o root -g "$APP_GROUP" -m 750 "$CONF_DIR"
install -d -o "$APP_USER" -g "$APP_GROUP" -m 700 "$DATA_DIR" "$BACKUP_DIR"
if [[ "$KEEP_CONFIG" -eq 0 ]]; then
  tmp="$(mktemp "$CONF_DIR/.ziko.env.XXXXXX")"
  printf '# ZIKO configuration; keep private.\nBOT_TOKEN=%s\nADMIN_IDS=%s\nBUSINESS_OWNER_ID=%s\nDATABASE_PATH=%s\nLOG_LEVEL=INFO\n' \
    "$NEW_TOKEN" "$NEW_ADMINS" "$NEW_OWNER_ID" "$DB_FILE" > "$tmp"
  chown "root:$APP_GROUP" "$tmp"; chmod 640 "$tmp"; mv -- "$tmp" "$CONF_FILE"
  NEW_TOKEN=""
else
  # Never rewrite existing config silently. Validate exact database location and permissions.
  python3.12 - "$CONF_FILE" "$DB_FILE" <<'PY'
import sys
from pathlib import Path
vals={}
for line in Path(sys.argv[1]).read_text().splitlines():
 s=line.strip()
 if s and not s.startswith("#") and "=" in s:
  k,v=s.split("=",1); vals[k.strip().removeprefix("export ").strip()]=v.strip().strip("\"'")
if vals.get("DATABASE_PATH") != sys.argv[2]:
 raise SystemExit("Existing DATABASE_PATH must be exactly "+sys.argv[2]+"; no config was changed.")
PY
  if ! grep -q "^BUSINESS_OWNER_ID=" "$CONF_FILE"; then
    read -r -p "BUSINESS_OWNER_ID (numeric Telegram account ID): " NEW_OWNER_ID </dev/tty
    [[ "$NEW_OWNER_ID" =~ ^[0-9]+$ && "$NEW_OWNER_ID" -gt 0 ]] || die "Invalid BUSINESS_OWNER_ID."
    printf "\nBUSINESS_OWNER_ID=%s\n" "$NEW_OWNER_ID" >> "$CONF_FILE"
  else
    grep -Eq "^BUSINESS_OWNER_ID=[1-9][0-9]*$" "$CONF_FILE" || die "Existing BUSINESS_OWNER_ID is invalid; fix it explicitly."
  fi
  [[ "$(stat -c '%U:%G:%a' "$CONF_FILE")" == "root:$APP_GROUP:640" ]] || die "Existing config ownership/mode must be root:ziko 640; fix explicitly."
fi

# Stage a complete new release on the same filesystem; the live tree is untouched.
install -d -o root -g root -m 755 "$(dirname "$APP_DIR")"
mkdir -m 700 "$STAGE"
tar -C "$SRC" --exclude='./.venv' --exclude='./venv' --exclude='./.git' --exclude='./.env' \
  --exclude='./data' --exclude='./backups' --exclude='./tests' --exclude='__pycache__' \
  --exclude='*.pyc' --exclude='*.sqlite3*' --exclude='*.egg-info' --exclude='./build' -cf - . | tar -C "$STAGE" -xf -
python3.12 - "$STAGE/pyproject.toml" <<'PY'
import sys,tomllib
p=tomllib.load(open(sys.argv[1],"rb"))
assert p["project"]["scripts"].get("ziko") == "app.cli:main"
PY
[[ -s "$STAGE/VERSION" ]] || die "VERSION file is missing from release archive"
python3.12 - "$STAGE/VERSION" <<'PYCODE'
import re,sys
value=open(sys.argv[1],encoding="utf-8").read().strip()
if not re.fullmatch(r"v?\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?", value):
    raise SystemExit("VERSION marker is missing or malformed")
PYCODE
python3.12 -m venv "$STAGE/.venv"
"$STAGE/.venv/bin/python" -m pip install --disable-pip-version-check --upgrade pip
"$STAGE/.venv/bin/python" -m pip install --disable-pip-version-check -r "$STAGE/requirements.txt"
(cd "$STAGE" && "$STAGE/.venv/bin/python" -m pip install --no-deps .)
"$STAGE/.venv/bin/python" -m compileall -q "$STAGE/app"
(cd "$STAGE" && env -i PATH=/usr/bin:/bin "$STAGE/.venv/bin/ziko" --help >/dev/null)
# Validate config with the real loader; token never appears in output.
(cd "$STAGE" && env -i PATH=/usr/bin:/bin "$STAGE/.venv/bin/python" - "$CONF_FILE" "$DB_FILE" <<'PY'
import sys
from pathlib import Path
from app.config import load_settings
s=load_settings(env_file=Path(sys.argv[1]))
assert s.database_path == Path(sys.argv[2])
PY
)

# Snapshot current DB before any service stop/start or application migration.
DB_SNAPSHOT_PLAIN=""
if [[ -f "$DB_FILE" ]]; then
  DB_EXISTED=1
  : "${ZIKO_AGE_RECIPIENT:?Set ZIKO_AGE_RECIPIENT before upgrading an existing installation.}"
  command -v age >/dev/null 2>&1 || die "age is required for encrypted pre-install backups."
  AGE_IDENTITY="${ZIKO_AGE_IDENTITY:-/etc/ziko-assistant/backup-age-identity}"
  [[ -f "$AGE_IDENTITY" && ! -L "$AGE_IDENTITY" ]] || die "Install the root-only age identity before upgrading an existing database: $AGE_IDENTITY"
  [[ "$(stat -c '%u:%a' "$AGE_IDENTITY")" =~ ^0:(400|600)$ ]] || die "age identity must be root-owned with mode 0400 or 0600."
  DB_SNAPSHOT="$BACKUP_DIR/ziko-pre-install-${STAMP}.sqlite3.age"
  DB_SNAPSHOT_PLAIN="$STAGE/preinstall.sqlite3"
  python3.12 - "$DB_FILE" "$DB_SNAPSHOT_PLAIN" <<'PY'
import sqlite3,sys
a=sqlite3.connect(sys.argv[1]); b=sqlite3.connect(sys.argv[2])
try:
 a.backup(b)
 if b.execute("PRAGMA integrity_check").fetchone()[0] != "ok": raise SystemExit("database backup integrity failed")
finally: b.close(); a.close()
PY
  chmod 600 "$DB_SNAPSHOT_PLAIN"; chown "$APP_USER:$APP_GROUP" "$DB_SNAPSHOT_PLAIN"
fi

# Check existing database migration using the staged project's REAL Database.initialize() on a copy.
if [[ -f "$DB_FILE" ]]; then
  MIGRATION_COPY="$STAGE/migration-check.sqlite3"
  cp -- "$DB_SNAPSHOT_PLAIN" "$MIGRATION_COPY"
  chown root:root "$MIGRATION_COPY"
  (cd "$STAGE" && "$STAGE/.venv/bin/python" - "$MIGRATION_COPY" <<'PY'
import asyncio,sys
from pathlib import Path
from app.storage import Database
async def main():
 async with Database(Path(sys.argv[1])) as db:
  state=await db.get_app_state()
  assert not state.enabled or state.session_id is not None
asyncio.run(main())
import sqlite3
c=sqlite3.connect(sys.argv[1])
try:
 c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
 c.execute("PRAGMA journal_mode=DELETE")
finally: c.close()
PY
  )
  rm -f "$MIGRATION_COPY" "$MIGRATION_COPY-wal" "$MIGRATION_COPY-shm" "$MIGRATION_COPY-journal"
  age -r "$ZIKO_AGE_RECIPIENT" -o "$DB_SNAPSHOT" "$DB_SNAPSHOT_PLAIN"
  chmod 600 "$DB_SNAPSHOT"; chown "$APP_USER:$APP_GROUP" "$DB_SNAPSHOT"
  rm -f -- "$DB_SNAPSHOT_PLAIN"
  (cd "$BACKUP_DIR" && sha256sum "$(basename "$DB_SNAPSHOT")" > "$(basename "$DB_SNAPSHOT").sha256")
  chown "$APP_USER:$APP_GROUP" "$DB_SNAPSHOT.sha256"; chmod 600 "$DB_SNAPSHOT.sha256"
fi
# A venv embeds its absolute creation path in console-script shebangs; never move it.
rm -rf -- "$STAGE/.venv"

# Save prior unit/link without changing them until the staged app is proven runnable.
if [[ -f "$UNIT_DST" ]]; then HAD_UNIT=1; OLD_UNIT="$BACKUP_DIR/ziko.service.pre-install-${STAMP}"; cp -p "$UNIT_DST" "$OLD_UNIT"; chmod 600 "$OLD_UNIT"; fi
if [[ -L "$CLI_LINK" ]]; then HAD_LINK=1; OLD_LINK="$BACKUP_DIR/ziko-cli.pre-install-${STAMP}"; cp -a "$CLI_LINK" "$OLD_LINK"; fi

WAS_ENABLED="$(systemctl is-enabled ziko.service 2>/dev/null || true)"
if systemctl is-active --quiet ziko.service 2>/dev/null; then STOPPED=1; systemctl stop ziko.service; fi
if [[ -d "$APP_DIR" ]]; then
  [[ ! -e "$PREVIOUS" ]] || die "Previous-version path collision."
  mv -- "$APP_DIR" "$PREVIOUS"
fi
# From here, a failed stage rename must restore PREVIOUS (if one existed).
SWITCHED=1
mv -- "$STAGE" "$APP_DIR"
chown -R --no-dereference root:root "$APP_DIR"
# Recreate the venv at its final absolute path; venv console-script shebangs are not relocatable.
python3.12 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/python" -m pip install --disable-pip-version-check --upgrade pip
"$APP_DIR/.venv/bin/python" -m pip install --disable-pip-version-check -r "$APP_DIR/requirements.txt"
(cd "$APP_DIR" && "$APP_DIR/.venv/bin/python" -m pip install --no-deps .)
"$APP_DIR/.venv/bin/python" -m compileall -q "$APP_DIR/app"
(cd "$APP_DIR" && env -i PATH=/usr/bin:/bin "$APP_DIR/.venv/bin/ziko" --help >/dev/null)
chmod 755 "$APP_DIR" "$APP_DIR/.venv/bin/ziko"
install -o root -g root -m 644 "$APP_DIR/deploy/ziko.service" "$UNIT_DST"
ln -sfn "$APP_DIR/.venv/bin/ziko" "$CLI_LINK"
systemctl daemon-reload
systemctl enable ziko.service >/dev/null
if [[ "$START_SERVICE" -eq 1 ]]; then
  systemctl restart ziko.service
  for _ in {1..15}; do systemctl is-active --quiet ziko.service && break; sleep 1; done
  systemctl is-active --quiet ziko.service || die "Service failed health check."
fi
log "ZIKO installed successfully."
log "To open management panel, run: ziko"
log "Root privileges are required."
log "Previous version preserved at: ${PREVIOUS:-none}"
log "Auto-reply remains OFF unless explicitly enabled in the application."
log "App=$APP_DIR Config=$CONF_FILE Database=$DB_FILE Backups=$BACKUP_DIR CLI=$CLI_LINK"
trap - ERR INT TERM
exit 0

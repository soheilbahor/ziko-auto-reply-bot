#!/usr/bin/env bash
# ZIKO updater. Installs a GitHub release only after every check passes,
# and rolls back to the previous release if any later stage fails.
#
# Stages, in order (each is logged as STAGE=<name>):
#   UPDATE_START        update requested; logged first so every run has a start marker
#   RESOLVE             resolve the target release
#   DOWNLOAD_VERIFY     download; check SHA-256 and GitHub build attestation
#   STAGE_VALIDATE      unpack into a private stage; check files, VERSION and Python syntax
#   BACKUP_START        encrypted full-platform backup (main DB, registry, key, child DBs)
#   BACKUP_VERIFY       check the archive's checksum, decrypt it, verify its manifest
#   DEPENDENCIES_INSTALL install the release's pinned requirements (service still running)
#   SERVICE_STOP        stop the service
#   DB_BACKUP           encrypted main-database snapshot, verified
#   CODE_SNAPSHOT       snapshot of the current code
#   DATABASE_MIGRATION  dry-run the new migrations on COPIES of every database
#   FILES_DEPLOY        replace the code and verify it against the stage byte for byte
#   UNIT_INSTALL        install the systemd unit
#   SERVICE_RESTART     start the service and watch it for HEALTH_SECONDS
#   HEALTH_CHECK        integrity, schema version, token decryption, optional getMe
#   UPDATE_COMPLETE
#
# Usage (as root):
#   sudo scripts/update.sh                       # latest stable release
#   sudo scripts/update.sh --version vX.Y.Z      # specific stable release
#   sudo scripts/update.sh --channel beta        # latest main-branch commit (unstable)
#   sudo scripts/update.sh --check               # only report versions
#   sudo scripts/update.sh --repo OWNER/REPOSITORY
#
# Required: ZIKO_AGE_RECIPIENT (public key) and an age identity at
# /etc/ziko-assistant/backup-age-identity (root-owned, mode 0400 or 0600).
# Environment overrides: ZIKO_REPO, ZIKO_SERVICE, ZIKO_USER, ZIKO_ROOT, ZIKO_HEALTH_SECONDS,
# ZIKO_KEEP_CODE_SNAPSHOTS, ZIKO_BASE_URL, ZIKO_API_URL, ZIKO_HEALTH_TELEGRAM (1 default, 0 off),
# ZIKO_UNIT_FILE, ZIKO_LOCK_FILE, ZIKO_ENV_FILE, ZIKO_AGE_IDENTITY, ZIKO_BACKUP_DIR, ZIKO_DB_PATH.
set -Eeuo pipefail
umask 077

ROOT="${ZIKO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
REPO="${ZIKO_REPO:-}"
if [[ -z "$REPO" && -r "$ROOT/deploy/release.conf" ]]; then
  # This file is maintained by the project and contains only simple KEY=VALUE entries.
  while IFS='=' read -r key value; do
    [[ "$key" == "ZIKO_REPO" ]] || continue
    value="${value%$'\r'}"
    [[ -n "$value" ]] && REPO="$value"
    break
  done < "$ROOT/deploy/release.conf"
fi
REPO="${REPO:-ziko-freedom/ziko-auto-reply-bot}"
SERVICE="${ZIKO_SERVICE:-ziko}"
SERVICE_USER="${ZIKO_USER:-ziko}"
BASE_URL="${ZIKO_BASE_URL:-https://github.com}"
HEALTH_SECONDS="${ZIKO_HEALTH_SECONDS:-15}"
KEEP_SNAPSHOTS="${ZIKO_KEEP_CODE_SNAPSHOTS:-5}"
HEALTH_TELEGRAM="${ZIKO_HEALTH_TELEGRAM:-1}"
ASSET_PREFIX="ziko-assistant"
# Top-level entries of $ROOT that an update never touches.
PRESERVE=(.env data .venv backups)

ENV_FILE="${ZIKO_ENV_FILE:-/etc/ziko-assistant/ziko.env}"
AGE_IDENTITY="${ZIKO_AGE_IDENTITY:-/etc/ziko-assistant/backup-age-identity}"
UNIT_FILE="${ZIKO_UNIT_FILE:-/etc/systemd/system/ziko.service}"
LOCK_FILE="${ZIKO_LOCK_FILE:-/run/lock/ziko-update.lock}"
BACKUP_DIR="${ZIKO_BACKUP_DIR:-/var/backups/ziko-assistant}"
DB_PATH="${ZIKO_DB_PATH:-/var/lib/ziko-assistant/ziko.sqlite3}"

TAG=""
CHANNEL="stable"
CHECK_ONLY=0
WORK=""
STAGE_NAME="INIT"
DB_BACKUP=""
DB_PLAIN=""      # plaintext pre-update snapshot; cleanup() always removes it
PLATFORM_BACKUP=""
CODE_SNAPSHOT=""
UNIT_SNAPSHOT=""
HAD_UNIT=0
STOPPED=0
REPLACED=0
DEPS_CHANGED=0
STARTED_NEW=0
ROLLING_BACK=0

log()  { printf '[ziko-update] %s\n' "$*"; }
stage(){ STAGE_NAME="$1"; log "STAGE=$1${2:+ $2}"; }
warn() { printf '[ziko-update] WARNING (stage %s): %s\n' "$STAGE_NAME" "$*" >&2; }
die() {
  printf '[ziko-update] ERROR (stage %s): %s\n' "$STAGE_NAME" "$*" >&2
  if needs_rollback; then rollback; fi   # rollback() exits
  exit 1
}
usage() { sed -n '2,33p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

# A failure needs rollback once anything has changed: the service was stopped, or the
# Python environment was modified (the old requirements must be restored).
needs_rollback() { [[ "$STOPPED" -eq 1 || "$DEPS_CHANGED" -eq 1 ]]; }

is_preserved() {
  local name="$1" p
  for p in "${PRESERVE[@]}"; do [[ "$name" == "$p" ]] && return 0; done
  return 1
}

# Remove every non-preserved top-level entry of $ROOT.
clear_code() {
  local entry name
  shopt -s dotglob nullglob
  for entry in "$ROOT"/*; do
    name="$(basename "$entry")"
    is_preserved "$name" || rm -rf -- "$entry"
  done
  shopt -u dotglob nullglob
}

run_as_service_user() { runuser -u "$SERVICE_USER" -- "$@"; }

# Install the requirements of the release in $1 (a stage or the installed tree) into .venv.
# The package is built from a private copy so the source tree never gains build output.
install_requirements() {
  local src="$1" pkg
  pkg="$(mktemp -d "$WORK/pkgsrc.XXXXXX")"
  cp -a "$src/app" "$src/pyproject.toml" "$src/requirements.txt" "$src/VERSION" "$pkg/"
  [[ -f "$src/README.md" ]] && cp -a "$src/README.md" "$pkg/"
  (
    umask 022
    "$ROOT/.venv/bin/python" -m pip install --disable-pip-version-check \
      -q -r "$pkg/requirements.txt"
    "$ROOT/.venv/bin/python" -m pip install --disable-pip-version-check -q "$pkg"
  )
  local site_packages
  site_packages="$("$ROOT/.venv/bin/python" -c \
    'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
  chmod -R a+rX "$site_packages"
  if ! run_as_service_user "$ROOT/.venv/bin/python" \
      -c 'from cryptography.fernet import Fernet' >/dev/null 2>&1; then
    (
      umask 022
      "$ROOT/.venv/bin/python" -m pip install --disable-pip-version-check \
        --force-reinstall --no-cache-dir "cryptography>=46.0.7,<51"
    )
    chmod -R a+rX "$site_packages"
  fi
  run_as_service_user "$ROOT/.venv/bin/python" -c 'from cryptography.fernet import Fernet'
}

# Check a platform archive before it is trusted: checksum, decryption, allowed entries,
# and the manifest (every file's SHA-256).
verify_platform_archive() {
  local archive="$1" check="$WORK/verify-archive"
  [[ -f "$archive.sha256" && ! -L "$archive.sha256" ]] || die "backup checksum file is missing: $archive.sha256"
  (cd "$(dirname "$archive")" && sha256sum -c --status "$(basename "$archive").sha256") \
    || die "platform backup checksum mismatch: $archive"
  rm -rf -- "$check"; install -d -m 700 "$check"
  age -d -i "$AGE_IDENTITY" -o "$check.tar.gz" "$archive" \
    || die "platform backup cannot be decrypted with the age identity"
  if tar -tzf "$check.tar.gz" | grep -Ev '^\./?$|^\./(main\.sqlite3|registry\.sqlite3|tenant-token\.key|manifest\.json|tenants/?|tenants/(bot-)?[0-9]+\.sqlite3)$' | grep -q .; then
    die "platform backup contains unexpected entries"
  fi
  tar -xzf "$check.tar.gz" -C "$check" || die "platform backup cannot be extracted"
  rm -f -- "$check.tar.gz"
  "$ROOT/.venv/bin/python" "$WORK/stage/scripts/verify_platform.py" manifest "$check" \
    || die "platform backup failed manifest verification"
  rm -rf -- "$check"
  log "platform backup verified: $archive"
}

# Check the encrypted main-database snapshot: checksum, decryption and integrity.
verify_db_snapshot() {
  local snapshot="$1" plain="$WORK/dbcheck.sqlite3"
  [[ -f "$snapshot.sha256" ]] || die "database snapshot checksum is missing"
  (cd "$(dirname "$snapshot")" && sha256sum -c --status "$(basename "$snapshot").sha256") \
    || die "database snapshot checksum mismatch"
  age -d -i "$AGE_IDENTITY" -o "$plain" "$snapshot" || die "database snapshot cannot be decrypted"
  "$ROOT/.venv/bin/python" - "$plain" <<'PY' || die "database snapshot failed integrity_check"
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
try:
    raise SystemExit(0 if conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok" else 1)
finally:
    conn.close()
PY
  rm -f -- "$plain"
}

# Copy a live SQLite file with the online backup API (consistent even under WAL).
copy_sqlite() {
  local source="$1" target="$2"
  "$ROOT/.venv/bin/python" - "$source" "$target" <<'PY'
import sqlite3, sys
src = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True, timeout=30)
dst = sqlite3.connect(sys.argv[2])
try:
    src.backup(dst)
finally:
    dst.close()
    src.close()
PY
}

# Dry-run the staged release's migrations on copies of every database. The live files are
# never opened for writing here, so a migration failure leaves the installation untouched.
dry_run_migrations() {
  local copies="$WORK/migrate" data_dir db name
  data_dir="$(dirname "$DB_PATH")"
  install -d -m 700 "$copies"
  check_one() {
    PYTHONPATH="$WORK/stage" PYTHONDONTWRITEBYTECODE=1 \
      "$ROOT/.venv/bin/python" "$WORK/stage/scripts/migrate_check.py" "$1" >/dev/null \
      || die "database migration dry-run failed for $(basename "$2"); the live installation is unchanged"
    log "migration dry-run OK: $(basename "$2")"
  }
  if [[ -f "$DB_PATH" ]]; then
    copy_sqlite "$DB_PATH" "$copies/main.sqlite3"
    check_one "$copies/main.sqlite3" "$DB_PATH"
  fi
  shopt -s nullglob
  for db in "$data_dir"/tenants/*.sqlite3; do
    name="$(basename "$db")"
    copy_sqlite "$db" "$copies/tenant-$name"
    check_one "$copies/tenant-$name" "$db"
  done
  shopt -u nullglob
}

curl_get() {
  local proto='=https'
  [[ "$BASE_URL" == https://* ]] || proto='=file,https'   # non-https only for tests
  curl -fsSL --retry 3 --proto "$proto" --tlsv1.2 "$@"
}

resolve_latest_tag() {
  local url
  url="$(curl_get -I -o /dev/null -w '%{url_effective}' "$BASE_URL/$REPO/releases/latest")" \
    || die "cannot resolve the latest stable release of $REPO"
  printf '%s' "${url##*/}"
}

resolve_latest_beta_sha() {
  local api_url="${ZIKO_API_URL:-https://api.github.com}"
  curl_get -H "Accept: application/vnd.github+json" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    "$api_url/repos/$REPO/commits/main" -o "$WORK/main-commit.json" \
    || die "cannot query the latest main-branch commit for beta"
  python3 - "$WORK/main-commit.json" <<'PYCODE'
import json, re, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
sha = d.get("sha", "")
if not re.fullmatch(r"[0-9a-fA-F]{40}", sha):
    raise SystemExit("GitHub main branch returned an invalid commit SHA")
print(sha.lower())
PYCODE
}

rollback() {
  [[ "$ROLLING_BACK" -eq 0 ]] || return 0
  ROLLING_BACK=1
  trap - ERR INT TERM
  set +e
  local failed_stage="$STAGE_NAME"
  printf '[ziko-update] ROLLBACK: failed in stage %s; restoring the previous release\n' "$failed_stage" >&2
  systemctl stop "$SERVICE" 2>/dev/null
  if [[ "$REPLACED" -eq 1 && -f "$CODE_SNAPSHOT" ]]; then
    clear_code
    tar -xzf "$CODE_SNAPSHOT" -C "$ROOT" || warn "code restore failed; snapshot: $CODE_SNAPSHOT"
  fi
  if [[ "$DEPS_CHANGED" -eq 1 ]]; then
    install_requirements "$ROOT" || warn "could not reinstall the previous requirements"
  fi
  if [[ "$HAD_UNIT" -eq 1 && -f "$UNIT_SNAPSHOT" ]]; then
    install -o root -g root -m 644 "$UNIT_SNAPSHOT" "$UNIT_FILE" || warn "systemd unit rollback failed"
    systemctl daemon-reload || warn "systemd daemon-reload failed during rollback"
  fi
  local platform_restored=0
  if [[ "$STARTED_NEW" -eq 1 && -n "$PLATFORM_BACKUP" && -f "$PLATFORM_BACKUP" \
        && -f "$WORK/stage/scripts/restore-platform.sh" ]]; then
    # The verified platform archive restores the main DB, registry, key and child DBs together.
    if ZIKO_APP_DIR="$ROOT" ZIKO_ENV_FILE="$ENV_FILE" \
      ZIKO_DB_PATH="$DB_PATH" ZIKO_BACKUP_DIR="$BACKUP_DIR" ZIKO_AGE_IDENTITY="$AGE_IDENTITY" \
      bash "$WORK/stage/scripts/restore-platform.sh" --yes "$PLATFORM_BACKUP"; then
      platform_restored=1
    else
      warn "full platform rollback failed; inspect $BACKUP_DIR"
    fi
  fi
  if [[ "$STARTED_NEW" -eq 1 && "$platform_restored" -eq 0 && -n "$DB_BACKUP" && -f "$DB_BACKUP" ]]; then
    local db="$DB_PATH" rollback_db="$WORK/rollback.sqlite3"
    rm -f -- "$db-wal" "$db-shm"
    install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 700 "$(dirname "$db")"
    if [[ -f "$DB_BACKUP.sha256" && -f "$AGE_IDENTITY" && ! -L "$AGE_IDENTITY" ]] \
       && [[ "$(stat -c '%u:%a' "$AGE_IDENTITY")" =~ ^0:(400|600)$ ]] \
       && (cd "$(dirname "$DB_BACKUP")" && sha256sum -c "$(basename "$DB_BACKUP").sha256") \
       && age -d -i "$AGE_IDENTITY" -o "$rollback_db" "$DB_BACKUP"; then
      install -o "$SERVICE_USER" -g "$SERVICE_USER" -m 600 "$rollback_db" "$db" \
        || warn "database restore failed; backup: $DB_BACKUP"
    else
      warn "encrypted database rollback could not be verified or decrypted; backup: $DB_BACKUP"
    fi
  fi
  if [[ "$STOPPED" -eq 1 ]]; then
    systemctl start "$SERVICE" || warn "could not start $SERVICE after rollback"
  fi
  warn "rollback finished; check: systemctl status $SERVICE ; journalctl -u $SERVICE"
  [[ -n "$DB_BACKUP" ]] && warn "pre-update database backup: $DB_BACKUP"
  [[ -n "$PLATFORM_BACKUP" ]] && warn "pre-update platform backup: $PLATFORM_BACKUP"
  exit 1
}

# shellcheck disable=SC2317
cleanup() {
  # A plaintext database copy must never outlive the update, on success or failure.
  if [[ -n "$DB_PLAIN" && -f "$DB_PLAIN" ]]; then rm -f -- "$DB_PLAIN"; fi
  [[ -n "$WORK" && -d "$WORK" ]] && rm -rf -- "$WORK"
  if [[ -n "$DB_BACKUP" && -f "$DB_BACKUP" && ! -f "$DB_BACKUP.sha256" ]]; then
    rm -f -- "$DB_BACKUP"
  fi
}

# shellcheck disable=SC2317
on_error() {
  local code=$? line="${1:-?}"
  printf '[ziko-update] ERROR (stage %s): command failed (exit %s) at line %s\n' "$STAGE_NAME" "$code" "$line" >&2
  if needs_rollback; then rollback; fi
  exit "$code"
}

main() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --version) [[ $# -ge 2 ]] || die "--version needs a value"; TAG="$2"; shift 2 ;;
      --repo)    [[ $# -ge 2 ]] || die "--repo needs a value"; REPO="$2"; shift 2 ;;
      --channel) [[ $# -ge 2 ]] || die "--channel needs a value"; CHANNEL="$2"; shift 2 ;;
      --check)   CHECK_ONLY=1; shift ;;
      -h|--help) usage; exit 0 ;;
      *) die "unknown argument: $1 (see --help)" ;;
    esac
  done

  [[ "$EUID" -eq 0 ]] || die "run as root (sudo)"
  [[ "$REPO" =~ ^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$ ]] || die "invalid repository: $REPO"
  [[ "$CHANNEL" == "stable" || "$CHANNEL" == "beta" || "$CHANNEL" == "dev" ]] || die "channel must be stable or beta"
  [[ "$CHANNEL" == "dev" ]] && CHANNEL="beta"
  [[ "$REPO" != "OWNER/REPOSITORY" ]] || die "set the real repository: --repo <owner>/<repo> or ZIKO_REPO"
  local cmd
  for cmd in curl tar sha256sum systemctl runuser flock install age; do
    command -v "$cmd" >/dev/null || die "missing command: $cmd"
  done
  id "$SERVICE_USER" >/dev/null 2>&1 || die "service user '$SERVICE_USER' does not exist"
  [[ -x "$ROOT/.venv/bin/python" && -x "$ROOT/.venv/bin/pip" ]] || die "virtualenv not found at $ROOT/.venv"
  "$ROOT/.venv/bin/python" -c 'import sys; assert sys.version_info[:2] == (3,12)' \
    || die "existing virtualenv must use Python 3.12"
  [[ -f "$UNIT_FILE" ]] || die "systemd unit not found at $UNIT_FILE"

  install -d -m 755 "$(dirname "$LOCK_FILE")"
  exec 9>"$LOCK_FILE"
  flock -n 9 || die "another update is running"

  WORK="$(mktemp -d)"
  trap cleanup EXIT

  stage UPDATE_START
  stage RESOLVE
  local BETA_SHA=""
  if [[ "$CHANNEL" == "beta" ]]; then
    [[ -z "$TAG" ]] || die "--version is only supported for stable releases; choose --channel stable"
    BETA_SHA="$(resolve_latest_beta_sha)"
    TAG="dev-${BETA_SHA:0:12}"
  else
    [[ -n "$TAG" ]] || TAG="$(resolve_latest_tag)"
    [[ "$TAG" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "invalid stable release tag: $TAG"
  fi
  local current="unknown"
  [[ -f "$ROOT/VERSION" ]] && current="$(tr -d '[:space:]' < "$ROOT/VERSION")"
  current="${current#v}"
  local target_version="${TAG#v}"
  log "installed: $current   target: $target_version   channel: $CHANNEL"
  if [[ "$CHECK_ONLY" -eq 1 ]]; then exit 0; fi
  if [[ "$current" == "$target_version" ]]; then log "already up to date"; exit 0; fi

  local asset="$ASSET_PREFIX-$TAG.tar.gz"
  local rel_url="$BASE_URL/$REPO/releases/download/$TAG"
  local api_url="${ZIKO_API_URL:-https://api.github.com}"
  # Helper from the INSTALLED tree (never from the download). Stable releases only.
  local helper="$ROOT/scripts/release_asset.py" py="$ROOT/.venv/bin/python"

  # ---- download and verify (nothing on this machine is changed yet) ----
  stage DOWNLOAD_VERIFY
  if [[ "$CHANNEL" == "beta" ]]; then
    log "downloading latest main-branch snapshot ($BETA_SHA)"
    curl_get -H "Accept: application/vnd.github+json" \
      -H "X-GitHub-Api-Version: 2022-11-28" \
      -o "$WORK/$asset" "$api_url/repos/$REPO/tarball/$BETA_SHA"
    printf '%s  %s\n' "$(sha256sum "$WORK/$asset" | awk '{print $1}')" "$asset" > "$WORK/SHA256SUMS"
    log "beta snapshot downloaded over verified HTTPS; pinned commit: $BETA_SHA"
  else
    [[ -f "$helper" ]] || die "release helper is missing from the installed tree: $helper"
    # The asset name, format and download URLs come from the GitHub Release API,
    # never from a guessed file name. A release without a supported archive stops here.
    local resolved
    resolved="$("$py" "$helper" resolve --api "$api_url" --repo "$REPO" --tag "$TAG")" \
      || die "cannot use release $TAG (see the error above)"
    asset="$("$py" -c 'import json,sys; print(json.load(sys.stdin)["asset"])' <<<"$resolved")"
    local asset_url sums_url
    asset_url="$("$py" -c 'import json,sys; print(json.load(sys.stdin)["asset_url"])' <<<"$resolved")"
    sums_url="$("$py" -c 'import json,sys; print(json.load(sys.stdin)["sums_url"])' <<<"$resolved")"
    log "downloading $asset"
    curl_get -o "$WORK/$asset" "$asset_url"
    curl_get -o "$WORK/SHA256SUMS" "$sums_url"
    # Checksum, layout, forbidden files and the embedded VERSION are all checked here.
    "$py" "$helper" verify --file "$WORK/$asset" --sums "$WORK/SHA256SUMS" \
      --asset "$asset" --tag "$TAG" || die "release archive failed verification - refusing to install"
    log "SHA-256, layout and embedded VERSION verified"
    command -v gh >/dev/null 2>&1 || die "GitHub CLI (gh) is required to verify release provenance; install gh and retry."
    gh attestation verify "$WORK/$asset" --repo "$REPO" \
      || die "GitHub artifact attestation verification failed - refusing to install"
    log "GitHub build provenance attestation verified"
  fi

  # ---- stage: unpack privately and validate before touching anything ----
  stage STAGE_VALIDATE
  mkdir "$WORK/stage"
  if [[ "$CHANNEL" == "stable" ]]; then
    "$py" "$helper" extract --file "$WORK/$asset" --sums "$WORK/SHA256SUMS" \
      --asset "$asset" --tag "$TAG" --dest "$WORK/stage" \
      || die "release archive could not be extracted safely"
  else
    if tar -tzf "$WORK/$asset" | grep -Eq '(^/|(^|/)\.\.(/|$))'; then
      die "archive contains unsafe paths"
    fi
    tar -xzf "$WORK/$asset" -C "$WORK/stage" --strip-components=1
  fi
  local f
  for f in app/main.py app/storage.py requirements.txt pyproject.toml scripts/update.sh \
           scripts/backup.sh scripts/restore-platform.sh scripts/verify_platform.py \
           scripts/migrate_check.py deploy/ziko.service; do
    [[ -f "$WORK/stage/$f" ]] || die "release is missing $f"
  done
  if [[ -n "$(find "$WORK/stage" \( -name '.env' -o -name '*.sqlite*' -o -name '*.db' \) -print -quit)" ]]; then
    die "release contains .env or database files - refusing to install"
  fi
  if [[ "$CHANNEL" == "stable" ]]; then
    [[ -s "$WORK/stage/VERSION" ]] || die "release archive is missing VERSION"
    local archive_version
    archive_version="$(tr -d '[:space:]' < "$WORK/stage/VERSION")"
    archive_version="${archive_version#v}"
    [[ "$archive_version" == "$target_version" ]] \
      || die "release archive VERSION ($archive_version) does not match tag ($target_version)"
  else
    printf '%s\n' "$TAG" > "$WORK/stage/VERSION"
  fi
  if ! PYTHONDONTWRITEBYTECODE=1 "$ROOT/.venv/bin/python" - "$WORK/stage/app" <<'PY'
import ast, pathlib, sys
for path in pathlib.Path(sys.argv[1]).rglob("*.py"):
    ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
PY
  then
    die "staged release has Python syntax errors"
  fi
  local expected_schema
  expected_schema="$(sed -n 's/^SCHEMA_VERSION = \([0-9][0-9]*\)$/\1/p' "$WORK/stage/app/storage.py" | head -n1)"
  [[ -n "$expected_schema" ]] || die "cannot read SCHEMA_VERSION from the staged release"
  log "staged release validated; database schema $expected_schema"

  # ---- from here on a failure triggers rollback ----
  trap 'on_error $LINENO' ERR
  trap 'warn "interrupted"; if needs_rollback; then rollback; fi; exit 130' INT TERM

  local stamp; stamp="$(date -u +%Y%m%dT%H%M%S)-$$"
  install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 700 "$BACKUP_DIR" "$(dirname "$DB_PATH")"

  # ---- platform backup (mandatory) ----
  stage BACKUP_START "encrypted full-platform backup"
  [[ -n "${ZIKO_AGE_RECIPIENT:-}" ]] || die "Set ZIKO_AGE_RECIPIENT before updating; encrypted platform backup is mandatory."
  [[ -f "$AGE_IDENTITY" && ! -L "$AGE_IDENTITY" ]] || die "Install the root-only age identity before updating: $AGE_IDENTITY"
  [[ "$(stat -c '%u:%a' "$AGE_IDENTITY")" =~ ^0:(400|600)$ ]] || die "age identity must be root-owned with mode 0400 or 0600."
  local backup_output
  backup_output="$(run_as_service_user env ZIKO_APP_DIR="$ROOT" \
    ZIKO_ENV_FILE="$ENV_FILE" ZIKO_DB_PATH="$DB_PATH" ZIKO_BACKUP_DIR="$BACKUP_DIR" \
    ZIKO_AGE_RECIPIENT="$ZIKO_AGE_RECIPIENT" \
    bash "$WORK/stage/scripts/backup.sh" "$BACKUP_DIR")" \
    || die "full-platform pre-update backup failed; update cancelled"
  printf '%s\n' "$backup_output"
  PLATFORM_BACKUP="$(printf '%s\n' "$backup_output" | \
    sed -n 's/^Full platform backup created and verified: //p' | tail -n1)"
  [[ -n "$PLATFORM_BACKUP" && -f "$PLATFORM_BACKUP.sha256" ]] \
    || die "full-platform backup was not published; update cancelled"

  stage BACKUP_VERIFY "platform archive"
  verify_platform_archive "$PLATFORM_BACKUP"

  # ---- dependencies: installed while the service still runs, reverted on failure ----
  stage DEPENDENCIES_INSTALL "from the staged requirements.txt"
  DEPS_CHANGED=1
  install_requirements "$WORK/stage"
  log "requirements installed"

  # ---- stop, snapshot, migrate on copies, deploy ----
  stage SERVICE_STOP
  systemctl stop "$SERVICE"
  STOPPED=1

  stage DB_BACKUP "encrypted main-database snapshot"
  if [[ -f "$DB_PATH" ]]; then
    DB_PLAIN="$BACKUP_DIR/.ziko-pre-update-$stamp.sqlite3"
    DB_BACKUP="$BACKUP_DIR/ziko-pre-update-$stamp.sqlite3.age"
    run_as_service_user "$ROOT/.venv/bin/python" - "$DB_PATH" "$DB_PLAIN" <<'PY'
import sqlite3, sys
src = sqlite3.connect(sys.argv[1]); dst = sqlite3.connect(sys.argv[2])
try:
    src.backup(dst)
    if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise SystemExit("backup integrity check failed")
finally:
    dst.close(); src.close()
PY
    chmod 600 "$DB_PLAIN"
    run_as_service_user age -r "$ZIKO_AGE_RECIPIENT" -o "$DB_BACKUP" "$DB_PLAIN"
    rm -f -- "$DB_PLAIN"
    chmod 600 "$DB_BACKUP"
    (cd "$BACKUP_DIR" && sha256sum "$(basename "$DB_BACKUP")" > "$(basename "$DB_BACKUP").sha256")
    chown "$SERVICE_USER:$SERVICE_USER" "$DB_BACKUP.sha256"
    chmod 600 "$DB_BACKUP.sha256"
    stage BACKUP_VERIFY "database snapshot"
    verify_db_snapshot "$DB_BACKUP"
    log "encrypted database snapshot: $DB_BACKUP"
  else
    warn "no database found at $DB_PATH - skipping database snapshot"
  fi

  stage CODE_SNAPSHOT
  CODE_SNAPSHOT="$BACKUP_DIR/code-${current}-$stamp.tar.gz"
  tar -czf "$CODE_SNAPSHOT" -C "$ROOT" \
    --exclude=./.env --exclude=./data --exclude=./.venv --exclude=./backups .
  chmod 600 "$CODE_SNAPSHOT"

  stage DATABASE_MIGRATION "dry-run on copies"
  dry_run_migrations

  stage FILES_DEPLOY "$TAG"
  REPLACED=1
  clear_code
  local entry name
  shopt -s dotglob nullglob
  for entry in "$WORK/stage"/*; do   # entry by entry: never touch $ROOT's own mode/owner
    name="$(basename "$entry")"
    cp -a "$entry" "$ROOT/$name"
    chown -R root:root "$ROOT/$name"
  done
  shopt -u dotglob nullglob
  # The installed tree must equal the verified stage (preserved entries are excluded).
  if ! diff -rq -x .venv -x .env -x data -x backups "$WORK/stage" "$ROOT" >"$WORK/deploy.diff" 2>&1; then
    head -n 20 "$WORK/deploy.diff" >&2
    die "installed files differ from the verified release"
  fi
  log "installed tree matches the verified release"

  stage UNIT_INSTALL
  HAD_UNIT=1
  UNIT_SNAPSHOT="$BACKUP_DIR/ziko.service.pre-update-$stamp"
  cp -p -- "$UNIT_FILE" "$UNIT_SNAPSHOT"
  chmod 600 "$UNIT_SNAPSHOT"
  install -o root -g root -m 644 "$ROOT/deploy/ziko.service" "$UNIT_FILE"
  systemctl daemon-reload
  if [[ -f "$ENV_FILE" && -f "$ROOT/.env.example" ]]; then
    local key
    while IFS= read -r key; do
      grep -q "^${key}=" "$ENV_FILE" || warn "new setting $key is in .env.example but not in $ENV_FILE"
    done < <(grep -oE '^[A-Z_][A-Z0-9_]*' "$ROOT/.env.example")
  fi

  stage SERVICE_RESTART
  STARTED_NEW=1
  systemctl restart "$SERVICE"
  local i
  for ((i = 0; i < HEALTH_SECONDS; i++)); do
    sleep 1
    systemctl is-active --quiet "$SERVICE" || die "$SERVICE is not active after update"
  done
  [[ "$(systemctl show -p NRestarts --value "$SERVICE")" == "0" ]] \
    || die "$SERVICE restarted during the health window"

  # The running code must report the release it was installed from. Without this,
  # a stale package metadata or a partial install could leave the old version visible.
  if [[ "$CHANNEL" == "stable" ]]; then
    local runtime_version expected_runtime="${TAG#v}"
    runtime_version="$(cd "$ROOT" && PYTHONDONTWRITEBYTECODE=1 ZIKO_HOME="$ROOT" \
      "$ROOT/.venv/bin/python" -c 'import app; print(app.__version__)')" \
      || die "cannot read the running ZIKO version"
    [[ "$runtime_version" == "$expected_runtime" ]] \
      || die "running version ($runtime_version) does not match release ($expected_runtime)"
  fi

  stage HEALTH_CHECK "integrity, schema $expected_schema, tokens$([[ "$HEALTH_TELEGRAM" == 1 ]] && echo ', getMe')"
  local health_args=(health --data-dir "$(dirname "$DB_PATH")" --expected-schema "$expected_schema" --env-file "$ENV_FILE")
  [[ "$HEALTH_TELEGRAM" == 1 ]] && health_args+=(--telegram)
  "$ROOT/.venv/bin/python" "$ROOT/scripts/verify_platform.py" "${health_args[@]}" \
    || die "post-update health check failed"

  # Success: drop disarmed traps, prune old code snapshots (DB backups are kept).
  trap - ERR INT TERM
  # shellcheck disable=SC2012
  ls -1t "$BACKUP_DIR"/code-*.tar.gz 2>/dev/null | tail -n +"$((KEEP_SNAPSHOTS + 1))" \
    | while IFS= read -r old; do rm -f -- "$old"; done
  stage UPDATE_COMPLETE "updated to $TAG"
  log "pre-update DB snapshot: ${DB_BACKUP:-none}; platform backup: $PLATFORM_BACKUP"
}

main "$@"
exit 0

#!/usr/bin/env bash
# ZIKO updater: downloads a GitHub release, verifies its SHA-256, backs up the
# database and current code, swaps the code (keeping the venv and any legacy local
# .env/data/backups directories), restarts the service and rolls back on failure.
#
# Usage (as root):
#   sudo scripts/update.sh                       # latest release
#   sudo scripts/update.sh --version v1.2.0      # specific release
#   sudo scripts/update.sh --channel stable      # latest stable (default)
#   sudo scripts/update.sh --channel beta        # latest main-branch commit (unstable)
#   sudo scripts/update.sh --check               # only report selected channel versions
#   sudo scripts/update.sh --repo OWNER/REPOSITORY
#
# Environment overrides: ZIKO_REPO, ZIKO_SERVICE, ZIKO_USER, ZIKO_ROOT,
# ZIKO_HEALTH_SECONDS, ZIKO_KEEP_CODE_SNAPSHOTS, ZIKO_BASE_URL, ZIKO_API_URL.
set -Eeuo pipefail
umask 077

ROOT="${ZIKO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
REPO="${ZIKO_REPO:-OWNER/REPOSITORY}"
SERVICE="${ZIKO_SERVICE:-ziko}"
SERVICE_USER="${ZIKO_USER:-ziko}"
BASE_URL="${ZIKO_BASE_URL:-https://github.com}"
HEALTH_SECONDS="${ZIKO_HEALTH_SECONDS:-15}"
KEEP_SNAPSHOTS="${ZIKO_KEEP_CODE_SNAPSHOTS:-5}"
ASSET_PREFIX="ziko-assistant"
# Top-level entries of $ROOT that an update never touches.
PRESERVE=(.env data .venv backups)

TAG=""
CHANNEL="stable"
CHECK_ONLY=0
WORK=""
BACKUP_DIR="${ZIKO_BACKUP_DIR:-/var/backups/ziko-assistant}"
DB_PATH="${ZIKO_DB_PATH:-/var/lib/ziko-assistant/ziko.sqlite3}"
DB_BACKUP=""
PLATFORM_BACKUP=""
CODE_SNAPSHOT=""
UNIT_FILE="/etc/systemd/system/ziko.service"
UNIT_SNAPSHOT=""
HAD_UNIT=0
STOPPED=0
REPLACED=0
STARTED_NEW=0
ROLLING_BACK=0

log()  { printf '[ziko-update] %s\n' "$*"; }
warn() { printf '[ziko-update] WARNING: %s\n' "$*" >&2; }
die() {
  printf '[ziko-update] ERROR: %s\n' "$*" >&2
  if [[ "$STOPPED" -eq 1 ]]; then rollback; fi   # rollback() exits
  exit 1
}

usage() { sed -n '2,13p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

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

install_requirements() {
  (
    umask 022
    "$ROOT/.venv/bin/python" -m pip install --disable-pip-version-check \
      -q -r "$ROOT/requirements.txt"
    cd "$ROOT"
    "$ROOT/.venv/bin/python" -m pip install --disable-pip-version-check .
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
        --force-reinstall --no-cache-dir "cryptography>=42,<47"
    )
    chmod -R a+rX "$site_packages"
  fi

  run_as_service_user "$ROOT/.venv/bin/python" \
    -c 'from cryptography.fernet import Fernet'
}
rollback() {
  [[ "$ROLLING_BACK" -eq 0 ]] || return 0
  ROLLING_BACK=1
  trap - ERR INT TERM
  set +e
  warn "update failed - rolling back"
  systemctl stop "$SERVICE" 2>/dev/null
  if [[ "$REPLACED" -eq 1 && -f "$CODE_SNAPSHOT" ]]; then
    clear_code
    tar -xzf "$CODE_SNAPSHOT" -C "$ROOT" || warn "code restore failed; snapshot: $CODE_SNAPSHOT"
    install_requirements || warn "could not reinstall previous requirements"
  fi
  if [[ "$HAD_UNIT" -eq 1 && -f "$UNIT_SNAPSHOT" ]]; then
    install -o root -g root -m 644 "$UNIT_SNAPSHOT" "$UNIT_FILE" || warn "systemd unit rollback failed"
    systemctl daemon-reload || warn "systemd daemon-reload failed during rollback"
  fi
  local platform_restored=0
  if [[ "$STARTED_NEW" -eq 1 && -n "$PLATFORM_BACKUP" && -f "$PLATFORM_BACKUP" \
        && -f "$WORK/stage/scripts/restore-platform.sh" ]]; then
    if ZIKO_APP_DIR="$ROOT" ZIKO_ENV_FILE="${ZIKO_ENV_FILE:-/etc/ziko-assistant/ziko.env}" \
      ZIKO_DB_PATH="$DB_PATH" ZIKO_BACKUP_DIR="$BACKUP_DIR" \
      bash "$WORK/stage/scripts/restore-platform.sh" --yes "$PLATFORM_BACKUP"; then
      platform_restored=1
    else
      warn "full platform rollback failed; inspect $BACKUP_DIR"
    fi
  fi
  if [[ "$STARTED_NEW" -eq 1 && "$platform_restored" -eq 0 && -n "$DB_BACKUP" && -f "$DB_BACKUP" ]]; then
    local db="$DB_PATH"
    rm -f -- "$db-wal" "$db-shm"
    install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 700 "$(dirname "$db")"
    install -o "$SERVICE_USER" -g "$SERVICE_USER" -m 600 "$DB_BACKUP" "$db" \
      || warn "database restore failed; backup: $DB_BACKUP"
  fi
  if [[ "$STOPPED" -eq 1 ]]; then
    systemctl start "$SERVICE" || warn "could not start $SERVICE after rollback"
  fi
  warn "rollback finished; check: systemctl status $SERVICE ; journalctl -u $SERVICE"
  [[ -n "$DB_BACKUP" ]] && warn "pre-update database backup: $DB_BACKUP"
  exit 1
}

# shellcheck disable=SC2317
cleanup() { [[ -n "$WORK" && -d "$WORK" ]] && rm -rf -- "$WORK"; }

# shellcheck disable=SC2317
on_error() {
  local code=$? line="${1:-?}"
  warn "command failed (exit $code) at line $line"
  if [[ "$STOPPED" -eq 1 ]]; then rollback; fi
  exit "$code"
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
  [[ "$REPO" != "OWNER/REPOSITORY" ]] \
    || die "set the real repository: --repo <owner>/<repo> or ZIKO_REPO"
  local cmd
  for cmd in curl tar sha256sum systemctl runuser flock install; do
    command -v "$cmd" >/dev/null || die "missing command: $cmd"
  done
  id "$SERVICE_USER" >/dev/null 2>&1 || die "service user '$SERVICE_USER' does not exist"
  [[ -x "$ROOT/.venv/bin/python" && -x "$ROOT/.venv/bin/pip" ]] \
    || die "virtualenv not found at $ROOT/.venv"
  "$ROOT/.venv/bin/python" -c 'import sys; assert sys.version_info[:2] == (3,12)' \
    || die "existing virtualenv must use Python 3.12"
  [[ -f "$UNIT_FILE" ]] || die "systemd unit not found at $UNIT_FILE"

  exec 9>"/run/lock/ziko-update.lock"
  flock -n 9 || die "another update is running"

  WORK="$(mktemp -d)"
  trap cleanup EXIT
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

  # ---- download and verify (nothing is changed yet) ----
  if [[ "$CHANNEL" == "beta" ]]; then
    log "downloading latest main-branch snapshot ($BETA_SHA)"
    curl_get -H "Accept: application/vnd.github+json" \
      -H "X-GitHub-Api-Version: 2022-11-28" \
      -o "$WORK/$asset" "$api_url/repos/$REPO/tarball/$BETA_SHA"
    printf '%s  %s\n' "$(sha256sum "$WORK/$asset" | awk '{print $1}')" "$asset" > "$WORK/SHA256SUMS"
    log "beta snapshot downloaded over verified HTTPS; pinned commit: $BETA_SHA"
  else
    log "downloading $asset"
    curl_get -o "$WORK/$asset" "$rel_url/$asset"
    curl_get -o "$WORK/SHA256SUMS" "$rel_url/SHA256SUMS"
    [[ "$(grep -cF -- "  $asset" "$WORK/SHA256SUMS")" -eq 1 ]] \
      || die "SHA256SUMS has no unique entry for $asset"
    (cd "$WORK" && grep -F -- "  $asset" SHA256SUMS | sha256sum -c --status -) \
      || die "SHA-256 mismatch - refusing to install"
    log "SHA-256 checksum OK"
  fi

  if tar -tzf "$WORK/$asset" | grep -Eq '(^/|(^|/)\.\.(/|$))'; then
    die "archive contains unsafe paths"
  fi
  mkdir "$WORK/stage"
  tar -xzf "$WORK/$asset" -C "$WORK/stage" --strip-components=1
  local f
  for f in app/main.py requirements.txt scripts/update.sh scripts/backup.sh scripts/restore-platform.sh; do
    [[ -f "$WORK/stage/$f" ]] || die "release is missing $f"
  done
  if [[ -n "$(find "$WORK/stage" \( -name '.env' -o -name '*.sqlite*' -o -name '*.db' \) -print -quit)" ]]; then
    die "release contains .env or database files - refusing to install"
  fi
  printf '%s\n' "$TAG" > "$WORK/stage/VERSION"

  # ---- from here on a failure triggers rollback ----
  trap 'on_error $LINENO' ERR
  trap 'warn "interrupted"; if [[ "$STOPPED" -eq 1 ]]; then rollback; fi; exit 130' INT TERM

  local stamp; stamp="$(date -u +%Y%m%dT%H%M%S)-$$)"
  install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 700 "$BACKUP_DIR" "$(dirname "$DB_PATH")"

  if [[ -f "$ROOT/scripts/backup.sh" && -f "$ROOT/scripts/restore-platform.sh" ]]; then
    log "creating verified full-platform pre-update backup"
    local backup_output
    backup_output="$(run_as_service_user env ZIKO_APP_DIR="$ROOT" \
      ZIKO_ENV_FILE="${ZIKO_ENV_FILE:-/etc/ziko-assistant/ziko.env}" \
      ZIKO_DB_PATH="$DB_PATH" ZIKO_BACKUP_DIR="$BACKUP_DIR" \
      bash "$ROOT/scripts/backup.sh" "$BACKUP_DIR")" \
      || die "full-platform pre-update backup failed; update cancelled"
    printf '%s\n' "$backup_output"
    PLATFORM_BACKUP="$(printf '%s\n' "$backup_output" | \
      sed -n 's/^Full platform backup created and verified: //p' | tail -n1)"
    [[ -n "$PLATFORM_BACKUP" && -f "$PLATFORM_BACKUP.sha256" ]] \
      || die "full-platform backup was not published; update cancelled"
    log "full-platform backup: $PLATFORM_BACKUP"
  else
    warn "full-platform backup scripts are not installed; only the main database snapshot will be available for rollback"
  fi

  log "stopping $SERVICE"
  systemctl stop "$SERVICE"
  STOPPED=1

  if [[ -f "$DB_PATH" ]]; then
    DB_BACKUP="$BACKUP_DIR/ziko-pre-update-$stamp.sqlite3"
    run_as_service_user "$ROOT/.venv/bin/python" - "$DB_PATH" "$DB_BACKUP" <<'PY'
import sqlite3, sys
src = sqlite3.connect(sys.argv[1]); dst = sqlite3.connect(sys.argv[2])
try:
    src.backup(dst)
    if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise SystemExit("backup integrity check failed")
finally:
    dst.close(); src.close()
PY
    chmod 600 "$DB_BACKUP"
    (cd "$BACKUP_DIR" && sha256sum "$(basename "$DB_BACKUP")" > "$(basename "$DB_BACKUP").sha256")
    chown "$SERVICE_USER:$SERVICE_USER" "$DB_BACKUP.sha256"
    chmod 600 "$DB_BACKUP.sha256"
    log "database backup: $DB_BACKUP"
  else
    warn "no database found at $DB_PATH - skipping database backup"
  fi

  CODE_SNAPSHOT="$BACKUP_DIR/code-${current}-$stamp.tar.gz"
  tar -czf "$CODE_SNAPSHOT" -C "$ROOT" \
    --exclude=./.env --exclude=./data --exclude=./.venv --exclude=./backups .
  chmod 600 "$CODE_SNAPSHOT"

  log "installing $TAG (keeping .venv and any legacy local data/config directories)"
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
  install_requirements
  [[ -f "$ROOT/deploy/ziko.service" ]] || die "release is missing deploy/ziko.service"
  HAD_UNIT=1
  UNIT_SNAPSHOT="$BACKUP_DIR/ziko.service.pre-update-$stamp"
  cp -p -- "$UNIT_FILE" "$UNIT_SNAPSHOT"
  chmod 600 "$UNIT_SNAPSHOT"
  install -o root -g root -m 644 "$ROOT/deploy/ziko.service" "$UNIT_FILE"
  systemctl daemon-reload

  if [[ -f /etc/ziko-assistant/ziko.env && -f "$ROOT/.env.example" ]]; then
    local key
    while IFS= read -r key; do
      grep -q "^${key}=" /etc/ziko-assistant/ziko.env || warn "new setting $key is in .env.example but not in .env"
    done < <(grep -oE '^[A-Z_][A-Z0-9_]*' "$ROOT/.env.example")
  fi

  log "starting $SERVICE"
  STARTED_NEW=1
  systemctl restart "$SERVICE"
  local i
  for ((i = 0; i < HEALTH_SECONDS; i++)); do
    sleep 1
    systemctl is-active --quiet "$SERVICE" || die "$SERVICE is not active after update"
  done
  [[ "$(systemctl show -p NRestarts --value "$SERVICE")" == "0" ]] \
    || die "$SERVICE restarted during the health window"

  # Success: drop disarmed traps, prune old code snapshots (DB backups are kept).
  trap - ERR INT TERM
  # shellcheck disable=SC2012
  ls -1t "$BACKUP_DIR"/code-*.tar.gz 2>/dev/null | tail -n +"$((KEEP_SNAPSHOTS + 1))" \
    | while IFS= read -r old; do rm -f -- "$old"; done
  log "updated to $TAG. Pre-update DB backup: ${DB_BACKUP:-none}"
}

main "$@"
exit 0

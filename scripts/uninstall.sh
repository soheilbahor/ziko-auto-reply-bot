#!/usr/bin/env bash
# ZIKO uninstall. By default removes ONLY the program (service, /opt files, CLI links)
# and PRESERVES config, database, backups and the ziko user.
# Usage: sudo uninstall.sh [-y] [--purge-data] [--remove-user]
set -euo pipefail

APP_DIR=/opt/ziko-assistant
CONF_DIR=/etc/ziko-assistant
DATA_DIR=/var/lib/ziko-assistant
BACKUP_DIR=/var/backups/ziko-assistant
UNIT=/etc/systemd/system/ziko.service
PURGE=0; RMUSER=0; YES=0

die() { echo "ERROR: $*" >&2; exit 1; }
for arg in "$@"; do
  case "$arg" in
    -y|--yes) YES=1 ;;
    --purge-data) PURGE=1 ;;
    --remove-user) RMUSER=1 ;;
    -h|--help) sed -n '2,5p' "$0"; exit 0 ;;
    *) die "Unknown option: $arg" ;;
  esac
done
[[ "${EUID}" -eq 0 ]] || die "Run as root."

echo "This will remove the ZIKO service and ${APP_DIR}."
if [[ "${PURGE}" -eq 1 ]]; then
  echo "!! --purge-data will ALSO DELETE: ${CONF_DIR}, ${DATA_DIR}, ${BACKUP_DIR}"
  read -r -p "Type DELETE ZIKO DATA to confirm: " a </dev/tty || die "No terminal."
  [[ "${a}" == "DELETE ZIKO DATA" ]] || die "Aborted; nothing changed."
else
  echo "Config, database and backups will be preserved."
  if [[ "${YES}" -ne 1 ]]; then
    read -r -p "Continue? [y/N] " a </dev/tty || die "No terminal."
    [[ "${a}" =~ ^[Yy]([Ee][Ss])?$ ]] || die "Aborted; nothing changed."
  fi
fi

if command -v systemctl >/dev/null; then
  systemctl stop ziko.service 2>/dev/null || true
  systemctl disable ziko.service 2>/dev/null || true
fi
rm -f "${UNIT}"
command -v systemctl >/dev/null && systemctl daemon-reload || true
command -v systemctl >/dev/null && systemctl reset-failed ziko.service 2>/dev/null || true

# Remove only CLI links that point into our venv.
for link in /usr/local/bin/*; do
  [[ -L "${link}" ]] || continue
  case "$(readlink "${link}")" in "${APP_DIR}"/.venv/bin/*) rm -f "${link}" ;; esac
done

[[ "${APP_DIR}" == "/opt/ziko-assistant" ]] && rm -rf "${APP_DIR}"
echo "Program removed."

if [[ "${PURGE}" -eq 1 ]]; then
  rm -rf "${CONF_DIR}" "${DATA_DIR}" "${BACKUP_DIR}"
  echo "Data purged."
else
  echo "Preserved: ${CONF_DIR} ${DATA_DIR} ${BACKUP_DIR}"
fi

if [[ "${RMUSER}" -eq 1 ]]; then
  if [[ "${PURGE}" -ne 1 ]]; then
    echo "Keeping user ziko: it owns preserved data (use --purge-data with --remove-user)."
  elif id -u ziko >/dev/null 2>&1; then
    userdel ziko 2>/dev/null || true
    getent group ziko >/dev/null && groupdel ziko 2>/dev/null || true
    echo "User ziko removed."
  fi
fi

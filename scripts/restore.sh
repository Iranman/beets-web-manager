#!/usr/bin/env bash
# Restore configuration and the beets database from a backup.tar.gz made by
# backup.sh. Stop the app before running this.
set -euo pipefail

if [ $# -ne 1 ]; then
  echo "Usage: $0 <backup-file.tar.gz>" >&2
  exit 1
fi

BACKUP_FILE="$1"
CONFIG_DIR="${BEETS_CONFIG_DIR:-/config}"

if [ ! -f "${BACKUP_FILE}" ]; then
  echo "Backup file not found: ${BACKUP_FILE}" >&2
  exit 1
fi

echo "This will overwrite files in ${CONFIG_DIR}. Make sure the app is stopped."
read -r -p "Continue? [y/N] " confirm
if [ "${confirm}" != "y" ] && [ "${confirm}" != "Y" ]; then
  echo "Aborted."
  exit 1
fi

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "${TMP_DIR}"' EXIT

tar -xzf "${BACKUP_FILE}" -C "${TMP_DIR}"
EXTRACTED="$(find "${TMP_DIR}" -mindepth 1 -maxdepth 1 -type d | head -n1)"

if [ -z "${EXTRACTED}" ]; then
  echo "Could not find backup contents inside ${BACKUP_FILE}" >&2
  exit 1
fi

mkdir -p "${CONFIG_DIR}"
for f in musiclibrary.blb musiclibrary.blb-shm musiclibrary.blb-wal config.yaml; do
  [ -f "${EXTRACTED}/${f}" ] && cp "${EXTRACTED}/${f}" "${CONFIG_DIR}/"
done
if [ -d "${EXTRACTED}/state" ]; then
  cp "${EXTRACTED}/state/"*.json "${CONFIG_DIR}/" 2>/dev/null || true
fi

echo "Restore complete. Start the app and confirm the library loads correctly."

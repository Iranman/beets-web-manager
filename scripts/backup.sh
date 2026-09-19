#!/usr/bin/env bash
# Back up configuration and the beets database. Does NOT back up your music
# library - back that up separately with your own storage/snapshot tooling.
set -euo pipefail

CONFIG_DIR="${BEETS_CONFIG_DIR:-/config}"
BACKUP_DIR="${BACKUP_DIR:-./backups}"
STAMP="$(date +%Y%m%d-%H%M%S)"
DEST="${BACKUP_DIR}/beets-backup-${STAMP}"

if [ ! -d "${CONFIG_DIR}" ]; then
  echo "Config directory not found: ${CONFIG_DIR}" >&2
  echo "Set BEETS_CONFIG_DIR if your /config volume is mounted elsewhere on the host." >&2
  exit 1
fi

mkdir -p "${DEST}"

# Beets database (WAL files included so an in-flight write isn't lost).
for f in musiclibrary.blb musiclibrary.blb-shm musiclibrary.blb-wal; do
  [ -f "${CONFIG_DIR}/${f}" ] && cp "${CONFIG_DIR}/${f}" "${DEST}/"
done

# Beets config.
[ -f "${CONFIG_DIR}/config.yaml" ] && cp "${CONFIG_DIR}/config.yaml" "${DEST}/"

# App JSON state files (review queue, format prefs, job history, etc.).
mkdir -p "${DEST}/state"
find "${CONFIG_DIR}" -maxdepth 1 -name "*.json" -exec cp {} "${DEST}/state/" \;

tar -czf "${DEST}.tar.gz" -C "${BACKUP_DIR}" "$(basename "${DEST}")"
rm -rf "${DEST}"

echo "Backup written to ${DEST}.tar.gz"
echo "This does not include your music library - back that up separately."

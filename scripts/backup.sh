#!/usr/bin/env bash
# Back up a Beets Web Manager stack's state: the Beets library database,
# Beets config.yaml and plugin files, and Web Manager's own state directory
# (/web-manager-data: settings, login, sessions key, audit trail).
#
# It does NOT back up your music files -- use your storage's own
# snapshot/backup tooling for those.
#
# Run it on the Docker host, from your stack directory (the directory with
# docker-compose.yml), or point it at the host folders explicitly:
#
#   ./backup.sh                                    # ./beets + ./web-manager
#   ./backup.sh --beets-config /srv/beets --web-manager-data /srv/web-manager --out /srv/backups
#
# Options (or the environment variable in brackets):
#   --beets-config DIR       host folder mounted at /config     [BEETS_CONFIG_DIR, default ./beets]
#   --web-manager-data DIR   host folder mounted at /web-manager-data
#                                                               [WEB_MANAGER_DATA_DIR, default ./web-manager]
#   --out DIR                where the .tar.gz is written       [BACKUP_DIR, default ./backups]
#   --beets-stopped          you have stopped the beets container (needed only
#                            when neither the sqlite3 tool nor python3 is installed)
#
# The database is copied with SQLite's online backup API (the `sqlite3`
# command's .backup, or python3's sqlite3 module, both opening the database
# read-only), so it is consistent even while Beets is running and writes
# that are still in the -wal file are included. Without either tool the
# script copies the file only when you confirm Beets is stopped and the WAL
# is empty. The live database is never modified.
#
# The archive contains secrets (Web Manager's API token, password hash and
# session key, the Beets plugin key). It is written with mode 600.
set -euo pipefail

BEETS_CONFIG_DIR="${BEETS_CONFIG_DIR:-./beets}"
WEB_MANAGER_DATA_DIR="${WEB_MANAGER_DATA_DIR:-./web-manager}"
BACKUP_DIR="${BACKUP_DIR:-./backups}"
BEETS_STOPPED=0
# Test hook: force one database copy method (sqlite3 | python | stopped).
FORCE_METHOD="${BWM_BACKUP_FORCE_METHOD:-}"

usage() { sed -n '2,32p' "$0" | sed 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
  case "$1" in
    --beets-config) BEETS_CONFIG_DIR="${2:?--beets-config needs a directory}"; shift 2 ;;
    --web-manager-data) WEB_MANAGER_DATA_DIR="${2:?--web-manager-data needs a directory}"; shift 2 ;;
    --out) BACKUP_DIR="${2:?--out needs a directory}"; shift 2 ;;
    --beets-stopped) BEETS_STOPPED=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1 (see --help)" >&2; exit 1 ;;
  esac
done

fail() { echo "ERROR: $*" >&2; exit 1; }

[ -d "${BEETS_CONFIG_DIR}" ] || fail "Beets config folder not found: ${BEETS_CONFIG_DIR}
  This is the host folder mounted at /config (default ./beets in the stack directory).
  Run from your stack directory, or pass --beets-config <dir> / set BEETS_CONFIG_DIR."
[ -d "${WEB_MANAGER_DATA_DIR}" ] || fail "Web Manager data folder not found: ${WEB_MANAGER_DATA_DIR}
  This is the host folder mounted at /web-manager-data (default ./web-manager).
  Pass --web-manager-data <dir> / set WEB_MANAGER_DATA_DIR."

DB="${BEETS_CONFIG_DIR}/musiclibrary.blb"
STAMP="$(date -u +%Y%m%d-%H%M%S)"  # UTC, so names sort the same on every host
NAME="beets-backup-${STAMP}"
umask 077
mkdir -p "${BACKUP_DIR}"
WORK="$(mktemp -d "${BACKUP_DIR}/.${NAME}.XXXXXX")"
trap 'rm -rf "${WORK}"' EXIT
STAGE="${WORK}/${NAME}"
mkdir -p "${STAGE}/beets" "${STAGE}/web-manager-data"

pick_method() {
  if [ -n "${FORCE_METHOD}" ]; then echo "${FORCE_METHOD}"; return; fi
  if command -v sqlite3 >/dev/null 2>&1; then echo sqlite3; return; fi
  if command -v python3 >/dev/null 2>&1 && python3 -c 'import sqlite3' 2>/dev/null; then echo python; return; fi
  echo stopped
}

METHOD="none"
if [ -f "${DB}" ]; then
  METHOD="$(pick_method)"
  # sqlite3's dot-commands take a single-quoted argument with no escaping, so
  # a ' in the destination path would break (or redirect) the .backup target.
  # Use the Python online backup for such paths, which passes them as data.
  case "${STAGE}" in
    *"'"*)
      if [ "${METHOD}" = sqlite3 ]; then
        if command -v python3 >/dev/null 2>&1 && python3 -c 'import sqlite3' 2>/dev/null; then
          METHOD=python
        else
          fail "the backup folder path contains a single quote ('), which the sqlite3 command cannot handle safely.
  Choose a backup folder without ' (--out <dir>), or install python3."
        fi
      fi
      ;;
  esac
  case "${METHOD}" in
    sqlite3)
      sqlite3 -readonly "${DB}" ".backup '${STAGE}/beets/musiclibrary.blb'"
      CHECK="$(sqlite3 "${STAGE}/beets/musiclibrary.blb" 'PRAGMA quick_check;')"
      ;;
    python)
      CHECK="$(python3 - "${DB}" "${STAGE}/beets/musiclibrary.blb" <<'PYEOF'
import sqlite3, sys, urllib.parse
src_path, dst_path = sys.argv[1], sys.argv[2]
src = sqlite3.connect("file:%s?mode=ro" % urllib.parse.quote(src_path), uri=True, timeout=30)
dst = sqlite3.connect(dst_path)
try:
    src.backup(dst)
    print(dst.execute("PRAGMA quick_check;").fetchone()[0])
finally:
    dst.close()
    src.close()
PYEOF
)"
      ;;
    stopped)
      WAL="${DB}-wal"
      [ "${BEETS_STOPPED}" -eq 1 ] || fail "Neither the sqlite3 command nor python3 is installed, so the database
  can only be copied while Beets is stopped. Stop it (docker compose stop beets),
  re-run with --beets-stopped, then start it again (docker compose start beets).
  Or install sqlite3 to back up while Beets runs."
      if [ -s "${WAL}" ]; then
        fail "${WAL} is not empty, so Beets is still running or did not close the database
  cleanly. Stop the beets container and try again (or install sqlite3)."
      fi
      cp -p "${DB}" "${STAGE}/beets/musiclibrary.blb"
      CHECK="not checked (no sqlite tool)"
      ;;
    *) fail "unknown database copy method: ${METHOD}" ;;
  esac
  case "${CHECK}" in
    ok|"not checked (no sqlite tool)") : ;;
    *) fail "the database copy failed its integrity check: ${CHECK}" ;;
  esac
else
  echo "Note: no ${DB} yet (empty library) -- backing up configuration only." >&2
fi

# Beets configuration and the plugin files Web Manager provisions.
for f in config.yaml .webmanager_api_key; do
  if [ -f "${BEETS_CONFIG_DIR}/${f}" ]; then cp -p "${BEETS_CONFIG_DIR}/${f}" "${STAGE}/beets/"; fi
done
if [ -d "${BEETS_CONFIG_DIR}/beetsplug" ]; then cp -Rp "${BEETS_CONFIG_DIR}/beetsplug" "${STAGE}/beets/"; fi
# Legacy JSON state some older installs kept in /config.
mkdir -p "${STAGE}/beets/state"
find "${BEETS_CONFIG_DIR}" -maxdepth 1 -name "*.json" -exec cp -p {} "${STAGE}/beets/state/" \;

# Web Manager's own state: everything except lock files and any leftover
# local database a very old version created (never used now).
( cd "${WEB_MANAGER_DATA_DIR}" && tar -cf - \
    --exclude='./musiclibrary.blb' --exclude='./musiclibrary.blb-wal' --exclude='./musiclibrary.blb-shm' \
    --exclude='*.lock' . ) | ( cd "${STAGE}/web-manager-data" && tar -xf - )

{
  echo "created=${STAMP} (UTC)"
  echo "database_method=${METHOD}"
  echo "database_check=${CHECK:-n/a}"
  echo "beets_config_dir=${BEETS_CONFIG_DIR}"
  echo "web_manager_data_dir=${WEB_MANAGER_DATA_DIR}"
  ( cd "${STAGE}" && find . -type f ! -name MANIFEST.txt -print | LC_ALL=C sort | while IFS= read -r f; do
      if command -v sha256sum >/dev/null 2>&1; then sha256sum "$f"; else shasum -a 256 "$f"; fi
    done )
} > "${STAGE}/MANIFEST.txt"

tar -czf "${BACKUP_DIR}/${NAME}.tar.gz" -C "${WORK}" "${NAME}"
chmod 600 "${BACKUP_DIR}/${NAME}.tar.gz"

echo "Backup written to ${BACKUP_DIR}/${NAME}.tar.gz (database: ${METHOD}, check: ${CHECK:-n/a})"
echo "It contains secrets -- keep it private. It does not include your music files."

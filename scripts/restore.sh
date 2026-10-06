#!/usr/bin/env bash
# Restore a backup made by backup.sh: the Beets library database, Beets
# config.yaml and plugin files, and Web Manager's state directory.
#
# Stop BOTH containers first (docker compose stop), run this on the Docker
# host, then start them again (docker compose up -d).
#
#   ./restore.sh backups/beets-backup-<timestamp>.tar.gz
#   ./restore.sh --beets-config /srv/beets --web-manager-data /srv/web-manager --yes <file>
#
# Options (or the environment variable in brackets):
#   --beets-config DIR       host folder mounted at /config     [BEETS_CONFIG_DIR, default ./beets]
#   --web-manager-data DIR   host folder mounted at /web-manager-data
#                                                               [WEB_MANAGER_DATA_DIR, default ./web-manager]
#   --yes                    do not ask for confirmation
#
# Nothing is deleted: every file the restore replaces is first moved to
# <folder>/.pre-restore-<timestamp>/ inside the same folder.
set -euo pipefail

BEETS_CONFIG_DIR="${BEETS_CONFIG_DIR:-./beets}"
WEB_MANAGER_DATA_DIR="${WEB_MANAGER_DATA_DIR:-./web-manager}"
ASSUME_YES=0
BACKUP_FILE=""

usage() { sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'; }
fail() { echo "ERROR: $*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --beets-config) BEETS_CONFIG_DIR="${2:?--beets-config needs a directory}"; shift 2 ;;
    --web-manager-data) WEB_MANAGER_DATA_DIR="${2:?--web-manager-data needs a directory}"; shift 2 ;;
    --yes|-y) ASSUME_YES=1; shift ;;
    -h|--help) usage; exit 0 ;;
    -*) fail "unknown option: $1 (see --help)" ;;
    *) [ -z "${BACKUP_FILE}" ] || fail "only one backup file may be given"; BACKUP_FILE="$1"; shift ;;
  esac
done

[ -n "${BACKUP_FILE}" ] || { usage >&2; exit 1; }
[ -f "${BACKUP_FILE}" ] || fail "backup file not found: ${BACKUP_FILE}"

# Refuse archives with absolute paths or '..' components before extracting.
if tar -tzf "${BACKUP_FILE}" | grep -Eq '(^/|(^|/)\.\.(/|$))'; then
  fail "the archive contains absolute or '..' paths -- refusing to extract it"
fi
# backup.sh writes only regular files and folders. A symbolic link, hard link
# or device entry could point a later copy at a file outside the restore
# target, so such archives are refused before anything is extracted.
if tar -tvzf "${BACKUP_FILE}" | cut -c1 | grep -qv '^[-d]$'; then
  fail "the archive contains links or special files -- refusing to extract it"
fi

umask 077
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "${TMP_DIR}"' EXIT
tar --no-same-owner --no-same-permissions -xzf "${BACKUP_FILE}" -C "${TMP_DIR}"
# Belt and braces: whatever tar produced, only regular files and folders may be copied.
if [ -n "$(find "${TMP_DIR}" ! -type f ! -type d -print -quit)" ]; then
  fail "the extracted archive contains links or special files -- refusing to restore it"
fi
EXTRACTED="$(find "${TMP_DIR}" -mindepth 1 -maxdepth 1 -type d | head -n1)"
[ -n "${EXTRACTED}" ] || fail "could not find backup contents inside ${BACKUP_FILE}"

# Layout written by backup.sh since it began including Web Manager state;
# older archives had the Beets files at the top level.
if [ -d "${EXTRACTED}/beets" ]; then
  SRC_BEETS="${EXTRACTED}/beets"
  SRC_WM="${EXTRACTED}/web-manager-data"
else
  SRC_BEETS="${EXTRACTED}"
  SRC_WM=""
  echo "Note: this is an older backup without Web Manager state; only Beets files will be restored." >&2
fi

# Verify every file against the sha256 list in MANIFEST.txt before anything
# is restored. A corrupted or edited archive is refused with the restore
# targets untouched. backup.sh has written MANIFEST.txt since it began using
# the beets/ + web-manager-data/ layout, so only older top-level archives may
# lack one.
MANIFEST="${EXTRACTED}/MANIFEST.txt"
if [ -f "${MANIFEST}" ]; then
  SUMS="${TMP_DIR}/manifest.sha256"
  grep -E '^[0-9a-f]{64}  ' "${MANIFEST}" > "${SUMS}" || true
  [ -s "${SUMS}" ] || fail "MANIFEST.txt lists no checksums -- refusing to restore a backup that cannot be verified"
  # Every listed path must stay inside the extracted backup: relative
  # ("./..."), with no ".." component. Checked before sha256sum reads them.
  if awk '{ p = substr($0, 67) } p !~ /^\.\// || p ~ /(^|\/)\.\.(\/|$)/ { bad = 1 } END { exit !bad }' "${SUMS}"; then
    fail "MANIFEST.txt lists a path outside the backup -- nothing was restored"
  fi
  if command -v sha256sum >/dev/null 2>&1; then
    ( cd "${EXTRACTED}" && sha256sum --quiet -c "${SUMS}" ) >&2 \
      || fail "checksum mismatch against MANIFEST.txt -- the backup is damaged or was modified; nothing was restored"
  else
    ( cd "${EXTRACTED}" && shasum -a 256 --quiet -c "${SUMS}" ) >&2 \
      || fail "checksum mismatch against MANIFEST.txt -- the backup is damaged or was modified; nothing was restored"
  fi
  UNLISTED="$(cd "${EXTRACTED}" && find . -type f ! -path ./MANIFEST.txt -print | LC_ALL=C sort \
    | LC_ALL=C comm -23 - <(sed -E 's/^[0-9a-f]{64}  //' "${SUMS}" | LC_ALL=C sort))"
  [ -z "${UNLISTED}" ] || fail "the backup contains files not listed in MANIFEST.txt -- nothing was restored:
${UNLISTED}"
  echo "Verified $(wc -l < "${SUMS}" | tr -d ' ') file checksums against MANIFEST.txt." >&2
elif [ -n "${SRC_WM}" ]; then
  fail "the backup has no MANIFEST.txt, so its contents cannot be verified -- refusing to restore it"
else
  echo "Note: older backup without MANIFEST.txt; file checksums cannot be verified." >&2
fi

if [ -s "${BEETS_CONFIG_DIR}/musiclibrary.blb-wal" ]; then
  fail "${BEETS_CONFIG_DIR}/musiclibrary.blb-wal is not empty -- Beets looks like it is still running.
  Stop both containers first (docker compose stop)."
fi

echo "This restores ${BACKUP_FILE} into:"
echo "  Beets config:      ${BEETS_CONFIG_DIR}"
[ -n "${SRC_WM}" ] && echo "  Web Manager data:  ${WEB_MANAGER_DATA_DIR}"
echo "Replaced files are kept under .pre-restore-<timestamp>/ in each folder."
echo "Both containers must be stopped (docker compose stop)."
if [ "${ASSUME_YES}" -ne 1 ]; then
  read -r -p "Continue? [y/N] " confirm
  case "${confirm}" in y|Y) : ;; *) echo "Aborted."; exit 1 ;; esac
fi

STAMP="$(date -u +%Y%m%d-%H%M%S)"

# move_aside <dir> <relative path>: keep the current file/dir before replacing it.
move_aside() {
  local dir="$1" rel="$2" keep="$1/.pre-restore-${STAMP}"
  if [ -e "${dir}/${rel}" ]; then
    mkdir -p "$(dirname "${keep}/${rel}")"
    mv "${dir}/${rel}" "${keep}/${rel}"
  fi
}

mkdir -p "${BEETS_CONFIG_DIR}"
if [ -f "${SRC_BEETS}/musiclibrary.blb" ]; then
  # The -wal/-shm of the current database belong to it, not to the restored copy.
  for f in musiclibrary.blb musiclibrary.blb-wal musiclibrary.blb-shm; do move_aside "${BEETS_CONFIG_DIR}" "$f"; done
  cp -p "${SRC_BEETS}/musiclibrary.blb" "${BEETS_CONFIG_DIR}/musiclibrary.blb"
  # Older backups also carried -wal/-shm copies.
  for f in musiclibrary.blb-wal musiclibrary.blb-shm; do
    if [ -f "${SRC_BEETS}/$f" ]; then cp -p "${SRC_BEETS}/$f" "${BEETS_CONFIG_DIR}/$f"; fi
  done
fi
for f in config.yaml .webmanager_api_key; do
  if [ -f "${SRC_BEETS}/$f" ]; then
    move_aside "${BEETS_CONFIG_DIR}" "$f"
    cp -p "${SRC_BEETS}/$f" "${BEETS_CONFIG_DIR}/$f"
    chmod 600 "${BEETS_CONFIG_DIR}/$f"  # config.yaml and the plugin key hold credentials
  fi
done
if [ -d "${SRC_BEETS}/beetsplug" ]; then
  move_aside "${BEETS_CONFIG_DIR}" beetsplug
  cp -RPp "${SRC_BEETS}/beetsplug" "${BEETS_CONFIG_DIR}/beetsplug"
fi
if [ -d "${SRC_BEETS}/state" ]; then
  for f in "${SRC_BEETS}/state/"*.json; do
    [ -f "$f" ] || continue
    move_aside "${BEETS_CONFIG_DIR}" "$(basename "$f")"
    cp -p "$f" "${BEETS_CONFIG_DIR}/"
  done
fi

if [ -n "${SRC_WM}" ] && [ -d "${SRC_WM}" ]; then
  mkdir -p "${WEB_MANAGER_DATA_DIR}"
  for entry in "${SRC_WM}"/* "${SRC_WM}"/.[!.]*; do
    [ -e "${entry}" ] || continue
    name="$(basename "${entry}")"
    move_aside "${WEB_MANAGER_DATA_DIR}" "${name}"
    cp -RPp "${entry}" "${WEB_MANAGER_DATA_DIR}/${name}"
  done
fi

echo "Restore complete. Start the stack (docker compose up -d), then check:"
echo "  docker compose exec beets beet stats"
echo "  curl -s http://127.0.0.1:8337/api/health"

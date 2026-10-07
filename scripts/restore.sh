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
#
# The archive is copied into a private temporary folder (mode 0700, owned by
# the user running the restore) and is listed, verified and extracted only
# there. Each restored file or folder is copied, without following links,
# into a private staging folder inside its target folder and then renamed
# into place, so a link planted in the target folder is never written
# through. If anything appears at a restored path while the restore runs,
# the restore stops (see "Backup and restore safety" in docs/TRUENAS_ROLLOUT.md).
# A restored file keeps the owner of the file it replaces (else the owner of
# its folder), so the containers' PUID/PGID can still read it. Needs GNU mv.
set -euo pipefail

BEETS_CONFIG_DIR="${BEETS_CONFIG_DIR:-./beets}"
WEB_MANAGER_DATA_DIR="${WEB_MANAGER_DATA_DIR:-./web-manager}"
ASSUME_YES=0
BACKUP_FILE=""

usage() { sed -n '2,29p' "$0" | sed 's/^# \{0,1\}//'; }
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

# Everything below reads only a private copy of the archive, so the archive
# that is checked is the archive that is extracted, even if the original is
# replaced while the restore runs.
umask 077
TMP_DIR="$(mktemp -d)"
STAGES=()
cleanup() { rm -rf "${TMP_DIR}" ${STAGES[@]+"${STAGES[@]}"}; }
trap cleanup EXIT
if [ ! -d "${TMP_DIR}" ] || [ -L "${TMP_DIR}" ] || [ ! -O "${TMP_DIR}" ]; then
  fail "could not create a private temporary folder"
fi
chmod 700 "${TMP_DIR}"
ARCHIVE="${TMP_DIR}/backup.tar.gz"
cp -- "${BACKUP_FILE}" "${ARCHIVE}"
mkdir "${TMP_DIR}/x"

# Refuse archives with absolute paths or '..' components before extracting.
if tar -tzf "${ARCHIVE}" | grep -Eq '(^/|(^|/)\.\.(/|$))'; then
  fail "the archive contains absolute or '..' paths -- refusing to extract it"
fi
# backup.sh writes only regular files and folders. A symbolic link, hard link
# or device entry could point a later copy at a file outside the restore
# target, so such archives are refused before anything is extracted.
if tar -tvzf "${ARCHIVE}" | cut -c1 | grep -qv '^[-d]$'; then
  fail "the archive contains links or special files -- refusing to extract it"
fi

tar --no-same-owner --no-same-permissions -xzf "${ARCHIVE}" -C "${TMP_DIR}/x"
# Belt and braces: whatever tar produced, only regular files and folders may be copied.
if [ -n "$(find "${TMP_DIR}/x" ! -type f ! -type d -print -quit)" ]; then
  fail "the extracted archive contains links or special files -- refusing to restore it"
fi
EXTRACTED="$(find "${TMP_DIR}/x" -mindepth 1 -maxdepth 1 -type d | head -n1)"
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

# Every rename below is `mv -T` (GNU coreutils): it renames onto exactly the
# path given and replaces a link planted there instead of moving into the
# folder it points at. Without it the restore cannot be made safe, so it stops.
touch "${TMP_DIR}/mvt-a"
mv -T -- "${TMP_DIR}/mvt-a" "${TMP_DIR}/mvt-b" 2>/dev/null \
  || fail "this system's mv does not support -T (GNU coreutils) -- the restore needs it to rename files safely; run it on a Linux host"
rm -f -- "${TMP_DIR}/mvt-b"

# pinned_cd <dir>: cd into a folder this run just created and refuse it if
# its name was swapped (for a link or another folder) in the meantime. The
# target folders may be writable by the containers' user, who can rename
# entries in them; once the cwd is the folder itself, later renames of its
# name no longer matter, so everything after this works on ./ paths.
pinned_cd() {
  local want="$1" parent
  parent="$(cd -- "$(dirname -- "${want}")" && pwd -P)" || fail "${want} vanished while the restore was running -- stopped"
  cd -- "${want}" 2>/dev/null && [ "$(pwd -P)" = "${parent}/$(basename -- "${want}")" ] && [ -O . ] \
    || fail "${want} was replaced while the restore was running -- stopped"
}

# move_aside <dir> <name>: keep the current <dir>/<name> under
# <dir>/.pre-restore-<stamp>/ before it is replaced, and print its owner
# (uid:gid) so the restored copy can be given the same one. The keep folder
# is created by this run (mkdir refuses an existing one) with umask 077.
# Call it as $(move_aside ...): it changes directory.
move_aside() {
  local dir rel="$2" keep
  dir="$(cd -- "$1" && pwd -P)" || fail "cannot open $1"
  keep="${dir}/.pre-restore-${STAMP}"
  if [ -e "${dir}/${rel}" ] || [ -L "${dir}/${rel}" ]; then
    if [ ! -d "${keep}" ] || [ -L "${keep}" ] || [ ! -O "${keep}" ]; then
      mkdir "${keep}" || fail "${keep} already exists and was not created by this restore -- stopped"
    fi
    pinned_cd "${keep}"
    mv -T -- "${dir}/${rel}" "./${rel}" || fail "could not move ${dir}/${rel} aside -- stopped"
    if [ ! -L "./${rel}" ]; then stat -c '%u:%g' -- "./${rel}"; fi
  fi
}

# place <src> <dir> <name> [mode] [owner]: restore one file or folder as
# <dir>/<name>. It is copied without following links into a private 0700
# staging folder inside <dir> (same filesystem, so the last step is a
# rename), entered by pinned_cd so a swapped stage cannot redirect the copy,
# re-checked, given [owner] (default: the owner of <dir>; only when running
# as root) and [mode], then renamed onto exactly <dir>/<name>. Anything that
# appeared at <dir>/<name> after move_aside stops the restore.
place() {
  local src="$1" dir name="$3" mode="${4:-}" owner="${5:-}" stage
  dir="$(cd -- "$2" && pwd -P)" || fail "cannot open $2"
  [ -n "${owner}" ] || owner="$(stat -c '%u:%g' -- "${dir}")"
  stage="$(mktemp -d "${dir}/.restore-stage.XXXXXX")"
  STAGES+=("${stage}")
  (
    pinned_cd "${stage}"
    cp -RPp -- "${src}" ./item || fail "could not copy ${src} -- ${dir}/${name} was not restored"
    if [ -n "$(find ./item ! -type f ! -type d -print -quit)" ]; then
      fail "${src} is or contains a link or special file -- ${dir}/${name} was not restored"
    fi
    if [ "$(id -u)" = 0 ]; then chown -R -- "${owner}" ./item || fail "could not set the owner of ${dir}/${name}"; fi
    if [ -n "${mode}" ]; then chmod "${mode}" ./item || fail "could not set the mode of ${dir}/${name}"; fi
    if [ -e "${dir}/${name}" ] || [ -L "${dir}/${name}" ]; then
      fail "${dir}/${name} appeared while the restore was running -- it was left as is and the restore stopped; check what else writes to ${dir}.
  Files restored so far stay in place; what they replaced is in ${dir}/.pre-restore-${STAMP}/."
    fi
    mv -T -- ./item "${dir}/${name}" || fail "could not rename the restored copy onto ${dir}/${name}"
  ) || exit 1
  rmdir -- "${stage}" 2>/dev/null || true
}

mkdir -p "${BEETS_CONFIG_DIR}"
if [ -f "${SRC_BEETS}/musiclibrary.blb" ]; then
  # The -wal/-shm of the current database belong to it, not to the restored copy.
  owner="$(move_aside "${BEETS_CONFIG_DIR}" musiclibrary.blb)"
  for f in musiclibrary.blb-wal musiclibrary.blb-shm; do ( move_aside "${BEETS_CONFIG_DIR}" "$f" >/dev/null ) || exit 1; done
  place "${SRC_BEETS}/musiclibrary.blb" "${BEETS_CONFIG_DIR}" musiclibrary.blb "" "${owner}"
  # Older backups also carried -wal/-shm copies.
  for f in musiclibrary.blb-wal musiclibrary.blb-shm; do
    if [ -f "${SRC_BEETS}/$f" ]; then place "${SRC_BEETS}/$f" "${BEETS_CONFIG_DIR}" "$f" "" "${owner}"; fi
  done
fi
for f in config.yaml .webmanager_api_key; do
  if [ -f "${SRC_BEETS}/$f" ]; then
    owner="$(move_aside "${BEETS_CONFIG_DIR}" "$f")"
    place "${SRC_BEETS}/$f" "${BEETS_CONFIG_DIR}" "$f" 600 "${owner}"  # config.yaml and the plugin key hold credentials
  fi
done
if [ -d "${SRC_BEETS}/beetsplug" ]; then
  owner="$(move_aside "${BEETS_CONFIG_DIR}" beetsplug)"
  place "${SRC_BEETS}/beetsplug" "${BEETS_CONFIG_DIR}" beetsplug "" "${owner}"
fi
if [ -d "${SRC_BEETS}/state" ]; then
  for f in "${SRC_BEETS}/state/"*.json; do
    [ -f "$f" ] || continue
    owner="$(move_aside "${BEETS_CONFIG_DIR}" "$(basename "$f")")"
    place "$f" "${BEETS_CONFIG_DIR}" "$(basename "$f")" "" "${owner}"
  done
fi

if [ -n "${SRC_WM}" ] && [ -d "${SRC_WM}" ]; then
  mkdir -p "${WEB_MANAGER_DATA_DIR}"
  for entry in "${SRC_WM}"/* "${SRC_WM}"/.[!.]*; do
    [ -e "${entry}" ] || continue
    name="$(basename "${entry}")"
    owner="$(move_aside "${WEB_MANAGER_DATA_DIR}" "${name}")"
    place "${entry}" "${WEB_MANAGER_DATA_DIR}" "${name}" "" "${owner}"
  done
fi

echo "Restore complete. Start the stack (docker compose up -d), then check:"
echo "  docker compose exec beets beet stats"
echo "  curl -s http://127.0.0.1:8337/api/health"

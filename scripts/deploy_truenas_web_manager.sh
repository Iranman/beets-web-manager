#!/usr/bin/env bash
# Guarded, fail-closed rollout of Beets Web Manager onto a TrueNAS Docker
# Compose stack. Reusable across releases -- pass VERSION (and, once known,
# EXPECTED_REVISION) rather than editing this file per release.
#
# See docs/TRUENAS_ROLLOUT.md for the full writeup.
#
# Design goals:
#   - Never touch the authoritative Beets database (/config/musiclibrary.blb
#     under the `beets` engine's own mount). While Beets runs, integrity is
#     ONLINE SEMANTIC INTEGRITY: counts, an identity digest over every item
#     and album, and plugin health, all read through the running engine's
#     own web API -- never by opening its SQLite file, and never by hashing
#     only the main .blb file (in WAL mode committed changes can live in
#     musiclibrary.blb-wal, so a matching main-file hash proves nothing).
#     OFFLINE BYTE IDENTITY is a separate, explicit mode
#     (--offline-db-identity) that stops Beets first.
#   - Never guess container names, host paths, or the deployed image --
#     everything is resolved from `docker inspect` / `docker compose config`
#     and independently re-verified.
#   - Perform ZERO mutating action until every safety check has passed.
#   - Archive (never delete) the stale, pre-#64 web-manager-created database
#     using exact filenames -- no wildcards, no `rm`.
#   - Recreate only the `beets-web-manager` service. Plex, Lidarr and every
#     other service are left untouched. The Beets engine is never recreated;
#     it is RESTARTED (same container) only when the webmanager plugin files
#     Web Manager provisioned differ from the plugin version the running
#     engine reports, with an online semantic snapshot before and after.
#   - Support --dry-run (inspect-only; see docs/TRUENAS_ROLLOUT.md for the
#     exact list of what it pulls and writes) and --rollback DIR (undo this
#     script's own change set, then prove the previous image is running).
#
# Usage:
#   /bin/bash scripts/deploy_truenas_web_manager.sh              # real rollout
#   /bin/bash scripts/deploy_truenas_web_manager.sh --dry-run     # inspect only
#   /bin/bash scripts/deploy_truenas_web_manager.sh --rollback DIR
#   /bin/bash scripts/deploy_truenas_web_manager.sh --offline-db-identity
#       (stops the Beets engine briefly, hashes the settled database file,
#        restarts it and re-verifies; BASELINE_DB_SHA256=<hex> to compare)
#   /bin/bash scripts/deploy_truenas_web_manager.sh --prune-backups-older-than DAYS [--dry-run]
#       (opt-in retention: deletes this script's own rollout backup
#        directories older than DAYS, always keeping the newest one; with
#        --dry-run it only lists what it would delete)
#
# Configuration (env vars):
#   STACK_DIR (required for every mode except --help),
#   VERSION (required and validated for --dry-run/deploy only -- --rollback
#   and --help do not need it), EXPECTED_REVISION (strongly recommended),
#   SERVICE, ENGINE_SERVICE, COMPOSE_FILE, MIN_ITEM_COUNT, STALE_DB_MAX_ITEMS,
#   ENDPOINT_BASE_URL, RESTORE_STALE_DB (rollback only), BACKUP_ROOT
#
# Exit codes: 0 success/dry-run-clean, 1 any safety check or stage failure
# (see the printed "ROLLOUT FAILED" block for stage/backup-dir/rollback cmd).

set -Eeuo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
# No generic default makes sense here -- every deployment stack directory
# is host-specific. Left unset here (not required at global init) so
# --help works with zero environment configured; --dry-run, deploy, and
# --rollback all require it -- enforced just after arg parsing below, once
# we know the requested mode isn't --help.
STACK_DIR="${STACK_DIR:-}"
SERVICE="${SERVICE:-beets-web-manager}"
ENGINE_SERVICE="${ENGINE_SERVICE:-beets}"
# No release default -- pinning one here would make the script silently
# redeploy a stale version forever. Left unset at global init (same reason
# as STACK_DIR: --help must work without it); required and validated only
# for --dry-run and the real rollout via validate_version(), called from
# run_dry_run()/run_deploy() only. --rollback never needs a version -- it
# restores whatever image reference the backup recorded.
VERSION="${VERSION:-}"
IMAGE_REPO="ghcr.io/iranman/beets-web-manager"
EXPECTED_IMAGE="${IMAGE_REPO}:${VERSION}"
# The shipped Compose files use the literal moving tag below. The script
# never edits it: it pulls that tag, proves the pulled image's version label
# is VERSION before anything changes, and records the previous image ID and
# registry digest so --rollback can re-tag it locally.
LATEST_IMAGE="${IMAGE_REPO}:latest"
# Set by verify_compose_image(): "latest" (the literal :latest tag),
# "pinned" (an exact :VERSION tag) or "variable" (${BEETS_WEB_MANAGER_VERSION}).
IMAGE_LAYOUT=""
# The image reference the Compose file resolves to, and the image ID that
# was pulled and verified for it before the recreate.
DEPLOY_REF=""
DEPLOY_IMAGE_ID=""
# Set once the VERSION release's commit is known (e.g. EXPECTED_REVISION=<sha>)
# to pin the exact org.opencontainers.image.revision label. Strongly recommended
# for pinned production rollouts. When unset, revision-label check is skipped
# with a warning.
EXPECTED_REVISION="${EXPECTED_REVISION:-}"
COMPOSE_FILE="${COMPOSE_FILE:-}"
DB_FILENAME="musiclibrary.blb"
WAL_FILENAME="musiclibrary.blb-wal"
SHM_FILENAME="musiclibrary.blb-shm"
TOKEN_FILENAME=".auth_token"
BACKUP_ROOT="${BACKUP_ROOT:-${STACK_DIR}/_backups}"
# Item count below this is treated as "suspiciously low" for the
# authoritative database -- raise this to your real library size.
MIN_ITEM_COUNT="${MIN_ITEM_COUNT:-10}"
# A "stale" web-manager-created DB containing more items than this (or more
# than the authoritative DB) is refused as unsafe to treat as disposable.
STALE_DB_MAX_ITEMS="${STALE_DB_MAX_ITEMS:-100000}"
# Health/readiness wait budget after (re)creating the web-manager container.
HEALTH_TIMEOUT_SECONDS="${HEALTH_TIMEOUT_SECONDS:-120}"
ENDPOINT_BASE_URL="${ENDPOINT_BASE_URL:-http://127.0.0.1:8337}"
RESTORE_STALE_DB="${RESTORE_STALE_DB:-0}"

MODE="deploy"
ROLLBACK_DIR=""
DRY_RUN=0
PRUNE_DAYS=""

# Environment keys whose VALUES may be kept in the redacted copies of
# `docker inspect` / `docker compose config` stored in a rollout backup.
# Everything else keeps its key name only (value replaced by <redacted>):
# the stack's Compose file and container env carry API keys and tokens for
# every service (Plex, Lidarr, slskd, AI providers, ...), and backups are
# kept on disk. The real .env is still copied verbatim (mode 600) because
# rollback needs it -- see docs/TRUENAS_ROLLOUT.md.
REDACTION_ALLOWLIST="PUID PGID TZ UMASK WEBCONTROL_PORT BEETS_WEB_URL BEETS_OUTBOUND_ALLOWLIST WEB_MANAGER_DATA_DIR BEETS_TRANSACTION_DIR MUSIC_ROOT BEETSDIR MUSIC_LIBRARY_PATH DOWNLOAD_PATH DEMO_MODE BEETS_WEB_AUTH_DISABLED BEETS_WEB_MANAGER_VERSION PATH LANG HOME PYTHONUNBUFFERED PYTHON_VERSION"

# ---------------------------------------------------------------------------
# Arg parsing
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)
      DRY_RUN=1
      # --dry-run combined with --prune-backups-older-than stays in prune
      # mode (list only); on its own it is the rollout dry-run.
      [[ "$MODE" == "prune-backups" ]] || MODE="dry-run"
      shift
      ;;
    --prune-backups-older-than)
      MODE="prune-backups"
      PRUNE_DAYS="${2:-}"
      [[ "$PRUNE_DAYS" =~ ^[1-9][0-9]*$ ]] || { echo "FATAL: --prune-backups-older-than requires a whole number of days >= 1" >&2; exit 1; }
      shift 2
      ;;
    --offline-db-identity)
      MODE="offline-db-identity"
      shift
      ;;
    --rollback)
      MODE="rollback"
      ROLLBACK_DIR="${2:-}"
      [[ -n "$ROLLBACK_DIR" ]] || { echo "FATAL: --rollback requires a backup directory path" >&2; exit 1; }
      shift 2
      ;;
    -h|--help)
      # Print the leading comment block (everything up to `set -Eeuo`).
      awk 'NR > 1 && /^#/ { print; next } NR > 1 { exit }' "${BASH_SOURCE[0]}"
      exit 0
      ;;
    *)
      echo "FATAL: unrecognized argument: $1" >&2
      exit 1
      ;;
  esac
done

# ---------------------------------------------------------------------------
# Logging / error trap
# ---------------------------------------------------------------------------
STAGE="init"
# Stable, machine-readable code for a failure, printed in the failure block
# next to the human message (set just before the matching die). Codes are
# listed in docs/TRUENAS_ROLLOUT.md; never rename one -- add a new code.
REASON_CODE=""
BACKUP_DIR=""
PREVIOUS_IMAGE_ID=""

log()  { printf '[%s] %s\n' "$(date -u +%H:%M:%S)" "$*" >&2; }
warn() { printf '[%s] WARNING: %s\n' "$(date -u +%H:%M:%S)" "$*" >&2; }

_FAILURE_REPORTED=0

# Prints the failed-stage/backup-dir/rollback-command diagnostic block.
report_failure() {
  local ec="$1"
  [[ "$_FAILURE_REPORTED" -eq 1 ]] && return 0
  _FAILURE_REPORTED=1
  {
    echo ""
    if [[ "$MODE" == "offline-db-identity" ]]; then
      echo "=============== OFFLINE DB CHECK FAILED ================"
    else
      echo "==================== ROLLOUT FAILED ===================="
    fi
    echo "Failed stage:          ${STAGE}"
    [[ -z "$REASON_CODE" ]] || echo "Reason code:           ${REASON_CODE}"
    echo "Backup directory:      ${BACKUP_DIR:-<none created yet>}"
    echo "Previous image ID:     ${PREVIOUS_IMAGE_ID:-<unknown/not reached>}"
    echo "Current container status:"
    if command -v docker >/dev/null 2>&1; then
      docker inspect --format '  {{.Name}}: {{.State.Status}} (health={{if .State.Health}}{{.State.Health.Status}}{{else}}n/a{{end}})' "$SERVICE" 2>/dev/null \
        || echo "  (container '${SERVICE}' not found or docker unavailable)"
    fi
    echo "Rollback command:"
    if [[ "$DRY_RUN" -eq 1 || "$MODE" == "dry-run" ]]; then
      echo "  (Dry-run only: no production mutation occurred; no rollback is required.)"
    elif [[ "$MODE" == "rollback" ]]; then
      echo "  (the rollback itself failed -- fix the cause above, then re-run: $0 --rollback ${ROLLBACK_DIR})"
    elif [[ -n "$BACKUP_DIR" && -d "$BACKUP_DIR" && -f "$BACKUP_DIR/docker-compose.yml.bak" ]]; then
      echo "  $0 --rollback ${BACKUP_DIR}"
    else
      echo "  (no backup was created yet -- nothing to roll back; production was not touched)"
    fi
    echo "=========================================================="
  } >&2
  return "$ec"
}

# Latest layout: the image :latest named before this run pulled it. Until
# the recreate, a refusal, failure or dry run points :latest back at it, so
# a later plain 'docker compose up -d' cannot drift onto an image this run
# pulled but did not deploy.
PRE_PULL_LATEST_ID=""
RETAG_LATEST_PENDING=0
record_pre_pull_latest_tag() {
  [[ "$IMAGE_LAYOUT" == "latest" ]] || return 0
  PRE_PULL_LATEST_ID="$(docker image inspect "$LATEST_IMAGE" --format '{{.Id}}' 2>/dev/null || true)"
  RETAG_LATEST_PENDING=1
}
restore_pre_pull_latest_tag() {
  [[ "${RETAG_LATEST_PENDING:-0}" -eq 1 ]] || return 0
  RETAG_LATEST_PENDING=0
  local now
  now="$(docker image inspect "$LATEST_IMAGE" --format '{{.Id}}' 2>/dev/null || true)"
  [[ "$now" != "$PRE_PULL_LATEST_ID" ]] || return 0
  if [[ -z "$PRE_PULL_LATEST_ID" ]]; then
    warn "${LATEST_IMAGE} was not on this host before this run; it now names ${now}, which was NOT deployed"
  elif docker tag "$PRE_PULL_LATEST_ID" "$LATEST_IMAGE" >/dev/null 2>&1; then
    log "Re-tagged ${LATEST_IMAGE} back to ${PRE_PULL_LATEST_ID} (the image it named before this run); ${now} was not deployed."
  else
    warn "could not re-tag ${LATEST_IMAGE} back to ${PRE_PULL_LATEST_ID} -- a plain 'docker compose up -d' would now start ${now}"
  fi
}

die() {
  printf '[%s] FATAL (%s): %s\n' "$(date -u +%H:%M:%S)" "$STAGE" "$*" >&2
  restore_pre_pull_latest_tag
  restart_engine_if_stopped
  report_failure 1
  exit 1
}

on_error() {
  local ec=$?
  [[ "$ec" -eq 0 ]] && return 0
  restore_pre_pull_latest_tag
  restart_engine_if_stopped
  report_failure "$ec"
  exit "$ec"
}

# Set while --offline-db-identity has the Beets engine stopped; every exit
# path (die, ERR trap) restarts it so a failed check never leaves it down.
ENGINE_STOPPED_BY_US=0
restart_engine_if_stopped() {
  [[ "$ENGINE_STOPPED_BY_US" -eq 1 ]] || return 0
  ENGINE_STOPPED_BY_US=0
  printf '[%s] Restarting %s after an offline check failure...\n' "$(date -u +%H:%M:%S)" "${ENGINE_SERVICE}" >&2
  docker compose -f "$COMPOSE_FILE" start "$ENGINE_SERVICE" >&2 || true
}
trap on_error ERR

# STACK_DIR is required for every mode that reaches this point: --help
# exits inside the arg-parsing loop above, before here, so it never hits
# this check and needs no environment configured at all.
[[ -n "$STACK_DIR" ]] || die "STACK_DIR is required (set STACK_DIR to your deployment stack directory)"

# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------
validate_version() {
  [[ -n "${VERSION:-}" ]] || die "VERSION is required (set VERSION to the exact release version, e.g. VERSION=0.1.6)"
  if [[ "$VERSION" =~ ^(latest|stable|edge)$ ]] || ! [[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-[a-zA-Z0-9\.-]+)?$ ]]; then
    die "VERSION must be an explicit numbered release (e.g. 0.1.6, 1.0.0, 1.2.3-rc.1), got: '${VERSION}'"
  fi
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "required command not found on PATH: $1"
}

require_cmd docker
require_cmd python3

_compose() {
  docker compose -f "$COMPOSE_FILE" "$@"
}

_py() {
  python3 "$@" | tr -d '\r'
}

resolve_compose_file() {
  if [[ -n "$COMPOSE_FILE" ]]; then
    [[ -f "$COMPOSE_FILE" ]] || die "COMPOSE_FILE does not exist: $COMPOSE_FILE"
    return 0
  fi
  local candidate
  for candidate in "${STACK_DIR}/docker-compose.arrs.yml" "${STACK_DIR}/docker-compose.yml"; do
    if [[ -f "$candidate" ]]; then
      COMPOSE_FILE="$candidate"
      return 0
    fi
  done
  die "no Compose file found under ${STACK_DIR} (looked for docker-compose.arrs.yml, docker-compose.yml) -- set COMPOSE_FILE explicitly"
}

canon_path() {
  _py -c 'import os,sys; print(os.path.realpath(sys.argv[1]).replace("\\","/"))' "$1"
}

sha256_file() {
  if command -v sha256sum >/dev/null 2>&1; then
    # GNU coreutils prefixes the whole output line with a literal '\' when
    # the filename needs escaping (contains a backslash, newline, or CR) --
    # `canon_path()` output never does, but defend anyway rather than
    # silently returning that marker as part of the hash if ever fed one.
    sha256sum "$1" | awk '{print $1}' | sed 's/^\\//'
  else
    _py -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$1"
  fi
}

file_inode() {
  _py -c 'import os,sys; print(os.stat(sys.argv[1]).st_ino)' "$1"
}

file_owner() {
  _py -c '
import os, sys
st = os.stat(sys.argv[1])
try:
    import pwd
    print(pwd.getpwuid(st.st_uid).pw_name)
except Exception:
    print(st.st_uid)
' "$1"
}

file_mode_octal() {
  _py -c 'import os,stat,sys; print(oct(stat.S_IMODE(os.stat(sys.argv[1]).st_mode)))' "$1"
}

file_size() {
  _py -c 'import os,sys; print(os.path.getsize(sys.argv[1]))' "$1"
}

sqlite_ro_query() {
  local db="$1" sql="$2"
  _py - "$db" "$sql" <<'PYEOF'
import sqlite3, sys
db, sql = sys.argv[1], sys.argv[2]
uri = "file:%s?mode=ro" % db
con = sqlite3.connect(uri, uri=True, timeout=5)
try:
    cur = con.execute(sql)
    rows = cur.fetchall()
    for row in rows:
        print("|".join("" if c is None else str(c) for c in row))
finally:
    con.close()
PYEOF
}

# ONLINE SEMANTIC INTEGRITY: read the library through the running engine's
# own interfaces (stock Beets web API /stats, /item/, /album/ and the
# webmanager plugin's /webmanager/status) inside the engine container. The
# SQLite file is never opened. The digest covers every item's identity
# (id, album row, Recording/Release/Release Group IDs, disc, track, path)
# and every album's identity, so any unexpected mutation changes it. The
# plugin API key is read inside the container and never printed.
SEMANTIC_SNAPSHOT_PY='
import hashlib, json, urllib.request
BASE = "http://127.0.0.1:8337"
def get(path, headers=None):
    req = urllib.request.Request(BASE + path, headers=headers or {})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))
stats = get("/stats")
items = get("/item/").get("items") or []
albums = get("/album/").get("albums") or []
digest = hashlib.sha256()
for it in sorted(items, key=lambda r: r.get("id") or 0):
    digest.update(json.dumps([it.get(k) for k in ("id", "album_id", "mb_trackid", "mb_albumid",
                                                    "mb_releasegroupid", "disc", "track", "path")]).encode())
digest.update(b"|albums|")
for al in sorted(albums, key=lambda r: r.get("id") or 0):
    digest.update(json.dumps([al.get(k) for k in ("id", "mb_albumid", "mb_releasegroupid",
                                                    "albumartist", "album")]).encode())
status = {}
try:
    key = open("/config/.webmanager_api_key", encoding="utf-8").read().strip()
    status = get("/webmanager/status", {"Authorization": "Bearer " + key})
except Exception:
    status = {}
print(json.dumps({"items": stats.get("items"), "albums": stats.get("albums"),
                  "listed_items": len(items), "listed_albums": len(albums),
                  "digest": digest.hexdigest(),
                  "plugin_version": status.get("plugin_version") or ""}))
'

beets_semantic_snapshot() {
  docker exec "$ENGINE_CID" python3 -c "$SEMANTIC_SNAPSHOT_PY" | tr -d '\r'
}

snapshot_field() {
  _py -c 'import json,sys; v=json.loads(sys.argv[1]).get(sys.argv[2]); print("" if v is None else v)' "$1" "$2"
}

# Takes a semantic snapshot and validates it; prints the JSON on stdout.
checked_semantic_snapshot() {
  local label="$1" snap items listed plugin
  snap="$(beets_semantic_snapshot)" || die "${label}: could not read the library through the Beets engine's web API"
  items="$(snapshot_field "$snap" items)"
  listed="$(snapshot_field "$snap" listed_items)"
  plugin="$(snapshot_field "$snap" plugin_version)"
  [[ "$items" =~ ^[0-9]+$ ]] || die "${label}: engine /stats returned no valid item count"
  [[ "$items" == "$listed" ]] || die "${label}: engine /stats reports ${items} items but /item/ listed ${listed}"
  [[ -n "$plugin" ]] || die "${label}: webmanager plugin is not healthy (no /webmanager/status)"
  printf '%s\n' "$snap"
}

file_is_open() {
  local f="$1"
  if command -v lsof >/dev/null 2>&1; then
    lsof -- "$f" >/dev/null 2>&1
    return $?
  elif command -v fuser >/dev/null 2>&1; then
    fuser "$f" >/dev/null 2>&1
    return $?
  else
    die "neither lsof nor fuser is available -- cannot verify '$f' is closed before moving it; install one of them"
  fi
}

compose_config_json() {
  _compose config --format json 2>/dev/null || _compose config | _py -c '
import sys, json
try:
    import yaml
    print(json.dumps(yaml.safe_load(sys.stdin)))
except ImportError:
    sys.exit(97)
'
}

compose_service_image() {
  local svc="$1"
  compose_config_json | _py -c "
import json, sys
data = json.load(sys.stdin)
svc = data.get('services', {}).get('$svc', {})
print(svc.get('image', ''))
"
}

resolve_container_id() {
  local svc="$1" cid
  # -a: a stopped container (e.g. after a failed deploy) must still resolve.
  cid="$(_compose ps -a -q "$svc" 2>/dev/null || true)"
  [[ -n "$cid" ]] || die "could not resolve a container for compose service '${svc}' via 'docker compose ps -a -q' -- refusing to guess a container name"
  echo "$cid"
}

mount_source_for_dest() {
  local cid="$1" dest="$2"
  docker inspect --format '{{json .Mounts}}' "$cid" | _py -c "
import json, sys
mounts = json.load(sys.stdin)
for m in mounts:
    if m.get('Destination') == '$dest':
        print(m.get('Source', ''))
        break
"
}

wait_for_health() {
  local cid="$1" timeout="$2" waited=0
  while [[ "$waited" -lt "$timeout" ]]; do
    local status
    status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$cid" 2>/dev/null || echo "missing")"
    if [[ "$status" == "healthy" ]]; then
      return 0
    fi
    if [[ "$status" == "missing" ]]; then
      die "container disappeared while waiting for health"
    fi
    sleep 3
    waited=$((waited + 3))
  done
  return 1
}

# ---------------------------------------------------------------------------
# Phase A.1 -- Mount discovery & verification (read-only)
# ---------------------------------------------------------------------------
ENGINE_CID=""
WEBMGR_CID=""
ENGINE_CONFIG_SRC=""
WEBMGR_DATA_SRC=""
WEBMGR_LEGACY_CONFIG_SRC=""

discover_and_verify_mounts() {
  STAGE="mount-discovery"
  log "Resolving containers for services '${ENGINE_SERVICE}' and '${SERVICE}'..."
  ENGINE_CID="$(resolve_container_id "$ENGINE_SERVICE")"
  WEBMGR_CID="$(resolve_container_id "$SERVICE")"

  ENGINE_CONFIG_SRC="$(mount_source_for_dest "$ENGINE_CID" /config)"
  [[ -n "$ENGINE_CONFIG_SRC" ]] || die "could not determine the Beets engine's /config host source from 'docker inspect ${ENGINE_SERVICE}'"

  # Where the running app keeps its state: every shipped Compose file mounts
  # /web-manager-data and sets WEB_MANAGER_DATA_DIR=/web-manager-data. When
  # WEB_MANAGER_DATA_DIR is NOT set, backend/app_runtime.py falls back to
  # /data whenever /data exists in the container (a legacy single-mount
  # layout), and to /web-manager-data otherwise. /data is checked first so a
  # legacy deployment that still mounts it is verified against the directory
  # the app actually uses. Only an exact /data mount matches here; media
  # mounted below it (/data/music) does not.
  WEBMGR_DATA_SRC="$(mount_source_for_dest "$WEBMGR_CID" /data)"
  if [[ -z "$WEBMGR_DATA_SRC" ]]; then
    WEBMGR_DATA_SRC="$(mount_source_for_dest "$WEBMGR_CID" /web-manager-data)"
  fi
  [[ -n "$WEBMGR_DATA_SRC" ]] || die "could not determine web-manager's data host source from 'docker inspect ${SERVICE}' -- is /data or /web-manager-data mounted at all?"

  WEBMGR_LEGACY_CONFIG_SRC="$(mount_source_for_dest "$WEBMGR_CID" /config || true)"

  local engine_canon webmgr_canon
  engine_canon="$(canon_path "$ENGINE_CONFIG_SRC")"
  webmgr_canon="$(canon_path "$WEBMGR_DATA_SRC")"

  [[ "$engine_canon" != "$webmgr_canon" ]] || die "Beets /config source and web-manager data source resolve to the SAME path (${engine_canon}) -- refusing to continue"

  AUTH_DB_PATH="${engine_canon%/}/${DB_FILENAME}"
  STALE_DB_PATH="${webmgr_canon%/}/${DB_FILENAME}"
  STALE_WAL_PATH="${webmgr_canon%/}/${WAL_FILENAME}"
  STALE_SHM_PATH="${webmgr_canon%/}/${SHM_FILENAME}"
  TOKEN_PATH="${webmgr_canon%/}/${TOKEN_FILENAME}"

  case "$AUTH_DB_PATH" in
    "$webmgr_canon"/*) die "authoritative database path (${AUTH_DB_PATH}) resolves INSIDE the web-manager data source -- refusing to continue" ;;
  esac
  case "$STALE_DB_PATH" in
    "$engine_canon"/*) die "stale database path (${STALE_DB_PATH}) resolves INSIDE the authoritative Beets /config source -- refusing to continue" ;;
  esac

  log "Beets engine /config source:        ${engine_canon}"
  log "web-manager data source: ${webmgr_canon}"
  [[ -n "$WEBMGR_LEGACY_CONFIG_SRC" ]] && log "web-manager legacy /config source:  $(canon_path "$WEBMGR_LEGACY_CONFIG_SRC")"
  log "Authoritative DB path (expected):   ${AUTH_DB_PATH}"
  log "Stale DB path (if present):         ${STALE_DB_PATH}"
}

# ---------------------------------------------------------------------------
# Phase A.2 -- Compose / image verification (read-only)
# ---------------------------------------------------------------------------
# True when an `image:` line (not a comment) interpolates the version variable.
compose_image_line_uses_version_variable() {
  grep -Eq '^[[:space:]]*image:[^#]*BEETS_WEB_MANAGER_VERSION' "$COMPOSE_FILE"
}

verify_compose_image() {
  STAGE="compose-image-verification"
  local resolved_image
  # Only a Compose file that uses ${BEETS_WEB_MANAGER_VERSION} reads this.
  export BEETS_WEB_MANAGER_VERSION="$VERSION"
  resolved_image="$(compose_service_image "$SERVICE")"
  [[ -n "$resolved_image" ]] || die "compose service '${SERVICE}' has no image defined in ${COMPOSE_FILE}"
  if [[ "$resolved_image" == "$LATEST_IMAGE" ]]; then
    IMAGE_LAYOUT="latest"
  elif [[ "$resolved_image" == "$EXPECTED_IMAGE" ]]; then
    if compose_image_line_uses_version_variable; then IMAGE_LAYOUT="variable"; else IMAGE_LAYOUT="pinned"; fi
  else
    REASON_CODE="compose_image_mismatch"
    die "compose service '${SERVICE}' resolves to '${resolved_image}', expected '${LATEST_IMAGE}' (the shipped layout) or '${EXPECTED_IMAGE}' (pinned to this script's VERSION). This script never edits the image line; change it yourself or deploy the version it pins."
  fi
  DEPLOY_REF="$resolved_image"
  log "Compose service '${SERVICE}' resolves to ${resolved_image} (layout: ${IMAGE_LAYOUT})"
}

# ---------------------------------------------------------------------------
# Phase A.3 -- Authoritative database safety checks (read-only)
# ---------------------------------------------------------------------------
AUTH_DB_SIZE="" AUTH_DB_SHA256="" AUTH_DB_INODE="" AUTH_DB_OWNER="" AUTH_DB_MODE=""
AUTH_ITEM_COUNT="" AUTH_ALBUM_COUNT="" AUTH_SEMANTIC="" AUTH_SEMANTIC_DIGEST=""

verify_authoritative_database() {
  STAGE="authoritative-db-verification"
  [[ -f "$AUTH_DB_PATH" ]] || die "authoritative database missing: ${AUTH_DB_PATH}"

  # Online: the engine owns this file and may be writing it (WAL mode), so
  # the library is read through its own web API, never by opening SQLite.
  AUTH_SEMANTIC="$(checked_semantic_snapshot "pre-deploy")"
  AUTH_ITEM_COUNT="$(snapshot_field "$AUTH_SEMANTIC" items)"
  AUTH_ALBUM_COUNT="$(snapshot_field "$AUTH_SEMANTIC" albums)"
  AUTH_SEMANTIC_DIGEST="$(snapshot_field "$AUTH_SEMANTIC" digest)"
  [[ "$AUTH_ITEM_COUNT" =~ ^[0-9]+$ ]] || die "authoritative item count is not a valid integer: '${AUTH_ITEM_COUNT}'"
  [[ "$AUTH_ALBUM_COUNT" =~ ^[0-9]+$ ]] || die "authoritative album count is not a valid integer: '${AUTH_ALBUM_COUNT}'"
  [[ "$AUTH_ITEM_COUNT" -ge "$MIN_ITEM_COUNT" ]] || die "authoritative item count (${AUTH_ITEM_COUNT}) is below MIN_ITEM_COUNT (${MIN_ITEM_COUNT}) -- suspiciously low, refusing to continue"

  AUTH_DB_SIZE="$(file_size "$AUTH_DB_PATH")"
  AUTH_DB_SHA256="$(sha256_file "$AUTH_DB_PATH")"
  AUTH_DB_INODE="$(file_inode "$AUTH_DB_PATH")"
  AUTH_DB_OWNER="$(file_owner "$AUTH_DB_PATH")"
  AUTH_DB_MODE="$(file_mode_octal "$AUTH_DB_PATH")"

  if [[ -f "$STALE_DB_PATH" ]]; then
    local auth_canon stale_canon
    auth_canon="$(canon_path "$AUTH_DB_PATH")"
    stale_canon="$(canon_path "$STALE_DB_PATH")"
    [[ "$auth_canon" != "$stale_canon" ]] || die "authoritative and stale database resolve to the SAME canonical path (${auth_canon}) -- refusing to continue"
    local stale_inode
    stale_inode="$(file_inode "$STALE_DB_PATH")"
    [[ "$AUTH_DB_INODE" != "$stale_inode" ]] || die "authoritative and stale database share the same inode (${AUTH_DB_INODE}) -- refusing to continue"
    local stale_sha
    stale_sha="$(sha256_file "$STALE_DB_PATH")"
    [[ "$AUTH_DB_SHA256" != "$stale_sha" ]] || die "authoritative and stale database have identical SHA-256 checksums -- refusing to continue"
  fi

  log "Authoritative DB file: path=${AUTH_DB_PATH} size=${AUTH_DB_SIZE} owner=${AUTH_DB_OWNER} mode=${AUTH_DB_MODE}"
  log "  live main-file sha256=${AUTH_DB_SHA256} (informational only: in WAL mode the main file alone is not the logical database)"
  log "Online semantic integrity (engine API): items=${AUTH_ITEM_COUNT} albums=${AUTH_ALBUM_COUNT} digest=${AUTH_SEMANTIC_DIGEST} plugin=$(snapshot_field "$AUTH_SEMANTIC" plugin_version)"
}

# ---------------------------------------------------------------------------
# Phase A.4 -- Stale database inspection (read-only; move happens in Phase C)
# ---------------------------------------------------------------------------
STALE_DB_EXISTS=0
STALE_ITEM_COUNT=""

inspect_stale_database() {
  STAGE="stale-db-inspection"
  if [[ ! -f "$STALE_DB_PATH" ]]; then
    log "No stale database present at ${STALE_DB_PATH} -- nothing to archive."
    return 0
  fi
  STALE_DB_EXISTS=1

  local webmgr_canon engine_canon stale_canon
  webmgr_canon="$(canon_path "$WEBMGR_DATA_SRC")"
  engine_canon="$(canon_path "$ENGINE_CONFIG_SRC")"
  stale_canon="$(canon_path "$STALE_DB_PATH")"
  case "$stale_canon" in
    "$webmgr_canon"/*) : ;;
    *) die "stale database (${stale_canon}) is not under the web-manager data source (${webmgr_canon})" ;;
  esac
  case "$stale_canon" in
    "$engine_canon"/*) die "stale database (${stale_canon}) is under the Beets engine /config source (${engine_canon}) -- refusing to continue" ;;
  esac

  local quick_check
  if ! quick_check="$(sqlite_ro_query "$STALE_DB_PATH" 'PRAGMA quick_check;')"; then
    die "PRAGMA quick_check failed to execute against stale database (${STALE_DB_PATH}) -- refusing to continue"
  fi
  [[ "$quick_check" == "ok" ]] || die "stale database failed PRAGMA quick_check: '${quick_check}'"

  local has_items_table
  if ! has_items_table="$(sqlite_ro_query "$STALE_DB_PATH" "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='items';")"; then
    die "could not query sqlite_master on stale database -- refusing to continue"
  fi

  if [[ "$has_items_table" == "1" ]]; then
    if ! STALE_ITEM_COUNT="$(sqlite_ro_query "$STALE_DB_PATH" 'SELECT count(*) FROM items;')"; then
      die "could not read item count from stale database -- refusing to continue"
    fi
    [[ "$STALE_ITEM_COUNT" =~ ^[0-9]+$ ]] || die "stale item count is not a valid integer: '${STALE_ITEM_COUNT}'"
    [[ "$STALE_ITEM_COUNT" -le "$STALE_DB_MAX_ITEMS" ]] || die "stale database item count (${STALE_ITEM_COUNT}) exceeds STALE_DB_MAX_ITEMS (${STALE_DB_MAX_ITEMS}) -- too large to treat as disposable, investigate before proceeding"
    [[ "$STALE_ITEM_COUNT" -le "$AUTH_ITEM_COUNT" ]] || die "stale database contains MORE items (${STALE_ITEM_COUNT}) than the authoritative database (${AUTH_ITEM_COUNT}) -- this is not safe to treat as disposable, investigate before proceeding"
  else
    STALE_ITEM_COUNT=0
  fi

  log "Stale DB verified as disposable: path=${STALE_DB_PATH} items=${STALE_ITEM_COUNT} (quick_check=ok)"
}

# ---------------------------------------------------------------------------
# Phase A.5 -- Token verification (read-only; migration happens in Phase C)
# ---------------------------------------------------------------------------
TOKEN_EXISTS=0
TOKEN_SIZE="" TOKEN_MODE="" TOKEN_OWNER="" TOKEN_SHA256=""
NEEDS_TOKEN_MIGRATION=0
LEGACY_TOKEN_PATH=""
ACTIVE_AUTH_TOKEN_PATH=""

inspect_auth_token() {
  STAGE="token-inspection"
  log "Token env vars present (names only, values never read here): $(env | awk -F= '/^BEETS_(API|WEB_AUTH)_TOKEN/{print $1}' | paste -sd, -)"

  [[ ! -L "$TOKEN_PATH" ]] || die "the auth token path (${TOKEN_PATH}) is a symbolic link -- refusing to continue; replace it with a regular file first"
  if [[ -f "$TOKEN_PATH" ]]; then
    TOKEN_SIZE="$(file_size "$TOKEN_PATH")"
    if [[ "$TOKEN_SIZE" -gt 0 ]]; then
      TOKEN_EXISTS=1
      ACTIVE_AUTH_TOKEN_PATH="$TOKEN_PATH"
      TOKEN_MODE="$(file_mode_octal "$TOKEN_PATH")"
      TOKEN_OWNER="$(file_owner "$TOKEN_PATH")"
      TOKEN_SHA256="$(sha256_file "$TOKEN_PATH")"
      case "$TOKEN_MODE" in
        0o600|0o400) : ;;
        *) warn "persistent auth token file mode is ${TOKEN_MODE}, expected 0o600 (world/group readable tokens are a real risk)" ;;
      esac
      log "Persistent web auth token found: path=${TOKEN_PATH} size=${TOKEN_SIZE} mode=${TOKEN_MODE} owner=${TOKEN_OWNER} sha256=${TOKEN_SHA256}"
      return 0
    else
      die "persistent auth token file exists but is empty: ${TOKEN_PATH}"
    fi
  fi

  log "No persistent token file at ${TOKEN_PATH} yet."
  if [[ -n "$WEBMGR_LEGACY_CONFIG_SRC" ]]; then
    local legacy_canon candidate
    legacy_canon="$(canon_path "$WEBMGR_LEGACY_CONFIG_SRC")"
    candidate="${legacy_canon%/}/${TOKEN_FILENAME}"
    if [[ -f "$candidate" ]]; then
      local legacy_size api_token_val legacy_val
      legacy_size="$(file_size "$candidate")"
      if [[ "$legacy_size" -gt 0 ]]; then
        api_token_val="${BEETS_API_TOKEN:-}"
        legacy_val="$(cat "$candidate")"
        if [[ -n "$api_token_val" && "$legacy_val" == "$api_token_val" ]]; then
          unset legacy_val api_token_val
          warn "legacy token candidate at ${candidate} is identical to BEETS_API_TOKEN -- unusable as web auth token; ignoring"
        else
          unset legacy_val api_token_val
          LEGACY_TOKEN_PATH="$candidate"
          NEEDS_TOKEN_MIGRATION=1
          ACTIVE_AUTH_TOKEN_PATH="$candidate"
          if [[ "$DRY_RUN" -eq 1 ]]; then
            log "Using validated legacy auth token read-only for dry-run endpoint verification"
          else
            log "Legacy web-manager token candidate found: ${LEGACY_TOKEN_PATH} (guarded migration will run in Phase C)"
          fi
          return 0
        fi
      else
        warn "legacy token candidate at ${candidate} is empty -- ignoring"
      fi
    fi
  fi
  ACTIVE_AUTH_TOKEN_PATH=""
}

# ---------------------------------------------------------------------------
# Phase A.6 -- Backup destination planning
# ---------------------------------------------------------------------------
plan_backup_dir() {
  STAGE="backup-planning"
  local stamp
  stamp="$(date -u +%Y%m%d-%H%M%S)"
  if [[ "$DRY_RUN" -eq 1 ]]; then
    BACKUP_DIR="$(mktemp -d)/web-manager-rollout-${stamp}"
  else
    BACKUP_DIR="${BACKUP_ROOT}/web-manager-rollout-${stamp}"
  fi
  log "Backup directory planned: ${BACKUP_DIR}"
}

# ---------------------------------------------------------------------------
# Phase A.7 -- Endpoint reachability
# ---------------------------------------------------------------------------
probe_endpoint() {
  # probe_endpoint <path> <auth: 0|1> -- prints "status|elapsed_ms|bytes"
  local path="$1" auth="$2" tok_arg=()
  if [[ "$auth" -eq 1 && -n "$ACTIVE_AUTH_TOKEN_PATH" && -f "$ACTIVE_AUTH_TOKEN_PATH" ]]; then
    local tok
    tok="$(cat "$ACTIVE_AUTH_TOKEN_PATH")"
    tok_arg=(-H "Authorization: Bearer ${tok}")
    unset tok
  fi
  local start end status size out body
  body="$(mktemp)"
  start="$(_py -c 'import time; print(time.monotonic())')"
  out="$(curl -sS -o "$body" -w '%{http_code}' --max-time 10 "${tok_arg[@]}" "${ENDPOINT_BASE_URL}${path}" 2>/dev/null || echo "000")"
  end="$(_py -c 'import time; print(time.monotonic())')"
  status="$out"
  size="$(file_size "$body" 2>/dev/null || echo 0)"
  rm -f "$body" 2>/dev/null || true
  local elapsed_ms
  elapsed_ms="$(_py -c "print(int((${end}-${start})*1000))")"
  echo "${status}|${elapsed_ms}|${size}"
}

verify_endpoints() {
  local mode="${1:-post-deploy}"
  STAGE="endpoint-verification"
  local endpoints=("/api/health:0:500" "/api/setup/status:1:2000" "/api/library?limit=1:1:2000" "/api/library?limit=50:1:5000")
  local spec path auth budget_ms i result status ms size any_failed=0
  for spec in "${endpoints[@]}"; do
    path="${spec%%:*}"; local rest="${spec#*:}"
    auth="${rest%%:*}"; budget_ms="${rest#*:}"
    for i in 1 2 3; do
      result="$(probe_endpoint "$path" "$auth")"
      status="${result%%|*}"; local rest2="${result#*|}"
      ms="${rest2%%|*}"; size="${rest2#*|}"
      if [[ "$status" != "200" ]]; then
        warn "endpoint ${path} attempt ${i}: HTTP ${status} (expected 200)"
        any_failed=1
      fi
      if [[ "$ms" -gt "$budget_ms" ]]; then
        warn "endpoint ${path} attempt ${i}: SLOW ${ms}ms (target <${budget_ms}ms) -- not hidden, reporting as-is"
      fi
      log "endpoint ${path} attempt ${i}: status=${status} time=${ms}ms size=${size}bytes"
    done
  done

  # Pagination contract check against the authoritative item count recorded
  # earlier -- never hard-coded.
  local tok pagination_json total returned
  if [[ -n "$ACTIVE_AUTH_TOKEN_PATH" && -f "$ACTIVE_AUTH_TOKEN_PATH" ]]; then
    tok="$(cat "$ACTIVE_AUTH_TOKEN_PATH")"
    pagination_json="$(curl -sS --max-time 10 -H "Authorization: Bearer ${tok}" "${ENDPOINT_BASE_URL}/api/library?limit=1" 2>/dev/null || true)"
    unset tok
    total="$(_py -c "
import json, sys
try:
    d = json.loads(sys.argv[1])
    print(d.get('pagination', {}).get('total', ''))
except Exception:
    print('')
" "$pagination_json" 2>/dev/null || true)"
    returned="$(_py -c "
import json, sys
try:
    d = json.loads(sys.argv[1])
    print(d.get('pagination', {}).get('returned', ''))
except Exception:
    print('')
" "$pagination_json" 2>/dev/null || true)"
    if [[ -n "$total" && -n "$AUTH_ITEM_COUNT" ]]; then
      if [[ "$total" != "$AUTH_ITEM_COUNT" ]]; then
        warn "pagination.total (${total}) does not match authoritative item count (${AUTH_ITEM_COUNT}) recorded earlier"
        any_failed=1
      else
        log "pagination.total (${total}) matches authoritative item count"
      fi
    fi
    [[ "$returned" == "1" ]] || warn "pagination.returned for limit=1 was '${returned}', expected '1'"
  fi

  if [[ "$any_failed" -eq 1 ]]; then
    if [[ "$mode" == "dry-run" ]]; then
      warn "one or more endpoint checks failed during dry-run (see warnings above)"
      return 1
    else
      die "one or more endpoint checks failed -- see warnings above"
    fi
  fi
  return 0
}

# ---------------------------------------------------------------------------
# Setup readiness (RD-6): /api/setup/status before vs after
# ---------------------------------------------------------------------------
# A deploy whose endpoints all answer 200 can still leave the app unusable
# (e.g. a new "Cannot write to downloads/staging path" blocking reason).
# The status and blocking_reasons are recorded before anything changes and
# compared after the recreate; a NEW blocking reason fails the deploy.
#
# Reasons are compared by their stable machine-readable codes when both the
# previous and the new version report them (`blocking_reason_codes`, a list
# parallel to `blocking_reasons`), so rewording a message never changes the
# gate. When either side has no codes (a version from before they existed)
# the comparison falls back to the exact message text and says so.
SETUP_STATUS_BEFORE=""

# Prints {"http": "<code>", "status": "...", "blocking_reasons": [...],
# "blocking_reason_codes": [...] or null}.
fetch_setup_status() {
  local tok_arg=() body http
  if [[ -n "$ACTIVE_AUTH_TOKEN_PATH" && -f "$ACTIVE_AUTH_TOKEN_PATH" ]]; then
    local tok
    tok="$(cat "$ACTIVE_AUTH_TOKEN_PATH")"
    tok_arg=(-H "Authorization: Bearer ${tok}")
    unset tok
  fi
  body="$(mktemp)"
  http="$(curl -sS -o "$body" -w '%{http_code}' --max-time 15 "${tok_arg[@]}" "${ENDPOINT_BASE_URL}/api/setup/status" 2>/dev/null || echo "000")"
  _py - "$body" "$http" <<'PYEOF'
import json, sys
path, http = sys.argv[1], sys.argv[2]
try:
    data = json.load(open(path, encoding="utf-8"))
except Exception:
    data = {}
if not isinstance(data, dict):
    data = {}
reasons = data.get("blocking_reasons")
reasons = [str(r) for r in reasons] if isinstance(reasons, list) else []
codes = data.get("blocking_reason_codes")
# Codes are usable only as a list of non-empty strings parallel to the messages.
if not (isinstance(codes, list) and len(codes) == len(reasons)
        and all(isinstance(c, str) and c for c in codes)):
    codes = None
print(json.dumps({
    "http": http,
    "status": "".join(c if c.isprintable() else "\\u%04x" % ord(c) for c in str(data.get("status", ""))),
    "blocking_reasons": reasons,
    "blocking_reason_codes": codes,
}))
PYEOF
  rm -f "$body"
}

record_setup_status_before() {
  STAGE="setup-status-before"
  SETUP_STATUS_BEFORE="$(fetch_setup_status)"
  log "Setup status before: $(snapshot_field "$SETUP_STATUS_BEFORE" status) http=$(snapshot_field "$SETUP_STATUS_BEFORE" http) blocking_reasons=$(_py -c 'import json,sys; print(json.dumps(json.loads(sys.argv[1])["blocking_reasons"]))' "$SETUP_STATUS_BEFORE") blocking_reason_codes=$(_py -c 'import json,sys; print(json.dumps(json.loads(sys.argv[1]).get("blocking_reason_codes")))' "$SETUP_STATUS_BEFORE")"
  if [[ "$(snapshot_field "$SETUP_STATUS_BEFORE" http)" != "200" ]]; then
    warn "could not read /api/setup/status before the deploy -- after the deploy ANY blocking reason will fail it"
  fi
}

# Dies when the post-deploy status has a blocking reason that was not there
# before (or any blocking reason, if "before" could not be read).
assert_no_new_setup_blocking_reasons() {
  STAGE="setup-status-after"
  local after new by before_json="${SETUP_STATUS_BEFORE}"
  [[ -n "$before_json" ]] || before_json='{}'
  after="$(fetch_setup_status)"
  if [[ "$(snapshot_field "$after" http)" != "200" ]]; then
    REASON_CODE="setup_status_unavailable"
    die "/api/setup/status did not answer 200 after the deploy (http=$(snapshot_field "$after" http)). Roll back with: $0 --rollback ${BACKUP_DIR}"
  fi
  # First output line: what the reasons were compared by ("code" or
  # "message"). Then one "<message><TAB><code>" line per NEW reason.
  new="$(_py - "$before_json" "$after" <<'PYEOF'
import json, sys
before = json.loads(sys.argv[1] or "{}")
after = json.loads(sys.argv[2])
before_ok = before.get("http") == "200"
messages = after.get("blocking_reasons") or []
after_codes = after.get("blocking_reason_codes")
before_codes = before.get("blocking_reason_codes")
if after_codes is not None and (before_codes is not None or not before_ok):
    by, keys, known = "code", after_codes, set(before_codes or []) if before_ok else set()
else:
    by, keys = "message", messages
    known = set(before.get("blocking_reasons") or []) if before_ok else set()
# The server's text goes into the operator's log: control characters
# (newlines, tabs, ANSI escapes) are escaped so a line cannot be forged and
# the message/code pairing on each line holds.
def esc(text):
    return "".join(c if c.isprintable() else "\\u%04x" % ord(c) for c in str(text))
print(by)
for i, key in enumerate(keys):
    if key not in known:
        code = after_codes[i] if after_codes is not None else ""
        print(f"{esc(messages[i])}\t{esc(code)}")
PYEOF
)"
  by="${new%%$'\n'*}"
  new="${new#"$by"}"
  new="${new#$'\n'}"
  log "Setup status after: $(snapshot_field "$after" status)"
  if [[ "$by" == "code" ]]; then
    log "Setup blocking reasons compared by reason code."
  else
    log "Setup blocking reasons compared by exact message text: the previous or the new version does not report blocking_reason_codes."
  fi
  if [[ -n "$new" ]]; then
    local r msg code
    while IFS= read -r r; do
      [[ -n "$r" ]] || continue
      msg="${r%$'\t'*}"
      code="${r##*$'\t'}"
      warn "NEW setup blocking reason after deploy: ${msg} (reason_code=${code:-none})"
    done <<< "$new"
    REASON_CODE="setup_new_blocking_reason"
    die "the deploy introduced new setup blocking reason(s) (listed above). The new version is running but not ready; fix the cause, or roll back with: $0 --rollback ${BACKUP_DIR}"
  fi
  log "No new setup blocking reasons."
}

# ---------------------------------------------------------------------------
# Plugin refresh (RD-8): restart Beets only when its loaded plugin is stale
# ---------------------------------------------------------------------------
# Web Manager copies its bundled webmanager plugin into the engine's
# /config/beetsplug at its own startup, but the running Beets process keeps
# the plugin code it imported at ITS startup. When the provisioned files
# carry a different PLUGIN_VERSION than the engine reports through
# /webmanager/status, the engine (and only the engine) is restarted -- same
# container, never recreated -- with the online semantic snapshot (counts +
# identity digest) taken before and compared after.
ENGINE_RESTARTED_FOR_PLUGIN=0

provisioned_plugin_version() {
  local f="${ENGINE_CONFIG_SRC%/}/beetsplug/webmanager/version.py"
  [[ -f "$f" ]] || { echo ""; return 0; }
  _py - "$f" <<'PYEOF'
import re, sys
m = re.search(r'^PLUGIN_VERSION\s*=\s*["\']([^"\']+)["\']', open(sys.argv[1], encoding="utf-8").read(), re.M)
print(m.group(1) if m else "")
PYEOF
}

refresh_engine_plugin_if_stale() {
  STAGE="engine-plugin-refresh"
  local provisioned before running after
  provisioned="$(provisioned_plugin_version)"
  before="$(checked_semantic_snapshot "before plugin check")"
  running="$(snapshot_field "$before" plugin_version)"
  if [[ -z "$provisioned" ]]; then
    warn "could not read the provisioned plugin version (${ENGINE_CONFIG_SRC%/}/beetsplug/webmanager/version.py) -- not restarting ${ENGINE_SERVICE}; running plugin is ${running}"
    return 0
  fi
  if [[ "$provisioned" == "$running" ]]; then
    log "Beets engine already runs the provisioned webmanager plugin ${running} -- no engine restart needed."
    return 0
  fi
  log "Provisioned webmanager plugin is ${provisioned} but the running engine reports ${running}: restarting ${ENGINE_SERVICE} only (same container)."
  ENGINE_STOPPED_BY_US=1
  _compose restart "$ENGINE_SERVICE" >&2
  ENGINE_STOPPED_BY_US=0
  after="$(wait_for_engine_semantics)" || die "${ENGINE_SERVICE} did not come back with a healthy webmanager plugin within ${HEALTH_TIMEOUT_SECONDS}s after the plugin restart"
  [[ "$(snapshot_field "$before" items)" == "$(snapshot_field "$after" items)" ]] || die "item count changed across the engine restart: $(snapshot_field "$before" items) -> $(snapshot_field "$after" items)"
  [[ "$(snapshot_field "$before" albums)" == "$(snapshot_field "$after" albums)" ]] || die "album count changed across the engine restart: $(snapshot_field "$before" albums) -> $(snapshot_field "$after" albums)"
  [[ "$(snapshot_field "$before" digest)" == "$(snapshot_field "$after" digest)" ]] || die "library identity digest changed across the engine restart"
  [[ "$(snapshot_field "$after" plugin_version)" == "$provisioned" ]] || die "after restarting ${ENGINE_SERVICE} the engine reports plugin $(snapshot_field "$after" plugin_version), expected ${provisioned}"
  ENGINE_RESTARTED_FOR_PLUGIN=1
  log "Engine restarted: plugin ${running} -> ${provisioned}; items/albums/digest unchanged."
  if [[ -n "$BACKUP_DIR" && -d "$BACKUP_DIR" ]]; then
    printf 'engine_restarted_for_plugin=1\nplugin_before=%s\nplugin_after=%s\n' "$running" "$provisioned" >> "$BACKUP_DIR/engine-plugin.txt"
  fi
}

# ---------------------------------------------------------------------------
# Backup helpers (RD-7 / RD-20)
# ---------------------------------------------------------------------------
# Web Manager's own durable state that a new version may rewrite. Restored
# on rollback. transactions/ is the mutation audit trail: it is backed up,
# but rollback only ADDS back files that are missing and never deletes or
# overwrites records written after the deploy.
WEBMGR_STATE_FILES=(".env" ".browser_username" ".browser_password" ".flask_secret_key" ".setup_complete" ".browser_setup_state")
WEBMGR_STATE_DIRS=("transactions")

# redact_json <kind> < in > out   (kind: inspect | compose)
redact_json() {
  # Values are redacted unless their variable name is allowlisted. Even an
  # allowlisted URL loses any user:password@ part. Free-form strings (command,
  # entrypoint, healthcheck, labels, build args, x-* extensions) keep their
  # shape but every NAME=value whose NAME looks like a credential is scrubbed.
  _py -c '
import json, re, sys
kind, allow = sys.argv[1], set(sys.argv[2].split())
data = json.load(sys.stdin)
SECRET_ASSIGN = re.compile(r"(?i)((?:token|key|secret|pass(?:word)?)[^=\s]*=)\S+")
URL_USERINFO = re.compile(r"(?i)([a-z][a-z0-9+.-]*://)[^/@\s]*@")
def strip_userinfo(v):
    return URL_USERINFO.sub(r"\1<redacted>@", v) if isinstance(v, str) else v
def scrub(v):
    if isinstance(v, str):
        return strip_userinfo(SECRET_ASSIGN.sub(r"\1<redacted>", v))
    if isinstance(v, list):
        return [scrub(x) for x in v]
    if isinstance(v, dict):
        # mappings (labels, build args, x-* blocks): a credential-like name
        # loses its scalar value outright
        return {k: ("<redacted>" if secretish(k) and not isinstance(x, (dict, list)) else scrub(x))
                for k, x in v.items()}
    return v
def secretish(name):
    return re.search(r"(?i)token|key|secret|pass(word)?", str(name)) is not None
scrub_map = scrub
def red_list(env):
    out = []
    for entry in env or []:
        key, sep, val = str(entry).partition("=")
        if key in allow:
            out.append(key + sep + strip_userinfo(val))
        else:
            out.append(key + "=<redacted>" if sep else key)
    return out
def red_map(env):
    return {k: (strip_userinfo(v) if k in allow else "<redacted>") for k, v in (env or {}).items()}
if kind == "inspect":
    for obj in data if isinstance(data, list) else [data]:
        cfg = obj.get("Config") or {}
        if "Env" in cfg:
            cfg["Env"] = red_list(cfg.get("Env"))
        for field in ("Cmd", "Entrypoint"):
            if cfg.get(field) is not None:
                cfg[field] = scrub(cfg[field])
        if cfg.get("Labels") is not None:
            cfg["Labels"] = scrub_map(cfg["Labels"])
        hc = cfg.get("Healthcheck")
        if isinstance(hc, dict) and hc.get("Test") is not None:
            hc["Test"] = scrub(hc["Test"])
        for field in ("Path", "Args"):
            if obj.get(field) is not None:
                obj[field] = scrub(obj[field])
else:
    for key in list(data):
        if str(key).startswith("x-"):
            data[key] = scrub(data[key])
    for svc in (data.get("services") or {}).values():
        if not isinstance(svc, dict):
            continue
        env = svc.get("environment")
        if isinstance(env, dict):
            svc["environment"] = red_map(env)
        elif isinstance(env, list):
            svc["environment"] = red_list(env)
        for field in ("command", "entrypoint"):
            if svc.get(field) is not None:
                svc[field] = scrub(svc[field])
        hc = svc.get("healthcheck")
        if isinstance(hc, dict) and hc.get("test") is not None:
            hc["test"] = scrub(hc["test"])
        if svc.get("labels") is not None:
            svc["labels"] = scrub_map(svc["labels"])
        build = svc.get("build")
        if isinstance(build, dict) and build.get("args") is not None:
            build["args"] = scrub_map(build["args"])
        for key in list(svc):
            if str(key).startswith("x-"):
                svc[key] = scrub(svc[key])
json.dump(data, sys.stdout, indent=1)
' "$1" "$REDACTION_ALLOWLIST"
}

# This script runs as root and copies files between the stack's data
# folders (writable by the containers) and the backup folder. A symbolic link
# planted in either place must never redirect a root-owned copy to or from
# an arbitrary host path, so every copy below goes through these helpers.

# copy_regular_file <src> <dst> [mode]: copies a regular, non-link file.
# Refuses a symlinked source or a symlinked destination folder. The copy is
# never written to <dst> by name (a link could be planted there between a
# check and the copy): it goes to a private staging folder next to <dst>,
# is re-checked and (optionally) chmod-ed there, then renamed over <dst>.
# rename(2) replaces a link at <dst> instead of writing through it.
copy_regular_file() {
  local src="$1" dst="$2" mode="${3:-}"
  if [[ -L "$src" || ! -f "$src" ]]; then
    warn "not copying ${src}: it is a symbolic link or not a regular file"
    return 1
  fi
  if [[ -L "$(dirname -- "$dst")" ]]; then
    warn "not copying to ${dst}: its folder is a symbolic link"
    return 1
  fi
  place_by_rename "$src" "$dst" "$mode"
}

# pinned_cd <dir>: cd into a folder this run just created and fail unless
# its real path is exactly <dir> (which callers build from a canonical root,
# never resolved again) and this run owns it. That refuses a swapped name and
# a link anywhere between the root and the folder. The target folders are
# writable by the containers' user, who can rename entries in them; once the
# cwd is the folder itself, later renames no longer matter, so callers work
# on ./ and ../ paths after this.
pinned_cd() {
  cd -- "$1" 2>/dev/null && [[ "$(pwd -P)" == "$1" && -O . ]]
}

# place_by_rename <src> <dst> [mode]: copy <src> (file or folder) without
# following links into a fresh 0700 staging folder in <dst>'s folder (same
# filesystem), entered with pinned_cd so a swapped stage cannot redirect
# the copy; refuse the copy if it is or holds anything but regular files
# and folders, then `mv -T` it onto exactly <dst>. Returns 1 (with a
# warning) instead of copying when any step fails.
# <dst>'s folder must be given as a canonical path (callers build it from
# canon_path roots); it is not resolved again, so a link planted anywhere on
# the way makes pinned_cd refuse. The final rename is ./item -> ../<name>
# from inside the pinned stage, so it lands next to the stage whatever
# happened to the path since.
place_by_rename() {
  local src="$1" dst="$2" mode="${3:-}" stage name rc=1
  src="$(canon_path "$(dirname -- "$src")")/$(basename -- "$src")"
  name="$(basename -- "$dst")"
  stage="$(mktemp -d "$(dirname -- "$dst")/.rollback-stage.XXXXXX")" || return 1
  if ( pinned_cd "$stage" \
      && cp -RPp -- "$src" ./item \
      && [[ -z "$(find ./item ! -type f ! -type d -print -quit)" ]] \
      && { [[ -z "$mode" ]] || chmod "$mode" ./item; } \
      && mv -fT -- ./item "../${name}" ); then
    rc=0
  else
    warn "${src} was not copied to ${dst}: the copy failed, its staging folder was replaced, or it is or contains a link or special file"
  fi
  rm -rf -- "$stage"
  return "$rc"
}

# tree_has_symlink <dir>: true if <dir> is a symlink or contains one.
tree_has_symlink() {
  [[ -L "$1" ]] && return 0
  [[ -d "$1" ]] || return 1
  [[ -n "$(find "$1" -type l -print -quit)" ]]
}

backup_state_files() {
  local data_src engine_src
  data_src="$(canon_path "$WEBMGR_DATA_SRC")"
  engine_src="$(canon_path "$ENGINE_CONFIG_SRC")"
  mkdir -p "$BACKUP_DIR/web-manager-data" "$BACKUP_DIR/beets-config"
  local f d manifest="$BACKUP_DIR/state-manifest.txt"
  : > "$manifest"
  for f in "${WEBMGR_STATE_FILES[@]}"; do
    if [[ -L "${data_src}/${f}" ]]; then
      warn "web-manager-data/${f} is a symbolic link -- not backed up"
      echo "web-manager-data/${f} skipped (symbolic link)" >> "$manifest"
    elif [[ -f "${data_src}/${f}" ]]; then
      cp -p -- "${data_src}/${f}" "$BACKUP_DIR/web-manager-data/${f}"
      echo "web-manager-data/${f} sha256=$(sha256_file "${data_src}/${f}")" >> "$manifest"
    else
      echo "web-manager-data/${f} absent" >> "$manifest"
    fi
  done
  for d in "${WEBMGR_STATE_DIRS[@]}"; do
    if [[ -L "${data_src}/${d}" ]]; then
      warn "web-manager-data/${d}/ is a symbolic link -- not backed up"
      echo "web-manager-data/${d}/ skipped (symbolic link)" >> "$manifest"
    elif [[ -d "${data_src}/${d}" ]]; then
      # -P: copy links as links (never follow them), then drop them.
      cp -RPp -- "${data_src}/${d}" "$BACKUP_DIR/web-manager-data/${d}"
      find "$BACKUP_DIR/web-manager-data/${d}" -type l -delete
      echo "web-manager-data/${d}/ files=$(find "${data_src}/${d}" -type f | wc -l | tr -d ' ')" >> "$manifest"
    fi
  done
  # Beets config only -- NEVER the library database (musiclibrary.blb and
  # its -wal/-shm are deliberately not in this list).
  if [[ -L "${engine_src}/config.yaml" ]]; then
    warn "Beets config.yaml is a symbolic link -- not backed up"
    echo "beets-config/config.yaml skipped (symbolic link)" >> "$manifest"
  elif [[ -f "${engine_src}/config.yaml" ]]; then
    cp -p -- "${engine_src}/config.yaml" "$BACKUP_DIR/beets-config/config.yaml"
    echo "beets-config/config.yaml sha256=$(sha256_file "${engine_src}/config.yaml")" >> "$manifest"
  fi
  if [[ -L "${engine_src}/beetsplug" ]]; then
    warn "Beets beetsplug/ is a symbolic link -- not backed up"
    echo "beets-config/beetsplug/ skipped (symbolic link)" >> "$manifest"
  elif [[ -d "${engine_src}/beetsplug" ]]; then
    cp -RPp -- "${engine_src}/beetsplug" "$BACKUP_DIR/beets-config/beetsplug"
    find "$BACKUP_DIR/beets-config/beetsplug" -type l -delete
    echo "beets-config/beetsplug/ files=$(find "${engine_src}/beetsplug" -type f | wc -l | tr -d ' ')" >> "$manifest"
  fi
  # Owner-only, whatever umask/ACLs the host applies.
  chmod -R go-rwx "$BACKUP_DIR"
  find "$BACKUP_DIR" -type f -exec chmod 600 {} +
  find "$BACKUP_DIR" -type d -exec chmod 700 {} +
}

# Restores what backup_state_files saved. Every file it replaces is first
# kept as <backup>/pre-rollback/<path> so the rollback itself is reversible.
restore_state_files() {
  local data_src engine_src pre="$ROLLBACK_DIR/pre-rollback-$(date -u +%Y%m%d-%H%M%S)"
  data_src="$(canon_path "$WEBMGR_DATA_SRC")"
  engine_src="$(canon_path "$ENGINE_CONFIG_SRC")"
  if [[ ! -d "$ROLLBACK_DIR/web-manager-data" && ! -d "$ROLLBACK_DIR/beets-config" ]]; then
    warn "backup has no web-manager-data/ or beets-config/ (made by an older version of this script) -- Web Manager settings and Beets config are left as they are"
    return 0
  fi
  ( umask 077; mkdir -p "$pre/web-manager-data" "$pre/beets-config" )
  local f
  for f in "${WEBMGR_STATE_FILES[@]}"; do
    if [[ -f "$ROLLBACK_DIR/web-manager-data/${f}" ]]; then
      if [[ -f "${data_src}/${f}" && ! -L "${data_src}/${f}" ]]; then
        cp -Pp -- "${data_src}/${f}" "$pre/web-manager-data/${f}"
      fi
      if copy_regular_file "$ROLLBACK_DIR/web-manager-data/${f}" "${data_src}/${f}"; then
        log "Restored web-manager-data/${f}"
      else
        warn "web-manager-data/${f} was NOT restored"
      fi
    elif [[ -f "${data_src}/${f}" ]] && grep -q "^web-manager-data/${f} absent$" "$ROLLBACK_DIR/state-manifest.txt" 2>/dev/null; then
      # Did not exist before the deploy: move it aside (never delete).
      mv "${data_src}/${f}" "$pre/web-manager-data/${f}"
      log "Moved web-manager-data/${f} (created after the deploy) aside to ${pre}/web-manager-data/"
    fi
  done
  if [[ -d "$ROLLBACK_DIR/web-manager-data/transactions" ]] && tree_has_symlink "${data_src}/transactions"; then
    warn "web-manager-data/transactions/ is or contains a symbolic link -- missing transaction records were NOT restored; copy them from ${ROLLBACK_DIR}/web-manager-data/transactions/ by hand after checking the folder"
  elif [[ -d "$ROLLBACK_DIR/web-manager-data/transactions" ]]; then
    mkdir -p "${data_src}/transactions"
    # Audit trail: add back missing records only; never overwrite or delete.
    # The transaction store keeps its records flat (<id>.json), so only
    # top-level *.json files are restored: a nested path would be resolved
    # through folders a container could replace with a link meanwhile.
    local added=0 src rel
    if [[ -n "$(find "$ROLLBACK_DIR/web-manager-data/transactions" -mindepth 2 -print -quit)" ]]; then
      warn "web-manager-data/transactions/ in the backup has subfolders -- only its top-level *.json records are restored"
    fi
    while IFS= read -r -d '' src; do
      rel="${src##*/}"
      if [[ ! -e "${data_src}/transactions/${rel}" && ! -L "${data_src}/transactions/${rel}" ]]; then
        if copy_regular_file "$src" "${data_src}/transactions/${rel}"; then
          added=$((added + 1))
        fi
      fi
    done < <(find "$ROLLBACK_DIR/web-manager-data/transactions" -mindepth 1 -maxdepth 1 -type f -name '*.json' -print0)
    log "transactions/: ${added} missing record(s) restored; records written after the deploy were kept."
  fi
  if [[ -f "$ROLLBACK_DIR/beets-config/config.yaml" ]]; then
    if [[ -f "${engine_src}/config.yaml" && ! -L "${engine_src}/config.yaml" ]]; then
      cp -Pp -- "${engine_src}/config.yaml" "$pre/beets-config/config.yaml"
    fi
    if copy_regular_file "$ROLLBACK_DIR/beets-config/config.yaml" "${engine_src}/config.yaml"; then
      log "Restored Beets config.yaml (takes effect at the engine's next start)."
    else
      warn "Beets config.yaml was NOT restored"
    fi
  fi
  if [[ -d "$ROLLBACK_DIR/beets-config/beetsplug" ]]; then
    if tree_has_symlink "${engine_src}/beetsplug" || tree_has_symlink "$ROLLBACK_DIR/beets-config/beetsplug"; then
      warn "Beets beetsplug/ (or its backup) is or contains a symbolic link -- plugin files were NOT restored; restore ${ROLLBACK_DIR}/beets-config/beetsplug/ by hand after checking the folder"
    else
      # Exact restore: files the new version added must not survive the
      # rollback (a stale module can shadow or break the old plugin). The
      # backed-up tree is copied (links never followed) into a private
      # staging folder inside the Beets config folder and checked there; the
      # current beetsplug/ is kept under pre-rollback/, renamed out of the
      # way and the staged copy renamed in. Nothing is copied into or
      # deleted from beetsplug/ by path, so a link planted inside it during
      # the rollback cannot redirect a write.
      local stage bp="${engine_src}/beetsplug" src_bp pre_abs rc=0
      src_bp="$(canon_path "$ROLLBACK_DIR/beets-config")/beetsplug"
      pre_abs="$(canon_path "$pre")"
      stage="$(mktemp -d "${engine_src}/.rollback-stage.XXXXXX")"
      # Exit codes: 2 = the staging folder was replaced, 3 = the backup holds
      # a link or special file, 4 = beetsplug/ is no longer a plain folder.
      ( pinned_cd "$stage" || exit 2
        cp -RPp -- "$src_bp" ./new || exit 1
        [[ -z "$(find ./new ! -type f ! -type d -print -quit)" ]] || exit 3
        if [[ -e "$bp" || -L "$bp" ]]; then
          [[ -d "$bp" && ! -L "$bp" ]] || exit 4
          cp -RPp -- "$bp" "$pre_abs/beets-config/beetsplug" || exit 1
          mv -fT -- "$bp" ./old || exit 1
        fi
        mv -fT -- ./new "$bp" ) || rc=$?
      rm -rf -- "$stage"
      case "$rc" in
        0) ;;
        2) die "the staging folder ${stage} was replaced while the rollback ran -- plugin files were NOT restored; check what else writes to ${engine_src}" ;;
        3) die "the backed-up beetsplug/ contains a link or special file -- plugin files were NOT restored; restore ${ROLLBACK_DIR}/beets-config/beetsplug/ by hand after checking it" ;;
        4) die "Beets beetsplug/ is not a plain folder any more (became a symbolic link?) -- plugin files were NOT restored; restore ${ROLLBACK_DIR}/beets-config/beetsplug/ by hand after checking the folder" ;;
        *) die "restoring Beets beetsplug/ failed -- check ${bp} and restore ${ROLLBACK_DIR}/beets-config/beetsplug/ by hand" ;;
      esac
      log "Restored Beets beetsplug/ exactly as backed up (current contents kept in ${pre}/beets-config/beetsplug/)."
    fi
  fi
  chmod -R go-rwx "$pre"
}

# Restores only the BEETS_WEB_MANAGER_VERSION line of the stack .env to the
# value it had before the deploy (persist_deployed_version rewrote it).
# Other lines are left alone: the operator may have edited them since.
restore_env_version_line() {
  local env_file prev_line="" prev_ver="" previous_image_ref="$1"
  env_file="$(dirname "$COMPOSE_FILE")/.env"
  if [[ -f "$ROLLBACK_DIR/.env.bak" ]]; then
    prev_line="$(grep '^BEETS_WEB_MANAGER_VERSION=' "$ROLLBACK_DIR/.env.bak" | tail -n 1 || true)"
  fi
  if [[ -n "$prev_line" ]]; then
    prev_ver="${prev_line#BEETS_WEB_MANAGER_VERSION=}"
  elif [[ "$previous_image_ref" == ghcr.io/iranman/beets-web-manager:* ]]; then
    prev_ver="${previous_image_ref##*:}"
  fi
  if [[ ! -f "$env_file" ]]; then
    [[ -z "$prev_line" ]] || warn "backup .env had BEETS_WEB_MANAGER_VERSION but ${env_file} no longer exists -- not recreating it"
    return 0
  fi
  if [[ -z "$prev_line" ]] && ! compose_image_line_uses_version_variable; then
    # The Compose file does not use the variable (literal :latest or a
    # pinned tag): the .env is not part of the image choice; leave it alone.
    return 0
  fi
  if ! [[ "$prev_ver" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-[a-zA-Z0-9\.-]+)?$ ]]; then
    warn "could not determine a numbered previous version for ${env_file} (got '${prev_ver}') -- BEETS_WEB_MANAGER_VERSION left as is; the post-rollback check below decides whether that is safe"
    return 0
  fi
  local tmp="${env_file}.tmp.$$"
  if grep -q '^BEETS_WEB_MANAGER_VERSION=' "$env_file"; then
    sed "s/^BEETS_WEB_MANAGER_VERSION=.*/BEETS_WEB_MANAGER_VERSION=${prev_ver}/" "$env_file" > "$tmp"
  else
    cp "$env_file" "$tmp"
    printf '\nBEETS_WEB_MANAGER_VERSION=%s\n' "$prev_ver" >> "$tmp"
  fi
  chmod 600 "$tmp"
  mv "$tmp" "$env_file"
  log "Restored BEETS_WEB_MANAGER_VERSION=${prev_ver} in ${env_file}"
}

# ---------------------------------------------------------------------------
# Backup retention (opt-in only)
# ---------------------------------------------------------------------------
run_prune_backups() {
  STAGE="prune-backups"
  [[ -d "$BACKUP_ROOT" ]] || { log "No backup directory at ${BACKUP_ROOT} -- nothing to prune."; return 0; }
  local cutoff newest="" d name stamp
  cutoff="$(_py -c 'import sys,datetime; print((datetime.datetime.utcnow()-datetime.timedelta(days=int(sys.argv[1]))).strftime("%Y%m%d-%H%M%S"))' "$PRUNE_DAYS")"
  # Only directories this script created, by exact name pattern; the
  # timestamp in the NAME decides age (mtime can be touched by copies).
  local candidates=()
  for d in "$BACKUP_ROOT"/web-manager-rollout-*; do
    [[ -d "$d" ]] || continue
    name="$(basename "$d")"
    [[ "$name" =~ ^web-manager-rollout-([0-9]{8}-[0-9]{6})$ ]] || continue
    candidates+=("$name")
  done
  [[ ${#candidates[@]} -gt 0 ]] || { log "No rollout backups under ${BACKUP_ROOT}."; return 0; }
  newest="$(printf '%s\n' "${candidates[@]}" | sort | tail -n 1)"
  local pruned=0 kept=0
  for name in "${candidates[@]}"; do
    stamp="${name#web-manager-rollout-}"
    if [[ "$name" == "$newest" || ! "$stamp" < "$cutoff" ]]; then
      kept=$((kept + 1)); continue
    fi
    if [[ -d "$BACKUP_ROOT/$name/stale-database" ]]; then
      warn "keeping ${name}: it holds an archived stale database (stale-database/) -- review and remove it by hand"
      kept=$((kept + 1)); continue
    fi
    if [[ "$DRY_RUN" -eq 1 ]]; then
      log "would delete ${BACKUP_ROOT}/${name}"
    else
      rm -rf -- "${BACKUP_ROOT:?}/${name:?}"
      log "deleted ${BACKUP_ROOT}/${name}"
    fi
    pruned=$((pruned + 1))
  done
  log "Retention (${PRUNE_DAYS} days): $([[ "$DRY_RUN" -eq 1 ]] && echo 'would delete' || echo 'deleted') ${pruned}, kept ${kept} (the newest backup is always kept)."
}

# ---------------------------------------------------------------------------
# Phase B -- Dry-run entry point
# ---------------------------------------------------------------------------
# Recreate and rollback run `up --pull never` (Docker Compose v2.22+). An
# older Compose rejects the flag, which would fail the recreate after the
# service was stopped, so this is checked before anything changes.
require_compose_pull_flag() {
  STAGE="compose-version-check"
  # Capture first: piping into `grep -q` can SIGPIPE docker under pipefail.
  local up_help
  up_help="$(docker compose up --help 2>/dev/null)" || up_help=""
  if [[ "$up_help" != *"--pull"* ]]; then
    REASON_CODE="compose_too_old"
    die "this Docker Compose ($(docker compose version --short 2>/dev/null || echo unknown version)) does not support 'up --pull'; Docker Compose v2.22 or later is required. Nothing was changed -- update Docker Compose, then re-run."
  fi
}

run_dry_run() {
  log "=== DRY RUN: no containers will be stopped/recreated, no files moved, no tokens copied, no Compose changes ==="
  validate_version
  resolve_compose_file
  require_compose_pull_flag
  discover_and_verify_mounts
  verify_compose_image
  verify_authoritative_database
  inspect_stale_database
  inspect_auth_token
  plan_backup_dir
  log "Pulling image for label verification only (no recreate)..."
  export BEETS_WEB_MANAGER_VERSION="$VERSION"
  record_pre_pull_latest_tag
  _compose pull "$SERVICE" >&2 || warn "image pull failed in dry-run (network/registry issue) -- label verification skipped"
  verify_image_labels_if_present || true
  restore_pre_pull_latest_tag
  [[ "$IMAGE_LAYOUT" != "latest" ]] || log "Compose uses ${LATEST_IMAGE}: the real run deploys it only if its version label is ${VERSION} (checked above)."
  if docker inspect --format '{{.State.Status}}' "$WEBMGR_CID" >/dev/null 2>&1; then
    verify_endpoints "dry-run" || warn "endpoint verification reported issues (see above) -- not fatal in dry-run"
    record_setup_status_before
  else
    log "Service '${SERVICE}' is not currently running -- skipping live endpoint checks."
  fi
  log "Running engine plugin: $(snapshot_field "$AUTH_SEMANTIC" plugin_version). The real run compares it with the plugin the new image provisions and restarts ${ENGINE_SERVICE} only if they differ."
  log "=== DRY RUN COMPLETE: all checks passed. Nothing in the stack was changed (the image may have been pulled into the local image store; probe bodies went to temp files). ==="
}

# Verifies the labels of the image DEPLOY_REF points at locally and records
# its ID in DEPLOY_IMAGE_ID (the recreate must land on exactly that image).
verify_image_labels_if_present() {
  local img_id revision version
  img_id="$(docker image inspect "$DEPLOY_REF" --format '{{.Id}}' 2>/dev/null || true)"
  [[ -n "$img_id" ]] || { warn "image ${DEPLOY_REF} not present locally"; return 1; }
  revision="$(docker image inspect "$DEPLOY_REF" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' 2>/dev/null || true)"
  version="$(docker image inspect "$DEPLOY_REF" --format '{{index .Config.Labels "org.opencontainers.image.version"}}' 2>/dev/null || true)"
  if [[ "$version" != "$VERSION" ]]; then
    if [[ "$IMAGE_LAYOUT" == "latest" ]]; then
      REASON_CODE="latest_image_not_requested_version"
      die "${LATEST_IMAGE} carries version '${version}', not '${VERSION}' -- ${VERSION} is not published as latest yet, or a newer release is. Nothing in the stack was changed. Retry once :latest is ${VERSION}, or deploy the version :latest carries."
    fi
    REASON_CODE="image_version_label_mismatch"
    die "image org.opencontainers.image.version label is '${version}', expected '${VERSION}'"
  fi
  if [[ -n "$EXPECTED_REVISION" ]]; then
    if [[ "$revision" != "$EXPECTED_REVISION" ]]; then
      REASON_CODE="image_revision_label_mismatch"
      die "image org.opencontainers.image.revision label is '${revision}', expected '${EXPECTED_REVISION}'"
    fi
  else
    warn "EXPECTED_REVISION not set -- skipping the exact revision-label pin (relying on the version label '${version}' alone). Set EXPECTED_REVISION=<release commit sha> once it's known for a fully pinned deployment."
  fi
  DEPLOY_IMAGE_ID="$img_id"
  log "Image labels verified: ${DEPLOY_REF} version=${version} revision=${revision:-<none>} id=${img_id}"
}

# Pull and verify BEFORE anything in the stack changes: a :latest tag that
# does not carry VERSION must stop the rollout with production untouched.
pull_and_verify_image() {
  STAGE="image-pull-verification"
  record_pre_pull_latest_tag
  log "Pulling ${DEPLOY_REF}..."
  _compose pull "$SERVICE"
  verify_image_labels_if_present || die "image ${DEPLOY_REF} is not present after the pull"
}

# ---------------------------------------------------------------------------
# Phase C -- Real rollout (mutating)
# ---------------------------------------------------------------------------
create_backup_dir() {
  STAGE="backup-creation"
  ( umask 077; mkdir -p "$BACKUP_DIR" )
  chmod 700 "$BACKUP_DIR"

  cp "$COMPOSE_FILE" "$BACKUP_DIR/docker-compose.yml.bak"
  local env_file
  env_file="$(dirname "$COMPOSE_FILE")/.env"
  if [[ -f "$env_file" ]]; then
    # Verbatim: rollback restores BEETS_WEB_MANAGER_VERSION from it.
    cp "$env_file" "$BACKUP_DIR/.env.bak"
    chmod 600 "$BACKUP_DIR/.env.bak"
  fi
  # Diagnostic copies only (rollback never reads them): environment VALUES
  # are redacted except for REDACTION_ALLOWLIST; key names are kept.
  compose_config_json 2>/dev/null | redact_json compose > "$BACKUP_DIR/resolved-compose-config.json" 2>/dev/null \
    || { rm -f "$BACKUP_DIR/resolved-compose-config.json"; warn "could not save a redacted resolved Compose config"; }
  docker inspect "$WEBMGR_CID" 2>/dev/null | redact_json inspect > "$BACKUP_DIR/container-inspect-before.json" 2>/dev/null \
    || { rm -f "$BACKUP_DIR/container-inspect-before.json"; warn "could not save a redacted container inspect"; }

  PREVIOUS_IMAGE_ID="$(docker inspect --format '{{.Image}}' "$WEBMGR_CID" 2>/dev/null || echo "")"
  local previous_image_ref
  previous_image_ref="$(docker inspect --format '{{.Config.Image}}' "$WEBMGR_CID" 2>/dev/null || echo "")"
  # The registry digest lets --rollback pull the previous image back by
  # digest if it was pruned after a moving tag (:latest) left it untagged.
  local previous_repo_digest=""
  previous_repo_digest="$(docker image inspect "$PREVIOUS_IMAGE_ID" --format '{{json .RepoDigests}}' 2>/dev/null | _py -c '
import json, sys
try:
    digests = json.load(sys.stdin) or []
except Exception:
    digests = []
print(next((d for d in digests if d.startswith(sys.argv[1] + "@sha256:")), ""))' "$IMAGE_REPO" || true)"
  {
    echo "previous_image_id=${PREVIOUS_IMAGE_ID}"
    echo "previous_image_ref=${previous_image_ref}"
    echo "previous_image_repo_digest=${previous_repo_digest}"
  } > "$BACKUP_DIR/previous-image.txt"
  docker inspect "$PREVIOUS_IMAGE_ID" --format '{{json .Config.Labels}}' > "$BACKUP_DIR/previous-image-labels.json" 2>/dev/null || true

  {
    echo "auth_db_path=${AUTH_DB_PATH}"
    echo "auth_db_size=${AUTH_DB_SIZE}"
    echo "auth_db_sha256=${AUTH_DB_SHA256}"
    echo "auth_db_inode=${AUTH_DB_INODE}"
    echo "auth_db_owner=${AUTH_DB_OWNER}"
    echo "auth_db_mode=${AUTH_DB_MODE}"
    echo "auth_item_count=${AUTH_ITEM_COUNT}"
    echo "auth_album_count=${AUTH_ALBUM_COUNT}"
  } > "$BACKUP_DIR/authoritative-db-metadata.txt"

  local persistent_existed=0 legacy_existed=0 persistent_sha="" legacy_sha=""
  [[ ! -L "$TOKEN_PATH" ]] || die "the auth token path (${TOKEN_PATH}) is a symbolic link -- refusing to back it up; replace it with a regular file first"
  if [[ -f "$TOKEN_PATH" ]]; then
    persistent_existed=1
    persistent_sha="$(sha256_file "$TOKEN_PATH")"
    cp "$TOKEN_PATH" "$BACKUP_DIR/auth_token.bak"
    chmod 600 "$BACKUP_DIR/auth_token.bak"
  fi
  if [[ -n "$LEGACY_TOKEN_PATH" && -f "$LEGACY_TOKEN_PATH" ]]; then
    legacy_existed=1
    legacy_sha="$(sha256_file "$LEGACY_TOKEN_PATH")"
  fi
  {
    echo "persistent_token_existed_before=${persistent_existed}"
    echo "legacy_token_existed_before=${legacy_existed}"
    echo "token_migration_planned=${NEEDS_TOKEN_MIGRATION}"
    echo "persistent_token_path=${TOKEN_PATH}"
    echo "legacy_token_path=${LEGACY_TOKEN_PATH}"
    echo "persistent_token_sha256=${persistent_sha}"
    echo "legacy_token_sha256=${legacy_sha}"
  } > "$BACKUP_DIR/token-metadata.txt"
  chmod 600 "$BACKUP_DIR/token-metadata.txt"

  [[ -n "$SETUP_STATUS_BEFORE" ]] && printf '%s\n' "$SETUP_STATUS_BEFORE" > "$BACKUP_DIR/setup-status-before.json"
  find "$BACKUP_DIR" -type f -exec chmod 600 {} +

  log "Backup created at ${BACKUP_DIR}"
}

# Runs after the web manager is stopped, so its state files are quiescent.
backup_web_manager_and_beets_config() {
  STAGE="backup-state"
  backup_state_files
  log "Backed up Web Manager state ($(printf '%s ' "${WEBMGR_STATE_FILES[@]}")transactions/) and Beets config.yaml + beetsplug/ (never the library database) -- see ${BACKUP_DIR}/state-manifest.txt"
}

stop_web_manager() {
  STAGE="stop-web-manager"
  log "Stopping ${SERVICE} (only this service)..."
  _compose stop "$SERVICE"
  local status
  status="$(docker inspect --format '{{.State.Status}}' "$WEBMGR_CID" 2>/dev/null || echo "unknown")"
  [[ "$status" == "exited" || "$status" == "created" ]] || die "container '${SERVICE}' did not stop cleanly (status=${status})"
  log "Confirmed ${SERVICE} is stopped (status=${status})."
}

archive_stale_database() {
  STAGE="archive-stale-database"
  [[ "$STALE_DB_EXISTS" -eq 1 ]] || { log "No stale database to archive."; return 0; }

  for f in "$STALE_DB_PATH" "$STALE_WAL_PATH" "$STALE_SHM_PATH"; do
    if [[ -f "$f" ]]; then
      if file_is_open "$f"; then
        die "stale file '${f}' is still open by a process after stopping ${SERVICE} -- refusing to move it"
      fi
    fi
  done

  mkdir -p "$BACKUP_DIR/stale-database"
  chmod 700 "$BACKUP_DIR/stale-database"
  [[ -f "$STALE_DB_PATH" ]]  && mv "$STALE_DB_PATH" "$BACKUP_DIR/stale-database/${DB_FILENAME}"
  [[ -f "$STALE_WAL_PATH" ]] && mv "$STALE_WAL_PATH" "$BACKUP_DIR/stale-database/${WAL_FILENAME}"
  [[ -f "$STALE_SHM_PATH" ]] && mv "$STALE_SHM_PATH" "$BACKUP_DIR/stale-database/${SHM_FILENAME}"
  log "Stale database archived to ${BACKUP_DIR}/stale-database/ (moved, not deleted)."
}

migrate_token_if_needed() {
  STAGE="token-migration"
  if [[ "$TOKEN_EXISTS" -eq 1 ]]; then
    log "Persistent token already present -- no migration needed."
    return 0
  fi
  [[ "$NEEDS_TOKEN_MIGRATION" -eq 1 ]] || { log "No legacy token candidate and no existing token -- app will bootstrap a fresh one on first start."; return 0; }

  [[ ! -e "$TOKEN_PATH" ]] || die "refusing to overwrite existing destination token at ${TOKEN_PATH}"

  local legacy_val api_token_val
  legacy_val="$(cat "$LEGACY_TOKEN_PATH")"
  api_token_val="${BEETS_API_TOKEN:-}"
  if [[ -n "$api_token_val" && "$legacy_val" == "$api_token_val" ]]; then
    unset legacy_val api_token_val
    die "legacy token candidate at ${LEGACY_TOKEN_PATH} is identical to BEETS_API_TOKEN -- never using the Beets engine API token as the web auth token; refusing migration"
  fi
  unset legacy_val api_token_val
  [[ "$(file_size "$LEGACY_TOKEN_PATH")" -gt 0 ]] || die "legacy token candidate at ${LEGACY_TOKEN_PATH} is empty -- refusing migration"

  local tmp="${TOKEN_PATH}.migrate.tmp.$$"
  cp "$LEGACY_TOKEN_PATH" "$tmp"
  chmod 600 "$tmp"
  mv "$tmp" "$TOKEN_PATH"

  local src_sha dst_sha
  src_sha="$(sha256_file "$LEGACY_TOKEN_PATH")"
  dst_sha="$(sha256_file "$TOKEN_PATH")"
  [[ "$src_sha" == "$dst_sha" ]] || die "token migration checksum mismatch after copy -- source and destination differ"

  TOKEN_EXISTS=1
  TOKEN_SHA256="$dst_sha"
  ACTIVE_AUTH_TOKEN_PATH="$TOKEN_PATH"
  {
    echo "token_migration_performed=1"
    echo "migrated_token_sha256=${dst_sha}"
  } >> "$BACKUP_DIR/token-metadata.txt" 2>/dev/null || true
  log "Legacy token migrated to ${TOKEN_PATH} (checksum verified, contents never printed)."
}

deploy_image() {
  STAGE="image-deployment"
  export BEETS_WEB_MANAGER_VERSION="$VERSION"
  # Pulled and verified in pre-flight. A moving tag must still point at that
  # exact image now; it is not pulled again.
  local tag_id
  tag_id="$(docker image inspect "$DEPLOY_REF" --format '{{.Id}}' 2>/dev/null || true)"
  if [[ "$tag_id" != "$DEPLOY_IMAGE_ID" ]]; then
    REASON_CODE="image_tag_moved"
    die "${DEPLOY_REF} now points at ${tag_id:-nothing}, not the verified image ${DEPLOY_IMAGE_ID} -- refusing to recreate on an unverified image"
  fi

  local other_services other_before other_after
  other_services="$(compose_config_json | _py -c "
import json, sys
data = json.load(sys.stdin)
print('\n'.join(s for s in data.get('services', {}) if s != '$SERVICE'))
")"
  other_before="$(for s in $other_services; do printf '%s=%s\n' "$s" "$(_compose ps -q "$s" 2>/dev/null || true)"; done)"

  RETAG_LATEST_PENDING=0  # from here on, --rollback is the way back
  log "Recreating ${SERVICE} only (--no-deps --force-recreate)..."
  # --pull never: `up` must not fetch a tag that moved after verification
  # (a `pull_policy: always` in the Compose file, or a concurrent pull).
  _compose up -d --no-deps --pull never --force-recreate "$SERVICE"

  other_after="$(for s in $other_services; do printf '%s=%s\n' "$s" "$(_compose ps -q "$s" 2>/dev/null || true)"; done)"
  [[ "$other_before" == "$other_after" ]] || die "a service other than '${SERVICE}' changed container ID during recreate -- this must never happen: before=[${other_before}] after=[${other_after}]"

  WEBMGR_CID="$(resolve_container_id "$SERVICE")"
  local configured_image running_image_id
  configured_image="$(docker inspect --format '{{.Config.Image}}' "$WEBMGR_CID")"
  running_image_id="$(docker inspect --format '{{.Image}}' "$WEBMGR_CID")"
  if [[ "$configured_image" != "$DEPLOY_REF" || "$running_image_id" != "$DEPLOY_IMAGE_ID" ]]; then
    REASON_CODE="recreated_image_unverified"
    _compose stop "$SERVICE" >&2 || warn "could not stop ${SERVICE} -- stop it by hand"
    die "recreated ${SERVICE} runs '${configured_image}' (${running_image_id}), not the pulled and verified ${DEPLOY_REF} (${DEPLOY_IMAGE_ID}) -- ${SERVICE} was stopped so the unverified image does not keep running. Roll back with: $0 --rollback ${BACKUP_DIR}"
  fi

  log "Waiting up to ${HEALTH_TIMEOUT_SECONDS}s for ${SERVICE} to become healthy..."
  if ! wait_for_health "$WEBMGR_CID" "$HEALTH_TIMEOUT_SECONDS"; then
    die "container did not become healthy within ${HEALTH_TIMEOUT_SECONDS}s"
  fi
  log "${SERVICE} is healthy on image ${DEPLOY_REF} (version ${VERSION}, id=${running_image_id})."
}

assert_authoritative_db_unchanged() {
  local snap item_count album_count digest sha
  snap="$(checked_semantic_snapshot "post-deploy")"
  item_count="$(snapshot_field "$snap" items)"
  album_count="$(snapshot_field "$snap" albums)"
  digest="$(snapshot_field "$snap" digest)"
  [[ "$item_count" == "$AUTH_ITEM_COUNT" ]] || die "authoritative item count changed: was ${AUTH_ITEM_COUNT}, now ${item_count}"
  [[ "$album_count" == "$AUTH_ALBUM_COUNT" ]] || die "authoritative album count changed: was ${AUTH_ALBUM_COUNT}, now ${album_count}"
  [[ "$digest" == "$AUTH_SEMANTIC_DIGEST" ]] || die "library identity digest changed during deploy: was ${AUTH_SEMANTIC_DIGEST}, now ${digest}"
  sha="$(sha256_file "$AUTH_DB_PATH")"
  log "ONLINE SEMANTIC INTEGRITY confirmed post-deploy (items=${item_count} albums=${album_count} digest=${digest})."
  log "  live main-file sha256 before=${AUTH_DB_SHA256} after=${sha} (informational only; NOT a byte-identity claim -- use --offline-db-identity)"
}

assert_no_local_db_recreated() {
  local f
  for f in "$STALE_DB_PATH" "$STALE_WAL_PATH" "$STALE_SHM_PATH"; do
    [[ ! -e "$f" ]] || die "a local database file reappeared after deployment: ${f} -- local DB fallback may have regressed"
  done
  log "Confirmed no local database was recreated under ${WEBMGR_DATA_SRC}."
}

verify_post_deploy() {
  STAGE="post-deploy-verification"

  assert_authoritative_db_unchanged

  refresh_engine_plugin_if_stale
  STAGE="post-deploy-verification"

  local engine_status
  engine_status="$(docker inspect --format '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}(no healthcheck){{end}}' "$ENGINE_CID")"
  log "Beets engine container (same container ID) reports: ${engine_status}"

  local first_sha
  first_sha="$(sha256_file "$TOKEN_PATH")"
  log "Restarting ${SERVICE} a second time to confirm token persistence..."
  _compose restart "$SERVICE"
  WEBMGR_CID="$(resolve_container_id "$SERVICE")"
  wait_for_health "$WEBMGR_CID" "$HEALTH_TIMEOUT_SECONDS" || die "container did not become healthy after the confirmation restart"
  local second_sha
  second_sha="$(sha256_file "$TOKEN_PATH")"
  [[ "$first_sha" == "$second_sha" ]] || die "auth token checksum changed across restart -- token is not persisting (was ${first_sha}, now ${second_sha})"
  log "Token checksum unchanged across restart (persistence confirmed)."

  assert_no_local_db_recreated

  verify_endpoints "post-deploy"

  assert_no_new_setup_blocking_reasons
}

# Durably persist the deployed VERSION into .env's own BEETS_WEB_MANAGER_VERSION
# line -- verify_compose_image() only ever `export`s it for this script's own
# subprocesses, which resolves the image correctly for THIS run but leaves the
# on-disk file pointing at whatever version was there before. Anything that
# recreates the container without going through this script (a host reboot,
# a routine `docker compose pull && up -d` across the whole stack, TrueNAS's
# own app supervisor) then falls back to that stale on-disk value and silently
# redeploys an older image. Confirmed live: a routine stack-wide compose
# refresh reverted beets-web-manager from 0.1.22 back to 0.1.19 this way,
# because this line was never added after the very first time the drift fix
# was applied. Only called after verify_post_deploy has confirmed the new
# version is actually healthy -- never persist a version that didn't verify.
persist_deployed_version() {
  STAGE="persist-version"
  if [[ "$IMAGE_LAYOUT" == "latest" ]]; then
    log "Compose uses ${LATEST_IMAGE}: nothing is written to .env or the Compose file. A later 'docker compose pull' moves to whatever :latest is then."
    return 0
  fi
  local env_file
  env_file="$(dirname "$COMPOSE_FILE")/.env"
  [[ -f "$env_file" ]] || { warn "no .env file at ${env_file} -- BEETS_WEB_MANAGER_VERSION not persisted (in-process export for this run only)"; return 0; }

  local tmp
  tmp="${env_file}.tmp.$$"
  if grep -q '^BEETS_WEB_MANAGER_VERSION=' "$env_file"; then
    sed "s/^BEETS_WEB_MANAGER_VERSION=.*/BEETS_WEB_MANAGER_VERSION=${VERSION}/" "$env_file" > "$tmp"
  else
    cp "$env_file" "$tmp"
    printf '\nBEETS_WEB_MANAGER_VERSION=%s\n' "$VERSION" >> "$tmp"
  fi
  chmod 600 "$tmp"
  mv "$tmp" "$env_file"
  log "Persisted BEETS_WEB_MANAGER_VERSION=${VERSION} to ${env_file} (survives future recreations not run through this script)."
}

# ---------------------------------------------------------------------------
# OFFLINE BYTE IDENTITY (explicit mode; briefly stops the Beets engine)
# ---------------------------------------------------------------------------
wait_for_engine_semantics() {
  local deadline=$((SECONDS + HEALTH_TIMEOUT_SECONDS)) snap
  while (( SECONDS < deadline )); do
    if snap="$(beets_semantic_snapshot 2>/dev/null)" && [[ -n "$(snapshot_field "$snap" plugin_version 2>/dev/null)" ]]; then
      printf '%s\n' "$snap"
      return 0
    fi
    sleep 3
  done
  return 1
}

# Settles a WAL-mode database on a private COPY: main file + WAL are copied
# into a temp dir, the copy is checkpointed and quick_checked, and the
# settled copy is hashed. The authoritative files are only read.
settle_copy_and_hash() {
  local db="$1" wal="$2" tmp
  tmp="$(mktemp -d)"
  cp -p "$db" "${tmp}/${DB_FILENAME}"
  [[ -e "$wal" ]] && cp -p "$wal" "${tmp}/${WAL_FILENAME}"
  _py - "${tmp}/${DB_FILENAME}" <<'PYEOF'
import hashlib, sqlite3, sys
path = sys.argv[1]
con = sqlite3.connect(path)
try:
    busy, log_frames, checkpointed = con.execute("PRAGMA wal_checkpoint(TRUNCATE);").fetchone()
    check = con.execute("PRAGMA quick_check;").fetchone()[0]
finally:
    con.close()
if busy:
    print("busy"); sys.exit(2)
digest = hashlib.sha256(open(path, "rb").read()).hexdigest()
print(f"{check}|{digest}|{log_frames}|{checkpointed}")
PYEOF
  local rc=$?
  rm -rf -- "$tmp"
  return $rc
}

run_offline_db_identity() {
  log "=== Offline database byte-identity check (the Beets engine is stopped briefly) ==="
  resolve_compose_file
  discover_and_verify_mounts
  STAGE="offline-db-identity"
  local before after wal_path shm_path wal_size shm_size settled quick_check sha status method
  local orig_main_sha orig_wal_sha result frames
  before="$(checked_semantic_snapshot "before stop")"
  wal_path="$(dirname "$AUTH_DB_PATH")/${WAL_FILENAME}"
  shm_path="$(dirname "$AUTH_DB_PATH")/${SHM_FILENAME}"

  log "Stopping ${ENGINE_SERVICE} gracefully..."
  ENGINE_STOPPED_BY_US=1
  _compose stop -t 60 "$ENGINE_SERVICE" >&2
  status="$(docker inspect --format '{{.State.Status}}' "$ENGINE_CID")"
  [[ "$status" == "exited" ]] || die "${ENGINE_SERVICE} did not stop (status=${status})"

  wal_size="absent"; shm_size="absent"
  [[ -e "$wal_path" ]] && wal_size="$(file_size "$wal_path")"
  [[ -e "$shm_path" ]] && shm_size="$(file_size "$shm_path")"
  settled=1
  [[ "$wal_size" != "absent" && "$wal_size" != "0" ]] && settled=0

  orig_main_sha="$(sha256_file "$AUTH_DB_PATH")"
  orig_wal_sha="absent"
  [[ -e "$wal_path" ]] && orig_wal_sha="$(sha256_file "$wal_path")"
  if [[ "$settled" -eq 1 ]]; then
    method="main file (WAL empty or absent)"
    quick_check="$(sqlite_ro_query "$AUTH_DB_PATH" 'PRAGMA quick_check;')" || die "offline PRAGMA quick_check failed to execute"
    sha="$orig_main_sha"
  else
    # Beets' web server does not close SQLite on stop, so its WAL is left
    # unsettled. Never checkpoint the authoritative file: settle a copy.
    warn "WAL holds ${wal_size} bytes after stop; settling a private copy (the authoritative files are only read)."
    method="settled copy of main file + WAL (checkpointed off to the side)"
    result="$(settle_copy_and_hash "$AUTH_DB_PATH" "$wal_path")" || die "could not settle a copy of the database"
    quick_check="${result%%|*}"
    sha="$(printf '%s' "$result" | cut -d'|' -f2)"
    frames="$(printf '%s' "$result" | cut -d'|' -f3-)"
  fi
  [[ "$quick_check" == "ok" ]] || die "offline PRAGMA quick_check is not ok: '${quick_check}'"
  [[ "$(sha256_file "$AUTH_DB_PATH")" == "$orig_main_sha" ]] || die "the authoritative database file changed while the engine was stopped"
  if [[ -e "$wal_path" ]]; then
    [[ "$(sha256_file "$wal_path")" == "$orig_wal_sha" ]] || die "the authoritative WAL changed while the engine was stopped"
  fi

  log "Restarting ${ENGINE_SERVICE}..."
  _compose start "$ENGINE_SERVICE" >&2
  ENGINE_STOPPED_BY_US=0
  after="$(wait_for_engine_semantics)" || die "${ENGINE_SERVICE} did not come back with a healthy webmanager plugin within ${HEALTH_TIMEOUT_SECONDS}s"
  [[ "$(snapshot_field "$before" digest)" == "$(snapshot_field "$after" digest)" ]] \
    || die "library identity digest differs across the stop/start -- investigate before trusting this check"

  local verdict="n/a (no BASELINE_DB_SHA256 given)"
  if [[ -n "${BASELINE_DB_SHA256:-}" ]]; then
    if [[ "$sha" == "$BASELINE_DB_SHA256" ]]; then verdict="IDENTICAL to baseline"; else verdict="DIFFERENT from baseline"; fi
  fi
  {
    echo "OFFLINE BYTE IDENTITY"
    echo "  engine stopped cleanly:   yes"
    echo "  WAL (${WAL_FILENAME}):     ${wal_size}"
    echo "  SHM (${SHM_FILENAME}):     ${shm_size}"
    echo "  WAL settled by engine:    $([[ "$settled" -eq 1 ]] && echo yes || echo "NO (settled on a private copy${frames:+; wal frames/checkpointed ${frames/|//}})")"
    echo "  hashed:                   ${method}"
    echo "  PRAGMA quick_check:       ${quick_check}"
    echo "  database sha256:          ${sha}"
    echo "  authoritative files:      unchanged (main ${orig_main_sha:0:16}..., WAL ${orig_wal_sha:0:16}...)"
    echo "  baseline comparison:      ${verdict}"
    echo "ONLINE SEMANTIC INTEGRITY (before stop / after restart)"
    echo "  items:  $(snapshot_field "$before" items) / $(snapshot_field "$after" items)"
    echo "  albums: $(snapshot_field "$before" albums) / $(snapshot_field "$after" albums)"
    echo "  digest: $(snapshot_field "$before" digest) / $(snapshot_field "$after" digest)"
    echo "  plugin: $(snapshot_field "$after" plugin_version)"
  }
  log "=== Offline check complete; ${ENGINE_SERVICE} restarted and verified. ==="
}

run_deploy() {
  log "=== Beets Web Manager ${VERSION} guarded rollout starting ==="
  validate_version
  resolve_compose_file
  require_compose_pull_flag
  discover_and_verify_mounts
  verify_compose_image
  verify_authoritative_database
  inspect_stale_database
  inspect_auth_token
  record_setup_status_before
  plan_backup_dir
  pull_and_verify_image
  log "=== All pre-flight safety checks passed. Beginning mutating actions. ==="

  create_backup_dir
  stop_web_manager
  backup_web_manager_and_beets_config
  archive_stale_database
  migrate_token_if_needed
  deploy_image
  verify_post_deploy
  persist_deployed_version

  log "=== Rollout of ${VERSION} completed successfully. Backup at ${BACKUP_DIR} ==="
}

# ---------------------------------------------------------------------------
# Rollback mode
# ---------------------------------------------------------------------------
run_rollback() {
  STAGE="rollback"
  [[ -d "$ROLLBACK_DIR" ]] || die "rollback directory does not exist: ${ROLLBACK_DIR}"
  [[ -f "$ROLLBACK_DIR/docker-compose.yml.bak" ]] || die "rollback directory is missing docker-compose.yml.bak -- not a valid backup from this script"

  resolve_compose_file
  require_compose_pull_flag
  discover_and_verify_mounts

  log "Stopping ${SERVICE} for rollback..."
  # A failed stop is not fatal on its own: the recreate below uses
  # --force-recreate (which replaces a running container) and the rollback is
  # only declared complete after the running image ID, the configured image,
  # the Compose resolution and /health/live version are all PROVEN to be the
  # previous release. It is reported, never hidden.
  local stop_rc=0
  _compose stop "$SERVICE" || stop_rc=$?
  if [[ "$stop_rc" -ne 0 ]]; then
    warn "'docker compose stop ${SERVICE}' failed (exit ${stop_rc}) -- continuing the rollback; the forced recreate and the image/version proof below decide whether it succeeded"
  fi

  log "Restoring Compose file from backup..."
  cp "$ROLLBACK_DIR/docker-compose.yml.bak" "$COMPOSE_FILE"

  if [[ -L "$TOKEN_PATH" ]]; then
    warn "the auth token path (${TOKEN_PATH}) is a symbolic link -- token left untouched; restore ${ROLLBACK_DIR}/auth_token.bak by hand after checking it"
  elif [[ -f "$ROLLBACK_DIR/token-metadata.txt" ]]; then
    local p_existed l_existed migration_performed p_sha m_sha meta_p_path
    p_existed="$(grep '^persistent_token_existed_before=' "$ROLLBACK_DIR/token-metadata.txt" | cut -d= -f2 || echo "")"
    l_existed="$(grep '^legacy_token_existed_before=' "$ROLLBACK_DIR/token-metadata.txt" | cut -d= -f2 || echo "")"
    migration_performed="$(grep '^token_migration_performed=' "$ROLLBACK_DIR/token-metadata.txt" | cut -d= -f2 || echo "")"
    p_sha="$(grep '^persistent_token_sha256=' "$ROLLBACK_DIR/token-metadata.txt" | cut -d= -f2 || echo "")"
    m_sha="$(grep '^migrated_token_sha256=' "$ROLLBACK_DIR/token-metadata.txt" | cut -d= -f2 || echo "")"
    meta_p_path="$(grep '^persistent_token_path=' "$ROLLBACK_DIR/token-metadata.txt" | cut -d= -f2 || echo "")"

    if [[ "$p_existed" == "1" ]]; then
      if [[ -f "$ROLLBACK_DIR/auth_token.bak" && -f "$TOKEN_PATH" ]]; then
        local current_sha
        current_sha="$(sha256_file "$TOKEN_PATH")"
        if [[ -n "$p_sha" && "$current_sha" != "$p_sha" ]]; then
          warn "current token differs from recorded pre-rollout value (${p_sha}) -- NOT restoring automatically; restore ${ROLLBACK_DIR}/auth_token.bak manually if needed"
        else
          copy_regular_file "$ROLLBACK_DIR/auth_token.bak" "$TOKEN_PATH" 600 \
            || die "the pre-rollout token was NOT restored to ${TOKEN_PATH}; restore ${ROLLBACK_DIR}/auth_token.bak by hand after checking the path"
          log "Restored pre-rollout persistent token."
        fi
      elif [[ -f "$ROLLBACK_DIR/auth_token.bak" && ! -f "$TOKEN_PATH" ]]; then
        copy_regular_file "$ROLLBACK_DIR/auth_token.bak" "$TOKEN_PATH" 600 \
          || die "the pre-rollout token was NOT restored to ${TOKEN_PATH}; restore ${ROLLBACK_DIR}/auth_token.bak by hand after checking the path"
        log "Restored pre-rollout persistent token (file was missing)."
      fi
    elif [[ "$p_existed" == "0" && "$migration_performed" == "1" ]]; then
      if [[ -f "$TOKEN_PATH" ]]; then
        local current_sha canon_dst canon_meta
        current_sha="$(sha256_file "$TOKEN_PATH")"
        canon_dst="$(canon_path "$TOKEN_PATH")"
        canon_meta="$(canon_path "${meta_p_path:-$TOKEN_PATH}")"
        if [[ "$canon_dst" == "$canon_meta" && -n "$m_sha" && "$current_sha" == "$m_sha" ]]; then
          rm -f "$TOKEN_PATH"
          log "Removed persistent token created by this rollout migration (restored pre-rollout state: no persistent token)."
        else
          warn "refusing automatic deletion of persistent token ${TOKEN_PATH}: path or checksum (${current_sha}) differs from recorded migration value (${m_sha}); leaving file in place"
        fi
      fi
    elif [[ "$p_existed" == "0" && "$migration_performed" != "1" ]]; then
      if [[ -f "$TOKEN_PATH" ]]; then
        warn "a new persistent token appeared at ${TOKEN_PATH} during/after rollout; refusing automatic deletion without explicit operator verification"
      fi
    fi
  else
    warn "missing token metadata in backup directory ${ROLLBACK_DIR} -- leaving token file at ${TOKEN_PATH} untouched"
  fi

  if [[ "$RESTORE_STALE_DB" -eq 1 && -d "$ROLLBACK_DIR/stale-database" ]]; then
    log "RESTORE_STALE_DB=1 -- restoring archived stale database files..."
    for f in "$DB_FILENAME" "$WAL_FILENAME" "$SHM_FILENAME"; do
      if [[ -f "$ROLLBACK_DIR/stale-database/$f" ]]; then
        copy_regular_file "$ROLLBACK_DIR/stale-database/$f" "$(canon_path "$WEBMGR_DATA_SRC")/$f" || warn "stale database file ${f} was NOT restored"
      fi
    done
  else
    log "Stale database files left archived (set RESTORE_STALE_DB=1 to restore them -- current architecture never reads them)."
  fi

  restore_state_files

  rollback_recreate_and_verify

  log "=== Rollback complete: ${SERVICE} runs the previous image and a plain 'docker compose up -d' resolves to it. The Beets library database was never touched. ==="
}

# Reads previous-image.txt / previous-image-labels.json from ROLLBACK_DIR,
# restores the .env version pin, recreates SERVICE on the previous image and
# PROVES the result. Any mismatch is fatal: a rollback that silently leaves
# the new version running is worse than one that fails loudly.
rollback_recreate_and_verify() {
  STAGE="rollback-recreate"
  local previous_image_ref="" previous_image_id="" previous_version=""
  if [[ -f "$ROLLBACK_DIR/previous-image.txt" ]]; then
    previous_image_ref="$(grep '^previous_image_ref=' "$ROLLBACK_DIR/previous-image.txt" | cut -d= -f2- || true)"
    previous_image_id="$(grep '^previous_image_id=' "$ROLLBACK_DIR/previous-image.txt" | cut -d= -f2- || true)"
  fi
  [[ -n "$previous_image_ref" && -n "$previous_image_id" ]] || die "backup has no previous image reference/ID (previous-image.txt) -- cannot prove a rollback; restore by hand"
  if [[ -f "$ROLLBACK_DIR/previous-image-labels.json" ]]; then
    previous_version="$(_py -c '
import json, sys
try:
    labels = json.load(open(sys.argv[1], encoding="utf-8")) or {}
except Exception:
    labels = {}
print(labels.get("org.opencontainers.image.version", ""))' "$ROLLBACK_DIR/previous-image-labels.json")"
  fi

  # The on-disk .env decides what any later `docker compose up -d` deploys.
  restore_env_version_line "$previous_image_ref"
  # Resolve from the files on disk only, never from this shell's environment.
  unset BEETS_WEB_MANAGER_VERSION

  # A moving tag (:latest) now names the newer image. Point the local tag
  # back at the recorded previous image (pulled back by its registry digest
  # if it was pruned), so the recreate below and any later plain
  # 'docker compose up -d' use it. The Compose file and .env are not edited.
  local tag_id previous_repo_digest=""
  previous_repo_digest="$(grep '^previous_image_repo_digest=' "$ROLLBACK_DIR/previous-image.txt" | cut -d= -f2- || true)"
  tag_id="$(docker image inspect "$previous_image_ref" --format '{{.Id}}' 2>/dev/null || true)"
  if [[ "$tag_id" != "$previous_image_id" ]]; then
    if ! docker image inspect "$previous_image_id" --format '{{.Id}}' >/dev/null 2>&1; then
      [[ -n "$previous_repo_digest" ]] || die "the previous image ${previous_image_id} is no longer on this host and the backup has no registry digest for it -- cannot roll back automatically"
      log "Previous image is no longer local; pulling it back by digest ${previous_repo_digest}..."
      docker pull "$previous_repo_digest" >&2 || die "pulling the previous image by digest (${previous_repo_digest}) failed"
      [[ "$(docker image inspect "$previous_repo_digest" --format '{{.Id}}' 2>/dev/null || true)" == "$previous_image_id" ]] \
        || die "the image pulled by digest ${previous_repo_digest} is not the recorded previous image ${previous_image_id}"
    fi
    docker tag "$previous_image_id" "$previous_image_ref"
    log "Re-tagged ${previous_image_ref} to the previous image ${previous_image_id} (it pointed at ${tag_id:-nothing}). A later 'docker compose pull' moves it forward again."
  fi
  # The old container may have been created from another ref (pinned or
  # variable layout) than the Compose file now names (the shipped literal
  # :latest). That :latest still names the newer image; point it back too.
  local compose_ref
  compose_ref="$(compose_service_image "$SERVICE")"
  if [[ "$compose_ref" == "$LATEST_IMAGE" && "$compose_ref" != "$previous_image_ref" \
        && "$(docker image inspect "$compose_ref" --format '{{.Id}}' 2>/dev/null || true)" != "$previous_image_id" ]]; then
    docker tag "$previous_image_id" "$compose_ref"
    log "Re-tagged ${compose_ref} (what the Compose file names) to the previous image ${previous_image_id} as well."
  fi

  local override
  # Next to the Compose file (removed again below): the docker CLI must be
  # able to open it by that path, which is not true for every temp dir
  # (e.g. a Windows docker.exe driven from Git Bash).
  override="$(mktemp "$(dirname "$COMPOSE_FILE")/.rollback-override.XXXXXX")"
  printf 'services:\n  %s:\n    image: "%s"\n' "$SERVICE" "$previous_image_ref" > "$override"
  log "Recreating ${SERVICE} on previous image reference: ${previous_image_ref}"
  if ! docker compose -f "$COMPOSE_FILE" -f "$override" up -d --no-deps --pull never --force-recreate "$SERVICE" >&2; then
    rm -f "$override"
    die "recreating ${SERVICE} on ${previous_image_ref} failed (output above)"
  fi
  rm -f "$override"

  STAGE="rollback-verification"
  WEBMGR_CID="$(resolve_container_id "$SERVICE")"
  local running_id configured resolved
  running_id="$(docker inspect --format '{{.Image}}' "$WEBMGR_CID")"
  configured="$(docker inspect --format '{{.Config.Image}}' "$WEBMGR_CID")"
  if [[ "$running_id" != "$previous_image_id" || "$configured" != "$previous_image_ref" ]]; then
    REASON_CODE="recreated_image_unverified"
    docker compose -f "$COMPOSE_FILE" stop "$SERVICE" >&2 || warn "could not stop ${SERVICE} -- stop it by hand"
    die "after rollback ${SERVICE} runs '${configured}' (${running_id}), expected the previous image ${previous_image_ref} (${previous_image_id}) -- ${SERVICE} was stopped so that image does not keep running"
  fi
  wait_for_health "$WEBMGR_CID" "$HEALTH_TIMEOUT_SECONDS" || die "container did not become healthy after rollback"
  log "Running image verified: ${configured} (${running_id})"

  # Durable means: what a plain 'docker compose up -d' resolves to names the
  # previous image (by ID; the ref itself may differ, e.g. :0.1.2 vs :latest).
  local resolved_id
  resolved="$(compose_service_image "$SERVICE")"
  resolved_id="$(docker image inspect "$resolved" --format '{{.Id}}' 2>/dev/null || true)"
  [[ "$resolved_id" == "$previous_image_id" ]] || die "the restored Compose file and .env resolve ${SERVICE} to '${resolved}' (${resolved_id:-not on this host}), not the previous image ${previous_image_id} (${previous_image_ref}) -- the next plain 'docker compose up -d' would leave the rolled-back version. Fix BEETS_WEB_MANAGER_VERSION / the image line in $(dirname "$COMPOSE_FILE")"
  log "A plain 'docker compose up -d' resolves ${SERVICE} to ${resolved} = ${resolved_id} (rollback is durable)."

  if [[ -n "$previous_version" ]]; then
    local live_version="" i
    for i in 1 2 3 4 5; do
      live_version="$(curl -sS --max-time 10 "${ENDPOINT_BASE_URL}/health/live" 2>/dev/null | _py -c 'import json,sys
try: print(json.load(sys.stdin).get("version",""))
except Exception: print("")' || true)"
      [[ "$live_version" == "$previous_version" ]] && break
      sleep 2
    done
    [[ "$live_version" == "$previous_version" ]] || die "/health/live reports version '${live_version}', expected the previous version '${previous_version}'"
    log "/health/live reports version ${live_version}"
  else
    warn "previous image had no version label recorded -- /health/live version not compared"
  fi

  refresh_engine_plugin_if_stale
}

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
main() {
  case "$MODE" in
    dry-run)  run_dry_run ;;
    rollback) run_rollback ;;
    offline-db-identity) run_offline_db_identity ;;
    deploy)   run_deploy ;;
    prune-backups) run_prune_backups ;;
    *) die "unknown mode: $MODE" ;;
  esac
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  main
fi

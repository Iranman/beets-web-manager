#!/usr/bin/env bash
# Guarded, fail-closed rollout of Beets Web Manager onto a TrueNAS Docker
# Compose stack. Reusable across releases -- pass VERSION (and, once known,
# EXPECTED_REVISION) rather than editing this file per release.
#
# Current target: v0.1.4 (v0.1.3's image, ghcr.io/iranman/beets-web-manager:0.1.3
# at revision 36bfc7554378a9ef6bd8f9c47a7d1be553647503, contains only PR #64.
# It does NOT contain the token-persistence, status-cache, or
# get_db_connection fixes from PR #65 -- those require a v0.1.4 release. Do
# not deploy 0.1.3 expecting PR #65's fixes; do not deploy 0.1.4 with this
# script until v0.1.4 is actually tagged and published.) See
# docs/operations/TRUENAS_ROLLOUT.md for the full writeup.
#
# Design goals:
#   - Never touch the authoritative Beets database (/config/musiclibrary.blb
#     under the `beets` engine's own mount) -- read-only, checksum-verified
#     before AND after.
#   - Never guess container names, host paths, or the deployed image --
#     everything is resolved from `docker inspect` / `docker compose config`
#     and independently re-verified.
#   - Perform ZERO mutating action until every safety check has passed.
#   - Archive (never delete) the stale, pre-#64 web-manager-created database
#     using exact filenames -- no wildcards, no `rm`.
#   - Recreate only the `beets-web-manager` service; the Beets engine, Plex,
#     Lidarr, and every other Arrs service are left untouched.
#   - Support --dry-run (inspect-only, writes nothing outside a temp dir)
#     and --rollback DIR (undo this script's own change set).
#
# Usage:
#   scripts/deploy_truenas_web_manager.sh              # real rollout
#   scripts/deploy_truenas_web_manager.sh --dry-run     # inspect only
#   scripts/deploy_truenas_web_manager.sh --rollback DIR
#
# Configuration (env vars, all optional):
#   STACK_DIR, SERVICE, ENGINE_SERVICE, VERSION, EXPECTED_REVISION,
#   COMPOSE_FILE, MIN_ITEM_COUNT, STALE_DB_MAX_ITEMS,
#   ENDPOINT_BASE_URL, RESTORE_STALE_DB (rollback only)
#
# Exit codes: 0 success/dry-run-clean, 1 any safety check or stage failure
# (see the printed "ROLLOUT FAILED" block for stage/backup-dir/rollback cmd).

set -Eeuo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
STACK_DIR="${STACK_DIR:-/mnt/PLEX/Apps/Arrs}"
SERVICE="${SERVICE:-beets-web-manager}"
ENGINE_SERVICE="${ENGINE_SERVICE:-beets}"
VERSION="${VERSION:-0.1.4}"
EXPECTED_IMAGE="ghcr.io/iranman/beets-web-manager:${VERSION}"
# Set once the VERSION release's commit is known (e.g. EXPECTED_REVISION=<sha>
# when actually deploying v0.1.4) to pin the exact org.opencontainers.image.revision
# label, not just the version label. Left unset here since v0.1.4 has not
# been tagged yet -- see docs/operations/TRUENAS_ROLLOUT.md. When unset, the
# revision-label check is skipped with a warning rather than pinned to a
# guessed value.
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

# ---------------------------------------------------------------------------
# Arg parsing
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)
      DRY_RUN=1
      MODE="dry-run"
      shift
      ;;
    --rollback)
      MODE="rollback"
      ROLLBACK_DIR="${2:-}"
      [[ -n "$ROLLBACK_DIR" ]] || { echo "FATAL: --rollback requires a backup directory path" >&2; exit 1; }
      shift 2
      ;;
    -h|--help)
      sed -n '2,35p' "${BASH_SOURCE[0]}"
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
BACKUP_DIR=""
PREVIOUS_IMAGE_ID=""

log()  { printf '[%s] %s\n' "$(date -u +%H:%M:%S)" "$*" >&2; }
warn() { printf '[%s] WARNING: %s\n' "$(date -u +%H:%M:%S)" "$*" >&2; }

_FAILURE_REPORTED=0

# Prints the failed-stage/backup-dir/rollback-command diagnostic block.
# Called from both die() (the normal, deliberate rejection path -- an
# explicit `exit` builtin does NOT fire bash's ERR trap, so die() cannot
# rely on the trap alone) and on_error() (the ERR trap, for genuinely
# unexpected failures: unbound variables, a command failing under -e that
# nothing explicitly checked). Guarded so a failure that somehow triggers
# both paths only prints once.
report_failure() {
  local ec="$1"
  [[ "$_FAILURE_REPORTED" -eq 1 ]] && return 0
  _FAILURE_REPORTED=1
  {
    echo ""
    echo "==================== ROLLOUT FAILED ===================="
    echo "Failed stage:          ${STAGE}"
    echo "Backup directory:      ${BACKUP_DIR:-<none created yet>}"
    echo "Previous image ID:     ${PREVIOUS_IMAGE_ID:-<unknown/not reached>}"
    echo "Current container status:"
    if command -v docker >/dev/null 2>&1; then
      docker inspect --format '  {{.Name}}: {{.State.Status}} (health={{if .State.Health}}{{.State.Health.Status}}{{else}}n/a{{end}})' "$SERVICE" 2>/dev/null \
        || echo "  (container '${SERVICE}' not found or docker unavailable)"
    fi
    echo "Rollback command:"
    if [[ -n "$BACKUP_DIR" ]]; then
      echo "  $0 --rollback ${BACKUP_DIR}"
    else
      echo "  (no backup was created yet -- nothing to roll back; production was not touched)"
    fi
    echo "=========================================================="
  } >&2
  return "$ec"
}

die() {
  printf '[%s] FATAL (%s): %s\n' "$(date -u +%H:%M:%S)" "$STAGE" "$*" >&2
  report_failure 1
  exit 1
}

on_error() {
  local ec=$?
  [[ "$ec" -eq 0 ]] && return 0
  report_failure "$ec"
  exit "$ec"
}
trap on_error ERR

# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------
require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "required command not found on PATH: $1"
}

# Prefer a real interpreter check up front -- almost every safety check below
# depends on python3 (compose-config JSON parsing, SQLite inspection
# fallback, checksum/permission reporting).
require_cmd docker
require_cmd python3

_compose() {
  docker compose -f "$COMPOSE_FILE" "$@"
}

# Runs python3 and strips any \r from its output before bash ever sees it.
# On Linux (the real TrueNAS target) this is a no-op. It exists because a
# Windows-hosted python3 (dev/test environments only) CRLF-translates
# stdout by default, and bash's $(...) command substitution strips only
# trailing \n -- silently leaving \r embedded in captured multi-line output,
# which then breaks exact string comparisons (mount-source matching,
# before/after container-ID diffing, etc). Every python3 call whose output
# this script captures goes through here.
_py() {
  python3 "$@" | tr -d '\r'
}

# Resolve the compose file once, never guessing a container/service exists
# just because a directory name matches.
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

# Canonicalize a path without requiring the target to exist (works for a
# not-yet-created backup dir, and for DB paths we check existence of
# separately).
canon_path() {
  _py -c 'import os,sys; print(os.path.realpath(sys.argv[1]).replace("\\","/"))' "$1"
}

sha256_file() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
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

# Read-only SQL against a SQLite file: cannot write, cannot create -wal/-shm,
# cannot modify the file even on error. NEVER converts a query failure into
# "0 rows" -- any failure here is a hard stop (fail closed per spec).
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

# Any process holding the file open, on the host or in any container sharing
# the bind mount. Prefers lsof, falls back to fuser. Fails closed (refuses to
# proceed) if neither tool is available -- "we couldn't verify" is never
# treated as "so it's fine".
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

# docker compose config, resolved to JSON, parsed with python3 (never
# regex/grep over YAML).
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
  cid="$(_compose ps -q "$svc" 2>/dev/null || true)"
  [[ -n "$cid" ]] || die "could not resolve a container for compose service '${svc}' via 'docker compose ps -q' -- refusing to guess a container name"
  echo "$cid"
}

# Mount source for a given destination inside a container, resolved from
# `docker inspect`, never inferred from directory naming conventions.
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

  WEBMGR_DATA_SRC="$(mount_source_for_dest "$WEBMGR_CID" /web-manager-data)"
  [[ -n "$WEBMGR_DATA_SRC" ]] || die "could not determine web-manager's /web-manager-data host source from 'docker inspect ${SERVICE}' -- is /web-manager-data mounted at all?"

  # Optional: an old-architecture web-manager may still mount /config too
  # (pre-#64 local-db-fallback deployments). Not fatal if absent.
  WEBMGR_LEGACY_CONFIG_SRC="$(mount_source_for_dest "$WEBMGR_CID" /config || true)"

  local engine_canon webmgr_canon
  engine_canon="$(canon_path "$ENGINE_CONFIG_SRC")"
  webmgr_canon="$(canon_path "$WEBMGR_DATA_SRC")"

  [[ "$engine_canon" != "$webmgr_canon" ]] || die "Beets /config source and web-manager /web-manager-data source resolve to the SAME path (${engine_canon}) -- refusing to continue"

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
  log "web-manager /web-manager-data source: ${webmgr_canon}"
  [[ -n "$WEBMGR_LEGACY_CONFIG_SRC" ]] && log "web-manager legacy /config source:  $(canon_path "$WEBMGR_LEGACY_CONFIG_SRC")"
  log "Authoritative DB path (expected):   ${AUTH_DB_PATH}"
  log "Stale DB path (if present):         ${STALE_DB_PATH}"
}

# ---------------------------------------------------------------------------
# Phase A.2 -- Compose / image verification (read-only)
# ---------------------------------------------------------------------------
verify_compose_image() {
  STAGE="compose-image-verification"
  local resolved_image
  # Force the exact pinned release for THIS invocation only -- never edits
  # the operator's .env, just overrides the process environment used to
  # resolve/pull/recreate the service.
  export BEETS_WEB_MANAGER_VERSION="$VERSION"
  resolved_image="$(compose_service_image "$SERVICE")"
  [[ -n "$resolved_image" ]] || die "compose service '${SERVICE}' has no image defined in ${COMPOSE_FILE}"
  [[ "$resolved_image" == "$EXPECTED_IMAGE" ]] || die "compose service '${SERVICE}' resolves to '${resolved_image}', expected '${EXPECTED_IMAGE}' (check .env BEETS_WEB_MANAGER_VERSION and this script's VERSION)"
  log "Compose service '${SERVICE}' resolves to expected image: ${resolved_image}"
}

# ---------------------------------------------------------------------------
# Phase A.3 -- Authoritative database safety checks (read-only)
# ---------------------------------------------------------------------------
AUTH_DB_SIZE="" AUTH_DB_SHA256="" AUTH_DB_INODE="" AUTH_DB_OWNER="" AUTH_DB_MODE=""
AUTH_ITEM_COUNT="" AUTH_ALBUM_COUNT=""

verify_authoritative_database() {
  STAGE="authoritative-db-verification"
  [[ -f "$AUTH_DB_PATH" ]] || die "authoritative database missing: ${AUTH_DB_PATH}"

  local quick_check
  if ! quick_check="$(sqlite_ro_query "$AUTH_DB_PATH" 'PRAGMA quick_check;')"; then
    die "PRAGMA quick_check failed to execute against authoritative database (${AUTH_DB_PATH}) -- refusing to continue"
  fi
  [[ "$quick_check" == "ok" ]] || die "authoritative database failed PRAGMA quick_check: '${quick_check}'"

  if ! AUTH_ITEM_COUNT="$(sqlite_ro_query "$AUTH_DB_PATH" 'SELECT count(*) FROM items;')"; then
    die "could not read item count from authoritative database -- refusing to continue"
  fi
  if ! AUTH_ALBUM_COUNT="$(sqlite_ro_query "$AUTH_DB_PATH" 'SELECT count(*) FROM albums;')"; then
    die "could not read album count from authoritative database -- refusing to continue"
  fi
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

  log "Authoritative DB verified: path=${AUTH_DB_PATH} size=${AUTH_DB_SIZE} sha256=${AUTH_DB_SHA256}"
  log "Authoritative DB counts:   items=${AUTH_ITEM_COUNT} albums=${AUTH_ALBUM_COUNT} owner=${AUTH_DB_OWNER} mode=${AUTH_DB_MODE}"
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

inspect_auth_token() {
  STAGE="token-inspection"
  log "Token env vars present (names only, values never read here): $(env | awk -F= '/^BEETS_(API|WEB_AUTH)_TOKEN/{print $1}' | paste -sd, -)"

  if [[ -f "$TOKEN_PATH" ]]; then
    TOKEN_EXISTS=1
    TOKEN_SIZE="$(file_size "$TOKEN_PATH")"
    [[ "$TOKEN_SIZE" -gt 0 ]] || die "persistent auth token file exists but is empty: ${TOKEN_PATH}"
    TOKEN_MODE="$(file_mode_octal "$TOKEN_PATH")"
    TOKEN_OWNER="$(file_owner "$TOKEN_PATH")"
    TOKEN_SHA256="$(sha256_file "$TOKEN_PATH")"
    case "$TOKEN_MODE" in
      0o600|0o400) : ;;
      *) warn "persistent auth token file mode is ${TOKEN_MODE}, expected 0o600 (world/group readable tokens are a real risk)" ;;
    esac
    log "Persistent web auth token found: path=${TOKEN_PATH} size=${TOKEN_SIZE} mode=${TOKEN_MODE} owner=${TOKEN_OWNER} sha256=${TOKEN_SHA256}"
    return 0
  fi

  log "No persistent token file at ${TOKEN_PATH} yet."
  if [[ -n "$WEBMGR_LEGACY_CONFIG_SRC" ]]; then
    local legacy_canon candidate
    legacy_canon="$(canon_path "$WEBMGR_LEGACY_CONFIG_SRC")"
    candidate="${legacy_canon%/}/${TOKEN_FILENAME}"
    if [[ -f "$candidate" ]]; then
      LEGACY_TOKEN_PATH="$candidate"
      NEEDS_TOKEN_MIGRATION=1
      log "Legacy web-manager token candidate found: ${LEGACY_TOKEN_PATH} (guarded migration will run in Phase C)"
    fi
  fi
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
# Phase A.7 -- Endpoint reachability (best-effort, read-only, used by both
# dry-run and the post-deploy verification phase)
# ---------------------------------------------------------------------------
probe_endpoint() {
  # probe_endpoint <path> <auth: 0|1> -- prints "status|elapsed_ms|bytes"
  local path="$1" auth="$2" tok_arg=()
  if [[ "$auth" -eq 1 && "$TOKEN_EXISTS" -eq 1 ]]; then
    local tok
    tok="$(cat "$TOKEN_PATH")"
    tok_arg=(-H "Authorization: Bearer ${tok}")
    unset tok
  fi
  local start end status size out
  start="$(_py -c 'import time; print(time.monotonic())')"
  out="$(curl -sS -o /tmp/.rollout_probe_body.$$ -w '%{http_code}' --max-time 10 "${tok_arg[@]}" "${ENDPOINT_BASE_URL}${path}" 2>/dev/null || echo "000")"
  end="$(_py -c 'import time; print(time.monotonic())')"
  status="$out"
  size="$(file_size /tmp/.rollout_probe_body.$$ 2>/dev/null || echo 0)"
  rm -f /tmp/.rollout_probe_body.$$ 2>/dev/null || true
  local elapsed_ms
  elapsed_ms="$(_py -c "print(int((${end}-${start})*1000))")"
  echo "${status}|${elapsed_ms}|${size}"
}

verify_endpoints() {
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
  if [[ "$TOKEN_EXISTS" -eq 1 ]]; then
    tok="$(cat "$TOKEN_PATH")"
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
    die "one or more endpoint checks failed -- see warnings above"
  fi
}

# ---------------------------------------------------------------------------
# Phase B -- Dry-run entry point
# ---------------------------------------------------------------------------
run_dry_run() {
  log "=== DRY RUN: no containers will be stopped/recreated, no files moved, no tokens copied, no Compose changes ==="
  resolve_compose_file
  discover_and_verify_mounts
  verify_compose_image
  verify_authoritative_database
  inspect_stale_database
  inspect_auth_token
  plan_backup_dir
  log "Pulling image for label verification only (no recreate)..."
  export BEETS_WEB_MANAGER_VERSION="$VERSION"
  _compose pull "$SERVICE" >&2 || warn "image pull failed in dry-run (network/registry issue) -- label verification skipped"
  verify_image_labels_if_present || true
  if docker inspect --format '{{.State.Status}}' "$SERVICE" >/dev/null 2>&1; then
    verify_endpoints || warn "endpoint verification reported issues (see above) -- not fatal in dry-run"
  else
    log "Service '${SERVICE}' is not currently running -- skipping live endpoint checks."
  fi
  log "=== DRY RUN COMPLETE: all checks passed, nothing was changed ==="
}

verify_image_labels_if_present() {
  local img_id revision version
  img_id="$(docker image inspect "$EXPECTED_IMAGE" --format '{{.Id}}' 2>/dev/null || true)"
  [[ -n "$img_id" ]] || { warn "image ${EXPECTED_IMAGE} not present locally"; return 1; }
  revision="$(docker image inspect "$EXPECTED_IMAGE" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' 2>/dev/null || true)"
  version="$(docker image inspect "$EXPECTED_IMAGE" --format '{{index .Config.Labels "org.opencontainers.image.version"}}' 2>/dev/null || true)"
  [[ "$version" == "$VERSION" ]] || die "image org.opencontainers.image.version label is '${version}', expected '${VERSION}'"
  if [[ -n "$EXPECTED_REVISION" ]]; then
    [[ "$revision" == "$EXPECTED_REVISION" ]] || die "image org.opencontainers.image.revision label is '${revision}', expected '${EXPECTED_REVISION}'"
  else
    warn "EXPECTED_REVISION not set -- skipping the exact revision-label pin (relying on the version label '${version}' alone). Set EXPECTED_REVISION=<release commit sha> once it's known for a fully pinned deployment."
  fi
  log "Image labels verified: version=${version} revision=${revision:-<none>}"
}

# ---------------------------------------------------------------------------
# Phase C -- Real rollout (mutating)
# ---------------------------------------------------------------------------
create_backup_dir() {
  STAGE="backup-creation"
  mkdir -p "$BACKUP_DIR"
  chmod 700 "$BACKUP_DIR"

  cp "$COMPOSE_FILE" "$BACKUP_DIR/docker-compose.yml.bak"
  local env_file
  env_file="$(dirname "$COMPOSE_FILE")/.env"
  if [[ -f "$env_file" ]]; then
    cp "$env_file" "$BACKUP_DIR/.env.bak"
    chmod 600 "$BACKUP_DIR/.env.bak"
  fi
  compose_config_json > "$BACKUP_DIR/resolved-compose-config.json" 2>/dev/null || true
  chmod 600 "$BACKUP_DIR/resolved-compose-config.json" 2>/dev/null || true

  docker inspect "$WEBMGR_CID" > "$BACKUP_DIR/container-inspect-before.json" 2>/dev/null || true

  PREVIOUS_IMAGE_ID="$(docker inspect --format '{{.Image}}' "$WEBMGR_CID" 2>/dev/null || echo "")"
  local previous_image_ref
  previous_image_ref="$(docker inspect --format '{{.Config.Image}}' "$WEBMGR_CID" 2>/dev/null || echo "")"
  {
    echo "previous_image_id=${PREVIOUS_IMAGE_ID}"
    echo "previous_image_ref=${previous_image_ref}"
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

  if [[ "$TOKEN_EXISTS" -eq 1 ]]; then
    cp "$TOKEN_PATH" "$BACKUP_DIR/auth_token.bak"
    chmod 600 "$BACKUP_DIR/auth_token.bak"
    {
      echo "token_path=${TOKEN_PATH}"
      echo "token_size=${TOKEN_SIZE}"
      echo "token_mode=${TOKEN_MODE}"
      echo "token_owner=${TOKEN_OWNER}"
      echo "token_sha256=${TOKEN_SHA256}"
    } > "$BACKUP_DIR/token-metadata.txt"
    chmod 600 "$BACKUP_DIR/token-metadata.txt"
  fi

  log "Backup created at ${BACKUP_DIR}"
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
  echo "token_migration_performed=1" >> "$BACKUP_DIR/token-metadata.txt" 2>/dev/null || true
  log "Legacy token migrated to ${TOKEN_PATH} (checksum verified, contents never printed)."
}

deploy_image() {
  STAGE="image-deployment"
  export BEETS_WEB_MANAGER_VERSION="$VERSION"
  log "Pulling ${EXPECTED_IMAGE}..."
  _compose pull "$SERVICE"
  verify_image_labels_if_present

  # Snapshot every other running service's container ID so we can prove
  # nothing else was touched by --force-recreate.
  local other_services other_before other_after
  other_services="$(compose_config_json | _py -c "
import json, sys
data = json.load(sys.stdin)
print('\n'.join(s for s in data.get('services', {}) if s != '$SERVICE'))
")"
  other_before="$(for s in $other_services; do printf '%s=%s\n' "$s" "$(_compose ps -q "$s" 2>/dev/null || true)"; done)"

  log "Recreating ${SERVICE} only (--no-deps --force-recreate)..."
  _compose up -d --no-deps --force-recreate "$SERVICE"

  other_after="$(for s in $other_services; do printf '%s=%s\n' "$s" "$(_compose ps -q "$s" 2>/dev/null || true)"; done)"
  [[ "$other_before" == "$other_after" ]] || die "a service other than '${SERVICE}' changed container ID during recreate -- this must never happen: before=[${other_before}] after=[${other_after}]"

  WEBMGR_CID="$(resolve_container_id "$SERVICE")"
  local configured_image running_image_id
  configured_image="$(docker inspect --format '{{.Config.Image}}' "$WEBMGR_CID")"
  running_image_id="$(docker inspect --format '{{.Image}}' "$WEBMGR_CID")"
  local expected_image_id
  expected_image_id="$(docker image inspect "$EXPECTED_IMAGE" --format '{{.Id}}')"
  [[ "$configured_image" == "$EXPECTED_IMAGE" ]] || die "recreated container's configured image is '${configured_image}', expected '${EXPECTED_IMAGE}'"
  [[ "$running_image_id" == "$expected_image_id" ]] || die "recreated container's running image ID does not match the pulled ${EXPECTED_IMAGE} image ID"

  log "Waiting up to ${HEALTH_TIMEOUT_SECONDS}s for ${SERVICE} to become healthy..."
  if ! wait_for_health "$WEBMGR_CID" "$HEALTH_TIMEOUT_SECONDS"; then
    die "container did not become healthy within ${HEALTH_TIMEOUT_SECONDS}s"
  fi
  log "${SERVICE} is healthy on image ${EXPECTED_IMAGE} (id=${running_image_id})."
}

# Standalone (directly unit-testable) re-check that the authoritative
# database's quick_check/counts/checksum are byte-for-byte what they were
# before this rollout touched anything.
assert_authoritative_db_unchanged() {
  local quick_check item_count album_count sha
  quick_check="$(sqlite_ro_query "$AUTH_DB_PATH" 'PRAGMA quick_check;')" || die "post-deploy PRAGMA quick_check failed against authoritative database"
  [[ "$quick_check" == "ok" ]] || die "post-deploy authoritative database quick_check is not ok: '${quick_check}'"
  item_count="$(sqlite_ro_query "$AUTH_DB_PATH" 'SELECT count(*) FROM items;')" || die "post-deploy item count query failed"
  album_count="$(sqlite_ro_query "$AUTH_DB_PATH" 'SELECT count(*) FROM albums;')" || die "post-deploy album count query failed"
  sha="$(sha256_file "$AUTH_DB_PATH")"
  [[ "$item_count" == "$AUTH_ITEM_COUNT" ]] || die "authoritative item count changed: was ${AUTH_ITEM_COUNT}, now ${item_count}"
  [[ "$album_count" == "$AUTH_ALBUM_COUNT" ]] || die "authoritative album count changed: was ${AUTH_ALBUM_COUNT}, now ${album_count}"
  [[ "$sha" == "$AUTH_DB_SHA256" ]] || die "authoritative database SHA-256 changed: was ${AUTH_DB_SHA256}, now ${sha}"
  log "Authoritative database confirmed unchanged post-deploy (items=${item_count} albums=${album_count} sha256=${sha})."
}

# Standalone (directly unit-testable) proof the local-DB-fallback
# architecture removed by PR #64 has not regressed: nothing reappears at the
# stale web-manager-owned DB/WAL/SHM paths after a (re)start.
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

  local engine_status
  engine_status="$(docker inspect --format '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}(no healthcheck){{end}}' "$ENGINE_CID")"
  log "Beets engine container unchanged and reports: ${engine_status}"

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

  verify_endpoints
}

run_deploy() {
  log "=== Beets Web Manager ${VERSION} guarded rollout starting ==="
  resolve_compose_file
  discover_and_verify_mounts
  verify_compose_image
  verify_authoritative_database
  inspect_stale_database
  inspect_auth_token
  plan_backup_dir
  log "=== All pre-flight safety checks passed. Beginning mutating actions. ==="

  create_backup_dir
  stop_web_manager
  archive_stale_database
  migrate_token_if_needed
  deploy_image
  verify_post_deploy

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
  discover_and_verify_mounts

  log "Stopping ${SERVICE} for rollback..."
  _compose stop "$SERVICE" || true

  log "Restoring Compose file from backup..."
  cp "$ROLLBACK_DIR/docker-compose.yml.bak" "$COMPOSE_FILE"

  if [[ -f "$ROLLBACK_DIR/token-metadata.txt" ]] && grep -q '^token_migration_performed=1' "$ROLLBACK_DIR/token-metadata.txt" 2>/dev/null; then
    if [[ -f "$ROLLBACK_DIR/auth_token.bak" && -f "$TOKEN_PATH" ]]; then
      local current_sha backup_sha
      current_sha="$(sha256_file "$TOKEN_PATH")"
      backup_sha="$(grep '^token_sha256=' "$ROLLBACK_DIR/token-metadata.txt" | cut -d= -f2)"
      if [[ "$current_sha" != "$backup_sha" ]]; then
        warn "current token differs from the one recorded before this rollout's migration -- NOT restoring it automatically (something else changed it since); restore ${ROLLBACK_DIR}/auth_token.bak manually if needed"
      else
        cp "$ROLLBACK_DIR/auth_token.bak" "$TOKEN_PATH"
        chmod 600 "$TOKEN_PATH"
        log "Restored pre-migration token."
      fi
    fi
  fi

  if [[ "$RESTORE_STALE_DB" -eq 1 && -d "$ROLLBACK_DIR/stale-database" ]]; then
    log "RESTORE_STALE_DB=1 -- restoring archived stale database files..."
    for f in "$DB_FILENAME" "$WAL_FILENAME" "$SHM_FILENAME"; do
      [[ -f "$ROLLBACK_DIR/stale-database/$f" ]] && cp "$ROLLBACK_DIR/stale-database/$f" "$(canon_path "$WEBMGR_DATA_SRC")/$f"
    done
  else
    log "Stale database files left archived (set RESTORE_STALE_DB=1 to restore them -- current architecture never reads them)."
  fi

  local previous_image_ref=""
  if [[ -f "$ROLLBACK_DIR/previous-image.txt" ]]; then
    previous_image_ref="$(grep '^previous_image_ref=' "$ROLLBACK_DIR/previous-image.txt" | cut -d= -f2-)"
  fi
  if [[ -n "$previous_image_ref" ]]; then
    log "Recreating ${SERVICE} on previous image reference: ${previous_image_ref}"
    docker compose -f "$COMPOSE_FILE" -f <(echo "services: {${SERVICE}: {image: ${previous_image_ref}}}") up -d --no-deps --force-recreate "$SERVICE" 2>/dev/null \
      || { warn "compose-level image override failed; recreating via 'docker compose up' with the restored Compose file as-is"; _compose up -d --no-deps --force-recreate "$SERVICE"; }
  else
    warn "no previous image reference recorded in backup -- recreating with the restored Compose file's current image"
    _compose up -d --no-deps --force-recreate "$SERVICE"
  fi

  WEBMGR_CID="$(resolve_container_id "$SERVICE")"
  wait_for_health "$WEBMGR_CID" "$HEALTH_TIMEOUT_SECONDS" || die "container did not become healthy after rollback"

  if [[ -f "$TOKEN_PATH" && -f "$ROLLBACK_DIR/token-metadata.txt" ]]; then
    local expected_sha actual_sha
    expected_sha="$(grep '^token_sha256=' "$ROLLBACK_DIR/token-metadata.txt" | cut -d= -f2)"
    actual_sha="$(sha256_file "$TOKEN_PATH")"
    [[ -z "$expected_sha" || "$expected_sha" == "$actual_sha" ]] || warn "token checksum after rollback (${actual_sha}) differs from the recorded pre-rollout value (${expected_sha})"
  fi

  log "=== Rollback complete. Beets engine and its database were never touched. ==="
}

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
main() {
  case "$MODE" in
    dry-run)  run_dry_run ;;
    rollback) run_rollback ;;
    deploy)   run_deploy ;;
    *) die "unknown mode: $MODE" ;;
  esac
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  main
fi

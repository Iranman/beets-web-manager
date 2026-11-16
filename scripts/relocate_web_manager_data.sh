#!/usr/bin/env bash
# Relocate the Beets Web Manager persistent data directory and repoint its
# bind mount -- reversibly, dry-run first.
#
# WHAT IT MOVES: only the host folder bind-mounted at the container's
# /web-manager-data (settings, login, session key, audit trail, jobs,
# transactions). It NEVER touches your music files or the Beets /config
# database (musiclibrary.blb) -- those are different mounts.
#
# WHY: the shipped Compose files resolve that mount source from an env var
# (e.g. ${BEETS_WEB_MANAGER_DATA_PATH:-...}). When the data dir ended up
# nested inside some other checkout/folder, this moves it to a clean path
# OUTSIDE that folder and repoints the mount by setting ONE key in the stack
# env file -- so the old location can later be archived without touching live
# data. The Compose YAML is never edited.
#
# SAFETY MODEL:
#   - Dry-run by default. Nothing changes without --apply.
#   - Backup-first: a timestamped .tar.gz of the data dir is made and verified
#     non-empty before anything moves (refuses otherwise).
#   - Copy, never cut: data is copied (rsync -aHAX / cp -a) and byte/count
#     verified; the OLD directory is left in place. Removal is manual, or
#     gated behind a separate --remove-old (only after health passes).
#   - Repoint via the env file (one key), never by editing Compose YAML.
#   - Recreate only SERVICE and verify health + /api/health before success.
#     If health fails the script STOPS and prints the exact rollback commands.
#   - Idempotent/reversible: safe to re-run (--resume); rollback = point the
#     env key back to the old source and recreate (printed, and in the runbook).
#
# See docs/runbooks/relocate-web-manager-data.md for the full procedure.
#
# USAGE (paths below are placeholders -- pass your own):
#   # Inspect the plan (resolves the live source from docker inspect):
#   bash scripts/relocate_web_manager_data.sh \
#       --stack-dir /path/to/stack --dest /path/to/new/web-manager-data
#
#   # Execute it:
#   bash scripts/relocate_web_manager_data.sh \
#       --stack-dir /path/to/stack --dest /path/to/new/web-manager-data --apply
#
#   # Source given explicitly, no Docker involved (operator recreates later):
#   bash scripts/relocate_web_manager_data.sh \
#       --source /old/web-manager-data --dest /new/web-manager-data \
#       --env-file /path/to/stack/.env --env-key BEETS_WEB_MANAGER_DATA_PATH \
#       --no-recreate --apply
#
# OPTIONS:
#   --dest DIR         (required) new absolute host path for the data dir
#   --source DIR       old data dir; default: resolved from `docker inspect`
#                      SERVICE's mount at --mount-dest
#   --stack-dir DIR    stack directory (holds the Compose file and .env)
#   --compose-file F   Compose file; default: <stack-dir>/docker-compose.yml
#   --env-file F       env file to edit; default: <compose dir>/.env
#   --env-key NAME     env key to set; default: detected from the Compose
#                      /web-manager-data mount line
#   --service NAME     Compose service; default: beets-web-manager
#   --mount-dest PATH  container mount point to resolve; default: /web-manager-data
#   --backup-dir DIR   where the .tar.gz backup is written;
#                      default: <dest parent>/_web-manager-relocate-backups
#   --resume           allow --dest to already exist and be non-empty (re-sync)
#   --no-recreate      skip the docker recreate + health check (operator does it)
#   --remove-old       after success, delete the OLD source dir (off by default)
#   --apply            actually perform the move (otherwise dry-run)
#   -h, --help         this help
#
# Exit codes: 0 ok / dry-run clean; 1 any guard or stage failure.

set -Eeuo pipefail

SERVICE="beets-web-manager"
MOUNT_DEST="/web-manager-data"
SOURCE=""
DEST=""
STACK_DIR=""
COMPOSE_FILE=""
ENV_FILE=""
ENV_KEY=""
BACKUP_DIR=""
APPLY=0
RESUME=0
RECREATE=1
REMOVE_OLD=0
ENDPOINT_BASE_URL="${ENDPOINT_BASE_URL:-http://127.0.0.1:8337}"
HEALTH_TIMEOUT_SECONDS="${HEALTH_TIMEOUT_SECONDS:-120}"

log()  { printf '[%s] %s\n' "$(date -u +%H:%M:%S)" "$*" >&2; }
warn() { printf '[%s] WARNING: %s\n' "$(date -u +%H:%M:%S)" "$*" >&2; }
die()  { printf '[%s] FATAL: %s\n' "$(date -u +%H:%M:%S)" "$*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dest)        DEST="${2:?--dest needs a path}"; shift 2 ;;
    --source)      SOURCE="${2:?--source needs a path}"; shift 2 ;;
    --stack-dir)   STACK_DIR="${2:?--stack-dir needs a path}"; shift 2 ;;
    --compose-file) COMPOSE_FILE="${2:?--compose-file needs a path}"; shift 2 ;;
    --env-file)    ENV_FILE="${2:?--env-file needs a path}"; shift 2 ;;
    --env-key)     ENV_KEY="${2:?--env-key needs a name}"; shift 2 ;;
    --service)     SERVICE="${2:?--service needs a name}"; shift 2 ;;
    --mount-dest)  MOUNT_DEST="${2:?--mount-dest needs a path}"; shift 2 ;;
    --backup-dir)  BACKUP_DIR="${2:?--backup-dir needs a path}"; shift 2 ;;
    --resume)      RESUME=1; shift ;;
    --no-recreate) RECREATE=0; shift ;;
    --remove-old)  REMOVE_OLD=1; shift ;;
    --apply)       APPLY=1; shift ;;
    -h|--help)     awk 'NR>1 && /^#/{sub(/^# ?/,"");print;next} NR>1{exit}' "${BASH_SOURCE[0]}"; exit 0 ;;
    *)             die "unrecognized argument: $1 (see --help)" ;;
  esac
done

require_cmd() { command -v "$1" >/dev/null 2>&1 || die "required command not found on PATH: $1"; }
require_cmd python3

# Absolute real path with forward slashes, without requiring the path to exist.
canon_path() { python3 -I -c 'import os,sys; print(os.path.abspath(sys.argv[1]).replace("\\","/"))' "$1"; }
# (file_count, total_bytes) over regular files under a dir, newline-separated.
tree_stats() {
  python3 -I - "$1" <<'PY'
import os, sys
root = sys.argv[1]
n = b = 0
for dp, _, fs in os.walk(root):
    for f in fs:
        p = os.path.join(dp, f)
        if os.path.islink(p):
            n += 1
            continue
        try:
            b += os.path.getsize(p); n += 1
        except OSError:
            pass
print(n); print(b)
PY
}

# ---- Resolve the Compose file and env file (best effort; only required for
#      auto-detection and recreate) -------------------------------------------
resolve_compose_file() {
  [[ -n "$COMPOSE_FILE" ]] && { [[ -f "$COMPOSE_FILE" ]] || die "COMPOSE_FILE does not exist: $COMPOSE_FILE"; return; }
  [[ -n "$STACK_DIR" ]] || return 0
  local c
  for c in "$STACK_DIR/docker-compose.arrs.yml" "$STACK_DIR/docker-compose.yml" "$STACK_DIR/docker-compose.full.yml"; do
    [[ -f "$c" ]] && { COMPOSE_FILE="$c"; return; }
  done
  warn "no Compose file found under $STACK_DIR -- pass --compose-file to enable env-key detection and recreate"
}

# The env var that the Compose /web-manager-data mount line interpolates. The
# OUTERMOST ${VAR...} wins (Compose resolves it first); that is the key whose
# value actually selects the mount source. Left empty if the line has no
# variable (a hardcoded path -- cannot be repointed via env).
detect_env_key() {
  [[ -n "$COMPOSE_FILE" ]] || return 0
  local line
  line="$(grep -E ":${MOUNT_DEST}(\$|:|[[:space:]])" "$COMPOSE_FILE" | grep -E '\$\{' | head -n 1 || true)"
  [[ -n "$line" ]] || return 0
  ENV_KEY="$(printf '%s' "$line" | grep -oE '\$\{[A-Za-z_][A-Za-z0-9_]*' | head -n 1 | sed 's/^\${//')"
}

resolve_env_file() {
  [[ -n "$ENV_FILE" ]] && return 0
  [[ -n "$COMPOSE_FILE" ]] || return 0
  ENV_FILE="$(dirname "$COMPOSE_FILE")/.env"
}

# ---- Resolve the live source from docker inspect, if not given --------------
resolve_source_from_docker() {
  require_cmd docker
  [[ -n "$COMPOSE_FILE" ]] || die "cannot resolve --source without a Compose file -- pass --source, or --stack-dir/--compose-file"
  local cid
  cid="$(docker compose -f "$COMPOSE_FILE" ps -q "$SERVICE" 2>/dev/null || true)"
  [[ -n "$cid" ]] || die "could not resolve a container for service '$SERVICE' -- pass --source explicitly"
  SOURCE="$(docker inspect --format '{{json .Mounts}}' "$cid" | python3 -I -c '
import json, sys
dest = sys.argv[1]
for m in json.load(sys.stdin):
    if m.get("Destination") == dest:
        print(m.get("Source", "")); break
' "$MOUNT_DEST")"
  [[ -n "$SOURCE" ]] || die "service '$SERVICE' has no mount at $MOUNT_DEST -- refusing to guess; pass --source"
}

# ---- Set exactly one key in the env file, preserving everything else --------
set_env_key() {
  local file="$1" key="$2" val="$3"
  ( umask 077; [[ -e "$file" ]] || : > "$file" )
  python3 -I - "$file" "$key" "$val" <<'PY'
import os, sys
path, key, val = sys.argv[1], sys.argv[2], sys.argv[3]
with open(path, "r", encoding="utf-8") as f:
    lines = f.readlines()
prefix = key + "="
out, replaced = [], False
for ln in lines:
    if ln.lstrip().startswith(prefix) and not ln.lstrip().startswith("#"):
        if not replaced:
            out.append("%s=%s\n" % (key, val)); replaced = True
        # drop any later duplicates of the same key
    else:
        out.append(ln)
if not replaced:
    if out and not out[-1].endswith("\n"):
        out[-1] += "\n"
    out.append("%s=%s\n" % (key, val))
with open(path, "w", encoding="utf-8") as f:
    f.writelines(out)
PY
}

# =============================================================================
# Validate inputs
# =============================================================================
[[ -n "$DEST" ]] || die "--dest is required (the new absolute host path)"
case "$DEST" in /*|[A-Za-z]:[\\/]*) : ;; *) die "--dest must be an absolute path: $DEST" ;; esac
case "$DEST" in *[$' \t\n']*) die "--dest must not contain whitespace (env-file safety): $DEST" ;; esac

resolve_compose_file
detect_env_key
resolve_env_file

[[ -n "$SOURCE" ]] || resolve_source_from_docker
[[ -d "$SOURCE" ]] || die "source data directory does not exist or is not a directory: $SOURCE"

SRC_CANON="$(canon_path "$SOURCE")"
DST_CANON="$(canon_path "$DEST")"
[[ "$SRC_CANON" != "$DST_CANON" ]] || die "source and dest resolve to the same path ($SRC_CANON) -- nothing to do"
case "$DST_CANON/" in "$SRC_CANON"/*) die "dest ($DST_CANON) is INSIDE source ($SRC_CANON) -- refusing (would recurse)";; esac
case "$SRC_CANON/" in "$DST_CANON"/*) die "source ($SRC_CANON) is INSIDE dest ($DST_CANON) -- refusing";; esac

if [[ -e "$DST_CANON" ]]; then
  [[ -d "$DST_CANON" ]] || die "dest exists and is not a directory: $DST_CANON"
  if [[ -n "$(ls -A "$DST_CANON" 2>/dev/null)" && "$RESUME" -ne 1 ]]; then
    die "dest already exists and is non-empty: $DST_CANON (pass --resume to re-sync into it)"
  fi
fi

# Repointing the mount needs a key + file. --no-recreate operators can still
# want it set; only refuse when we truly have nowhere to write it.
if [[ "$APPLY" -eq 1 ]]; then
  if [[ -z "$ENV_KEY" ]]; then
    die "could not determine which env key repoints $MOUNT_DEST -- pass --compose-file to detect it, or --env-key NAME"
  fi
  if [[ -z "$ENV_FILE" ]]; then
    die "no env file to edit -- pass --env-file, or --stack-dir/--compose-file so <compose dir>/.env is used"
  fi
fi

[[ -z "$BACKUP_DIR" ]] && BACKUP_DIR="$(dirname "$DST_CANON")/_web-manager-relocate-backups"

# =============================================================================
# Plan (always printed)
# =============================================================================
cat >&2 <<PLAN

=================== RELOCATE WEB-MANAGER DATA ($([[ "$APPLY" -eq 1 ]] && echo APPLY || echo DRY-RUN)) ===================
Service:          $SERVICE   (mount $MOUNT_DEST)
Source (old):     $SRC_CANON
Dest (new):       $DST_CANON
Compose file:     ${COMPOSE_FILE:-<none: source given explicitly>}
Env file:         ${ENV_FILE:-<none>}
Env key to set:   ${ENV_KEY:-<none detected>}  ->  $DST_CANON
Backup .tar.gz:   under $BACKUP_DIR
Copy:             $(command -v rsync >/dev/null 2>&1 && echo 'rsync -aHAX --numeric-ids' || echo 'cp -a') (old dir is kept)
Recreate:         $([[ "$RECREATE" -eq 1 ]] && echo "docker compose -f <file> up -d $SERVICE, then verify health + /api/health" || echo "SKIPPED (--no-recreate)")
Remove old dir:   $([[ "$REMOVE_OLD" -eq 1 ]] && echo "yes, after health passes (--remove-old)" || echo "no (left for operator; removal command printed on success)")
ROLLBACK:         set $ENV_KEY=$SRC_CANON in $ENV_FILE and recreate $SERVICE
==============================================================================
PLAN

if [[ "$APPLY" -ne 1 ]]; then
  log "DRY-RUN: no changes made. Re-run with --apply to perform the move."
  exit 0
fi

# =============================================================================
# Apply
# =============================================================================
# 1) Backup-first.
( umask 077; mkdir -p "$BACKUP_DIR" )
STAMP="$(date -u +%Y%m%d-%H%M%S)"
BACKUP_FILE="$BACKUP_DIR/web-manager-data-${STAMP}.tar.gz"
log "Backing up $SRC_CANON -> $BACKUP_FILE ..."
tar -czf "$BACKUP_FILE" -C "$(dirname "$SRC_CANON")" "$(basename "$SRC_CANON")"
chmod 600 "$BACKUP_FILE" 2>/dev/null || true
[[ -s "$BACKUP_FILE" ]] || die "backup is missing or empty: $BACKUP_FILE -- refusing to proceed"
log "Backup ok ($(wc -c < "$BACKUP_FILE") bytes)."

# 2) Copy to dest, preserving perms/ownership/timestamps.
( umask 022; mkdir -p "$DST_CANON" )
log "Copying data to $DST_CANON ..."
if command -v rsync >/dev/null 2>&1; then
  rsync -aHAX --numeric-ids "$SRC_CANON"/ "$DST_CANON"/
else
  # ponytail: cp -a is the fallback; it preserves mode/owner/timestamps and
  # links but not ACLs/xattrs. Install rsync if your data dir needs those.
  cp -a "$SRC_CANON"/. "$DST_CANON"/
fi

# 3) Verify count + bytes match.
# ponytail: count+size compare (fast). Content corruption by cp/rsync is not
# expected; upgrade to `rsync --checksum` or a sha256 tree compare if paranoid.
mapfile -t SRC_STATS < <(tree_stats "$SRC_CANON")
mapfile -t DST_STATS < <(tree_stats "$DST_CANON")
log "Source: ${SRC_STATS[0]} files / ${SRC_STATS[1]} bytes   Dest: ${DST_STATS[0]} files / ${DST_STATS[1]} bytes"
[[ "${SRC_STATS[0]}" == "${DST_STATS[0]}" && "${SRC_STATS[1]}" == "${DST_STATS[1]}" ]] \
  || die "copy verification FAILED (file count or byte total differ) -- source left intact; delete $DST_CANON and retry"
log "Copy verified."

# 4) Repoint the mount via the env file (one key only).
log "Setting $ENV_KEY=$DST_CANON in $ENV_FILE (other keys untouched) ..."
set_env_key "$ENV_FILE" "$ENV_KEY" "$DST_CANON"

# 5) Recreate SERVICE and verify health.
if [[ "$RECREATE" -eq 1 ]]; then
  require_cmd docker
  [[ -n "$COMPOSE_FILE" ]] || die "cannot recreate without a Compose file -- pass --compose-file, or use --no-recreate and recreate manually"
  print_rollback() {
    cat >&2 <<RB

!!! HEALTH VERIFICATION FAILED -- the new path is NOT confirmed working.
The old data dir is intact at: $SRC_CANON
ROLL BACK with:
  # 1. point the mount back to the old source
  <edit $ENV_FILE so $ENV_KEY=$SRC_CANON>   (or re-run this script with --source/--dest swapped)
  # 2. recreate the service on the old path
  docker compose -f $COMPOSE_FILE up -d $SERVICE
RB
  }
  log "Recreating $SERVICE on the new path ..."
  docker compose -f "$COMPOSE_FILE" up -d "$SERVICE" || { print_rollback; die "docker compose up failed"; }
  CID="$(docker compose -f "$COMPOSE_FILE" ps -q "$SERVICE" 2>/dev/null || true)"
  [[ -n "$CID" ]] || { print_rollback; die "could not resolve the recreated container"; }
  # Wait for the container healthcheck if it has one; else poll /api/health.
  waited=0
  while [[ "$waited" -lt "$HEALTH_TIMEOUT_SECONDS" ]]; do
    hs="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$CID" 2>/dev/null || echo missing)"
    [[ "$hs" == "missing" ]] && { print_rollback; die "container disappeared while waiting for health"; }
    [[ "$hs" == "healthy" ]] && break
    if [[ "$hs" == "none" ]]; then
      curl -fsS "$ENDPOINT_BASE_URL/api/health" >/dev/null 2>&1 && break
    fi
    sleep 3; waited=$((waited + 3))
  done
  if ! curl -fsS "$ENDPOINT_BASE_URL/api/health" >/dev/null 2>&1; then
    print_rollback
    die "service did not become healthy within ${HEALTH_TIMEOUT_SECONDS}s (/api/health not OK)"
  fi
  log "Health OK: $SERVICE is running on the new path and /api/health responds."
else
  warn "--no-recreate: env repointed but the service was NOT recreated. Recreate it yourself:"
  warn "  docker compose -f ${COMPOSE_FILE:-<compose file>} up -d $SERVICE   # then verify /api/health"
fi

# 6) Remove old dir only if asked and only after everything above passed.
if [[ "$REMOVE_OLD" -eq 1 ]]; then
  log "Removing OLD data dir (--remove-old): $SRC_CANON"
  rm -rf -- "$SRC_CANON"
  log "Old data dir removed. Backup remains at $BACKUP_FILE"
else
  cat >&2 <<DONE

SUCCESS. The data now lives at: $DST_CANON
The OLD dir is untouched at:    $SRC_CANON
Once you have confirmed the app works on the new path, remove the old dir:
  rm -rf -- '$SRC_CANON'
Backup (keep private -- contains secrets): $BACKUP_FILE
ROLLBACK (if ever needed): set $ENV_KEY=$SRC_CANON in $ENV_FILE and
  docker compose -f ${COMPOSE_FILE:-<compose file>} up -d $SERVICE
DONE
fi

exit 0

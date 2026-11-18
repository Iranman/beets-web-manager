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
# data. By DEFAULT the Compose YAML is never edited. If your Compose pins the
# mount source as a HARDCODED host path (no ${VAR}), pass --rewrite-compose to
# surgically rewrite only that ONE mount line in place (a .bak is saved first).
#
# SAFETY MODEL:
#   - Dry-run by default. Nothing changes without --apply.
#   - Backup-first: a timestamped .tar.gz of the data dir is made and verified
#     non-empty before anything moves (refuses otherwise).
#   - Copy, never cut: data is copied (rsync -aHAX / cp -a) and byte/count
#     verified; the OLD directory is left in place. Removal is manual, or
#     gated behind a separate --remove-old (only after health passes).
#   - Repoint via the env file (one key) by default; with --rewrite-compose,
#     rewrite only the one hardcoded mount-source line, after saving a .bak and
#     verifying every other Compose line is byte-identical.
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
#   --rewrite-compose  when the mount source is a HARDCODED host path (no
#                      ${VAR} to set in the env file), rewrite that ONE Compose
#                      mount line in place (writes <compose>.bak first and keeps
#                      every other line byte-identical); opt-in, off by default
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
REWRITE_COMPOSE=0
COMPOSE_MOUNT_IS_LITERAL=0
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
    --rewrite-compose) REWRITE_COMPOSE=1; shift ;;
    --apply)       APPLY=1; shift ;;
    -h|--help)     awk 'NR>1 && /^#/{sub(/^# ?/,"");print;next} NR>1{exit}' "${BASH_SOURCE[0]}"; exit 0 ;;
    *)             die "unrecognized argument: $1 (see --help)" ;;
  esac
done

# C1: --remove-old deletes the OLD dir only after proving (via docker inspect)
# that the LIVE container actually mounts the NEW path at MOUNT_DEST. That F1
# cross-check is gated on the recreate, so with --no-recreate it is skipped and
# the rm -rf would run with no live verification. Reject the combination up
# front (the only safe choice): recreate it yourself, confirm it, then remove
# the old dir by hand (the SUCCESS output prints the exact command).
if [[ "$RECREATE" -eq 0 && "$REMOVE_OLD" -eq 1 ]]; then
  die "--remove-old cannot be combined with --no-recreate: removal is gated on the post-recreate live-mount verification. Recreate the service yourself, confirm it runs on the new path, then remove the old dir by hand (the printed 'rm -rf' command)."
fi

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

# Print the volume-entry lines of the TARGET service's `volumes:` block ONLY.
# Detection and rewrite must be scoped to --service: another service using a
# short-form literal bind at the same container path must never be mistaken for
# this service's mount (that would repoint/rewrite the wrong line -- data loss).
compose_service_volume_lines() {
  [[ -n "$COMPOSE_FILE" ]] || return 0
  python3 -I - "$COMPOSE_FILE" "$SERVICE" <<'PY'
import sys
path, service = sys.argv[1], sys.argv[2]
with open(path, "r", encoding="utf-8", newline="") as f:
    lines = f.readlines()
def indent(s):
    return len(s) - len(s.lstrip(" "))

def strip_comment(line):
    # Drop an unquoted trailing `# ...` YAML comment. A '#' begins a comment
    # only at line start or after whitespace AND outside quotes; a '#' inside a
    # quoted value (or mid-token) is kept. Returns the entry with the comment
    # (and trailing whitespace) removed.
    out = []
    quote = None
    prev_ws = True  # start of line counts as "preceded by whitespace"
    for ch in line:
        if quote is not None:
            out.append(ch)
            if ch == quote:
                quote = None
            prev_ws = False
            continue
        if ch in ("'", '"'):
            quote = ch; out.append(ch); prev_ws = False; continue
        if ch == "#" and prev_ws:
            break
        out.append(ch)
        prev_ws = ch in (" ", "\t")
    return "".join(out).rstrip()

# Anchor to the TOP-LEVEL `services:` mapping and require the target to be a
# DIRECT CHILD of it. A same-named key under an `x-...` extension/anchor or any
# other top-level map (appearing before or after `services:`) must NEVER shadow
# the real service -- picking its ${VAR} would repoint the wrong mount and leave
# the target on its old path (a data-loss false success).
services_i = None
for idx, ln in enumerate(lines):
    s = ln.strip()
    if not s or s.startswith("#"):
        continue
    if indent(ln) == 0 and s == "services:":
        services_i = idx; break
if services_i is None:
    sys.exit(0)
svc_i = svc_ind = child_ind = None
for idx in range(services_i + 1, len(lines)):
    ln = lines[idx]; s = ln.strip()
    if not s or s.startswith("#"):
        continue
    ind = indent(ln)
    if ind == 0:
        break  # next top-level key -- left the services block
    if child_ind is None:
        child_ind = ind  # indent of the services map's direct children
    if ind == child_ind and s.endswith(":") and s[:-1].strip().strip("\"'") == service:
        svc_i, svc_ind = idx, ind; break
if svc_i is None:
    sys.exit(0)
vol_ind = None
for ln in lines[svc_i + 1:]:
    s = ln.strip()
    if s and not s.startswith("#") and indent(ln) <= svc_ind:
        break  # left the service block
    if vol_ind is None:
        if s == "volumes:":
            vol_ind = indent(ln)
        continue
    if s and not s.startswith("#") and indent(ln) <= vol_ind:
        break  # left the volumes block
    # Emit real volume entries only. Skip comment-only / blank lines, and strip
    # a trailing `# ...` comment from each entry: a commented-out
    # `# - ${VAR}:...` entry, or a ${VAR} buried in a trailing comment on a
    # literal mount, must never reach detect_env_key (it would harvest that
    # ${VAR} and repoint the wrong mount -- a data-loss false success).
    if not s or s.startswith("#"):
        continue
    entry = strip_comment(ln)
    if entry.strip():
        print(entry)
PY
}

# The env var that the Compose /web-manager-data mount line interpolates. The
# OUTERMOST ${VAR...} wins (Compose resolves it first); that is the key whose
# value actually selects the mount source. Left empty if the line has no
# variable (a hardcoded path -- cannot be repointed via env). Scoped to the
# target service's volumes block.
detect_env_key() {
  [[ -n "$COMPOSE_FILE" ]] || return 0
  # Honor an operator-supplied --env-key: detection must not overwrite it
  # (mirrors detect_literal_source's guard). This is the documented workaround
  # for layouts the scoper below can't read.
  [[ -z "$ENV_KEY" ]] || return 0
  # The interpolated ${VAR} /web-manager-data mount line in the TARGET service's
  # volumes block, if any. This is ONLY ever scoped to --service -- NEVER a
  # whole-file grep: a key picked from another service's ${VAR}:/web-manager-data
  # mount would repoint the wrong service and leave the target on its old path,
  # reporting SUCCESS (a data-loss false success). Layouts the scoper can't read
  # (indentless `volumes:` sequence, trailing-comment service header, quoted
  # short-form mount) yield nothing here and fall through to the helpful refusal,
  # which directs the operator to --env-key NAME or --rewrite-compose.
  #
  # Finding 3: the mount SOURCE must be EXACTLY a bare `${VAR}` or
  # `${VAR:-default}` to be treated as a repointable env key. A partial-path
  # source like `${DATA_ROOT}/manager:/web-manager-data` is NOT repointable
  # (setting DATA_ROOT=<dest> would yield `<dest>/manager`), so it is rejected
  # here and falls through to literal/refuse -- never a false success that
  # silently corrupts the path.
  # NB: the program is passed via -c (NOT a `<<heredoc`), so python's stdin stays
  # connected to the pipe from compose_service_volume_lines. A heredoc would
  # occupy stdin, starving the read and SIGPIPE-ing the upstream scoper.
  # shellcheck disable=SC2016  # $ inside the single-quoted program is Python regex, not shell expansion
  ENV_KEY="$(compose_service_volume_lines | python3 -I -c '
import re, sys
dest = sys.argv[1]
# Short-form bind whose TARGET is exactly `dest`; capture its SOURCE. A quoted
# WHOLE-mount short form (- "${VAR}:/web-manager-data") does not match here
# (the quote wraps source+target) and is left to the refusal, as before.
entry_re = re.compile(r"^\s*-\s+(?P<src>[^#\s][^#]*?):" + re.escape(dest) + r"(?:[:\s].*)?$")
# A clean, fully-repointable env source: nothing but ${VAR} or ${VAR:-default}.
bare_var_re = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-[^}]*)?\}$")
# Consume ALL input (do not break early) so the upstream scoper never gets a
# SIGPIPE/BrokenPipeError that would trip set -o pipefail; keep the FIRST key.
key = ""
for raw in sys.stdin:
    m = entry_re.match(raw.rstrip("\n"))
    if not m:
        continue
    vm = bare_var_re.match(m.group("src").strip())
    if vm and not key:
        key = vm.group(1)
if key:
    print(key)
' "$MOUNT_DEST")"
  return 0
}

# If the /web-manager-data mount SOURCE is a literal host path (no ${VAR} AND no
# bare $VAR -- both are Compose interpolation, not a literal), there is no env
# key to set; repointing it means rewriting the Compose line (opt-in
# --rewrite-compose). Only relevant when env-key detection found none. Scoped to
# the target service's volumes block.
detect_literal_source() {
  [[ -n "$COMPOSE_FILE" ]] || return 0
  [[ -z "$ENV_KEY" ]] || return 0
  local line
  line="$(compose_service_volume_lines | grep -E ":${MOUNT_DEST}(\$|:|[[:space:]])" | grep -vE '\$' | head -n 1 || true)"
  [[ -n "$line" ]] && COMPOSE_MOUNT_IS_LITERAL=1
  return 0  # never let an empty match fail under `set -e` (falls to helpful error)
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

# ---- Rewrite ONLY the one hardcoded /web-manager-data mount-source line ------
# Saves <file>.bak first, replaces just the source token on the single matching
# short-form bind line, and verifies every other line is byte-identical.
rewrite_compose_source() {
  local file="$1" dest="$2" val="$3"
  local bak="${file}.bak"
  # Keep the FIRST backup: a re-run (--resume) must not clobber the original
  # with an already-rewritten copy, or rollback becomes impossible.
  if [[ ! -e "$bak" ]]; then
    cp -p -- "$file" "$bak" || die "could not write compose backup: $bak"
  fi
  python3 -I - "$file" "$dest" "$val" "$SERVICE" <<'PY' || { cp -p -- "$bak" "$file" 2>/dev/null || true; die "compose rewrite failed -- original restored from $bak"; }
import re, sys
path, dest, val, service = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
with open(path, "r", encoding="utf-8", newline="") as f:
    lines = f.readlines()
def indent(s):
    return len(s) - len(s.lstrip(" "))
# Restrict the rewrite to the TARGET service's volumes block [lo, hi): a literal
# bind at the same container path under a DIFFERENT service must never be hit.
# The target is matched as a DIRECT CHILD of the TOP-LEVEL `services:` map -- a
# same-named key under an `x-...` extension/anchor (or any other top-level map)
# must never shadow it, or the rewrite would edit the wrong service's line and
# leave the real service on its old path while reporting SUCCESS (data loss).
services_i = None
for idx, ln in enumerate(lines):
    s = ln.strip()
    if not s or s.startswith("#"):
        continue
    if indent(ln) == 0 and s == "services:":
        services_i = idx; break
if services_i is None:
    sys.exit("top-level services: mapping not found in compose file")
svc_i = svc_ind = child_ind = None
for idx in range(services_i + 1, len(lines)):
    ln = lines[idx]; s = ln.strip()
    if not s or s.startswith("#"):
        continue
    ind = indent(ln)
    if ind == 0:
        break  # next top-level key -- left the services block
    if child_ind is None:
        child_ind = ind
    if ind == child_ind and s.endswith(":") and s[:-1].strip().strip("\"'") == service:
        svc_i, svc_ind = idx, ind; break
if svc_i is None:
    sys.exit("service %r not found under top-level services:" % service)
lo = hi = vol_ind = None
j = svc_i + 1
while j < len(lines):
    ln = lines[j]; s = ln.strip()
    if s and not s.startswith("#") and indent(ln) <= svc_ind:
        break
    if vol_ind is None:
        if s == "volumes:":
            vol_ind = indent(ln); lo = j + 1
    elif s and not s.startswith("#") and indent(ln) <= vol_ind:
        break
    j += 1
if lo is None:
    sys.exit("service %r has no volumes: block" % service)
hi = j
# A short-form bind whose target is exactly `dest` and whose source is a literal
# host path (no $ interpolation; Linux paths contain no ':'). head=indent+'- '
# (the '- ' separator space is REQUIRED and captured so it can't be absorbed
# into src); src=the literal source (no leading space, no ':' or '$');
# rest=':<dest>'+optional mode/comment/closing-quote.
pat = re.compile(
    r'^(?P<head>\s*-\s+["\']?)(?P<src>[^:$\s][^:$]*)'
    r'(?P<rest>:' + re.escape(dest) + r'(?:[:\s].*)?["\']?\s*)$'
)
hits = [i for i in range(lo, hi)
        if '$' not in lines[i] and pat.match(lines[i].rstrip('\n'))]
if len(hits) != 1:
    sys.exit("expected exactly one hardcoded %s mount line in service %r, found %d"
             % (dest, service, len(hits)))
i = hits[0]
m = pat.match(lines[i].rstrip('\n'))
if m.group('src') == val:
    sys.exit(0)  # idempotent: already points at the new path -- nothing to do
nl = '\n' if lines[i].endswith('\n') else ''
newline = m.group('head') + val + m.group('rest') + nl
# Re-validate the edited line is still a well-formed `  - <val>:<dest>` entry
# (separator space intact); abort WITHOUT writing if the edit malformed it.
if not re.match(r'^\s*-\s+' + re.escape(val) + ':' + re.escape(dest) + r'(?:[:\s].*)?["\']?\s*$',
                newline.rstrip('\n')):
    sys.exit("post-edit validation failed: edited mount line is malformed")
lines[i] = newline
with open(path, "w", encoding="utf-8", newline="") as f:
    f.writelines(lines)
PY
  # Verify only the single mount line changed (siblings byte-identical). 2 =
  # the one mount line differs from the backup; 0 = idempotent re-run (source
  # already at the new path, nothing rewritten).
  if command -v diff >/dev/null 2>&1; then
    local changed
    changed="$(diff -- "$bak" "$file" | grep -cE '^[<>]' || true)"
    [[ "$changed" -eq 2 || "$changed" -eq 0 ]] \
      || die "compose rewrite touched more than the single mount line ($changed changed lines) -- restore from $bak"
  fi
  log "Compose mount line rewritten (pre-change copy saved at $bak)."
}

# =============================================================================
# Validate inputs
# =============================================================================
[[ -n "$DEST" ]] || die "--dest is required (the new absolute host path)"
case "$DEST" in /*|[A-Za-z]:[\\/]*) : ;; *) die "--dest must be an absolute path: $DEST" ;; esac
case "$DEST" in *[$' \t\n']*) die "--dest must not contain whitespace (env-file safety): $DEST" ;; esac
# A bind-mount source ending up in Compose/YAML or an env value must not carry
# characters that re-delimit it: ':' splits a short-form bind, '$' interpolates,
# quotes/backtick break quoting. Linux data-dir paths never contain these.
case "$DEST" in *[:'$'\"\'\`]*) die "--dest must not contain ':' '\$' quotes or backtick (compose/env safety): $DEST" ;; esac

resolve_compose_file
detect_env_key
detect_literal_source
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

# How the mount is repointed:
#   env            -> set one key in the env file (DEFAULT; mount uses ${VAR})
#   compose        -> rewrite the one hardcoded mount line (opt-in)
#   compose-needed -> mount is hardcoded but --rewrite-compose was not passed
#   "" (empty)     -> could not determine either
REPOINT_MODE=""
if [[ -n "$ENV_KEY" ]]; then
  REPOINT_MODE="env"
  if [[ "$REWRITE_COMPOSE" -eq 1 ]]; then
    warn "--rewrite-compose ignored: the $MOUNT_DEST mount uses \${$ENV_KEY}; repointing via the env file instead."
  fi
elif [[ "$COMPOSE_MOUNT_IS_LITERAL" -eq 1 && "$REWRITE_COMPOSE" -eq 1 ]]; then
  REPOINT_MODE="compose"
elif [[ "$COMPOSE_MOUNT_IS_LITERAL" -eq 1 ]]; then
  REPOINT_MODE="compose-needed"
fi

case "$REPOINT_MODE" in
  env)
    REPOINT_DESC="set ${ENV_KEY}=$DST_CANON in ${ENV_FILE:-<none>} (other keys untouched)"
    ROLLBACK_HINT="set $ENV_KEY=$SRC_CANON in $ENV_FILE" ;;
  compose)
    REPOINT_DESC="REWRITE the $MOUNT_DEST mount source in $COMPOSE_FILE (<compose>.bak saved first)  ->  $DST_CANON"
    ROLLBACK_HINT="restore the $MOUNT_DEST mount source in $COMPOSE_FILE to $SRC_CANON (pre-change copy at ${COMPOSE_FILE}.bak)" ;;
  compose-needed)
    REPOINT_DESC="HARDCODED source in ${COMPOSE_FILE:-<compose file>} -- pass --rewrite-compose to rewrite it in place (or edit by hand)"
    ROLLBACK_HINT="restore the $MOUNT_DEST mount source in ${COMPOSE_FILE:-the Compose file} to $SRC_CANON" ;;
  *)
    REPOINT_DESC="<none detected>"
    ROLLBACK_HINT="restore the previous $MOUNT_DEST mount source" ;;
esac

# Repointing the mount needs a writable target. --no-recreate operators can
# still want it set; only refuse when we truly have nowhere to write it.
if [[ "$APPLY" -eq 1 ]]; then
  case "$REPOINT_MODE" in
    env)
      [[ -n "$ENV_FILE" ]] || die "no env file to edit -- pass --env-file, or --stack-dir/--compose-file so <compose dir>/.env is used" ;;
    compose)
      [[ -n "$COMPOSE_FILE" ]] || die "cannot rewrite Compose without a Compose file -- pass --compose-file or --stack-dir" ;;
    compose-needed)
      die "the $MOUNT_DEST mount source in ${COMPOSE_FILE:-the Compose file} is a HARDCODED host path (no \${VAR} to set in the env file). Re-run with --rewrite-compose to rewrite that one Compose line in place (a <compose>.bak is saved first), or edit the Compose file by hand." ;;
    *)
      if [[ "$REWRITE_COMPOSE" -eq 1 ]]; then
        die "--rewrite-compose: could not find a hardcoded $MOUNT_DEST mount line in ${COMPOSE_FILE:-<no compose file>} -- pass --compose-file/--stack-dir pointing at the stack's Compose file"
      fi
      die "could not determine how the $MOUNT_DEST mount is repointed. Auto-detection reads the standard indented volumes block only; an indentless volumes sequence, a trailing-comment service header, or a quoted mount can't be scoped. Pass --env-key NAME if the mount uses an env var (e.g. \${BEETS_WEB_MANAGER_DATA_PATH}), or --rewrite-compose if it is a hardcoded literal path. Also pass --compose-file/--stack-dir pointing at the stack's Compose file." ;;
  esac
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
Repoint:          $REPOINT_DESC
Backup .tar.gz:   under $BACKUP_DIR
Copy:             $(command -v rsync >/dev/null 2>&1 && echo 'rsync -aHAX --numeric-ids' || echo 'cp -a') (old dir is kept)
Recreate:         $([[ "$RECREATE" -eq 1 ]] && echo "docker compose -f <file> up -d $SERVICE, then verify health + /api/health" || echo "SKIPPED (--no-recreate)")
Remove old dir:   $([[ "$REMOVE_OLD" -eq 1 ]] && echo "yes, after health passes (--remove-old)" || echo "no (left for operator; removal command printed on success)")
ROLLBACK:         $ROLLBACK_HINT and recreate $SERVICE
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
require_cmd tar
( umask 077; mkdir -p "$BACKUP_DIR" )
STAMP="$(date -u +%Y%m%d-%H%M%S)"
BACKUP_FILE="$BACKUP_DIR/web-manager-data-${STAMP}.tar.gz"
log "Backing up $SRC_CANON -> $BACKUP_FILE ..."
# The archive holds secrets (.env/.auth_token/.browser_password/...). Create it
# under a restrictive umask so it is mode 600 from birth, not 644-then-chmod
# (which left a brief world-readable window and was best-effort).
( umask 077; tar -czf "$BACKUP_FILE" -C "$(dirname "$SRC_CANON")" "$(basename "$SRC_CANON")" )
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

# 4) Repoint the mount -- env file (one key) by default, or the one Compose
#    mount line when --rewrite-compose was used for a hardcoded source.
if [[ "$REPOINT_MODE" == compose ]]; then
  log "Rewriting the $MOUNT_DEST mount source in $COMPOSE_FILE (<compose>.bak saved first) -> $DST_CANON ..."
  rewrite_compose_source "$COMPOSE_FILE" "$MOUNT_DEST" "$DST_CANON"
else
  log "Setting $ENV_KEY=$DST_CANON in $ENV_FILE (other keys untouched) ..."
  set_env_key "$ENV_FILE" "$ENV_KEY" "$DST_CANON"
fi

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
  $ROLLBACK_HINT   (or re-run this script with --source/--dest swapped)
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
  warn "--no-recreate: mount repointed but the service was NOT recreated. Recreate it yourself:"
  warn "  docker compose -f ${COMPOSE_FILE:-<compose file>} up -d $SERVICE   # then verify /api/health"
fi

# 6) Remove old dir only if asked and only after everything above passed.
if [[ "$REMOVE_OLD" -eq 1 ]]; then
  # F1 safety net: before deleting the OLD dir, prove the LIVE container really
  # mounts the NEW path at MOUNT_DEST. If the repoint hit the wrong service/line
  # the app is still running on the old dir -- deleting it would be data loss.
  if [[ "$RECREATE" -eq 1 ]]; then
    # C4: emit the mount list as JSON and match in Python rather than
    # interpolating the operator-supplied $MOUNT_DEST into the Go template
    # (same pattern as resolve_source_from_docker above).
    live_src="$(docker inspect --format '{{json .Mounts}}' "$CID" 2>/dev/null | python3 -I -c '
import json, sys
dest = sys.argv[1]
for m in json.load(sys.stdin):
    if m.get("Destination") == dest:
        print(m.get("Source", "")); break
' "$MOUNT_DEST" 2>/dev/null || true)"
    [[ -n "$live_src" ]] || die "refusing --remove-old: could not read the live $MOUNT_DEST mount from the recreated container -- old data left intact at $SRC_CANON"
    [[ "$(canon_path "$live_src")" == "$DST_CANON" ]] \
      || die "refusing --remove-old: live container mounts $MOUNT_DEST from '$live_src', not the new path '$DST_CANON' -- the repoint did not take effect; old data left intact at $SRC_CANON"
    log "Verified: live container mounts $MOUNT_DEST from the new path."
  fi
  # Defense-in-depth: refuse to rm -rf a suspiciously shallow path (root or a
  # single top-level component), even though SRC_CANON is a verified data dir
  # and a backup exists. Requires at least two path segments (e.g. /a/b).
  case "$SRC_CANON" in
    /*/*) : ;;
    *) die "refusing to 'rm -rf' a too-shallow source path: $SRC_CANON" ;;
  esac
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
ROLLBACK (if ever needed): $ROLLBACK_HINT and
  docker compose -f ${COMPOSE_FILE:-<compose file>} up -d $SERVICE
DONE
fi

exit 0

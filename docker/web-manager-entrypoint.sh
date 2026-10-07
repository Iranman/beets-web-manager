#!/bin/bash
# Entrypoint for the beets-web-manager image. Runs briefly as root (only for
# the operations below, which genuinely require it) then execs the real
# application as an unprivileged user -- the application itself never runs
# as root.
#
# Two distinct problems this solves:
#
# 1. PUID/PGID: the image bakes in a default `beets` user at UID/GID 1000
#    at build time. Docker Compose has always exposed PUID/PGID environment
#    variables for this service, but nothing ever read them at runtime --
#    the container silently always ran as UID 1000 regardless of what a
#    user set. This remaps the existing `beets` user/group to the
#    requested PUID/PGID (default 1000/1000, unchanged) before dropping
#    privileges, so the setting genuinely works.
#
# 2. Fresh bind mounts: a host directory created by whatever process set up
#    the Compose project (root, a CI runner, a NAS's own default umask,
#    etc.) will not generally already be owned by this container's UID,
#    PUID/PGID customized or not -- this is what actually broke a
#    completely fresh /data mount (PermissionError creating .auth_token /
#    .flask_secret_key) even with no PUID/PGID override in play at all.
#
# /web-manager-data is exclusively Web Manager's own state (settings,
# tokens, session keys, audit/transaction logs) and /config is shared only
# with the beets sibling container's own equivalent PUID/PGID-driven
# ownership fix -- both are small, so a full recursive chown is safe and
# fast; it runs only when the top level is not already PUID:PGID (#282). /music and /downloads can be enormous real media
# libraries: only the top-level directory's own ownership is fixed (so the
# app can create new files/subdirectories there), never a recursive walk
# over existing files.
set -e

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

# SEC-9: the application must never run as root. usermod -o would happily
# give the `beets` user UID 0, so refuse that (and non-numeric ids) here.
case "$PUID" in ''|*[!0-9]*) echo "ERROR: PUID must be a numeric user id (got '$PUID')" >&2; exit 64 ;; esac
case "$PGID" in ''|*[!0-9]*) echo "ERROR: PGID must be a numeric group id (got '$PGID')" >&2; exit 64 ;; esac
if [ "$PUID" -eq 0 ] || [ "$PGID" -eq 0 ]; then
    echo "ERROR: PUID/PGID 0 (root) is not allowed; set PUID/PGID to the owner of your media folders (e.g. 1000)." >&2
    exit 64
fi

CURRENT_UID="$(id -u beets)"
CURRENT_GID="$(id -g beets)"
RUN_AS="beets:beets"

if [ "$PUID" != "$CURRENT_UID" ] || [ "$PGID" != "$CURRENT_GID" ]; then
    if [ -w /etc/passwd ] && [ -w /etc/group ]; then
        if [ "$PUID" != "$CURRENT_UID" ]; then
            usermod -o -u "$PUID" beets
        fi
        if [ "$PGID" != "$CURRENT_GID" ]; then
            groupmod -o -g "$PGID" beets
        fi
        chown -R beets:beets /app
    else
        # BI-8: hardened variants run with a read-only root filesystem, so
        # /etc/passwd cannot be edited. Run as the numeric PUID:PGID
        # instead; /app stays world-readable and needs no chown.
        RUN_AS="$PUID:$PGID"
        echo "read-only root filesystem: running as numeric uid:gid $RUN_AS" >&2
    fi
fi

# #282: the app keeps its data tree private (0700, PUID-owned). Under the
# hardened settings (cap_drop ALL + CHOWN) root has no DAC_READ_SEARCH, so it
# cannot even list such a tree and `chown -R` aborted every second boot. A
# tree whose top level was already PUID:PGID before the walk is the app's own,
# so a walk that cannot read it is expected there and is not an error.
# The walk still always runs: with default capabilities it repairs stray
# root-owned files (from `docker exec`, `sudo cp`, a root restore).
own_tree() {
    local owned=0 err
    if [ "$(stat -c '%u:%g' "$1")" = "$PUID:$PGID" ]; then
        owned=1
    fi
    if ! err="$(chown -R "$RUN_AS" "$1" 2>&1)"; then
        # Hardened + already ours: chown's "cannot read directory" is
        # expected on every boot, so keep it out of the logs.
        [ "$owned" = 1 ] && return 0
        echo "$err" >&2
        echo "ERROR: cannot take ownership of $1 for $PUID:$PGID. If PUID/PGID changed under the hardened (cap_drop: ALL) settings, root cannot read the old private tree: run 'chown -R $PUID:$PGID' on the host directory once, then start again." >&2
        exit 1
    fi
}

own_tree /web-manager-data
# /config is only mounted in the bundled-Beets layout. In the external-Beets
# layout it is the image's own directory on a read-only root filesystem, and
# an unconditional chown aborted startup there (EROFS under `set -e`).
if awk '$5 == "/config" { found = 1 } END { exit !found }' /proc/self/mountinfo; then
    own_tree /config
fi
chown "$RUN_AS" /music /downloads 2>/dev/null || true

exec gosu "$RUN_AS" "$@"

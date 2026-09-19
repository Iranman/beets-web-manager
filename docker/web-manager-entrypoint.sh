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
# /data and /web-manager-data are exclusively Web Manager's own state
# (settings, tokens, session keys, audit logs) and /config is shared only
# with the beets sibling container's own equivalent PUID/PGID-driven
# ownership fix -- all three are small, so a full recursive chown on every
# start is safe and fast. /music and /downloads can be enormous real media
# libraries: only the top-level directory's own ownership is fixed (so the
# app can create new files/subdirectories there), never a recursive walk
# over existing files.
set -e

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

CURRENT_UID="$(id -u beets)"
CURRENT_GID="$(id -g beets)"

if [ "$PUID" != "$CURRENT_UID" ] || [ "$PGID" != "$CURRENT_GID" ]; then
    if [ "$PUID" != "$CURRENT_UID" ]; then
        usermod -o -u "$PUID" beets
    fi
    if [ "$PGID" != "$CURRENT_GID" ]; then
        groupmod -o -g "$PGID" beets
    fi
    chown -R beets:beets /app
fi

chown -R beets:beets /data /web-manager-data /config
chown beets:beets /music /downloads 2>/dev/null || true

exec gosu beets "$@"

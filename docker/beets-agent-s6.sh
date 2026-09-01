#!/usr/bin/with-contenv bash
# shellcheck shell=bash
#
# Runs the fail-closed database guard (beets_startup_guard.py) before the
# control agent -- which is the process that actually opens
# musiclibrary.blb in this architecture -- is allowed to start.
#
# This intentionally is NOT implemented as a custom-cont-init.d script:
# the stock LinuxServer.io init-custom-files runner logs a non-zero exit
# from a custom-cont-init.d script but does not stop the rest of the S6
# startup chain (confirmed by reading /etc/s6-overlay/s6-rc.d/init-custom-files/run
# in the base image), so a failing guard placed there would not actually
# block anything. Wrapping the control agent's own service script, and
# idling instead of exec'ing the agent on failure, is what makes this
# fail-closed for real.
GUARD_SCRIPT="/opt/beets-web-manager-agent/beets_startup_guard.py"
LIBRARY_PATH="${BEETS_LIBRARY:-/config/musiclibrary.blb}"

# Wave 27 CodeQL/permission closure: unlike the base image's own svc-beets
# (see /etc/s6-overlay/s6-rc.d/svc-beets/run), /custom-services.d/ scripts
# are NOT privilege-dropped automatically -- without this, both the guard
# and the control agent ran as root for the container's entire life, and
# anything either process wrote (config.yaml, the Beets library DB,
# transaction ledgers) ended up root-owned instead of owned by the
# configured PUID/PGID, contradicting the 0600 "owner-only" permission
# contract those files are otherwise held to (root-owned still means
# nobody unauthorized can read it, but it silently drops least-privilege,
# and a later legitimate non-root write path would fail against it).
# Matches svc-beets's own s6-setuidgid idiom, including its
# LSIO_NON_ROOT_USER escape hatch for containers already configured to run
# fully non-root end to end.
if [[ -z ${LSIO_NON_ROOT_USER} ]]; then
    RUN_AS=(s6-setuidgid abc)
else
    RUN_AS=()
fi

echo "[beets-agent] running startup guard against ${LIBRARY_PATH}..."
if ! "${RUN_AS[@]}" /lsiopy/bin/python3 "${GUARD_SCRIPT}" "${LIBRARY_PATH}"; then
    echo "[beets-agent] STARTUP GUARD REFUSED -- control agent will NOT be started."
    echo "[beets-agent] Fix the underlying database/config issue, then restart this container."
    exec sleep infinity
fi
echo "[beets-agent] startup guard passed."

echo "[beets-agent] Starting Beets Control Agent under S6 supervision..."
exec "${RUN_AS[@]}" /lsiopy/bin/python3 /opt/beets-web-manager-agent/beets_control_agent.py

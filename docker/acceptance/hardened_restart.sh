#!/bin/bash
# Issue #282: under the hardened Compose settings (cap_drop ALL + CHOWN/
# SETUID/SETGID, read_only, no-new-privileges) the container must come up
# healthy on first boot AND on every later boot that reuses its data,
# for a named volume and a bind mount. The first boot of each case starts
# from a root-owned data directory, as a fresh host mount would be.
#
# Usage: docker/acceptance/hardened_restart.sh [image] [bind-mount-dir]
# Uses the real examples/docker-compose.external-beets.yml service
# definition so the hardening flags tested here cannot drift from it.
set -euo pipefail

IMAGE="${1:-beets-web-manager:ci}"
BIND_DIR="${2:-}"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PROJECT="bwm-sec-hardened"
VOLUME="${PROJECT}-data"
OVERRIDE="$(mktemp)"
export BEETS_WEB_URL=http://beets.ci.invalid:8337
export BEETS_OUTBOUND_ALLOWLIST=beets.ci.invalid:8337
export BEETS_WEB_BIND_ADDRESS=127.0.0.1 WEBCONTROL_PORT=18339

compose() {
    docker compose -p "$PROJECT" -f "$ROOT/examples/docker-compose.external-beets.yml" -f "$OVERRIDE" "$@"
}

cleanup() {
    compose down -v --remove-orphans >/dev/null 2>&1 || true
    docker volume rm "$VOLUME" >/dev/null 2>&1 || true
    rm -f "$OVERRIDE"
}
trap cleanup EXIT

wait_healthy() {
    local cid status
    cid="$(compose ps -q beets-web-manager)"
    for _ in $(seq 1 60); do
        status="$(docker inspect -f '{{.State.Status}}/{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$cid")"
        case "$status" in
            running/healthy) return 0 ;;
            exited/*|dead/*) break ;;
        esac
        # Don't wait for the 30s healthcheck interval: probe the same URL.
        if curl -fsS "http://127.0.0.1:${WEBCONTROL_PORT}/api/health" >/dev/null 2>&1; then
            return 0
        fi
        sleep 2
    done
    echo "::error::container not healthy (state $status)" >&2
    docker logs --tail 50 "$cid" >&2 || true
    return 1
}

# $1 = label, $2 = volume source (named volume or host path)
run_case() {
    cat >"$OVERRIDE" <<EOF
services:
  beets-web-manager:
    image: $IMAGE
    container_name: ${PROJECT}-app
    volumes:
      - $2:/web-manager-data
EOF
    case "$2" in
        /*) ;;
        *) printf 'volumes:\n  %s:\n    external: true\n' "$2" >>"$OVERRIDE" ;;
    esac
    for boot in 1 2 3; do
        echo "== $1: boot $boot"
        if [ "$boot" = 3 ]; then
            # Plain restart of the same container, not just recreate.
            compose restart beets-web-manager >/dev/null
        else
            compose up -d --force-recreate >/dev/null
        fi
        wait_healthy
        if [ "$boot" = 1 ]; then
            # Precondition for the regression: the app made its tree private.
            MSYS_NO_PATHCONV=1 docker run --rm -v "$2:/d" alpine stat -c '%a %u:%g' /d | grep -qx '700 1000:1000' \
                || { echo "::error::$1: data dir not 0700 1000:1000 after first boot" >&2; exit 1; }
        fi
    done
    compose down >/dev/null
}

# Named volume, first created root-owned.
docker volume create "$VOLUME" >/dev/null
MSYS_NO_PATHCONV=1 docker run --rm -v "$VOLUME:/d" alpine sh -c 'chown 0:0 /d && chmod 755 /d'
run_case "named volume" "$VOLUME"

if [ -n "$BIND_DIR" ]; then
    # Bind mount, first created root-owned (as `sudo mkdir` would).
    MSYS_NO_PATHCONV=1 docker run --rm -v "$(dirname "$BIND_DIR"):/p" alpine sh -c "rm -rf /p/$(basename "$BIND_DIR") && install -d -o 0 -g 0 -m 755 /p/$(basename "$BIND_DIR")"
    run_case "bind mount" "$BIND_DIR"
    MSYS_NO_PATHCONV=1 docker run --rm -v "$(dirname "$BIND_DIR"):/p" alpine rm -rf "/p/$(basename "$BIND_DIR")"
fi
echo "hardened restart check: OK"

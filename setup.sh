#!/usr/bin/env bash
# Setup script for Beets Web Manager Docker Compose installation.
set -euo pipefail
cd "$(dirname "$0")"

DEV_MODE=0
for arg in "$@"; do
  if [ "$arg" = "--dev" ]; then
    DEV_MODE=1
  fi
done

echo "==> Checking Docker..."
if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: Docker is not installed. Install Docker Desktop or Docker Engine first: https://docs.docker.com/get-docker/" >&2
  exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
  echo "ERROR: Docker Compose v2 (the 'docker compose' subcommand) is required." >&2
  exit 1
fi
if ! docker info >/dev/null 2>&1; then
  echo "ERROR: Docker daemon is not running. Start Docker Desktop (or the docker service) and re-run this script." >&2
  exit 1
fi

echo "==> Creating persistent data directories..."
mkdir -p web-manager-data
# Wave 24 final review round 3, section 21-24 (found only by actually
# booting the real container against a freshly-created host directory,
# not by unit tests, which never run inside the read_only container at
# all): the published image runs as a fixed non-root `beets` user (build-
# time UID/GID, default 1000 -- the runtime PUID/PGID in .env only affect
# a locally-built image, never the published ghcr.io one), and a host
# bind mount's on-disk permissions come entirely from THIS directory as
# just created, not from anything baked into the image. Left at the
# default mkdir mode, a container UID that doesn't happen to match
# whoever ran this script cannot write its own state here (the auth-
# token bootstrap file, the transaction audit ledger, and more) --
# world-writable is the simplest correct fix for a directory that holds
# no data yet and exists solely to become this one container's own
# volume.
chmod 777 web-manager-data
if [ "$DEV_MODE" -eq 1 ]; then
  mkdir -p config data/music data/downloads
fi

# Set/replace KEY=VALUE in .env without going anywhere near sed replacement-
# text escaping: a value containing '&', '/', or '\' (all valid, common
# password characters) corrupts a naive `sed s/.../${value}/` substitution
# (e.g. '&' expands to the whole matched line) rather than being written as
# typed. Deleting the old line and appending the new one sidesteps that
# class of bug entirely -- printf '%s' never reinterprets its argument.
set_env_value() {
  key="$1"; value="$2"
  if grep -q "^${key}=" .env 2>/dev/null; then
    grep -v "^${key}=" .env > .env.tmp
    mv .env.tmp .env
  fi
  printf '%s=%s\n' "$key" "$value" >> .env
}

FRESH_ENV=0
if [ -f .env ]; then
  echo "==> .env already exists, leaving existing secrets untouched."
else
  echo "==> Creating .env from .env.example..."
  cp .env.example .env
  FRESH_ENV=1
  TOKEN="$(openssl rand -hex 32 2>/dev/null || head -c32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  API_TOKEN="$(openssl rand -hex 32 2>/dev/null || head -c32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  sed -i.bak "s/^BEETS_WEB_AUTH_TOKEN=.*/BEETS_WEB_AUTH_TOKEN=${TOKEN}/" .env && rm -f .env.bak
  sed -i.bak "s/^BEETS_API_TOKEN=.*/BEETS_API_TOKEN=${API_TOKEN}/" .env && rm -f .env.bak
  echo "    Generated random BEETS_WEB_AUTH_TOKEN and BEETS_API_TOKEN in .env."
fi

# Interactive Web Access prompt -- only on a genuinely fresh .env, so
# re-running setup.sh on an existing install never silently changes an
# already-configured bind address. .env.example ships BEETS_WEB_BIND_ADDRESS
# with a non-empty default (127.0.0.1), so "is it empty" can't gate this the
# way it gates the password prompt below.
if [ "$FRESH_ENV" -eq 1 ] && [ -t 0 ]; then
  echo ""
  echo "=== Web Access ==="
  echo "1. This computer only (127.0.0.1)"
  echo "2. Other devices on my local network (0.0.0.0)"
  echo ""
  read -r -p "Choose [2 for most NAS/server installs]: " BIND_CHOICE
  if [ "$BIND_CHOICE" = "1" ]; then
    BIND_ADDR="127.0.0.1"
  else
    BIND_ADDR="0.0.0.0"
  fi
  set_env_value BEETS_WEB_BIND_ADDRESS "$BIND_ADDR"
  if [ "$BIND_ADDR" = "0.0.0.0" ]; then
    echo "    Set BEETS_WEB_BIND_ADDRESS=0.0.0.0 in .env."
    echo "    This is the LISTENING address, not a browser URL -- from another device on your network, browse to:"
    echo "      http://<this-machine's-LAN-IP>:8337"
    echo "    Find this machine's LAN IP with 'ip addr' / 'ifconfig' / 'hostname -I', or your NAS's network settings page."
  else
    echo "    Set BEETS_WEB_BIND_ADDRESS=127.0.0.1 in .env -- only reachable from this computer, at http://localhost:8337"
  fi
fi

# Interactive Browser Login Prompt if BEETS_WEB_PASSWORD is not configured
WEB_PASS_VAL="$(grep -E '^BEETS_WEB_PASSWORD=' .env | cut -d= -f2- | tr -d '\r" ' || true)"
if [ -z "$WEB_PASS_VAL" ] && [ -t 0 ]; then
  echo ""
  echo "=== Browser Login Setup ==="
  echo "This is the username and password you will use to open Beets Web Manager in your browser."
  echo "Note: BEETS_WEB_AUTH_TOKEN (API bearer token) and BEETS_API_TOKEN (engine token) are separate internal tokens."
  echo ""
  read -r -p "Browser username [admin]: " INPUT_USER
  USER_VAL="${INPUT_USER:-admin}"
  USER_VAL="$(echo "$USER_VAL" | tr -d '\r\n')"

  while true; do
    read -r -s -p "Browser password (min 32 chars, upper, lower, number, special): " PASS_VAL
    echo ""
    PASS_VAL="$(echo "$PASS_VAL" | tr -d '\r\n')"
    if [ -z "$PASS_VAL" ]; then
      echo "Password cannot be empty." >&2
      continue
    fi
    ERRS=""
    if [ "${#PASS_VAL}" -lt 32 ]; then
      ERRS="${ERRS} at least 32 characters;"
    fi
    if ! echo "$PASS_VAL" | grep -q '[A-Z]'; then
      ERRS="${ERRS} an uppercase letter;"
    fi
    if ! echo "$PASS_VAL" | grep -q '[a-z]'; then
      ERRS="${ERRS} a lowercase letter;"
    fi
    if ! echo "$PASS_VAL" | grep -q '[0-9]'; then
      ERRS="${ERRS} a number;"
    fi
    if ! echo "$PASS_VAL" | grep -q '[^a-zA-Z0-9]'; then
      ERRS="${ERRS} a special character;"
    fi
    if [ -z "$ERRS" ]; then
      break
    else
      echo "Password does not meet requirements:${ERRS}" >&2
    fi
  done

  set_env_value BEETS_WEB_USERNAME "$USER_VAL"
  set_env_value BEETS_WEB_PASSWORD "$PASS_VAL"
  echo "    Configured browser username ($USER_VAL) and password in .env."
fi

# Validation
API_TOKEN_VAL="$(grep -E '^BEETS_API_TOKEN=' .env | cut -d= -f2- | tr -d '\r" ' || true)"
if [ -z "$API_TOKEN_VAL" ] || [ "$API_TOKEN_VAL" = "changeme" ]; then
  echo "WARNING: BEETS_API_TOKEN is unconfigured or set to 'changeme' placeholder." >&2
  echo "Please set BEETS_API_TOKEN in .env to match your Beets control agent." >&2
fi

API_URL_VAL="$(grep -E '^BEETS_API_URL=' .env | cut -d= -f2- | tr -d '\r" ' || true)"
if [ -z "$API_URL_VAL" ]; then
  if [ "$DEV_MODE" -eq 1 ]; then
    echo "WARNING: BEETS_API_URL is empty in .env. Defaulting to http://beets:8338 (docker-compose.dev.yml)." >&2
  else
    echo "WARNING: BEETS_API_URL is empty in .env. docker-compose.yml requires BEETS_API_URL to be set -- the container will fail to start without it." >&2
  fi
fi

if [ "$DEV_MODE" -eq 1 ]; then
  echo "==> Starting Beets Web Manager in DEVELOPMENT mode (source build)..."
  docker compose -f docker-compose.dev.yml up -d --build
  COMPOSE_FILE="docker-compose.dev.yml"
else
  echo "==> Pulling published image from GitHub Container Registry..."
  docker compose pull beets-web-manager
  echo "==> Starting Beets Web Manager..."
  docker compose up -d beets-web-manager
  COMPOSE_FILE="docker-compose.yml"
fi

echo "==> Waiting for Beets Web Manager to become healthy..."
HEALTHY=0
for i in $(seq 1 30); do
  if docker compose -f "$COMPOSE_FILE" ps --format '{{.Health}}' 2>/dev/null | grep -q healthy; then
    HEALTHY=1
    break
  fi
  sleep 2
done

PORT="$(grep -E '^WEBCONTROL_PORT=' .env | cut -d= -f2 | tr -d '\r" ' || true)"
PORT="${PORT:-8337}"
BIND_ADDR_FINAL="$(grep -E '^BEETS_WEB_BIND_ADDRESS=' .env | cut -d= -f2 | tr -d '\r" ' || true)"

if [ "$HEALTHY" -eq 1 ]; then
  echo ""
  echo "SUCCESS: Beets Web Manager is running and healthy."
  echo "Access the UI at: http://localhost:${PORT}"
  if [ "$BIND_ADDR_FINAL" = "0.0.0.0" ]; then
    echo "It is also reachable from other devices on your network at:"
    echo "  http://<this-machine's-LAN-IP>:${PORT}"
    echo "(find this machine's LAN IP with 'ip addr' / 'ifconfig' / 'hostname -I')"
  fi
else
  echo ""
  echo "ERROR: Beets Web Manager did not reach healthy state within 60 seconds." >&2
  echo "Check container logs with: docker compose -f $COMPOSE_FILE logs beets-web-manager" >&2
  exit 1
fi

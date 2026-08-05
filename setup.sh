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
mkdir -p config data/music data/downloads web-manager-data

if [ -f .env ]; then
  echo "==> .env already exists, leaving existing secrets untouched."
else
  echo "==> Creating .env from .env.example..."
  cp .env.example .env
  TOKEN="$(openssl rand -hex 32 2>/dev/null || head -c32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  API_TOKEN="$(openssl rand -hex 32 2>/dev/null || head -c32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  sed -i.bak "s/^BEETS_WEB_AUTH_TOKEN=.*/BEETS_WEB_AUTH_TOKEN=${TOKEN}/" .env && rm -f .env.bak
  sed -i.bak "s/^BEETS_API_TOKEN=.*/BEETS_API_TOKEN=${API_TOKEN}/" .env && rm -f .env.bak
  echo "    Generated random BEETS_WEB_AUTH_TOKEN and BEETS_API_TOKEN in .env."
fi

# Validation
API_TOKEN_VAL="$(grep -E '^BEETS_API_TOKEN=' .env | cut -d= -f2- | tr -d '\r" ' || true)"
if [ -z "$API_TOKEN_VAL" ] || [ "$API_TOKEN_VAL" = "changeme" ]; then
  echo "WARNING: BEETS_API_TOKEN is unconfigured or set to 'changeme' placeholder." >&2
  echo "Please set BEETS_API_TOKEN in .env to match your Beets control agent." >&2
fi

API_URL_VAL="$(grep -E '^BEETS_API_URL=' .env | cut -d= -f2- | tr -d '\r" ' || true)"
if [ -z "$API_URL_VAL" ]; then
  echo "WARNING: BEETS_API_URL is empty in .env. Defaulting to http://beets:8338." >&2
fi

if [ "$DEV_MODE" -eq 1 ]; then
  echo "==> Starting Beets Web Manager in DEVELOPMENT mode (source build)..."
  docker compose -f docker-compose.dev.yml up -d --build
  COMPOSE_FILE="docker-compose.dev.yml"
else
  echo "==> Pulling published image from GitHub Container Registry..."
  docker compose pull
  echo "==> Starting Beets Web Manager..."
  docker compose up -d
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

if [ "$HEALTHY" -eq 1 ]; then
  echo ""
  echo "SUCCESS: Beets Web Manager is running and healthy."
  echo "Access the UI at: http://localhost:${PORT}"
else
  echo ""
  echo "ERROR: Beets Web Manager did not reach healthy state within 60 seconds." >&2
  echo "Check container logs with: docker compose -f $COMPOSE_FILE logs beets-web-manager" >&2
  exit 1
fi

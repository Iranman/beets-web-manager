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
mkdir -p beets music downloads web-manager config data/music data/downloads web-manager-data
chmod 777 web-manager web-manager-data 2>/dev/null || true
chmod 777 beets config 2>/dev/null || true
chmod 777 music data/music 2>/dev/null || true
chmod 777 downloads data/downloads 2>/dev/null || true

if [ ! -f "beets/config.yaml" ] && [ -f "config.yaml.example" ]; then
  cp config.yaml.example beets/config.yaml
  echo "    Initialized default beets/config.yaml from template."
fi
if [ ! -f "config/config.yaml" ] && [ -f "config.yaml.example" ]; then
  cp config.yaml.example config/config.yaml
fi

# Set/replace KEY=VALUE in .env safely
set_env_value() {
  key="$1"; value="$2"
  if grep -q "^${key}=" .env 2>/dev/null; then
    grep -v "^${key}=" .env > .env.tmp
    mv .env.tmp .env
  fi
  printf '%s=%s\n' "$key" "$value" >> .env
}

get_lan_ip() {
  local ip=""
  if command -v hostname >/dev/null 2>&1; then
    ip="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
  fi
  if [ -z "$ip" ] && command -v ip >/dev/null 2>&1; then
    ip="$(ip route get 1.1.1.1 2>/dev/null | grep -oP 'src \K\S+' || true)"
  fi
  if [ -z "$ip" ] && command -v ifconfig >/dev/null 2>&1; then
    ip="$(ifconfig 2>/dev/null | grep -Eo 'inet (addr:)?([0-9]*\.){3}[0-9]*' | grep -Eo '([0-9]*\.){3}[0-9]*' | grep -v '127.0.0.1' | head -n1 || true)"
  fi
  echo "${ip:-<LAN-IP>}"
}

FRESH_ENV=0
if [ -f .env ]; then
  echo "==> .env already exists, leaving existing secrets untouched."
else
  echo "==> Creating .env from .env.example..."
  cp .env.example .env
  FRESH_ENV=1
  TOKEN="$(openssl rand -hex 32 2>/dev/null || head -c32 /dev/urandom 2>/dev/null | od -An -tx1 | tr -d ' \n' || python3 -c 'import secrets; print(secrets.token_hex(32))' 2>/dev/null || python -c 'import secrets; print(secrets.token_hex(32))' 2>/dev/null)"
  API_TOKEN="$(openssl rand -hex 32 2>/dev/null || head -c32 /dev/urandom 2>/dev/null | od -An -tx1 | tr -d ' \n' || python3 -c 'import secrets; print(secrets.token_hex(32))' 2>/dev/null || python -c 'import secrets; print(secrets.token_hex(32))' 2>/dev/null)"
  set_env_value "BEETS_WEB_AUTH_TOKEN" "${TOKEN}"
  set_env_value "BEETS_API_TOKEN" "${API_TOKEN}"
  if [ ! -f "beets/musiclibrary.blb" ] && [ ! -f "config/musiclibrary.blb" ]; then
    set_env_value "BEETS_EXPECT_EXISTING_LIBRARY" "0"
  else
    set_env_value "BEETS_EXPECT_EXISTING_LIBRARY" "1"
  fi
  echo "    Generated random BEETS_WEB_AUTH_TOKEN and BEETS_API_TOKEN in .env."
fi

# Interactive Web Access prompt -- only on a genuinely fresh .env
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
    echo "      http://$(get_lan_ip):8337"
  else
    echo "    Set BEETS_WEB_BIND_ADDRESS=127.0.0.1 in .env -- only reachable from this computer, at http://localhost:8337"
  fi
fi

# Validation
API_TOKEN_VAL="$(grep -E '^BEETS_API_TOKEN=' .env | cut -d= -f2- | tr -d '\r" ' || true)"
if [ -z "$API_TOKEN_VAL" ] || [ "$API_TOKEN_VAL" = "changeme" ]; then
  echo "WARNING: BEETS_API_TOKEN is unconfigured or set to 'changeme' placeholder." >&2
  echo "Please set BEETS_API_TOKEN in .env to match your Beets control agent." >&2
fi

if [ "$DEV_MODE" -eq 1 ]; then
  echo "==> Starting Beets stack in DEVELOPMENT mode (source build)..."
  docker compose -f docker-compose.dev.yml up -d --build
  COMPOSE_FILE="docker-compose.dev.yml"
else
  echo "==> Pulling published images from GitHub Container Registry..."
  docker compose pull
  echo "==> Starting Beets stack (beets engine + beets-web-manager)..."
  docker compose up -d
  COMPOSE_FILE="docker-compose.yml"
fi

echo "==> Waiting for services to become healthy..."
HEALTHY=0
for i in $(seq 1 45); do
  PS_OUTPUT="$(docker compose -f "$COMPOSE_FILE" ps --format '{{.Health}}' 2>/dev/null || true)"
  if [ -z "$PS_OUTPUT" ]; then
    PS_OUTPUT="$(docker compose -f "$COMPOSE_FILE" ps 2>/dev/null || true)"
  fi
  if echo "$PS_OUTPUT" | grep -q healthy; then
    HEALTHY=1
    break
  fi
  sleep 2
done

PORT="$(grep -E '^WEBCONTROL_PORT=' .env | cut -d= -f2 | tr -d '\r" ' || true)"
PORT="${PORT:-8337}"
BIND_ADDR_FINAL="$(grep -E '^BEETS_WEB_BIND_ADDRESS=' .env | cut -d= -f2 | tr -d '\r" ' || true)"
LAN_IP="$(get_lan_ip)"

if [ "$HEALTHY" -eq 1 ]; then
  echo ""
  echo "======================================================================"
  echo "SUCCESS: Beets and Beets Web Manager are running and healthy!"
  echo ""
  echo "Open the Web UI in your browser:"
  echo "  Local:   http://localhost:${PORT}"
  if [ "$BIND_ADDR_FINAL" = "0.0.0.0" ]; then
    echo "  Network: http://${LAN_IP}:${PORT}"
  fi
  echo ""
  echo "Complete initial setup and configure your admin login in the browser."
  echo ""
  echo "Management commands:"
  echo "  View logs:   docker compose -f $COMPOSE_FILE logs -f"
  echo "  Stop stack:  docker compose -f $COMPOSE_FILE down"
  echo "  Restart:     docker compose -f $COMPOSE_FILE restart"
  echo "======================================================================"
else
  echo ""
  echo "ERROR: Services did not reach healthy state within 90 seconds." >&2
  echo "Check container logs with: docker compose -f $COMPOSE_FILE logs" >&2
  exit 1
fi

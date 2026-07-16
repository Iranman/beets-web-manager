#!/usr/bin/env bash
# One-command setup for the Docker Compose installation.
set -euo pipefail
cd "$(dirname "$0")"

echo "==> Checking Docker..."
if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is not installed. Install Docker Desktop or Docker Engine first: https://docs.docker.com/get-docker/" >&2
  exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
  echo "Docker Compose v2 (the 'docker compose' subcommand) is required." >&2
  exit 1
fi
if ! docker info >/dev/null 2>&1; then
  echo "Docker daemon is not running. Start Docker Desktop (or the docker service) and re-run this script." >&2
  exit 1
fi

echo "==> Creating local directories..."
mkdir -p config data/music data/downloads backups

if [ -f .env ]; then
  echo "==> .env already exists, leaving it untouched."
else
  echo "==> Creating .env from .env.example..."
  cp .env.example .env
  TOKEN="$(openssl rand -hex 32 2>/dev/null || head -c32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  # Portable in-place edit (works on GNU and BSD/macOS sed).
  sed -i.bak "s/^BEETS_WEB_AUTH_TOKEN=.*/BEETS_WEB_AUTH_TOKEN=${TOKEN}/" .env && rm -f .env.bak
  echo "    Generated a random BEETS_WEB_AUTH_TOKEN in .env (not printed here)."
  echo "    Edit .env now to add AI/Plex/AcoustID/Lidarr credentials, or configure them later in the app."
fi

if [ -f config.yaml ]; then
  echo "==> config.yaml already exists, leaving it untouched."
else
  echo "==> Creating config.yaml from config.yaml.example..."
  cp config.yaml.example config.yaml
fi

read -r -p "Host UID for file ownership [$(id -u)]: " uid_input
read -r -p "Host GID for file ownership [$(id -g)]: " gid_input
UID_VAL="${uid_input:-$(id -u)}"
GID_VAL="${gid_input:-$(id -g)}"
if grep -q '^PUID=' .env; then
  sed -i.bak "s/^PUID=.*/PUID=${UID_VAL}/" .env && rm -f .env.bak
else
  echo "PUID=${UID_VAL}" >> .env
fi
if grep -q '^PGID=' .env; then
  sed -i.bak "s/^PGID=.*/PGID=${GID_VAL}/" .env && rm -f .env.bak
else
  echo "PGID=${GID_VAL}" >> .env
fi

echo "==> Building and starting the stack..."
docker compose up -d --build

echo "==> Waiting for the app to become healthy..."
for _ in $(seq 1 30); do
  if docker compose ps --format '{{.Health}}' 2>/dev/null | grep -q healthy; then
    break
  fi
  sleep 2
done

PORT="$(grep -m1 '^WEBCONTROL_PORT=' .env | cut -d= -f2)"
PORT="${PORT:-8337}"

echo ""
echo "Done. Open http://localhost:${PORT} in your browser."
echo "If this is a fresh install, complete the guided setup at http://localhost:${PORT}/setup."

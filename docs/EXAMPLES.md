# Beets Web Manager - Deployment Examples

This document provides deployment examples for **Beets Web Manager**. Beets runs inside the Web Manager container ([ADR 0006](adr/0006-embedded-beets.md)); there is no separate Beets container.

---

## 1. Standard Stack (Recommended)

```yaml
services:
  beets-web-manager:
    image: ghcr.io/iranman/beets-web-manager:latest
    container_name: beets-web-manager
    restart: unless-stopped
    ports:
      - "8337:8337"
    environment:
      - PUID=1000
      - PGID=1000
      - TZ=Etc/UTC
      - MUSIC_ROOT=/music
      - DOWNLOADS_ROOT=/downloads
      - BEETS_CONFIG=/config/config.yaml
    volumes:
      - ./beets:/config
      - /path/to/music:/music
      - /path/to/downloads:/downloads
      - ./web-manager:/web-manager-data
    healthcheck:
      # Unhealthy while Beets' self-test fails (beets.ok false).
      test: ["CMD", "python", "-c", "import json, sys, urllib.request; d = json.load(urllib.request.urlopen('http://127.0.0.1:8337/api/health', timeout=5)); sys.exit(0 if d.get('beets', {}).get('ok') else 1)"]
      interval: 10s
      timeout: 5s
      retries: 5
      start_period: 30s
```

`./beets` holds Beets' `config.yaml` and `musiclibrary.blb` (an existing Beets `/config` folder works as is). `/music` is read-write: Beets moves, tags and imports files from inside this container. `MUSIC_ROOT` must equal `directory:` in `config.yaml`.

---

## 2. Adding Web Manager to an Existing Stack

Add the service above to the Compose file that already holds your other services (Lidarr, slskd, Plex, ...), and mount the same host folders they use. If that stack ran a Beets container on the same library, remove it first ([Upgrading from two containers](INSTALLATION.md#upgrading-from-two-containers)): two programs must never write one library, and while one is detected Web Manager refuses library changes (`separate_beets_running`).

Beets on another server: Web Manager no longer connects to Beets over the network. Mount that server's Beets `/config` and music folders into this container instead, and stop the Beets there.

---

## 3. Deployment Commands

### Production Deployment (Pull published image)
```bash
docker compose pull
docker compose up -d beets-web-manager
curl -s http://127.0.0.1:8337/api/health
```

### Upgrading
```bash
docker compose pull beets-web-manager
docker compose up -d beets-web-manager
docker compose ps beets-web-manager
docker compose logs --tail=100 beets-web-manager
```

### Development Build (From local source)
```bash
docker compose -f docker-compose.dev.yml up -d --build
```

---

## 4. Image Tag

The shipped Compose files use `image: ghcr.io/iranman/beets-web-manager:latest`, the newest release. Update with:

```bash
docker compose pull beets-web-manager && docker compose up -d beets-web-manager
```

To pin a release, or roll back to one, replace `latest` in the `image:` line with an exact version such as `0.9.0` (`ghcr.io/iranman/beets-web-manager:0.9.0`).

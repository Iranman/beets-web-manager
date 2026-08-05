# Beets Web Manager - Deployment Examples

This document provides deployment examples for embedding **Beets Web Manager** into existing Compose stacks (such as TrueNAS, Unraid, Portainer, or existing Arrs stacks).

---

## 1. Existing Compose Stack Example (e.g., TrueNAS / Arrs Stack)

When adding `beets-web-manager` to an existing Compose file (such as `/mnt/PLEX/Apps/Arrs/docker-compose.yml`), copy only the service definition below.

> [!IMPORTANT]
> - Use the published GHCR image.
> - Do **NOT** copy `build: .` or use local-only image names.
> - Do **NOT** mount source code directories.
> - Ensure `BEETS_API_URL` points to your Beets control agent endpoint.

```yaml
services:
  beets-web-manager:
    image: ghcr.io/iranman/beets-web-manager:${BEETS_WEB_MANAGER_VERSION:-stable}
    container_name: beets-web-manager
    restart: unless-stopped

    ports:
      - "${BEETS_WEB_BIND_ADDRESS:-127.0.0.1}:8337:8337"

    environment:
      TZ: America/Los_Angeles
      BEETS_API_URL: http://beets:8338
      BEETS_API_TOKEN: "${BEETS_API_TOKEN:?set in .env}"
      BEETS_WEB_AUTH_TOKEN: "${BEETS_WEB_AUTH_TOKEN:-}"
      BEETS_WEB_AUTH_TOKEN_FILE: /web-manager-data/.auth_token

    volumes:
      - /mnt/PLEX/Apps/Arrs/beets-web-manager:/web-manager-data

    security_opt:
      - no-new-privileges:true

    cap_drop:
      - ALL

    read_only: true

    tmpfs:
      - /tmp:rw,noexec,nosuid,nodev,size=512m
      - /run:rw,nosuid,nodev,size=64m

    healthcheck:
      test:
        - CMD
        - python
        - -c
        - "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8337/api/health', timeout=5)"
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 20s
```

---

## 2. Beets Connection Modes

Beets Web Manager communicates with the Beets engine solely through HTTP API calls using `BEETS_API_URL` and `BEETS_API_TOKEN`.

### Mode A: Beets in the same Compose project
If the Beets control agent runs as a service named `beets` inside the same `docker-compose.yml`:

```yaml
BEETS_API_URL: http://beets:8338
```

You may optionally include `depends_on` inside the same project:
```yaml
depends_on:
  beets:
    condition: service_healthy
```

### Mode B: Beets on a separate host or separate Compose stack
If the Beets control agent runs on another host or in a separate Compose project:

```yaml
BEETS_API_URL: http://192.168.1.50:8338
```

> [!NOTE]
> Remove `depends_on: beets` when Beets runs outside the current Compose file. Docker Compose cannot validate dependencies across separate stack files or separate hosts.

---

## 3. Deployment Commands

### Production Deployment (Pull published image)
```bash
docker compose pull
docker compose up -d beets-web-manager
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

## 4. Image Release Channels

Configure `BEETS_WEB_MANAGER_VERSION` in `.env` to select the desired release channel:

```env
# Recommended production channel
BEETS_WEB_MANAGER_VERSION=stable

# Conventional newest stable release
BEETS_WEB_MANAGER_VERSION=latest

# Exact version for predictable deployment and rollback
BEETS_WEB_MANAGER_VERSION=0.1.0

# Development builds from main; not recommended for production
BEETS_WEB_MANAGER_VERSION=edge
```

`stable` is the recommended default channel for production deployments. Specifying an exact version (e.g., `0.1.0`) is the safest choice for environments requiring fully pinned, predictable upgrades and rollbacks.

> [!NOTE]
> Prerelease tags (e.g., `v0.2.0-rc.1`) publish exact prerelease image tags for testing, but never update the production `stable` or `latest` channels.

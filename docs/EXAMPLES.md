# Beets Web Manager - Deployment Examples

This document provides deployment examples for **Beets Web Manager**.

---

## 1. Standard Unified Stack (Recommended)

Run both Beets and Beets Web Manager together in the same Compose file sharing your library volumes:

```yaml
services:
  beets:
    image: lscr.io/linuxserver/beets:2.13.1
    container_name: beets
    restart: unless-stopped
    environment:
      - PUID=1000
      - PGID=1000
      - TZ=Etc/UTC
    volumes:
      - ./beets:/config
      - /path/to/music:/music
      - /path/to/downloads:/downloads

  beets-web-manager:
    image: ghcr.io/iranman/beets-web-manager:stable
    container_name: beets-web-manager
    restart: unless-stopped
    ports:
      - "8337:8337"
    environment:
      - PUID=1000
      - PGID=1000
      - TZ=Etc/UTC
    volumes:
      - ./beets:/config
      - /path/to/music:/music
      - /path/to/downloads:/downloads
      - ./web-manager:/data
    depends_on:
      - beets
```

---

## 2. External / Standalone Beets Example (e.g. TrueNAS)

When connecting Beets Web Manager to an existing remote Beets instance (see [examples/docker-compose.external-beets.yml](../examples/docker-compose.external-beets.yml)):

```yaml
services:
  beets-web-manager:
    image: ghcr.io/iranman/beets-web-manager:stable
    container_name: beets-web-manager
    restart: unless-stopped
    ports:
      - "8337:8337"
    environment:
      - PUID=1000
      - PGID=1000
      - TZ=Etc/UTC
      - BEETS_API_URL=http://192.168.1.50:8338
      - BEETS_API_TOKEN=your-strong-token-here
    volumes:
      - ./web-manager:/data
```

### Connection Modes (Advanced / External deployments only)

These only apply when running Beets Web Manager against a separately-managed Beets control agent, as above — the standard stack in section 1 needs no `BEETS_API_URL`/`BEETS_API_TOKEN` configuration at all; it uses its own embedded control agent automatically.

#### Mode A: A remote control agent that happens to share a Compose project
If a separately-managed Beets control agent runs as a service named `beets` inside the same `docker-compose.yml` (not the standard stock `lscr.io/linuxserver/beets` image, which has no control agent of its own):

```yaml
BEETS_API_URL: http://beets:8338
```

You may optionally include `depends_on` inside the same project:
```yaml
depends_on:
  beets:
    condition: service_healthy
```

#### Mode B: Beets on a separate host or separate Compose stack
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
BEETS_WEB_MANAGER_VERSION=0.1.18

# Development builds from main; not recommended for production
BEETS_WEB_MANAGER_VERSION=edge
```

`stable` is the recommended default channel for production deployments. Specifying an exact version (e.g., `0.1.18`) is the safest choice for environments requiring fully pinned, predictable upgrades and rollbacks.

> [!NOTE]
> Prerelease tags (e.g., `v0.2.0-rc.1`) publish exact prerelease image tags for testing, but never update the production `stable` or `latest` channels.

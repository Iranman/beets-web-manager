# Beets Web Manager - Deployment Examples

This document provides deployment examples for **Beets Web Manager**.

---

## 1. Standard Unified Stack (Recommended)

Run both Beets and Beets Web Manager together in the same Compose file sharing your library volumes:

```yaml
services:
  beets:
    image: lscr.io/linuxserver/beets:latest
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
    expose:
      - "8337"
    depends_on:
      beets-web-manager:
        condition: service_healthy

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
      - BEETS_WEB_URL=http://beets:8337
      - BEETS_OUTBOUND_ALLOWLIST=beets:8337
    volumes:
      - ./beets:/config
      - /path/to/music:/music:ro
      - /path/to/downloads:/downloads
      - ./web-manager:/web-manager-data
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8337/api/health', timeout=5)"]
      interval: 10s
      timeout: 5s
      retries: 5
      start_period: 20s
```

---

## 2. External / Standalone Beets Example (e.g. TrueNAS)

When connecting Beets Web Manager to an existing, separately-managed stock Beets instance instead of starting one from this Compose file (see [examples/docker-compose.external-beets.yml](../examples/docker-compose.external-beets.yml)):

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
      - BEETS_WEB_URL=http://192.168.1.50:8337
      - BEETS_OUTBOUND_ALLOWLIST=192.168.1.50:8337
    volumes:
      - ./web-manager:/web-manager-data
```

### Connection Modes (Advanced / External deployments only)

These only apply when pointing Beets Web Manager at a separately-managed stock Beets instance, as above — the standard stack in section 1 needs no extra connection configuration at all, since it starts its own `beets` service and defaults `BEETS_WEB_URL` to it.

The remote Beets instance must already have its `web` and `webmanager` plugins enabled (see `config.yaml.example`) and the `webmanager` plugin's files provisioned into its own `/config/beetsplug` — Web Manager cannot provision a plugin into a filesystem it does not mount.

#### Mode A: A remote stock Beets that happens to share a Compose project
If a separately-managed stock Beets instance runs as a service named `beets` inside the same `docker-compose.yml`:

```yaml
BEETS_WEB_URL: http://beets:8337
```

You may optionally include `depends_on` inside the same project:
```yaml
depends_on:
  beets:
    condition: service_healthy
```

#### Mode B: Beets on a separate host or separate Compose stack
If the remote Beets instance runs on another host or in a separate Compose project:

```yaml
BEETS_WEB_URL: http://192.168.1.50:8337
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

## 4. Image Tag

The shipped Compose files use `image: ghcr.io/iranman/beets-web-manager:latest`, the newest release. Update with:

```bash
docker compose pull beets-web-manager && docker compose up -d beets-web-manager
```

To pin a release, or roll back to one, replace `latest` in the `image:` line with an exact version such as `0.1.49` (`ghcr.io/iranman/beets-web-manager:0.1.49`).

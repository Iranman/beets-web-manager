# Installation & Deployment Guide

> [!IMPORTANT]
> **Beets Web Manager requires Beets.** Both services run together in the same Docker Compose stack sharing your library volumes. You do **not** need to install Python, Beets, `pip`, or `uv` on your host machine.

---

## Production Installation (Recommended)

Production deployment uses published images and requires only Docker and Docker Compose.

### Step 1: Create a directory for your stack

```bash
mkdir beets-stack && cd beets-stack
```

### Step 2: Create `docker-compose.yml`

Create a file named `docker-compose.yml` with the following content:

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
    image: ghcr.io/iranman/beets-web-manager:stable
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

> [!TIP]
> - Update `/path/to/music` to point to your music library on the host.
> - Update `/path/to/downloads` to point to your downloads or staging directory.
> - `./beets` and `./web-manager` will be created automatically in your current directory for persistent configuration and application state.

### Step 3: Start the stack

```bash
docker compose up -d
```

### Step 4: Open Beets Web Manager

Open **`http://<server-ip>:8337`** in your browser. On your first visit, you will see the **First-Run Setup Wizard** where you can create your administrator username and password.

---

## Directory & Volume Architecture

Both containers share the same underlying files:

| Host Path | Container Path | Purpose |
|---|---|---|
| `./beets` | `/config` | Beets configuration (`config.yaml`), plugins, and SQLite library database (`musiclibrary.blb`), owned by the `beets` container. Web Manager also mounts this read/write, only to provision its integration plugin and merge `config.yaml` entries — it never opens `musiclibrary.blb` directly. |
| `/path/to/music` | `/music` | Authoritative music library files. Mounted read-only into Web Manager; only the `beets` container writes here. |
| `/path/to/downloads` | `/downloads` | Ingest and download staging directory |
| `./web-manager` | `/web-manager-data` | Web manager settings, user accounts, sessions, and transaction audit logs (Web Manager's own state; not shared with `beets`) |

---

## Running Beets CLI Commands

You can execute any standard `beet` command anytime inside the `beets` container:

```bash
# Check Beets version and loaded plugins
docker compose exec beets beet version

# List music in your library
docker compose exec beets beet ls

# Import music via CLI
docker compose exec beets beet import /downloads/new-album
```

Read-only commands (`beet ls`, `beet version`) are always safe, any time. Prefer doing mutating operations (`beet import`, `beet modify`, `beet rm`) through Beets Web Manager's own UI/API, which serializes them through its own controlled preview/apply/audit workflow; if you do run a manual mutating `beet` command in the container, avoid doing so while a Web Manager job is actively running — SQLite's own file-level locking on `/config/musiclibrary.blb` prevents literal database corruption from simultaneous writes, but it does not coordinate with Web Manager's own multi-step operations.

---

## Advanced: External / Standalone Beets Deployment

If you run Beets on a separate host (e.g. TrueNAS, Unraid, or another server) and want Beets Web Manager to connect over the network instead of starting its own `beets` service:

1. Use [examples/docker-compose.external-beets.yml](../examples/docker-compose.external-beets.yml).
2. Configure `BEETS_WEB_URL` (e.g. `http://192.168.1.50:8337`) and `BEETS_OUTBOUND_ALLOWLIST`.
3. The remote Beets instance must already have its `web`/`webmanager` plugins enabled and provisioned — Web Manager cannot provision a plugin into a filesystem it does not mount.

---

## Development Installation (Source Builds)

If you are developing Beets Web Manager and modifying source code:

```bash
docker compose -f docker-compose.dev.yml up -d --build
```

---

## Upgrades

To upgrade to the latest stable release:

```bash
docker compose pull
docker compose up -d
```

---

## Health Checks

Verify that both services are healthy:

```bash
# Web manager health
curl -s http://127.0.0.1:8337/api/health
curl -s http://127.0.0.1:8337/health/live
```
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
| `./beets` | `/config` | Beets configuration (`config.yaml`), plugins, and SQLite library database (`musiclibrary.blb`) |
| `/path/to/music` | `/music` | Authoritative music library files |
| `/path/to/downloads` | `/downloads` | Ingest and download staging directory |
| `./web-manager` | `/data` | Web manager settings, user accounts, sessions, and transaction audit logs |

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

SQLite's own file-level locking on `/config/musiclibrary.blb` prevents literal database corruption from simultaneous writes, but it does not coordinate multi-step operations: Beets Web Manager's own jobs (imports, cleanup, tag writes) additionally serialize on a higher-level lock file (`/config/.beet_db.lock`) that only Web Manager's code acquires — a manual `beet` command run here does not take that lock. Read-only commands (`beet ls`, `beet version`) are always safe; avoid running a manual mutating command (`beet import`, `beet modify`, `beet rm`) at the same time as an active Web Manager job.

---

## Advanced: External / Standalone Beets Deployment

If you run Beets on a separate host (e.g. TrueNAS, Unraid, or another server) and want Beets Web Manager to connect over the network:

1. Use [examples/docker-compose.external-beets.yml](../examples/docker-compose.external-beets.yml).
2. Configure `BEETS_API_URL` (e.g. `http://192.168.1.50:8338`) and `BEETS_API_TOKEN`.

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
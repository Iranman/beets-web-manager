# Installation & Deployment Guide

Beets Web Manager is packaged as a matched two-container release pair published to GitHub Container Registry:
- **`beets`** (`ghcr.io/iranman/beets-engine:${BEETS_WEB_MANAGER_VERSION:-stable}`): Authoritative Beets engine, plugins, SQLite library, and control agent.
- **`beets-web-manager`** (`ghcr.io/iranman/beets-web-manager:${BEETS_WEB_MANAGER_VERSION:-stable}`): Web UI, API backend, import queue, and job engine.

Leaving `BEETS_WEB_MANAGER_VERSION=stable` in `.env` pulls the matched stable release images for both containers automatically. Pin an exact release such as `0.1.17` when you need predictable rollback to a known image pair.

---

## Production Installation (Recommended)

Production deployment does **not** require Node.js, Python, or build tools on the host system.

### 1. Automated Setup (Recommended)

Run the setup script for automated zero-friction deployment:

```bash
git clone https://github.com/Iranman/beets-web-manager.git
cd beets-web-manager
./setup.sh          # Linux / macOS
.\setup.ps1         # Windows PowerShell
```

The setup script automatically:
1. Creates persistent directories (`config`, `data/music`, `data/downloads`, `web-manager-data`).
2. Copies `config.yaml.example` to `config/config.yaml` if no configuration exists.
3. Generates strong, random cryptographic tokens for `BEETS_API_TOKEN` and `BEETS_WEB_AUTH_TOKEN`.
4. Pulls official published GHCR images (`ghcr.io/iranman/beets-engine:stable` and `ghcr.io/iranman/beets-web-manager:stable`).
5. Starts the two-container stack and verifies health and internal IPC connectivity.
6. Prompts for local vs. LAN network access and displays the ready URL.

### 2. Manual Docker Compose Deployment

If you prefer deploying manually:

```bash
git clone https://github.com/Iranman/beets-web-manager.git
cd beets-web-manager
mkdir -p config data/music data/downloads web-manager-data
cp config.yaml.example config/config.yaml
cp .env.example .env
```

Edit `.env` to set your generated `BEETS_API_TOKEN` and network bind address, then start:

```bash
docker compose pull
docker compose up -d
docker compose ps
```

### 3. First-Run Browser Setup
Open `http://<server-ip>:8337` in your browser. On a fresh installation:
1. You will see the **Finish Beets Web Manager Setup** wizard.
2. Enter your preferred administrator username (default: `admin`) and secure password.
3. Click **Create Login & Complete Setup**.
4. Sign in with your created login when prompted.

---

## Existing Stack Installation (TrueNAS / Portainer / Multi-App Stacks)

To embed `beets-web-manager` into an existing Compose stack (such as `/srv/media-stack/docker-compose.yml`):

1. Copy the `beets-web-manager` service block from [docs/EXAMPLES.md](EXAMPLES.md).
2. Reference the published image `ghcr.io/iranman/beets-web-manager:${BEETS_WEB_MANAGER_VERSION:-stable}`.
3. Do **not** include `build: .` or local image names.
4. Mount host data path for persistent state:
   ```yaml
   volumes:
     - ${BEETS_WEB_MANAGER_DATA_PATH:-./web-manager-data}:/web-manager-data
   ```
   The published image runs as a fixed non-root user (UID/GID 1000, baked in at build time -- unlike the Beets engine image, this is not something a runtime `PUID`/`PGID` environment variable can change), and a host bind mount's on-disk permissions come entirely from the host directory itself. Before first start, make sure that directory is writable by UID 1000, e.g. `mkdir -p web-manager-data && chmod 777 web-manager-data` (or `chown 1000:1000 web-manager-data` if you'd rather not use world-writable permissions) -- otherwise the container cannot create its own state there (its bootstrapped auth-token file, its transaction audit ledger) and every mutating request will fail with a generic server error.
5. Configure `BEETS_API_URL` and `BEETS_API_TOKEN`.
6. Choose the connection mode that matches your setup -- see [Beets Connection Modes](EXAMPLES.md#2-beets-connection-modes) for whether `depends_on` applies.

---

## Development Installation (Source Builds)

If you are modifying source code and building both services locally:

```bash
docker compose -f docker-compose.dev.yml up -d --build
```

---

## Optional Bundled Beets Stack (Advanced Users)

If you want to run the published Beets Web Manager alongside a locally built custom Beets engine (`Dockerfile.beets`), use the bundled Compose file:

```bash
docker compose -f docker-compose.full.yml up -d --build
```

> [!WARNING]
> `docker-compose.full.yml` builds `beets-engine` locally from `Dockerfile.beets`. Running it outside the repository root without a local build context will fail with `pull access denied for beets-engine`. Do not copy the `beets` service definition into an external directory unless `Dockerfile.beets` and source files are also present.

Both `docker-compose.dev.yml` and `docker-compose.full.yml` accept a `BEETS_BASE_IMAGE` variable (in `.env` or the shell environment) to select the upstream LinuxServer Beets version the engine is built from -- default is the tested production candidate. See `docs/CONFIGURATION.md` ("Beets engine version") before setting it to `lscr.io/linuxserver/beets:latest`, and see `docs/BEETS_ENGINE_MIGRATION.md` before changing it on a deployment with an existing library.

`docker-compose.full.yml` also accepts `BEETS_EXPECT_EXISTING_LIBRARY` (default `1`): the locally built engine refuses to start if it expects an existing library but `./config` is empty, so it doesn't silently treat a missing database as an empty one. On a genuine first run against a brand new `./config` with no prior library, set `BEETS_EXPECT_EXISTING_LIBRARY=0` in `.env` for that first run; leave it at `1` (or unset) for every run afterward, so the engine keeps refusing to start against an unexpectedly empty database later.

---

## Image Release Channels

Configure `BEETS_WEB_MANAGER_VERSION` in `.env` to select the image channel:

```env
# Recommended production channel
BEETS_WEB_MANAGER_VERSION=stable

# Conventional newest stable release
BEETS_WEB_MANAGER_VERSION=latest

# Exact version for predictable deployment and rollback
BEETS_WEB_MANAGER_VERSION=0.1.17

# Development builds from main; not recommended for production
BEETS_WEB_MANAGER_VERSION=edge
```

`stable` is the recommended default for production deployments. Using an exact version tag (e.g. `0.1.17`) is recommended for predictable deployments and rollbacks. Prerelease tags (such as `v0.2.0-rc.1`) publish exact prerelease image tags for testing, but never touch `stable` or `latest`.

---

## Upgrade Process

To upgrade to the latest published release on the active channel (`stable` by default):

```bash
docker compose pull beets-web-manager
docker compose up -d beets-web-manager
docker compose ps beets-web-manager
docker compose logs --tail=100 beets-web-manager
```

---

## Rollback Process

To roll back to a specific previous release:

1. Pin the exact release version in `.env`:
   ```env
   BEETS_WEB_MANAGER_VERSION=0.1.17
   ```
2. Recreate the service:
   ```bash
   docker compose pull beets-web-manager
   docker compose up -d beets-web-manager
   ```

---

## Health Checks

Verify container health:

```bash
curl -s http://127.0.0.1:8337/api/health
curl -s http://127.0.0.1:8337/health/live
```
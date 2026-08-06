# Installation & Deployment Guide

Beets Web Manager is packaged as a lightweight, pre-built runtime image published to GitHub Container Registry (`ghcr.io/iranman/beets-web-manager`).

It communicates with an existing Beets engine control agent via `BEETS_API_URL` and `BEETS_API_TOKEN`.

---

## Production Installation (Recommended)

Production deployment does **not** require Node.js, Python, or build tools on the host system.

### 1. Obtain deployment files
```bash
git clone https://github.com/Iranman/beets-web-manager.git
cd beets-web-manager
cp .env.example .env
```
*(Git clone is used only to obtain `docker-compose.yml`, `.env.example`, and setup scripts. No source code compilation is performed.)*

### 2. Configure environment
Edit `.env` to configure your settings. Ensure `BEETS_API_URL` and `BEETS_API_TOKEN` match your Beets control agent instance:

```env
BEETS_API_URL=http://beets:8338
BEETS_API_TOKEN=your-secure-beets-token
```

Alternatively, run the automated setup script:
```bash
./setup.sh          # Linux / macOS
.\setup.ps1         # Windows PowerShell
```

### 3. Deploy
```bash
docker compose pull
docker compose up -d
docker compose ps
```

---

## Existing Stack Installation (TrueNAS / Portainer / Multi-App Stacks)

To embed `beets-web-manager` into an existing Compose stack (such as `/mnt/PLEX/Apps/Arrs/docker-compose.yml`):

1. Copy the `beets-web-manager` service block from [docs/EXAMPLES.md](EXAMPLES.md).
2. Reference the published image `ghcr.io/iranman/beets-web-manager:${BEETS_WEB_MANAGER_VERSION:-stable}`.
3. Do **not** include `build: .` or local image names.
4. Mount host data path for persistent state:
   ```yaml
   volumes:
     - /mnt/PLEX/Apps/Arrs/beets-web-manager:/web-manager-data
   ```
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

---

## Image Release Channels

Configure `BEETS_WEB_MANAGER_VERSION` in `.env` to select the image channel:

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

`stable` is the recommended default for production deployments. Using an exact version tag (e.g. `0.1.0`) is recommended for predictable deployments and rollbacks. Prerelease tags (such as `v0.2.0-rc.1`) publish exact prerelease image tags for testing, but never touch `stable` or `latest`.

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
   BEETS_WEB_MANAGER_VERSION=0.1.0
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
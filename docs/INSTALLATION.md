# Installation

This project runs as a two-service Beets deployment.

- `beets`: authoritative Beets engine. Owns `/config/config.yaml`, `/config/musiclibrary.blb`, Beets CLI execution, tag writes, and media-library mutations. The control agent listens on internal port `8338` only.
- `beets-web-manager`: Flask and React web manager. Serves the UI on `127.0.0.1:8337` by default and talks to the engine through `BEETS_API_URL` plus `BEETS_API_TOKEN`. It must not contain Beets, open the Beets SQLite file directly, or mutate media files as the authority.

## First start

```bash
git clone https://github.com/Iranman/beets-web-manager.git
cd beets-web-manager
cp .env.example .env
```

Edit `.env` before starting. `BEETS_API_TOKEN` must be a strong shared secret; blank, short, weak, or placeholder values fail closed in the Beets engine.

Standalone stack:

```bash
docker compose config
docker compose up -d beets beets-web-manager
```

Arr stack:

```bash
docker compose -f docker-compose.arrs.yml config
docker compose -f docker-compose.arrs.yml up -d bgutil-provider beets beets-web-manager
```

## Ports

- Web manager: `127.0.0.1:${WEBCONTROL_PORT:-8337}:8337`
- Beets control agent: `8338` exposed only to the Compose network

Do not publish port `8338` to the host. Put the web port behind a reverse proxy only after configuring authentication, TLS, trusted proxy settings, and outbound allowlist entries for private services.

## Volumes

Generic `docker-compose.yml` uses local disposable-friendly paths by default:

- `./config:/config`
- `${MUSIC_LIBRARY_PATH:-./data/music}:/data/media/music`
- `${DOWNLOAD_PATH:-./data/downloads}:/data/torrents`
- `./web-manager-data:/web-manager-data`

`docker-compose.arrs.yml` uses the operator media-stack paths in that file. Change those paths only after backing up config and validating the Compose output.

## Health and readiness

```bash
curl -s http://127.0.0.1:8337/api/health
curl -s http://127.0.0.1:8337/health/ready
curl -s http://127.0.0.1:8337/api/setup/status
```

`/api/setup/status` queries the internal Beets control agent for the remote Beets version, loaded plugins, plugin failures, command readiness, tool availability, and engine path health. It does not contact external providers.

## Backups and migration

Before migrating an existing library, back up at least:

- `/config/config.yaml`
- `/config/musiclibrary.blb`
- `/config/beetsplug/` if present
- web-manager state under `/web-manager-data` or the configured state directory

Passing local tests or building images does not authorize production deployment. Validate with disposable volumes first, then perform a separate migration plan and rollback test before touching production data.
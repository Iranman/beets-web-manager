# Beets Web Manager

Beets Web Manager is a self-hosted web application for managing a Beets music library, import review, playlist repair, acquisition queues, cleanup jobs, metadata verification, and media-server synchronization from one operator-focused interface.

The app is designed for local or self-hosted deployments where the music library, download staging folders, Beets database, Plex, downloader services, MusicBrainz, AcoustID, and optional AI providers are controlled by the administrator.

## Features

- Flask backend that reads and updates a Beets library directly.
- React and Next.js static frontend served by the backend.
- Import review queue with evidence-driven accept, reject, and cleanup actions.
- Playlist ingestion from files, URLs, pasted tracks, and saved playlist manifests.
- Missing-track acquisition through SLSKD/Soulseek and configured direct-source helpers.
- MusicBrainz release, release-group, recording, and tracklist matching.
- AcoustID fingerprint checks for track verification and destructive cleanup safeguards.
- AI-assisted metadata suggestions with deterministic validation before mutation.
- Background job system with logs, retry state, cancellation, and cleanup workflows.
- Library cleanup for duplicates, artist folders, album folders, placeholders, and missing metadata.
- Plex synchronization for playlists and library refresh workflows.
- Security hardening around authentication, CSRF, secret redaction, outbound URL validation, and Docker deployment defaults.

## Screenshots

Screenshots will be added as the public documentation is prepared.

## Installation

Clone the repository and copy `.env.example` to `.env`. Replace every placeholder value with credentials for your own services before starting the stack.

```bash
git clone https://github.com/Iranman/beets-web-manager.git
cd beets-web-manager
cp .env.example .env
```

## Docker Installation

The provided Compose file is intended for a broader Arr/media stack. The Beets app service is hardened to run non-root, bind to loopback by default, and mount only the Beets config, music library, and music staging roots.

```bash
docker compose -f docker-compose.arrs.yml config
docker compose -f docker-compose.arrs.yml up -d bgutil-provider beets
```

By default the web app binds to `127.0.0.1:8337`. Put it behind a reverse proxy only after configuring authentication, allowed outbound internal services, TLS, and trusted proxy settings.

## Development Installation

Backend checks use the system Python available in the deployment environment:

```bash
python -m py_compile app.py helpers_mb.py job_engine.py routes_jobs.py
python -m unittest discover -s tests -p "test_*.py"
```

Frontend development uses Node and npm from `frontend/`:

```bash
cd frontend
npm ci
npm run typecheck
npm run lint
npm run build
```

## Configuration

Most runtime configuration comes from environment variables and `/config/config.yaml` inside the container. Secrets must be provided through `.env`, Docker secrets, or mounted secret files. Do not commit real credentials.

See `.env.example` for the required and optional variables.

## Environment Variables

Important variables include:

- `BEETS_WEB_AUTH_TOKEN`: strong owner token for the web API.
- `OPENAI_API_KEY` or compatible provider key: optional AI metadata features.
- `PLEX_URL` and `PLEX_TOKEN`: Plex sync and refresh integration.
- `LIDARR_URL` and `LIDARR_API_KEY`: wanted-music and Arr integration.
- `SLSKD_SLSK_USERNAME` and `SLSKD_SLSK_PASSWORD`: Soulseek client credentials.
- `BEETS_OUTBOUND_ALLOWLIST`: exact host:port or CIDR:port entries for local services the backend may call.
- `BEETS_TRUSTED_PROXIES`: direct proxy CIDRs whose forwarded client IP headers may be trusted.

## How Imports Work

Downloaded or staged files enter an import-review flow. The backend compares filenames, tags, MusicBrainz release evidence, track counts, durations, and fingerprints where available before importing into Beets. Failed or ambiguous imports remain visible for review instead of being silently deleted.

## How AI Matching Works

AI can suggest metadata or cleanup candidates, but model output is treated as untrusted. Application code validates structured output, checks identifiers and paths, and requires deterministic evidence before destructive actions.

## How AcoustID Is Used

AcoustID fingerprints are used as evidence for track identity, duplicate review, replacement safety, and MusicBrainz recording checks. Fingerprinting is part of the safety model for track verification and destructive cleanup workflows.

## How MusicBrainz Is Used

MusicBrainz release, release-group, recording, medium, and tracklist data are used to validate album editions, missing tracks, replacement candidates, folder placement, and metadata corrections.

## Playlist Support

Saved playlist manifests track desired entries, removed/excluded tombstones, staged files, import status, Plex sync status, and retry state. Playlist deletion removes playlist artifacts, not Beets library audio.

## Library Cleanup

Cleanup jobs cover duplicates, folder placeholders, artist-folder MBID stamping, album-track repair, missing metadata, and controlled replacement workflows. Destructive operations must remain constrained to approved library or staging roots.

## Jobs System

Long-running operations are represented as jobs with status, logs, cancellation, and bounded retry behavior. The Jobs page is the operator surface for acquisition, import, cleanup, playlist, and maintenance work.

## Technology Stack

- Python and Flask backend
- Beets library APIs and CLI
- React, Next.js static export, TypeScript
- Tailwind CSS, MUI, Headless UI, TanStack Query
- Docker Compose deployment
- MusicBrainz, AcoustID, Plex, SLSKD, yt-dlp-compatible helper tooling

## Roadmap

- Expand destructive-workflow race-condition tests.
- Improve SBOM and container scan publishing in CI.
- Add public screenshots and deployment diagrams.
- Continue narrowing service credentials and mounts for multi-service stacks.
- Improve release automation and signed provenance.

## First-Run Setup

For the simplest possible start:

```bash
./setup.sh          # or .\setup.ps1 on Windows
```

This creates `.env`/`config.yaml` from the example templates, generates a random `BEETS_WEB_AUTH_TOKEN`, builds the image, and starts the stack via `docker-compose.yml` (a minimal single-container Compose file — see `docker-compose.arrs.yml` for the broader Arr-stack variant used above). Readiness and per-integration connectivity checks are available at `GET /api/setup/status` and `POST /api/setup/test/{ai,musicbrainz,acoustid,plex}`, and standard health probes at `/health`, `/health/live`, `/health/ready`.

## Documentation

- [Installation Guide](docs/INSTALLATION.md) — per-platform path-mapping examples (Linux, TrueNAS, Unraid, Synology, Windows) and local dev setup.
- [Configuration Reference](docs/CONFIGURATION.md) — every environment variable and `config.yaml` setting.
- [Troubleshooting](docs/TROUBLESHOOTING.md) — common failure modes and fixes.

## Backups

```bash
./scripts/backup.sh              # writes ./backups/beets-backup-<timestamp>.tar.gz
./scripts/restore.sh <file.tar.gz>
```

Backs up the beets database and configuration/state JSON files under `/config` — **not** your music library, which you should back up separately with your own storage/snapshot tooling.

## Updates

```bash
docker compose pull   # or: docker compose build
docker compose up -d
```

Run `./scripts/backup.sh` first. There is no separate database-migration step — the beets library schema is managed by beets itself on next library open.

## Contributing

See `CONTRIBUTING.md`. Commit messages should use concise conventional prefixes such as `feat:`, `fix:`, `docs:`, `test:`, `build:`, `ci:`, and `chore:`.

## License

MIT. See `LICENSE`.

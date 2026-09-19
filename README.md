# Beets Web Manager

Beets Web Manager is a self-hosted web application for managing a Beets music library, import review, playlist repair, acquisition queues, cleanup jobs, metadata verification, and media-server synchronization from one operator-focused interface.

The app is designed for local or self-hosted deployments where the music library, download staging folders, Beets database, Plex, downloader services, MusicBrainz, AcoustID, and optional AI providers are controlled by the administrator.

This project exists because the Beets web plugin didn't cover enough on its own: a full UI, import review, playlist repair, acquisition queues, cleanup jobs, and metadata verification on top of Beets. Issues and improvement suggestions are welcome.

## Features

- Flask web manager that orchestrates Beets through an authenticated internal control agent.
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

See [SCREENSHOTS.md](SCREENSHOTS.md) for a tour of the app (Library, Import, Playlists, Jobs, Submissions, Settings, Library Changes).

## Installation & Deployment

> [!IMPORTANT]
> **Beets Web Manager requires Beets.** Both services run together in the same Docker Compose stack sharing your library volumes. You do **not** need to install Python, Beets, `pip`, or `uv` on your host machine.

### Quick Start (Production)

Deploying Beets Web Manager takes just 4 steps:

#### 1. Create a project directory
```bash
mkdir beets-stack && cd beets-stack
```

#### 2. Create `docker-compose.yml`
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

*(Adjust `/path/to/music` and `/path/to/downloads` to match your media storage folders).*

#### 3. Start the stack
```bash
docker compose up -d
```

#### 4. Open Beets Web Manager
Open **`http://<server-ip>:8337`** in your browser. On your first visit, you will be guided through a simple setup wizard to create your administrator username and password.

---

### Running Beets CLI Commands
You can run any standard Beets command anytime directly inside the `beets` container:

```bash
docker compose exec beets beet version
docker compose exec beets beet ls
docker compose exec beets beet import /downloads/new-album
```

Both Beets Web Manager and the Beets CLI share the exact same library and configuration files atomically.

---

### Advanced: Standalone / External Beets (e.g. TrueNAS)
If you already run a standalone Beets container on a separate server or stack, see [examples/docker-compose.external-beets.yml](examples/docker-compose.external-beets.yml) to connect Beets Web Manager over the network.

### Development Installation (Source Builds)
To build both services from local source code:
```bash
docker compose -f docker-compose.dev.yml up -d --build
```

For backend tests:

```bash
python -m unittest discover -s tests -p "test_*.py"
```

For frontend development:

```bash
cd frontend
npm ci
npm run typecheck
npm run lint
npm run build
```

## Configuration

Most runtime configuration comes from environment variables and `/config/config.yaml` inside the container. Secrets can be provided through `.env`, Docker Compose environment variables, or configured directly in the web UI.

The standard Compose stack shares `/config`, `/music`, `/downloads`, and `/data` volumes between Beets and Beets Web Manager.

## Environment Variables

Key variables include:

- `PUID` / `PGID`: user and group IDs for file permissions (defaults to `1000`).
- `TZ`: time zone (defaults to `Etc/UTC`).
- `WEBCONTROL_PORT`: web UI port (defaults to `8337`).
- `BEETS_WEB_PASSWORD` / `BEETS_WEB_USERNAME`: administrator credentials for browser access (created via the browser setup wizard on first visit).
- `OPENAI_API_KEY` or compatible provider key: **optional** AI metadata features — see [How AI Matching Works](#how-ai-matching-works).
- `PLEX_URL` and `PLEX_TOKEN`: Plex sync and refresh integration (optional).
- `LIDARR_URL` and `LIDARR_API_KEY`: wanted-music and Arr integration (optional).
- `ACOUSTID_API_KEY` / `ACOUSTID_KEY`: optional — AcoustID lookups work without a key via a shared, rate-limited test key.
- `SLSKD_SLSK_USERNAME` and `SLSKD_SLSK_PASSWORD`: Soulseek client credentials (optional, required only for SLSKD-based acquisition).

### Required vs. optional integrations

| Integration | Requirement | Notes |
| ----------- | ----------- | ----- |
| Beets | Required | Core music library engine; runs in the same Compose stack |
| MusicBrainz | Built-in | Public metadata API used for release and recording matching |
| AcoustID | Optional | Audio fingerprint matching and safety verification |
| Plex | Optional | Media server sync and playlist synchronization |
| SLSKD | Optional | Missing-track acquisition via Soulseek |
| AI (OpenAI / OpenRouter) | Optional | Enhancement for candidate metadata ranking |

## Authentication

The app provides separate authentication for human browser operators and API/script clients.

### 1. Web Browser Login
On your first visit to `http://<server-ip>:8337`, the setup wizard will guide you to choose your administrator username and password.

### 2. API / Scripts (`BEETS_WEB_AUTH_TOKEN`)
For automated scripts or API clients, use `Authorization: Bearer <token>`. An API token is automatically generated on first boot and persisted to `./web-manager/.auth_token`. You never have to invent `BEETS_WEB_AUTH_TOKEN` yourself.

### Password Requirements

Browser passwords must satisfy these rules (enforced server-side when saving, and shown live as a strength meter in System settings):

- At least 16 characters by default (`BEETS_WEB_PASSWORD_MIN_LENGTH`, minimum configurable floor 12)
- Long passphrases are supported
- Uppercase letters, numbers, and symbols are allowed but not mandatory

Set `BEETS_WEB_AUTH_DISABLED=1` only for isolated local development with no network exposure.

## How Imports Work

Downloaded or staged files enter an import-review flow. The backend compares filenames, tags, MusicBrainz release evidence, track counts, durations, and fingerprints where available before importing into Beets. Failed or ambiguous imports remain visible for review instead of being silently deleted.

## How AI Matching Works

AI can suggest metadata or cleanup candidates, but model output is treated as untrusted. Application code validates structured output, checks identifiers and paths, and requires deterministic evidence before destructive actions.

**AI is an enhancement, not a requirement.** MusicBrainz search and AcoustID fingerprinting always run first and are what actually identify a release or recording; the AI call, when configured, only ranks/adjudicates between the candidates that search and fingerprinting already found. If the configured AI provider is unreachable or rejects the request for any reason — no API key, an invalid key, an HTTP 401/403, a timeout, a rate limit, an unavailable provider, or an invalid model — the app does not stop or fail the import. It logs the reason, marks that suggestion as `ai_available: false` with a human-readable `ai_unavailable_reason`, and falls back to the top-ranked MusicBrainz/AcoustID candidate with a downgraded confidence tier and a reason string of the form:

> Matched using MusicBrainz and AcoustID (AI unavailable: the AI provider rejected the API key (invalid or unauthorized)).

Import Review and the Library repair flows surface this exactly like any other match — nothing is silently skipped, and nothing requires a working AI key to complete.

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
- Beets CLI, SQLite library, and media mutation isolated in the `beets` engine container
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

## Network Access

By default, `docker-compose.yml` publishes port `8337` on all interfaces (`0.0.0.0:8337`). You can access Beets Web Manager from any browser on your network at:

```text
http://<server-ip>:8337
```

To restrict access to the local machine only, set `WEBCONTROL_PORT=127.0.0.1:8337` or adjust the ports mapping in `docker-compose.yml`.

## Troubleshooting

**`pull access denied for beets-engine`**
Docker encountered a local-only engine image tag (`beets-engine:local` or `beets-engine:dev`) without a local build context. Do **not** run `docker login`. Use the production `docker-compose.yml` (bundled beets + beets-web-manager using published GHCR images), `examples/docker-compose.external-beets.yml` (for standalone web-manager deployments), or run `docker compose -f docker-compose.full.yml up -d --build` from the repository root.

**`pull access denied for beets-web-manager`**
Compose is attempting to use a local-only image name instead of the published registry image. Make sure your Compose file uses `image: ghcr.io/iranman/beets-web-manager:${BEETS_WEB_MANAGER_VERSION:-stable}` or run `docker compose -f docker-compose.dev.yml up -d --build` for local source builds.

**`failed to read dockerfile`**
A development Compose file or `build: .` block is being run outside the repository root directory. Production Compose files use published images and do not require a local Dockerfile. See [docs/EXAMPLES.md](docs/EXAMPLES.md) for existing stack snippets.

**`cannot connect to Beets API`**
In the standard unified deployment (the production `docker-compose.yml` above), Beets Web Manager talks to its own embedded control agent over an internal loopback address and there is nothing to configure. This error normally only applies to the advanced [external Beets](examples/docker-compose.external-beets.yml) deployment — verify `BEETS_API_URL` and `BEETS_API_TOKEN` are set correctly there and that the remote Beets control agent service is healthy and reachable over the network.

**The app returns 503 "Authentication is required" and I can't reach the UI at all.**
This means neither `BEETS_WEB_AUTH_TOKEN` nor `BEETS_WEB_PASSWORD` resolved to a usable value when the process started. On a fresh install, read the auto-generated API token from the file it was persisted to (it is never printed to logs): `docker exec <container> cat /data/.auth_token`. The provided Compose files persist that generated token to `/data/.auth_token` (via `BEETS_WEB_AUTH_TOKEN_FILE`) so it survives a restart — make sure the mounted `./web-manager` host directory (mounted at `/data`) is writable, or startup will fail closed rather than run with an unrecoverable, unpersisted token.

**"AI authentication failed" / no OpenAI key configured — will my imports still work?**
Yes. AI is optional everywhere it's used for matching. A missing/invalid AI key, an HTTP 401/403 from the provider, a timeout, or a rate limit never stops MusicBrainz or AcoustID matching — those run unconditionally and are what actually identify releases and recordings.

**I set `BEETS_WEB_PASSWORD` but saving it was rejected.**
Passwords must be at least 16 characters by default (`BEETS_WEB_PASSWORD_MIN_LENGTH`) and must not be obvious placeholders. Long passphrases are supported; uppercase letters, numbers, and symbols are allowed but not mandatory.

**Where do I check whether MusicBrainz, AcoustID, AI, and Plex are actually reachable right now?**
`GET /api/setup/status` queries the internal Beets control agent for readiness. Use `POST /api/setup/test/{ai,musicbrainz,acoustid,plex}` or the System page connection tests for live provider connectivity.

## Demo Mode

Try the app without your own music library or paid AI credentials:

```bash
python scripts/seed_demo_library.py
```

Generates a few short, self-synthesized sine-wave WAV files (not copies of any real recording — zero copyright concern) tagged as "Demo Artist / Beets Web Manager Demo Album" under your music path. Set `DEMO_MODE=1` in `.env` so `/api/setup/status` flags it clearly as demo data. Fully removable: delete the generated folder and unset `DEMO_MODE`.

## Backups

```bash
./scripts/backup.sh              # writes ./backups/beets-backup-<timestamp>.tar.gz
./scripts/restore.sh <file.tar.gz>
```

Back up `/config/config.yaml`, `/config/musiclibrary.blb`, plugin configuration, and web-manager state files under `/config` before upgrades or migrations — **not** your music library, which should be backed up separately with storage/snapshot tooling.

## Manual Beets CLI and Shared Locking

The standard `docker-compose.yml` above runs the **stock, unmodified** `lscr.io/linuxserver/beets` image as the `beets` service — it does not include the `beet-locked` wrapper (that only exists in the custom-built `beets-engine` image used by `docker-compose.full.yml`/`examples/docker-compose.external-beets.yml`). `docker compose exec beets beet-locked ...` will fail with "command not found" on the standard stack.

Beets Web Manager owns `/config/musiclibrary.blb` through its own embedded engine, and every mutation it performs (imports, cleanup, tag writes, moves) serializes on `/config/.beet_db.lock`. Plain `beet` commands run manually inside the `beets` container do **not** acquire that lock — SQLite's own file-level locking prevents literal database corruption from two processes writing at once, but it does not coordinate with Web Manager's own multi-step operations (for example, a manual `beet import` racing a Web Manager cleanup job that is mid-way through renaming the same files).

Recommended safe usage:

```bash
# Read-only inspection is always safe, any time
docker compose exec beets beet ls artist:311
docker compose exec beets beet version
```

For mutating operations (`beet import`, `beet move`, `beet write`, etc.), prefer doing them through Beets Web Manager's own UI/API. If you do need to run a manual mutating `beet` command in the `beets` container, do it while Beets Web Manager has no import/cleanup job actively running.

No second Beets database is created — both containers open the exact same `/config/musiclibrary.blb`, and both are pinned to Beets 2.13.1 so there is no schema-version skew between them.

## Architecture Migration & Upgrades

Upgrading the Beets **engine version** specifically (not just rebuilding the
same version) needs the additional backup/verification/rollback steps in
`docs/BEETS_ENGINE_MIGRATION.md` -- newer Beets releases can perform an
automatic, one-time, non-reversible database schema migration on first open.

```bash
# 1. Back up config and library
./scripts/backup.sh

# 2. Rebuild both services with clean layers
docker compose build --no-cache beets beets-web-manager

# 3. Re-create and restart the stack
docker compose up -d --force-recreate beets beets-web-manager

# 4. Verify agent health, web health, and remote Beets diagnostics
docker compose exec beets /lsiopy/bin/beet version
curl -s http://127.0.0.1:8337/api/health
curl -s http://127.0.0.1:8337/health/ready
curl -s http://127.0.0.1:8337/api/setup/status
```

### Rollback

If a rollback is required, restore the prior image tags and `/config` backup:

```bash
./scripts/restore.sh ./backups/beets-backup-<timestamp>.tar.gz
docker compose up -d --force-recreate
```

## Support Beets Web Manager

If this project helps your library, please consider supporting its future.

[![ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/K5G823CQ0S)

- ⭐ Leave a star on this project: One shines alone; together, they make it visible and keep it alive.
- Donate to support future development, AI licenses, homelab infrastructure, and ongoing maintenance.

Sponsor links are configured through GitHub's Sponsor button when available.

## Documentation

- [`docs/INSTALLATION.md`](docs/INSTALLATION.md) — detailed installation and deployment.
- [`docs/CONFIGURATION.md`](docs/CONFIGURATION.md) — configuration variables and integrations.
- [`docs/EXAMPLES.md`](docs/EXAMPLES.md) — embedding into an existing Compose stack.
- [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md) — common problems and fixes.
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — current system shape and non-negotiable product rules.
- [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md) — developer setup, validation commands, and engineering constraints.
- [`docs/TECHNICAL_DEBT.md`](docs/TECHNICAL_DEBT.md) — known open architecture work.
- [`SECURITY.md`](SECURITY.md) — supported versions and how to report a vulnerability.

## Contributing

See `CONTRIBUTING.md`. Commit messages should use concise conventional prefixes such as `feat:`, `fix:`, `docs:`, `test:`, `build:`, `ci:`, and `chore:`.

## License

MIT. See `LICENSE`.

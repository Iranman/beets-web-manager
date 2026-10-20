# Beets Web Manager

Beets Web Manager is a self-hosted web application for managing a Beets music library, import review, playlist repair, acquisition queues, cleanup jobs, metadata verification, and media-server synchronization from one operator-focused interface.

The app is designed for local or self-hosted deployments where the music library, download staging folders, Beets database, Plex, downloader services, MusicBrainz, AcoustID, and optional AI providers are controlled by the administrator.

This project exists because the Beets web plugin didn't cover enough on its own: a full UI, import review, playlist repair, acquisition queues, cleanup jobs, and metadata verification on top of Beets. Issues and improvement suggestions are welcome.

## Features

- Beets runs embedded inside Web Manager: one container, Beets 2.14.1 as a pinned library, your existing `config.yaml`, `musiclibrary.blb` and music folder, and a startup self-test that names what is wrong ([ADR 0006](docs/adr/0006-embedded-beets.md)). Enable it with `BEETS_ENGINE=embedded`; it becomes the default in the next phase. The legacy two-container mode (a stock `lscr.io/linuxserver/beets` container reached over HTTP) still works and is the rollback path.
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
> **Beets now runs inside Web Manager.** One container holds the web app and Beets itself (a pinned Beets 2.14.1). It uses the same Beets `config.yaml`, `musiclibrary.blb` and music folder a Beets container would. You do **not** need to install Python, Beets, `pip`, or `uv` on your host machine.
>
> Embedded Beets is turned on with `BEETS_ENGINE=embedded`. Until it becomes the default, a Compose file without that line runs the legacy two-container setup below.

### Quick Start (Production, single container)

#### 1. Create a project directory with a Beets config
```bash
mkdir beets-stack && cd beets-stack
mkdir beets web-manager
curl -fsSL -o beets/config.yaml https://raw.githubusercontent.com/Iranman/beets-web-manager/main/config.yaml.example
```

Already have a Beets `/config` folder (with `config.yaml` and `musiclibrary.blb`)? Use that folder instead of `./beets`, and see [Moving from two containers to one](docs/INSTALLATION.md#moving-from-two-containers-to-one) first.

#### 2. Create `docker-compose.yml`
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
      - BEETS_ENGINE=embedded
      - MUSIC_ROOT=/music
      - DOWNLOADS_ROOT=/downloads
      - BEETS_CONFIG=/config/config.yaml
    volumes:
      - ./beets:/config
      - /path/to/music:/music
      - /path/to/downloads:/downloads
      - ./web-manager:/web-manager-data
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8337/api/health', timeout=5)"]
      interval: 10s
      timeout: 5s
      retries: 5
      start_period: 30s
```

`/music` is mounted read-write: Beets moves, tags and imports files from inside this container. [`examples/docker-compose.embedded.yml`](examples/docker-compose.embedded.yml) is the full, commented file with every optional integration variable and the container hardening.

*(Adjust `/path/to/music` and `/path/to/downloads` to match your media storage folders).*

#### 3. Start it
```bash
docker compose up -d
```

#### 4. Check Beets and open Beets Web Manager
```bash
curl -s http://127.0.0.1:8337/api/health
```

`"beets": {"engine": "embedded", "ok": true, ...}` means Beets started. If `ok` is false, `codes` names the problem; [Troubleshooting](docs/TROUBLESHOOTING.md#embedded-beets-self-test-codes) has the fix for each code. A new, empty library reports `library_created`, which is expected on a fresh install.

Open **`http://<server-ip>:8337`** in your browser. On your first visit, you will be guided through a simple setup wizard to create your administrator username and password.

---

### Legacy: two containers (stock Beets over HTTP)

This is the setup the repository's [`docker-compose.yml`](docker-compose.yml) still ships until embedded becomes the default, and the rollback path from embedded mode. Web Manager talks to a stock LinuxServer Beets container over HTTP and installs its `webmanager` plugin into that container's `/config/beetsplug`.

```yaml
services:
  beets:
    image: lscr.io/linuxserver/beets:2.14.1-ls356@sha256:ae076820ec34ea5e9ec05a22931e98ab555e9c25e784c5fb001c84f411119c73
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

See the repository's [`docker-compose.yml`](docker-compose.yml) for the exact, fully-commented two-container file.

---

### Running Beets CLI Commands

Embedded: the same `beet` runs inside the Web Manager container. Run it as the app's user, and keep to read-only commands; library changes go through the Web Manager UI so they get a preview, an audit entry and a rollback:

```bash
docker compose exec -u 1000:1000 beets-web-manager beet version
docker compose exec -u 1000:1000 beets-web-manager beet ls artist:311
```

Two containers: run commands in the `beets` container, for example `docker compose exec beets beet ls`.

---

### Advanced: Standalone / External Beets (legacy)
If you run a standalone Beets container on a separate server or stack and want to keep it, see [examples/docker-compose.external-beets.yml](examples/docker-compose.external-beets.yml) to connect Beets Web Manager over the network. With embedded Beets you mount that server's Beets `/config` and music folders into the Web Manager container instead.

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

With embedded Beets, the Web Manager container mounts `/config` (Beets `config.yaml` and `musiclibrary.blb`), `/music` (read-write), `/downloads`, and `/web-manager-data` (Web Manager's own state). Web Manager never edits `config.yaml` in this mode; change Beets settings in the file or on the Config page. In the legacy two-container stack, `/config`, `/music` (read-only in Web Manager; stock Beets owns the writes) and `/downloads` are shared between the two containers.

## Environment Variables

Key variables include:

- `PUID` / `PGID`: user and group IDs for file permissions (defaults to `1000`).
- `TZ`: time zone (defaults to `Etc/UTC`).
- `WEBCONTROL_PORT`: web UI port (defaults to `8337`).
- `BEETS_WEB_PASSWORD` / `BEETS_WEB_USERNAME`: administrator credentials for browser access (created via the browser setup wizard on first visit).
- `OPENAI_API_KEY` or compatible provider key: **optional** AI metadata features — see [How AI Matching Works](#how-ai-matching-works).
- `PLEX_URL` and `PLEX_TOKEN`: Plex sync and refresh integration (optional).
- `LIDARR_URL` and `LIDARR_API_KEY`: wanted-music and Arr integration (optional).
- `ACOUSTID_API_KEY` / `ACOUSTID_KEY`: your own AcoustID application key (https://acoustid.org/new-application). Required for fingerprint lookups; without it AcoustID is reported as not configured and fingerprint evidence is unavailable.
- `SLSKD_SLSK_USERNAME` and `SLSKD_SLSK_PASSWORD`: Soulseek client credentials (optional, required only for SLSKD-based acquisition).

### Required vs. optional integrations

| Integration | Requirement | Notes |
| ----------- | ----------- | ----- |
| Beets | Required | Core music library engine; runs in the same Compose stack |
| MusicBrainz | Built-in | Public metadata API used for release and recording matching |
| AcoustID | Recommended (needs your own application key) | Audio fingerprint matching and safety verification |
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

**`pull access denied for beets-web-manager`**
Compose is attempting to use a local-only image name instead of the published registry image. Make sure your Compose file uses `image: ghcr.io/iranman/beets-web-manager:latest` or run `docker compose -f docker-compose.dev.yml up -d --build` for local source builds.

**`failed to read dockerfile`**
A development Compose file or `build: .` block is being run outside the repository root directory. Production Compose files use published images and do not require a local Dockerfile. See [docs/EXAMPLES.md](docs/EXAMPLES.md) for existing stack snippets.

**`/api/health` shows `"beets": {"ok": false, "codes": [...]}`**
Embedded Beets failed a startup check. Each code and its fix is in [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md#embedded-beets-self-test-codes). While a check fails, reads keep working but library changes are refused with the reason.

**`cannot connect to Beets`** (two-container mode)
Beets Web Manager talks to the stock `beets` container over HTTP at `BEETS_WEB_URL` (default `http://beets:8337`, the Docker Compose service name and stock Beets' own `web`/`webmanager` plugin port). Confirm the `beets` container is running and healthy, and that `BEETS_WEB_URL` and `BEETS_OUTBOUND_ALLOWLIST` in your `.env` point at it.

**The app returns 503 "Authentication is required" and I can't reach the UI at all.**
This means neither `BEETS_WEB_AUTH_TOKEN` nor `BEETS_WEB_PASSWORD` resolved to a usable value when the process started. On a fresh install, read the auto-generated API token from the file it was persisted to (it is never printed to logs): `docker exec <container> cat /web-manager-data/.auth_token`. The provided Compose files persist that generated token to `/web-manager-data/.auth_token` (via `BEETS_WEB_AUTH_TOKEN_FILE`) so it survives a restart — make sure the mounted `./web-manager` host directory (mounted at `/web-manager-data`) is writable, or startup will fail closed rather than run with an unrecoverable, unpersisted token.

**"AI authentication failed" / no OpenAI key configured — will my imports still work?**
Yes. AI is optional everywhere it's used for matching. A missing/invalid AI key, an HTTP 401/403 from the provider, a timeout, or a rate limit never stops MusicBrainz or AcoustID matching — those run unconditionally and are what actually identify releases and recordings.

**I set `BEETS_WEB_PASSWORD` but saving it was rejected.**
Passwords must be at least 16 characters by default (`BEETS_WEB_PASSWORD_MIN_LENGTH`) and must not be obvious placeholders. Long passphrases are supported; uppercase letters, numbers, and symbols are allowed but not mandatory.

**Where do I check whether MusicBrainz, AcoustID, AI, and Plex are actually reachable right now?**
`GET /api/setup/status` calls the stock Beets `webmanager` integration plugin for readiness. Use `POST /api/setup/test/{ai,musicbrainz,acoustid,plex}` or the System page connection tests for live provider connectivity, and `GET /api/plex/status` for the Plex library. A failed test returns a `reason`: `outbound_not_allowlisted` means Web Manager's outbound network policy refused the address (the message names the `host:port` to set or allow), while `unreachable`, `timeout` and `auth_failed` are real network or credential problems. See [Troubleshooting](docs/TROUBLESHOOTING.md) sections 15 and 16.

## Demo Mode

Try the app without your own music library or paid AI credentials:

```bash
python scripts/seed_demo_library.py
```

Generates a few short, self-synthesized sine-wave WAV files (not copies of any real recording — zero copyright concern) tagged as "Demo Artist / Beets Web Manager Demo Album" under your music path. Set `DEMO_MODE=1` in `.env` so `/api/setup/status` flags it clearly as demo data. Fully removable: delete the generated folder and unset `DEMO_MODE`.

## Backups

`scripts/backup.sh` and `scripts/restore.sh` are plain bash scripts you run on the Docker host. Copy them into your stack directory (the one with `docker-compose.yml`), or run them from a clone of this repository and pass the folders explicitly.

```bash
# From the stack directory: backs up ./beets and ./web-manager into ./backups/
./backup.sh

# Or name the folders (the host folders mounted at /config and /web-manager-data)
./backup.sh --beets-config /srv/stack/beets --web-manager-data /srv/stack/web-manager --out /srv/backups
```

A backup holds:

- the Beets library database (`musiclibrary.blb`), copied with SQLite's online backup API so it is consistent while Beets runs. This needs the `sqlite3` command or `python3` on the host. Without either, stop Beets first (`docker compose stop beets`) and pass `--beets-stopped`.
- Beets `config.yaml`, `beetsplug/` and the plugin key file;
- Web Manager's whole state folder (`/web-manager-data`): settings, login, API token, session key and the transaction audit trail.

It does **not** include your music files; back those up with your storage's snapshot tooling. The archive contains secrets and is written with mode 600.

To restore, stop both containers, restore, and start them again. Files the restore replaces are moved to `.pre-restore-<timestamp>/` in the same folder, never deleted:

```bash
docker compose stop
./restore.sh backups/beets-backup-<timestamp>.tar.gz      # add --beets-config/--web-manager-data if not ./beets and ./web-manager
docker compose up -d
```

## Manual Beets CLI

With embedded Beets, `beet` runs inside the Web Manager container (see [Running Beets CLI Commands](#running-beets-cli-commands)). The rest of this section is about the legacy two-container stack.

The `beets` service is the **stock, unmodified** `lscr.io/linuxserver/beets` image — the sole authoritative Beets runtime and the sole owner of `/config/musiclibrary.blb`. Beets Web Manager never opens that database directly; it reads and writes through the same `beets` container's HTTP API (its `web` plugin for reads, its bundled `webmanager` plugin for authenticated mutations).

Read-only CLI inspection inside the `beets` container is always safe, any time:

```bash
docker compose exec beets beet ls artist:311
docker compose exec beets beet version
```

For mutating operations (`beet import`, `beet move`, `beet write`, etc.), prefer doing them through Beets Web Manager's own UI/API, which serializes them through its own controlled preview/apply/audit workflow. If you do need to run a manual mutating `beet` command in the `beets` container, do it while Beets Web Manager has no import/cleanup job actively running — SQLite's own file-level locking prevents literal database corruption from two processes writing at once, but it does not coordinate with Web Manager's own multi-step operations.

## Upgrades

Upgrading the **Beets image version** specifically (bumping the `beets` service's tag, not just recreating the same version) needs the additional backup/verification/rollback steps in `docs/BEETS_ENGINE_MIGRATION.md` — newer Beets releases can perform an automatic, one-time, non-reversible database schema migration on first open.

Read the release's **Upgrade Notes** in [`CHANGELOG.md`](CHANGELOG.md) or on the GitHub release page first.

```bash
# 1. Back up the library database, Beets config and Web Manager state
./backup.sh

# 2. Pull the new images
docker compose pull

# 3. Re-create the stack
docker compose up -d

# 4. A new Web Manager version can ship a new webmanager plugin. Beets loads
#    plugin code only when it starts, so restart it once after the upgrade:
docker compose restart beets

# 5. Verify
docker compose exec beets beet version
curl -s http://127.0.0.1:8337/health/live      # reports the running Web Manager version
curl -s http://127.0.0.1:8337/health/ready
curl -s http://127.0.0.1:8337/api/setup/status  # "blocking_reasons" should be empty
```

With embedded Beets there is no `beets` container to restart: steps 4 and the `beet version` check do not apply. `curl -s http://127.0.0.1:8337/api/health` shows the Beets self-test instead.

### Rollback

To go from embedded Beets back to two containers, see [Rolling back to two containers](docs/INSTALLATION.md#rolling-back-to-two-containers).

Put the previous version back in the `image:` line of `beets-web-manager` (for example `ghcr.io/iranman/beets-web-manager:0.1.48` instead of `:latest`), restore the backup you made before upgrading, and start again:

```bash
docker compose stop
./restore.sh backups/beets-backup-<timestamp>.tar.gz
docker compose up -d --force-recreate
docker compose restart beets     # load the previous plugin version
curl -s http://127.0.0.1:8337/health/live   # confirm the version
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

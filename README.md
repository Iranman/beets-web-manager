# Beets Web Manager

Beets Web Manager is a self-hosted web application for managing a Beets music library, import review, playlist repair, acquisition queues, cleanup jobs, metadata verification, and media-server synchronization from one operator-focused interface.

The app is designed for local or self-hosted deployments where the music library, download staging folders, Beets database, Plex, downloader services, MusicBrainz, AcoustID, and optional AI providers are controlled by the administrator.
I made this because the beets web plugin just wasn't cutting it. Not only does this web app have a UI but it can do everything I need to manage my music libary. Yes this was vibe-coded and if that bothers you please dont waste your time. But for everyone else I tried my best to cover all vunerbilities and make sure all features work correctly. Please feel free to point out issues or how to make it better.

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

Beets Web Manager is packaged as a container image published to GitHub Container Registry (`ghcr.io/iranman/beets-web-manager`). End users do not need Node.js, Python, or build tools on the host system.

### Quick Start (Production)

Clone the repository to obtain `docker-compose.yml`, `.env.example`, and setup scripts:

```bash
git clone https://github.com/Iranman/beets-web-manager.git
cd beets-web-manager
cp .env.example .env
```

Configure your Beets control agent credentials in `.env`:

```env
BEETS_API_URL=http://beets:8338
BEETS_API_TOKEN=your-beets-api-token
```

Pull the published image and start the container:

```bash
docker compose pull
docker compose up -d
docker compose ps
```

Or run the automated setup script:

```bash
./setup.sh          # Linux / macOS
.\setup.ps1         # Windows PowerShell
```

### Existing Stack Integration (TrueNAS / Portainer / Multi-App Stacks)

To add `beets-web-manager` to an existing Docker Compose stack (e.g., `/srv/media-stack/docker-compose.yml`), copy the service block from [docs/EXAMPLES.md](docs/EXAMPLES.md).

It uses the published image (`ghcr.io/iranman/beets-web-manager:${BEETS_WEB_MANAGER_VERSION:-stable}`), requires no local `build:` directive, and persists state to a host path (e.g., `/srv/media-stack/beets-web-manager:/web-manager-data`).

### Development Installation (Source Builds)

To build both `beets-web-manager` and the `beets` engine from local source code:

```bash
docker compose -f docker-compose.dev.yml up -d --build
```

### Optional Bundled Beets Stack (Advanced Users)

To deploy the published Beets Web Manager alongside a locally built custom Beets engine (`Dockerfile.beets`):

```bash
docker compose -f docker-compose.full.yml up -d --build
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

Most runtime configuration comes from environment variables and `/config/config.yaml` inside the container. Secrets must be provided through `.env`, Docker secrets, or mounted secret files. Do not commit real credentials.

Beets and Beets plugins are installed only in the `beets` engine image built from `Dockerfile.beets`. The web-manager image built from `Dockerfile` does not include Beets and must communicate with the engine through `BEETS_API_URL` plus `BEETS_API_TOKEN`. The engine image includes the bundled `discpath` plugin under `/opt/beets-web-manager-agent/beetsplug`; user plugins may be mounted at `/config/beetsplug`, and `pluginpath` searches `/config/beetsplug` before the bundled path. Do not install Python packages manually inside running containers; rebuild images from source.

See `.env.example` for the required and optional variables.

## Environment Variables

Important variables include:

- `BEETS_API_TOKEN`: strong shared secret between `beets-web-manager` and the internal Beets control agent. **Required**; blank, weak, and placeholder values are rejected by the engine.
- `BEETS_API_URL`: internal URL for the Beets control agent, normally `http://beets:8338`.
- `BEETS_WEB_AUTH_TOKEN`: bearer token for API/script clients. **Required**, but not something you have to invent yourself - see [Authentication](#authentication) below.
- `BEETS_WEB_PASSWORD` / `BEETS_WEB_USERNAME`: Basic Auth credentials for browser access. See [Authentication](#authentication) for the password requirements.
- `OPENAI_API_KEY` or compatible provider key: **optional** AI metadata features — see [How AI Matching Works](#how-ai-matching-works).
- `PLEX_URL` and `PLEX_TOKEN`: Plex sync and refresh integration (optional).
- `LIDARR_URL` and `LIDARR_API_KEY`: wanted-music and Arr integration (optional).
- `ACOUSTID_API_KEY` / `ACOUSTID_KEY`: optional — AcoustID lookups work without a key via a shared, rate-limited test key.
- `SLSKD_SLSK_USERNAME` and `SLSKD_SLSK_PASSWORD`: Soulseek client credentials (optional, required only for SLSKD-based acquisition).
- `BEETS_OUTBOUND_ALLOWLIST`: exact host:port or CIDR:port entries for local services the backend may call; defaults to the internal Beets control agent (`beets:8338`).
- `BEETS_TRUSTED_PROXIES`: direct proxy CIDRs whose forwarded client IP headers may be trusted.

MusicBrainz needs no key or account — it is a public API used for every release/recording lookup regardless of what else is configured.

### Required vs. optional integrations

| Integration | Required? | What breaks if missing |
|---|---|---|
| Authentication (token or password) | **Required** | The app refuses every request until one is configured — see [Authentication](#authentication). An unconfigured install now auto-generates a token instead of locking itself out. |
| MusicBrainz | **Required** (no setup needed) | N/A — public API, always available. |
| AcoustID | Optional | Fingerprint-based matching falls back to a shared, rate-limited test key; add your own key for higher-volume use. |
| AI (OpenAI-compatible) | Optional | AI-assisted ranking/adjudication is skipped. Matching continues on MusicBrainz + AcoustID evidence alone, at a lower confidence tier — see below. |
| Plex | Optional | Plex sync/refresh actions are unavailable; everything else works normally. |
| Lidarr, SLSKD, Discogs, ListenBrainz, Spotify | Optional | Each feature they back (wanted-album import, Soulseek acquisition, discography/art lookups, scrobble history, playlist parsing) is disabled on its own; nothing else is affected. |

## Authentication

The app provides separate authentication boundaries for human browser operators and API/script clients.

### Quick Reference: Login Credentials & Tokens

| Purpose | Credential | Where to find / set | Details |
|---|---|---|---|
| **Browser Login** | Username: `admin` (or `BEETS_WEB_USERNAME`) <br>Password: Created in browser or set in `.env` | Created at `http://<server-ip>:8337` on first run, or prompted during `setup.sh`/`setup.ps1` | Web browser setup & Basic Auth prompt (`http://<server-ip>:8337`) |
| **API / Scripts** | `BEETS_WEB_AUTH_TOKEN` (Bearer token) | Persisted to `/web-manager-data/.auth_token` | Used via header `Authorization: Bearer <token>`. **NOT** your browser password! |
| **Beets Engine** | `BEETS_API_TOKEN` | Configured in `.env` | Internal service token between Web Manager and Beets control agent |

> [!IMPORTANT]
> `BEETS_WEB_AUTH_TOKEN` is an API bearer token for scripts and tools. It is **NOT** your browser password. Use your browser username and browser password when prompted by your web browser.

**You never have to invent `BEETS_WEB_AUTH_TOKEN` yourself.** Same as the browser password, if it's left blank the app generates a cryptographically secure 256-bit token on first boot and persists it to `/web-manager-data/.auth_token` (`0600` permissions) — the two are generated and persisted independently of each other.

### First-Run Browser Setup

1. **Browser First-Run Setup (Default):**  
   Start Beets Web Manager (`docker compose up -d`) and open `http://<server-ip>:8337` in your browser. You will see the **Finish Beets Web Manager Setup** page where you can create your administrator username and password directly. Once setup completes, sign in with your credentials.

2. **Guided Interactive CLI Setup (`setup.sh` / `setup.ps1`):**  
   Prompts you to create your administrator username and password in your terminal before starting the containers.

3. **Recovery / Headless Fallback:**  
   If no browser password was configured before startup, an initial fallback password is also generated at:
   ```text
   /web-manager-data/.initial_admin_password
   ```
   If needed for headless or recovery scenarios, retrieve it using:
   ```bash
   docker exec beets-web-manager cat /web-manager-data/.initial_admin_password
   ```
3. **Changing Your Browser Password:**  
   Sign in to the web UI, go to **System → Environment Variables**, enter a new password for `BEETS_WEB_PASSWORD`, and click **Save environment**. Once the new password is verified, `.initial_admin_password` is automatically removed.

### Password Requirements

Browser passwords must satisfy these complexity rules (enforced server-side when saving, and shown live as a strength meter in System settings):

- At least 32 characters (matches `BEETS_WEB_AUTH_MIN_LENGTH`)
- At least one uppercase letter (`A-Z`)
- At least one lowercase letter (`a-z`)
- At least one number (`0-9`)
- At least one special (non-alphanumeric) character

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

## First-Run Setup

For the simplest possible start:

```bash
./setup.sh          # or .\setup.ps1 on Windows
```

This creates `.env`/`config.yaml` from the example templates, generates a random `BEETS_WEB_AUTH_TOKEN`, and starts the two-service stack via `docker-compose.yml`. You must set a strong `BEETS_API_TOKEN` before the Beets engine will accept internal control-agent requests. Readiness and per-integration connectivity checks are available at `GET /api/setup/status` and `POST /api/setup/test/{ai,musicbrainz,acoustid,plex}`, and standard health probes at `/health`, `/health/live`, `/health/ready`.

On a fresh `.env`, the setup script also asks how the web UI should be reachable:

```text
=== Web Access ===
1. This computer only (127.0.0.1)
2. Other devices on my local network (0.0.0.0)

Choose [2 for most NAS/server installs]:
```

This sets `BEETS_WEB_BIND_ADDRESS` in `.env`. **`docker-compose.yml` defaults to `127.0.0.1`** (published as `127.0.0.1:8337->8337`) — secure by default, but if Beets Web Manager runs on a NAS/server (TrueNAS, Portainer, Unraid, a headless Linux box, etc.) and you access it from another computer, the UI will be unreachable until you set:

```env
BEETS_WEB_BIND_ADDRESS=0.0.0.0
```

**`BEETS_WEB_BIND_ADDRESS` is the container's *listening* address, not a browser URL** — setting it to `0.0.0.0` does not mean browsing to `http://0.0.0.0:8337`. From another device on your network, browse to:

```text
http://<server-ip>:8337
```

using that server's actual LAN IP (find it with `ip addr` / `hostname -I` on Linux, `ipconfig` on Windows, or your NAS's network settings page — this README does not assume or print one for you). `0.0.0.0` widens *which network interfaces* the container listens on; authentication (see below) still gates every request regardless of bind address.

Packaging status: passing tests on a local or CI branch do not authorize production deployment. Rebuild and validate both services in disposable storage before migrating an existing library.

## Troubleshooting

**`pull access denied for beets-engine`**
Docker encountered a local-only engine image tag (`beets-engine:local` or `beets-engine:dev`) without a local build context. Do **not** run `docker login`. Use the production `docker-compose.yml` (web-manager only) with an existing `BEETS_API_URL`, or run `docker compose -f docker-compose.full.yml up -d --build` from the repository root.

**`pull access denied for beets-web-manager`**
Compose is attempting to use a local-only image name instead of the published registry image. Make sure your Compose file uses `image: ghcr.io/iranman/beets-web-manager:${BEETS_WEB_MANAGER_VERSION:-stable}` or run `docker compose -f docker-compose.dev.yml up -d --build` for local source builds.

**`failed to read dockerfile`**
A development Compose file or `build: .` block is being run outside the repository root directory. Production Compose files use published images and do not require a local Dockerfile. See [docs/EXAMPLES.md](docs/EXAMPLES.md) for existing stack snippets.

**`cannot connect to Beets API`**
Verify that `BEETS_API_URL` and `BEETS_API_TOKEN` are set correctly in `.env` and that the Beets control agent service is healthy and reachable over the network.

**The app returns 503 "Authentication is required" and I can't reach the UI at all.**
This means neither `BEETS_WEB_AUTH_TOKEN` nor `BEETS_WEB_PASSWORD` resolved to a usable value when the process started. On a fresh install, read the auto-generated API token from the file it was persisted to (it is never printed to logs): `docker exec <container> cat /web-manager-data/.auth_token`. The provided Compose files persist that generated token to `/web-manager-data/.auth_token` (via `BEETS_WEB_AUTH_TOKEN_FILE`) so it survives a restart — make sure the mounted `web-manager-data` host directory is writable, or startup will fail closed rather than run with an unrecoverable, unpersisted token.

**"AI authentication failed" / no OpenAI key configured — will my imports still work?**
Yes. AI is optional everywhere it's used for matching. A missing/invalid AI key, an HTTP 401/403 from the provider, a timeout, or a rate limit never stops MusicBrainz or AcoustID matching — those run unconditionally and are what actually identify releases and recordings.

**I set `BEETS_WEB_PASSWORD` but saving it was rejected.**
Passwords must be at least 32 characters (the same floor `_MIN_AUTH_SECRET_LENGTH`/`BEETS_WEB_AUTH_MIN_LENGTH` uses to decide whether a secret is usable at all) and include an uppercase letter, a lowercase letter, a number, and a special character.

**Where do I check whether MusicBrainz, AcoustID, AI, and Plex are actually reachable right now?**
`GET /api/setup/status` queries the internal Beets control agent for readiness. Use `POST /api/setup/test/{ai,musicbrainz,acoustid,plex}` or the System page connection tests for live provider connectivity.

## Documentation

- [Installation Guide](docs/INSTALLATION.md) — per-platform path-mapping examples (Linux, TrueNAS, Unraid, Synology, Windows) and local dev setup.
- [Configuration Reference](docs/CONFIGURATION.md) — every environment variable and `config.yaml` setting.
- [Troubleshooting](docs/TROUBLESHOOTING.md) — common failure modes and fixes.

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

All manual CLI operations run inside the `beets` container. Mutating manual CLI operations should use the `beet-locked` wrapper to automatically acquire the shared database file lock (`/config/.beet_db.lock`):

```bash
# Run manual read-only Beets CLI query
docker compose exec beets /lsiopy/bin/beet ls artist:311

# Verify the Beets version and loaded plugin line
docker compose exec beets /lsiopy/bin/beet version

# Run manual mutating import inside the Beets container (locked)
docker compose exec beets beet-locked import /data/torrents/music
```

No second Beets database is created. `Dockerfile.beets` applies its Chroma plugin-resolution compatibility patch (`docker/beets/apply_patches.py`) only against exactly Beets 2.4.0; it is skipped (with the upstream fix verified directly) on Beets >= 2.5.0. See `docs/CONFIGURATION.md` ("Beets engine version") and `docs/BEETS_ENGINE_MIGRATION.md` for the version policy and upgrade/rollback procedure.

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

## Contributing

See `CONTRIBUTING.md`. Commit messages should use concise conventional prefixes such as `feat:`, `fix:`, `docs:`, `test:`, `build:`, `ci:`, and `chore:`.

## License

MIT. See `LICENSE`.

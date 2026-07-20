# Beets Web Manager

Beets Web Manager is a self-hosted web application for managing a Beets music library, import review, playlist repair, acquisition queues, cleanup jobs, metadata verification, and media-server synchronization from one operator-focused interface.

The app is designed for local or self-hosted deployments where the music library, download staging folders, Beets database, Plex, downloader services, MusicBrainz, AcoustID, and optional AI providers are controlled by the administrator.
I made this because the beets web plugin just wasn't cutting it. Not only does this web app have a UI but it can do everything I need to manage my music libary. Yes this was vibe-coded and if that bothers you please dont waste your time. But for everyone else I tried my best to cover all vunerbilities and make sure all features work correctly. Please feel free to point out issues or how to make it better.

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

- `BEETS_WEB_AUTH_TOKEN`: bearer token for API/script clients. **Required**, but not something you have to invent yourself — see [Authentication](#authentication) below.
- `BEETS_WEB_PASSWORD` / `BEETS_WEB_USERNAME`: Basic Auth credentials for browser access. See [Authentication](#authentication) for the password requirements.
- `OPENAI_API_KEY` or compatible provider key: **optional** AI metadata features — see [How AI Matching Works](#how-ai-matching-works).
- `PLEX_URL` and `PLEX_TOKEN`: Plex sync and refresh integration (optional).
- `LIDARR_URL` and `LIDARR_API_KEY`: wanted-music and Arr integration (optional).
- `ACOUSTID_API_KEY` / `ACOUSTID_KEY`: optional — AcoustID lookups work without a key via a shared, rate-limited test key.
- `SLSKD_SLSK_USERNAME` and `SLSKD_SLSK_PASSWORD`: Soulseek client credentials (optional, required only for SLSKD-based acquisition).
- `BEETS_OUTBOUND_ALLOWLIST`: exact host:port or CIDR:port entries for local services the backend may call.
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

The app requires at least one working credential — a bearer token, a browser password, or both — before it will serve any route other than health checks and static assets.

**You never have to invent `BEETS_WEB_AUTH_TOKEN` yourself.** If the app starts with no usable token and no usable password configured, it automatically generates a cryptographically secure 256-bit token, stores it (`/config/.auth_token` by default, overridable with `BEETS_WEB_AUTH_TOKEN_FILE`), applies it for that process, and prints it once to the startup log:

```
No BEETS_WEB_AUTH_TOKEN or BEETS_WEB_PASSWORD was configured.
Generated a secure API token automatically...
  BEETS_WEB_AUTH_TOKEN=<token>
Save this now -- it will not be printed again.
```

Save that value from your container logs, or regenerate a fresh one any time from **System → Authentication → Regenerate API token** (the plaintext is shown exactly once, then masked forever after, same as every other secret in the environment editor). The token is a Bearer credential for API/script clients (`Authorization: Bearer <token>`) — the browser UI has no login form and does not send it.

For **browser access**, set `BEETS_WEB_PASSWORD` (paired with `BEETS_WEB_USERNAME`, default `admin`). The server sends `WWW-Authenticate: Basic` on an unauthenticated request, so your browser's native credential prompt handles sign-in — there is no in-app login page. Passwords must meet these requirements (enforced server-side when you save it, and shown live as a strength meter in the System page):

- At least 32 characters (matches `BEETS_WEB_AUTH_MIN_LENGTH`, the same floor the app uses to decide whether *any* configured secret is usable — a shorter password can never be accepted here, because it would immediately fail that separate check and lock browser access out on the next request)
- One uppercase letter
- One lowercase letter
- One number
- One special (non-alphanumeric) character

Set `BEETS_WEB_AUTH_DISABLED=1` only for local development with no network exposure — it fully disables the authentication boundary.

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

Packaging status: the baseline branch has passing GitHub CI for lint, typecheck, frontend build, Python tests, security, and Docker image build. The unreleased setup/demo packaging path still needs a final Docker/startup check after `routes_lidarr.py` is restored.

## Troubleshooting

**"AI authentication failed" / no OpenAI key configured — will my imports still work?**
Yes. AI is optional everywhere it's used for matching. A missing/invalid AI key, an HTTP 401/403 from the provider, a timeout, or a rate limit never stops MusicBrainz or AcoustID matching — those run unconditionally and are what actually identify releases and recordings. The AI call, when it succeeds, only re-ranks candidates that search/fingerprinting already found. When AI isn't available, the top-ranked MusicBrainz/AcoustID candidate is used instead, at a lower confidence tier, with a reason string starting `Matched using MusicBrainz and AcoustID (AI unavailable: ...)`. See [How AI Matching Works](#how-ai-matching-works).

**The app returns 503 "Authentication is required" and I can't reach the UI at all.**
This means neither `BEETS_WEB_AUTH_TOKEN` nor `BEETS_WEB_PASSWORD` resolved to a usable value when the process started. On a fresh install this shouldn't happen — the app auto-generates a token on first boot specifically to avoid this lockout (check your container logs for the one-time printed value). If you still hit this, check that `/config` is writable (the generated token is persisted to `/config/.auth_token`) and that `BEETS_WEB_AUTH_DISABLED` isn't accidentally set to a falsy-looking-but-truthy value.

**I set `BEETS_WEB_PASSWORD` but saving it was rejected.**
Passwords must be at least 32 characters (the same floor `_MIN_AUTH_SECRET_LENGTH`/`BEETS_WEB_AUTH_MIN_LENGTH` uses to decide whether a secret is usable at all) and include an uppercase letter, a lowercase letter, a number, and a special character. The System page's password field shows a live strength meter and a checklist of which requirements are still unmet.

**Where do I check whether MusicBrainz, AcoustID, AI, and Plex are actually reachable right now?**
`GET /api/setup/status` reports each integration's configured/not-configured state without making network calls. For a live connectivity check, use `POST /api/setup/test/{ai,musicbrainz,acoustid,plex}` — each integration is tested and reported independently, so one being down or misconfigured never hides or blocks the results of the others. The System page's Integrations panel has a "Test connections" button that runs all four and shows a ✓ Connected / ⚠ Warning / ✗ Not Configured badge per integration.

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

Backs up the beets database and configuration/state JSON files under `/config` — **not** your music library, which you should back up separately with your own storage/snapshot tooling.

## Updates

```bash
docker compose pull   # or: docker compose build
docker compose up -d
```

Run `./scripts/backup.sh` first. There is no separate database-migration step — the beets library schema is managed by beets itself on next library open.

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

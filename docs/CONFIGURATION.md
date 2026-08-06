# Configuration

Configuration is split between environment variables for the web-manager and control-agent boundary, and `/config/config.yaml` for Beets itself.

## Core variables

| Variable | Service | Required | Meaning |
|---|---|---:|---|
| `BEETS_WEB_MANAGER_VERSION` | compose | no | Published image tag to deploy: `stable` (recommended default), `latest`, exact version `0.1.0`, or `edge`. |
| `BEETS_BASE_IMAGE` | dev/full compose only | no | Upstream LinuxServer Beets base image for the locally built engine (default: `lscr.io/linuxserver/beets:2.13.1`, the tested production candidate). See "Beets engine version" below. |
| `WEBCONTROL_PORT` | web | no | Web port inside the container, default `8337`. |
| `BEETS_WEB_BIND_ADDRESS` | compose | no | Host bind IP address for web UI access (default: `127.0.0.1`, local-only; set to `0.0.0.0` only after configuring authentication, TLS, and a reverse proxy). |
| `BEETS_WEB_MANAGER_DATA_PATH` | compose | no | Host directory for persistent application state (default: `./web-manager-data`). |
| `BEETS_API_TOKEN` | both | yes | Strong shared secret for authenticated internal HTTP from `beets-web-manager` to `beets`. Blank, weak, and placeholder values are rejected by the control agent. |
| `BEETS_API_URL` | web | yes | Control agent endpoint URL. Same stack: `http://beets:8338`, external LAN: `http://192.168.1.50:8338`. |
| `BEETS_WEB_AUTH_TOKEN` | web | yes | Owner API/script token. The app auto-generates a secure token when no credential is set. |
| `BEETS_WEB_PASSWORD` | web | optional | Browser Basic Auth password. Must satisfy strength rules (32+ chars, upper, lower, number, special). |
| `BEETS_WEB_USERNAME` | web | optional | Browser Basic Auth username, default `admin`. |
| `BEETS_OUTBOUND_ALLOWLIST` | web | optional | Comma-separated host:port or CIDR:port entries for private services the web manager may contact. Defaults to `beets:8338`. |
| `BEETS_TRUSTED_PROXIES` | web | optional | Proxy CIDRs whose forwarded client IP headers may be trusted. |
| `BEETS_ENABLE_LEGACY_LOCAL_SCAN` | web | optional | Opt-in guard for the old local `/api/library/scan` filesystem walk. Keep `0`. |
| `BEETS_SCAN_STATE_FILE` | web | optional | State file for the legacy local scan guard, default `/web-manager-data/last_scan.txt`. |

## Optional integrations

| Variable | Purpose |
|---|---|
| `OPENAI_API_KEY`, `OPENROUTER_API_KEY`, `AI_API_KEY`, `AI_BASE_URL`, `AI_MODEL` | Optional AI-assisted candidate ranking. Matching still uses MusicBrainz and AcoustID evidence without AI. |
| `ACOUSTID_API_KEY`, `ACOUSTID_KEY` | Optional higher-volume AcoustID lookups and submission readiness. Fingerprinting still requires Chroma/fpcalc in the Beets engine. |
| `DISCOGS_TOKEN`, `DISCOGS_USER_TOKEN` | Optional Discogs metadata. |
| `LISTENBRAINZ_TOKEN` | Optional ListenBrainz integration. |
| `PLEX_URL`, `PLEX_TOKEN` | Optional Plex sync and refresh. Use deployment-specific URLs, not committed private LAN defaults. |
| `LIDARR_URL`, `LIDARR_API_KEY` | Optional wanted-album integration. |
| `SLSKD_URL`, `SLSKD_API_KEY`, `SLSKD_API_KEY_FILE`, `SLSKD_SLSK_USERNAME`, `SLSKD_SLSK_PASSWORD` | Optional SLSKD/Soulseek acquisition. |
| `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET` | Optional Spotify playlist parsing. |
| `YTDLP_PO_PROVIDER_URL`, `YTDLP_COOKIE_FILE`, `YTDLP_ALLOW_BROWSER_COOKIES`, `YTDLP_NETRC_FILE` | Optional direct-source helper configuration. |
| `DEMO_MODE` | Marks setup/status responses as demo data when using the synthetic demo library. |

## Beets config

The authoritative Beets config is `/config/config.yaml` in the `beets` engine container. The web manager does not own a second Beets config.

The engine image includes the bundled `discpath` plugin under `/opt/beets-web-manager-agent/beetsplug`; user plugins can be mounted under `/config/beetsplug`. Configure `pluginpath` so `/config/beetsplug` is searched before the bundled path.

`/api/health` is a web-manager liveness check. `/health/ready` and `/api/setup/status` query the remote control agent for Beets readiness and fail closed when the engine is unreachable or rejects authentication.

## Submission and fingerprinting terms

- Fingerprinting: local audio fingerprint generation through Chroma/fpcalc in the Beets engine.
- AcoustID lookup: querying AcoustID for candidate recordings from a fingerprint.
- `submit`: Beets/Chroma command for AcoustID submission readiness. Requires Chroma loaded and the command registered.
- `mbsubmit`: MusicBrainz submission command readiness. It is independent of `submit`.

## Beets engine version

`Dockerfile.beets` selects its upstream LinuxServer Beets base image via the `BEETS_BASE_IMAGE` build argument (`docker-compose.dev.yml`/`docker-compose.full.yml` read it from the `BEETS_BASE_IMAGE` environment variable, default `lscr.io/linuxserver/beets:2.13.1`):

- **An exact version** (e.g. `lscr.io/linuxserver/beets:2.13.1`): the tested, reproducible production default. Update it only through an intentional repository change validated per `docs/BEETS_ENGINE_MIGRATION.md`.
- **A digest pin** (e.g. `lscr.io/linuxserver/beets:2.13.1@sha256:...`): preferred for production once a version has been validated -- fully reproducible, immune to a registry retagging the same version tag.
- **`latest`**: resolves to whatever LinuxServer currently publishes. Useful only for advanced compatibility testing (and the scheduled `beets-engine-latest-compat` CI job, which is informational and never gates a release); never use it as a production default, since it can silently change the running Beets version on a routine rebuild.

The Beets engine version is independent of the Beets Web Manager image version -- upgrading one does not require upgrading the other.

Beets 2.4.0 was previously pinned because of a narrow plugin-resolution defect (`beetbox/beets#6033`), fixed upstream in Beets 2.5.0 (`beetbox/beets#6039`). `docker/beets/apply_patches.py` applies the local backport patch only when the installed Beets version is exactly 2.4.0, skips it (and instead verifies the upstream fix directly) on Beets >= 2.5.0, and fails the build for any other, unsupported version -- it is never applied outside 2.4.0. Beets 2.4.0 remains explicitly supported and tested (see the `beets-engine-verification` CI matrix) for deployments that have not yet migrated. Upgrading the Beets engine version on a deployment with an existing library requires the backup/migration/rollback procedure in `docs/BEETS_ENGINE_MIGRATION.md` -- newer Beets releases can perform an automatic, one-time, non-reversible database schema migration on first open.

# Configuration

Configuration is split between environment variables for the web-manager and control-agent boundary, and `/config/config.yaml` for Beets itself.

## Configuration ownership and precedence

Every setting in this app belongs to exactly one of four owners, each with its own lifecycle:

| Owner | Examples | Lifecycle | Where it lives |
|---|---|---|---|
| **Deployment (Docker/host)** | published port, bind address, UID/GID, bind mounts, service names | requires `docker compose up -d` / container recreation to change | Compose file `environment:`/`ports:`, host filesystem |
| **Web-manager application settings** | selected AI model, non-secret wizard choices | applied immediately, survives restart, does not require recreation | `/web-manager-data/app_settings.json` (`GET`/`POST /api/setup/settings`) |
| **Web-manager secrets** | administrator password hash, Flask/session secret, AI/Plex/AcoustID credentials | applied immediately to the running process; some (session secret) require a restart to rotate | dedicated files under `/web-manager-data/` (see below), never mixed into `app_settings.json` |
| **Beets configuration** | enabled plugins, plugin settings, importer behavior | owned and read directly by the Beets engine | `/config/config.yaml` in the `beets` container |

Precedence for any given web-manager value, highest first:

1. **Explicit Docker environment variable** (set in the Compose file or host environment). This is what an operator controls at deploy time and it always wins.
2. **Persistent web-manager state** written by the app itself (`/web-manager-data/.env`, `.browser_password`, `.browser_username`, `app_settings.json`). This is what changes when you use the Settings/System UI.
3. **Built-in application default.**

Concretely: `_first_config_secret()`/`_security_auth_password()` and friends in `app.py` always check `os.environ` first. `/web-manager-data/.env` (see below) only ever *fills in* a variable that is still blank in the process environment -- it can never override a non-blank Docker-supplied value. This means a value pinned by an operator in `docker-compose.yml` cannot be silently overridden by a Settings-page save, and the System page reports each variable's `source` (`process` = Docker env, `file` = `.env`, `example` = default) so "why is this value X?" always has a concrete answer.

## Web-manager secrets: what's stored where

| File | Contents | Format |
|---|---|---|
| `/web-manager-data/.browser_password` | Administrator password | Werkzeug hash (`scrypt:...`) only, **never plaintext**. Mode `0600`. |
| `/web-manager-data/.browser_username` | Administrator username | Plaintext (not a secret). |
| `/web-manager-data/.browser_setup_state` | `fresh` / `claimed` / `legacy_established` | Canonical source of truth for whether first-run setup has been claimed. |
| `/web-manager-data/.setup_complete` | Presence = setup wizard finished | Empty marker file, created with `O_CREAT|O_EXCL` so only one completion request can ever win, even under concurrent requests. |
| `/web-manager-data/.auth_token` | Auto-generated `BEETS_WEB_AUTH_TOKEN` (only if the operator never set one) | Plaintext token, mode `0600` -- this is a bearer token, not a password, so it must remain reversible. |
| `/web-manager-data/.flask_secret_key` | Flask session-signing secret | Generated once, mode `0600`, never rotated automatically (rotating it invalidates all sessions). |
| `/web-manager-data/.env` | Runtime configuration this app itself has written (AI/Plex/AcoustID keys, etc.) | Plaintext, mode `0600`. `BEETS_WEB_PASSWORD` is deliberately excluded from this file even if posted to `/api/setup/env` -- only its hash goes to `.browser_password`. |

A password hash is not a reusable API secret and is never returned by any API response. A saved API key/token is shown to the browser as `Configured` (masked), never as its literal value, except immediately after `POST /api/setup/auth-token/regenerate`, which returns the newly generated token's plaintext exactly once (by definition -- that is the only way to hand a freshly generated bearer token to its owner) and is never repeated in any later response.

### `/web-manager-data/.env` is not the Compose `.env`

Despite the filename, this file has nothing to do with `docker compose --env-file` or a `.env` sitting next to `docker-compose.yml`. It is application-owned runtime state, loaded by the web-manager process itself, that:

- only supplies a value when the process's actual environment variable is blank (see precedence above);
- is what `POST /api/setup/env` (the System page's environment editor) writes to;
- takes effect for the *currently running* web-manager process without a container recreation;
- never affects the separate `beets` engine container;
- cannot override a non-blank Docker-supplied environment value, by design.

If this ambiguity trips you up, that's expected -- treat "Deployment setting" (Compose/host, needs recreation) and "Web Manager setting" (`.env`, applies live) as the two questions to ask before changing a value, and prefer the System page's UI, which labels each field with which one it is.

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
| `BEETS_WEB_AUTH_TOKEN` | web | yes | Owner API/script bearer token, for scripted/API clients. The app auto-generates a secure token when no credential is set (min length governed by `BEETS_WEB_AUTH_MIN_LENGTH`, default 32). Never used for the browser login form. |
| `BEETS_WEB_PASSWORD` | web | optional | Administrator browser login password (session cookie or HTTP Basic). Passphrase-friendly policy: at least `BEETS_WEB_PASSWORD_MIN_LENGTH` characters (default 16, floor 12), no forced uppercase/lowercase/digit/special composition, just a placeholder-string check (`admin`, `changeme`, ...). Prefer setting this via the first-run browser setup wizard, which hashes it (Werkzeug scrypt) before persisting -- an explicit env var is compared in plaintext at login time since the web manager does not own its lifecycle. |
| `BEETS_WEB_PASSWORD_MIN_LENGTH` | web | optional | Minimum browser password length, default `16`, hard floor `12`. Independent of `BEETS_WEB_AUTH_MIN_LENGTH` (the bearer-token floor) -- the two credentials have different threat models and are validated separately. |
| `BEETS_WEB_USERNAME` | web | optional | Browser login username, default `admin`. |
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

The authoritative Beets config is `/config/config.yaml` in the `beets` engine container. The web manager does not own a second Beets config, and -- since the two-service architecture cutover (2026-07-28) -- has no local mount of this file at all; its own `/config` volume is a separate, unrelated directory for its own app state.

The Settings page's config editor (`GET/POST /api/config`, `POST /api/config/revert`) therefore does not touch a local filesystem path. It proxies through `beets_client` to dedicated control-agent endpoints (`GET/POST /config`, `POST /config/revert` on the agent, BEETSDIR-relative, never a client-supplied path) that read/write the engine's own `config.yaml`. Secret-line redaction (API keys, tokens, passwords) still happens in the web manager, at the boundary before content reaches the browser -- the agent returns raw content across the trusted internal network only. An earlier version of these routes read/wrote a local `/config/config.yaml` path directly, which never existed in the real deployed topology and made the config editor unconditionally return 500 (fixed in v0.1.12; see `docs/TECHNICAL_DEBT.md`).

The engine image includes the bundled `discpath` plugin under `/opt/beets-web-manager-agent/beetsplug`; user plugins can be mounted under `/config/beetsplug`. Configure `pluginpath` so `/config/beetsplug` is searched before the bundled path.

`/api/health` is a web-manager liveness check. `/health/ready` and `/api/setup/status` query the remote control agent for Beets readiness and fail closed when the engine is unreachable or rejects authentication. The control agent's own `/status` (deep plugin/version diagnostics) is single-flight cached for `BEETS_VERSION_CACHE_TTL_SECONDS` (default 30s) and, on a transient probe failure, keeps serving the last known-good plugin list (marked `diagnostics_fresh: false`) rather than reporting a false `0 plugins loaded` -- see v0.1.12's BUG-3 fix in `docs/TECHNICAL_DEBT.md`. Its own `/health` liveness check is a hardcoded, dependency-free response, served by a threaded HTTP server so a slow `/status` probe elsewhere can never block it.

## Authentication and sessions

First-run setup (`POST /api/setup/first-run`) creates the administrator credential and establishes a session in one step; `POST /api/setup/complete` (also session-authenticated at that point) validates Beets connectivity and atomically marks setup done. Both are cross-process-safe: `.setup_complete` is created with `O_CREAT|O_EXCL`, so only one completion request can ever succeed even if this deployment model ever changed from its current single-process Waitress server.

Session cookies are `HttpOnly`, `SameSite=Lax`, and `Secure` when the app detects HTTPS. `remember=false` produces a browser-session cookie (cleared on browser close); `remember=true` produces a persistent cookie with a `BEETS_WEB_SESSION_HOURS`-hour lifetime (default 168 = 7 days). State-changing requests (`POST`/`PUT`/`PATCH`/`DELETE`) are additionally checked for same-origin intent (`Origin`/`Referer`/`Sec-Fetch-Site` plus an `X-Beets-CSRF: 1` header) unless an explicit `Authorization: Bearer`/`Basic` header is present, since a script presenting its own credential is not a browser CSRF target.

## MusicBrainz is core, not a plugin

MusicBrainz autotagging is built into Beets itself -- there is no `musicbrainz` entry in a `plugins:` list to enable or disable, unlike `fetchart`, `mbsync`, `discogs`, etc. `/api/setup/status`'s `integrations.musicbrainz` therefore reports Beets-engine/plugin-loader health (`connected` / `unavailable` / `plugin_loader_failed`), never plugin membership, and every integration entry carries a `category` field (`service` for MusicBrainz/AcoustID/AI/Plex/Lidarr/SLSKD, `beets_plugin` for togglable Beets plugins) so the UI can render them as distinct groups. MusicBrainz lookups, matching, and review never depend on any AI provider credential being configured.

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

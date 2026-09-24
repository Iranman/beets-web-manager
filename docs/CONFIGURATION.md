# Configuration

Configuration is split between environment variables for Beets Web Manager (including how it reaches stock Beets), and `/config/config.yaml` for Beets itself.

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
| `BEETS_WEB_MANAGER_VERSION` | compose | no | Published image tag to deploy: `stable` (recommended default), `latest`, exact version `0.1.18`, or `edge`. |
| `WEBCONTROL_PORT` | web | no | Web port inside the container, default `8337`. |
| `BEETS_WEB_URL` | web | optional | Stock Beets container's `web`/`webmanager` plugin URL. Defaults to `http://beets:8337` (the standard Compose service name/port). For an externally-managed Beets instance, set to its URL (e.g. `http://192.168.1.50:8337`). |
| `BEETS_WEB_AUTH_TOKEN` | web | optional | Owner API/script bearer token. The app auto-generates a secure token if none is set. |
| `BEETS_WEB_PASSWORD` | web | optional | Administrator browser login password. Prefer setting this via the first-run browser setup wizard. |
| `BEETS_WEB_USERNAME` | web | optional | Browser login username, default `admin`. |
| `BEETS_OUTBOUND_ALLOWLIST` | web | optional | Comma-separated host:port or CIDR:port entries for private services the web manager may contact. |
| `BEETS_TRUSTED_PROXIES` | web | optional | Proxy CIDRs whose forwarded client IP headers may be trusted. |

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

The authoritative Beets config is `/config/config.yaml`, owned by the stock `beets` container. Web Manager also mounts `/config` (read/write) — not to run Beets itself, but to provision the bundled `webmanager` integration plugin's files into `/config/beetsplug` and to safely merge required `plugins:`/`pluginpath:` entries into `config.yaml` at startup (additive only: it backs up the file first and never removes an operator's existing settings).

> [!NOTE]
> The Settings page's config text editor (`GET/POST /api/config`) still calls the retired `backend/beets_client.py` control-agent client and is currently non-functional — see `docs/TECHNICAL_DEBT.md` (ARCH-010). Edit `/config/config.yaml` directly on the host, or via `docker compose exec beets sh`, until that route is migrated onto `backend/beets_adapter.py`.

Beets Web Manager provisions the bundled `discpath` plugin (and the `webmanager` plugin itself) into `/config/beetsplug`; further user plugins can be dropped into the same directory. `pluginpath` must include `/config/beetsplug` for any of them to load — the provisioning step ensures this automatically.

`/api/health` is a Web-Manager liveness check. `/health/ready` and `/api/setup/status` call `BeetsAdapter.get_plugin_status()`/`get_stats()` (the `webmanager` plugin's live HTTP handshake) and fail closed when stock Beets is unreachable or rejects authentication.

### Item pagination is not real upstream pagination (known limitation)

`BeetsAdapter.get_items_page()` (used by `GET /api/library/items` and similar paged reads) does not have a real bounded query to page against: stock `beetsplug.web`'s `GET /item/` always returns the entire library, with no `offset`/`limit` support. Web Manager therefore fetches the full item list and slices it in Python. This is deliberately not disguised as real pagination -- the response shape (`items`/`offset`/`limit`/`returned`/`total`) is honest about `total` being the whole library, not an upstream-reported page count.

To avoid re-fetching the whole library on every single page request within one browsing session or workflow, `BeetsAdapter` caches the full item list for a short, bounded TTL (`_ITEMS_PAGE_CACHE_TTL_SECONDS`, 5 seconds). This is a latency/load mitigation only -- it does not make the underlying operation real pagination, and the short TTL is intentional so a concurrent import/modify becomes visible again within a few seconds. Building a custom SQL/pagination endpoint against `musiclibrary.blb` to fix this properly is explicitly out of scope: Beets Web Manager does not become a second owner of the Beets database (see `docs/ARCHITECTURE.md`'s non-negotiable rules). If upstream Beets ever adds real `beetsplug.web` pagination, `get_items_page()` should be updated to use it directly instead of this workaround.

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

## Beets image version

`docker-compose.yml`'s `beets` service image tag selects the stock Beets version:

- **`latest`** (the production default): resolves to whatever LinuxServer currently publishes. This is also the version the `stock-beets-acceptance` CI job in `.github/workflows/docker-build.yml` runs the real Docker acceptance test against.
- **An exact version** (e.g. `lscr.io/linuxserver/beets:2.13.1`): fully reproducible; use this if you need a pinned version instead of floating with `latest`.
- **A digest pin** (e.g. `lscr.io/linuxserver/beets:2.13.1@sha256:...`): fully reproducible and immune to a registry retagging the same version tag.

The Beets image version is independent of the Beets Web Manager image version -- upgrading one does not require upgrading the other.

Beets Web Manager builds and publishes exactly one image, `ghcr.io/iranman/beets-web-manager`. There is no custom Beets image anywhere in this repository or its CI -- the sole Beets runtime this repository verifies against is the unmodified, official `lscr.io/linuxserver/beets` image, exercised over HTTP by the `webmanager` integration plugin (`beetsplug/webmanager/`) in the `stock-beets-acceptance` job. Upgrading the Beets image version on a deployment with an existing library requires the backup/upgrade/rollback procedure in `docs/BEETS_ENGINE_MIGRATION.md` -- newer Beets releases can perform an automatic, one-time, non-reversible database schema migration on first open.

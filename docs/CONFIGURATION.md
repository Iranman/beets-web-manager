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

Concretely: `_first_config_secret()`/`_security_auth_password()` and friends in `backend/auth_service.py` always check `os.environ` first. `/web-manager-data/.env` (see below) only ever *fills in* a variable that is still blank in the process environment -- it can never override a non-blank Docker-supplied value. This means a value pinned by an operator in `docker-compose.yml` cannot be silently overridden by a Settings-page save, and the System page reports each variable's `source` (`process` = Docker env, `file` = `.env`, `example` = default) so "why is this value X?" always has a concrete answer.

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
- is what `POST /api/setup/env` (the System page's environment editor) writes to (`GET /api/setup/env` shows every `*_URL` setting without `user:pass@`; saving that redacted URL back keeps the stored value, so re-saving the same plain URL does not remove credentials from a setting such as `PLEX_URL`: clear the field and save, then enter the URL again, or change the URL; `BEETS_WEB_URL` is the exception, where saving the plain URL replaces the stored one);
- takes effect for the *currently running* web-manager process without a container recreation;
- never affects the separate `beets` engine container;
- cannot override a non-blank Docker-supplied environment value, by design.

If this ambiguity trips you up, that's expected -- treat "Deployment setting" (Compose/host, needs recreation) and "Web Manager setting" (`.env`, applies live) as the two questions to ask before changing a value, and prefer the System page's UI, which labels each field with which one it is.

## Configuration layers

Every variable belongs to exactly one layer (`backend/config_layers.py`). The layer decides where you set it, whether Web Manager may read it, and whether the System page may save it. The System page shows each variable's layer and how a change applies: `live`, `restart` (restart Web Manager) or `deploy` (edit Compose and recreate the container).

### 1. Host (Compose interpolation only)

Values on the Docker host, used only to build the volume list in `docker-compose.yml`. They mean nothing inside a container and Web Manager never reads them (a test enforces this). The System page shows them read-only.

| Variable | Default | Meaning |
|---|---|---|
| `BEETS_CONFIG_PATH` | `./beets` | Host folder mounted at `/config` in both containers. |
| `MUSIC_PATH` | `./music` | Host library folder mounted at `/music` (read-only in Web Manager). The older name `MUSIC_LIBRARY_PATH` is accepted as a fallback. |
| `DOWNLOADS_PATH` | `./downloads` | Host downloads/staging folder mounted at `/downloads`. The older name `DOWNLOAD_PATH` is accepted as a fallback. |
| `WEB_MANAGER_DATA_PATH` | `./web-manager` | Host folder mounted at `/web-manager-data`. The older name `BEETS_WEB_MANAGER_DATA_PATH` is accepted as a fallback. |
| `BEETS_WEB_BIND_ADDRESS` | `127.0.0.1` | Published bind address (`docker-compose.full.yml`, `docker-compose.dev.yml`). |

The shipped Compose files use the literal image `ghcr.io/iranman/beets-web-manager:latest`. To pin a release, replace `latest` with an exact version such as `0.1.49`.

`docker-compose.yml` and `docker-compose.dev.yml` accept the older names through nested defaults, for example `${MUSIC_PATH:-${MUSIC_LIBRARY_PATH:-./music}}`.

### 2. Deployment (Compose `environment:`, pinned)

| Variable | Default | Meaning |
|---|---|---|
| `PUID`, `PGID` | `1000` | User and group the container runs as. |
| `TZ` | `UTC` | Time zone. |
| `WEBCONTROL_PORT` | `8337` | In `ports:` it is the published host port. Inside the container the app always listens on 8337, which the shipped healthcheck expects. |

Every shipped Compose file sets these, so a value saved from the System page could never take effect. They are read-only in the app.

### 3. Container paths (Compose `environment:`)

Absolute paths inside the Web Manager container. They must match the mount targets, and `MUSIC_ROOT` must equal Beets' own `directory:` in the `beets` container (both `/music` by default). Change one only together with the matching volume target, then recreate the container. A relative value is ignored with a warning.

| Variable | Default | Meaning |
|---|---|---|
| `MUSIC_ROOT` | `/music` | Library mount inside Web Manager. Deprecated aliases: `MUSIC_LIBRARY_PATH`, `BEETS_MUSIC_DIR`. |
| `DOWNLOADS_ROOT` | `/downloads` | Downloads/staging mount inside Web Manager. The setup "downloads" check tests this path, and the app uses it as its downloads root. It is also the default for `TORRENT_SOURCE_ROOTS` and `QBIT_REPAIR_ALLOWED_ROOTS`; `PLAYLIST_DOWNLOAD_ROOT` defaults to `DOWNLOADS_ROOT/music/Playlist Downloads`. Because `TORRENT_SOURCE_ROOTS` defaults to it, a folder under it that the app did not create is a preserved torrent source (move imports are refused and imports copy, unless `ALLOW_TORRENT_SOURCE_MOVE=1`), and app-managed download folders under it are eligible for the "already in library" source cleanup. It must not be `/` or overlap `MUSIC_ROOT` (equal, inside or containing it): such a value is left out of every download, import and cleanup allowlist and blocks setup; album downloads then fail with that message instead of creating folders, and import sources are copied, never moved. The same rule drops matching `TORRENT_SOURCE_ROOTS` and `QBIT_REPAIR_ALLOWED_ROOTS` entries. Deprecated alias: `DOWNLOAD_PATH`. |
| `BEETS_CONFIG` | `/config/config.yaml` | Beets `config.yaml` inside the container. The config editor reads and writes only this file. It refuses a relative path or a file outside the Beets config directory (`BEETSDIR`, default `/config`). |
| `WEB_MANAGER_DATA_DIR` | `/web-manager-data` | Web Manager's own durable state. |
| `BEETS_TRANSACTION_DIR` | `/web-manager-data/transactions` | Transaction and audit records. |
| `BEETS_LIBRARY` | | Deprecated. Web Manager does not open the Beets database. This variable will be removed. |

### 4. Application settings and secrets (System page, `/web-manager-data/.env`)

Only these keys may be saved from the System page, and only these keys are loaded from `/web-manager-data/.env` at startup. A non-blank Docker value always wins.

| Variable | Applies | Meaning |
|---|---|---|
| `BEETS_WEB_URL` | restart | Stock Beets `web`/`webmanager` URL. The default is `http://beets:8337` everywhere. `docker-compose.yml` forwards `${BEETS_WEB_URL:-http://beets:8337}`, so set it in the Compose `.env` for an external Beets. Keep `BEETS_OUTBOUND_ALLOWLIST` in step: it must contain the URL's host:port. Do not put a user name or password in it (`user:pass@host`): Web Manager authenticates to the `webmanager` plugin with its bearer key and never sends URL credentials, so such a URL is refused (setup reason `beets_web_url_userinfo`) and no request is made. A `%40` or fullwidth `＠` counts as `@`. |
| `BEETS_WEBMANAGER_API_KEY` | restart | Integration plugin bearer key. Normally read from `/config/.webmanager_api_key`. Set it only when Web Manager does not mount the Beets `/config` (external Beets). |
| `BEETS_WEB_AUTH_TOKEN` | live | Owner API/script bearer token. Auto-generated if unset. |
| `BEETS_WEB_PASSWORD` | live | Administrator password, stored only as a hash. Prefer the first-run wizard. |
| `BEETS_WEB_USERNAME` | live | Browser login username, default `admin`. |
| `BEETS_OUTBOUND_ALLOWLIST` | live | Comma-separated `host:port`, `IP:port`, `CIDR:port` or `[IPv6]:port` / `[IPv6-CIDR]:port` entries (for example `beets:8337,lidarr:8686,192.168.1.10:32400,10.0.0.0/24:8080,[fd00::5]:5030`) for non-public services the web manager may contact. Any address that is not globally routable needs an entry, including private LAN ranges, CGNAT/Tailscale `100.64.0.0/10`, and IPv6 forms that embed such an IPv4 address. A malformed entry is reported in the log at startup, and every request to an operator-configured service is refused until it is fixed. Applies only to operator-configured services (Beets, Plex, Lidarr, slskd, qBittorrent, the yt-dlp PO provider). `docker-compose.full.yml` defaults it to `beets:8337,bgutil-provider:4416` so its bundled PO provider is reachable; keep that entry if you override the list. It never applies to URLs a user pastes or a provider response supplies (reference URLs, artwork image URLs): those are fetched from public internet addresses only, with the connection pinned to the validated address, and they do not use `HTTP_PROXY`/`HTTPS_PROXY`. |
| `BEETS_OUTBOUND_TOTAL_TIMEOUT_SECONDS` | restart | Wall-clock limit (default 60 s) for one fetch of a user-supplied or provider-supplied URL (reference URLs, artwork images). It covers trying each DNS answer, every redirect, and reading the whole body, so a server that sends data very slowly cannot hold a worker open. A value that is not a positive number is ignored with a warning in the log, and 60 s is used. |
| `BEETS_TRUSTED_PROXIES` | restart | Comma-separated proxy CIDRs whose forwarded client IP headers may be trusted. `X-Forwarded-For` is read right to left: entries added by trusted proxies are skipped and the first untrusted address is the client, so a client cannot spoof its address by sending its own `X-Forwarded-For`. Leave empty when the app is not behind a reverse proxy. |
| `BEETS_AUTH_RATE_LIMIT` / `BEETS_AUTH_RATE_WINDOW` | live | Failed sign-in attempts allowed per client IP per window (default 30 per 60 s). While a client is over the limit every attempt, including one with the correct password, gets HTTP 429 without the password being checked. |
| `BEETS_AUTH_ACCOUNT_RATE_LIMIT` / `BEETS_AUTH_ACCOUNT_RATE_WINDOW` | live | Failed password attempts allowed across all client IPs per window (default 100 per 300 s), so rotating IPs does not bypass the per-IP limit. Existing signed-in sessions and bearer-token clients are not affected while it is exhausted. |

The provider settings in the next section are application settings too.

### Saved-settings migration (upgrading from v0.1.49 or earlier)

Earlier releases copied the whole `.env.example` template into `/web-manager-data/.env` on the first Settings save. That included host-side values such as `DOWNLOADS_PATH=./downloads` and `BEETS_CONFIG_PATH=./beets`, which Web Manager then exported into its own environment. The result was a false "Cannot write to downloads/staging path downloads" warning, and a Beets config editor that showed an empty file and saved to the wrong place.

On startup Web Manager now removes host, deployment, container and retired keys from `/web-manager-data/.env`. It does this once, after saving a `.env.bak-migration-<timestamp>` backup next to the file (mode 0600). Only key names are logged. The System page and `/api/setup/status` (`settings_migration`) report how many keys were removed. Nothing else changes, and later starts do nothing.

Keys Web Manager does not recognise (for example a variable from a newer or older release, or a typo) are kept in `/web-manager-data/.env` but are not loaded into the environment. The log lists them at startup as "Ignored non-application key(s)". Remove them from the file if you no longer need them.

Files in `/web-manager-data` that can hold secrets are created at mode 0600: `.env`, each `.env.bak-<timestamp>` backup written when settings are saved, the `.env.bak-migration-<timestamp>` backup, and the `.env.migration.json` report (key names only). If the filesystem rejects the mode change, Web Manager logs a warning. API responses give the backup file name only, not its full path.

## Optional integrations

| Variable | Purpose |
|---|---|
| `OPENAI_API_KEY`, `OPENROUTER_API_KEY`, `AI_API_KEY`, `AI_BASE_URL`, `AI_MODEL` | Optional AI-assisted candidate ranking. Matching still uses MusicBrainz and AcoustID evidence without AI. |
| `ACOUSTID_API_KEY`, `ACOUSTID_KEY` | Your AcoustID **application** key (register one at https://acoustid.org/new-application); `ACOUSTID_KEY` is a legacy alias read only when `ACOUSTID_API_KEY` is empty. Required for fingerprint lookups: there is no built-in fallback key. Without it, lookups report `not_configured` (fingerprint evidence unavailable, never "no match", never cached) and the setup status says "AcoustID not configured". Fingerprinting also requires Chroma/fpcalc in the Beets engine. |
| `DISCOGS_TOKEN`, `DISCOGS_USER_TOKEN` | Optional Discogs metadata. |
| `LISTENBRAINZ_TOKEN` | Optional ListenBrainz integration. |
| `PLEX_URL`, `PLEX_TOKEN` | Optional Plex sync and refresh. Use deployment-specific URLs, not committed private LAN defaults. |
| `PLEX_MUSIC_ROOTS`, `PLAYLIST_PATH_ROOT_ALIASES` | `PLEX_MUSIC_ROOTS`: comma-separated library paths as Plex sees them (Plex path mapping). `PLAYLIST_PATH_ROOT_ALIASES`: extra library roots that playlist and Plex track paths may start with; falls back to `PLEX_MUSIC_ROOT`. Defaults to `MUSIC_ROOT`; no other path is assumed. |
| `LIDARR_URL`, `LIDARR_API_KEY` | Optional wanted-album integration. |
| `SLSKD_URL`, `SLSKD_API_KEY`, `SLSKD_API_KEY_FILE`, `SLSKD_SLSK_USERNAME`, `SLSKD_SLSK_PASSWORD` | Optional SLSKD/Soulseek acquisition. |
| `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET` | Optional Spotify playlist parsing. |
| `YTDLP_PO_PROVIDER_URL`, `YTDLP_COOKIE_FILE`, `YTDLP_ALLOW_BROWSER_COOKIES`, `YTDLP_NETRC_FILE` | Optional direct-source helper configuration. |
| `DEMO_MODE` | Marks setup/status responses as demo data when using the synthetic demo library. |

## Beets config

The authoritative Beets config is `/config/config.yaml`, owned by the stock `beets` container. Web Manager also mounts `/config` (read/write) — not to run Beets itself, but to provision the bundled `webmanager` integration plugin's files into `/config/beetsplug` and to safely merge the required `plugins:`/`pluginpath:`/`web.include_paths` entries into `config.yaml` at startup (additive only: it backs up the file first and never removes an operator's existing settings; see below).

The Settings page's config text editor (`GET/POST /api/config`, `POST /api/config/revert`) edits the file named by `BEETS_CONFIG` through `backend/config_manager.py`. Every save checks the revision, validates the YAML, makes a backup and writes atomically. The editor fails closed rather than showing an empty config: it returns an error when the file is missing or the path is outside the Beets config directory. Restart the `beets` container so Beets reads a change.

Beets Web Manager provisions the bundled `discpath` plugin (and the `webmanager` plugin itself) into `/config/beetsplug`; further user plugins can be dropped into the same directory. `pluginpath` must include `/config/beetsplug` for any of them to load — the provisioning step ensures this automatically.

### What Web Manager changes in `config.yaml`, and what it only offers

At startup Web Manager adds only what its own transport needs:

- `web` and `webmanager` in `plugins:`;
- `/config/beetsplug` in `pluginpath:`;
- `include_paths: yes` in the `web:` block, when the key is not set at all.

It never removes a plugin, never adds feature plugins to an existing config, never rewrites an existing settings block (for example your `replaygain:` backend), never flips an explicit `include_paths: no`, and never adds `/app/beetsplug`. A fresh install with no `config.yaml` is created from `config.yaml.example`.

Two further edits are available only as explicit actions. When they change the file, each takes a timestamped `config.yaml.bak-<YYYYmmdd-HHMMSS>` backup (mode 600) next to it, writes atomically, keeps your comments, and responds with `restart_required: true`. Restart the `beets` container afterwards. Both POST routes require the CSRF token.

- **Enable `web.include_paths`.** `POST /api/setup/beets-config/include-paths`. Use this when setup warns `beets_web_include_paths_disabled` (your config has `include_paths: no`). Without paths, path-based operations fail with `BEETS_PATHS_UNAVAILABLE` (HTTP 503) instead of treating the library as empty. A flow-style `web: {...}` mapping is refused with HTTP 409; edit that file by hand.
- **Recommended plugins.** `GET /api/setup/plugins/recommended` writes nothing. It returns the recommended set (`fetchart`, `embedart`, `scrub`, `zero`, `ftintitle`, `fromfilename`, `mbsync`, `mbsubmit`, `chroma`, `replaygain`, `lastgenre`, `discpath`), which of them are `configured` and `missing`, and a unified `diff` of the change. `POST /api/setup/plugins/recommended/apply` with `{"plugins": ["fetchart", ...]}` adds only the named plugins. An unknown name or an empty list is rejected with HTTP 400 and nothing is written; a missing `config.yaml` returns HTTP 409. A newly added `replaygain` gets `auto: no` and `backend: ffmpeg`; an existing `replaygain:` block is left as it is.

### `webmanager` plugin roots and what the plugin reports

From plugin 1.6.0, `/webmanager/status` reports Beets' own view of the library:

- `library_directory` (Beets' `directory:`) and `library_path` (Beets' `library:`);
- the effective `allowed_roots` and `import_roots`;
- `web_include_paths`;
- whether `fpcalc` and `ffmpeg` are on the Beets container's `PATH` (`fpcalc_available`, `ffmpeg_available`).

The roots are configured in `config.yaml`:

- `webmanager.import_roots` (default `[/downloads]`): directories the plugin accepts imports from.
- `webmanager.allowed_roots`: directories the plugin may move or remove files in. When unset or empty, it is derived from Beets: the library `directory:` plus `import_roots`. A derived root that is `/`, the Beets config directory, one of its parents or a directory inside it is skipped (logged once in the Beets log); if `directory:` itself is skipped, the plugin falls back to its default roots. Set it only if you need something different. The `BEETS_ALLOWED_ROOTS` environment variable on the `beets` container (comma-separated) overrides it.

Setup compares these with Web Manager's own mounts. It changes nothing, but it warns when:

- `music_root_mismatch`: Beets' `directory:` is not Web Manager's `MUSIC_ROOT`. Both containers must see the library at the same container path.
- `downloads_root_not_import_root`: `DOWNLOADS_ROOT` is not inside any `webmanager.import_roots`, so the plugin would reject imports from it.
- `beets_restart_required`: Beets is running an older `webmanager` plugin than the one Web Manager provisioned. Setup status also sets `restart_required`.

With a plugin older than 1.6.0 these fields are reported as `unknown` and the checks are skipped. Restart Beets to load the provisioned plugin.

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

## Library location and unattended duplicate deletion

- `MUSIC_ROOT` — where the Beets library is mounted inside the Web Manager container (default `/music`). Every shipped compose file mounts this path and now also sets the variable explicitly. It is the only setting for the library location. The setup and System page "Music Library" check tests this path inside the Web Manager container. It blocks setup only when the path is missing or unreadable; a read-only mount is fine. Web Manager uses the absolute item paths Beets reports, so `MUSIC_ROOT` must be the same path as Beets' `directory:` in the `beets` container. It is a container-side variable (layer 3 above): change it under the `beets-web-manager` service's `environment:` together with the volume target.
- Unattended (scheduled) duplicate deletion is a separate, explicit authorization, **off by default**. It is stored in `web-manager-data/duplicate_cleanup_authorization.json` and changed only through `POST /api/dedup/unattended-cleanup` (enabling requires the confirmation phrase `ENABLE UNATTENDED DUPLICATE DELETION`) or the Duplicate Files panel. Changing `MUSIC_ROOT` or any other setting never enables it. While it is off, the maintenance duplicate step still scans, fingerprint-verifies and records a review proposal (both paths, sizes, Recording IDs, fingerprint evidence, release slot, which copy is kept) but deletes nothing. `POST /api/dedup/maintenance-run` runs that step on its own.

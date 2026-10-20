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
| `PUID`, `PGID` | `1000` | User and group the container runs as. With the hardened Compose files, changing them needs a one-time `chown` of the data folder; see [Troubleshooting §11](TROUBLESHOOTING.md#11-web-manager-stops-at-startup-with-cannot-take-ownership-of-web-manager-data-after-a-puidpgid-change). |
| `TZ` | `UTC` | Time zone. |
| `WEBCONTROL_PORT` | `8337` | In `ports:` it is the published host port. Inside the container the app always listens on 8337, which the shipped healthcheck expects. |

Every shipped Compose file sets these, so a value saved from the System page could never take effect. They are read-only in the app.

### 3. Container paths (Compose `environment:`)

Absolute paths inside the Web Manager container. They must match the mount targets, and `MUSIC_ROOT` must equal Beets' own `directory:` in `config.yaml` (both `/music` by default; with two containers, the same path in both). Change one only together with the matching volume target, then recreate the container. A relative value is ignored with a warning.

| Variable | Default | Meaning |
|---|---|---|
| `MUSIC_ROOT` | `/music` | Library mount inside Web Manager. Deprecated aliases: `MUSIC_LIBRARY_PATH`, `BEETS_MUSIC_DIR`. |
| `DOWNLOADS_ROOT` | `/downloads` | Downloads/staging mount inside Web Manager. The setup "downloads" check tests this path, and the app uses it as its downloads root. It is also the default for `TORRENT_SOURCE_ROOTS` and `QBIT_REPAIR_ALLOWED_ROOTS`; `PLAYLIST_DOWNLOAD_ROOT` defaults to `DOWNLOADS_ROOT/music/Playlist Downloads`, which is also used when it is empty or relative. Because `TORRENT_SOURCE_ROOTS` defaults to it, a folder under it that the app did not create is a preserved torrent source (move imports are refused and imports copy; album rename and "move to library" hard link its tracks and cover into the library, or copy them when `/downloads` and `/music` are different filesystems or separate mounts, and never move them; a later tag write first gives the library file its own copy; the rule compares Beets' own paths, so mount the downloads folder at the same path in Web Manager and Beets; rejected downloads stay in place; all unless `ALLOW_TORRENT_SOURCE_MOVE=1`), and app-managed download folders under it are eligible for the "already in library" source cleanup. It must not be `/` or overlap `MUSIC_ROOT` (equal, inside or containing it): such a value is left out of every download, import and cleanup allowlist and blocks setup; album downloads then fail with that message instead of creating folders, and import sources are copied, never moved. The same rule drops matching `TORRENT_SOURCE_ROOTS` and `QBIT_REPAIR_ALLOWED_ROOTS` entries and an unsafe `PLAYLIST_DOWNLOAD_ROOT`. yt-dlp reads its output folder as a template, so a `DOWNLOADS_ROOT` or `PLAYLIST_DOWNLOAD_ROOT` that contains `$` makes yt-dlp downloads fail ("yt-dlp output folder must be an absolute path without '$'") instead of writing elsewhere. Deprecated alias: `DOWNLOAD_PATH`. |
| `BEETS_CONFIG` | `/config/config.yaml` | Beets `config.yaml` inside the container. The config editor reads and writes only this file. It refuses a relative path or a file outside the Beets config directory (`BEETSDIR`, default `/config`). |
| `WEB_MANAGER_DATA_DIR` | `/web-manager-data` | Web Manager's own durable state. Playlist job checkpoints go to `PLAYLIST_JOB_STATE_DIR` (default `WEB_MANAGER_DATA_DIR/playlists/jobs`, which is also used when it is empty or relative). Keep it on the data volume, or interrupted playlist jobs cannot resume after the container is recreated. |
| `BEETS_TRANSACTION_DIR` | `/web-manager-data/transactions` | Transaction and audit records. |
| `BEETS_LIBRARY` | | Deprecated. Web Manager does not open the Beets database. This variable will be removed. |

### Operator-only settings (Compose `environment:` only)

These run commands, name files the app reads or moves files into, or control a security boundary, so a web session must never be able to change them. Set them in the Web Manager service's `environment:` block (or the Compose `.env` when the Compose file forwards them) and recreate the container. The System page shows them read-only, and the Settings API refuses to save them. A value an earlier release saved to `/web-manager-data/.env` is removed by the startup settings migration (only the key name is logged; a `.env.bak-migration-*` backup is kept).

| Variable | Meaning |
|---|---|
| `BEETS_OUTBOUND_ALLOWLIST` | Extra non-public endpoints the operator path may contact, as comma-separated `host:port`, `IP:port`, `CIDR:port` or `[IPv6]:port` / `[IPv6-CIDR]:port` entries (for example `beets:8337,192.168.1.10:8080,10.0.0.0/24:8080,[fd00::5]:5030`). Default `127.0.0.1:8337,localhost:8337,beets:8337`; the shipped Compose files forward `${BEETS_OUTBOUND_ALLOWLIST:-beets:8337}` (`docker-compose.full.yml`: `beets:8337,bgutil-provider:4416`). You normally do **not** need an entry for Plex, Lidarr, slskd, qBittorrent, the yt-dlp PO provider or the AI endpoint: see *Configured service URLs are trusted* below. Every entry still requires every resolved address to be private (10/8, 172.16/12, 192.168/16), CGNAT (`100.64.0.0/10`), IPv6 ULA (`fc00::/7`) or loopback, so no entry can admit cloud-metadata (`169.254.169.254`, `fd00:ec2::254`, `100.100.100.200`), link-local, unspecified, multicast or reserved addresses. A CIDR entry wider than those ranges (`0.0.0.0/0`, `::/0`, `128.0.0.0/1`, ...) is ignored and logged at startup. A malformed entry is logged at startup, and every operator-path request is refused until it is fixed. It never applies to URLs a user pastes or a provider response supplies (reference URLs, artwork image URLs): those are fetched from public internet addresses only, with the connection pinned to the validated address. |
| `BEETS_ENGINE` | `embedded` is the recommended setting and will become the default. `http` (still the default until then, and the rollback path) talks to a separate stock Beets container at `BEETS_WEB_URL`. `embedded` runs Beets 2.14.1 inside Web Manager ([ADR 0006](adr/0006-embedded-beets.md)): it reads Beets' `config.yaml` from `BEETSDIR`, and opens the `library` and `directory` named there, with the same database and paths as the Beets container. Web Manager then needs the music folder mounted read-write, and the Beets container must be stopped first (one writer). It never rewrites `config.yaml`. A startup self-test checks the Beets version, the config, the library, the music folder being writable, the plugins loading, and `fpcalc`/`ffmpeg`. `/api/health` lists the failing check codes, and the authenticated `/api/health/detail` explains each one (codes and fixes: `docs/TROUBLESHOOTING.md`, *Embedded Beets self-test codes*). While an error-level check fails, reads work and library changes are refused. `BEETS_WEB_URL`, `BEETS_WEBMANAGER_API_KEY` and the key file are not used in this mode. |
| `BEETS_TRUSTED_PROXIES` | Comma-separated proxy CIDRs whose forwarded client IP headers may be trusted. `X-Forwarded-For` is read right to left: entries added by trusted proxies are skipped and the first untrusted address is the client, so a client cannot spoof its address by sending its own `X-Forwarded-For`. Leave empty when the app is not behind a reverse proxy. |
| `BEETS_AUTH_RATE_LIMIT` / `BEETS_AUTH_RATE_WINDOW` | Failed sign-in attempts allowed per client IP per window (default 30 per 60 s). While a client is over the limit every attempt, including one with the correct password, gets HTTP 429 without the password being checked. |
| `BEETS_AUTH_ACCOUNT_RATE_LIMIT` / `BEETS_AUTH_ACCOUNT_RATE_WINDOW` | Failed password attempts allowed across all client IPs per window (default 100 per 300 s), so rotating IPs does not bypass the per-IP limit. Existing signed-in sessions and bearer-token clients are not affected while it is exhausted. |
| `BEETS_WEB_AUTH_DISABLED` | `1` turns authentication off. Only for isolated local development. |
| `BEETS_WEB_SESSION_COOKIE_SECURE` | Marks the session cookie `Secure`. |
| `SPOTIFLAC_CMD` | Command line used to run SpotiFLAC (split with shell rules and executed). |
| `SPOTIFLAC_SERVICES` | SpotiFLAC `--service` list in priority order (default `tidal,qobuz,deezer,amazon,soundcloud`). Only SpotiFLAC's own service names (`tidal`, `qobuz`, `deezer`, `amazon`, `soundcloud`, `apple`, `youtube`, `youtube-music`, `gdstudio`, `pandora`) and `ext:<extension id>` are accepted, on save and again before each run, so the value can never add other SpotiFLAC options. After a SpotiFLAC run, a symlink it left in the download folder is removed (the link only) and files reached through one are not counted or imported. |
| `YTDLP_JS_RUNTIMES` | JavaScript runtimes for yt-dlp's YouTube challenge solving, in order (default `deno,node,quickjs`); `name:/path` names an executable. The image ships Deno at `/usr/local/bin/deno`, so the default works without changes. |
| `YTDLP_REMOTE_COMPONENTS` | Comma-separated yt-dlp remote components (for example `ejs:github`) that yt-dlp may download and run for challenge solving. Leave it empty (the default): the image bundles the solver scripts (`yt-dlp-ejs`), so nothing is fetched at runtime. |
| `YTDLP_COOKIE_FILE`, `YTDLP_NETRC_FILE`, `YTDLP_ALLOW_BROWSER_COOKIES` | Credential files and browser cookie stores yt-dlp may read. |
| `SLSKD_API_KEY_FILE` | File whose first line is sent to `SLSKD_URL` as the slskd API key. |
| `IMPORT_REVIEW_QUARANTINE_DIR`, `MUSIC_FORMAT_QUARANTINE_DIR` | Directories that rejected or quarantined files are moved into. |

#### Configured service URLs are trusted

Each operator-hosted service trusts exactly the scheme, host and port of its own configured URL, only for its own calls, and only when every address that URL resolves to is private (RFC 1918), CGNAT (`100.64.0.0/10`) or IPv6 ULA (`fc00::/7`):

| Service | Setting |
|---|---|
| Plex | `PLEX_URL` (or the Beets `config.yaml` `plex:` stanza, as Plex itself resolves it) |
| Lidarr | `LIDARR_URL` (or `LIDARR_URL_FILE`) |
| slskd | `SLSKD_URL` |
| qBittorrent | `QBITTORRENT_URL` / `QBIT_URL` / `QB_URL` (or `QBITTORRENT_URL_FILE`) |
| yt-dlp PO token provider | `YTDLP_PO_PROVIDER_URL` |
| AI provider | `AI_BASE_URL` |

So `PLEX_URL=http://192.168.1.20:32400` lets Plex calls reach that address without an allowlist entry, and a Docker service name such as the default `SLSKD_URL=http://slskd:5030` works because it resolves to a Docker network (private) address. Loopback (`127.0.0.0/8`, `::1`) and cloud-metadata addresses are never trusted this way; a configured URL that resolves to a public address follows the normal public-internet rules. The trust does not extend to another service, port or scheme, nor to a URL typed into a setup connectivity test that differs from the configured one. A request it admits may not be redirected to another origin.

Every operator-path connection is pinned: the host is resolved once, every answer is checked, and the socket connects to a checked address (TLS still verifies the certificate for the hostname), so a DNS change between the check and the connection cannot redirect it. Each redirect hop is checked and pinned the same way. The operator path does not use `HTTP_PROXY`/`HTTPS_PROXY`.

When a request is refused, the error and the provider status name the entry that would admit it, for example `add 192.168.1.20:32400 to BEETS_OUTBOUND_ALLOWLIST or set PLEX_URL to it`.

### 4. Application settings and secrets (System page, `/web-manager-data/.env`)

Only these keys may be saved from the System page, and only these keys are loaded from `/web-manager-data/.env` at startup. A non-blank Docker value always wins, so the Settings API refuses to save a key the Docker/Compose environment set at startup (it would apply until the next restart and then silently revert); change it in Compose instead.

| Variable | Applies | Meaning |
|---|---|---|
| `BEETS_WEB_URL` | restart | Stock Beets `web`/`webmanager` URL. The default is `http://beets:8337` everywhere. `docker-compose.yml` forwards `${BEETS_WEB_URL:-http://beets:8337}`, so set it in the Compose `.env` for an external Beets. Keep `BEETS_OUTBOUND_ALLOWLIST` in step: it must contain the URL's host:port. Do not put a user name or password in it (`user:pass@host`): Web Manager authenticates to the `webmanager` plugin with its bearer key and never sends URL credentials, so such a URL is refused (setup reason `beets_web_url_userinfo`) and no request is made. A `%40` or fullwidth `＠` counts as `@`. |
| `BEETS_WEBMANAGER_API_KEY` | restart | Integration plugin bearer key. Normally read from `/config/.webmanager_api_key`. Set it only when Web Manager does not mount the Beets `/config` (external Beets). |
| `BEETS_WEB_AUTH_TOKEN` | live | Owner API/script bearer token. Auto-generated if unset. |
| `BEETS_WEB_PASSWORD` | live | Administrator password, stored only as a hash. Prefer the first-run wizard. |
| `BEETS_WEB_USERNAME` | live | Browser login username, default `admin`. |
| `BEETS_OUTBOUND_TOTAL_TIMEOUT_SECONDS` | restart | Wall-clock limit (default 60 s) for one fetch of a user-supplied or provider-supplied URL (reference URLs, artwork images). It covers trying each DNS answer, every redirect, and reading the whole body, so a server that sends data very slowly cannot hold a worker open. A value that is not a positive number is ignored with a warning in the log, and 60 s is used. |

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
| `PLAYLIST_AUTO_SYNC`, `PLAYLIST_AUTO_SYNC_INTERVAL` | Automatic playlist sync with Plex (default on; `0`/`false`/`no`/`off` turns it off) every `PLAYLIST_AUTO_SYNC_INTERVAL` seconds (default `300`, minimum `60`; the first run is about 30 s after start). Each run is a tracked job on the Jobs page ("Playlist sync (automatic)"); it never overlaps a manual sync (a manual start while one runs answers 409 `job_already_running`) and skips a playlist whose download/import pipeline is running. Errors show in `GET /api/playlists/sync/status` (`last_error`, `last_job_id`, `next_run_at`). The Web Manager list is the master copy: the Plex playlist is updated in place (entries added, removed and moved, so its Plex ID and link stay the same); tracks added in Plex or in the playlist's `.m3u` are merged into the list; a track leaves Plex only when it was removed in Web Manager. A sync with no changes makes no Plex writes. A track listed twice stays twice in the Web Manager list and its `.m3u`; Plex keeps one entry per track, so the repeat is satisfied by that one entry and is not re-sent. The number of tracks added is read back from Plex after the write. Tracks that cannot be placed are listed as pending with a reason (not in the Beets library, or not indexed by Plex yet). Playlists that exist only in Plex are not adopted. Local playlist files are imported once, read-only, when the Playlists page loads or a sync runs, and only when Web Manager has no list with that name: `*.m3u` and `*.playlist.json` in `MUSIC_ROOT/playlists` (older versions and the Beets smartplaylist folder; never written to; a symlinked `playlists` folder, symlinked or non-regular files, files over 5 MB, and files beyond the first 500 per folder are skipped), and `*.m3u` files you place in `WEB_MANAGER_DATA_DIR/playlists` (Web Manager's own exports there, named `<id>_<hash>.m3u`, are not imported). The list name is the file name without `.m3u`; `#EXTINF` "Artist - Title" labels give the tracks, and files in the library are matched by path. Parsing with source `local_m3u` also finds such a file before it is imported. Imports are recorded in `legacy_imports.json` in the playlist state folder, so a deleted list is not imported again. |
| `PLEX_API_TIMEOUT` | Seconds (default `20`, minimum `5`) for one Plex API request. It is also the total time a playlist sync spends on per-track title searches when the Plex index is unavailable; tracks not searched in time stay pending. |
| `PLEX_SYNC_MAX_FALLBACK_SEARCHES` | Most per-track Plex title searches (default `10`, `0` turns them off) one playlist sync makes when the Plex index is unavailable. |
| `PLEX_INDEX_TIMEOUT` | Total seconds (default `90`, minimum `20`) for reading the Plex music index during a playlist sync, across all pages. Past it, the sync reports a Plex timeout instead of waiting. |
| `PLEX_MUSIC_ROOTS`, `PLAYLIST_PATH_ROOT_ALIASES` | `PLEX_MUSIC_ROOTS`: comma-separated library paths as Plex sees them (Plex path mapping); it can also be set as `plex_music_roots` in the `plex:` section of the Beets `config.yaml`. `PLAYLIST_PATH_ROOT_ALIASES`: comma-separated library roots that playlist and Plex track paths may start with; falls back to `PLEX_MUSIC_ROOT`, then to `MUSIC_ROOT`. Plex path mapping also always tries `MUSIC_ROOT` and `/music`. Neither variable is an application setting: the System page cannot save them, they are not loaded from `/web-manager-data/.env`, and the shipped Compose files do not pass them through, so set them under the Web Manager service's `environment:` in Compose and recreate the container. |
| `LIDARR_URL`, `LIDARR_API_KEY` | Optional wanted-album integration. The wanted list is read in pages of 1000 until Lidarr's `totalRecords` is covered, with a 60 s timeout per page and one retry after a timeout or 5xx. If any page fails, no list is shown, only the reason; a partial list is never shown as complete. |
| `SLSKD_URL`, `SLSKD_API_KEY`, `SLSKD_API_KEY_FILE`, `SLSKD_SLSK_USERNAME`, `SLSKD_SLSK_PASSWORD` | Optional SLSKD/Soulseek acquisition. Without an API key, an slskd download that allows a direct-source fallback skips slskd and goes straight to the fallback (the job log says why). One that does not is refused with `slskd is not configured (SLSKD_API_KEY)`. The acquisition queue reports `download_sources.slskd_configured` and each fallback's availability. When slskd fails, its reason is logged and appears first in the job's final error. |
| `SLSKD_FALLBACK_METHODS` | Direct sources tried, in order, after slskd fails (default `ytdlp,soundcloud`). SpotiFLAC is not in the default and is used only when it is listed here and its CLI is installed; a download request cannot add or reorder sources (`SPOTIFLAC_CMD`). `ytdlp` is always in the chain: a list without it gets it added before `soundcloud`, or at the end (`spotiflac,soundcloud` runs as `spotiflac,ytdlp,soundcloud`). A source that cannot run here (yt-dlp not installed, no JavaScript runtime for YouTube, no SpotiFLAC CLI) is skipped once per job, with the reason in the log. |
| `PLAYLIST_DOWNLOAD_METHODS` | Sources a playlist download tries for missing tracks, in order, when the Playlists page method is **Auto** (default `slskd,ytdlp,soundcloud`; applies after a restart). SpotiFLAC is not in the default and is used only when it is listed here (`slskd,spotiflac,ytdlp,soundcloud`) or chosen as the method for one download, and its CLI is installed (`SPOTIFLAC_CMD`). |
| `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET` | Optional Spotify playlist parsing. |
| `YTDLP_PO_PROVIDER_URL` | Optional bgutil PO token provider for YouTube, blank by default (`docker-compose.full.yml` runs the sidecar and sets it). It is used only when the outbound policy admits it (its own configured private URL is trusted) and it answers. Otherwise YouTube downloads go ahead without PO tokens, and a warning is logged. |
| `YTDLP_COOKIE_FILE`, `YTDLP_ALLOW_BROWSER_COOKIES`, `YTDLP_NETRC_FILE` | Optional direct-source helper configuration. |
| `DEMO_MODE` | Marks setup/status responses as demo data when using the synthetic demo library. |

## Beets config

The authoritative Beets config is `/config/config.yaml` (`BEETSDIR=/config`).

**Embedded Beets (`BEETS_ENGINE=embedded`).** Beets runs inside Web Manager and reads this file as it is: `library:` and `directory:` name the database and the music folder, `plugins:` the plugins to load. Web Manager adds `web` and `webmanager` to the plugin list and turns on `web.include_paths` in memory only; it never writes the file at startup and never provisions `/config/beetsplug`. The image's own `webmanager` and `discpath` plugins are always used. Everything `config.yaml` makes Beets run (plugins, `pluginpath` modules, `hook` commands, plugin commands such as `convert`) runs in the Web Manager container, so the browser may change only an allowlist of settings: `import` (except `import.log` and `import.set_fields`), `match`, `paths`, `art_filename`, `ui`, `ignore`, the format/sort/naming scalars, `acoustid`, and the sections of the plugins it may add. It may add only `musicbrainz`, `fetchart`, `embedart`, `scrub`, `zero`, `ftintitle`, `fromfilename`, `mbsync`, `chroma`, `lastgenre`, `discogs`, `deezer`, `spotify`, `listenbrainz`, `lyrics`, `parentwork` and `discpath`, none of which runs a command or a user-written expression taken from `config.yaml`. Two of them still reach outside Beets: `chroma` runs the image's fixed `fpcalc` helper on each file it fingerprints, and `fetchart`'s `cover_art_url` source fetches the URL stored in a file's `cover_art_url` tag, which does not go through Web Manager's outbound policy. Removing a plugin is always allowed. File-path and host keys inside those sections stay locked (`import.log`, `import.set_fields` (it can set any field, including a file's path), `musicbrainz.host`/`https`, `lyrics.lrcmux`, `lastgenre.whitelist`/`canonical`, `fetchart.fallback`, `discogs.tokenfile`, `spotify.tokenfile`). `replace` is locked too: its rules (by default, `/` and a leading `.` become `_`) are what keep `paths` and `art_filename` inside `directory`, so path templates stay editable only while `replace` is not. A save that adds a YAML alias (`*name`) the file did not already have is refused; aliases already in the file still load. Any other change (`pluginpath`, `include`, `hook`, `inline` fields, plugin command options such as `convert` or `replaygain`, `replace`, `directory`, `library`, ...) is refused with `CONFIG_CODE_EXECUTION_EDIT_REFUSED` (403). The rule applies to every Web Manager writer: the config editor's save and revert, Setup's recommended-plugins and `web.include_paths` fixes, and plugin provisioning (always refused in embedded mode). Settings that are already in the file and are not changed still load. Make any other edit in the file on the server. Restart Web Manager so Beets reads a change. Plugin network traffic (MusicBrainz, AcoustID, cover art) leaves from the Web Manager container through Beets' own clients, not through Web Manager's outbound policy. The rest of this section describes the two-container setup unless it says otherwise; the config editor works the same in both.

**Two containers (`BEETS_ENGINE=http`).** The file is owned by the stock `beets` container. Web Manager also mounts `/config` (read/write) — not to run Beets itself, but to provision the bundled `webmanager` integration plugin's files into `/config/beetsplug` and to safely merge the required `plugins:`/`pluginpath:`/`web.include_paths` entries into `config.yaml` at startup (additive only: it backs up the file first and never removes an operator's existing settings; see below).

The Settings page's config text editor (`GET/POST /api/config`, `POST /api/config/revert`) edits the file named by `BEETS_CONFIG` through `backend/config_manager.py`. Every save checks the revision, validates the YAML, makes a backup and writes atomically, all under one lock shared with Web Manager's own plugin edits to `config.yaml`. The backups (`config.yaml.bak` and `config.yaml.bak.<time>`) are created mode 0600 and are never written through a symlink. Only the newest 10 `config.yaml.bak.<time>` copies are kept; older ones are deleted on the next save. `config.yaml.bak` (the revert source) is always kept. A file that uses CRLF line endings keeps them when saved. The revision is an HMAC under a key that changes when Web Manager restarts, so it reveals nothing about the file's secrets; after a restart, reload the page before saving. Secrets are shown as `[REDACTED]`: values under secret-looking keys (the Beets secret keys plus any key ending in `key`, `pass` or `pwd`, starting with `auth`, or containing `token`, `secret`, `password`...), in any YAML form (flow mappings, anchors, `? key` entries, JSON), and passwords or `token`/`key`-style query parameters inside URLs. If a secret cannot be hidden reliably, the editor shows no content (`config_redaction_failed`). The editor fails closed rather than showing an empty config: it returns an error when the file is missing or the path is outside the Beets config directory. Restart the `beets` container so Beets reads a change.

Beets Web Manager provisions the bundled `discpath` plugin (and the `webmanager` plugin itself) into `/config/beetsplug`; further user plugins can be dropped into the same directory. `pluginpath` must include `/config/beetsplug` for any of them to load — the provisioning step ensures this automatically.

### What Web Manager changes in `config.yaml`, and what it only offers

With embedded Beets, nothing at startup. With two containers, at startup Web Manager adds only what its own transport needs:

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

## MusicBrainz is a Beets plugin (Beets 2.4 and later)

Since Beets 2.4, MusicBrainz autotagging is the `musicbrainz` plugin and must be listed in `plugins:`. Without it the importer finds no candidates, so every quiet import skips its albums ("not matched; left in place for review"). Web Manager's startup provisioning adds `musicbrainz` to an existing `plugins:` entry, next to `web` and `webmanager`. It keeps every plugin already listed, whether written inline (`plugins: a b`), as a flow list (`[a, b]`) or as a block list, and backs up `config.yaml` first. Fresh installs get it from `config.yaml.example`. On Beets older than 2.4, where MusicBrainz is built in, the extra entry only logs `plugin musicbrainz not found`.

The plugin report (`/api/plugins/status`) shows `musicbrainz` as enabled and loaded only when Beets really lists and loads it. `/api/setup/status`'s `integrations.musicbrainz` still reports Beets-engine/plugin-loader health (`connected` / `unavailable` / `plugin_loader_failed`), and every integration entry carries a `category` field (`service` for MusicBrainz/AcoustID/AI/Plex/Lidarr/SLSKD, `beets_plugin` for togglable Beets plugins) so the UI can render them as distinct groups. MusicBrainz lookups, matching, and review never depend on any AI provider credential being configured.

## Submission and fingerprinting terms

- Fingerprinting: local audio fingerprint generation through Chroma/fpcalc in the Beets engine.
- AcoustID lookup: querying AcoustID for candidate recordings from a fingerprint.
- `submit`: Beets/Chroma command for AcoustID submission readiness. Requires Chroma loaded and the command registered.
- `mbsubmit`: MusicBrainz submission command readiness. It is independent of `submit`.

## Beets image version

With embedded Beets, the Beets version is part of the Web Manager image: `requirements.txt` pins `beets==2.14.1`, and the self-test reports any other version as `beets_version_mismatch`. A Beets upgrade is a reviewed PR that changes that pin. The rest of this section is about the separate Beets container.

`docker-compose.yml`'s `beets` service `image:` line selects the stock Beets version. The shipped Compose files pin it to an exact LinuxServer build tag plus its manifest digest:

```yaml
    image: lscr.io/linuxserver/beets:2.14.1-ls356@sha256:ae076820ec34ea5e9ec05a22931e98ab555e9c25e784c5fb001c84f411119c73
```

That is the exact image the `stock-beets-acceptance` and `production-docker-acceptance` CI jobs test; CI reads it from `docker-compose.yml`, so the pin is the single source of truth. A newer Beets therefore never reaches you unannounced: it arrives as a Dependabot PR (or a maintainer PR) that changes this line, re-runs the acceptance against the new image, and is listed in the CHANGELOG. Past upstream Beets releases changed behavior Web Manager depends on (Beets 2.13 renamed `genre` to `genres`; Beets 2.14 changed relative paths and broke stock web routes), so the shipped default no longer floats on `latest`.

You can still choose another tag in your own Compose file (`lscr.io/linuxserver/beets:latest` floats; any other tag is untested by this project's CI). Changing the Beets version on an existing library can migrate its database irreversibly: follow `docs/BEETS_ENGINE_MIGRATION.md`. The scheduled `beets-latest-canary` workflow runs the same acceptance against `latest` every week so upstream breakage is seen before a pin bump; it is informational and never blocks a merge.

The Beets image version is independent of the Beets Web Manager image version -- upgrading one does not require upgrading the other.

Beets Web Manager builds and publishes exactly one image, `ghcr.io/iranman/beets-web-manager`. There is no custom Beets image anywhere in this repository or its CI -- the sole Beets runtime this repository verifies against is the unmodified, official `lscr.io/linuxserver/beets` image, exercised over HTTP by the `webmanager` integration plugin (`beetsplug/webmanager/`) in the `stock-beets-acceptance` job. Upgrading the Beets image version on a deployment with an existing library requires the backup/upgrade/rollback procedure in `docs/BEETS_ENGINE_MIGRATION.md` -- newer Beets releases can perform an automatic, one-time, non-reversible database schema migration on first open.

## Music Format Preferences and rejected downloads

Settings > Music Format Preferences decides which audio an import or download may bring in. The preferences are stored in `MUSIC_FORMAT_PREFS_FILE` (default `/config/music_format_preferences.json`). The defaults are:

- **Accepted channel layouts:** stereo, 2.1 and Dolby Atmos. Mono, 5.1 and 7.1 are rejected, and so is any other channel count unless you set a custom maximum.
- **Accepted formats:** FLAC, MP3, AAC, E-AC-3 and TrueHD. Every other format is rejected, including WAV, ALAC, Opus and Ogg Vorbis.
- A file whose channel layout cannot be determined, or that ffprobe cannot inspect, is rejected too.
- **Rejected downloads:** Quarantine.

Folder imports (`POST /api/import`), imports with a MusicBrainz ID, disk re-imports, AI imports, import review, album downloads and playlist downloads check every audio file before Beets imports it. When a file is rejected:

- **The source is outside `MUSIC_ROOT`** (for example under `/downloads`): with "Quarantine", the rejected file is **moved** out of its folder to `MUSIC_FORMAT_QUARANTINE_DIR/<YYYYMMDD>/<file name>`. A name that is already taken gets `-<unix time>` added. With "Delete", the file is deleted. Only the rejected audio files are handled; other files in the folder stay where they are. The import stops (a playlist download skips that track instead), and the job log names each file and where it went (`Rejected download quarantined: …`).
- **The source is a preserved torrent source** (a folder under `TORRENT_SOURCE_ROOTS`, which defaults to `DOWNLOADS_ROOT`, that the app did not create, unless `ALLOW_TORRENT_SOURCE_MOVE=1`; the same rule that makes imports copy instead of move): the rejected file is **left in place** under both "Quarantine" and "Delete", so a torrent client keeps seeding it. The import still stops, and the job log says `Rejected download left in place (file is a preserved torrent source that may still be seeding): <file>`. Folder imports, imports with a MusicBrainz ID and AI imports apply the rule to the source folder, so a rejected file and its import agree; other paths apply it to the file. If Web Manager cannot evaluate the rule, it also leaves the file in place. App-created downloads (album downloads staged under `DOWNLOADS_ROOT/_beets_missing_import`, playlist downloads) are still quarantined or deleted.
- **The source is inside `MUSIC_ROOT`:** nothing is moved or deleted. The files are marked "Needs replacement" and the import stops.
- Import review is the exception: a selected file that could not be inspected stays in review for a retry and is not moved.

| Variable | Default | Meaning |
|---|---|---|
| `MUSIC_FORMAT_QUARANTINE_DIR` | `/config/music_format_quarantine` | Where rejected downloads are moved. In the shipped Compose files, Web Manager's `/config` is the Beets config folder (`BEETS_CONFIG_PATH`, default `./beets`), so the files end up on the host in `<Beets config folder>/music_format_quarantine/`. It is operator-only: set it in the Web Manager service's `environment:` block (`docker-compose.yml` does not forward it from the Compose `.env`) and recreate the container. |

The quarantine move is not a library transaction. It does not appear on the Transactions page and cannot be rolled back there. Nothing empties the quarantine folder. To use a quarantined file, move it back yourself, change the preferences so that it is accepted, and import again.

## Library location and unattended duplicate deletion

- `MUSIC_ROOT` — where the Beets library is mounted inside the Web Manager container (default `/music`). Every shipped compose file mounts this path and now also sets the variable explicitly. It is the only setting for the library location. The setup and System page "Music Library" check tests this path inside the Web Manager container. It blocks setup only when the path is missing or unreadable; a read-only mount is fine. Web Manager uses the absolute item paths Beets reports, so `MUSIC_ROOT` must be the same path as Beets' `directory:` in the `beets` container. It is a container-side variable (layer 3 above): change it under the `beets-web-manager` service's `environment:` together with the volume target.
- Unattended (scheduled) duplicate deletion is a separate, explicit authorization, **off by default**. It is stored in `web-manager-data/duplicate_cleanup_authorization.json` and changed only through `POST /api/dedup/unattended-cleanup` (enabling requires the confirmation phrase `ENABLE UNATTENDED DUPLICATE DELETION`) or the Duplicate Files panel. Changing `MUSIC_ROOT` or any other setting never enables it. While it is off, the maintenance duplicate step still scans, fingerprint-verifies and records a review proposal (both paths, sizes, Recording IDs, fingerprint evidence, release slot, which copy is kept) but deletes nothing. `POST /api/dedup/maintenance-run` runs that step on its own.

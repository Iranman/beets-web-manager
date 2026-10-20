# Installation & Deployment Guide

> [!IMPORTANT]
> **Beets runs inside Web Manager.** One container holds the web app and Beets itself (pinned Beets 2.14.1, see [ADR 0006](adr/0006-embedded-beets.md)), and it uses the same Beets `config.yaml`, `musiclibrary.blb` and music folder a Beets container would. There is no separate Beets container. You do **not** need to install Python, Beets, `pip`, or `uv` on your host machine.
>
> Coming from the old two-container setup? Follow [Upgrading from two containers](#upgrading-from-two-containers) before you start the new version.

---

## Install

Needs only Docker and Docker Compose.

### Step 1: Create the stack directory and a Beets config

```bash
mkdir beets-stack && cd beets-stack
mkdir beets web-manager
curl -fsSL -o beets/config.yaml https://raw.githubusercontent.com/Iranman/beets-web-manager/main/config.yaml.example
```

`config.yaml` must set `directory: /music` and `library: /config/musiclibrary.blb` (the template does). Without a `config.yaml`, Beets starts with exactly those defaults and reports `config_missing` until you add one. Already have a Beets `/config` folder? Use it instead of `./beets`.

### Step 2: Create `docker-compose.yml`

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
      # Container paths: must match the mount targets below and config.yaml.
      - MUSIC_ROOT=/music
      - DOWNLOADS_ROOT=/downloads
      - BEETS_CONFIG=/config/config.yaml
    volumes:
      - ./beets:/config
      - /path/to/music:/music
      - /path/to/downloads:/downloads
      - ./web-manager:/web-manager-data
    healthcheck:
      # Unhealthy while Beets' self-test fails (beets.ok false).
      test: ["CMD", "python", "-c", "import json, sys, urllib.request; d = json.load(urllib.request.urlopen('http://127.0.0.1:8337/api/health', timeout=5)); sys.exit(0 if d.get('beets', {}).get('ok') else 1)"]
      interval: 10s
      timeout: 5s
      retries: 5
      start_period: 30s
```

The repository's [`docker-compose.yml`](../docker-compose.yml) is the full file, with every optional integration variable and the container hardening (`read_only`, `cap_drop: ALL`, `no-new-privileges`). Run it from the folder that holds it: its relative paths (`./beets`, `./web-manager`) resolve next to the file.

> [!TIP]
> - `/music` is mounted **read-write**: Beets moves, tags and imports files from inside this container.
> - Change only the **left** side of each volume (the host folder). The right side (`/music`, `/downloads`, `/config`) is the path inside the container; `MUSIC_ROOT`/`DOWNLOADS_ROOT`/`BEETS_CONFIG` and Beets' own `directory:` refer to it. If you change a right side, set the matching variable too (see `docs/CONFIGURATION.md`, "Configuration layers").
> - `config.yaml` plugins, `pluginpath` modules and hook commands run inside the Web Manager container. The Config page refuses changes to `hook:`, `pluginpath:` and `include:` for that reason; edit those in the file and restart.
> - The image is `ghcr.io/iranman/beets-web-manager:latest`; `docker compose pull` updates it. To pin or roll back, replace `latest` with an exact version such as `0.8.1`. To see the running version, use `curl -s http://127.0.0.1:8337/health/live` or the image's `org.opencontainers.image.version` label.

### Step 3: Start and check Beets

```bash
docker compose up -d
curl -s http://127.0.0.1:8337/api/health
```

`"beets": {"engine": "embedded", "ok": true, "codes": [...]}` means Beets started. `library_created` is expected for a brand-new library. Any other code is explained, with its fix, in [Troubleshooting](TROUBLESHOOTING.md#embedded-beets-self-test-codes). While an error-level check fails, reads work, every library change is refused with the reason, and Docker reports the container unhealthy.

### Step 4: Open Beets Web Manager

Open **`http://<server-ip>:8337`** in your browser. On your first visit, the **First-Run Setup Wizard** lets you create your administrator username and password.

### Step 5: Add your AcoustID application key

Audio fingerprint lookups need your own free AcoustID application key; there is no built-in shared key. Register an application at <https://acoustid.org/new-application>, then set `ACOUSTID_API_KEY` in the stack `.env` (or in **System → Settings**) and recreate the `beets-web-manager` container. Until it is set, the System page and `/api/setup/status` report **AcoustID not configured**, and fingerprint evidence is reported as unavailable (never as "no match"), so imports that need it go to the review queue instead of being decided without it. `ACOUSTID_USER_KEY` (from your AcoustID account page) is only needed to submit fingerprints.

---

## Upgrading from two containers

Earlier releases ran a stock LinuxServer Beets container (`beets`) next to Web Manager and talked to it over HTTP. That setup is gone: Beets now runs inside Web Manager, and the old `beets` service must be **removed**. Two programs must never write one library; while Web Manager sees the old service answering, or a `beet` command changing the library, it refuses library changes (`separate_beets_running` / `beet_command_running`).

Your library does not change: the same `config.yaml`, `musiclibrary.blb`, schema and paths are used, with the same Beets version (2.14.1) the pinned Beets container ran.

1. **Back up** while the old stack still runs. From the stack directory: `./backup.sh` (see the README's [Backups](../README.md#backups)). Note the album and item counts: `docker compose exec beets beet stats`.
2. **Stop and remove the old Beets service:** `docker compose stop beets && docker compose rm -f beets`.
3. **Edit `docker-compose.yml`** (or take the new file from the repository):
   - delete the whole `beets:` service, and any `depends_on: beets`;
   - in `beets-web-manager`, change the music volume from `:/music:ro` to `:/music` (read-write);
   - keep the `/config` volume pointing at the old Beets container's config folder;
   - make sure `MUSIC_ROOT` is the same path as `directory:` in that `config.yaml` (both `/music` by default);
   - make sure `plugins:` in that `config.yaml` lists `musicbrainz` (the old setup added it at startup; nothing adds it now, and without it imports find no candidates);
   - remove `BEETS_ENGINE`, `BEETS_WEB_URL`, `BEETS_WEBMANAGER_API_KEY`, `BEETS_WEBMANAGER_API_KEY_FILE`, and `BEETS_OUTBOUND_ALLOWLIST` if you set it only for Beets;
   - use the healthcheck from [Step 2](#step-2-create-docker-composeyml), so Docker reports the container unhealthy while Beets' self-test fails.
4. **Start:** `docker compose up -d --remove-orphans`.
5. **Check:** `curl -s http://127.0.0.1:8337/api/health` shows `"ok": true`, with no `library_created` (that code means the wrong `/config` folder is mounted) and no `legacy_settings_ignored`. The Library page shows the same album and item counts as step 1.

Forgot a step? Leftover settings never stop Web Manager: it starts anyway and reports `legacy_settings_ignored` naming them. A still-running `beets` service is reported as `separate_beets_running` and library changes stay refused until it is gone.

The `webmanager` plugin files and `.webmanager_api_key` the old setup put in `/config` are not used any more; the image ships its own copy. Delete them once you no longer need to roll back.

### Rolling back

Rollback is the previous image, not a mode switch:

1. `docker compose stop beets-web-manager`.
2. Restore the backup from step 1 (`./restore.sh backups/beets-backup-<timestamp>.tar.gz`) if you want the library exactly as it was; otherwise the library written by the new version is used as is (same schema).
3. Put back your previous `docker-compose.yml` (with its `beets` service and `:/music:ro`) and pin the previous Web Manager release in its `image:` line, for example `ghcr.io/iranman/beets-web-manager:0.8.1`.
4. `docker compose up -d`, then `docker compose restart beets` so it loads the `webmanager` plugin, and check `curl -s http://127.0.0.1:8337/api/setup/status`.

---

## Directory & Volume Architecture

| Host Path | Container Path | Purpose |
|---|---|---|
| `./beets` | `/config` | Beets `config.yaml` and the SQLite library (`musiclibrary.blb`), opened by Beets inside Web Manager. Web Manager changes `config.yaml` only when you ask it to, with a backup. |
| `/path/to/music` | `/music` | Music library files, **read-write**. |
| `/path/to/downloads` | `/downloads` | Ingest and download staging directory |
| `./web-manager` | `/web-manager-data` | Web Manager settings, user accounts, sessions, and transaction audit logs |

---

## Running Beets CLI Commands

`beet` is in the Web Manager container. Run it as the app's user (`PUID:PGID`):

```bash
docker compose exec -u 1000:1000 beets-web-manager beet version
docker compose exec -u 1000:1000 beets-web-manager beet ls artist:311
```

Read-only commands (`beet ls`, `beet version`, `beet stats`) are always safe. Do library changes (`beet import`, `beet modify`, `beet rm`) through Beets Web Manager's UI/API, which runs them through its preview/apply/audit workflow. While a mutating `beet` command runs, Web Manager reports `beet_command_running` and refuses its own library changes.

---

## Beets on another server

Web Manager no longer connects to a Beets instance over the network. Mount that server's Beets `/config` and music folders into the Web Manager container instead (for example over NFS or SMB), and stop the Beets there.

---

## Development Installation (Source Builds)

If you are developing Beets Web Manager and modifying source code:

```bash
docker compose -f docker-compose.dev.yml up -d --build
```

---

## Upgrades

To upgrade to the latest stable release, back up first (see [Backups](../README.md#backups)), then:

```bash
docker compose pull
docker compose up -d
curl -s http://127.0.0.1:8337/api/health
```

Beets and its plugins are part of the image, so there is nothing else to restart. Read the release's Upgrade Notes in [`CHANGELOG.md`](../CHANGELOG.md) before upgrading. The full upgrade, verification and rollback steps are in the README's [Upgrades](../README.md#upgrades) section.

Backups are not signed: the checksums in a backup's `MANIFEST.txt` detect damage, not tampering. Keep backups in a folder owned by root that no container can write to or mount. `restore.sh` refuses a backup without `MANIFEST.txt`. An old-layout archive (Beets files at the top level, made by a `backup.sh` from before manifests existed) can still be restored, unverified, with `--allow-legacy-backup`. A current-layout archive without `MANIFEST.txt` is always refused.

---

## Health Checks

```bash
# Web Manager health, including Beets' self-test ("beets": {"ok": ..., "codes": [...]})
curl -s http://127.0.0.1:8337/api/health
curl -s http://127.0.0.1:8337/health/live
```

With an admin login or the API token, `GET /api/health/detail` returns each Beets self-test check with its message.

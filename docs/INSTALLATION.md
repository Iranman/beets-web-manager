# Installation & Deployment Guide

> [!IMPORTANT]
> **Beets runs embedded inside Web Manager.** One container holds the web app and Beets itself (pinned Beets 2.14.1, see [ADR 0006](adr/0006-embedded-beets.md)), and it uses the same Beets `config.yaml`, `musiclibrary.blb` and music folder a Beets container would. You do **not** need to install Python, Beets, `pip`, or `uv` on your host machine.
>
> Embedded Beets is turned on with `BEETS_ENGINE=embedded` and will become the default in the next phase. Without it, Web Manager runs in the legacy two-container mode, which stays available as the rollback path until that mode is removed.

---

## Single container: Beets inside Web Manager (recommended)

Needs only Docker and Docker Compose, and a Web Manager release that includes embedded Beets (check that `docker run --rm --entrypoint python ghcr.io/iranman/beets-web-manager:latest -c "import beets; print(beets.__version__)"` prints `2.14.1`).

### Step 1: Create the stack directory and a Beets config

```bash
mkdir beets-stack && cd beets-stack
mkdir beets web-manager
curl -fsSL -o beets/config.yaml https://raw.githubusercontent.com/Iranman/beets-web-manager/main/config.yaml.example
```

`config.yaml` must set `directory: /music` and `library: /config/musiclibrary.blb` (the template does). Coming from a Beets container? Do not create a new config: follow [Moving from two containers to one](#moving-from-two-containers-to-one).

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
      - BEETS_ENGINE=embedded
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
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8337/api/health', timeout=5)"]
      interval: 10s
      timeout: 5s
      retries: 5
      start_period: 30s
```

[`examples/docker-compose.embedded.yml`](../examples/docker-compose.embedded.yml) is the full file, with every optional integration variable and the container hardening (`read_only`, `cap_drop: ALL`, `no-new-privileges`).

> [!TIP]
> - `/music` is mounted **read-write**: Beets moves, tags and imports files from inside this container.
> - There is no `BEETS_WEB_URL`, no `beets` service and no plugin to install. Web Manager never edits `config.yaml` in this mode.
> - `config.yaml` plugins, `pluginpath` modules and hook commands now run inside the Web Manager container. The Config page refuses changes to `hook:`, `pluginpath:` and `include:` for that reason; edit those in the file and restart.

### Step 3: Start and check Beets

```bash
docker compose up -d
curl -s http://127.0.0.1:8337/api/health
```

`"beets": {"engine": "embedded", "ok": true, "codes": [...]}` means Beets started. `library_created` is expected for a brand-new library. Any other code is explained, with its fix, in [Troubleshooting](TROUBLESHOOTING.md#embedded-beets-self-test-codes). While an error-level check fails, reads work and every library change is refused with the reason.

Then open **`http://<server-ip>:8337`** and complete the First-Run Setup Wizard, and add your AcoustID key ([Step 5](#step-5-add-your-acoustid-application-key) below applies to both setups).

### Moving from two containers to one

Your library does not change: the same `config.yaml`, `musiclibrary.blb`, schema and paths are used. Beets inside Web Manager is the same version as the pinned Beets container (2.14.1).

1. **Back up.** From the stack directory: `./backup.sh` (see the README's [Backups](../README.md#backups)). Note the album and item counts: `docker compose exec beets beet stats`.
2. **Stop the Beets container**, and keep it stopped: `docker compose stop beets`. Only one process may write the library. Do not run a separate `beet` against the same `/config` while Web Manager runs.
3. **Edit `docker-compose.yml`** for the `beets-web-manager` service:
   - add `BEETS_ENGINE=embedded`;
   - remove `BEETS_WEB_URL` and `BEETS_OUTBOUND_ALLOWLIST` if you set them only for Beets;
   - change the music volume from `:/music:ro` to `:/music` (read-write);
   - keep the `/config` volume pointing at the Beets container's config folder;
   - delete the `beets` service (or leave it stopped for an easy rollback).
4. **Start:** `docker compose up -d beets-web-manager`.
5. **Check:** `curl -s http://127.0.0.1:8337/api/health` shows `"engine": "embedded"` and `"ok": true`, with no `library_created` (that code means the wrong `/config` folder is mounted). The Library page shows the same album and item counts as step 1.

The `webmanager` plugin files the old setup put in `/config/beetsplug` are ignored: the image's own copy is always used. You can delete them once you no longer need to roll back.

### Rolling back to two containers

1. `docker compose stop beets-web-manager`.
2. Restore the backup from step 1 above if you want the library exactly as it was before; otherwise the library written by embedded Beets is used as is (same schema).
3. Put back the previous `docker-compose.yml`: remove `BEETS_ENGINE=embedded` (or set `BEETS_ENGINE=http`), mount `/music` read-only again, restore the `beets` service and `BEETS_WEB_URL`.
4. `docker compose up -d`, then `docker compose restart beets` so it loads the `webmanager` plugin, and check `curl -s http://127.0.0.1:8337/api/setup/status`.

---

## Legacy: two containers (stock Beets over HTTP)

The repository's `docker-compose.yml` still ships this setup until embedded Beets becomes the default. It runs a stock LinuxServer Beets container next to Web Manager.

### Step 1: Create a directory for your stack

```bash
mkdir beets-stack && cd beets-stack
```

### Step 2: Create `docker-compose.yml`

Create a file named `docker-compose.yml` with the following content:

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
      # Container paths: must match the mount targets below.
      - MUSIC_ROOT=/music
      - DOWNLOADS_ROOT=/downloads
      - BEETS_CONFIG=/config/config.yaml
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

> [!TIP]
> - Update `/path/to/music` to point to your music library on the host.
> - Update `/path/to/downloads` to point to your downloads or staging directory.
> - `./beets` and `./web-manager` will be created automatically in your current directory for persistent configuration and application state.
> - The Web Manager image is `ghcr.io/iranman/beets-web-manager:latest`; `docker compose pull` updates it. To pin or roll back, replace `latest` in the `image:` line with an exact version such as `0.1.49`. No `.env` variable selects the version: a `BEETS_WEB_MANAGER_VERSION=` line left over from an older Compose layout does nothing and can be deleted. To see the running version, use `curl -s http://127.0.0.1:8337/health/live` or the image's `org.opencontainers.image.version` label.
> - Change only the **left** side of each volume (the host folder). The right side (`/music`, `/downloads`, `/config`) is the path inside the containers. Web Manager's `MUSIC_ROOT`/`DOWNLOADS_ROOT`/`BEETS_CONFIG` and Beets' own `directory:` refer to it. If you change a right side, change it in both services and set the matching variable (see `docs/CONFIGURATION.md`, "Configuration layers").

### Step 3: Start the stack

```bash
docker compose up -d
```

### Step 4: Open Beets Web Manager

Open **`http://<server-ip>:8337`** in your browser. On your first visit, you will see the **First-Run Setup Wizard** where you can create your administrator username and password.

### Step 5: Add your AcoustID application key

Audio fingerprint lookups need your own free AcoustID application key; there is no built-in shared key. Register an application at <https://acoustid.org/new-application>, then set `ACOUSTID_API_KEY` in the stack `.env` (or in **System → Settings**) and recreate the `beets-web-manager` container. Until it is set, the System page and `/api/setup/status` report **AcoustID not configured**, and fingerprint evidence is reported as unavailable (never as "no match"), so imports that need it go to the review queue instead of being decided without it. `ACOUSTID_USER_KEY` (from your AcoustID account page) is only needed to submit fingerprints.

---

## Directory & Volume Architecture

With embedded Beets, the one Web Manager container mounts:

| Host Path | Container Path | Purpose |
|---|---|---|
| `./beets` | `/config` | Beets `config.yaml` and the SQLite library (`musiclibrary.blb`). Read by Beets inside Web Manager; Web Manager never edits `config.yaml` in this mode. |
| `/path/to/music` | `/music` | Music library files, **read-write**. |
| `/path/to/downloads` | `/downloads` | Ingest and download staging directory |
| `./web-manager` | `/web-manager-data` | Web Manager settings, user accounts, sessions, and transaction audit logs |

In the legacy two-container stack, both containers share the same underlying files:

| Host Path | Container Path | Purpose |
|---|---|---|
| `./beets` | `/config` | Beets configuration (`config.yaml`), plugins, and SQLite library database (`musiclibrary.blb`), owned by the `beets` container. Web Manager also mounts this read/write, only to provision its integration plugin and merge `config.yaml` entries — it never opens `musiclibrary.blb` directly. |
| `/path/to/music` | `/music` | Authoritative music library files. Mounted read-only into Web Manager; only the `beets` container writes here. |
| `/path/to/downloads` | `/downloads` | Ingest and download staging directory |
| `./web-manager` | `/web-manager-data` | Web manager settings, user accounts, sessions, and transaction audit logs (Web Manager's own state; not shared with `beets`) |

---

## Running Beets CLI Commands

With embedded Beets, `beet` is in the Web Manager container. Run it as the app's user (`PUID:PGID`) and keep to read-only commands, because it is a second process on the same library:

```bash
docker compose exec -u 1000:1000 beets-web-manager beet version
docker compose exec -u 1000:1000 beets-web-manager beet ls artist:311
```

In the legacy two-container stack, you can execute any standard `beet` command inside the `beets` container:

```bash
# Check Beets version and loaded plugins
docker compose exec beets beet version

# List music in your library
docker compose exec beets beet ls

# Import music via CLI
docker compose exec beets beet import /downloads/new-album
```

Read-only commands (`beet ls`, `beet version`) are always safe, any time. Prefer doing mutating operations (`beet import`, `beet modify`, `beet rm`) through Beets Web Manager's own UI/API, which serializes them through its own controlled preview/apply/audit workflow; if you do run a manual mutating `beet` command in the container, avoid doing so while a Web Manager job is actively running — SQLite's own file-level locking on `/config/musiclibrary.blb` prevents literal database corruption from simultaneous writes, but it does not coordinate with Web Manager's own multi-step operations.

---

## Advanced: External / Standalone Beets Deployment (legacy)

With embedded Beets, mount the Beets host's `/config` and music folders into the Web Manager container instead (for example over NFS or SMB), and stop that Beets instance. The rest of this section is for keeping a separate Beets instance.

If you run Beets on a separate host (e.g. TrueNAS, Unraid, or another server) and want Beets Web Manager to connect over the network instead of starting its own `beets` service:

1. Use [examples/docker-compose.external-beets.yml](../examples/docker-compose.external-beets.yml).
2. Configure `BEETS_WEB_URL` (e.g. `http://192.168.1.50:8337`) and `BEETS_OUTBOUND_ALLOWLIST`. Always set `BEETS_WEB_URL` when Web Manager does not run on the same Compose network as a service named `beets`: the default `http://beets:8337` is resolved by normal DNS there, and Web Manager sends its `webmanager` bearer key to whatever host answers to `beets`.
3. The remote Beets instance must already have its `web`/`webmanager` plugins enabled and provisioned — Web Manager cannot provision a plugin into a filesystem it does not mount.
4. Without the shared `/config` mount Web Manager cannot read the integration key file. Copy the 64-hex key from the Beets host's `/config/.webmanager_api_key` into `BEETS_WEBMANAGER_API_KEY` (System page, or the Compose environment).

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
docker compose restart beets   # two-container stack only: load a new webmanager plugin version
```

With embedded Beets the plugin and Beets are part of the image, so there is nothing else to restart. Check `curl -s http://127.0.0.1:8337/api/health` afterwards.

Read the release's Upgrade Notes in [`CHANGELOG.md`](../CHANGELOG.md) before upgrading. The full upgrade, verification and rollback steps are in the README's [Upgrades](../README.md#upgrades) section.

Backups are not signed: the checksums in a backup's `MANIFEST.txt` detect damage, not tampering. Keep backups in a folder owned by root that no container can write to or mount. `restore.sh` refuses a backup without `MANIFEST.txt`. An old-layout archive (Beets files at the top level, made by a `backup.sh` from before manifests existed) can still be restored, unverified, with `--allow-legacy-backup`. A current-layout archive without `MANIFEST.txt` is always refused.

---

## Health Checks

Verify that Web Manager (and, embedded, Beets inside it) is healthy:

```bash
# Web Manager health; with embedded Beets this includes the Beets self-test
curl -s http://127.0.0.1:8337/api/health
curl -s http://127.0.0.1:8337/health/live
```

With an admin login or the API token, `GET /api/health/detail` returns each Beets self-test check with its message.
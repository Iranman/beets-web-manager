# Troubleshooting Guide

This guide covers common errors and resolution steps for Beets Web Manager.

---

## Common Deployment Failures

### 1. `pull access denied for beets` or `beets-web-manager`
* **Cause**: Incorrect image tag or registry name.
* **Fix**:
  1. For standard deployments: Use the production `docker-compose.yml` with official images `lscr.io/linuxserver/beets:latest` and `ghcr.io/iranman/beets-web-manager:${BEETS_WEB_MANAGER_VERSION:-stable}` (the tag defaults to `stable`).
  2. For development builds from source: Run `docker compose -f docker-compose.dev.yml up -d --build`.

---

### 2. `failed to read dockerfile`
* **Cause**: Docker Compose is attempting to execute a build directive (`build: .`) when running outside the source repository root directory.
* **Fix**: Use the production `docker-compose.yml`. Production deployments use published pre-built images and do **not** require a local `Dockerfile` or repository source code.

---

### 3. Web UI is unreachable (`http://<server-ip>:8337`)
* **Cause**: Port binding or network firewall.
* **Fix**:
  1. Confirm the container is running: `docker compose ps`.
  2. Check container logs: `docker compose logs beets-web-manager`.
  3. Ensure port `8337` is not blocked by host firewall.

---

### 4. Setting up administrator password
* **First Run**: Open `http://<server-ip>:8337` in your browser. The setup wizard lets you set your username and password directly.
* **Reset Password**: You can set `BEETS_WEB_PASSWORD` in your Compose environment variables or remove `./web-manager/.browser_password` to re-trigger the initial setup.

---

### 5. Setup says "Music library path ... is not accessible"
* **Cause**: Web Manager cannot read its library mount. It checks `MUSIC_ROOT` (default `/music`) inside the `beets-web-manager` container.
* **Fix**:
  1. Confirm the music volume is mounted into `beets-web-manager` at `/music` (read-only is fine), as in `docker-compose.yml`.
  2. If you mount it somewhere else, set `MUSIC_ROOT` to that path under the `beets-web-manager` service's `environment:`. It must match Beets' `directory:` in the `beets` container (see `docs/CONFIGURATION.md`).
  3. Check that the container's `PUID`/`PGID` can read the host directory.

### 6. Setup says "Cannot write to downloads/staging path ..."
* **Cause**: Web Manager checks `DOWNLOADS_ROOT` (default `/downloads`) inside the `beets-web-manager` container. In v0.1.49 and earlier it read the host-side `DOWNLOADS_PATH` instead. A saved Settings value such as `DOWNLOADS_PATH=./downloads` therefore produced the false path `downloads`.
* **Fix**:
  1. Upgrade. On startup Web Manager removes host-side keys from `/web-manager-data/.env` once, keeping a `.env.bak-migration-<timestamp>` backup.
  2. Confirm the downloads volume is mounted at `/downloads` and is writable by `PUID`/`PGID`.
  3. If you mount it elsewhere, set `DOWNLOADS_ROOT` under the service's `environment:`.

---

### 7. The Config page shows an empty `config.yaml`, or says the config was not found
* **Cause**: The editor reads `BEETS_CONFIG` (default `/config/config.yaml`). In v0.1.49 and earlier it read the host-side `BEETS_CONFIG_PATH`. After any Settings save, that sent it to an empty file inside the container, and edits were never written to Beets' real config.
* **Fix**: Upgrade. If you saved edits from the Config page on v0.1.49 or earlier, they are not in `/config/config.yaml`; apply them again. The editor now refuses to read or write anything other than a file directly inside the Beets config directory.

---

### 8. Setup says "Stock Beets is unavailable"
* **Cause**: Web Manager cannot reach the `webmanager` plugin at `BEETS_WEB_URL` (default `http://beets:8337`). While Beets is down, plugin and fpcalc checks show as unknown. Only real local mount problems are listed in addition.
* **Fix**: `docker compose ps beets`, `docker compose logs beets`. For an external Beets, check that `BEETS_WEB_URL` and `BEETS_OUTBOUND_ALLOWLIST` agree.
* **Outside Compose, always set `BEETS_WEB_URL`.** The default `http://beets:8337` only means "the `beets` service" on a Compose network. Under `docker run`, Kubernetes, or a host whose DNS search domain resolves `beets` to some other machine, Web Manager would send its `webmanager` bearer key to whatever `beets` resolves to. Set `BEETS_WEB_URL` to your Beets address explicitly and keep `BEETS_OUTBOUND_ALLOWLIST` limited to that host and port.

### 8a. Startup did not add the webmanager plugin to `config.yaml`
* **Cause**: Startup auto-provisioning backs up `config.yaml` before every edit, and a failed backup aborts the edit. If the Beets config directory is mounted read-only into Web Manager (or is not writable by its `PUID`/`PGID`), the backup cannot be written, so startup skips the `config.yaml` edit and logs `Auto plugin provisioning on startup skipped/failed`. Nothing is changed. The same happens when `BEETS_CONFIG` is not a file directly inside `BEETSDIR` (default `/config`), or when `config.yaml` is a symbolic link (Web Manager never edits a config file through a link; replace it with the real file).
* **Fix**: Mount the Beets config directory read-write into Web Manager (as the shipped Compose files do) and restart it, or add `web` and `webmanager` to `plugins:` and `/config/beetsplug` to `pluginpath:` yourself. Then restart the `beets` container.

* **Beets log: "not using ... as an allowed mutation root"**: the `webmanager` plugin (1.6.1+) never derives a mutation root from `/`, the Beets config directory or one of its parents, whether it comes from Beets' `directory:` or `webmanager.import_roots`. Fix `directory:`/`import_roots` in `config.yaml`, or set `webmanager.allowed_roots` explicitly (an explicit value is used as given).

### 9. Setup warns about include_paths, root mismatches or a required restart
* **`beets_web_include_paths_disabled`**: your Beets `config.yaml` has `web: include_paths: no`, so Beets returns items without file paths. Path-based operations then fail with `BEETS_PATHS_UNAVAILABLE` (HTTP 503). Use the setup action "Enable web.include_paths" (`POST /api/setup/beets-config/include-paths`), which backs up `config.yaml` first. Then restart the `beets` container.
* **`music_root_mismatch`**: Beets' `directory:` and Web Manager's `MUSIC_ROOT` differ. Mount the library at the same container path in both services, or change one of the two settings (see `docs/CONFIGURATION.md`).
* **`downloads_root_not_import_root`**: Web Manager's `DOWNLOADS_ROOT` is not inside the plugin's `webmanager.import_roots`. Mount downloads at the same path in both containers, or add that path to `webmanager.import_roots` in `config.yaml`.
* **`beets_restart_required`**: Beets is still running an older `webmanager` plugin than the one Web Manager provisioned. Restart the `beets` container.
* If these fields show as `unknown`, Beets is running a plugin older than 1.6.0. Restart Beets so it loads the provisioned plugin.

### 10. fpcalc is reported missing although chroma is enabled
* **Cause**: from plugin 1.6.0, Beets reports whether the `fpcalc` binary is on its own `PATH`. The `chroma` plugin can load without it, but fingerprinting then fails.
* **Fix**: use the stock LinuxServer Beets image, which ships `fpcalc`, or install chromaprint in your Beets image. Web Manager itself never needs `fpcalc`.

---

## Operational Diagnostics

### Check Container Health
```bash
docker compose ps beets-web-manager
curl -fsS http://127.0.0.1:8337/api/health
```

### View Recent Logs
```bash
docker compose logs --tail=100 beets-web-manager
```

### Inspect Beets Setup Status
```bash
curl -fsS -H "Authorization: Bearer <your-token>" http://127.0.0.1:8337/api/setup/status
```
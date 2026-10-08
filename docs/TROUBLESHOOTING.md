# Troubleshooting Guide

This guide covers common errors and resolution steps for Beets Web Manager.

---

## Common Deployment Failures

### 1. `pull access denied for beets` or `beets-web-manager`
* **Cause**: Incorrect image tag or registry name.
* **Fix**:
  1. For standard deployments: Use the production `docker-compose.yml` with official images `lscr.io/linuxserver/beets:latest` and `ghcr.io/iranman/beets-web-manager:latest` (replace `latest` with an exact version such as `0.1.49` to pin).
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
* **Saving refused with `config_secret_ambiguous` or `config_redacted_placeholder`**: secret values show as `[REDACTED]`; leave that text unchanged to keep the stored secret, replace the whole `[REDACTED]` (quotes included) to set a new one, or use `""` to clear it. `config_secret_ambiguous` means the stored `config.yaml` has that secret key twice in the same section, or not at all at that place (for example you moved or copied the line). Type the real value on that line, or reload the page and redo the edit.

---

### 8. Setup says "Stock Beets is unavailable"
* **Cause**: Web Manager cannot reach the `webmanager` plugin at `BEETS_WEB_URL` (default `http://beets:8337`). While Beets is down, plugin and fpcalc checks show as unknown. Only real local mount problems are listed in addition.
* **Fix**: `docker compose ps beets`, `docker compose logs beets`. For an external Beets, check that `BEETS_WEB_URL` and `BEETS_OUTBOUND_ALLOWLIST` agree.
* **`beets_web_url_userinfo` ("BEETS_WEB_URL contains a user name or password")**: `BEETS_WEB_URL` has `user:pass@` in it. This never worked (no credentials were sent and the request failed), so Web Manager now refuses the URL and sends nothing. Remove the credentials, for example `BEETS_WEB_URL=http://beets:8337`, and restart Web Manager. The System page refuses to save such a URL. The `webmanager` plugin is authenticated with its bearer key; if a reverse proxy in front of Beets needs Basic auth, point `BEETS_WEB_URL` at Beets directly instead.
* **Credentials stay in another `*_URL` setting after saving the plain URL**: the System page shows URL settings such as `PLEX_URL` without `user:pass@`, and saving that unchanged URL keeps the stored value with its credentials. To remove them, clear the field and save, then enter the URL again, or save a different URL.
* **Outside Compose, always set `BEETS_WEB_URL`.** The default `http://beets:8337` only means "the `beets` service" on a Compose network. Under `docker run`, Kubernetes, or a host whose DNS search domain resolves `beets` to some other machine, Web Manager would send its `webmanager` bearer key to whatever `beets` resolves to. Set `BEETS_WEB_URL` to your Beets address explicitly and keep `BEETS_OUTBOUND_ALLOWLIST` limited to that host and port.

### 8a. Startup did not add the webmanager plugin to `config.yaml`
* **Cause**: Startup auto-provisioning backs up `config.yaml` before every edit, and a failed backup aborts the edit. If the Beets config directory is mounted read-only into Web Manager (or is not writable by its `PUID`/`PGID`), the backup cannot be written, so startup skips the `config.yaml` edit and logs `Auto plugin provisioning on startup skipped/failed`. Nothing is changed. The same happens when `BEETS_CONFIG` is not a file directly inside `BEETSDIR` (default `/config`), or when `config.yaml` is a symbolic link (Web Manager never edits a config file through a link; replace it with the real file).
* **Fix**: Mount the Beets config directory read-write into Web Manager (as the shipped Compose files do) and restart it, or add `web` and `webmanager` to `plugins:` and `/config/beetsplug` to `pluginpath:` yourself. Then restart the `beets` container.

* **Beets log: "not using ... as an allowed mutation root"**: the `webmanager` plugin (1.6.1+) never derives a mutation root from `/`, the Beets config directory or one of its parents (1.6.2+: or a directory inside it, such as `/config/music`), whether it comes from Beets' `directory:` or `webmanager.import_roots`. The warning is logged once per root. Fix `directory:`/`import_roots` in `config.yaml`, or set `webmanager.allowed_roots` explicitly (an explicit value is used as given).

### 9. Setup warns about include_paths, root mismatches or a required restart
* **`beets_web_include_paths_disabled`**: your Beets `config.yaml` has `web: include_paths: no`, so Beets returns items without file paths. Path-based operations then fail with `BEETS_PATHS_UNAVAILABLE` (HTTP 503). Use the setup action "Enable web.include_paths" (`POST /api/setup/beets-config/include-paths`), which backs up `config.yaml` first. Then restart the `beets` container.
* **`music_root_mismatch`**: Beets' `directory:` and Web Manager's `MUSIC_ROOT` differ. Mount the library at the same container path in both services, or change one of the two settings (see `docs/CONFIGURATION.md`).
* **`downloads_root_not_import_root`**: Web Manager's `DOWNLOADS_ROOT` is not inside the plugin's `webmanager.import_roots`. Mount downloads at the same path in both containers, or add that path to `webmanager.import_roots` in `config.yaml`.
* **`beets_restart_required`**: Beets is still running an older `webmanager` plugin than the one Web Manager provisioned. Restart the `beets` container.
* If these fields show as `unknown`, Beets is running a plugin older than 1.6.0. Restart Beets so it loads the provisioned plugin.

### 10. fpcalc is reported missing although chroma is enabled
* **Cause**: from plugin 1.6.0, Beets reports whether the `fpcalc` binary is on its own `PATH`. The `chroma` plugin can load without it, but fingerprinting then fails.
* **Fix**: use the stock LinuxServer Beets image, which ships `fpcalc`, or install chromaprint in your Beets image. Web Manager itself never needs `fpcalc`.

### 11. Web Manager stops at startup with "cannot take ownership of /web-manager-data" after a `PUID`/`PGID` change
* **Cause**: Web Manager keeps `/web-manager-data` private (mode 0700, owned by `PUID`:`PGID`). The hardened Compose files (`docker-compose.full.yml`, `examples/docker-compose.external-beets.yml`) drop all capabilities except `CHOWN`, `SETUID` and `SETGID`. Without the capability to read other users' folders, the startup step cannot read a private folder that still belongs to the old `PUID`:`PGID`, so it cannot give the folder to the new IDs. `docker-compose.yml` keeps the default capabilities and is not affected.
* **Fix**: give the folder to the new IDs once, then start the container again. Use your new `PUID`/`PGID` values in place of `1001:1001` below.
  * Bind mount (a host folder): `sudo chown -R 1001:1001 /path/to/web-manager-data`
  * Named volume: `docker run --rm -v <volume-name>:/d alpine chown -R 1001:1001 /d`

### 12. A tag, genre or MusicBrainz ID edit fails with `WRITE_FAILED`
* **Cause**: Beets saved the change in its library, but could not write the tags into the music file. Usually the file is not writable by the user the `beets` container runs as (`PUID`:`PGID`). A common reason is files imported as root, for example with `docker exec beets beet import` without `--user abc`. Beets logs the exact error (`error writing ...`) in the `beets` container log.
* **Effect**: the transaction is marked Failed. The library database already holds the new value, while the file keeps its old tag. A failed tag write cannot be rolled back from the Transactions page yet.
* **Fix**: make the music files writable by `PUID`:`PGID` (for example `sudo chown -R <PUID>:<PGID> /path/to/music`), then apply the same edit again so Beets rewrites the tag. To undo the edit instead, edit the field back to its old value. When running Beets commands by hand in the container, use `docker exec --user abc beets beet ...`.

### 13. A download disappeared from `/downloads` after an import
* **Cause**: the file failed Music Format Preferences. By default mono, 5.1, 7.1, WAV, ALAC, Opus/Vorbis and any file ffprobe cannot inspect are rejected. With the default "Rejected downloads: Quarantine", a rejected file under `/downloads` is moved out of its folder to `MUSIC_FORMAT_QUARANTINE_DIR` (default `/config/music_format_quarantine`, which is the Beets config folder on the host in the shipped Compose files). With "Delete", it is deleted. A preserved torrent source (a folder under `TORRENT_SOURCE_ROOTS` that Web Manager did not create) is never moved or deleted: the log says `Rejected download left in place (...)` and the file is still where it was.
* **Check**: the import job's log shows `Rejected download: <reason>: <file>` and `Rejected download quarantined: <path>`. Look in `<quarantine dir>/<YYYYMMDD>/`.
* **Fix**: move the file back yourself, then change Settings > Music Format Preferences so that the format or channel layout is accepted, and import again. The move is not a library transaction, so the Transactions page cannot roll it back. See [Music Format Preferences and rejected downloads](CONFIGURATION.md#music-format-preferences-and-rejected-downloads).

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
# Troubleshooting Guide

This guide covers common errors and resolution steps for Beets Web Manager.

---

## Embedded Beets self-test codes

With `BEETS_ENGINE=embedded`, Web Manager checks Beets once at startup. `GET /api/health` lists the failing codes (`"beets": {"ok": ..., "codes": [...]}`); the authenticated `GET /api/health/detail` and the log (`Beets self-test <code>: ...`) give the full message. While an **error** fails, reads keep working and every library change is refused with that message (`BEETS_EMBEDDED_DEGRADED`). Codes marked **stops Beets** prevent the engine from starting at all (`BEETS_EMBEDDED_UNAVAILABLE`). Restart the container after a fix: the check runs at startup.

| Code | Level | Meaning | Fix |
|---|---|---|---|
| `config_invalid` | error, stops Beets | `config.yaml` could not be parsed. | Fix the YAML (the message names the file and the parser error). |
| `config_missing` | warning | No `config.yaml` in `BEETSDIR` (`/config`); Beets defaults are used. | Mount the folder with your `config.yaml` at `/config`, or create one from `config.yaml.example`. |
| `library_open_failed` | error, stops Beets | `library:` could not be opened as a Beets database. | Check the `/config` mount, the `library:` path and ownership for `PUID`/`PGID`. |
| `library_created` | warning | No database existed at `library:`, so an empty one was created. | Expected on a fresh install. Otherwise the wrong folder is mounted at `/config`: stop, mount the folder that holds `musiclibrary.blb`, and delete the empty file. |
| `library_dir_missing` | error | Beets' `directory:` does not exist in the container. | Mount the music folder at that path (normally `/music`). |
| `library_dir_not_writable` | error | The music folder is not writable. | Mount `/music` read-write (remove `:ro`) and check ownership for `PUID`/`PGID`. |
| `library_db_not_writable` | error | The database or its folder is not writable (SQLite needs the folder for its journal). | Fix ownership of the `/config` folder for `PUID`/`PGID`. |
| `plugins_failed` | error | A plugin in `plugins:` failed to load; the message names it. | Fix the name, remove the plugin, or (for a plugin with extra Python packages the image does not ship) remove it from `plugins:`. |
| `operations_shadowed` | error, stops Beets | A plugin the image ships (`webmanager`, `discpath`) resolved to another copy. | Remove that copy (usually an old one in `/config/beetsplug` on a non-default `pluginpath`) and restart. |
| `beets_version_mismatch` | error | The running Beets is not the version Web Manager is built for (2.14.1). | Use the published image; do not install another Beets into it. |
| `fpcalc_missing` | error with `chroma`, otherwise warning | `fpcalc` (Chromaprint) is not on `PATH`. | Use the published image, which includes it. |
| `ffmpeg_missing` | warning | `ffmpeg` is not on `PATH`. | Use the published image, which includes it. |
| `engine_start_failed` | error, stops Beets | Beets raised an unexpected error while starting. | See the log for the exception. |

**The Config page refuses a save with `CONFIG_CODE_EXECUTION_EDIT_REFUSED`**: with embedded Beets, whatever `config.yaml` makes Beets run runs inside Web Manager, so the browser (the Config page and Setup's plugin fixes) may change only the allowlisted settings described under *Embedded Beets* in [CONFIGURATION.md](CONFIGURATION.md); the error names the refused keys. `import.set_fields`, `replace` and new YAML aliases (`*name`) are among them. Make that change by editing `config.yaml` on the server, then restart Web Manager.

---

## Common Deployment Failures

### 1. `pull access denied for beets` or `beets-web-manager`
* **Cause**: Incorrect image tag or registry name.
* **Fix**:
  1. For standard deployments: Use the production `docker-compose.yml` with official images `lscr.io/linuxserver/beets` (pinned to the tag and digest in `docker-compose.yml`) and `ghcr.io/iranman/beets-web-manager:latest` (replace `latest` with an exact version such as `0.1.49` to pin).
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
* **`[REDACTED]` inside a flow mapping, an anchor, a URL or a JSON line, and saving is refused**: the editor can restore a stored secret only on its own `key: value` line. Replace that `[REDACTED]` with the real value, or edit `config.yaml` directly.
* **The Config page shows no content with `config_redaction_failed`**: a secret is written in a form the editor cannot hide (for example escaped characters in a JSON string). Edit `config.yaml` directly, or write the secret as a plain `key: value` line.
* **Saving says the config was changed by another writer after Web Manager restarted**: revisions change on restart. Reload the page and save again.

---

### 8. Setup says "Stock Beets is unavailable" (two-container mode)
* **Cause**: Web Manager cannot reach the `webmanager` plugin at `BEETS_WEB_URL` (default `http://beets:8337`). While Beets is down, plugin and fpcalc checks show as unknown. Only real local mount problems are listed in addition.
* **Fix**: `docker compose ps beets`, `docker compose logs beets`. For an external Beets, check that `BEETS_WEB_URL` and `BEETS_OUTBOUND_ALLOWLIST` agree.
* **`beets_web_url_userinfo` ("BEETS_WEB_URL contains a user name or password")**: `BEETS_WEB_URL` has `user:pass@` in it. This never worked (no credentials were sent and the request failed), so Web Manager now refuses the URL and sends nothing. Remove the credentials, for example `BEETS_WEB_URL=http://beets:8337`, and restart Web Manager. The System page refuses to save such a URL. The `webmanager` plugin is authenticated with its bearer key; if a reverse proxy in front of Beets needs Basic auth, point `BEETS_WEB_URL` at Beets directly instead.
* **Credentials stay in another `*_URL` setting after saving the plain URL**: the System page shows URL settings such as `PLEX_URL` without `user:pass@`, and saving that unchanged URL keeps the stored value with its credentials. To remove them, clear the field and save, then enter the URL again, or save a different URL.
* **The Library page fails with an authentication error, while other Beets pages load**: since webmanager plugin 1.18.0 the Library page reads the library through the authenticated plugin (`/webmanager/fields/...`). Before, it used Beets' unauthenticated `/item/` list, so a wrong or missing key went unnoticed there. Make Web Manager's key (`BEETS_WEBMANAGER_API_KEY`, or the key file `BEETS_WEBMANAGER_API_KEY_FILE`, default `/config/.webmanager_api_key`) match the plugin's `webmanager.api_key_file` in Beets' `config.yaml`, then recreate the Web Manager container. The System page's plugin status reports the same 401.
* **Outside Compose, always set `BEETS_WEB_URL`.** The default `http://beets:8337` only means "the `beets` service" on a Compose network. Under `docker run`, Kubernetes, or a host whose DNS search domain resolves `beets` to some other machine, Web Manager would send its `webmanager` bearer key to whatever `beets` resolves to. Set `BEETS_WEB_URL` to your Beets address explicitly and keep `BEETS_OUTBOUND_ALLOWLIST` limited to that host and port.

### 8a. Startup did not add the webmanager plugin to `config.yaml` (two-container mode)
* **Cause**: Startup auto-provisioning backs up `config.yaml` before every edit, and a failed backup aborts the edit. If the Beets config directory is mounted read-only into Web Manager (or is not writable by its `PUID`/`PGID`), the backup cannot be written, so startup skips the `config.yaml` edit and logs `Auto plugin provisioning on startup skipped/failed`. Nothing is changed. The same happens when `BEETS_CONFIG` is not a file directly inside `BEETSDIR` (default `/config`), or when `config.yaml` is a symbolic link (Web Manager never edits a config file through a link; replace it with the real file).
* **Fix**: Mount the Beets config directory read-write into Web Manager (as the shipped Compose files do) and restart it, or add `web` and `webmanager` to `plugins:` and `/config/beetsplug` to `pluginpath:` yourself. Then restart the `beets` container.

* **Beets log: "not using ... as an allowed mutation root"**: the `webmanager` plugin (1.6.1+) never derives a mutation root from `/`, the Beets config directory or one of its parents (1.6.2+: or a directory inside it, such as `/config/music`), whether it comes from Beets' `directory:` or `webmanager.import_roots`. The warning is logged once per root. Fix `directory:`/`import_roots` in `config.yaml`, or set `webmanager.allowed_roots` explicitly (an explicit value is used as given).

### 9. Setup warns about include_paths, root mismatches or a required restart (two-container mode)
* **`beets_web_include_paths_disabled`**: your Beets `config.yaml` has `web: include_paths: no`, so Beets returns items without file paths. Path-based operations then fail with `BEETS_PATHS_UNAVAILABLE` (HTTP 503). Use the setup action "Enable web.include_paths" (`POST /api/setup/beets-config/include-paths`), which backs up `config.yaml` first. Then restart the `beets` container.
* **`music_root_mismatch`**: Beets' `directory:` and Web Manager's `MUSIC_ROOT` differ. Mount the library at the same container path in both services, or change one of the two settings (see `docs/CONFIGURATION.md`).
* **`downloads_root_not_import_root`**: Web Manager's `DOWNLOADS_ROOT` is not inside the plugin's `webmanager.import_roots`. Mount downloads at the same path in both containers, or add that path to `webmanager.import_roots` in `config.yaml`.
* **`beets_restart_required`**: Beets is still running an older `webmanager` plugin than the one Web Manager provisioned. Restart the `beets` container.
* If these fields show as `unknown`, Beets is running a plugin older than 1.6.0. Restart Beets so it loads the provisioned plugin.

### 10. fpcalc is reported missing although chroma is enabled
* **Embedded Beets**: see `fpcalc_missing` in [Embedded Beets self-test codes](#embedded-beets-self-test-codes). The rest of this entry is for the two-container stack.
* **Cause**: from plugin 1.6.0, Beets reports whether the `fpcalc` binary is on its own `PATH`. The `chroma` plugin can load without it, but fingerprinting then fails.
* **Fix**: use the stock LinuxServer Beets image, which ships `fpcalc`, or install chromaprint in your Beets image. Web Manager itself never needs `fpcalc`.

### 11. Web Manager stops at startup with "cannot take ownership of /web-manager-data" after a `PUID`/`PGID` change
* **Cause**: Web Manager keeps `/web-manager-data` private (mode 0700, owned by `PUID`:`PGID`). The hardened Compose files (`docker-compose.full.yml`, `examples/docker-compose.external-beets.yml`) drop all capabilities except `CHOWN`, `SETUID` and `SETGID`. Without the capability to read other users' folders, the startup step cannot read a private folder that still belongs to the old `PUID`:`PGID`, so it cannot give the folder to the new IDs. `docker-compose.yml` keeps the default capabilities and is not affected.
* **Fix**: give the folder to the new IDs once, then start the container again. Use your new `PUID`/`PGID` values in place of `1001:1001` below.
  * Bind mount (a host folder): `sudo chown -R 1001:1001 /path/to/web-manager-data`
  * Named volume: `docker run --rm -v <volume-name>:/d alpine chown -R 1001:1001 /d`

### 12. A tag, genre or MusicBrainz ID edit fails with `WRITE_FAILED`
* **Cause**: Beets saved the change in its library, but could not write the tags into the music file. Usually the file is not writable by the user the `beets` container runs as (`PUID`:`PGID`). A common reason is files imported as root, for example with `docker exec beets beet import` without `--user abc`. Beets logs the exact error (`error writing ...`) in the `beets` container log.
* **Effect**: the transaction is marked Failed. The library database already holds the new value, while the file keeps its old tag. The Failed transaction (item or album edit) can still be rolled back from the Transactions page.
* **Fix**: first make the music files writable by `PUID`:`PGID` (for example `sudo chown -R <PUID>:<PGID> /path/to/music`). Then either apply the same edit again so Beets rewrites the tag, or roll the transaction back to restore the old values. Fix the ownership before rolling back: a rollback also writes the tags, so on a file that is still not writable it cannot complete. When running Beets commands by hand in the container, use `docker exec --user abc beets beet ...`.

### 13. A download disappeared from `/downloads` after an import
* **Cause**: the file failed Music Format Preferences. By default mono, 5.1, 7.1, WAV, ALAC, Opus/Vorbis and any file ffprobe cannot inspect are rejected. With the default "Rejected downloads: Quarantine", a rejected file under `/downloads` is moved out of its folder to `MUSIC_FORMAT_QUARANTINE_DIR` (default `/config/music_format_quarantine`, which is the Beets config folder on the host in the shipped Compose files). With "Delete", it is deleted. A preserved torrent source (a folder under `TORRENT_SOURCE_ROOTS` that Web Manager did not create) is never moved or deleted: the log says `Rejected download left in place (...)` and the file is still where it was.
* **Check**: the import job's log shows `Rejected download: <reason>: <file>` and `Rejected download quarantined: <path>`. Look in `<quarantine dir>/<YYYYMMDD>/`.
* **Fix**: move the file back yourself, then change Settings > Music Format Preferences so that the format or channel layout is accepted, and import again. The move is not a library transaction, so the Transactions page cannot roll it back. See [Music Format Preferences and rejected downloads](CONFIGURATION.md#music-format-preferences-and-rejected-downloads).

### 14. "Move to library" left the album in `/downloads` too, or was refused with `plugin_outdated`
* **Cause**: the album is in a preserved torrent source (a folder under `TORRENT_SOURCE_ROOTS` that Web Manager did not create). Its files are never moved, so the torrent keeps seeding: Beets hard links them into `/music` (same file, two names) or, when `/downloads` and `/music` are on different filesystems or Docker volumes, copies them. Beets then tracks the copy in `/music`. With webmanager plugin older than 1.15.0, such a move is refused and nothing changes.
* **Check**: the transaction log says `Preserved torrent source left in place: N track(s) hard linked and M copied`. `stat` on a hard-linked track shows the same inode as the torrent file and a link count of 2.
* **Fix**: nothing to fix. A copy uses extra disk space. To get hard links, mount one folder that holds both the downloads and the music folders once into each container (for example `/data`, with `/data/downloads` and `/data/music`): two separate mounts count as different filesystems even on the same disk. Mount the downloads folder at the same path in both containers, or the torrent-source rule cannot recognize it. A tag write never changes the torrent's file: Beets first gives the library file its own copy, so `stat` shows a link count of 1 afterwards. Rolling the transaction back removes only the library files the move made; if it was more than 7 days ago (or more than 1000 plugin operations since), the rollback refuses with `relocation_record_missing` and the album stays in the library with the torrent originals untouched; if the file was retagged by `beet write` from the command line, it refuses with `library_file_changed`. In both cases nothing changes; remove the library copies by hand if you no longer want them. For `plugin_outdated`, restart the `beets` container so it loads plugin 1.16.0. Set `ALLOW_TORRENT_SOURCE_MOVE=1` only if moving seeding files is what you want.

### 14a. A transaction says Recovery Required, or a retry says `resource_busy`
* **Cause**: Web Manager or Beets stopped while the transaction was running, so whether it changed the library cannot be proven. The transaction log ends with a `Recovered after restart: ...` line that names what is known. A retry right after a Web Manager crash can also get `resource_busy`: the crashed attempt's lock expires within about 2 minutes.
* **Fix**: read the last log lines. For an album move (`Beets never reported where the tracks went`), run Move to library (or Rename) on the album again: Beets moves only what is not yet in place; this move cannot be rolled back. For a Clean All folder cleanup or an import-review cleanup, check the folders named in the log, then run Clean All again: it re-scans instead of re-applying. For an interrupted album move rollback, retry the rollback: Beets resumes from the recorded paths. For a library sync (MBSync All), cancel the Beets operation from the transaction if it is still running. Nothing is replayed automatically.

### 14b. A file named `.<name>.<random>.beets` is left in `/music` or `/downloads`
* **Cause**: Beets was killed while it copied a file across filesystems (an album move, its rollback, or an import). Beets copies to this temporary name first and renames it when the copy is complete, so the file is an incomplete copy. The original is still where it was.
* **Fix**: check that the album's tracks play, then delete the temporary file by hand. Web Manager never deletes it, because removing a file is a destructive action. It also keeps an otherwise empty folder from being removed.

### 14c. After "Move to library", an empty album folder is left in `/downloads`
* **Cause**: Beets removes emptied folders only inside its library directory. The source folder in `/downloads` is not part of the library.
* **Fix**: delete the empty folder by hand, or let your download client remove it. A preserved torrent source keeps its files and is never emptied.

### 14d. A staging move or delete is refused with "Refusing to move onto an existing or symlinked target" or "contains another mounted filesystem"
* **Cause**: since 0.4.0 a staging move never replaces a file or folder that appeared at the target after it was checked, and a staging delete never descends into a folder that is a mount point. A downloads or import root configured through a symlink (`DOWNLOAD_PATH`, `BEETS_IMPORT_ROOTS`) is refused in its symlink spelling.
* **Fix**: remove or rename the existing target and retry; unmount or move the mount out of the staging folder; configure the resolved (real) path for the staging roots.

### 15. Plex, Lidarr, slskd or another service is "blocked by the outbound network policy" (or "blocked by outbound URL policy")
* **Cause**: the service's address is not on the public internet, and nothing admits it. Each service trusts only its own configured URL (scheme, host and port exactly), and only when every address it resolves to is a private LAN (10/8, 172.16/12, 192.168/16), CGNAT (`100.64.0.0/10`) or IPv6 ULA (`fc00::/7`) address. Common reasons: the request goes to a different host, port or scheme than the configured URL (for example `http://plex:32400` configured but `https://192.168.1.20:32400` requested); the URL resolves to loopback (`localhost`, `127.0.0.1`), which is never trusted implicitly; or the endpoint redirected to another origin.
* **Check**: the error and the provider row on the System page name the exact entry that would admit it, for example `add 192.168.1.20:32400 to BEETS_OUTBOUND_ALLOWLIST or set PLEX_URL to it`. The Web Manager log has a `blocked outbound request ...` line with the host and port (never credentials or query strings).
* **Fix**: set the service's URL (`PLEX_URL`, `LIDARR_URL`, `SLSKD_URL`, `QBITTORRENT_URL`, `YTDLP_PO_PROVIDER_URL`, `AI_BASE_URL`) to the exact address the app should call, or add the named `host:port` to `BEETS_OUTBOUND_ALLOWLIST` in the Compose `.env`/`environment:` and recreate the container. The allowlist is operator-only and cannot be changed from the System page. Cloud-metadata, link-local, unspecified, multicast and reserved addresses are refused even when listed, and a catch-all CIDR such as `0.0.0.0/0` is ignored (logged at startup). See CONFIGURATION, *Operator-only settings*.

### 15a. An import, missing-track fill or match went to Review with "AcoustID not configured" or "fingerprints were not checked"
* **Cause**: Web Manager acts on album identity only with deterministic evidence. When AcoustID has no key, rejects the key, is unreachable or rate limited, or `fpcalc` is missing, the tracks are *not checked*, which is not the same as a match. A Release Group match backed only by titles and durations, and any missing-track fill that would be merged into an existing album, then goes to Review instead of being applied. A MusicBrainz text search without a folder to compare against never sets album identity either.
* **Check**: the job log or candidate reason says `acoustid_unavailable`, `AcoustID not configured: set ACOUSTID_API_KEY`, or `AcoustID lookup failed (...); fingerprints were not checked`. Suggestion responses show `acoustid_status` (`not_configured`, `auth_failed` or `lookup_failed`) and `musicbrainz_unavailable`.
* **Fix**: set `ACOUSTID_API_KEY` (see CONFIGURATION), make sure `fpcalc` is present, and re-check the item; or confirm the match yourself in Review. Until then, a missing-track fill stays as a separate album row in the same Release Group; nothing is deleted.

---

## Operational Diagnostics

### 16. Plex status or a Plex playlist sync says "Could not reach Plex", "Plex token is invalid or expired" or "blocked by the outbound network policy"
* **Where**: System page, Integrations (the Plex connection test and badge); `GET /api/plex/status`; the playlist's Plex sync result. Each carries a `reason` code next to the message.
* **`outbound_not_allowlisted`** ("Plex is blocked by the outbound network policy. To allow it, add <host:port> to BEETS_OUTBOUND_ALLOWLIST."; the System badge shows "Blocked by outbound policy"): Web Manager refused to call that address. It is not a network fault. See section 15: point `PLEX_URL` at the exact private address the app should call, or add the named entry.
* **`unreachable`** ("Could not reach Plex."): the request was allowed but nothing answered: wrong host or port, Plex is down, the name does not resolve inside the Web Manager container, or a firewall drops it. Check `PLEX_URL` from inside the container network (`http://<plex-host>:32400/identity` should answer).
* **`auth_failed`** ("Plex token is invalid or expired."): set a current `PLEX_TOKEN`. **`timeout`**: Plex answered too slowly; it is often busy scanning. **`not_configured`**: Plex needs both a URL and a token, from `PLEX_URL`/`PLEX_TOKEN` or the Beets `config.yaml` `plex:` stanza; playlist sync skips Plex until both are set.
* `PLEX_URL` must be a plain `http://` or `https://` address without `user:pass@`, a query (`?X-Plex-Token=...`) or a fragment; the System page refuses to save one. Put the token in `PLEX_TOKEN`. Status responses never show the token or URL credentials.

### 16a. A playlist sync shows pending tracks, "Plex playlist update did not finish", or 409 `job_already_running`
* **Pending tracks**: each has a reason. "not in the Beets library": download or import the track. "file exists but Plex has not indexed it": let Plex finish scanning (or refresh the library), then sync again.
* **"Plex playlist update did not finish"**: Plex failed partway through adding, removing or moving entries. Sync again: the next run continues from what Plex now holds, and the playlist keeps its Plex ID.
* **409 `job_already_running`**: a playlist sync (manual or automatic) is already running; wait for it on the Jobs page.
* **A playlist was skipped with `pipeline_running`**: its download/import job is running; the next sync picks it up.
* **A track came back after being removed in Plex**: remove it in Web Manager instead. The Web Manager list is the master copy; Plex-only removals are not applied.
* **A playlist from an older version is missing**: it is imported once from `MUSIC_ROOT/playlists` (or a hand-placed `.m3u` in `WEB_MANAGER_DATA_DIR/playlists`) when the Playlists page loads or a sync runs, unless Web Manager already has a list with that name.

### 16b. slskd says "HTTP 401" / "slskd rejected the API key"
* Set `SLSKD_API_KEY` (or `SLSKD_API_KEY_FILE`) to a key slskd accepts. A cancelled or failed playlist track cancels its queued slskd transfer and removes only the partial files proven to belong to it.

### 16c. "qBittorrent rejected the login"
* Check `QBITTORRENT_USERNAME` / `QBITTORRENT_PASSWORD` (or their `_FILE` variants). The refusal is recorded as `authentication_error` for `qbittorrent` in `GET /api/providers/health` and in the qBittorrent status `health` field.

### 16d. YouTube downloads fail with "need a supported JavaScript runtime ... Deno/Node/QuickJS was not found"
* **Cause**: yt-dlp solves YouTube's signature challenges in a JavaScript runtime. Since 0.6.0 the Web Manager image ships Deno at `/usr/local/bin/deno` (cache in `DENO_DIR=/tmp/deno`), so this means `YTDLP_JS_RUNTIMES` was set to a runtime that is not present, or a custom image dropped Deno.
* **Check**: `docker exec beets-web-manager deno --version`.
* **Fix**: unset `YTDLP_JS_RUNTIMES` (default `deno,node,quickjs`) or point it at a working executable (`deno:/usr/local/bin/deno`), and recreate the container. The challenge solver scripts come from the bundled `yt-dlp-ejs` package; leave `YTDLP_REMOTE_COMPONENTS` empty so nothing is downloaded at runtime.

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

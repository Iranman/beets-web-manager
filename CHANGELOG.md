# Changelog

All notable changes to this project will be documented in this file.

The project uses Semantic Versioning.

## Unreleased

### Upgrade Notes
- `scripts/backup.sh` and `scripts/restore.sh` changed. They now default to the stack folders `./beets` and `./web-manager` (or `--beets-config`/`--web-manager-data`/`--out`) instead of the container path `/config`, and the archive layout has `beets/` and `web-manager-data/` folders. `restore.sh` still reads archives from the old script.
- The TrueNAS rollout script (`scripts/deploy_truenas_web_manager.sh`) now restarts the `beets` service when a release changes the webmanager plugin version, and fails a deploy that adds a new setup blocking reason. Its backups now hold Web Manager state and the Beets config; their diagnostic copies of the container environment are redacted.
- On first start, Web Manager removes host-side Compose values (`MUSIC_PATH`, `DOWNLOADS_PATH`, `BEETS_CONFIG_PATH`, `WEB_MANAGER_DATA_PATH`), Compose-pinned values (`PUID`, `PGID`, `TZ`, `WEBCONTROL_PORT`), container paths and retired keys (`PLAYLIST_DIR`, `BEETS_SQLITE_TIMEOUT`, `WEB_MANAGER_PATH`) from the saved settings file `/web-manager-data/.env`. A backup `.env.bak-migration-<timestamp>` is written next to it first. The System page and `/api/setup/status` (`settings_migration`) report how many keys were removed. Only key names are logged.
- The System page can no longer save `PUID`, `PGID`, `TZ`, `WEBCONTROL_PORT`, container paths (`BEETS_CONFIG`, `BEETS_LOG`, `MUSIC_ROOT`, `DOWNLOADS_ROOT`) or host paths. Every shipped Compose file sets these, so a saved value never took effect. Set them in Compose and recreate the container.
- Startup now adds `include_paths: yes` to the Beets `web:` block when the key is missing, so Beets returns file paths. An explicit `include_paths: no` is left alone and reported by setup (see Added). Restart the `beets` container once after upgrading so it loads the `webmanager` plugin 1.6.1.
- Startup no longer adds the recommended feature plugins to an existing `config.yaml`. Plugins added by earlier versions stay. To add the rest, use the recommended-plugins preview (see Added).
- If you saved edits from the Config page on v0.1.49 or earlier, those edits never reached `/config/config.yaml` (see Fixed). Apply them again.
- `docker-compose.yml` now forwards `BEETS_WEB_URL` and `BEETS_OUTBOUND_ALLOWLIST` from the Compose `.env` (defaults unchanged). It also sets `MUSIC_ROOT`, `DOWNLOADS_ROOT` and `BEETS_CONFIG` explicitly, and accepts the older host-path names `MUSIC_LIBRARY_PATH`, `DOWNLOAD_PATH` and `BEETS_WEB_MANAGER_DATA_PATH` as fallbacks.
- **AcoustID needs your own key.** The built-in fallback client key is gone. Set `ACOUSTID_API_KEY` (or the legacy `ACOUSTID_KEY`) to your own application key from https://acoustid.org/new-application. Without it, fingerprint lookups report `not_configured`: the evidence is unavailable, which is not the same as "no match", so ambiguous identities go to review. Setup shows "AcoustID not configured".
- **Outbound allowlist entries are now required for LAN, Tailscale and other non-public addresses.** An operator-configured service (Beets, Plex, Lidarr, slskd, qBittorrent, the PO provider, the AI provider) whose host resolves to a private, loopback, CGNAT/Tailscale (100.64.0.0/10), benchmarking or documentation address must be covered by a `BEETS_OUTBOUND_ALLOWLIST` entry. The documented `CIDR:port` form (for example `192.168.1.0/24:32400`) and `[IPv6]:port` / `[IPv6-CIDR]:port` entries now actually parse. A malformed entry is logged at startup and fails closed.
- **Sign-in rate limits now apply before the password is checked.** While a client IP, or the new account-wide bucket, is limited, even a correct password gets HTTP 429 until the window passes. The account bucket is set with `BEETS_AUTH_ACCOUNT_RATE_LIMIT` / `BEETS_AUTH_ACCOUNT_RATE_WINDOW` (default 100 failed attempts per 300 s across all IPs). Signed-in sessions and bearer-token clients are not affected.
- **`X-Forwarded-For` is read right to left** from a trusted proxy, skipping trusted hops. A reverse proxy that appends the client address (nginx `$proxy_add_x_forwarded_for`, Traefik, Caddy) works unchanged. A proxy that overwrites the header with a client-supplied value is no longer trusted for the leftmost entry.
- **Setup probes no longer reuse stored keys for a URL you type in.** `/api/setup/test/ai` and `/api/setup/test/plex` use the stored `OPENAI_API_KEY` / `PLEX_TOKEN` only for a signed-in caller testing the configured endpoint. To test a different URL, send the key with it, as the setup wizard already does.
- **Playlist URL import accepts only supported media hosts.** `POST /api/playlist/parse` with `source=url` now rejects URLs that are not on yt-dlp's allowlisted public media hosts (YouTube, SoundCloud and the other listed hosts), with a clear message. `ytsearch:` / `scsearch:` queries are unchanged.
- **Public URL fetches have a total deadline.** `BEETS_OUTBOUND_TOTAL_TIMEOUT_SECONDS` (default 60) bounds a whole reference-URL or artwork fetch, including redirects and the body read. A TLS certificate failure is now final and is no longer retried.
- **The setup env API shows a fixed `********` for configured secrets** instead of the first and last characters and the length. Use the password-confirmed reveal to see the value.
- **No-audio folder cleanup no longer scans `/tmp`, `/data/downloads` or `/download`.** It covers the music root, `DOWNLOADS_ROOT` and the `DOWNLOAD_PATH` mount.
- **Container image:**
  - `PUID` or `PGID` set to `0`, or to a non-numeric value, is refused at start with exit code 64.
  - The image no longer contains `git`, `pip` or `tests/`. `docker exec ... pip install` no longer works; build a derived image if you need extra packages.
  - In the hardened compose files (`docker-compose.full.yml` and the external-Beets example), `cap_drop: [ALL]` now adds back `CHOWN`, `SETUID` and `SETGID`. Without these, both files crash-looped at start. After the user switch, the app runs non-root with no effective capabilities, and `no-new-privileges` stays on.
  - On the read-only root filesystem, a custom `PUID`/`PGID` runs as that numeric uid:gid instead of remapping the built-in user. `/config` is chowned only when it is a mount, so the external-Beets example now starts.
- Web Manager no longer deletes audio files inside the music library except through an album cleanup the operator explicitly confirms with `DELETE ALBUM FILES` (see Security). Track removals and proven duplicates go to a restorable quarantine instead. Deletion outside the library is limited to the staging/download roots (no-audio folders, rejected preview-length downloads). Several endpoints now need an explicit confirmation or a preview's ids; requests without them are refused with a `code` (`confirmation_required`, `planned_ids_required`, `empty_selection`, `not_preview`, `requires_review`, `music_root_not_allowed`). Frontend flows that called these endpoints without those fields will show the refusal until they are updated.
- `POST /api/transactions/<id>/approve` now refuses an album-cleanup plan that deletes files unless the body has `confirm_delete_files: "DELETE ALBUM FILES"` (400 `confirmation_required`). In Library Changes, Approve on such a plan opens a dialog that stays disabled until that phrase is typed exactly; refusals and lost state races (409 `not_preview`) show what failed and what to do next. Row-only plans approve as before.
- Clean All's folder-safe renames run again, now through the transaction engine (`folder_cleanup_v1`, one audited transaction per folder, confined to `MUSIC_ROOT`). The import template pre-rename and the import Step 0b orphan pre-cleanup are no longer performed; they log `not_supported` and the import continues without them.
- The Web Manager data directory (`WEB_MANAGER_DATA_DIR`), `transactions.db`, any `*.db` file and the backup directories can no longer be deleted or moved by the staging-file helpers, even when they sit under a staging root.
- Internal playlist media cleanup (after a playlist import or library-wide removal) now removes Beets rows only and keeps the files; it needs an Approved transaction and runs once.

### Fixed
- Library Changes: the Rollback button is enabled only when the server can accept the rollback (Completed, or a Failed, Recovery Required or Partially Rolled Back transaction with an engine apply record, so an engine-backed rollback can be retried), not for Preview, Approved, Cancelled, Running or Rolled Back transactions, or ones that never applied. A refused or failed rollback now reloads the transaction and list like Cancel and Apply. A successful rollback with no transaction in the response reloads it instead of closing the detail pane, and says "Rollback finished." (or names the rollback job). A 409 refusal says nothing was changed; a timeout or 5xx says the outcome is unknown.
- **Rollback is refused unless the transaction was applied (#219).** `POST /api/transactions/<id>/rollback` for a Metadata Update or MusicBrainz Match used to restore the captured old values for a Preview, Approved or Cancelled transaction that never wrote anything, overwriting the item and any edits made since. It now claims the transaction Completed -> Running with a compare-and-set and returns 409 with `mutated: false` for any other status. A Failed transaction qualifies only when it carries an engine apply record (`metadata.engine_result`), which these families do not write.
- `POST /api/transactions/<id>/apply` now gives every engine family (item replacement, track quarantine, reviewed duplicate cleanup, album row merge, untracked recovery) the same controlled errors as album cleanup (#220): a held lock or lock timeout returns 409 `resource_busy` (`mutated: false` while the transaction is still Approved), Beets unavailable returns 503, and anything else returns a fixed 500. `POST /api/transactions/<id>/rollback` for these families has its own mapping: a lock conflict returns 409 `resource_busy` (`mutated: false` only for album row merge and untracked recovery, whose rollbacks take their locks before writing), Beets unavailable returns 503, and anything else returns a fixed 500 `rollback_failed`. Unexpected failures are logged with the message and traceback redacted. No response carries exception text. A metadata apply whose job cannot be started now returns a fixed 500 instead of echoing the error with a 409.
- `docker-compose.full.yml`: the default `BEETS_OUTBOUND_ALLOWLIST` now includes `bgutil-provider:4416`, so the bundled yt-dlp PO token provider is no longer blocked by the outbound policy out of the box.
- **Library Changes shows refused Cancel and Apply requests.** A 400, 404 or 409 from cancel or apply (for example `not_cancellable` on a Running transaction) used to fail silently. The page now shows the server's reason and reloads the transaction and the list. If Apply fails after changing files, or with an unknown outcome (server error or timeout), the page says the library may be partly changed and tells you not to apply again. Cancel is enabled only for Pending, Preview and Approved transactions.
- A non-numeric or non-positive `BEETS_OUTBOUND_TOTAL_TIMEOUT_SECONDS` no longer stops Web Manager from starting. It logs a warning and uses 60 seconds (#186).
- **Approved album cleanups apply through the generic route (#187 F-4).** `POST /api/transactions/<id>/apply` returned 409 for an approved `album_cleanup_v1` plan. It now runs the album cleanup apply. Rollback through the generic route reports `not_supported`, because album removal has no rollback.
- **Row-only album cleanup failures no longer claim files were deleted.** When a row-only album cleanup (files kept) failed after it had started, the error said files "were already deleted". It now says library rows were partly changed and no audio files were removed. Plans that delete files keep the previous warning.
- The Library album cleanup dialog (opened by the album's "Remove from Library…" button, previously "Delete Album…") no longer says it deletes track files. Its plan never sends `delete_files`, so it removes the album and track rows from Beets and keeps the files on disk. The title, warning, before/after summary, completion summary and rollback text now say so (#184).
- The album cleanup review step now shows what the plan actually contains: the album from the plan and its track list (number, title, path), taken from the plan's `items`. It used to read fields the plan never returns, so it showed an empty "Target Album Directory:" and "Proposed Mutations (0 steps)". A plan whose `delete_files` is true is no longer offered for Apply there; the dialog points to the typed `DELETE ALBUM FILES` approval in Library Changes. The never-reachable rollback and quarantine displays are removed.
- The `DELETE ALBUM FILES` approval dialog in Library Changes now exposes its consequence text as the dialog's accessible description (`aria-describedby`), so screen readers announce it (#187 F-7).

- **Rollout `--rollback` now actually rolls back (RD-5).** It used to report "Rollback complete" while the new version kept running: the image override was a process substitution whose errors were discarded, the fallback recreated from the `.env` that the deploy had already moved to the new version, and nothing checked the result. Rollback now restores the `BEETS_WEB_MANAGER_VERSION` line in the stack `.env`, recreates through a temporary override file, and fails unless the running image ID is the recorded previous one, `docker compose config` resolves to it (so a later `docker compose up -d` keeps it) and `/health/live` reports the previous version.
- **Deploys fail on a new setup blocking reason (RD-6).** `/api/setup/status` is recorded before the deploy and compared after; a new blocking reason fails the rollout with the rollback command, before the version is written to `.env`. Previously a deploy passed as long as every endpoint answered 200.
- **Rollout backups include the state a new version can change (RD-7).** Web Manager's Settings `.env`, browser login files, Flask session key, setup markers and `transactions/`, plus Beets `config.yaml` and `beetsplug/`, are backed up (mode 600) after the web manager stops, and restored on rollback. Replaced files are kept under `pre-rollback-*/`, and audit records written after the deploy are never removed. The library database is never copied.
- **A changed webmanager plugin is loaded (RD-8).** Beets keeps the plugin it imported at start-up, and the rollout only recreated the web manager, so a new plugin version stayed inactive until someone restarted Beets. The rollout now compares the provisioned plugin version with the one the engine reports and, when they differ, restarts only `beets` (same container) with library snapshots before and after.
- **`docker-compose.yml` follows `BEETS_WEB_MANAGER_VERSION`.** It hard-coded `ghcr.io/iranman/beets-web-manager:stable`, so the version the rollout script and `--rollback` write to the stack `.env` was ignored by a plain `docker compose up -d`. The image is now `ghcr.io/iranman/beets-web-manager:${BEETS_WEB_MANAGER_VERSION:-stable}`, like the other Compose files; `.env.example`, the README, `INSTALLATION.md`, `EXAMPLES.md` and `TROUBLESHOOTING.md` match, and a test keeps them in step.
- **Rollback restores `beetsplug/` exactly.** Files that the new version added to the Beets plugin folder used to stay behind after `--rollback`; the folder is now emptied (after its current contents are kept under `pre-rollback-*/`) and refilled from the backup, without following symbolic links.
- **A failed `docker compose stop` during rollback is reported.** It was ignored silently; it is now logged and the rollback continues, with the running image ID and version proof deciding whether it succeeded.
- **Backup names and restore stamps are in UTC.** `backup.sh` archive names, the `created=` line in `MANIFEST.txt` and the `.pre-restore-<timestamp>` folders now use UTC (marked `(UTC)` in the manifest) instead of the host's local time.
- **`backup.sh` backs up Web Manager state and works on a Compose host (QA-9, RD-14).** It used to fail with its default `/config` path, and it never included `/web-manager-data`. It now copies the database with SQLite's online backup API (`sqlite3` or `python3`, read-only), so writes still in the WAL are included while Beets runs; without either tool it requires `--beets-stopped`. `restore.sh` refuses unsafe archives and a running Beets, and moves replaced files aside instead of overwriting them.
- Security: the Beets config editing added in this release is hardened. Parsing a `config.yaml` with many blank indented lines followed by a CRLF line no longer hangs (regex backtracking), and neither does an `include_paths:` line with a long run of spaces. Rewriting `config.yaml` keeps its file permissions (a `0600` file stays `0600`) and uses a unique temporary file. The include-paths and recommended-plugins routes now refuse a `BEETS_CONFIG` outside the Beets config directory, including through a symlink.
- Security: Beets `config.yaml` backups are now created with a unique name (no overwrite when two edits land in the same second) and `0600` permissions, API responses report only the backup file name rather than its full path, and the startup/provisioning plugin migration refuses to rewrite `config.yaml` if the backup cannot be made.
- **The Beets config editor showed an empty file and saved to the wrong place.** The first Settings save copied the whole `.env.example` template into `/web-manager-data/.env`, including the host-side `BEETS_CONFIG_PATH=./beets`. The editor read that variable as the container path of `config.yaml`, so after a restart `GET /api/config` returned an empty config. `POST /api/config` then reported success while writing `/app/beets` inside the container instead of `/config/config.yaml`.
  - Settings saves now write only the keys that were saved, and startup loads only application settings from that file.
  - The editor uses `BEETS_CONFIG` (default `/config/config.yaml`), never `BEETS_CONFIG_PATH`. It refuses a relative path or a file outside the Beets config directory, and it reports a missing file as an error instead of an empty config.
- **False "Cannot write to downloads/staging path downloads" warning.** Setup checked the host-side `DOWNLOADS_PATH` as if it were a container path. It now checks `DOWNLOADS_ROOT` (default `/downloads`).
- **A Beets outage no longer produces a list of false local failures.** While stock Beets was unreachable, setup status and `/health/ready` also reported that the config directory, music library, downloads and `config.yaml` were inaccessible, and that fpcalc was missing. Local mounts are now checked locally whether or not Beets answers. "Stock Beets is unavailable" is the single primary reason, and fpcalc is reported as `unknown`.
- The Beets `config.yaml` path report no longer shows `ok: false` for a healthy file. It used a directory check on a file.
- The Beets adapter's default URL is `http://beets:8337` everywhere. The adapter used to fall back to `http://127.0.0.1:8337`, which is Web Manager itself.
- **Clean All folder-safe renames work again.** LT-13 confined `move_file` to staging roots, which silently broke Clean All's in-library folder renames. They now go through `safe_rename_library_folder` (engine `folder_cleanup_v1`), which is confined to `MUSIC_ROOT`, refuses existing targets and records an audited transaction.
- **Import review cleanup reports honestly (LT-12).** `apply_import_review_cleanup` reported success when row removal failed. It now marks the transaction Failed and reports which rows were not removed. Missing-file sync and orphan cleanup also mark their transaction Failed when Beets raises.
- **The library-path prefix check no longer matches sibling folders.** `/music2` is no longer treated as inside `/music`, and folder removal no longer ignores errors.
- **The MusicBrainz sync prune removes album rows only** (`delete_album(delete_files=False)`).
- **Playlist staged-track delete reports honestly.** `delete_playlist_staged_track` returned success without deleting anything when the path was outside staging. It now goes through the staging-root check and returns `ok: false` with the reason when it refuses or fails, and `deleted: true` only after the file is removed.
- **Import review cleanup accepts files in the configured staging roots** as well as the import cleanup roots. The approval is recorded through the plan's `approved_by` when it is applied, instead of a separate route-level approve step.
- **The library health report works again (LT-18).** `get_library_health()` was called with keyword arguments it did not accept. `GET /api/clean/library-health` and Clean All's first step failed with a TypeError. It now builds the report from live Beets reads. Missing files are reported only when the music root is usable.
- **Album maintenance no longer reports work it did not do (LT-3).** Every mode (remove tracks, deduplicate, filename cleanup) used to fall through to a relocation (`move`) and record Completed. Only removing an empty album row is implemented. Other modes are refused with `not_supported` and the transaction is marked Failed. `delete_album` removes only empty album rows.
- **Helpers that crashed or faked success now fail honestly (LT-18, LT-12).**
  - `run_command("mbsubmit")` reported "completed" without contacting Beets. The item/album "mbsubmit" jobs now fail with `not_supported`.
  - `get_job()` returned success for any id; it now reads the engine's operation registry. `cancel_job()` no longer claims a cancellation the engine cannot do.
  - `POST /api/library/move-all` and `POST /api/library/mbsync-all` called helpers with arguments they did not accept. They now fail with `not_supported` before changing anything; a library-wide move or MusicBrainz rewrite has no plan or rollback yet.
  - `replace_album_art()` (used by artwork upload/URL replace) wrote into the album folder with no audit and was called with unsupported arguments. It now returns `not_supported`, and the artwork routes report "Could not update album artwork" as before.
  - `POST /api/albums/<id>/move-to-library` now relocates through the album relocation family.
  - `create_hardlink()` (torrent re-seed linking) accepts the expected size and links only into staging roots.
  - A new test checks that every `composite_workflows` call site binds to the real signature. Eleven pre-existing unbound call sites (matching, playlist and import flows) are listed in the test as a shrink-only baseline for a later wave.
- **Startup no longer adds feature plugins or rewrites settings in an existing Beets `config.yaml`.** Provisioning used to append the whole recommended plugin set and could rewrite an existing `replaygain` backend. It now ensures only `web`, `webmanager`, `pluginpath: /config/beetsplug` and a missing `web.include_paths`. The legacy-config repair now drops only `plexsync`.
- **Setup no longer reports fpcalc available when it is not installed in Beets.** With plugin 1.6.0 the `chroma` capability is combined with a real `fpcalc` probe in the Beets container. A missing `chroma` on an older plugin still blocks fingerprinting.
- **Path-less Beets responses no longer look like an empty library.** When Beets returns items without paths (`web.include_paths` off), the adapter raises `BeetsAdapterPathsUnavailableError` (`BEETS_PATHS_UNAVAILABLE`, HTTP 503) instead of returning an empty path list.

### Security
- Every `config.yaml` writer now refuses a `BEETS_CONFIG` outside `BEETSDIR`: plugin provisioning (`/api/plugins/provision`), startup auto-provisioning and the startup legacy-config repair resolve the path through the same check as the config editor. These edits read `config.yaml` once without following symlinks (a symlinked `config.yaml` is refused) and replace the file atomically; the plugin-provisioning and opt-in edits back up exactly the bytes they read. Provisioning replaces a symlinked file in `beetsplug/` instead of writing through it, and refuses a symlinked plugin package directory.
- `webmanager` plugin 1.6.1: derived allowed mutation roots never include `/`, the Beets config directory or one of its ancestors. A Beets `directory` like that falls back to `/music` and `/downloads`, and such an `import_roots` entry is skipped. An explicit `webmanager.allowed_roots` or `BEETS_ALLOWED_ROOTS` is still used exactly as given.
- The recommended-plugins preview diff (`GET /api/setup/plugins/recommended`) no longer includes unchanged lines, and masks secret-looking values (`*key`, `*pass`, `auth*`, password, passwd, secret, token, bearer, credential; also in flow mappings, block scalars and URL user info) on the changed lines. The masking is a linear scan, not a regex.
- Bump the yt-dlp PO-token provider bgutil-ytdlp-pot-provider 1.3.1 -> 2.0.1 for GHSA-qpv9-8xfj-xx9m (high, remote code execution through the provider HTTP server from a local-network device or a malicious web page). The pip plugin (`requirements.txt` and the `YTDLP_BGUTIL_PIP_PACKAGE` default) and the `bgutil-provider` sidecar in `docker-compose.full.yml` (`brainicism/bgutil-ytdlp-pot-provider:2.0.1-deno`) move together. The sidecar stays reachable only on the Compose network (`expose`, no published port). If you run your own provider container, update it to 2.0.0 or later and do not publish its port on 0.0.0.0. The 2.0 server rejects browser-originated requests and needs JSON request bodies; yt-dlp and the Web Manager status probe are not affected.
- Files that can hold secrets are now created at mode 0600 from the start, instead of being created at the umask mode and then changed: `.env` backups written by Settings saves, the `.env.bak-migration-<timestamp>` backup, the `.env` file itself, the `.env.migration.json` report and the legacy `config.yaml.bak-legacy-plugin-migration` backup. A failed mode change is now logged as a warning instead of being ignored (#183 S-4, #185 N2/N4).
- The setup status and diagnostics no longer show the user name and password in `BEETS_WEB_URL`. The settings save and token routes return the backup file name in `backup_path`, not its full path, and an ignored relative container path is logged by variable name only, not by value (#183 S-8).
- Playlist download matching and MusicBrainz artist-credit parsing no longer use regular expressions that slow down quadratically on long whitespace runs in file names or provider responses (#186). The `" - "`, `"/"` and `feat`/`ft`/`featuring` splits now run in linear time with the same results.
- Bracketed `(produced by …)` credits are stripped from slskd titles again when a line break separates `produced` and `by`, as before the SEC-5 rewrite (#186 N1).
- **Staging delete/move hardening (#182).** The staging delete and move helpers now compare the entry's identity (device, inode, type) recorded at validation with the entry just before `rmtree`/`unlink`/`move`, so a path replaced after validation is refused. A path that was never validated is also refused. A move runs the symlink re-check before it creates any target parent. A folder that contains `MUSIC_ROOT` (for example `/data/media` with `MUSIC_ROOT=/data/media/music`) is no longer accepted as a staging target. Import Review cleanup checks the transaction family before it approves a Preview plan. Playlist media cleanup no longer retries `remove()` without its idempotency key when `remove()` raises an internal `TypeError`.
- **Transaction cancel is a compare-and-set (#187 F-6).** `POST /api/transactions/<id>/cancel` cancels only a Pending, Preview or Approved transaction and returns 409 otherwise, so a cancel that races an apply can no longer overwrite Running, Completed or another final status.
- **An apply can no longer start after a successful cancel (#206 F3/F4).** Claiming an Approved transaction (`claim_approved`, and the Metadata Update apply before its job starts) is now a compare-and-set to Running. Job-status sync (the transaction list and detail polls) only advances a Running transaction, so it can no longer claim an Approved one or turn Cancelled, Rolled Back or another final status into Running, Failed or Completed. A refused claim now names the current status ("no longer Approved (now Cancelled)") instead of "Another attempt already claimed this transaction." A cancel that lands between the status check and the claim wins, and the apply changes nothing. Before, the cancel returned `ok: true` and the apply still ran. Every transaction store on the same directory now shares one lock, so the route's store and the engine's store cannot interleave a status change. The internal approve-then-apply paths (album row merge, item replacement, reconcile duplicate cleanup, manual and unattended duplicate cleanup) approve only a Preview transaction; a Cancelled or Failed one is refused instead of being approved again.
- **Staging deletes and moves can no longer be redirected by a swapped parent directory (#206 F1, F2).** Staging file and folder deletes and moves now work from an fd opened on the staging root. Each path component is opened with `O_DIRECTORY|O_NOFOLLOW`, the entry is checked against the identity seen at validation, and the delete, rename or `mkdir` is relative to that fd. A parent replaced by a symlink after the last check is refused instead of followed, so the operation can no longer reach `MUSIC_ROOT`. A race probe hit the victim 0 times in 1000 attempts per mode, against 10 to 556 on the old code. A refused or failed move now removes the target folders it created, so it no longer leaves empty folders behind. A move between staging roots on different filesystems copies a regular file through fds, fsyncs it and then removes the source. A folder move between filesystems is refused, and nothing is moved. Where the platform lacks fd-relative operations, staging mutations are refused rather than run path-based.
- **A failed staged-track delete no longer returns server paths (#206 F5).** The client gets "Could not delete the staged track file."; the path and reason go to the server log only.
- **The generic apply route handles album cleanup like the album route (PR #204 QA F-B).** `POST /api/transactions/<id>/apply` for an `album_cleanup_v1` transaction returns the same `error_kind` classification as `/api/albums/cleanup/apply`. A held album lock returns 409 `resource_busy`, and an unexpected error returns a fixed 500 message instead of an unhandled exception. Both routes no longer echo exception text.
- Rollout backups no longer keep plain-text copies of every service's environment: the `docker inspect` and `docker compose config` copies keep key names but redact values except for a short allowlist of non-secret keys (RD-20). New opt-in `--prune-backups-older-than DAYS` deletes old rollout backups (never automatically; keeps the newest and any backup holding an archived stale database).
- `restore.sh` refuses archives with symbolic links, hard links or special files (checked before and after extraction), extracts without the archive's owners and permissions, and sets `config.yaml` and `.webmanager_api_key` to mode 600 after restoring them.
- The rollout script (which runs as root) never follows symbolic links when it backs up or restores state: a linked source file or folder is skipped and reported, a linked destination file is replaced rather than written through, a linked destination folder is refused, folders are copied without following links, and a linked auth token path stops the deploy.
- Redacted diagnostic copies also scrub `key=`/`token=`/`secret=`/`password=` values from container commands, entrypoints, labels and health checks and from Compose `command`, `entrypoint`, `healthcheck`, `labels`, `build.args` and `x-*` extensions, and remove credentials embedded in allowlisted URLs (`BEETS_WEB_URL`, `BEETS_OUTBOUND_ALLOWLIST`).
- `backup.sh` no longer builds a `sqlite3 .backup` command from a path containing `'`: it uses the Python online backup for such paths, or stops with an error when `python3` is unavailable.
- The `github-release` CI job checks out with `persist-credentials: false`, so its write-scoped token is not left in `.git/config`.
- `restore.sh` checks every file against the sha256 list in the backup's `MANIFEST.txt` before restoring anything, and refuses a backup with a mismatched or unlisted file, a manifest path outside the backup, or a current-layout backup without a manifest; nothing is touched when it refuses.
- Bump `sharp` override 0.35.4 -> 0.35.5 (GHSA-wq5f-xc86-pv6w, librsvg CVE-2026-96889) and `source-map-js` 1.2.1 -> 1.2.2 (GHSA-68fv-2mgg-jv7q, event-loop DoS) in the frontend lockfile. This clears the `npm audit --audit-level=high` CI gate; `next` stays at 16.3.8.
- **Stored provider secrets could be sent to any host (SEC-1).** During first-run setup, `/api/setup/test/ai` and `/api/setup/test/plex` are public. When the body had no key, they fell back to the stored `OPENAI_API_KEY` / `PLEX_TOKEN` but still honoured a body-supplied URL, so an anonymous caller could make the server send those secrets to a host of its choosing. A stored credential is now used only by an authenticated caller probing the configured endpoint (same scheme, host, port and path). Plex and provider auth headers are also stripped on cross-origin redirects.
- **SSRF through yt-dlp (SEC-2).** yt-dlp opens its own connections, so the outbound URL policy never applied to it, and its generic extractor fetched any URL passed to playlist import, including loopback, cloud-metadata and internal service URLs. Every `YoutubeDL` now takes its options from `backend.ytdlp_guard.ytdlp_guarded_options()`. That function turns off the generic extractor and accepts only search queries or http(s) URLs on allowlisted media hosts whose DNS answers are all public. A structural test requires the guard at every call site.
- **Sign-in limiter did not slow brute force (SEC-3, SEC-4, SEC-11).**
  - SEC-3: the limiter was consulted only after a failed password check, and a correct password was accepted while limited. It now refuses before the scrypt verify for login, Basic auth and reveal re-authentication, and an account-wide bucket stops IP rotation.
  - SEC-11: Basic auth always runs the password check, even for a wrong username.
  - SEC-4: `X-Forwarded-For` is walked right to left, so a client-supplied leftmost entry can no longer claim a LAN address or rotate past the limiter.
- **Outbound allowlist parsing (SEC-6, F6, BA-4).** `CIDR:port` entries were always rejected, because `urlsplit` read `/24:8080` as a path. Entries are now split on the last colon outside brackets, and a malformed entry raises `OutboundPolicyError`. Operator-configured fetches to non-global addresses, including CGNAT/Tailscale and IPv6 forms that embed them, now need an explicit entry, matching the public-URL policy.
- **Public URL fetch robustness (SEC-7, IA-09).**
  - `open_public_url()` now falls back to the next validated DNS answer when the first is unreachable.
  - A certificate verification failure is classified `rejected` and is never retried or tried on another address.
  - A monotonic total deadline means a server that sends data very slowly can no longer hold a worker open.
- **Quadratic regex backtracking (SEC-5).** CodeQL #1291, #1292, #1294, #1295, #1298, #1299 and #1301 had been dismissed as linear. The `(?<!\s)` rewrite that replaced them was flagged again as #1357-#1363, and the slskd bracket-credit strip still took about 30 s on 100k `(ft.`. The leading whitespace quantifiers are gone: `backend/title_normalize.py` now searches only the whitespace-free core of each pattern, or scans by hand, and extends each match back over whitespace in a single pass. Results are identical to the original patterns, which is checked by oracle tests on a corpus and on seeded Unicode/newline fuzz. All of these normalizers now run in linear time (under 0.05 s on 100k-character inputs). The 1024-character input caps remain as defence in depth.
- **AcoustID (IA-12, IA-11).** The shared built-in client key is removed (user decision: require a user key). The key is resolved in one place, `helpers_mb.acoustid_api_key()`. The setup probe maps AcoustID error code 6 and a bare HTTP 401/403 to `auth_failed`.
- **Secret mask (FE-16).** `GET /api/setup/env` no longer reveals the first and last two characters, or the length, of configured secrets.
- **ffmpeg input and cleanup roots (SEC-13).**
  - ffmpeg and ffprobe now receive `file:<path>`, so a crafted path cannot be interpreted as a network URL, another libavformat protocol or an option.
  - `/tmp` and the hard-coded download paths are no longer folder-cleanup roots.
- **Container image (SEC-9).**
  - The node and python base images are pinned by digest, and Dependabot now tracks the `docker` and `pip` ecosystems.
  - Debian security updates are applied at build time. `git` (unused, and its perl dependency carried most CRITICAL findings), `pip` (its vendored packages had fixable HIGH advisories) and `tests/` are removed.
  - The entrypoint refuses UID/GID 0.
  - Trivy, same database: CRITICAL 20 -> 11, HIGH 265 -> 243, fixable findings 7 -> 0, image size 1.17 GB -> 1.05 GB. The remaining CRITICAL findings have no fixed Debian package yet.
- **Hardened compose (BI-8).** See Upgrade Notes. The fix was verified with real `docker compose up` runs of both hardened files, with PUID 1000 and 1001.
- Import-review cleanup decides whether a path is inside the music library by normalized containment (realpath + root-prefix check), so `..` traversal and sibling-prefix paths (`<root>-evil`) are no longer counted as library paths. Errors from album-track preview, album remove (engine offline, now `error_code: ENGINE_OFFLINE`), import-review plan/apply, music-root checks and no-audio folder scan/delete no longer return exception text to the client; details go to the server log (CodeQL py/path-injection, py/stack-trace-exposure).
- **Orphan cleanup no longer deletes media (LT-1).** `POST /api/clean/remove-orphaned-items` and Clean All's Missing Files step called `remove` with `delete_files=True` for any id they were given, without checking that the id was an orphan. An empty id list widened to every singleton in the library. Each id is now re-checked against live Beets and the disk (still tracked, file absent now). Only the Beets row is removed, never a file, with an audit record. An empty list is refused, and nothing is removed when the music root is missing or empty, or when half or more of the rows look missing.
- **A failed import validation no longer deletes library files (LT-17).** When an in-library import failed validation, its rollback removed the album with `delete_files=True` while logging that the files were kept. The same was true of the copy-import cleanup and `_delete_album_ids_from_db`. All three now remove only the Beets rows. A confirmed import now honours copy mode; it used to always move.
- **Album cleanup needs an approved plan and never deletes files by default (LT-4).** `apply_album_cleanup` ran straight from a preview, deleted files, and a second call repeated the delete. It now:
  - requires an Approved transaction, claimed under a durable `album:` lock, and refuses a second apply;
  - refuses a plan whose album changed after planning;
  - removes rows only, unless the plan was created with `delete_files` and `confirm_delete_files="DELETE ALBUM FILES"`.
  `POST /api/albums/cleanup/apply` treats the operator's Apply on a row-only preview as the approval. `POST /api/albums/<id>/remove` now only creates that plan.
- **Removing selected tracks quarantines them instead of deleting (album-tracks, duplicate resolver).** `POST /api/clean/album-tracks/remove`, `/remove-batch` and the duplicate resolver's delete action defaulted to `delete_files=True` and ran without approval. They now need `confirm: true` for a live run (dry run is the default). They use a new `track_quarantine_v1` family: the file's SHA-256 is pinned, the engine moves the file to its quarantine, the transaction is recorded, and rollback restores the tracks. Emptying an album this way is refused.
- **Missing-file DB sync applies only the previewed rows (LT-2).** `POST /api/library/sync-deleted` recomputed "missing" at apply time and could drop every row if the music mount was absent. Apply now needs the `item_ids` its preview returned (`missing_item_ids`), re-checks each one, and refuses when the music root is unusable or half or more of the rows look missing. Removals are recorded. The opt-in legacy auto-scan (`BEETS_ENABLE_LEGACY_LOCAL_SCAN`) now only reports missing rows; it no longer removes rows or album records.
- **Album deduplicate requires proof (MI-3).** `POST /api/albums/<id>/deduplicate` grouped by track number only, so disc 1 track 3 and disc 2 track 3 counted as duplicates. It deleted a copy unless AcoustID positively disagreed, and it deleted unmatched (track 0) items by default.
  - Slots are now (disc, track).
  - A copy counts as a duplicate only when both files fingerprint as CONFIRMED for one recording; unknown evidence spares the copy and reports it.
  - Unmatched items are always kept, and `keep_extras` defaults to true.
  - Proven copies go to the reviewed duplicate cleanup (quarantine, rollback). It is applied only with `confirm: true`; otherwise the plan is left in Preview.
- **Matching an album no longer deletes nonmatching tracks (MI-8).** `POST /api/albums/<id>/match` deleted every local track that did not match the selected release. The job now stops before any change, reports those tracks for review, and fails with `requires_review`.
- **Track integrity scan only proposes removal on strong audio evidence (MI-4).** An AI "remove" suggestion, a title similarity below 0.62, a fingerprint conflict scoring below 80, the low-album-match promotion, and "two files mapped to the same MusicBrainz track" now all produce `review`. Only an AcoustID conflict at a score of 80 or more may propose `remove`.
- **No-audio folder deletion is limited to staging roots (QA-1).** `POST /api/clean/no-audio-folders/delete` could `rmtree` folders inside the music library and reported success even when a delete failed. It now refuses any folder under `MUSIC_ROOT`. It deletes only inside the staging/download roots, with no symlink in the path, after re-checking that the folder has no audio and no symlink. A live run needs `confirm: true`, and failures are reported.
- **`composite_workflows.delete_file` / `move_file` are confined to staging roots (LT-13).** They accepted any path, and `delete_file` ignored errors. They now refuse `MUSIC_ROOT`, a staging root itself, symlinked paths and existing move targets, and they raise on failure.
- **Staging-file helpers protect app data and re-check before acting (S1 F1-F4).** `delete_file`, `move_file`, `delete_staging_file`, `move_staging_file` and the no-audio folder delete now:
  - refuse the data directory, `transactions.db`, any `*.db` file and backup directories, as source or destination;
  - refuse a staging root itself as source or destination (`move_file` included);
  - resolve the path once, then re-check it with `lstat` (same device and inode, not a symlink) immediately before `rmtree`, unlink, move or link, so a path swapped for a symlink in between is refused;
  - detect symlinks in relative paths (they are made absolute first).
- **Approval and rollback check their status transitions (S1 F5).** The approve route requires `DELETE ALBUM FILES` for file-deleting album-cleanup plans. It, the track-quarantine rollback and the duplicate route now report a conflict when the compare-and-set transition loses a race, instead of reporting success.
- **Playlist media cleanup is approved, claimed and rows-only.** `apply_playlist_media_cleanup` deleted files straight from a plan; every caller created a plan and applied it in the same call. It now requires an Approved transaction, claims it under the `item:` locks, refuses a second apply, and removes only Beets rows (files stay; they can be re-imported, so rollback reports `not_supported` honestly). The import, library and playlist callers go through `remove_item_rows_keep_files`, which records who approved each plan before applying it once.
- **Approving a transaction only works from Preview (LT-16).** `POST /api/transactions/<id>/approve` re-opened Completed, Failed, Rolled Back, Cancelled or Recovery Required transactions. It is now a compare-and-set from Preview to Approved; any other status returns 409 `not_preview`.
- **AI-only duplicate suggestions can never become a cleanup pair (O-1).** Manual duplicate cleanup now ignores AI duplicate entries without fingerprint proof. The reviewed cleanup's own AcoustID re-verification was already required.

### Added
- GitHub Releases are created by CI (RD-11): on a `v*` tag, after the image is published, the `github-release` job creates the release with the tag's CHANGELOG section as its body (`scripts/release_metadata.py notes`). An existing release is left untouched.
- A `release-metadata` CI job (required before publishing) fails when `VERSION` and the newest CHANGELOG release heading disagree, or when a tag is not `v` + `VERSION`.
- A versioning policy for `0.x` (when a release is minor rather than patch) in `CONTRIBUTING.md` and `AGENTS.md` (RD-17).
- `webmanager` Beets plugin 1.6.0 (protocol 1.0, additive fields): `/webmanager/status` reports `library_directory`, `library_path`, `allowed_roots`, `import_roots`, `web_include_paths`, `fpcalc_available` and `ffmpeg_available`. When `webmanager.allowed_roots` is not set, it is derived from Beets' `directory:` plus `import_roots`.
- Setup status `warnings` and `actions`: `beets_restart_required`, `beets_web_include_paths_disabled` (with the action `enable_web_include_paths`), `music_root_mismatch` and `downloads_root_not_import_root`. Setup status also reports `restart_required` when Beets runs an older plugin than the bundled one. For plugins older than 1.6.0 the new fields are `unknown`, and no warning is raised from them.
- `POST /api/setup/beets-config/include-paths`, `GET /api/setup/plugins/recommended` (preview with a diff) and `POST /api/setup/plugins/recommended/apply`. The writes require CSRF, take a timestamped backup and report `restart_required`.

### Changed
- `test_real_stock_docker_container_acceptance` retries `docker pull` and `docker run` (3 attempts each, with backoff and a fresh loopback port per run) and puts the last `docker` stderr in the failure message. CI failed twice with `docker run` exit 125 from a registry flake. A failure that persists after the retries still fails the test.
- Dependabot groups `react`, `react-dom`, `@types/react` and `@types/react-dom` into one PR, and ignores odd (non-LTS) `node` image majors.
- Test and CI Beets pins move from `beets==2.13.1` to `beets==2.14.1` (`requirements-dev.txt`, `unit-tests.yml`, `security.yml`, `BEETS_TEST_FIXTURE_VERSION` in `docker-build.yml`), matching `lscr.io/linuxserver/beets:latest` (2.14.1-ls355). Supersedes Dependabot #195, which bumped only `requirements-dev.txt`.
- The Dockerfile frontend build stage uses `node:24-bookworm-slim` (Active LTS, pinned by digest), the same Node major as CI. Its npm 11 accepts lockfiles written by npm 11; npm 10 in the old `node:22` stage failed `npm ci` with `Missing: yaml@2.9.1 from lock file`, which broke the Docker build of Dependabot npm PRs while CI passed. `docs/DEVELOPMENT.md` names Node.js 24.
- `docs/TRUENAS_ROLLOUT.md` describes what the script actually does: it edits the stack `.env` (deploy and rollback rewrite `BEETS_WEB_MANAGER_VERSION`), what a dry run pulls and writes, the full backup contents and the rollback proof. `docs/DEVELOPMENT.md` no longer references the missing `scripts/verify_security_config.py` and describes the image/tag/rollout release flow instead of copying files. README backup, upgrade and rollback sections match the scripts.
- `docs/TRUENAS_ROLLOUT.md` documents a known limitation of the setup-readiness check: blocking reasons have no codes and are compared as exact strings, so a reason whose wording changed between versions counts as new and fails the rollout; it says how to tell that false positive from a real regression.
- The `gh release create` step in the `github-release` CI job has consistent whitespace (no behaviour change).
- Configuration layers (`backend/config_layers.py`, `docs/CONFIGURATION.md`): every variable is classified as host, deployment, container, application or secret. `GET /api/setup/env` reports each variable's `layer` and `apply` (`live`, `restart` or `deploy`), and only application keys are editable. A test forbids application code from reading host-side Compose variables.
- `PLAYLIST_DIR`, `BEETS_SQLITE_TIMEOUT` and `WEB_MANAGER_PATH` are removed from the settings catalog; nothing used them. `BEETS_LIBRARY` is marked deprecated and read-only.
- The built-in fallback settings template no longer lists unrelated variables (`DIGARR_INITIAL_PASSWORD`, `POSTGRES_PASSWORD`, `BEETS_UID`, `BEETS_GID`).
- Docs: `ARCHITECTURE.md`, `DEVELOPMENT.md` and `CONFIGURATION.md` no longer describe the deleted `backend/beets_client.py` as present or ARCH-010 as open.


## v0.1.49 - 2026-10-04

### Upgrade Notes
- Pasted reference URLs and artwork image URLs (from a user or a provider response) are now fetched directly and no longer use `HTTP_PROXY`/`HTTPS_PROXY`. Operator-configured services are unchanged.
- Artwork URLs that point at a LAN, private, loopback or CGNAT host are now refused, even if the host is in `BEETS_OUTBOUND_ALLOWLIST`. To use such an image, download it and use the artwork upload (`POST /api/albums/<id>/art/upload`), which still works.
- The setup "Music library" check now tests `MUSIC_ROOT` (default `/music`) inside the `beets-web-manager` container. `MUSIC_ROOT` must be the same path as Beets' `directory:` in the `beets` container (see `docs/CONFIGURATION.md`).

### Fixed
- **Setup no longer reports a correctly mounted library as missing (#143).** The setup status still probed a hard-coded `/data/media/music`, and the diagnostics never reported a `music_library` path at all. A stack deployed with the documented `/music` mount was always told "Music library path /data/media/music is not accessible", and the System and setup pages showed it as Missing. The check now tests `MUSIC_ROOT` (default `/music`) inside the Web Manager container. When stock Beets is unreachable, the fallback paths shown are `MUSIC_ROOT` and `/downloads` instead of the legacy `/data/media/music` and `/data/torrents`.
- **Setup no longer always reports fpcalc missing.** Stock Beets reports fingerprinting support (the chroma plugin loaded) but never a binary path, and setup gated on that always-empty path. So "fpcalc (chromaprint) not found" was always a blocking reason and AcoustID was always shown as a missing dependency. Setup now uses the availability Beets reports.
- **AcoustID setup test classifies the answer by error code.** `POST /api/setup/test/acoustid` sends a dummy fingerprint, so a valid key usually comes back as error code 3 (invalid fingerprint) over HTTP 400. That was reported as a failure or as "Could not reach AcoustID". The probe now reads the error body of non-2xx answers too, and maps by code: 3/8 mean the key was accepted (ready); 4 means the key was rejected; 5/13 or HTTP 5xx mean the service is unavailable; 14 or HTTP 429 mean rate limited; any other code fails. Only timeouts and network errors report "Could not reach AcoustID". Responses carry a fixed message and a `reason` field, and never echo provider text or the key.

### Added
- `AGENTS.md` agent guide (imported by `CLAUDE.md`): project goal, scope and autonomy, live-library safety rules, git workflow, definition of done, release/deploy steps, and communication expectations. Host-specific details go in a gitignored `CLAUDE.local.md`. This reverses the Sep 16 removal of these files (#119). The governance test now enforces a single source instead: `CLAUDE.md` may only import `AGENTS.md`, which prevents the drift that caused the removal.

### Security
- Bump `next` 16.3.4 -> 16.3.8 for GHSA-vcvr-r3jv-pc5j (critical, RCE in `next/og` `ImageResponse`). The frontend never imports `next/og`, so this is defensive. It also unblocks the `npm audit --audit-level=high` CI gate.
- Fix server-side request forgery in the reference-URL fetch (`POST /api/submissions/reference-url`, CodeQL #1350, `py/full-ssrf`). Before this fix, the pasted URL was checked against the outbound policy, but urllib looked the host up again when it connected, so a DNS-rebinding domain could reach loopback, LAN or cloud-metadata addresses. The check also honoured `BEETS_OUTBOUND_ALLOWLIST`, so a pasted URL could reach allowlisted internal services (by default the Beets plugin at `beets:8337` / `127.0.0.1:8337`). Shared/CGNAT addresses (100.64.0.0/10) were not blocked. The fetch now uses `backend.security.open_public_url()`. It resolves the host once and requires every address to be globally routable; IPv6 forms that embed a private IPv4 address are rejected. It never consults the allowlist, connects the socket to the exact address it validated (TLS still verifies the original hostname), and repeats all of this for every redirect hop. This supersedes the July false-positive dismissal of the same finding (alert #18).
- Apply the same public-only, address-pinned fetch to artwork image URLs that come from a user or a provider response: `POST /api/albums/<id>/art/url`, saved Discogs/candidate artwork, the Discogs artist-image cache and the Cover Art Archive/Discogs release-art cache. Operator-configured services (Beets, Plex, Lidarr, slskd, qBittorrent, the yt-dlp PO provider) keep the allowlist-based policy.
- `provider_boundary` now classifies an outbound-policy refusal as `rejected` and does not retry it. Previously it was treated as a transient error and retried.
- Bump Pillow 10.4.0 -> 12.3.0 for GHSA-cfh3-3jmp-rvhc, GHSA-pwv6-vv43-88gr, GHSA-whj4-6x5x-4v2j, GHSA-wjx4-4jcj-g98j, GHSA-r73j-pqj5-w3x7, GHSA-45hq-cxwh-f6vc, GHSA-5x94-69rx-g8h2, GHSA-8v84-f9pq-wr9x, GHSA-phj9-mv4w-65pm, GHSA-4x4j-2g7c-83w6, GHSA-62p4-gmf7-7g93, GHSA-6r8x-57c9-28j4, GHSA-9hw9-ch79-4vh6, GHSA-fj7v-r99m-22gq, GHSA-jjj6-mw9f-p565, GHSA-vjc4-5qp5-m44j and GHSA-xj96-63gp-2gmr (out-of-bounds writes and reads, decompression bombs, DoS). Artwork bytes from users and providers reached these parsers.
- Artwork validation now passes `formats=("JPEG", "PNG", "WEBP")` to `Image.open` (`backend/artwork_service.py`, `backend/transaction_engine.py`). PSD, FITS, GD, McIdas and other non-accepted formats are refused before their Pillow plugin parses the payload. Error responses are unchanged: a recognised but unsupported format is still reported as an unsupported type, and unrecognisable bytes as corrupt.
- Bump yt-dlp 2024.11.4 -> 2026.8.19 for GHSA-c6mh-fpjc-4pr3, GHSA-vx4q-3cr2-7cg2, GHSA-69qj-pvh9-c5wg, GHSA-6v4j-43gg-vj32, GHSA-g3gw-q23r-pgqm and GHSA-f7j3-774f-rfhj. All YoutubeDL options and APIs the app uses exist in the new release. The bgutil PO-token plugin stays at 1.3.1, matching the `bgutil-ytdlp-pot-provider:1.3.1-deno` server in `docker-compose.full.yml`. YouTube downloads still need a JavaScript runtime (Deno, Node or QuickJS), which the app already requires.
- Bump Flask 3.1.0 -> 3.1.3 (GHSA-4grg-w6v8-c28g, GHSA-68rp-wp8r-4726), Werkzeug 3.1.3 -> 3.1.6 (GHSA-hgf8-39gv-g3f2, GHSA-87hc-h4r5-73f7, GHSA-29vq-49wr-vm6x; these affect Windows `safe_join` only, so not the Linux image) and requests 2.32.5 -> 2.33.1 (GHSA-gc5v-m9x4-r6x2).

### Changed
- Frontend dependencies (these replace Dependabot PRs #121, #123 and #125, which were based on a stale commit and whose lockfiles broke `npm ci`):
  - `react-router` 8.3.0 -> 8.3.1: a patch release that adds origin validation for action requests and extra URL validation on client-side navigations and redirects. The exact-pin test in `tests/test_import_page_navigation.py` now expects 8.3.1.
  - `typescript-eslint` ^8.64.0 -> ^8.70.1 (dev).
  - `@testing-library/react` 16.3.2 -> 16.3.3 (dev).
- The lockfile was regenerated with npm 10 (the npm in the node 22 CI and Docker images) and keeps the optional `vitest/node_modules/yaml` entry that `npm ci` there requires.

## v0.1.48 - 2026-09-30

### Changed
- **Every external provider call goes through one boundary (ARCH-006).** All 48 outbound HTTP call sites (MusicBrainz, AcoustID, Discogs, Spotify, artwork downloads, Plex, Lidarr, SLSKD, qBittorrent, the yt-dlp PO provider and the AI provider) now open their connection with `provider_boundary.opened(provider, request, ...)`.
  - Each provider has a declared policy: attempts and backoff. Retries are bounded, honour Retry-After (capped at 30 s), and apply only to requests that are safe to repeat (GET/HEAD). A POST is never repeated.
  - A 4xx refusal (400, 404, 422) is classified `rejected` and never retried. Previously the retry helper treated it as transient.
  - Each call site keeps its own error handling: on final failure the original exception is raised.
  - `PROVIDER_MAX_ATTEMPTS=1` turns retries off.

### Added
- `GET /api/providers/health`: the last classified outcome, attempt count and retry policy per provider since start-up. Redacted: no URLs, no keys.

## v0.1.47 - 2026-09-30

### Changed
- **Import Review action decisions are backend-owned (ARCH-005).**
  - `backend/import_review_decision.py` is the authority for an item's bucket, whether the action is blocked and why, the next step, the action label and the source files an import takes. `POST /api/import-review/decision` serves it.
  - Before an import or repair starts, the page asks the backend for the verdict and stops if it is blocked or the backend cannot be reached.
  - The page's own decision rules moved, unchanged, out of the 4,700-line page into one pure module (`importReviewDecision.ts`) that mirrors the backend for instant feedback.
  - Both implementations run against the same 45 cases in CI, so a rule changed on one side only fails the build.

## v0.1.46 - 2026-09-30

### Added
- **A shared contract for long-running mutating jobs (ARCH-004).** `backend/job_contract.py` gives a workflow a durable `workflow:<name>` lock for as long as it runs, a heartbeat, a checkpoint in the durable job record, and cancellation while waiting.
  - Adopted by Clean All, playlist download and playlist pipeline actions, AI batch import, Acquire Download All, single album download+import, the music-format replacement retry, and the import slot used by folder import and disk re-import.
  - One instance of each runs at a time across processes and restarts. A lock left behind by a process that died is reclaimed once its heartbeat expires, so the first run after a crash may wait up to two minutes.
  - Each workflow's own in-process guard and HTTP behaviour are unchanged; the durable lock is taken after it.
  - Clean All, playlist download and Acquire Download All also publish their resumable position into the job record.

## v0.1.45 - 2026-09-30

### Added
- **New album rows from untracked files of releases Beets does not have yet (ARCH-021).** These are the files a single-file attach cannot place, because no album row exists for their release.
  - `GET /api/library/untracked-recovery/album-candidates` lists the folders of untracked album files from the persisted inventory.
  - `POST /api/library/untracked-recovery/plan` with `action: "attach_album"` and one folder plans a new album row in a read-only job. A file is included only when its tags carry Recording, Release and Release Group IDs and a track position, MusicBrainz lists that recording at that position of that release (and gives the same Release Group), and AcoustID confirms the audio. Other files are excluded with a reason and stay untracked.
  - Beets plugin 1.5.0: `/webmanager/untracked/attach-album` creates the row in place (no tag write, no move) and refuses a release that already has a row. Rollback removes the rows and leaves the files untouched.
  - Approve, apply and roll back through the transaction routes, like every other recovery.

## v0.1.44 - 2026-09-30

### Changed
- **Album merges from Clean and from import now use the one album-row merge (ARCH-020).** The legacy merge reassigned `album_id` through a field update the engine refuses, and overwrote the Release ID and other album fields on the moved items.
  - Clean's duplicate-album and Release-Group merges, the split-album move and the import's existing-album reconcile now move item ownership only, within one Release Group and Release ID, into free slots.
  - Another edition, a release without IDs, or an already-filled slot is refused and stays for review.
  - Beets plugin 1.4.0: `album-row-merge` accepts `"partial": true` to move some of a row's items; the source row is retired only when emptied.
- **Import reconcile no longer removes imported duplicates on its own.** Imported copies of slots the existing album already holds become a reviewed duplicate cleanup: audio proof required, file moved to the engine quarantine, applied only on a reviewer's decision.
- The Release-Group merge plan refuses a retained row whose files are missing, so stale rows are recovered first.

### Fixed
- `merge-duplicate-album`, the Release-Group merge and the split-album merge failed at run time because of mismatched call arguments.
- Importing a single plugin module first could fail on a circular import; shared helpers now live in `engine_common.py`.
- A failed merge now restores a source row even if Beets removed it before raising.

### Known limitation
- The duplicate resolver's "retag" action is refused (`identity_rewrite_not_supported`): it rewrote Recording ID and track position on moved items without audio proof.

## v0.1.43 - 2026-09-29

### Added
- **Reviewed cleanup of a duplicate album row's only copy.** When a proven duplicate (shared AcoustID recording or identical bytes) is the only item of a duplicate album row of the keeper's own release, the operator-reviewed cleanup plan may now remove it and retire that emptied row.
  - Required: same Release ID and Release Group, the keeper in a different row at the same disc/track.
  - Beets plugin 1.3.1: `quarantine-remove-items` takes `retire_album_id` + `sibling_keeper_item_id`, re-checks those conditions itself, snapshots the row and retires it. Rollback recreates the row at its original id and puts the item back at its original id.
  - The unattended path and bulk cleanup never opt in; their album-slot gate is unchanged.

### Changed
- `quarantine-remove-items` never empties an album row implicitly any more (`ALBUM_WOULD_EMPTY`), and its rollback restores removed items at their original ids when those ids are free.

## v0.1.42 - 2026-09-29

### Added
- **Album-row merge (ARCH-020).** Beets plugin 1.3.0 adds `/webmanager/album-row-merge` with rollback and status endpoints.
  - It merges the duplicate album rows of one canonical album by changing item ownership only: no tag write, no file move.
  - It requires Release Group **and** Release ID proof, full source coverage, unchanged identity and content, and no slot overlap.
  - Rollback restores the original album rows at their original ids.
  - `POST /api/library/album-duplicate-analysis/plan-merge` plans only groups the live analysis proves deterministic.
- **Untracked recovery (ARCH-021).** Engine ops `/webmanager/untracked/attach` and `/untracked/quarantine`, with rollback and status.
  - `GET /api/library/untracked-recovery/candidates` and `POST /api/library/untracked-recovery/plan` expose four action classes, with backend-owned eligibility:
    - quarantine only byte-identical copies of tracked files;
    - attach a missing album file after tag, MusicBrainz and AcoustID proof;
    - track a better encoding, then plan its replacement through the one replacement authority;
    - no action otherwise.
  - The inventory is incremental: it reuses persisted records and hashes only new or changed files.
- **Durable jobs and locks (ARCH-004).**
  - Job records persist under `<data>/jobs` with checkpoints, heartbeats and a new terminal status, `recovery_required`. After a restart, an interrupted job is never re-run: read-only jobs become `failed`, other jobs `recovery_required`.
  - Durable, hierarchical, cross-process resource locks live under `<data>/locks`.
  - Engine-backed transactions interrupted by a restart are finished from engine evidence, never replayed.
- **Provider boundary (ARCH-006):** typed outcomes (`confirmed`, `no_result`, `unavailable`, `rate_limited`, `authentication_error`, `transient_error`, and others), bounded Retry-After-aware retries and redaction. Used by AcoustID lookups and MusicBrainz release fetches.

### Fixed
- An AcoustID outage, throttle, rejected key or timeout was cached permanently as "no match" for that file. Only real answers are cached now.
- A rollback job could report success after its transaction's "Rolled Back" status had been overwritten back to "Running" (ARCH-019). The route now marks the transaction Running before the job starts.

## v0.1.41 - 2026-09-29

### Fixed
- `--offline-db-identity` works with the stock Beets engine. Its web server does not close SQLite on a graceful stop, so the WAL is never settled by the engine. The check now copies the main file and WAL to a private temp directory, then checkpoints, `quick_check`s and hashes the settled copy. It never writes the authoritative files, and it verifies both are byte-for-byte unchanged afterwards. A failure prints "OFFLINE DB CHECK FAILED" with the right stage, not a rollout banner.
- The untracked inventory classifies folders and files carrying a never-resolved naming token (for example `{Album MbId}`) as import artifacts, not unknown.

## v0.1.40 - 2026-09-29

### Fixed
- Unattended duplicate cleanup no longer uses the old cleanup path. That path ran `os.unlink` inside the Web Manager on the read-only `/music` mount: the delete failed, only a warning was logged, and the transaction was still marked Completed with the file reported deleted. Its "rollback" did nothing. The path is removed.
- Manual live duplicate cleanup (`/api/dedup/cleanup` with `dry_run: false`) goes through the reviewed-cleanup authority. Every path must be the source copy of a proven duplicate pair; anything else is left in place and reported.

### Added
- **Reviewed duplicate cleanup** (`duplicate_cleanup_v1`): Plan → Approve → Apply → Verify → Rollback.
  - `POST /api/dedup/reviewed-cleanup/plan` re-proves every reviewed pair against live Beets using the scan's own policy: shared AcoustID recording, same release slot, keeper policy, album-slot gate, and no lossless rival.
  - It also checks that paths, sizes and slot evidence have not drifted since the proposal. A drifted pair is skipped and stays in review.
  - Apply needs approval, then verifies keepers, album slots and the exact library-count change.
  - Unattended cleanup, when an operator authorizes it, uses the same authority.
- Beets plugin 1.2.0: `POST /webmanager/quarantine-remove-items` and its rollback. The engine refuses the whole request unless every file still matches its reviewed SHA-256. It moves each file into the engine quarantine (never deleted) and removes the row through Beets, recording both in a manifest. On failure it puts everything back. Rollback re-adds the rows, into their album row when that row still exists.
- **One replacement authority** (`backend/item_replacement.py`). The item route, the music-format quality pipeline, import album merges and reconciliation reviews all plan through it onto the canonical engine-backed item-file replacement:
  - AcoustID decides; text never does.
  - The slot keeps its identity.
  - An occupied destination is displaced only for identical audio.
  - Anything unproven fails closed, with both files kept.
- Import merges and the music-format pipeline now *plan* replacements and leave them for approval, instead of applying them unreviewed. The pipeline records "Replacement awaiting approval" and never re-downloads a pending replacement.
- Read-only library integrity reports:
  - `POST /api/library/album-duplicate-analysis` groups album rows by Release Group and proposes a merge plan per group: retained row, item moves, overlapping slots, edition differences, blockers. It merges nothing.
  - `POST /api/library/untracked-inventory` runs a one-walk inventory of audio files Beets does not track. It reads AcoustID from the cache only, saves evidence under `/web-manager-data/untracked_inventory/`, and mutates nothing.
- `scripts/deploy_truenas_web_manager.sh --offline-db-identity`: stops Beets briefly, hashes the database only when its WAL is settled, restarts Beets and re-verifies it.

### Changed
- Deploy verification is WAL-aware. Online checks read the library through the Beets web API (counts, an identity digest over every item and album, plugin health) instead of opening SQLite. A live main-file hash is logged as informational only, never as proof of an unchanged database.

### Removed
- The legacy replacement engine and its bulk import wrappers:
  - `transaction_engine` track and bulk replacement functions.
  - `composite_workflows.plan/apply/rollback_bulk_import_replacement`.
  - The `unlink`-based `apply_library_cleanup` / `rollback_library_cleanup`.
- None of these have production callers left.

## v0.1.39 - 2026-09-29

### Fixed
- Beets plugin 1.1.1: `/webmanager` operations now act on the real absolute file paths. Beets 2.x stores paths relative to the library and expands them through a context variable, which is set only in the thread that opened the library. Web server request threads started with it empty, so every `item.path` loaded relative. As a result, replace-item-file refused files that existed, and `move` and file-deleting `remove` would have used the wrong paths. Each `/webmanager` request now binds the library's music directory first.
- Replacement failure recovery is complete. If the engine fails part-way, it now also moves the replacement file back to its original path and re-creates the replacement's library row, in addition to restoring the album item and its original file.

### Added
- Track replacement handles an occupied canonical destination. Suppose an untracked file already has the name Beets will give the replacement (Beets would otherwise add a `.1` suffix):
  - The plan step decodes both files with ffmpeg and compares PCM MD5s. The occupant may be displaced only if its audio is identical to the replacement's. Anything else fails closed with `destination_occupied`.
  - The Preview transaction records the occupant's SHA-256.
  - On apply, the engine displaces the file only if it is still untracked and its SHA-256 still matches; otherwise it refuses before changing anything.
  - The occupant goes to the engine quarantine folder and is never deleted. Rollback puts it back.

## v0.1.38 - 2026-09-28

### Fixed
- Track replacement resolves the library-relative paths the stock Beets web API reports: the plan route fingerprints the real files under the music root, and the post-apply check accepts the engine's absolute path for the same file.

## v0.1.37 - 2026-09-28

### Fixed
- Track replacement (preview → approve → apply → rollback) works again, now on the Beets engine. Before this fix, planning always failed: it read the wrong payload keys. Apply also tried to copy a file into the read-only music mount. Replacement now puts a tracked library copy (for example, a proven lossless duplicate) into an album slot:
  - The album item keeps its Release Group, Release ID, Recording ID, disc/track and tags.
  - Its old file is moved to an engine quarantine folder (`/config/webmanager-quarantine/<id>/`, with a manifest the engine rolls back from) and is never deleted.
  - The replacement's own library row is removed.
  - Beets moves the file to its canonical path.
- Apply requires an approved transaction. It verifies afterwards that the album slot kept its identity; if not, the transaction is marked Recovery Required.
- Replacing from an untracked staged file now fails closed with `staged_replacement_unsupported`.

### Added
- Beets plugin 1.1.0: `POST /webmanager/replace-item-file` and `/webmanager/replace-item-file/rollback` (takes only the engine's replacement id, never caller paths; capability `replace_item_file`). Both are idempotent and run under the mutation lock.
- `POST /api/items/<id>/replacement/plan` accepts `candidate_item_id`. The candidate's path comes from Beets, and the pair must match by AcoustID fingerprint. The generic `/api/transactions/<id>/apply` and `/rollback` routes handle these transactions.

## v0.1.36 - 2026-09-28

### Changed
- Unattended duplicate cleanup no longer deletes either copy when the preferred album copy is lossy and a proven duplicate is lossless. Such a group is flagged for replacement review instead: the right fix is replacing the album file through the reviewed replacement transaction. The review shows in the job log, the proposal (`action: replacement_review`) and the Duplicate Files panel.

## v0.1.35 - 2026-09-28

### Changed
- Unattended duplicate cleanup now picks which copy to keep by an explicit policy:
  1. a copy attached to the album row over a loose/singleton copy;
  2. valid canonical metadata (Recording ID, Release Group ID, release ID, disc/track);
  3. an embedded Recording ID that agrees with AcoustID;
  4. the canonical Beets path over a duplicate or decorated filename;
  5. audio quality (lossless over lossy, then bitrate, sample rate and bit depth; file size only between copies of the same format);
  6. the lowest item id, only as the final tie-breaker.
- Each proposal row states why its copy is kept.

### Added
- Album-slot gate: unattended cleanup never deletes a copy that is attached to an album row unless the retained copy is a tracked item in that same row. No album slot is ever left without a retained tracked item.

## v0.1.34 - 2026-09-28

### Fixed
- The tracked-library duplicate scan is single-pass:
  - It loads the library once and builds every index (Recording ID, size, path, album, fuzzy title) once.
  - It makes no full-library lookups per file.
  - Fuzzy matches resolve to the real Beets item, so release-slot evidence stays intact.
- Scan progress is reported live: N / total, percentage, current file, candidate count, files per minute and ETA in the Jobs view.
- Match scores shown in the log and UI are clamped to 100%.
- Log lines name their evidence: `FINGERPRINT VERIFIED`, `BYTE VERIFIED`, `CANDIDATE`, `REVIEW REQUIRED` or `REJECTED`.
- A cancelled job now reports `cancelled` instead of `failed`. Cancelled import jobs stay retryable.
- The playlist library index is keyed on a stable cache generation instead of a timestamp.

## v0.1.33 - 2026-09-28

### Changed
- The scheduled duplicate step checks only Beets-tracked library files instead of every audio file under the library mount. The live `/music` holds about 106k audio files but Beets tracks 3,144, and unattended deletion can only ever select tracked pairs. The same rules and proof apply, and the step finishes in minutes instead of days. The manual Duplicate Files scan still walks the whole folder.

## v0.1.32 - 2026-09-28

Library root fixed; unattended duplicate deletion needs explicit authorization.

### Fixed
- The Web Manager hard-coded its library root as `/data/media/music`, but every shipped compose file mounts the library at `/music`, so on live installs file-level features found no files. That included the scheduled duplicate scan, fingerprint checks and local artwork/path checks. `MUSIC_ROOT` is now the one configurable setting (default `/music`), and backend and frontend no longer hard-code a library path.

### Added
- Unattended duplicate deletion is a separate authorization, off by default and independent of `MUSIC_ROOT`. While it is off, the scheduled duplicate step scans, verifies with AcoustID and records a review proposal, but deletes nothing.
- Each proposal row shows:
  - both paths;
  - sizes;
  - embedded Recording IDs;
  - AcoustID fingerprint evidence;
  - the release/track slot;
  - which copy is kept.
- The proposal appears in the Duplicate Files panel and the job log.
- Enabling requires an exact confirmation phrase.
- New endpoints: `GET/POST /api/dedup/unattended-cleanup` and `POST /api/dedup/maintenance-run`.
- New regression tests prove that changing `MUSIC_ROOT` cannot enable destructive cleanup.

## v0.1.31 - 2026-09-28

`app.py` decomposed into owned services (ARCH-001). No API, route, or behavior changes are intended.

### Changed
- `app.py` went from 52,665 lines to about 570 lines of application glue: app creation, request hooks, security headers, error handlers, static and SPA serving, and route-module loading.
- The code moved verbatim into layered services under `backend/` (for example `library_service`, `playlist_service`, `import_service`, `dedup_service` and `ai_service`) and into route modules (`routes_library`, `routes_cleanup`, `routes_import`, `routes_playlist`, `routes_acquisition`, `routes_maintenance`, `routes_system`).
- `app.<name>` still resolves every moved name.
- The layer order, the global-state audit and the remaining debt are documented in `docs/arch001_service_decomposition.md`.
- Some routes were called in-process through a fake request context. They now call request-free services, for example `start_dedup_scan`, `start_album_download` and `start_folder_import_with_id`.
- Globals that functions rebound (the library cache, the last scan job) now live on shared state objects.

### Fixed
- The playlist JSON-state safety check looked up its allowed roots through `globals()`. That lookup would have silently found none once the code left `app.py`, so it now uses explicit references.
- Two `_extract_mb_uuid` definitions existed, and the later strict one silently shadowed the URL-capable parser. Pasted MusicBrainz URLs now resolve again.
- The dead `_db()` helper pointed at a removed control-agent function, so calling it could only raise a `NameError`. It now fails closed explicitly.

### Verified
- The Flask URL map is identical to v0.1.30: 259 rules, with the same endpoints and methods.
- The endpoint security inventory is unchanged (250 entries).
- The ARCH-003 mutation inventory is unchanged: 403 sinks with the same classifications.
- ARCH-002 `NEEDS_MIGRATION` is still 0.
- New CI guards in `tests/test_arch001_architecture.py` and `scripts/audit_arch001_ownership.py` check that:
  - nothing under `backend/` imports `app.py`;
  - a service only imports lower layers;
  - `app.py` has no SQLite, subprocess, Docker, `beet` command or matching policy;
  - the duplicate-deletion rule still requires fingerprint or byte proof.

## v0.1.30 - 2026-09-28

Duplicate cleanup requires audio proof (found with live AcoustID).

### Fixed
- Unattended duplicate cleanup treated a shared embedded MusicBrainz Recording ID as proof that two files were the same audio. With a working AcoustID key the live library showed this was wrong: in 6 of 7 duplicate groups the fingerprint contradicted the shared embedded Recording ID, and one proposed deletion ("Rush") had no fingerprint result at all. Unattended deletion now requires one of two things:
  - both copies fingerprint to a shared recording;
  - the files are byte-identical.
  Otherwise the pair is left for review.
- The dedup scan now fingerprint-checks "MB Track ID" matches the same way it checks fuzzy matches. A confirmed fingerprint mismatch rejects the candidate.

### Verified
- Live proposal: 6 deletions (one per audio-proven group; no group loses every copy). The unproven "Rush" pair is left for review. Previously there were 7 deletions.
## v0.1.29 - 2026-09-27

Final matching and identity closure: canonical import reconciliation, release-group identity contracts, and AcoustID key roles. **ARCH-002 and ARCH-009 are closed.**

### Added
- **`backend/import_reconciliation.py`**: a canonical reconciliation service for imports into an existing album. It first proves album identity by Release Group, then decides every contested disc/track slot with the canonical recording evaluator. There are four outcomes:
  - KEEP_EXISTING: both files are the same recording
  - KEEP_IMPORTED: the existing file is fingerprinted as the wrong recording
  - CONFLICT
  - KEEP_BOTH_REVIEW
  Text evidence never discards either file. Undecided slots keep both files and both library rows, and are recorded to `web-manager-data/import_reconciliation_reviews.json`.
- **Reconciliation review queue**: `GET /api/import-reconciliation/reviews` and `POST /api/import-reconciliation/reviews/<id>/resolve` (`keep_existing` / `keep_imported` / `keep_both`, applied only through engine transactions). A new "Reconciliation review" panel on the Import Review page shows each side's canonical evidence.
- **`backend/identity_contract.py`**: every release ID supplied to a mutation is resolved to its authoritative MusicBrainz Release Group. A mismatch is refused, an unverifiable release fails closed, and a Release ID is never copied into a Release Group field. Field classification is in `docs/arch009_identity_fields.md`.
- **`ACOUSTID_USER_KEY`**: the AcoustID user key used for fingerprint submissions. `ACOUSTID_API_KEY` is now documented as the *application* key for lookups. One variable had been serving both, so a user key made every lookup fail with "invalid API key". Legacy single-variable installs keep working.

### Changed
- `_merge_imported_album_into_existing` only orchestrates now. Previously:
  - a text title score (0.90, or 0.72 with a matching Recording ID) decided which file was discarded;
  - with no MusicBrainz tracklist the existing file was always retired;
  - album identity was never checked.
- Removed `import_guard.existing_track_matches_target` / `existing_track_can_block_downloaded_replacement`, the text-threshold destructive guards.
- ARCH-009 contract enforcement was added to these routes:
  - `add-mbids`
  - submissions `attach-mbids`
  - `rgid-group/relink`
  - `rgid-group/assign-representative-release` (previously failed open)
  - album `deduplicate` override
  - `duplicate-resolver/apply` override
  - the engine MB track repair, which no longer lets caller-supplied tracks inherit the album's release group and refuses when the release's release group is unknown
- The ARCH-002 caller audit now enforces NEEDS_MIGRATION = 0.

### Tests
- New and updated test files:
  - `tests/test_import_reconciliation.py` (28)
  - `tests/test_arch009_identity_contract.py`
  - `tests/test_acoustid_key_roles.py`
  - a duplicate-group safety property test covering groups of 2–6 copies: at least one copy is always kept
  - the wave18/wave20 real-path reconciliation tests, updated to the canonical rules
  - `frontend/tests/ReconciliationReviewPanel.test.tsx`
## v0.1.28 - 2026-09-26

Canonical single-recording evaluator, release-aware duplicate identity, and latent NameError fixes (ARCH-002 / ARCH-009).

### Added
- **`backend/matching/recording.py`** adds the canonical single-recording evaluator, `evaluate_recording_candidate`. It combines embedded Recording ID, AcoustID, title, artist, duration, filename, position and version-qualifier evidence, and reports a `RecordingIdentityProof` (insufficient / textual_support / embedded_recording_id / acoustid_recording_id / multi_source_deterministic). The same module adds `verify_audio_against_request` for checking downloaded audio against a request. AcoustID semantics match album alignment: a score floor of 80 and a 3-point ambiguity window.
- **`backend/recording_review.py`** is a new service module. It handles recording-candidate generation (candidates only) and canonical replacement-identity resolution.
- **`backend/duplicate_identity.py`** is a new service module for duplicate-file identity (release slot) and for selecting paths that unattended cleanup may delete.
- **`scripts/audit_arch002_callers.py` + `docs/arch002_caller_audit.json`** add a CI-enforced classification of every production final-decision pattern hit.

### Changed
- `build_recording_matching_decision` takes its attach eligibility, safety key, confidence state, conflicts and review reasons only from the canonical evaluator, so there is no second decision tree. It also takes the real AcoustID hit set. **Policy change:** a MusicBrainz text-search candidate with no embedded Recording ID and no AcoustID proof is now "Needs review" (it can still be attached with confirmation). Previously it could be one-click "Safe to attach".
- Import Review now shows identity proof, confidence state, hard conflicts, review reasons and backend attach eligibility for recording candidates. The AI-suggestion match builder honors the backend's canonical veto.
- The format-replacement workflow now establishes identity canonically. An AcoustID hit can no longer silently replace the embedded Recording ID, and a text-search result alone goes to review. The target album requires a Release Group.
- Playlist/download audio verification uses the canonical verdict. A fingerprint that names a different recording is never accepted because of title text.

### Fixed
- An MB text candidate that AcoustID contradicted was labelled "no result" and could be marked safe. It is now a hard conflict.
- The unattended Import Review auto-enqueue ignored the canonical album veto (`matching_decision.action_allowed=False`).
- Scheduled duplicate cleanup counted a shared Recording ID as duplicate-file identity, and for a mutual pair it selected **both** copies. On the live library, all 7 duplicated recordings would have lost every copy. Unattended deletion now requires the same release slot and keeps one copy.
- Duplicate-album merge let a row with an unknown release group inherit another row's release group (ARCH-009).
- Latent `NameError`s were fixed in:
  - `dedup_scan`'s album+title step, which aborted scans
  - `POST /api/albums/<id>/remove` (plus a nonexistent `jobs.create`)
  - `reimport_disk` (`temp_cfg_content`)
  - the AI genre fallback (`env`)
  - Plex playlist re-verification (`prior_rating_key`)
  - folder tag-evidence guessing (`mf`)
  - `/api/config` error handlers (`ConfigError`)
- AcoustID service rejections (for example an invalid API key) are now logged and recorded instead of looking like "no match".
- Removed the dead confirmed-import title scorer, which was passed but never used.
## v0.1.27 - 2026-09-26

Canonical album identity: identity verification split from release completeness (ARCH-002 Part 3).

### Changed
- **`backend/matching/models.py`**: new `IdentityProof` (INSUFFICIENT / RELEASE_GROUP_ID / DETERMINISTIC_TRACK_RECORDING_ID / CONFIRMED_RELEASE) and `ActionScope` (FULL_RELEASE / VERIFIED_SUBSET) enums. `ReleaseGroupMatchResult` now reports identity proof plus local and target track coverage as separate facts, and `MatchPolicy.scope` selects what an operation needs proven. The default `FULL_RELEASE` scope behaves exactly as before.
- **`backend/matching_contract.py`**: `build_album_matching_decision` no longer computes its own `identity_verified`/`action_allowed`. It now uses the canonical `identity_proof` and `can_auto_accept(scope=VERIFIED_SUBSET)`.
- Removed the dead duplicate track scorer from `backend/mb_alignment.py`. Both alignment helpers now resolve to the canonical `backend.matching` functions.

### Fixed
- A local album whose `mb_releasegroupid` matched the candidate's could authorize automatic action with no track-level evidence at all. A bare Release Group ID match no longer authorizes action.
- A partial album (for example 2 of 18 tracks) where every local track is proven by an exact Recording ID still authorizes action on those tracks. It no longer depends on whole-release completeness.

### Tests
- `tests/test_arch002_matching_corpus.py::TestArch002PartialAlbumIdentity` covers these cases:
  - a 2-of-18 deterministic subset is allowed under VERIFIED_SUBSET and denied under FULL_RELEASE
  - a bare Release Group ID match is denied
  - a Recording ID conflict is denied
  - a mix of deterministic and text-only tracks is denied
  - a duplicate local claim is denied
  - an AcoustID conflict is denied
## v0.1.26 - 2026-09-24

Live TrueNAS deployment validation and code-scanning closure pass.

### Fixed
- **`backend/composite_workflows.py`**: `get_unmatched_review_items()` never actually supported the `limit`/`offset`/`include_singletons` contract its three callers already used, throwing a `TypeError` on every call and breaking the Import Review "Needs MB ID" page entirely; reimplemented to return the `albums`/`singletons` shape callers expect. `get_folder_items()` silently dropped multi-prefix callers; now accepts a single path or a list of path prefixes.
- CodeQL `py/path-injection` (high): added containment checks to `plan_track_replacement`'s `source_path`, playlist staging paths (sanitized `playlist_key`), `get_artist_folder_inventory`/`resolve_folder_to_albums`/`get_folder_items` (must resolve under `MUSIC_ROOT`), and `inspect_import_source` (must resolve under `MUSIC_ROOT` or an approved staging root) -- previously unvalidated filesystem reads/walks on caller-supplied paths.
- CodeQL `py/stack-trace-exposure` (medium): `backend/config_manager.py` no longer interpolates raw filesystem exception text into `ConfigError` messages returned to API clients; the real exception is logged server-side and a generic message returned instead.
- CodeQL `py/polynomial-redos` (high): capped filename length before regex title-guessing on untrusted Soulseek/slskd search result names in `app.py`.

### Verified
- Deployed to the real TrueNAS installation (not a disposable test path): backups taken, stock-Beets plugin load confirmed genuinely inside the running `beets` container (not just present in the Web Manager's own copy of the source), `docker compose restart` and `up -d --force-recreate` both preserve library state (byte-identical `musiclibrary.blb` hash, unchanged track/album counts).
- All open GitHub code-scanning alerts (24) individually inspected and dispositioned: real findings fixed in code, remaining false positives dismissed with per-alert justification. Zero open alerts.

## v0.1.25 - 2026-09-24

Complete Composite Workflow Migration (ARCH-010) and full retirement of `backend/beets_client.py`.

### Added
- **`backend/composite_workflows.py`**: Complete implementation of all 20 composite workflow families orchestrating Plan / Apply / Rollback transactions directly backed by stock Beets (`backend/beets_adapter.py` on `:8337`).
- **`backend/config_manager.py`**: Robust Beets `config.yaml` management with optimistic CAS revision hash checks, strict YAML syntax validation, atomic replace (`fsync`), backup generation, and rollback support.

### Changed
- **Zero Legacy Engine References**: Migrated all remaining ~386 `beets_client` call sites across `app.py` to `composite_workflows`, `config_manager`, and `beets_adapter`.
- **Deleted `backend/beets_client.py`**: Retired the legacy HTTP client, port 8338, `BEETS_API_URL`, `BEETS_API_TOKEN`, and legacy control agent scaffolding.
- **Updated Mutation Inventory**: Reclassified and verified all 402 mutation sinks in `security/arch003_mutation_inventory.json` with 0 unresolved entries.

### Fixed
- Fixed and verified all 2,675 unit/integration test cases, architecture invariants, and security secret scans.

## v0.1.24 - 2026-09-24

Closure/hardening pass for the stock-Beets migration (#137-140 left main broken and materially less migrated than represented; see PR #141).

### Fixed

- **`/health/ready` and setup status were permanently unhealthy on a fresh install.** `chroma`'s required-plugin health check tested for a local `pyacoustid` Python package inside Web Manager, but AcoustID fingerprinting runs entirely inside the stock Beets container -- Web Manager has no `pyacoustid` dependency of its own.
- **Existing-install upgrades never actually enabled `web`/`webmanager`.** Both were missing or misclassified in the plugin manifest, so an existing user's config.yaml never got the plugin entries needed for stock Beets' own default service to even start, and `replaygain` could get enabled without the `backend:` setting it needs to avoid a hard load failure. Migrating a plugin name into `plugins:` without its minimum required settings block is now handled for `web`, `webmanager`, and `replaygain`, without ever touching a block a user already has.
- **A real upstream Beets 2.14.1 defect crashed every album/item read with `include_paths: yes`** (`beets.util.displayable_path(None)` raising instead of returning `""` for an album with no artwork yet -- i.e. every album immediately after import). Worked around defensively inside the `webmanager` integration plugin at load time; does not modify the Beets image itself.
- A real, previously-unredacted secret-leak path in `/api/setup/status`'s top-level `plugins` field.
- A silent no-op in `attach_album_mbids()`: it checked the stock-Beets integration plugin's modify response for an `"ok"` key that response never carries (the real key is `"success"`), so every real mutation through that endpoint was treated as a failure.
- The fresh-install plugin-provisioning ordering race: `beets` now waits on Web Manager's own healthcheck (inverted from the previous direction), since Web Manager provisions the webmanager plugin's files and config.yaml entries before its own HTTP port binds.
- Removed the forbidden generic `POST /api/plugins/run` beet-command endpoint.

### Changed

- `docker-compose.yml`/`docker-compose.dev.yml`/`docker-compose.full.yml` now use `lscr.io/linuxserver/beets:latest`, mount `/music` read-only into Web Manager, and unify Web Manager's durable-state mount on `/web-manager-data`.
- `job_engine.py`, `routes_setup.py`, and `routes_submissions.py` are now fully migrated onto `backend/beets_adapter.py`, with zero remaining references to the retired `backend/beets_client.py` control-agent client.
- Added a `PRODUCTION_LEGACY_BEETS_REFERENCES` CI invariant proving that migration claim from source.
- Rewrote `docs/ARCHITECTURE.md`, `docs/CONFIGURATION.md`, `docs/INSTALLATION.md`, `docs/EXAMPLES.md`, `docs/DEVELOPMENT.md`, `docs/BEETS_ENGINE_MIGRATION.md`, and `README.md` to describe the current stock-Beets architecture instead of the deleted control-agent one.

### Known remaining debt (not fixed in this release)

`app.py`'s composite Plan/Apply/Rollback mutation workflows (merge-album, merge-artist, Clean All, track replacement, folder/album cleanup, artist-folder reconcile, album maintenance/relocation/metadata-repair, artwork, genre repair, mbsync-all, move-all, and the `/api/config` editor) still call the retired `backend/beets_client.py` and are currently non-functional against the real stock-Beets stack. Tracked as `docs/TECHNICAL_DEBT.md` ARCH-010.

## v0.1.23 - 2026-09-22

Ships work that had been written and passing locally but never committed/deployed -- discovered while performing live acceptance testing of v0.1.22's secret-reveal feature (see #136).

### Added

- **Configuration inventory expanded to 59 curated settings across 8 sections** (System & Environment, Authentication & Security, AI & LLM Services, Beets Core & Engine, Storage & Paths, Music Services & Metadata, Media Server Integrations, Playlists & Download Providers), each now carrying `restart_required` and `type`.
- **Effective vs. saved distinction shown in the UI.** When a setting's environment value differs from its persisted/saved value, the System page now shows both ("Running: ... | Saved: ...") with an "Overridden" badge, instead of only ever showing one.
- Two more secrets are revealable (`BEETS_API_TOKEN`, `QBITTORRENT_PASSWORD`); `SLSKD_API_KEY` reveal now also checks its dedicated `_FILE` override, matching the existing convention used elsewhere.

### Security

- `BEETS_WEB_AUTH_TOKEN`'s masked display switched from a length-revealing partial mask to a fixed-length `********` placeholder.

## v0.1.22 - 2026-09-21

### Added

- **On-demand secret reveal on the System page.** Configured secrets (`BEETS_WEB_AUTH_TOKEN`, `OPENAI_API_KEY`, `PLEX_TOKEN`, and other recoverable settings) now have a per-field Show/Hide control to inspect the actual effective value when needed -- never shown by default, never returned by the normal `/api/setup/env` endpoint. A new, narrowly-scoped `POST /api/setup/env/<name>/reveal` endpoint returns exactly one value, only for settings marked `revealable` in configuration metadata, and only to an authenticated administrator (with a short password-reauthorization window on installs that have a browser password). `BEETS_WEB_PASSWORD` is never revealable -- it is stored only as a password hash, which cannot be converted back into the original password.

## v0.1.21 - 2026-09-21

Security cleanup following v0.1.20's live-deployment acceptance test (see #133).

### Fixed

- **Persisted web auth token file self-heals an incorrect `0700` mode.** A real deployment was found with `.auth_token` at mode `0700` (expected `0600`) -- created before `WebManagerConfigStore`'s write-time chmod existed, and never rewritten since (reusing an existing valid token never rewrites the file). The bootstrap path now corrects the mode in place on every startup, so existing installations self-correct on their next restart/recreation without a manual `chmod`.

### Security

- Rotated the `beets` engine's `BEETS_API_TOKEN` on the operator's production deployment after the previous value was inadvertently displayed in plaintext during the v0.1.20 live-verification session. No code change was required for this; noted here for the record.

## v0.1.20 - 2026-09-21

Two fixes found live during v0.1.19's TrueNAS rollout and the System-page acceptance verification that followed (see #131, #132).

### Fixed

- **System page's host-path variables (`MUSIC_PATH` et al.) still showed a fabricated `./music`-style default**, even after v0.1.19's fix marked them non-editable. `_env_catalog()` let the literal placeholder value in the bundled `.env.example` override the curated metadata's explicit `default: None`. Curated metadata now always wins.
- **The TrueNAS rollout script checked the wrong persistent-data mount** (`/web-manager-data` instead of `/data`), causing a false post-deploy-verification failure on a real rollout even though the deploy itself succeeded and persistence was intact. Now resolves `/data` first, matching `app.py`'s own precedence.

## v0.1.19 - 2026-09-21

Makes the System / Environment configuration page show the deployment's genuine effective configuration instead of static environment-variable echoes, and fixes two real configuration-accuracy bugs found while verifying it against a live deployment (PR #130). Also folds in PR #129 (zero-friction Beets plugin management), which had not yet been released under its own version tag.

### Changed

- **System page now resolves real effective configuration**: runtime/persisted/default precedence, empty Docker env value handling, secret masking with safe replace/remove, source badges, container-path metadata, and immediate post-save refresh.
- **AI requests now use the same configuration the System page displays.** Previously, 6 real AI-request call sites hardcoded their model (`gpt-4o`/`gpt-4o-mini`) and the OpenAI endpoint, and only ever read `OPENAI_API_KEY` -- while the System page implied `AI_MODEL`/`AI_BASE_URL`/`OPENROUTER_API_KEY`/`AI_API_KEY` were live, effective settings. Real requests now resolve model/endpoint/API key from the same variables via shared `_ai_api_key()`/`_ai_model_and_endpoint()` helpers.
- **Host volume path variables no longer claim a fake value.** `BEETS_CONFIG_PATH`/`MUSIC_PATH`/`DOWNLOADS_PATH`/`WEB_MANAGER_DATA_PATH` are `docker-compose.yml`'s own host-side bind-mount interpolation variables and are never forwarded into the container's environment -- the page previously showed a fabricated `./music`-style default and allowed silently-no-op edits. Now labeled container-path-only and marked non-editable, enforced on both the backend write-guard and the frontend.

## v0.1.18 - 2026-09-19

Simplifies the standard Docker deployment (PR #128, follow-up to #126): a normal install now runs a stock Beets container and Beets Web Manager together in one Compose file, with no custom Beets engine, no manual internal API tokens, no `.env`, and no setup scripts required.

### Changed

- **Standard deployment simplified.** The default `docker-compose.yml` now runs `lscr.io/linuxserver/beets:2.13.1` (stock, unmodified) alongside `ghcr.io/iranman/beets-web-manager:stable`, sharing `/config`, `/music`, `/downloads`, plus a `/data` mount for Web Manager's own persistent state. `docker compose up -d` and opening the browser setup wizard is the whole install.
- **Beets Web Manager bundles its own `beets==2.13.1` + `ffmpeg` + `fpcalc` + `pyacoustid`** and starts the existing Beets control agent on an internal loopback address (`127.0.0.1:8338`) inside its own container only -- never exposed to the host or another container. The stock `beets` container remains a normal, independently-usable Beets install and CLI environment (`docker compose exec beets beet ...`); Web Manager does not depend on it being reachable.
- Both Beets runtimes are pinned to the identical `2.13.1` release -- no schema-version-skew risk between them.
- `docker-compose.dev.yml` migrated to build Web Manager from source against the stock Beets image (previously built the old custom engine image). `docker-compose.full.yml` retained and explicitly labeled legacy/advanced-only. `.env.example` now states `.env` is optional and isolates external-engine variables under an "Advanced / External" section.
- CI's `production-docker-acceptance` job now boots and exercises the actual stock-Beets + Web Manager topology end-to-end (fresh install, setup wizard, real cross-container import, persistence across `down`/`up` and `--force-recreate`) and gates image publication; the old custom-engine/remote-HTTP acceptance job is retained separately as a legacy-compatibility check only.

### Fixed

- A fresh `/data` bind mount was not writable by Web Manager: the image ran as a fixed build-time UID and never actually applied the documented `PUID`/`PGID` settings at runtime. Fixed with a proper root-then-drop-privileges entrypoint that remaps the container's user to the runtime PUID/PGID (default unchanged) and fixes ownership of `/data`, `/config`, and the top level of `/music`/`/downloads` before dropping to the unprivileged user -- the application itself still never runs as root.
- The stock Beets container's own default service (`beet web`) was restart-looping with "unknown command 'web'" because the `web` plugin wasn't enabled in the config it ended up with. Fixed by enabling `web` in `config.yaml.example` for the setup-script path, and correcting the corresponding acceptance test, which had itself been pre-seeding an unrealistic config that no real installation ever produces.
- Fixed the same class of persistence bug found and corrected during this refactor's review: Web Manager's own data-directory auto-detection was not exported for other modules to see, silently writing settings and the setup-complete marker to a non-persistent path that was lost across `docker compose down && up` / `--force-recreate`.
- Corrected several stale documentation claims (`README.md`, `docs/ARCHITECTURE.md`, `docs/INSTALLATION.md`, `docs/EXAMPLES.md`) referencing a `beet-locked` locking wrapper that does not exist in the stock image used by the new default deployment, replaced with an accurate description of what SQLite's own locking actually protects versus what it does not.

### Migration Note

Existing v0.1.17 (or earlier) installs using the previous two-container `beets` + custom `beets-engine` architecture (port 8338, `BEETS_API_TOKEN`, `/data/media/music`) are **not** upgraded in place by swapping in the new `docker-compose.yml` -- that file assumes the stock LinuxServer image and the new mount layout. Keep using `docker-compose.full.yml` (now explicitly legacy/advanced) or deliberately migrate your `/config` bind mount and drop the old engine-specific env vars before switching.

## v0.1.17 - 2026-09-18

Hotfix release addressing ARCH-020 and related fail-closed/information-exposure defects found across three independent-review passes (PR #126, follow-up to #120).

### Fixed

- **ARCH-020 fixed for real, not just documented:** candidate discovery for artist-folder scan/merge/MBID-stamping now happens engine-side via a new read-only Control Agent endpoint (`/artists/folders/inventory`) and a typed `BeetsClient.get_artist_folder_inventory()` method, since the Web Manager has no local media mount in the supported two-service deployment. The `docs/TECHNICAL_DEBT.md` ARCH-020 entry is removed.
- **`_apply_artist_folder_reconcile_resilient()` error classification:** a definite HTTP 400/401/403/404 now fails immediately instead of entering the up-to-600s transaction poll; only genuine transport uncertainty still polls.
- **Clean All resume reattachment implemented and hardened:** a saved Clean All operation is no longer discarded on a transient `get_transaction()` lookup failure — the task stays `running` with the same `operation_id` preserved instead of risking a duplicate Plan/Apply.
- **Engine inventory failure no longer masquerades as "no work":** `_stamp_artist_folder_scan()` now distinguishes a genuine empty scan from an engine/inventory failure and fails closed with a structured error instead of silently reporting nothing to do.
- **Information exposure through an exception (CodeQL):** raw exception text from the new/modified hotfix error paths (inventory-scan failures, saved-operation lookup failures, resilient-Apply rejected/lost-response/poll-failure branches, async stamp-mbid job failures) is no longer surfaced in HTTP responses, job results, or job-visible logs. A shared classifier now maps each known `BeetsClient` exception type to a sanitized, safe message while preserving `error_code`/`status_code`; the real exception is only ever logged server-side.
- Max-stale diagnostics edge case: a stale-but-present cache with a stuck refresh now correctly reports `diagnostics_pending`/503 instead of a false "confirmed unavailable."
- Two previously-latent bugs in `_derive_artist_folder_identity()`/`_extract_recording_mbids()` fixed (Beets does not always store `items.path` as absolute).

## v0.1.16 - 2026-09-17

Consolidation release reconciling ARCH-002, ARCH-007, ARCH-012, repository hygiene, and dependency updates (PR #119). Requires a new release once merged: none of this is in the published `v0.1.15` image.

### Added

- **Canonical matching/evidence engine (ARCH-002):** introduced `backend/matching/` (normalizer, rules, scorer, `ReleaseGroupMatchResult.can_auto_accept()` auto-accept policy) as the single shared source of truth for release-group matching decisions. Migrated AI suggestions, folder candidate evaluation, and the `_album_track_*` family onto it; removed a polynomial-ReDoS-vulnerable regex from the matching normalizer, with adversarial test coverage. Closed a real gap in the playlist auto-placement path where a text-only confidence score (no fingerprint evidence at all) could authorize an unattended tag-write and file-move.

### Fixed

- **Structured read boundary completed (ARCH-007):** eliminated the remaining legacy raw-SQLite/`_db()` calls in production code across 12 server-owned endpoint families and 19 typed `BeetsClient` methods, with an AST-level regression test enforcing the zero-raw-query boundary going forward.
- **Library missing-album counting (ARCH-012):** fixed a defect in `_build_library_payload()` that could misclassify singleton tracks as missing albums; missing-album counting now resolves the dominant `album_id` from items and links through `beets_album_lk_by_id`. Multi-date missing releases are now grouped into a single card.
- **Frontend/Docker build:** `frontend/package-lock.json` was missing a nested `vitest`/`vite` peer dependency entry (`yaml@2.9.1`), which made a clean `npm ci` fail under the Node 22 build image used by both the production Dockerfile and CI's Docker acceptance jobs. Regenerated the lockfile against the actual Node 22 build environment; no application dependency changed.
- **Security:** fixed 8 real information-exposure findings where an unexpected backend/Beets-engine exception's raw text could reach a client-facing error response instead of only the server log. Reviewed 5 CodeQL path-injection findings individually; closed the ones with a real (if redundant) gap and confirmed the rest are downstream of an existing sound containment check.
- Reconciled frontend dependencies (`postcss`, `@tanstack/react-query`, `@types/node`, `@types/react`, `jsdom`) and GitHub Actions dependencies to their current tested versions.
- Repository hygiene: removed leftover AI-agent development process material from the repository; no user- or operator-facing behavior change.

### Known Remaining Work

- ARCH-002 migration is not yet complete for every matching call site; see `docs/TECHNICAL_DEBT.md` for the current register.
- ARCH-001 (monolithic `app.py` route/domain/mutation coupling) remains open, incremental extraction ongoing.

## v0.1.15 - 2026-09-15

SEC-002 / ARCH-003 controlled-mutation closure across Waves 15-29 (PRs #88-#102, #107-#109, #112), the repository-wide CodeQL closure (#108), a Jobs page transport fix (#103), and frontend dependency security remediation (#113). Requires a new release once merged: none of this is in the published `v0.1.14` image.

### Added

- **Wave 29 / ARCH-007 structured library reads:** new `BeetsClient` methods (`find_all_albums_by_albumartist`, `list_distinct_albumartists`, `find_all_orphan_albums`, `list_distinct_item_paths`) replace raw, permanently-broken `_db()` calls (the two-service topology's `raw_sqlite_query()` unconditionally rejects raw SQL) in `library_merge_artist`, `library_normalize_artists`, `_run_normalize_artists_if_needed`, `library_mbsync_all`, and `library_move_all`.
- New Control Agent endpoints `POST /library/mbsync`, `POST /library/move`, `POST /submissions/submit` with matching fail-closed `BeetsClient` methods (`mbsync()`, `move_library()`, `acoustid_submit()`).

### Fixed

- **Web Manager local Beets CLI execution eliminated (Wave 29):** removed all remaining `BEET_BIN`/`_beet_run`/`_beet_env` local-subprocess execution from the Web Manager image. `library_mbsync_all()`/`library_move_all()` now run `beet mbsync`/`beet move` exclusively via engine IPC; `attach_album_mbids()` (`routes_submissions.py`) rewired onto `album_metadata_repair_v1` and `beets_client.acoustid_submit()`. An AST-based structural regression test permanently bans any reintroduction across every Web Manager production module.
- **ARCH-003 controlled mutation closure (Waves 15-29):** every remaining unmigrated Beets DB/filesystem mutation sink across import review, cleanup, duplicate handling, album maintenance, artist-folder management, artwork, track/album replacement, AI import state, and configuration now runs through an audited plan/apply/verify engine transaction family. Mutation inventory closes at 0 unresolved blockers across 437 discovered candidate sinks.
- **Security (Wave 29, found during this release's own post-merge CodeQL validation):**
  - Fixed a real quadratic ReDoS in the MusicBrainz track-title normalizer (`_mb_track_repair_title_norm`/`album_track_norm`) reachable via caller-supplied import titles -- measured at 11.6s on a 64k-character adversarial payload before the fix, ~14ms after. Replaced with a linear-time algorithm, proven byte-identical to the original on 300,000+ randomized inputs.
  - Hardened the four temporary `beet -c` config-override files (one of which carries the AcoustID API key) against a symlink-follow and a world-readable window between file creation and permission narrowing; now created atomically at owner-only mode with `O_EXCL`/`O_NOFOLLOW`.
  - 22 additional `py/path-injection` CodeQL alerts individually reviewed against current source and dismissed as false positives with per-alert, line-cited rationale (read-only probes already gated by root-containment/symlink checks) -- none bulk-dismissed.
- **Repository-wide CodeQL closure (#108):** all 171 outstanding alerts individually dispositioned (35 fixed, 136 confirmed safe with sink-specific rationale).
- **Playlist ReDoS (Wave 28, #109):** fixed real polynomial-backtracking regressions in playlist artist/title splitting and "- Topic" channel detection.
- **Preservation-copy integrity (Wave 28, #107):** replaced a path+size "content signature" (which could not actually detect differing file content) with a real streamed SHA-256 digest; closed an unconditional-overwrite-on-collision gap and a silent copy-fallback that could mask a real copy failure as success.
- **Jobs page transport (#103):** resolved Jobs page fetch failures and hardened engine error-contract handling.
- **Frontend dependency security (#113):** `next` 16.2.11 -> 16.3.4 (2 critical RCE advisories), `sharp` 0.35.3 -> 0.35.4 (high, libheif), `vitest`/`@vitest/mocker` 4.1.10 -> 4.1.11 (moderate, path traversal). `npm audit --audit-level=high` clean (0 critical, 0 high, 0 moderate).

### Known Remaining Work

- ARCH-002 (duplicated matching/confidence rules across entry points) remains open, P0.
- ARCH-007's broader scope (other, not-yet-audited routes still expressing reads in a raw-SQL-compatibility shape) remains open; this release closed the five callers directly proven broken by real Docker acceptance testing, not a full repository audit.
- ARCH-001 (monolithic `app.py` route/domain/mutation coupling) remains open, incremental extraction ongoing.

## v0.1.14 - 2026-08-19

SEC-002 playlist-pipeline and MusicBrainz-identity engine-ownership work, Waves 9-14 (PRs #82, #83, #84, #86, #87). Requires a new release once merged: none of this is in the published `v0.1.13` image.

### Fixed

- **Playlist pipeline engine ownership completed (Waves 9-13):** removed every remaining local fallback in the playlist acquisition/staging/validation/import/placement path (local directory listing, local AcoustID fingerprinting, local media-tag reads, local `pl_default`-keyed staging) in favor of engine-owned `BeetsClient` IPC; fixed `playlist_id`/`playlist_key` confusion across six call sites with one strict shared resolver that refuses rather than guesses; closed an unauthenticated arbitrary-item mutation gap in the engine's `/playlists/place-imported` endpoint (an `item_id` is now required to belong to a recorded import operation for the calling `playlist_key`); restored DB backup and write/move-both-must-succeed truthfulness for placement; fixed a fabricated-candidacy regression in manual quality-repair; bounded and locked the playlist staging file-listing endpoint; fixed cross-playlist Plex ratingKey collision, checkpoint-corruption isolation, and non-circular AcoustID/fingerprint verification for playlist import (Waves 9-12).
- **MusicBrainz identity/matching authority consolidated (Wave 14):** introduced a single shared `MatchingDecision` contract (`backend/matching_contract.py`) so deterministic identity proof (Release Group ID equality, exact Recording ID matches) -- not independent per-workflow heuristics -- is the sole authority for whether a candidate can auto-import/auto-repair versus require manual review, across Import Review, Folder Preflight, Release Resolution, Reimport, Duplicate Cleanup, and the existing recording-attach gate. Fixed a disconnect where two response-compaction layers silently stripped the shared decision before it reached the real Import Review auto-import gate, letting an old independent heuristic authorize import even when the shared decision withheld it; fixed the same class of bypass in the oversized-partial-release acceptance path; fixed a generic-mapping-treated-as-a-release bug and a Release-ID-only-misclassified-as-a-hard-conflict bug in the matching contract itself; added a narrow Release-Group-ID consistency guard to the MusicBrainz track-repair workflow, which had no deterministic identity check at all.

## v0.1.0 - 2026-07-16

### Added

- Initial public source-control baseline for Beets Web Manager.
- Flask backend, React/Next static frontend, background jobs, playlist workflows, import review, cleanup tools, Plex integration, MusicBrainz and AcoustID verification, and AI-assisted metadata workflows.
- Security documentation, threat model, endpoint inventory, and CI security checks.
- GitHub issue templates, pull request template, Dependabot configuration, and build/test workflows.
- Standalone `Dockerfile`, `.dockerignore`, single-command `docker-compose.yml`, `requirements.txt`, and `config.yaml.example`.
- `setup.sh` / `setup.ps1` one-command bootstrap; `scripts/backup.sh` / `scripts/restore.sh`.
- `routes_setup.py`: `/api/setup/status`, `/api/setup/test/{ai,musicbrainz,acoustid,plex}`, `/api/setup/settings`, `/api/setup/complete`, and `/health`, `/health/live`, `/health/ready` probes with version reporting.
- `docs/INSTALLATION.md`, `docs/CONFIGURATION.md`, `docs/TROUBLESHOOTING.md`.
- CI: `docker-build.yml` now builds the image and runs a real start-container-and-probe-health smoke test.

### Fixed

- `config.yaml` (contains plaintext integration secret fields) was not excluded by `.gitignore`.
- Baseline CI no longer depends on local-only agent instruction files or private `config.yaml` files, and Docker dependency installation uses the available `pylistenbrainz==0.5.1` pin.

### Known Limitations

- The broader Arr stack still contains services outside the Beets app hardening scope.
- Some security scanner integrations may require repository-level GitHub settings or release artifacts.
- Operators must provide their own credentials and rotate any values that were ever exposed before this baseline.
- Setup wizard is backend-API-only in this release; no browser wizard UI yet.
- Baseline Docker image build passes GitHub CI; local Docker Desktop validation still fails before build start with a Linux engine `_ping` 500, and setup/demo packaging still needs a release check after `routes_lidarr.py` is restored.

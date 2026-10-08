# Architecture

This document describes the architecture that exists today and the intended direction. It does not claim that the intended architecture is complete.

## Non-Negotiable Rules

These are standing product/architecture invariants, not aspirations. Each is backed by an Architecture Decision Record under `docs/adr/` and, where practical, a regression test — see the linked ADR for full context and consequences.

- **Beets Web Manager builds on stock Beets; it does not build Beets.** The sole authoritative Beets runtime is the published `lscr.io/linuxserver/beets` image, run as its own container. Web Manager provides the UI, workflow orchestration, job tracking, and audit/rollback state, and talks to that container only over HTTP — never a local Beets Python import, a control-agent process, a Docker socket, `docker exec`, or a custom Beets image. (`docs/adr/0001-beets-remains-library-backend.md`)
- Beets remains the library backend and source of library mutations; the app does not grow a parallel music-library database. (`docs/adr/0001-beets-remains-library-backend.md`)
- MusicBrainz and AcoustID are the primary identity evidence. The canonical album-level identity is the MusicBrainz release-group ID (`mb_releasegroupid`); a release ID (`mb_albumid`) is edition-level secondary data and must never be substituted where a release-group ID is required. (`docs/adr/0002-release-group-id-is-canonical-album-identity.md`)
- AI is optional and untrusted. It may rank or explain candidates already found through deterministic sources; it must not invent an identity and treat it as verified, and its unavailability must never stop MusicBrainz/AcoustID matching. (`docs/adr/0003-ai-is-optional-and-not-source-of-truth.md`)
- No silent library mutations. Any move, rename, merge, delete, tag write, replacement, or artwork write requires the controlled preview/apply/audit/recovery workflow in `backend/transaction_engine.py`. (`docs/adr/0004-library-mutations-use-controlled-workflow.md`)
  - **One documented exception: playlist pre-import tag hints.** `composite_workflows.write_staging_tags` (reached only through `write_tags`, from `playlist_service`'s playlist-download tag stamping and enrichment, which `acquisition_service`'s playlist download matching also calls, always before the download is imported) writes `title`/`artist`/`albumartist`/`album`/`year` hints with `mediafile` straight into the downloaded file. It exists because playlist downloads (slskd) often carry no or wrong tags, and Beets' importer needs a usable artist and title to find MusicBrainz candidates. It is allowed only on a regular file under a staging root (the downloads root or `<data dir>/playlist_staging`), never under `MUSIC_ROOT` or on protected Web Manager data, with no symlinked path component and no second hard link (a hardlink made for seeding would share the library file's contents); the file is opened fd-relative with `O_NOFOLLOW` and must still be the entry that was validated, otherwise it raises `ValueError` and nothing is written. These files are not in the library yet: Beets' importer still does the real tagging from MusicBrainz when it imports them, and every tag write to a library file goes through Beets. (Tests: `tests/test_staging_tag_writes.py`.)
- Long-running operations use the shared job infrastructure (`job_engine.py`) rather than ad hoc background threads or bespoke checkpoint logic. (`docs/adr/0005-long-running-operations-use-shared-job-infrastructure.md`)
- Ambiguous or conflicting evidence goes to review; destructive actions require stronger evidence than suggestions.
- Never expose secrets in logs, API responses, frontend state, or committed files.

## Current Main Components

- `beets` (Stock Beets Container): Standard upstream LinuxServer Beets container (`lscr.io/linuxserver/beets:latest`). Owns `/config/config.yaml`, the authoritative `/config/musiclibrary.blb` database, and all `/music` writes. Runs the built-in `web` plugin (read-only HTTP API) and the bundled `webmanager` integration plugin (the only mutation surface Web Manager is allowed to call), both on its own internal `:8337`.
- `beets-web-manager` (Web Manager Container): Production web application built from `Dockerfile`. Has no local Beets Python runtime (`requirements.txt` does not install `beets`) and performs zero local Beets database or media-file mutation. Serves the browser UI and its own API on port 8337, executes background jobs, and talks to stock Beets exclusively through `backend/beets_adapter.py`.
- `app.py`: application glue only (~570 lines since ARCH-001, v0.1.31) — creates the Flask app, request hooks (auth/CSRF/rate limits), security headers, error handlers, static/SPA serving and health, and loads the route modules. Every moved name still resolves as `app.<name>`. See `docs/arch001_service_decomposition.md`.
- `routes_library.py`, `routes_cleanup.py`, `routes_import.py`, `routes_playlist.py`, `routes_acquisition.py`, `routes_maintenance.py`, `routes_system.py`: route families moved out of `app.py`; HTTP handlers over the owned services.
- `backend/*_service.py` (+ `app_runtime.py`, `serializers.py`, `pending_review_store.py`, `library_cache.py`): owned services, strictly layered (a service imports only lower layers; nothing under `backend/` imports `app.py`). Layer order and ownership: `docs/arch001_service_decomposition.md`, `docs/arch001_app_ownership.json`.
- `routes_jobs.py`: split route module for `/api/jobs/*` job listing, lookup, and cancellation.
- `routes_lidarr.py`: split route module for Lidarr/wanted endpoints.
- `routes_setup.py`: split route module for setup, authentication, and configuration checks; sources all Beets/plugin diagnostics from `backend/beets_adapter.py` and `backend/beets_plugins.py`.
- `routes_submissions.py`: split route module for MusicBrainz/AcoustID submission workflow and MBID attachment; AcoustID submission runs through the real `beetsplug.chroma` plugin inside stock Beets via `beets_adapter.mbsubmit()`.
- `job_engine.py`: `PythonJob` and the durable `JobStore` (ARCH-004): structured state, cooperative cancellation, log retention, restart resolution, and an opt-in, in-process duplicate-start guard: a job whose metadata carries `dedupe_key` is refused while a job with the same key runs (`DuplicateJobError`, HTTP 409). The key must name the job's whole input; today only Move All and MBSync All set one. A job that returns `{"ok": false}` ends `failed`. A job ends `cancelled` only when it stopped because of the cancel: it raised `"cancelled"`, or its cancel event's `is_set()`/`wait()` returned True before it returned or raised (`job_engine.cancel_honoured`). A cancel the job never saw keeps the real outcome (`success`/`failed`) and adds a log line, and the hook-created transaction follows the same rule, so a finished mutation is never recorded Cancelled.
- `helpers_mb.py`: MusicBrainz and AcoustID helper functions. It has no `app.py` dependency and is the strongest current provider boundary.
- `backend/beets_adapter.py`: the only supported transport to stock Beets — a narrow `BeetsAdapter` client for the `web` plugin's reads and the `webmanager` plugin's authenticated mutation operations (`modify`, `move`, `remove`, `mbsync`, `fetch_art`, `embed_art`, `lastgenre`, `mbsubmit`).
- `beetsplug/webmanager/`: the integration plugin itself, provisioned by Web Manager into stock Beets' `/config/beetsplug` and loaded by stock Beets like any other Beets plugin. Exposes `/webmanager/status` (handshake: protocol/plugin/Beets versions, loaded plugins, capabilities; from plugin 1.6.0 also Beets' own `directory`/`library` paths, the effective `allowed_roots`/`import_roots`, `web.include_paths`, and whether `fpcalc`/`ffmpeg` exist in the Beets container. These fields are additive, the protocol stays 1.0, and older plugins are reported as `unknown`) and the operation endpoints `BeetsAdapter` calls.
- `backend/`: helper package. `beets_adapter.py` and `beets_plugins.py` (plugin provisioning/health) are the stock-Beets integration surface; `album_match.py`, `audio_preferences.py`, `import_guard.py`, `mb_alignment.py`, `security.py`, `slskd.py`, `title_normalize.py`, `track_align.py`, and `transaction_engine.py` are Web-Manager-local domain/orchestration logic. The retired control-agent client `beets_client.py` has been deleted (ARCH-010, closed in v0.1.25); the composite workflows live in `backend/composite_workflows.py` on top of `beets_adapter.py`. `config_layers.py` defines the configuration layers (host, deployment, container, application) described in `docs/CONFIGURATION.md`.
- `frontend/src/`: React/Next/TypeScript frontend. `frontend/src/api/client.ts` centralizes API calls, `frontend/src/api/types.ts` centralizes response shapes, and views/features are split under `views/` and `features/`.
- `.github/workflows/`: CI covers Python syntax/unit tests, frontend typecheck/build, lint, Docker build, dependency audit, compose/security checks, stock-Beets acceptance (a real `lscr.io/linuxserver/beets` container), and production Docker acceptance.

## Intended Dependency Direction

```text
Browser
  -> Beets Web Manager routes (app.py, routes_*.py on port 8337)
  -> backend/beets_adapter.py (BeetsAdapter)
  -- HTTP, container-internal only -->
     reads    -> stock Beets `web` plugin           (:8337)
     mutations -> stock Beets `webmanager` plugin    (:8337, authenticated)
  -> stock Beets Library -> /config/musiclibrary.blb & /music
```

Both containers share these filesystem mounts:
- `/config` — Beets configuration and the authoritative SQLite library. Web Manager mounts this read/write only to provision the `webmanager` plugin's files and merge its config.yaml entries; it never opens `musiclibrary.blb` directly.
- `/music` — the target music library collection. Mounted **read-only** into Web Manager; stock Beets owns all `/music` writes.
- `/downloads` — incoming download staging folder, read/write in both containers.
- `/web-manager-data` — Web Manager's own durable state (settings, wizard completion, transaction/audit logs). Not shared with stock Beets.

## External Boundaries & Locking Model

- **Authoritative Database**: `/config/musiclibrary.blb` is opened exclusively by the stock Beets container. Web Manager never opens it directly — all reads and writes go through the `webmanager`/`web` plugins over HTTP.
- **Port Exposure**: Stock Beets' `:8337` is internal to the Docker network only, never published to the host. Web Manager's own `:8337` (`WEBCONTROL_PORT`) is the only port published, to `0.0.0.0:8337` (or `127.0.0.1:8337`) for browser access. Nothing in the current architecture uses port 8338.
- **Health and Readiness**: `/api/health` validates Web Manager liveness; `/health/ready` and `/api/setup/status` call `BeetsAdapter.get_plugin_status()`/`get_stats()` to report stock-Beets reachability, the `webmanager` plugin's protocol-version compatibility, loaded Beets plugins, and filesystem health.
- **MusicBrainz and AcoustID**: `helpers_mb.py` performs release, release-group, recording, and AcoustID lookup work. `routes_submissions.py` performs MusicBrainz validation and AcoustID submission orchestration through `beets_adapter.mbsubmit()`.
- **Submission Command Capability**: the `webmanager` plugin's `/webmanager/status` handshake reports whether `mbsubmit` (AcoustID, via the real `chroma` plugin) is available before Web Manager offers it.

## State Ownership

- **Library State**: `/config/musiclibrary.blb` is the authoritative Beets library, owned exclusively by the stock Beets container.
- **Web Manager Durable State**: `/web-manager-data` (`WEB_MANAGER_DATA_DIR`, mounted to `./web-manager` by default) holds `app_settings.json`, `.setup_complete`, `.auth_token`, and session encryption keys.
- **Job State**: Lives in `JobStore` in memory; structured progress and logs are exposed via `/api/jobs/*`.
- **Audit & Transaction State**: Stored in `backend.transaction_engine.TransactionStore` under `/web-manager-data/transactions` (`BEETS_TRANSACTION_DIR`).
- **Import Review State**: Staged under `/downloads` and tracked in `/web-manager-data/unmatched_drafts`.

## Job Lifecycle

Existing lifecycle:

1. Routes start Python callables through `JobStore.start_python()`.
2. `PythonJob` accepts `(log, cancel_event, update_state)` for jobs that support structured state.
3. `/api/jobs/*` serializes job status, logs, metadata, and compact results.
4. Some workflows add their own checkpoint files and resume logic, especially playlist and AI-batch/import flows.
5. There is no generic remote "run this Beets command" job abstraction — a narrow, operation-specific `BeetsAdapter` call (e.g. `mbsubmit`, `modify`) runs synchronously inside a `PythonJob`, and a job is never reported `cancelled` once stock Beets has already completed the underlying operation.

Intended direction:

- Every long-running workflow should have an operation id or idempotency key, durable progress, bounded retries, cancellation checks between safe steps, and clear terminal states.
- Jobs should orchestrate application services and checkpoint state rather than duplicate matching or mutation business rules inline.

## Matching Lifecycle

Existing entry points include:

- Import review AI and candidate flow in `backend/import_review_service.py`, `backend/ai_service.py`, `backend/ai_evidence_service.py` and `routes_import.py` (item/album/folder AI suggestion, target preview, auto-enqueue, revalidation, attach/match).
- MusicBrainz and AcoustID helpers in `helpers_mb.py`.
- Track alignment in `backend/track_align.py` and `backend/mb_alignment.py`.
- Import safety decisions in `backend/import_guard.py`.
- Playlist matching in `backend/playlist_service.py` around `_match_playlist_tracks`, reference matching, and quality-place flows.
- Missing-track replacement and Music Format Preferences matching in `backend/replacement_service.py` and `backend/acquisition_service.py`.
- Submission preparation and MusicBrainz validation in `routes_submissions.py`.

Intended direction:

- Converge matching entry points on one shared result contract containing local metadata, candidate identities, release-group ID, optional release ID, recording IDs, AcoustID evidence, tracklist evidence, duration evidence, filename/tag evidence, AI availability/contribution, confidence, conflicts, warnings, explanation, and action eligibility.
- Keep AI as an optional contributor, never the source of truth.

## Mutation Lifecycle

Existing mutation mechanisms include:

- Narrow, operation-specific `BeetsAdapter` methods (`modify`, `move`, `remove`, `mbsync`, `fetch_art`, `embed_art`, `lastgenre`, `mbsubmit`) — the only way Web Manager reaches a stock-Beets mutation. The web-manager container never shells out to `beet` locally and never runs a local Beets Python runtime (enforced by an AST-based structural test, `tests/test_arch003_boundary_enforcement.py`).
- `modify` stores only fields this Beets version has (webmanager plugin 1.11.0). Web Manager reads and edits `genre`. On Beets 2.13 and later, which replaced it with the multi-valued `genres` field, the plugin maps `genre` to `genres`, splitting the string the same way Beets' own genre migration does, and the adapter derives `genre` from `genres` when reading. Older Beets keeps `genre`. The plugin refuses any other field Beets has no column for with `400 UNSUPPORTED_FIELDS` before writing anything; only `data_source`, which Beets itself keeps as a flexible attribute, is exempt. A file-tag write failure answers `500 WRITE_FAILED` with the item ids, so the apply fails instead of reporting the fields as written. With `write`, an album update also writes its tracks' tags, as `beet modify -a` does.
- A file-backed `TransactionStore` in `backend/transaction_engine.py` (plan/apply/verify/recover) with statuses, changes, metadata diffs, rollback fields, and job attachment. This bookkeeping is pure Web-Manager-local orchestration; it does not itself talk to Beets.
- Rollback eligibility is server-owned (#228): `GET /api/transactions` and `GET /api/transactions/<id>` add `rollback.allowed` (bool), `rollback.allowed_code` (`allowed`, `not_applied`, `already_rolled_back`, `not_supported`, `unavailable`, `unsupported_operation`, `not_completed`) and `rollback.allowed_reason` (empty when allowed), computed by `routes_maintenance.rollback_eligibility` from the same gate `POST /api/transactions/<id>/rollback` applies. `rollback.available` and `rollback.reason` stay the stored values. An engine rollback that ran but failed verification (status Recovery Required) answers with `mutated: true`.
- Several workflow-specific preview/dry-run routes, including import target preview, cleanup scans, folder placeholder preview, and transaction endpoints.

Intended direction (the target shape for every production mutation path):

1. Inspect current state (via `BeetsAdapter` reads).
2. Produce a mutation plan.
3. Validate roots, identities, conflicts, and preconditions.
4. Display or record metadata and filesystem diffs.
5. Apply through `BeetsAdapter` calls with audit records.
6. Verify final state (via `BeetsAdapter` reads).
7. Record completed steps and recovery information.

Current migration status: every Beets read and mutation path, including the composite Plan/Apply/Rollback workflows (merge-album, merge-artist, Clean All, track replacement, folder/album cleanup, artist-folder reconcile, album maintenance/relocation/metadata-repair, artwork, genre repair, mbsync-all, move-all), runs through `backend/composite_workflows.py` and `backend/beets_adapter.py`. The retired `backend/beets_client.py` control-agent client is deleted (ARCH-010, closed in v0.1.25). `backend/transaction_engine.py` now holds only the TransactionStore, folder cleanup and import-review cleanup; the engine-side families that opened the Beets SQLite library directly were removed (BA-7), and folder cleanup checks Beets references through the adapter. Folder cleanup (`folder_cleanup_v1`: Clean All folder-safe renames, merges and empty-folder removal) never writes `/music` from Web Manager, which mounts it read-only: Web Manager plans, approves, audits and rolls back the transaction in its store, and each filesystem step (`move_file`, `rename_dir`, `remove_empty_dir`, `create_dir` for rollback) is one `BeetsAdapter.folder_op` call to the plugin's `POST /webmanager/folder-op` (plugin 1.7.0, capability `folder_op`). The plugin confines every path to the Beets library `directory` with no symlink component, refuses paths that hold library items (those move only through Beets' `item.move()`/`album.move()`), never re-creates a path Beets still references, never overwrites, and holds the plugin's `mutation_lock` and a library transaction. It uses Beets' `util.move` and `util.mkdirall`; an untracked folder rename uses `os.rename` (Beets has no API for it), and empty-folder removal uses `os.rmdir` rather than `util.prune_dirs`, which in Beets 2.13/2.14 checks emptiness and then calls `shutil.rmtree`, so it could delete a file created in between. Only an Approved transaction applies. Each confirmed step is recorded in `engine_result` as it happens. A step whose reply is lost is replayed with the same idempotency key, which makes the plugin report the original outcome; a step still unconfirmed after that is recorded too (rollback checks the filesystem before undoing it). A step that fails or cannot be confirmed leaves the transaction Failed with what already changed, and the generic rollback route (`POST /api/transactions/<id>/rollback`) undoes a Completed or Failed folder cleanup.

### Import contract

Every import is Beets' own importer run inside stock Beets by the `webmanager` plugin's `POST /webmanager/import` (`BeetsAdapter.run_import`). Web Manager never tags, matches or places files itself during an import; it only chooses the source, the copy/move mode and, when a person confirmed one, the MusicBrainz Release, and then verifies the result over the read API.

| Route | Composite | Beets equivalent |
|---|---|---|
| `POST /api/import` | `reimport_source()` | `beet import -q` with `autotag: yes`, `quiet_fallback` = the request's `fallback` (`skip` default; `asis` only when the caller sends it), `--search-id` = the optional `search_id` |
| `POST /api/folders/import-with-id` | `plan_confirmed_import()` / `apply_confirmed_import()` | `beet import -q --search-id <mb_albumid>` with `quiet_fallback: skip` |
| `POST /api/albums/reimport-disk` | same confirmed-import family | same; a folder inside the library is imported in place (no copy, no move) |

- Copy or move: a preserved torrent source (see `DOWNLOADS_ROOT` / `TORRENT_SOURCE_ROOTS` / `ALLOW_TORRENT_SOURCE_MOVE` in `docs/CONFIGURATION.md`) is always copied. `/api/import` copies unless `move: true` (a move from a preserved source is refused with HTTP 400); import-with-id moves when `move: true` was requested and the source is not preserved, and always moves an app-staged partial-import subset; reimport-disk moves download sources that are not preserved.
- Confirmed import verification: the Apply reads the albums carrying the planned Release ID before and after the import. Exactly one new album row must appear, and when a Release Group was confirmed its `mb_releasegroupid` must equal it. Zero rows means Beets skipped the folder (no confident match, or a duplicate with `duplicate_action: skip`) and the result is `not_imported`, which reimport-disk sends to review. Every outcome leaves the transaction Completed or Failed, never Preview.
- Unmatched albums: `skip` is the default on every route, because `asis` imports an album Beets cannot match with its existing tags and no `mb_releasegroupid`. Ambiguous identity goes to review instead, so whatever Beets skipped stays in place. The `/api/import` job result lists it under `not_matched` as "not matched; left in place for review". The list comes from the plugin's `skipped_paths` when the plugin reports it. Otherwise the whole source is listed when no album was added, and `not_matched_known: false` flags a partial import whose skipped folders the plugin did not name. Plugin 1.10.0 also returns `skipped`, one `{path, reason}` per folder, where `reason` is `no_candidates` (Beets found nothing, or the metadata source was unreachable), `no_strong_match` or `duplicate`. Each row carries that reason (`not_matched` from an older plugin). Because the plugin runs the importer without a Beets import log, `beet.log` never names these folders, so Web Manager records them in `import_skipped.json` under `WEB_MANAGER_DATA_DIR`. The Review Queue's Skipped source lists them with the `beet.log` skip lines: one row per folder, and the next `/api/import` of the same source replaces that source's entries, so a re-import never duplicates a row and a folder that imported drops out.
- No retag after the import: Beets applies the confirmed Release (tags, file placement, tag write), so import-with-id, reimport-disk and the AI batch import do not run `_match_tracks_from_mb`, `plan_album_mb_track_repair` or `relocate_album` afterwards. Both routes require the Release Group before importing and pass it to the plan, so the Apply verifies it. A reimport-disk that fills missing tracks merges the new album onto the existing one (`import_reconciliation`) and then checks that the merged album carries the Release and Release Group. An explicit `albumartist` override on reimport-disk is still applied, with a rename.
- The confirmed Release is imported as confirmed: import-with-id and the AI batch import pass the operator's (or the AI-chosen) Release to Beets unchanged. They never swap a single or EP for an album Release in another Release Group.
- The AI album suggestion surfaces the selected candidate's Release and Release Group unchanged.
- reimport-disk keeps the confirmed Release Group or goes to review. When the provided Release fails the folder tracklist preflight (and no manual override was sent), it may be replaced only by a Release of the same Release Group that passes the preflight. Otherwise the folder goes to review. A provided Release or Release Group never falls back to the free MusicBrainz search; input that cannot be resolved inside its own Release Group goes to review.
- import-with-id follows the same rule: a provided Release Group that resolves to no Release passing the folder preflight, a Release outside the selected Release Group, an unknown Release Group, or a source Beets skips as `not_imported` fails the job and queues one review item carrying the provided ID (`provided_mb_id`) and the real reason (unless the request set `queue_review: false`).
- Folder preflight artist evidence: only a source inside the library (Artist/Album layout) uses its parent folder name as the artist. A download or staging parent is a container, not an artist, and never raises `artist_conflict`. A refused preflight logs its real reason: the tracklist count when the tracklist failed, otherwise the identity decision's conflicts and missing AcoustID evidence.
- Verification failures keep the rows: when Beets imported but verification fails (`release_group_mismatch`, `import_ambiguous`, a rejected AcoustID check of requested missing tracks, a merged album without the Release), the job fails and the folder goes to review with the kept album_id in the reason, and the Failed transaction records it as `engine_result.kept_album_ids`. The rows Beets created stay in the library. Nothing in the import job removes them; a removal goes through the album cleanup preview/approve flow.
- Errors: plugin refusals map to stable codes (`autotag_not_allowed`, `path_not_allowed`, `source_not_found`, ...) with an operator message; the upstream body is never forwarded.
- Plugin requirement: the plugin must accept `autotag: true` with `search_ids` and `quiet_fallback`, report the folders Beets skipped as `skipped_paths`, and run Beets' quiet terminal import session. Plugin 1.8.0 and later do; an older plugin refuses `autotag` (`AUTOTAG_NOT_ALLOWED`), so every import route fails cleanly with `autotag_not_allowed` until the `beets` container is restarted on the current plugin.

## Frontend Architecture

- `frontend/src/app/*/page.tsx` files are thin route entries.
- `frontend/src/views/` contains major page shells such as Import, Library, Jobs, Playlists, Submissions, Clean, Config, and System.
- `frontend/src/features/` contains feature panels and larger workflow UI.
- `frontend/src/api/client.ts` centralizes API calls and CSRF header handling.
- `frontend/src/api/types.ts` defines many API response types.
- Existing large UI modules remain, including Import Review, Jobs, Playlists, and Library. These should be split only in behavior-preserving slices with tests.

Frontend direction:

- Keep UI compact and on the existing stack.
- Display evidence, conflicts, and backend action eligibility rather than recomputing authoritative identity or mutation decisions in components.
- Keep destructive actions explicit and visibly tied to evidence and confirmation.

## Areas Still Being Migrated

- Thick route handlers that still orchestrate workflows inline instead of calling their service (ARCH-001, narrowed after `app.py` was decomposed in v0.1.31).
- Job idempotency and checkpoint consistency across all long-running workflows (ARCH-004).
- Consistent provider-adapter contracts for AI, MusicBrainz, AcoustID, Plex, and download providers (ARCH-006).
- Outbound HTTP goes through `backend/provider_boundary.py`, using one of two entry points with the same policy, retries, classification and health record:
  - `opened(provider, request, ...)` is for operator-configured endpoints and fixed provider APIs. It connects with `urllib.request.urlopen`, which `backend/security.py` replaces with the allowlist-aware `secure_urlopen`.
  - `opened_public(provider, url, *, timeout, headers, max_bytes)` is for any URL supplied by a user or a provider response: the reference URL, and artwork image URLs. It connects only through `backend.security.open_public_url`, which allows public addresses only, ignores the allowlist, pins the connection to the validated address and re-validates every redirect hop. Its body has no path to `urlopen`.
  - There is deliberately no pluggable opener. That design let a user URL flow into `urlopen`, which CodeQL flagged as #1351/#1352.
  - An outbound-policy refusal is classified `rejected` and never retried.

Matching and identity: every production final identity/safety decision goes through `backend/matching/` (enforced by `scripts/audit_arch002_callers.py`). Album identity is the Release Group everywhere (`docs/arch009_identity_fields.md`).

See `docs/TECHNICAL_DEBT.md` for the full current list, including affected areas, risk, and desired state for each.
- Large frontend modules that mix rendering, polling, local state machines, and decision presentation.

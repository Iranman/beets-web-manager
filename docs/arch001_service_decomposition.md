# ARCH-001 — app.py decomposed into owned services

Status after v0.1.31: `app.py` went from 52,665 lines (v0.1.30, 1,323 top-level
functions) to about 570 lines of application glue. Every function that lived in
it is recorded, with its domain and current module, in
`docs/arch001_app_ownership.json` (`scripts/audit_arch001_ownership.py`
regenerates and checks it; CI fails if domain code comes back into app.py).

There is still exactly one Beets: stock `lscr.io/linuxserver/beets`, reached only
through `backend.beets_adapter` / `backend.composite_workflows`. Nothing here
reintroduces a custom engine, a control agent, port 8338, the Docker socket,
direct SQLite access, local `beet` mutation, or arbitrary command execution.

## Dependency direction

```
routes_*.py            HTTP handlers; register on `app`; import services
   │
backend/*_service.py   owned services, strictly layered (below)
   │
backend/matching/, import_reconciliation, duplicate_identity, identity_contract
   │                   domain decisions (no Flask)
backend/beets_adapter, composite_workflows, transaction_engine, helpers_mb, slskd
                       adapters to the Beets engine and external providers
```

Guards (`tests/test_arch001_architecture.py`):

- no module under `backend/` imports `app` (directly or via `import_module`);
- a service imports only services below it in the layer order; no service imports
  a route module; route modules do not import each other at load time;
- domain modules never import Flask; only the web-layer services
  (`auth_service`, `setup_service`, `serializers`) do;
- app.py imports no `sqlite3`/`subprocess`/Docker, holds no `beet` command, has
  zero ARCH-002 matching-pattern hits and every function in it is APP_GLUE;
- no `globals()` lookups and no `global` rebinding of a name another module
  imports;
- the Flask URL map equals the v0.1.30 baseline (259 rules: same rule,
  endpoint and methods) — `tests/arch001_route_baseline.json`;
- the unattended duplicate-deletion rule (below) is checked behaviorally.

## Layer order (extraction order)

Bottom-up; each module may import only those above it in this list. A helper
used by several domains lives in the lowest layer that uses it
(`EXTRACTED_SHARED` in the inventory).

| Layer | Module | Owns |
|---|---|---|
| 0 | `backend/app_runtime.py` | process config, env boot, primitives (`_s`, `_extract_mb_uuid`), `jobs`/`transactions` singletons, `_app_logger`, the registered Flask app handle |
| 1 | `auth_service`, `config_service`, `setup_service`, `job_service`, `serializers` | HTTP auth/CSRF/rate limits, config persistence, first-run state, job helpers, response shaping |
| 2 | `plex_service`, `acoustid_service`, `artwork_service`, `slskd_service`, `matching_service`, `musicbrainz_service` | providers; `matching_service` is adapters over `backend.matching` only |
| 3 | `cleanup_service`, `ytdlp_service`, `playlist_service` | album/folder cleanup, yt-dlp/SpotiFLAC, playlists |
| 4 | `ai_batch_state_service`, `import_reconciliation_service`, `pending_review_store`, `ai_evidence_service`, `library_service` | AI batch state, reconciliation orchestration, the review queue store, AI evidence, library/artist/album |
| 5 | `ai_service`, `import_service`, `acquisition_service`, `replacement_service`, `maintenance_service`, `import_review_service`, `dedup_service`, `transaction_service` | workflows |
| web | `routes_library`, `routes_cleanup`, `routes_import`, `routes_playlist`, `routes_acquisition`, `routes_maintenance`, `routes_system` (+ existing `routes_jobs`, `routes_lidarr`, `routes_setup`, `routes_submissions`) | HTTP |

The order is not the order the epic listed (Import Review first … System last):
a module can only be extracted after everything it depends on, so extraction
had to run leaves-first. The order above minimizes helpers pushed below their
domain (measured, then adjusted where a push was semantically wrong — e.g. the
review-queue store and AI batch state got their own modules instead of being
pulled into the library or reconciliation services).

## Compatibility

- `app.<name>` and `from app import <name>` still resolve every moved name
  (PEP 562 `app.__getattr__` over `app._ARCH001_OWNED_MODULES`), so existing
  route modules, scripts and tests addressing the old location keep working.
- Routes that were called in-process through `app.test_request_context(...)`
  now delegate to request-free services returning `(json_body, status)`:
  `start_album_download`, `start_reimport_disk`, `start_folder_import_with_id`,
  `start_dedup_scan`, `run_dedup_cleanup`, `start_fetch_missing_art`,
  `start_library_fix_genres`, `parse_playlist_request`, `start_playlist_download`,
  `get_library_payload`, `playlist_sync_status_payload`. The routes keep their
  historical return shapes (`serializers.json_route_result`).
- Worker threads that need an application context use
  `app_runtime.registered_flask_app()`; no service imports app.py.
- A test that re-imports `app` gets its own copy of each route module (the
  earlier instance keeps its routes), matching the old single-module behavior.

## Global-state audit

Rebinding (`global X; X = ...`) cannot cross module boundaries: a module that
imported `X` keeps the old object. Every such case was converted to shared,
mutated-in-place state:

| State | Was | Now |
|---|---|---|
| Library payload cache + playlist library index | `_lib_cache`, `_lib_cache_ts`, `_PLAYLIST_INDEX_CACHE` rebound by `_invalidate_lib_cache`/`_refresh_library_cache` | `backend/library_cache.LibraryCache` (`snapshot`/`store`/`invalidate`) |
| Last library scan job | `_last_scan_job_id` rebound by `_do_scan_job` | `_SCAN_STATE["last_job_id"]` |
| Job store / transaction store | module globals in app.py | `app_runtime.jobs` / `app_runtime.transactions` (one per process) |
| Flask app for worker contexts | `app` global | `app_runtime.register_flask_app` / `registered_flask_app` |
| Playlist JSON-state roots | `globals().get(...)` lookups (silently empty outside app.py) | explicit references |

Remaining module-level state is mutated in place and owned by one module
(caches, locks, registries):

- `app_runtime`: yt-dlp readiness/auth-smoke cache, plugin install log, MB tracklist cache lock, SQLite WAL lock;
- `auth_service`: auth rate-limit buckets; `artwork_service`: album-art cache;
  `matching_service`: disc caches; `musicbrainz_service`: artist cache;
- `playlist_service`: playlist state/manifest/sync/pipeline locks and download jobs;
- `ai_batch_state_service`: batch store registry, active-worker registry;
  `ai_service`: batch controls, match history lock; `pending_review_store`: queue/decision locks;
- `library_service`: attach-recording reservations; `import_service`: import job/target-preview/auto-import locks;
- `maintenance_service`: folder-cleanup/root-repair locks; `dedup_service`: dedup scan registry;
- web layer: `routes_library` artist-image cache, `routes_import` AI-batch skip event,
  `routes_acquisition` Download-All lock.

All of it is per process (Waitress, one process, many threads) as before.

## Jobs, transactions, security

- Jobs still use `job_engine.JobStore` (`app_runtime.jobs`) and `PythonJob`; no new
  job abstraction. ARCH-004 notes found while moving code: the in-process route
  calls hid job starts behind a fake request context (now explicit service
  calls); job identity/idempotency still differ per workflow (unchanged, ARCH-004).
- Plan/Apply/Rollback: the transaction families, `_install_transaction_job_hooks`
  and the generic rollback dispatcher moved verbatim; the ARCH-003 mutation
  inventory still lists 403 sinks with identical classifications.
- Security: the before-request auth/CSRF/rate-limit hook, security headers and
  error handlers stay in app.py; the public-endpoint allowlists moved with
  `auth_service`/`setup_service` and the endpoint inventory reads them there
  (250 endpoint entries, all security fields unchanged).

## Duplicate-deletion safety rule (Part 10)

Unattended duplicate deletion requires a shared **fingerprint** recording
identity (`fingerprint_verified`) or byte-identical files, plus the release-slot
safeguards (same album row or same release position, one copy per pair). A
shared embedded Recording ID alone is never enough. The rule lives in
`backend/duplicate_identity.select_unattended_cleanup_paths`;
`dedup_service._maintenance_duplicate_cleanup_paths` is the only unattended
caller. Both are pinned by tests.

## Remaining debt (ARCH-001 narrowed)

app.py is done. The route modules are not yet thin: several handlers still
orchestrate workflows inline and should delegate to their service. Route lines
per module (handlers over 100 lines):

| Route module | Route lines | >100-line handlers | Largest |
|---|---|---|---|
| `routes_library.py` | 4,543 | 13 | `item_attach_recording` (396), `ai_suggest` (338), `apply_album_duplicate_resolver` (256) |
| `routes_cleanup.py` | 1,938 | 5 | `dedup_ai_review` (430), `apply_folder_placeholder_action_api` (183) |
| `routes_import.py` | 1,458 | 3 | `import_review_queue` (279), `import_review_revalidate` (121) |
| `routes_maintenance.py` | 759 | 2 | `start_maintenance_runner` (494), `api_transaction_rollback` (137) |
| `routes_playlist.py` | 644 | 2 | `playlist_quality_cleanup` (113), `playlist_delete` (105) |
| `routes_acquisition.py` | 445 | 1 | `acquisition_download_all` (170) |

Owners: library/artwork → `library_service`/`artwork_service`; cleanup/dedup →
`cleanup_service`/`dedup_service`; import/review/AI → `import_service`,
`import_review_service`, `ai_service`; maintenance/transactions →
`maintenance_service`/`transaction_service`; playlist → `playlist_service`;
acquisition → `acquisition_service`. Also: 155 helpers are `EXTRACTED_SHARED`
(living in a lower-layer module than their domain) and could move to a shared
kernel when those services are next touched.

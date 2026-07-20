# Architecture

This document describes the architecture that exists today and the intended direction. It does not claim that the intended architecture is complete.

## Current Main Components

- `app.py`: primary Flask application. Repository inspection found most API routes still registered here, including import review, library, cleanup, deduplication, playlists, Plex, configuration, and transactions. It also contains domain logic, provider calls, Beets subprocess calls, direct SQLite access, and filesystem mutation helpers.
- `routes_jobs.py`: split route module for `/api/jobs/*` job listing, lookup, and cancellation.
- `routes_lidarr.py`: split route module for Lidarr/wanted endpoints.
- `routes_setup.py`: split route module for setup and configuration checks.
- `routes_submissions.py`: split route module for MusicBrainz/AcoustID submission workflow and MBID attachment.
- `job_engine.py`: in-memory `Job`, `PythonJob`, `JobStore`, structured state support, cooperative cancellation, log retention, and `_beet_run` subprocess wrapper.
- `helpers_mb.py`: MusicBrainz and AcoustID helper functions. It has no `app.py` dependency and is the strongest current provider boundary.
- `backend/`: partially extracted helper package. Notable modules include `album_match.py`, `audio_preferences.py`, `import_guard.py`, `mb_alignment.py`, `security.py`, `slskd.py`, `title_normalize.py`, `track_align.py`, and `transaction_engine.py`.
- `frontend/src/`: React/Next/TypeScript frontend. `frontend/src/api/client.ts` centralizes API calls, `frontend/src/api/types.ts` centralizes many response shapes, and views/features are split under `views/` and `features/`.
- `.github/workflows/`: CI covers Python syntax/unit tests, frontend typecheck/build, lint, Docker build, dependency audit, compose/security checks, and secret scan.

## Intended Dependency Direction

```text
Frontend
  -> API routes
  -> Application services
  -> Domain decisions
  -> Provider adapters and repositories
  -> Beets, filesystem, database, and external services
```

Current migration status: incomplete. The `backend/` package and `helpers_mb.py` already provide useful extraction seams, but `app.py` still contains route handlers, domain decisions, provider orchestration, Beets calls, direct DB access, direct filesystem mutations, job orchestration, and UI-shaped response construction.

## External Boundaries

- Beets: command execution uses `BEET_BIN`, `_beet_env()`, `_beet_run()`, and direct command arrays in `app.py` and `routes_submissions.py`.
- Beets database: `LIB_PATH` points to `/config/musiclibrary.blb` by default. `_db()` wraps SQLite access, but many code paths still call `sqlite3.connect(LIB_PATH)` directly.
- MusicBrainz and AcoustID: `helpers_mb.py` performs release, release-group, recording, and AcoustID lookup work. `routes_submissions.py` also performs MusicBrainz validation and AcoustID submission orchestration.
- AI provider: `app.py` includes OpenAI-key checks and AI suggestion calls. AI availability is already modeled in some paths, but matching still needs one shared contract.
- Download providers: SLSKD extraction exists in `backend/slskd.py`, while orchestration remains heavily in `app.py`.
- Plex: Plex endpoints and playlist sync logic are still in `app.py`.
- Filesystem: canonical roots include `MUSIC_ROOT` (`/data/media/music`) and `DOWNLOADS_ROOT` (`/data/torrents/music`). Root-validation helpers exist in several areas, but mutations are not yet funneled through one boundary.

## State Ownership

- Beets is the library source of truth.
- Beets SQLite data is accessed through Beets objects and direct SQLite reads/writes.
- Job state lives in `JobStore` in memory; selected workflows also persist checkpoints or last-run JSON under `/config` or metadata cache paths.
- Transaction/audit state lives in `backend.transaction_engine.TransactionStore`, file-backed under `BEETS_TRANSACTION_DIR` or `/config/transactions`.
- Import review state is composed from Beets library data plus JSON review files and computed evidence.
- Frontend server state is fetched through `frontend/src/api/client.ts`; some shared polling uses TanStack Query, while larger feature pages still use local React state and effects.

## Job Lifecycle

Existing lifecycle:

1. Routes start subprocess jobs through `JobStore.start()` or Python callables through `JobStore.start_python()`.
2. `PythonJob` accepts `(log, cancel_event, update_state)` for jobs that support structured state.
3. `/api/jobs/*` serializes job status, logs, metadata, and compact results.
4. Some workflows add their own checkpoint files and resume logic, especially playlist and AI-batch/import flows.

Intended direction:

- Every long-running workflow should have an operation id or idempotency key, durable progress, bounded retries, cancellation checks between safe steps, and clear terminal states.
- Jobs should orchestrate application services and checkpoint state rather than duplicate matching or mutation business rules inline.

## Matching Lifecycle

Existing entry points include:

- Import review AI and candidate flow in `app.py` around item/album/folder AI suggestion, target preview, auto-enqueue, revalidation, and attach/match routes.
- MusicBrainz and AcoustID helpers in `helpers_mb.py`.
- Track alignment in `backend/track_align.py` and `backend/mb_alignment.py`.
- Import safety decisions in `backend/import_guard.py`.
- Playlist matching in `app.py` around `_match_playlist_tracks`, reference matching, and quality-place flows.
- Missing-track replacement and Music Format Preferences matching in `app.py`.
- Submission preparation and MusicBrainz validation in `routes_submissions.py`.

Intended direction:

- Converge matching entry points on one shared result contract containing local metadata, candidate identities, release-group ID, optional release ID, recording IDs, AcoustID evidence, tracklist evidence, duration evidence, filename/tag evidence, AI availability/contribution, confidence, conflicts, warnings, explanation, and action eligibility.
- Keep AI as an optional contributor, never the source of truth.

## Mutation Lifecycle

Existing mutation mechanisms include:

- Direct Beets `modify`, `write`, `move`, and `import` command arrays in `app.py` and `routes_submissions.py`.
- Direct filesystem moves, deletes, copies, and directory removals in `app.py`.
- A file-backed `TransactionStore` in `backend/transaction_engine.py` with statuses, changes, metadata diffs, rollback fields, settings, and job attachment.
- Several workflow-specific preview/dry-run routes, including import target preview, cleanup scans, folder placeholder preview, and transaction endpoints.

Intended direction:

1. Inspect current state.
2. Produce a mutation plan.
3. Validate roots, identities, conflicts, and preconditions.
4. Display or record metadata and filesystem diffs.
5. Apply through Beets/filesystem steps with audit records.
6. Verify final filesystem and application state.
7. Record completed steps and recovery information.

Current migration status: partial. The transaction engine is a foundation, but not every mutating route uses it yet.

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

- `app.py` route/domain/mutation/job coupling.
- Duplicated matching and confidence rules across import review, playlist, replacement, cleanup, and submission flows.
- Direct filesystem and Beets mutation calls outside a single controlled mutation boundary.
- Job idempotency and checkpoint consistency across all long-running workflows.
- Consistent provider-adapter contracts for AI, MusicBrainz, AcoustID, Plex, and download providers.
- Large frontend modules that mix rendering, polling, local state machines, and decision presentation.
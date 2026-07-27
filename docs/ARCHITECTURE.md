# Architecture

This document describes the architecture that exists today and the intended direction. It does not claim that the intended architecture is complete.

## Current Main Components

- `beets` (Beets Engine Container): authoritative Beets installation built from `Dockerfile.beets` pinned to `lscr.io/linuxserver/beets:2.4.0@sha256:4cce2967154e849ca5466469d934a4bb9d8d83c3acf2a5279da47bccd16518f1`. Contains the Beets CLI, `/config/config.yaml`, `/config/musiclibrary.blb`, bundled plugins (`/opt/beets-web-manager-agent/beetsplug/discpath.py`), and the HTTP control agent (`backend/beets_control_agent.py`) supervised under S6 (`/custom-services.d/beets-control-agent`). Port 8338 is internal-only.
- `beets-web-manager` (Web Manager Container): lightweight Flask + React/Next application built from `Dockerfile`. Contains zero Beets binaries, zero Beets Python packages, and zero local SQLite database files. Communicates with the Beets engine strictly over internal HTTP using `BeetsClient` and `RemoteLibrary` (`backend/beets_client.py`).
- `app.py`: primary Flask application serving the web interface, operator routes, import workflows, matching adjudication, job tracking, and proxying requests to the remote Beets control agent.
- `routes_jobs.py`: split route module for `/api/jobs/*` job listing, lookup, and cancellation.
- `routes_lidarr.py`: split route module for Lidarr/wanted endpoints.
- `routes_setup.py`: split route module for setup, authentication, and configuration checks.
- `routes_submissions.py`: split route module for MusicBrainz/AcoustID submission workflow and MBID attachment.
- `job_engine.py`: in-memory `Job`, `PythonJob`, `JobStore`, structured state support, cooperative cancellation, log retention, and remote control agent task integration.
- `helpers_mb.py`: MusicBrainz and AcoustID helper functions. It has no `app.py` dependency and is the strongest current provider boundary.
- `backend/`: helper package containing `beets_client.py` (remote Beets API client), `beets_control_agent.py` (Beets container control agent), `album_match.py`, `audio_preferences.py`, `import_guard.py`, `mb_alignment.py`, `security.py`, `slskd.py`, `title_normalize.py`, `track_align.py`, and `transaction_engine.py`.
- `frontend/src/`: React/Next/TypeScript frontend. `frontend/src/api/client.ts` centralizes API calls, `frontend/src/api/types.ts` centralizes many response shapes, and views/features are split under `views/` and `features/`.
- `.github/workflows/`: CI covers Python syntax/unit tests, frontend typecheck/build, lint, Docker build, dependency audit, compose/security checks, and secret scan.

## Intended Dependency Direction

```text
Frontend
  -> Web Manager Routes (app.py, routes_*.py)
  -> Remote Beets API Client (backend/beets_client.py)
  -- HTTP (internal port 8338 with Bearer token) -->
  -> Beets Control Agent (backend/beets_control_agent.py in beets container)
  -> Beets CLI, SQLite DB (/config/musiclibrary.blb), & Media Filesystem
```

Current migration status: incomplete. External engine separation is complete; the web manager service has zero direct Beets imports or SQLite file handles, delegating all library, database, tag, and media mutations to the `beets` container via authenticated internal HTTP APIs.

## External Boundaries

- Beets Engine & CLI: All library mutations, database queries, tag writes, and Beets commands run inside the `beets` container under control-agent supervision or manual CLI execution (`docker compose exec beets /lsiopy/bin/beet ...`).
- Control Agent Supervision: LinuxServer S6 supervises `/opt/beets-web-manager-agent/beets_control_agent.py` at `/custom-services.d/beets-control-agent`. If the agent crashes, S6 restarts it automatically without terminating the container.
- Database & Lock Ownership: The single authoritative database `/config/musiclibrary.blb` lives exclusively in the `beets` container. Mutating operations acquire the shared file lock `/config/.beet_db.lock`. Manual CLI commands and control agent jobs serialize on this lock.
- Port Exposure: Control agent port 8338 is exposed internally to the Docker bridge network only (`expose: ["8338"]`). It is never published to host interfaces. Web manager port 8337 (`WEBCONTROL_PORT`) is published to localhost (`127.0.0.1:8337`) or behind a reverse proxy.
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
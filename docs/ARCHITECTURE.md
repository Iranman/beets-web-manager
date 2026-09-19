# Architecture

This document describes the architecture that exists today and the intended direction. It does not claim that the intended architecture is complete.

## Non-Negotiable Rules

These are standing product/architecture invariants, not aspirations. Each is backed by an Architecture Decision Record under `docs/adr/` and, where practical, a regression test — see the linked ADR for full context and consequences.

- Beets remains the library backend and source of library mutations; the app does not grow a parallel music-library database. (`docs/adr/0001-beets-remains-library-backend.md`)
- MusicBrainz and AcoustID are the primary identity evidence. The canonical album-level identity is the MusicBrainz release-group ID (`mb_releasegroupid`); a release ID (`mb_albumid`) is edition-level secondary data and must never be substituted where a release-group ID is required. (`docs/adr/0002-release-group-id-is-canonical-album-identity.md`)
- AI is optional and untrusted. It may rank or explain candidates already found through deterministic sources; it must not invent an identity and treat it as verified, and its unavailability must never stop MusicBrainz/AcoustID matching. (`docs/adr/0003-ai-is-optional-and-not-source-of-truth.md`)
- No silent library mutations. Any move, rename, merge, delete, tag write, replacement, or artwork write requires the controlled preview/apply/audit/recovery workflow in `backend/transaction_engine.py`. (`docs/adr/0004-library-mutations-use-controlled-workflow.md`)
- Long-running operations use the shared job infrastructure (`job_engine.py`) rather than ad hoc background threads or bespoke checkpoint logic. (`docs/adr/0005-long-running-operations-use-shared-job-infrastructure.md`)
- Ambiguous or conflicting evidence goes to review; destructive actions require stronger evidence than suggestions.
- Never expose secrets in logs, API responses, frontend state, or committed files.

## Current Main Components

- `beets` (Stock Beets Engine Container): Standard upstream LinuxServer Beets container (`lscr.io/linuxserver/beets:2.13.1` or `:latest`). Provides the operator-accessible Beets CLI environment, `/config/config.yaml`, and the authoritative `/config/musiclibrary.blb` database file. Does not require port exposure or custom services.
- `beets-web-manager` (Web Manager Container): Production web application built from `Dockerfile`. Bundles `beets==2.13.1`, `ffmpeg`, `libchromaprint-tools` (`fpcalc`), and an embedded `beets_control_agent` running strictly on loopback (`127.0.0.1:8338`). Serves the web UI on port 8337, executes background jobs, orchestrates Beets commands, and validates metadata.
- `app.py`: primary Flask application serving the web interface, operator routes, import workflows, matching adjudication, job tracking, and communicating with the embedded control agent on loopback (or a remote agent if configured).
- `routes_jobs.py`: split route module for `/api/jobs/*` job listing, lookup, and cancellation.
- `routes_lidarr.py`: split route module for Lidarr/wanted endpoints.
- `routes_setup.py`: split route module for setup, authentication, and configuration checks.
- `routes_submissions.py`: split route module for MusicBrainz/AcoustID submission workflow and MBID attachment.
- `job_engine.py`: in-memory `Job`, `PythonJob`, `JobStore`, structured state support, cooperative cancellation, log retention, and control agent task integration.
- `helpers_mb.py`: MusicBrainz and AcoustID helper functions. It has no `app.py` dependency and is the strongest current provider boundary.
- `backend/`: helper package containing `beets_client.py` (Beets API client), `beets_control_agent.py` (embedded/remote control agent), `album_match.py`, `audio_preferences.py`, `import_guard.py`, `mb_alignment.py`, `security.py`, `slskd.py`, `title_normalize.py`, `track_align.py`, and `transaction_engine.py`.
- `frontend/src/`: React/Next/TypeScript frontend. `frontend/src/api/client.ts` centralizes API calls, `frontend/src/api/types.ts` centralizes response shapes, and views/features are split under `views/` and `features/`.
- `.github/workflows/`: CI covers Python syntax/unit tests, frontend typecheck/build, lint, Docker build, dependency audit, compose/security checks, and production Docker acceptance.

## Intended Dependency Direction

```text
Frontend (Browser)
  -> Web Manager Routes (app.py, routes_*.py on port 8337)
  -> Beets Client (backend/beets_client.py)
  -- HTTP (internal loopback 127.0.0.1:8338 with auto-token) -->
  -> Embedded Control Agent (backend/beets_control_agent.py in web-manager)
  -> Beets CLI, SQLite DB (/config/musiclibrary.blb), & Shared Media Filesystem
```

In the standard unified deployment, both containers share the exact same filesystem mounts:
- `/config` (Beets configuration and authoritative SQLite library)
- `/music` (Target music library collection)
- `/downloads` (Incoming download staging folder)
- `/data` (Web Manager durable application state: settings, wizard completion, audit logs)

## External Boundaries & Locking Model

- **Authoritative Database**: The single authoritative database `/config/musiclibrary.blb` is shared directly between containers. Both the Web Manager (via its embedded Beets runtime) and the stock Beets container open the same database file.
- **Database-Level Locking**: SQLite provides cross-process ACID file locking on `/config/musiclibrary.blb`, ensuring database integrity across concurrent reads and transactions.
- **Application-Level Locking**: Web Manager operations acquire a higher-level file lock (`/config/.beet_db.lock`) during multi-step controlled mutations (Clean All, batch move/rename, metadata repair, deduplication).
- **Manual CLI Mutation Safety**: Manual CLI commands run in the stock Beets container (`docker compose exec beets beet ...`) acquire SQLite file locks, but do *not* acquire Web Manager's higher-level `.beet_db.lock`.
  - **Read-only CLI queries** (`beet ls`, `beet version`, `beet stats`) are completely safe to run anytime.
  - **Mutating CLI commands** (`beet import`, `beet modify`, `beet rm`) should **not** be run concurrently while Web Manager is actively executing multi-step mutation jobs.
- **Port Exposure**: In the standard stack, port 8338 is internal loopback only (`127.0.0.1:8338`) inside the Web Manager container and is never published to the host or Docker bridge. Web manager port 8337 (`WEBCONTROL_PORT`) is published to `0.0.0.0:8337` (or `127.0.0.1:8337`) for browser access.
- **Health and Readiness**: `/api/health` validates Web Manager liveness; `/health/ready` and `/api/setup/status` query the embedded Beets control agent for Beets readiness, loaded plugins (`chroma`, `musicbrainz`), and filesystem health.
- **MusicBrainz and AcoustID**: `helpers_mb.py` performs release, release-group, recording, and AcoustID lookup work. `routes_submissions.py` performs MusicBrainz validation and AcoustID submission orchestration.
- **Submission Command Capability**: `submit` (AcoustID) and `mbsubmit` (MusicBrainz) commands verify that required plugins are actually initialized inside Beets before execution.

## State Ownership

- **Library State**: `/config/musiclibrary.blb` is the authoritative Beets library.
- **Web Manager Durable State**: `/data` (`WEB_MANAGER_DATA_DIR`, mounted to `./web-manager`) holds `app_settings.json`, `.setup_complete`, `.auth_token`, and session encryption keys.
- **Job State**: Lives in `JobStore` in memory; structured progress and logs are exposed via `/api/jobs/*`.
- **Audit & Transaction State**: Stored in `backend.transaction_engine.TransactionStore` under `/data/transactions`.
- **Import Review State**: Staged under `/downloads` and tracked in `/data/unmatched_drafts`.

## Job Lifecycle

Existing lifecycle:

1. Routes start subprocess jobs through `JobStore.start()` or Python callables through `JobStore.start_python()`.
2. `PythonJob` accepts `(log, cancel_event, update_state)` for jobs that support structured state.
3. `/api/jobs/*` serializes job status, logs, metadata, and compact results.
4. Some workflows add their own checkpoint files and resume logic, especially playlist and AI-batch/import flows.
5. `job_engine.Job` (the remote-command job wrapper) tracks an explicit `_state` (`created`, `dispatching`, `running`, `cancelling`, `cancelled`, `success`, `failed`, `cancel_failed`, or a passthrough remote status such as `timeout`) rather than inferring status from a cancel-requested flag. A `cancelled` result is only ever reported once the remote control agent's own job record confirms it; a real remote `success`/`failed` that arrives after cancellation was requested is preserved as-is (with a log note that cancellation arrived too late), never silently overwritten. If cancellation is requested but the remote job never reaches a confirmed terminal state within `REMOTE_CANCEL_CONFIRM_TIMEOUT` (default 30s, `BEETS_CANCEL_CONFIRM_TIMEOUT` env override, injectable per-`Job` for tests), the job reports `cancel_failed` with a nonzero return code and a log line stating cancellation was not confirmed -- it never reports `cancelled` without that confirmation.

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

- Beets `modify`, `write`, `move`, `import`, `submit`, and `mbsubmit` command arrays in `app.py`, `job_engine.py`, and `routes_submissions.py`. These arrays are normalized and executed remotely through `backend.beets_client.BeetsClient`; the web-manager container never shells out to `beet` locally (enforced by an AST-based structural test, `tests/test_arch003_boundary_enforcement.py`).
- Control-agent endpoints for Beets commands, tag writes, file moves/deletes, and structured library reads inside the `beets` container.
- A file-backed `TransactionStore` in `backend/transaction_engine.py` (plan/apply/verify/recover) with statuses, changes, metadata diffs, rollback fields, and job attachment.
- Engine-owned source inspection for import/reimport: `POST /imports/source/inspect` (`backend/beets_control_agent.py:inspect_import_source()`), `POST /imports/source/discover`, and `POST /imports/source/preserve` validate and inventory engine-owned paths from inside the engine container, which has the real filesystem view the web manager does not have in any shipped Compose topology.
- Several workflow-specific preview/dry-run routes, including import target preview, cleanup scans, folder placeholder preview, and transaction endpoints.

Intended direction (now the default shape for every production mutation path):

1. Inspect current state.
2. Produce a mutation plan.
3. Validate roots, identities, conflicts, and preconditions.
4. Display or record metadata and filesystem diffs.
5. Apply through Beets/filesystem steps with audit records.
6. Verify final filesystem and application state.
7. Record completed steps and recovery information.

Current migration status: the controlled-mutation boundary itself is complete for every production Beets/media mutation path (`security/arch003_mutation_inventory.json`'s CI gate holds at 0 unresolved blockers) and the web-manager container performs zero local Beets CLI execution and zero local Beets database/media mutation — all of that runs inside the `beets` container behind the control agent. The web manager still owns and writes its own local state (`/web-manager-data`: config store, auth token, transaction/audit records), which is expected, not a boundary violation. What remains open is not the mutation boundary's existence but converging the many entry points that reach it on one shared matching/confidence contract — see `docs/TECHNICAL_DEBT.md` (ARCH-002).

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

- `app.py` route/domain/mutation/job coupling (ARCH-001).
- Duplicated matching and confidence rules across import review, playlist, replacement, cleanup, and submission flows: a canonical matching evidence engine exists (`backend/matching/`), but not every production entry point uses it yet (ARCH-002).
- Routes that still express a Beets library *read* as a raw-SQL-shaped compatibility call instead of an explicit `BeetsClient` repository method (ARCH-007).
- Job idempotency and checkpoint consistency across all long-running workflows (ARCH-004).
- Consistent provider-adapter contracts for AI, MusicBrainz, AcoustID, Plex, and download providers (ARCH-006).

See `docs/TECHNICAL_DEBT.md` for the full current list, including affected areas, risk, and desired state for each.
- Large frontend modules that mix rendering, polling, local state machines, and decision presentation.

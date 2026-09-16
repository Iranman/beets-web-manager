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

- `beets` (Beets Engine Container): authoritative Beets installation built from `Dockerfile.beets`, whose `ARG BEETS_BASE_IMAGE` selects the upstream LinuxServer Beets image at build time (default: the tested production candidate, currently `lscr.io/linuxserver/beets:2.13.1`; see `docs/BEETS_ENGINE_MIGRATION.md` for the version policy and `docker/beets/apply_patches.py` for the version-aware plugin-resolution compatibility patch). Contains the Beets CLI, `/config/config.yaml`, `/config/musiclibrary.blb`, bundled plugins (`/opt/beets-web-manager-agent/beetsplug/discpath.py`), and the HTTP control agent (`backend/beets_control_agent.py`) supervised under S6 (`/custom-services.d/beets-control-agent`). Port 8338 is internal-only.
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

Current migration status: the service split is complete and every production Beets/media mutation runs behind the control-agent boundary. The web manager service has zero direct Beets imports and no local SQLite file handles; `backend.beets_client.RemoteLibrary` and `RemoteSQLiteConnection` translate legacy library/SQL call shapes into authenticated internal HTTP requests — a legacy raw-SQL-shaped *read* attempted over that compatibility layer is a hard, unconditional failure by design (`raw_sqlite_query()` always raises), not a silent fallback, so a route still expressing a read that way is non-functional rather than degraded. Remaining work is replacing the routes that still express reads as raw-SQL-shaped compatibility calls with explicit, narrow `BeetsClient` repository methods (see `docs/TECHNICAL_DEBT.md`, ARCH-007) — route/service migration toward thin routes calling explicit services is otherwise still in progress (ARCH-001).

## External Boundaries

- Beets Engine & CLI: All library mutations, database queries, tag writes, and Beets commands run inside the `beets` container under control-agent supervision or manual CLI execution (`docker compose exec beets /lsiopy/bin/beet ...` for read-only, `docker compose exec beets beet-locked ...` for mutating commands).
- Control Agent Supervision: LinuxServer S6 supervises `/opt/beets-web-manager-agent/beets_control_agent.py` at `/custom-services.d/beets-control-agent`. If the agent crashes, S6 restarts it automatically without terminating the container.
- Database & Lock Ownership: The single authoritative database `/config/musiclibrary.blb` lives exclusively in the `beets` container. Mutating operations acquire the shared file lock `/config/.beet_db.lock`. Manual CLI commands (via `beet-locked`) and control agent jobs serialize on this lock.
- Port Exposure: Control agent port 8338 is exposed internally to the Docker bridge network only (`expose: ["8338"]`). It is never published to host interfaces. Web manager port 8337 (`WEBCONTROL_PORT`) is published to localhost (`127.0.0.1:8337`) or behind a reverse proxy.
- Health and readiness: `/api/health` is web-manager liveness only. `/health/ready` and `/api/setup/status` query the authenticated remote control agent for authoritative Beets readiness, loaded-plugin state, command capabilities, and engine path health.
- MusicBrainz and AcoustID: `helpers_mb.py` performs release, release-group, recording, and AcoustID lookup work. `routes_submissions.py` also performs MusicBrainz validation and AcoustID submission orchestration.
- Submission command capability: the `submit` (AcoustID, via the `chroma` plugin) and `mbsubmit` (MusicBrainz) remote commands are gated by whether Beets actually finished loading the required plugin, not merely whether the plugin module is importable on disk -- a plugin can be present and importable (`beetsplug.chroma`) and still fail to initialize inside Beets (e.g. `chroma` silently failing to load even with `fpcalc`/`pyacoustid` present), so `backend/beets_control_agent.py`'s `get_loaded_beet_plugins()` parses the actual "plugins: ..." line from `beet version`'s own output rather than using `importlib.util.find_spec()`. Capability is enforced in two independent places: `routes_submissions._submission_readiness()` on the web-manager side (fails closed -- any remote connection failure, authentication failure, or malformed `/status` response reports every capability unavailable, never a local `find_spec()`/`shutil.which()` fallback), and `backend/beets_control_agent.py`'s `require_command_capability()` inside `/commands/execute` and `/jobs/create` themselves, so a caller that reaches the control agent directly (bypassing the web-manager route) is still blocked before any subprocess or job object is created.
- AI provider: `app.py` includes OpenAI-key checks and AI suggestion calls. AI availability is already modeled in some paths, but matching still needs one shared contract.
- Download providers: SLSKD extraction exists in `backend/slskd.py`, while orchestration remains heavily in `app.py`.
- Plex: Plex endpoints and playlist sync logic are still in `app.py`.
- Filesystem: canonical roots include `MUSIC_ROOT` (`/data/media/music`) and `DOWNLOADS_ROOT` (`/data/torrents/music`). Root-validation helpers exist in several areas, but mutations are not yet funneled through one boundary.

## State Ownership

- Beets is the library source of truth.
- Beets SQLite data is stored and directly opened only inside the `beets` container. The control agent still uses direct `sqlite3.connect(LIB_PATH)` for several compatibility endpoints. The web manager reaches those paths through `backend.beets_client.RemoteSQLiteConnection`, not a local file handle.
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

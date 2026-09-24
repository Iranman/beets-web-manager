# Architecture

This document describes the architecture that exists today and the intended direction. It does not claim that the intended architecture is complete.

## Non-Negotiable Rules

These are standing product/architecture invariants, not aspirations. Each is backed by an Architecture Decision Record under `docs/adr/` and, where practical, a regression test — see the linked ADR for full context and consequences.

- **Beets Web Manager builds on stock Beets; it does not build Beets.** The sole authoritative Beets runtime is the published `lscr.io/linuxserver/beets` image, run as its own container. Web Manager provides the UI, workflow orchestration, job tracking, and audit/rollback state, and talks to that container only over HTTP — never a local Beets Python import, a control-agent process, a Docker socket, `docker exec`, or a custom Beets image. (`docs/adr/0001-beets-remains-library-backend.md`)
- Beets remains the library backend and source of library mutations; the app does not grow a parallel music-library database. (`docs/adr/0001-beets-remains-library-backend.md`)
- MusicBrainz and AcoustID are the primary identity evidence. The canonical album-level identity is the MusicBrainz release-group ID (`mb_releasegroupid`); a release ID (`mb_albumid`) is edition-level secondary data and must never be substituted where a release-group ID is required. (`docs/adr/0002-release-group-id-is-canonical-album-identity.md`)
- AI is optional and untrusted. It may rank or explain candidates already found through deterministic sources; it must not invent an identity and treat it as verified, and its unavailability must never stop MusicBrainz/AcoustID matching. (`docs/adr/0003-ai-is-optional-and-not-source-of-truth.md`)
- No silent library mutations. Any move, rename, merge, delete, tag write, replacement, or artwork write requires the controlled preview/apply/audit/recovery workflow in `backend/transaction_engine.py`. (`docs/adr/0004-library-mutations-use-controlled-workflow.md`)
- Long-running operations use the shared job infrastructure (`job_engine.py`) rather than ad hoc background threads or bespoke checkpoint logic. (`docs/adr/0005-long-running-operations-use-shared-job-infrastructure.md`)
- Ambiguous or conflicting evidence goes to review; destructive actions require stronger evidence than suggestions.
- Never expose secrets in logs, API responses, frontend state, or committed files.

## Current Main Components

- `beets` (Stock Beets Container): Standard upstream LinuxServer Beets container (`lscr.io/linuxserver/beets:latest`). Owns `/config/config.yaml`, the authoritative `/config/musiclibrary.blb` database, and all `/music` writes. Runs the built-in `web` plugin (read-only HTTP API) and the bundled `webmanager` integration plugin (the only mutation surface Web Manager is allowed to call), both on its own internal `:8337`.
- `beets-web-manager` (Web Manager Container): Production web application built from `Dockerfile`. Has no local Beets Python runtime (`requirements.txt` does not install `beets`) and performs zero local Beets database or media-file mutation. Serves the browser UI and its own API on port 8337, executes background jobs, and talks to stock Beets exclusively through `backend/beets_adapter.py`.
- `app.py`: primary Flask application serving the web interface, operator routes, import workflows, matching adjudication, and job tracking.
- `routes_jobs.py`: split route module for `/api/jobs/*` job listing, lookup, and cancellation.
- `routes_lidarr.py`: split route module for Lidarr/wanted endpoints.
- `routes_setup.py`: split route module for setup, authentication, and configuration checks; sources all Beets/plugin diagnostics from `backend/beets_adapter.py` and `backend/beets_plugins.py`.
- `routes_submissions.py`: split route module for MusicBrainz/AcoustID submission workflow and MBID attachment; AcoustID submission runs through the real `beetsplug.chroma` plugin inside stock Beets via `beets_adapter.mbsubmit()`.
- `job_engine.py`: in-memory `PythonJob`, `JobStore`, structured state support, cooperative cancellation, and log retention for Web-Manager-side workflow execution.
- `helpers_mb.py`: MusicBrainz and AcoustID helper functions. It has no `app.py` dependency and is the strongest current provider boundary.
- `backend/beets_adapter.py`: the only supported transport to stock Beets — a narrow `BeetsAdapter` client for the `web` plugin's reads and the `webmanager` plugin's authenticated mutation operations (`modify`, `move`, `remove`, `mbsync`, `fetch_art`, `embed_art`, `lastgenre`, `mbsubmit`).
- `beetsplug/webmanager/`: the integration plugin itself, provisioned by Web Manager into stock Beets' `/config/beetsplug` and loaded by stock Beets like any other Beets plugin. Exposes `/webmanager/status` (handshake: protocol/plugin/Beets versions, loaded plugins, capabilities) and the operation endpoints `BeetsAdapter` calls.
- `backend/`: helper package. `beets_adapter.py` and `beets_plugins.py` (plugin provisioning/health) are the stock-Beets integration surface; `album_match.py`, `audio_preferences.py`, `import_guard.py`, `mb_alignment.py`, `security.py`, `slskd.py`, `title_normalize.py`, `track_align.py`, and `transaction_engine.py` are Web-Manager-local domain/orchestration logic. `beets_client.py` (the retired control-agent HTTP client) is **not yet deleted** — see `docs/TECHNICAL_DEBT.md` (ARCH-010): a large set of composite mutation workflows in `app.py` still call it and are currently non-functional pending migration onto `beets_adapter.py`.
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

- Narrow, operation-specific `BeetsAdapter` methods (`modify`, `move`, `remove`, `mbsync`, `fetch_art`, `embed_art`, `lastgenre`, `mbsubmit`) — the only way Web Manager reaches a stock-Beets mutation. The web-manager container never shells out to `beet` locally and never runs a local Beets Python runtime (enforced by an AST-based structural test, `tests/test_arch003_boundary_enforcement.py`).
- A file-backed `TransactionStore` in `backend/transaction_engine.py` (plan/apply/verify/recover) with statuses, changes, metadata diffs, rollback fields, and job attachment. This bookkeeping is pure Web-Manager-local orchestration; it does not itself talk to Beets.
- Several workflow-specific preview/dry-run routes, including import target preview, cleanup scans, folder placeholder preview, and transaction endpoints.

Intended direction (the target shape for every production mutation path):

1. Inspect current state (via `BeetsAdapter` reads).
2. Produce a mutation plan.
3. Validate roots, identities, conflicts, and preconditions.
4. Display or record metadata and filesystem diffs.
5. Apply through `BeetsAdapter` calls with audit records.
6. Verify final state (via `BeetsAdapter` reads).
7. Record completed steps and recovery information.

Current migration status: `job_engine.py`, `routes_setup.py`, and `routes_submissions.py` are fully migrated onto `backend/beets_adapter.py`, with zero remaining references to the retired `backend/beets_client.py` control-agent client. **This is not yet true of `app.py`'s composite Plan/Apply/Rollback workflows** — merge-album, merge-artist, Clean All, track replacement, folder/album cleanup, artist-folder reconcile, album maintenance/relocation/metadata-repair, artwork, genre repair, mbsync-all, and move-all still call `backend/beets_client.py` and are currently non-functional against the real stock-Beets stack. This is tracked as the top-priority open item — see `docs/TECHNICAL_DEBT.md` (ARCH-010) — not as a closed migration.

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

- `backend/beets_client.py` and the composite mutation workflows in `app.py` that still call it instead of `backend/beets_adapter.py` (ARCH-010) — the largest and highest-priority open item.
- `app.py` route/domain/mutation/job coupling (ARCH-001).
- Duplicated matching and confidence rules across import review, playlist, replacement, cleanup, and submission flows: a canonical matching evidence engine exists (`backend/matching/`), but not every production entry point uses it yet (ARCH-002).
- Job idempotency and checkpoint consistency across all long-running workflows (ARCH-004).
- Consistent provider-adapter contracts for AI, MusicBrainz, AcoustID, Plex, and download providers (ARCH-006).

See `docs/TECHNICAL_DEBT.md` for the full current list, including affected areas, risk, and desired state for each.
- Large frontend modules that mix rendering, polling, local state machines, and decision presentation.

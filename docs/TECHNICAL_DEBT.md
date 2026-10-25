# Technical Debt Register

Current, unresolved architecture and security debt only. Statuses: Open, Accepted Risk. Resolved items are removed from this document once closed — their history lives in Git and in the pull request that closed them, not here.

Status stages (never collapsed): IMPLEMENTED (code merged-ready with tests) -> CI VERIFIED (all repository gates green) -> LIVE VERIFIED (production acceptance on TrueNAS) -> CLOSED (desired state reached).

Each entry: affected area, evidence, current risk, desired state, safe migration approach, priority, status.

## ARCH-001 Route Handlers Still Orchestrate Workflows

- Affected area: route modules `routes_library.py`, `routes_cleanup.py`, `routes_import.py`, `routes_maintenance.py`, `routes_playlist.py`, `routes_acquisition.py`.
- Evidence: v0.1.31 decomposed `app.py` (52,665 -> ~570 lines of application glue) into layered owned services with CI guards (see `docs/arch001_service_decomposition.md`; inventory `docs/arch001_app_ownership.json`). What remains (re-measured for v0.1.42: still 26): 26 route handlers over 100 lines still orchestrate inline (largest: `start_maintenance_runner` 494, `dedup_ai_review` 430, `item_attach_recording` 396, `ai_suggest` 338, `apply_album_duplicate_resolver` 256), and 155 helpers live in a lower-layer module than their domain (`EXTRACTED_SHARED`).
- Current risk: Workflow logic inside a handler is only reachable through HTTP and is harder to reuse or test directly; shared helpers sit in a neighbor domain's module.
- Desired state: Every handler parses input, calls one service, and shapes the response. Owners: library/artwork -> `library_service`/`artwork_service`; cleanup/dedup -> `cleanup_service`/`dedup_service`; import/review/AI -> `import_service`/`import_review_service`/`ai_service`; maintenance/transactions -> `maintenance_service`/`transaction_service`; playlist -> `playlist_service`; acquisition -> `acquisition_service`.
- Safe migration approach: Move one handler body at a time into its service as a request-free function returning `(json_body, status)` (the pattern used for the in-process route calls in v0.1.31); keep the route shape via `serializers.json_route_result`; `tests/test_arch001_architecture.py` keeps layering intact.
- 0.7.0 progress: `import_review_queue` (was 279 lines) no longer builds the library missing-ID rows; they moved to `library_service.library_missing_mb_payload` behind `GET /api/library/missing-mb-ids`. The handler still assembles the pending/skipped rows inline (about 130 lines).
- Priority: P2. Status: Open.

## ARCH-004 Job Persistence And Idempotency Are Uneven

- Affected area: `job_engine.py`, import review jobs, playlist download/sync jobs, AI batch import, acquisition, replacement, maintenance runner.
- Evidence: `JobStore` was in-memory (durable since v0.1.42). `PythonJob` supports structured state and cooperative cancellation. Some workflows add checkpoint files and uniqueness checks; others rely on route-local state or result inference.
- Current risk: Process restart, retry, or duplicate starts can repeat completed steps, lose progress, or leave stale active status unless each workflow implemented its own protections correctly.
- Desired state: Shared job requirements for operation identifiers, idempotency, resource locks, bounded retries, checkpoints, heartbeats, cancellation checks, and terminal-state recovery.
- Safe migration approach: Add job contract tests and a reusable idempotency/checkpoint helper. Migrate long-running workflows by risk, starting with import/replacement/playlist mutations.
- v0.1.42 progress (IMPLEMENTED, CI VERIFIED, LIVE VERIFIED for the scope below):
  - Live: an untracked-inventory job was SIGKILLed at a persisted checkpoint (processed 10,000 of 104,198). After the container was recreated it resolved to `failed` ("read-only job, safe to start again") with its checkpoint and last heartbeat kept; nothing re-ran it. Its orphaned lock was refused inside its 120 s TTL (the new container cannot see the old process), reclaimed after it, and released by the next run. The engine-backed transaction restart path (engine request recorded, finished from evidence, never replayed) is CI verified (A–G matrix) but was not crash-tested live.
  - `JobStore` is durable (`<data>/jobs/*.json`): throttled progress writes, checkpoints persisted immediately, heartbeats, terminal states `success`/`failed`/`cancelled`/`recovery_required`. A job interrupted by a restart is never re-run: read-only jobs become `failed`, anything else `recovery_required`.
  - Durable hierarchical resource locks (`backend/resource_locks.py`, `<data>/locks`): cross-process exclusive, order-enforced, heartbeat-kept, reclaimed only when the owner process is provably gone.
  - Engine-backed transactions (item replacement, reviewed duplicate cleanup, album-row merge, untracked attach/quarantine) record the engine request before calling it; `backend/transaction_recovery.py` finishes a transaction a restart left Running from engine evidence (manifest or operation registry) and never replays it.
- v0.1.46 live (LIVE VERIFIED for the lock and checkpoint, with no library mutation): a second process held `workflow:music-format-replace`; the real job started through the API waited with a persisted `waiting_for_lock` checkpoint and the contract metadata in its durable record, never ran beside the holder, was cancelled while waiting, and left no lock behind. No adopted workflow was run to completion or crashed live.
- v0.1.46 (IMPLEMENTED, CI VERIFIED): `backend/job_contract.py` is the shared job contract -- a durable `workflow:<name>` lock held for the job's lifetime (taken after the workflow's own in-process guard), heartbeat, a contract checkpoint in the durable job record, workflow progress republished into it, and cancellation while waiting. Adopted by the maintenance runner, playlist download and pipeline actions, AI batch import, Acquire Download All, album download+import, the music-format replacement retry and the import slot (folder import, disk re-import).
- Wave 5 (IMPLEMENTED): `JobStore.start_python` refuses a job whose explicit `metadata["dedupe_key"]` matches a running job (`DuplicateJobError`, HTTP 409 `job_already_running`). The guard is opt-in because labels and metadata often omit the input that makes two starts different; only Move All and MBSync All opt in. Move All fails at once with `not_supported` (nothing changes); MBSync All runs Beets' mbsync for as long as Beets takes, and the guard refuses a second start for that whole time. The transaction hook checks and starts under the store lock, so a refused start records no transaction. A job returning `{"ok": false}` ends `failed` (and its hook-created transaction `Failed`); a thread start failure no longer leaves a job running; "clear done" keeps `recovery_required` records.
- 0.4 jobs reliability (IMPLEMENTED, CI pending; not live verified):
  - Duplicate starts are guarded for **every** job-starting mutating request, not only opt-in ones. `routes_jobs.py` opens a start scope per POST/PUT/PATCH/DELETE; the first job that request starts carries the request fingerprint (method, path, query, body, upload bytes) and the optional `Idempotency-Key` header. An identical request while that job runs gets 409 `job_already_running` with its `job_id` (read-only jobs are exempt). The same `Idempotency-Key` again gets 409 `job_already_started` (`replay: true`, `job_id`, `status`) in any status, also after a restart while the record is retained, and never runs again; the same key with a different request gets 422 `idempotency_key_reused`. Explicit `dedupe_key`, the job contract, the import slot and transaction CAS guards still apply on top.
  - Job rows say `interrupted: true` (with `recovery`) when a restart cut them off.
  - Persisted `log_tail` and `result` are redacted centrally (`job_engine.redact_for_persistence`, #272).
  - `JobStore.every(name, interval, tick)` is the scheduler hook for recurring work (store-owned, stopped by `close()`, skips a tick while the job the previous tick returned still runs); `PythonJob.add_done_callback` replaces poll-until-finished watcher threads. The legacy auto-scan and the library-scan watchers use them. `tests/test_job_reliability.py` pins the remaining raw `threading.Thread` sites.
  - 0.7.0 (IMPLEMENTED, CI pending; not live verified): the batch import no longer starts a raw thread per folder for AI suggestions (F11). Its folders run one after another inside the batch's `job_engine` job, so `backend/ai_service.py` has no `threading.Thread` site left and was removed from that pin list. A restart requeues every unfinished folder (`requeued by recovery`), so recovery never trusts a cached per-folder decision.
  - #486 (IMPLEMENTED; CI pending): a multipart upload's idempotency key is scoped to its form fields and file metadata instead of method+URL, so a different upload to the same endpoint cannot reuse the key and get 422. A 5xx or lost response keeps the key for the retry; a definitive 2xx/4xx settles it. Concurrent artist-discography starts return the running job instead of 409, preserving the route's `job_id` contract.
- Remaining (why this is not Closed):
  - Resume is still each workflow's own: Clean All and playlist download resume from their own checkpoint files, AI batch from its state store. The contract makes the position visible in the job record; it does not resume a job. A restart still leaves the job `recovery_required` for the operator.
  - Not yet under the contract: the roughly 25 shorter mutating jobs (artwork, genre, folder and root repairs, Release-Group relinks, resolver jobs). They now have the request-level guard above. The ARCH-023 composite families (artwork set/remove/fetch, item metadata, album metadata, genre repair, album maintenance, confirmed import, artist-folder reconcile) now take consistent album/item locks across snapshot -> apply -> verify, but the other shorter jobs still have no per-resource lock: two *different* requests that touch the same album (e.g. rename, then fix-metadata) can still run together, serialized only by the webmanager plugin's global `mutation_lock`. Specific guards on top of the request guard: Move All / MBSync All `dedupe_key`; Clean All `dedupe_key: workflow:maintenance-runner` (atomic start, D1) plus the `workflow:` lock at run time; duplicate maintenance `dedupe_key: workflow:maintenance-duplicates`; `POST /api/import` `dedupe_key: import-source:<path>` (D2); Import Review revalidate-all (`import-review-revalidate`, also for more than 25 ids; it used to run up to 2000 items on the request thread) and the ready-items auto-import job (`import-review-auto-enqueue-ready`) each carry a `type` and a `dedupe_key` (0.7.0 F10); dedup scan/cleanup still only `_running_job_of_type` across types. Import `albums_imported` is the library album-count delta, so imports of *different* folders running together can still over-count each other (composite_workflows.reimport_source; needs a per-import count from the plugin). Across processes, all of them rely on `recovery_required`.
  - (RESOLVED, #486) The frontend sends `Idempotency-Key` and scopes a multipart key to the upload, keeping it after a 5xx and settling it after a definitive 2xx/4xx. A deliberate new action after a definitive answer still starts a new job, by design.
  - Jobs are durable only when `WEB_MANAGER_DATA_DIR` is set explicitly (every shipped Compose file does) or `BEETS_WEB_DURABLE_JOBS=1`.
  - A hook-created transaction whose job ends `recovery_required` is synced to `Failed`, not `Recovery Required` (`TransactionStore.update_from_job`). This is library-transaction work. (A Running transaction no recovery family covers is moved to Recovery Required by the startup sweep since 0.4.1, #266 item 3.)
  - Bounded retries are per workflow, not a contract feature.
- Priority: P1. Status: Open (narrowed).

## ARCH-005 Frontend Decision Logic Can Drift From Backend Authority

- Affected area: `frontend/src/features/importReview/ImportReviewPage.tsx`, other large feature panels, `frontend/src/api/types.ts`.
- Evidence: Large feature components render data, poll jobs, manage local workflow state, and calculate some block/eligibility display. Frontend panels sometimes adapt backend evidence shapes locally rather than displaying them as-is.
- Current risk: UI can enable, hide, or label actions differently than backend eligibility; explanations can diverge from backend safety decisions.
- Desired state: Backend returns authoritative evidence, conflicts, safety result, and action eligibility. Frontend displays those fields and only handles presentation state.
- Safe migration approach: Extend backend contracts first, then simplify frontend helpers as contract consumers. Add static and UI tests for visible evidence and disabled/destructive actions.
- v0.1.42: the new Untracked files panel follows the desired state (backend returns `action`, `action_eligibility`, `safety_result`, `conflicts`, `reason`, `requires_review`; the panel only displays/filters/confirms).
- v0.1.47 (IMPLEMENTED, CI VERIFIED, LIVE VERIFIED for the endpoint, read-only): the Import Review action decision is backend-owned. Live: `POST /api/import-review/decision` returned decisions for 200 real review-queue items; a crafted ready case and an unsafe-target-preview case returned the expected verdict, reason and next action; a malformed body got 400 and a call without a token 401; the deployed frontend bundle calls the endpoint. The apply path was not driven through the browser.
  - `backend/import_review_decision.py` decides bucket, blocked/ready, block reason, next action, action label and selected source files; `POST /api/import-review/decision` serves it; the apply path asks it for the verdict and fails closed.
  - The page's decision rules (`shouldShowBlockedBucket`, `applyBlockReason`, `targetPreviewBlockReason` and the rest) moved verbatim into `frontend/src/features/importReview/importReviewDecision.ts`; the page defines none of them.
  - One shared fixture (`frontend/tests/fixtures/import_review_decision_cases.json`, 45 cases) is checked by both `tests/test_import_review_decision.py` and `frontend/tests/importReviewDecision.test.tsx`.
- #228 (IMPLEMENTED): transaction rollback eligibility is computed by the backend (`rollback.allowed`, `allowed_code`, `allowed_reason` on the transaction list and detail). The Library Changes page still uses its own `canRollback` copy until the frontend switches to it. Composite families stored locally (import review cleanup, folder cleanup, library cleanup and the other `composite_workflows` families) report `allowed: false`: the generic route's composite dispatch only runs for a transaction missing from the store, which cannot happen in production because both read `BEETS_TRANSACTION_DIR`.
- Why this stays Open:
  - The page still evaluates the mirror locally for filters, counts and button state (contract-tested, but not served by the backend on every render).
  - The selected-match state those rules read (`confidence_level`, `auto_fix_eligible`, `is_importable`) is still assembled in the page from suggestion and preflight responses.
  - Other large panels (Clean, Acquire, Playlists) have not been audited the same way.
- Priority: P1. Status: Open (narrowed).

## ARCH-006 Provider Boundaries Are Inconsistent

- Affected area: MusicBrainz, AcoustID, OpenAI, Discogs, SLSKD, yt-dlp, Plex, Lidarr.
- Evidence: `helpers_mb.py` and `backend/slskd.py` are adapter boundaries; since ARCH-001 (v0.1.31) the provider logic lives in owned services (`musicbrainz_service`, `acoustid_service`, `plex_service`, `slskd_service`, `ytdlp_service`, `ai_service`), but those services still mix provider calls with workflow orchestration and do not share one retry/failure contract.
- Current risk: Retry, rate-limit, secret redaction, and failure representation differ by provider.
- Desired state: Each provider has a small adapter with typed inputs/outputs, explicit transient/permanent failure classification, bounded retries, and redaction.
- Safe migration approach: Extract adapters only when changing a workflow for a real bug. Preserve API responses and add contract tests.
- v0.1.42 progress (IMPLEMENTED, CI VERIFIED, LIVE VERIFIED for AcoustID: a live lookup returned `confirmed`; no key value in either container's logs or the data directory): `backend/provider_boundary.py` defines the typed outcomes (`confirmed`, `no_result`, `ambiguous`, `conflict`, `unavailable`, `rate_limited`, `authentication_error`, `transient_error`), bounded Retry-After-aware retries and redaction. Production callers: the AcoustID lookup (`helpers_mb.acoustid_lookup_outcome`) and its file cache, which now caches only real answers -- previously an outage, throttle or rejected key was cached permanently as "no match" -- and the MusicBrainz release tracklist fetch. The untracked recovery workflow consumes both outcomes and fails as "could not ask", never as "no match".
- v0.1.48 live (LIVE VERIFIED, read-only): `GET /api/providers/health` lists 12 providers with their policies and no URL or token; the app's own MusicBrainz call was recorded `confirmed`; a real refused request (HTTP 400) was classified `rejected` after one attempt with no retry; a real AcoustID lookup was `confirmed`. The check also exposed an interpretation bug of exactly the remaining kind: the AcoustID connectivity test reports a working setup as "Could not reach AcoustID" because AcoustID answers the probe's dummy fingerprint with HTTP 400 (code 3, after accepting the key).
- v0.1.48 (IMPLEMENTED, CI VERIFIED): every outbound provider call (48 sites: MusicBrainz, AcoustID, Discogs, Spotify, artwork, Plex, Lidarr, SLSKD, qBittorrent, yt-dlp PO provider, AI) opens its connection through `provider_boundary.opened(provider, request, ...)`.
  - Per-provider policy (`POLICIES`): bounded, Retry-After-aware retries for repeatable requests only; a POST is never repeated; a 4xx refusal is `rejected` and never retried.
  - The classified outcome of every call is recorded, redacted, and served by `GET /api/providers/health`.
  - `tests/test_provider_boundary_opened.py` fails the build if any application module calls `urlopen` directly, uses another HTTP client, or names a provider without a policy.
- Remaining (why this is not Closed):
  - Transport is uniform; interpretation is not. Call sites still parse responses and map failures to their own result shapes; only the AcoustID lookup and the MusicBrainz release fetch return a typed `ProviderResult` to their callers. A site that swallows an exception can still report an outage as an empty result.
  - yt-dlp and SLSKD downloads run as subprocesses or long-polls outside this boundary.
  - `routes_submissions._extract_ytdlp_info` runs yt-dlp metadata extraction on the request thread (wave 5 removed its helper thread) with no overall deadline; only yt-dlp's own socket timeouts bound it. Add a total deadline when yt-dlp extraction moves behind the provider boundary.
  - The AI provider is never retried (every call is a POST).
  - 0.6.0 (ACQ-3): the PO token provider URL (`YTDLP_PO_PROVIDER_URL`) is validated with its implicit configured-URL trust and probed through the boundary. A blocked or unreachable provider is dropped, and the result is cached for 300 s. The bgutil yt-dlp plugin then connects to it with its own HTTP stack, outside `secure_urlopen`. With a hostname value that leaves a DNS-rebinding or redirect window within the private floor; loopback and metadata are still refused at validation. Closing it needs address pinning in the plugin.
- Priority: P2. Status: Open (narrowed).

## ARCH-010 Composite Mutation Workflows Still Call The Retired `backend/beets_client.py`

- Affected area: `app.py`'s composite Plan/Apply/Rollback mutation workflows -- merge-album, merge-artist, Clean All, track replacement, folder/album cleanup, artist-folder reconcile, album maintenance/relocation/metadata-repair, album artwork fetch/embed, genre repair, mbsync-all, and move-all.
- Status: Closed / Completed (v0.1.25).
- Resolution: All composite workflows were migrated to `backend/composite_workflows.py` backed by `backend/beets_adapter.py` and `TransactionStore`. `backend/config_manager.py` was introduced for safe atomic configuration updates. `backend/beets_client.py` and all legacy control-agent references were completely eliminated (zero active references). Full 2,675 test suite and architecture invariants verified.

## ARCH-020 Duplicate Album Rows Need A Working Engine Merge

- Affected area: `composite_workflows.plan/apply_album_duplicate_merge`, the webmanager plugin's `modify` allowlist.
- Evidence: the existing album-merge Apply reassigns items through `/webmanager/modify` with `album_id`, which `ALLOWED_ITEM_FIELDS` deliberately excludes, so it cannot move items between album rows. The read-only `POST /api/library/album-duplicate-analysis` (v0.1.40) now produces a per-Release-Group merge proposal, but nothing can apply it yet.
- Desired state: an engine op that moves items into a retained album row by Release Group proof (keeping Release/Recording IDs, disc/track), with Plan → Approve → Apply → Verify → Rollback, used only for groups the analysis marks deterministic.
- v0.1.42 (IMPLEMENTED, CI VERIFIED, LIVE VERIFIED): live on Al Green – *He Is the Light* (RG 32922e5a): Preview → Approve → Apply (row 1428 retired, albums 413 → 412, second Apply refused) → Verify (identity, path, size, mtime, SHA-256 unchanged) → Rollback (row 1428 restored at its original id; albums, items and file hashes exactly equal to the baseline; second rollback idempotent) → Preview → Apply → Verify again. 311 – *Dammit!* was refused at Preview (`file_missing`: the source item's file is gone from disk). The three overlapping-slot groups stay review-only: Dennis Brown is audio-identical (same FLAC MD5), Sevyn Streeter has no FLAC MD5 to compare, and Al Campbell is two different masters (24-bit vs 16-bit). Engine op `/webmanager/album-row-merge` (+ rollback, + status) changes item ownership only (no tag write, no move), requires Release Group AND Release ID, full coverage, unchanged identity/content and no slot overlap, retires source rows after verified moves, and rolls back to the ORIGINAL album ids; `backend/album_row_merge.py` plans only deterministic groups.
- v0.1.43 live follow-up:
  - 311 – *Dammit!*: all 7 tracked rows pointed at missing files; their audio was found in the `{Album MbId}` folder by Recording ID. Each was recovered through track-for-replacement and item-file replacement (identity unchanged), then row 1388 was merged into 1793 (apply, exact rollback, re-apply).
  - Dennis Brown: the audio-identical copy was removed through reviewed cleanup, retiring duplicate row 2140 (plugin 1.3.1); its file is in the engine quarantine and rollback is available.
  - Still review-only: Sevyn Streeter (no FLAC MD5 to prove identity) and Al Campbell (24-bit vs 16-bit masters).
- v0.1.44 (IMPLEMENTED, CI VERIFIED, LIVE VERIFIED for the partial move): the legacy callers are migrated. Live, through the split-album route on 38 Spesh & Conway – *Speshal Machinery*: one item moved from row 2124 into row 2133 (row 2124 kept its other three items; identity and file bytes unchanged), then rollback restored the exact original state. The import-reconcile path was not exercised live.
  - `composite_workflows.plan/apply/rollback_album_duplicate_merge`, `merge_duplicate_albums`, `merge_split_album_items` and `*_existing_album_reconcile` are thin delegations to `backend/album_row_merge.py`; none reassigns `album_id` through `/webmanager/modify` or rewrites album-level fields on moved items.
  - Plugin 1.4.0: `album-row-merge` takes `"partial": true` to move some of a row's items; the row is retired only when emptied.
  - Import reconcile moves the imported items that fill free slots; imported copies of filled slots become a reviewed duplicate cleanup (audio proof, quarantine), applied only on a reviewer's decision and otherwise left in Preview.
  - The Release-Group plan refuses a retained row whose files are missing (`retained_row_file_missing`), the 311 case.
  - Fixed on the way: `merge_duplicate_albums` and `merge_split_album_items` were called with argument shapes that failed at run time.
- Why this stays Open: the duplicate resolver's "retag" action asked the merge to rewrite Recording ID and disc/track on the moved items; that payload is now refused (`identity_rewrite_not_supported`) and reported per source. It needs rebuilding on the recording-attach workflow (audio proof per item), then an ownership move.
- Priority: P2. Status: Open (narrowed to the resolver retag action).

## ARCH-021 Untracked Files Under The Music Root

- Affected area: `/music`.
- Evidence: roughly 103k audio files are not tracked by Beets. v0.1.40 adds a read-only inventory (`POST /api/library/untracked-inventory`) that classifies them and persists evidence; no cleanup or import exists for them yet.
- Desired state: category-scoped, reviewed recovery (import canonical-looking album files) and cleanup (exact duplicates of tracked files, import artifacts) through engine-owned quarantine, never by name alone.
- v0.1.42 (IMPLEMENTED, CI VERIFIED): incremental inventory (reuses persisted records; hashes only new/changed files); engine ops `/webmanager/untracked/attach` and `/webmanager/untracked/quarantine` (+ rollback, + status); `backend/untracked_recovery_service.py`:
  - Class A: quarantine only byte-identical copies of tracked files. A naming pattern alone is never eligible.
  - Class B: attach into a free album slot. Requires tags that name one album row, MusicBrainz confirming the recording at that position, and AcoustID confirming the audio.
  - Class C: tracked as a singleton, then a replacement is planned through `backend/item_replacement.py`.
  - Class D: no action.
- v0.1.42 live (LIVE VERIFIED for Class B; Class A has no live candidate):
  - Incremental inventory:
    - Pass 1 (library just changed): 104,198 untracked of 107,236, 33 s, 50 files hashed.
    - Pass 2: all 104,198 records reused, 0 rehashed, 0 AcoustID and MusicBrainz calls, 20 checkpoints, 6.9 s, peak 503 MB.
  - Class B attach: Alicia Keys – *HERE* – 12 into the free slot 1/12 of album 1823, with tags, MusicBrainz `confirmed` and AcoustID `confirmed`. Preview → Approve → Apply (items 3139 → 3140, file bytes unchanged, second Apply refused) → Rollback (verified, idempotent) → re-apply (verified). It stays applied.
  - Refused at Preview:
    - 10 candidates with no Recording ID tag;
    - one file AcoustID did not recognize (`no_result`, a real answer).
  - Of the 17,360 files classified as canonical album files, only 284 have tags naming exactly one album row with a free slot; 16,524 name a Release ID with no album row in Beets.
  - Class A: 0 byte-identical copies exist, so no quarantine was previewed.
  - Class C was not exercised live.
- v0.1.45 (IMPLEMENTED, CI VERIFIED, LIVE VERIFIED): new album rows for releases Beets does not have.
  - Live on Jimmy Cliff – *Struggling Man* (1973): 9 of the release's 10 tracks proven and tracked as new album row 2177 in place (items 3139 → 3148, albums 410 → 411, file bytes and mtimes unchanged, second Apply refused); rollback restored the exact prior state; re-apply verified. Track 01 was excluded (`slot_recording_mismatch`) and stays untracked.
  - The inventory lists 1,373 candidate folders (17,360 files).
  - One plan took 43–50 s for 10 files, even with AcoustID answered from cache; the per-file tracked-path check reloads the item list. That cost matters for a batch planner.
  - `action: "attach_album"` plans one folder as a new album row (per-file proof by tags, MusicBrainz tracklist and Release Group, and AcoustID); engine op `/webmanager/untracked/attach-album` (plugin 1.5.0) creates it in place with rollback.
  - One folder and one release per plan, operator-approved. There is no bulk planner for the roughly 1,400 folders yet; each plan costs one AcoustID lookup per file.
- Why this stays Open: the 69,828 import artifacts and 16,919 unknown files have no cleanup path, which is deliberate: a naming pattern is not proof. The album folders still need a batch planner (a durable, resumable job that plans folder after folder within the AcoustID rate limit) before the backlog can be worked through.
- Priority: P2. Status: Open (attach and new-album recovery implemented; batch planning and artifact cleanup remain).

## ARCH-022 S1 Mutation Containment Leftovers (PR #174)

- Affected area: `backend/composite_workflows.py`, `backend/import_service.py`, `backend/playlist_service.py`, `backend/transaction_engine.py`, `routes_maintenance.py`.
- Evidence: the Wave 0 S1 containment fixes closed the unsafe paths by refusing or narrowing them. These gaps remain:
  - The import template pre-rename and the import Step 0b orphan pre-cleanup log `not_supported`. They need an in-library rename and a rows-only cleanup through the engine.
  - Folder references for `safe_rename_library_folder` and `plan_folder_cleanup`/`apply_folder_cleanup` now come from the adapter (`_library_refs_under`), item paths only; an album `artpath` under the folder is not checked. The re-check at apply runs before the engine's apply lock, so a narrow window remains. `remove_empty` skips the scan because the engine requires the folder to be empty.
  - Playlist media cleanup is rows-only by design: files stay, and rollback is `not_supported` (the files can be re-imported). An engine-owned quarantine would make it restorable.
  - `playlist_service._playlist_normalize_staged_file` moves staged downloads with `shutil.move` outside the staging helpers (staging-only, never `MUSIC_ROOT`).
  - Approving an `album_cleanup_v1` plan with `delete_files` works from the UI since #174: Library Changes opens a dialog that stays disabled until `DELETE ALBUM FILES` is typed and sends `confirm_delete_files`. Applying it through `POST /api/transactions/<id>/apply` still returns 409, because `album_cleanup_v1` is not in `routes_maintenance._ENGINE_FAMILIES` (#187 F-4).
- Desired state: each of these runs through the canonical preview/approve/apply/audit workflow.
- Priority: P2. Status: Open.

## ARCH-023 Composite Mutation Status And Rollback Gaps (MI-1/MI-2 wave)

- Affected area: `backend/composite_workflows.py`, `backend/transaction_service.py`, `routes_maintenance.py`, `frontend/`.
- Evidence:
  - (RESOLVED, #486) The artwork, artwork-fetch, item-metadata, album-maintenance, genre-repair and import-folder rollbacks used to record nothing to restore: `_rollback_noop` refused unapplied transactions but marked an applied one Rolled Back without restoring anything. They now restore rows, files, tags and embedded art (or refuse with `rollback_unavailable` when the record has no before-state).
  - (RESOLVED, plugin 1.14.0) Album rename and move-to-library (`album_move_v1`) record every track path and the cover path at plan time, take the operator's click as the approval, apply through Beets' `Album.move()` with a stale-plan check, record one transaction per operation, resolve a restart from the recorded paths and the engine's durable record, and roll back through Beets (`POST /webmanager/album-relocation/rollback`), refusing without changes when the album changed since or an old location is taken. Older `album_relocation_v1` records stay non-rollbackable.
  - MBSync All (`mbsync_library_v1`, plugin 1.13.0) runs Beets' own mbsync over the whole library with no preview and no rollback: Beets keeps no undo, and the transaction records `rollback_available: false`. Its change log (the first 200 changed albums or singles, values cut to 120 characters) is for manual repair only. It never moves files (`move=False` regardless of `import.move`), so no old paths need recording; a library-wide move stays with the relocation work. A preview would need Beets' `pretend` output, which mbsync only prints, and would double the MusicBrainz lookups. If Web Manager restarts mid-run, Beets keeps syncing; the job is resolved by the restart sweep and the plugin operation's result is not read back into the transaction.
  - (RESOLVED, #218) The artist-folder reconcile, artwork, artwork fetch, item metadata, album maintenance, album relocation and genre repair applies now claim Preview/Approved -> Running with a compare-and-set before calling Beets, and finish with a Running -> Completed/Failed compare-and-set. A cancel that lands first wins and nothing is written. An adapter result of `ok: false` in these families is still read as success by the adapter wrappers that do not return one (only the nested artwork fetch reports it).
  - (RESOLVED, #224) Every engine-recorded rollback (track replacement, track quarantine, duplicate cleanup, album-row merge, untracked recovery, import-review cleanup) refuses a transaction that never applied (Pending, Preview, Approved, Cancelled or Running) with `rollback_not_eligible`, and writes its final status with a compare-and-set (`conflict` on a race). The import-review cleanup rollback claims Running first and marks Recovery Required if a restore step raises.
  - Folder cleanup steps Beets did not confirm in time (`FOLDER_OP_UNCONFIRMED`, typically because a Beets import holds `mutation_lock`) are recorded with their idempotency key in `unconfirmed_steps`. Rollback replays each key first and defers (`rollback_deferred`, nothing changed) while Beets still reports the step running (#300 R1). Remaining (#300 R2/R3, plugin side, owner beets-integration-engineer):
    - (RESOLVED, plugin 1.9.0, #307) R2: `folder-op` waits at most `LOCK_WAIT_SECONDS` (2 s) for `mutation_lock`, then refuses with 503 `BUSY` before registering the idempotency key (a known key still replays), so a step never queues behind an import. Web Manager reports `BUSY` on apply as a failed step with nothing recorded as unconfirmed, and defers a folder rollback on `BUSY`.
    - (RESOLVED, plugin 1.9.0, #307) R3: final folder-op outcomes are saved atomically in `webmanager_operations.json` next to the library DB (7-day TTL, shared 1000-entry cap), so a replay after a Beets restart reads the recorded outcome. Chosen over an `unknown` reply because a rollback replay of a key Beets can never vouch for would defer forever. A step Beets stopped in the middle of has no saved outcome; its replay runs against the current files, which is sound because nothing can still be running it.
  - (RESOLVED, #486) The track replacement, track quarantine and duplicate cleanup rollbacks neither claim Running nor hold resource locks before calling Beets (album-row merge and untracked recovery hold resource locks); only their final status write is a compare-and-set, so two concurrent rollbacks could both reach Beets; the one that writes status second reports `conflict` (HTTP 409). They now claim Running through `rollback_guard`, hold the family locks and rely on the restart sweep to resolve an interrupted rollback as Recovery Required.
  - (RESOLVED, #486) `rollback_folder_cleanup` now follows the guard contract: family check, a CAS claim with an atomic `rollback_request`, and the engine's resource locks while it replays unconfirmed steps; the engine's durable status move is recorded through `record_outcome`. An unfinished rollback from an earlier process remains Running (`rollback_resumable`), and route eligibility permits an explicit retry from its durable steps. A process-local active marker refuses concurrent retries. Before plugin 1.9.0 a rollback during a Beets import could block for several timeouts before it deferred; plugin 1.9.0 answers `BUSY` within about 2 s, and rollback defers at once.
  - (RESOLVED, QA #306 N1) A claimed composite apply interrupted by a Web Manager restart was left Running. The startup recovery sweep now moves a Running transaction that `_claim_apply` claimed before the process started to Recovery Required. Recovery Required is terminal for these families: `_claim_rollback` refuses it and a re-apply is refused, so it needs manual review (check the library, then re-plan). Since 0.4.0 the sweep also resolves a folder cleanup (`folder_cleanup_v1`) or import-review cleanup cut off by a restart (top-level `mutation_started`, `filesystem_mutated`, or the folder family): before, it stayed Running for good. Evidence-less job wrappers still follow their job, and a Running wrapper with no job evidence is left Running (owner backend-architecture-engineer: decide whether to resolve them too). Since 0.4.1 a Running record of a mutation family the sweep does not know (a removed family, or one cut off before it recorded evidence) also goes to Recovery Required with a log line naming the family (#266 item 3).
  - A rollback replay of an unconfirmed apply step runs the step if Beets never received it; it does so without Web Manager's pre-step TOCTOU re-checks, relying on the plugin's own refusals (TARGET_EXISTS, NOT_EMPTY, SOURCE_MISSING).
  - (RESOLVED, plugin 1.16.0) `delete_album_art` is an `album_art_replace_v1` transaction through `POST /webmanager/album-art/remove`, which quarantines the cover and rolls back.
  - The artist-folder reconcile fallback in `import_reconciliation_service` calls a composite helper with an unbound signature (listed in `KNOWN_UNBOUND_CALLS` in `tests/test_wave0_composite_call_signatures.py`).
  - The job wrapper in `transaction_service.start_python_with_transaction` sets its audit transaction Running unconditionally.
  - (RESOLVED, 0.4.1, #317) `_wait_for_child_job` no longer abandons a mutating child on cancel or timeout: it asks the child to stop and waits up to `CHILD_SETTLE_SECONDS` (300 s) for its real outcome, and reports it to the parent's job wrapper through `job_service.note_job_progress` (Failed partially applied, or Recovery Required while the child still runs). Remaining: a parent that is not transaction-wrapped records nothing at its own level (the child's transaction is still truthful), and a generic child that changed the library without publishing `mutation_started` and then failed is not reported as a change.
  - (RESOLVED, #486) `routes_maintenance`'s generic rollback cannot reach the composite metadata and MusicBrainz rollbacks: they record no `rollback.operations`. `item_metadata_v1`, `genre_repair_v1`, `album_metadata_v1`, `album_maintenance_v1`, `artist_folder_reconcile_v1`, the artwork families, `album_mb_track_repair_v1`, `folder_cleanup_v1` and `confirmed_import_v1` now dispatch through the generic route, and the route's eligibility table is generated from the same executor rules (C1/C2).
  - `update_album_metadata(aid, {}, force_write_tags=True)` plans nothing, so it writes no tags.
  - Match album (`routes_library._match_album_run`, shared by `POST /api/albums/<aid>/match` and the Missing IDs batch): when a stage after step 3 (track sync, tag write, relocation) fails, only the step-2 metadata operation is rolled back. The `album_mb_track_repair_v1` operation from step 3 stays applied (whatever `mbsync` changed on the tracks) and needs a manual check (QA #399 F3). Fix: record the repair operation id and roll it back before the metadata operation.
  - The frontend relink caller must send `mbAlbumId` or `mbReleaseGroupId`. Without one, the endpoint now refuses with `relink_identity_required`.
  - (RESOLVED, 0.4.0) Every engine-family step passed `logs=[...]` to `TransactionStore.update`/`transition`, which replaces the whole log, so a later step (a refusal, the restart sweep, the finish) erased the apply history. Steps now pass `append_logs=[...]`; `logs=` keeps replace semantics for job wrappers that pass the job's full log. Playlist media cleanup still uses `logs=` (playlist code was out of scope).
  - (RESOLVED, #486) `_restore_rows` restored with `move=True` even when the apply did not move files (QA #243 F-5). It now uses the move/write decision recorded by the apply, verifies the DB values, file tags and paths independently, and refuses to report `Rolled Back` when an empty DB diff hides a tag difference (H11).
  - (RESOLVED) A folder cleanup that failed part-way recorded no `engine_result`. Each step is now recorded as it completes, and the generic route rolls back a Completed or Failed `folder_cleanup_v1`. A Partially Rolled Back folder cleanup can be retried from the remaining durable steps.
  - Folder cleanup steps (`POST /webmanager/folder-op`) check containment and symlinks by path, then act by path, so a folder swapped for a symlink between the check and the `os.rename`/`os.rmdir`/`util.move` could redirect the step (low: needs write access inside the library during the step). The local code it replaced had the same window. Fix: resolve the parent with `os.open(..., O_DIRECTORY | O_NOFOLLOW)` per component and operate relative to it (`dir_fd=` for `rename`/`rmdir`/`mkdir`). In the same window, `rename_dir` on POSIX would silently replace an empty directory created at the target after the existence check (no data loss; `mutation_lock` excludes racers inside Beets); a no-replace rename (`renameat2(RENAME_NOREPLACE)`) is not in the Python standard library.
  - Folder cleanup paths are sent to Beets unchanged, so Web Manager and Beets must mount the library at the same path (the shipped Compose files use `/music` in both).
  - Folder reference checks compare Beets-reported item paths only; an album `artpath` under the folder is not checked.
  - A one-shot plan -> approve -> apply (album move, artwork upload/remove, folder safe rename, manual and unattended duplicate cleanup) that raised before the claim (busy or stale lock) was left Approved with nobody to apply it (#328 N1). It is now Cancelled with a "nothing was changed" log (`resource_locks.cancel_unclaimed_on_error`). Callers that apply an existing, possibly operator-approved plan (`album_row_merge.approve_and_apply`, `item_replacement`, reviewed cleanup from a transaction) keep it Approved so the operator can retry.
  - An album move interrupted before Beets reported its `after` paths cannot be rolled back; the recovery log and the rollback refusal now say to move the album again (#328 F2). A rollback-from-`before` would need the live paths proven against the plan.
  - A lock left by a crashed Web Manager blocks a retry until its heartbeat TTL (about 2 minutes) runs out (#328 F7/N3); the busy message says so. Reclaiming a same-host lock whose process is gone at once is safe only when the host name is unique per container; left as is.
  - Album move rollback adopts a file at its old path on size + mtime (±1 s) (#328 N2, accepted ceiling, named in `relocation_ops._adoptable`); a content hash recorded at apply would close it.
  - Staging moves (#230): a file moves by `link` + `unlink`, a directory by `rename` over an exclusively created empty placeholder, so a target planted after the check is never replaced; across filesystems a file is copied, fsynced, the target directory fsynced, and the source unlinked only if its identity, size and mtime are unchanged; a delete refuses a folder that holds another mount. Directory moves across filesystems are still refused (N5): a future caller that moves a staged folder between the default Compose volumes needs a per-file plan. A staging root configured through a symlink (`DOWNLOAD_PATH`, `BEETS_IMPORT_ROOTS`) is refused in its symlink spelling (S-4); configure the resolved path. The source's identity is re-checked right before the directory rename and before the file's old name is unlinked (#351 N4); POSIX has no rename/unlink by inode, so a swap within that last instant remains possible (needs write access inside staging, and stays inside staging).
  - Clean All's release-group merge tries each issue once per run (#298 N6). The planner still forces an empty folder with no Beets items to Safe only when the apply's identity gate (a valid Release Group) passes; an empty folder with no Release Group stays in review and is not removed by Clean All.
- #486 rollback completion (IMPLEMENTED; independent QA/security review pending; not live verified):
  - C1/C2: rollback eligibility now matches the executor for the covered families, and `item_metadata_repair_v1` / `genre_repair_v1` are registered with the generic route and dispatch to their real executors.
  - C3: artist-folder rollback restores the exact original paths through `POST /webmanager/item-path/restore` and verifies the albumartist DB value and tags; Beets' path-format move alone is not accepted as restoration. The plugin operation is hash-resumable, never overwrites an existing file, and is confined to the allowed roots.
  - C4: the claim and `rollback_request` are one atomic write. An interrupted rollback becomes `Recovery Required` before the restart sweep can re-finish the earlier apply as `Completed`; a rollback started in this process is left alone. Folder cleanup is the resumable exception described above. The redundant synchronous startup sweep was removed.
  - C5: `tests/test_arch004_023_real_restore.py` proves the real restoration against disposable Beets libraries and files, including crash/restart, concurrency, idempotent replay and exact paths; the frontend contract tests cover H9.
  - Artwork manifests record the cover and embedded-image hashes after the apply, so a same-named replacement is refused; fetch requires `embedart` up front and compensates a partial fetch/embed. Album-row/import manifests are written before the first move, membership and quarantine sources are preflighted under `mutation_lock`, and old records without before-state report `rollback_unavailable`. An in-place import whose tags were written reports `Partially Rolled Back` with `file_tags_restored: false`.
  - `undo_import_rollback` remains an intentional operator/recovery primitive for reversing a completed import rollback; it is not a UI transaction-state transition.
  - Verification at branch commit `d1f075b` (synced with `main` v0.13.4; only docs notes follow): full Linux runner 5,610 tests and sequential suite 5,610 tests, no failures/errors; five explicit sandbox skips (missing PowerShell, root-only ownership — run separately and passed as root — and three private-LAN tests with no network). GitHub unit and sequential security gates each passed 5,611 tests with 13 environment skips (the same skip count as clean v0.13.2), and the separate root/PowerShell gates passed. The real-library restore module passes 41 tests; frontend typecheck/lint/322 tests/build pass, as do syntax compilation of 105 Python files and architecture/security inventories. CodeQL findings 1449-1456 cleared after the canonical-destination fix, with no dismissals/suppressions and no open alerts on the PR ref. PR #496 carries the evidence and independent-review handoff. Priority Two (#487) remains blocked.
- Desired state: every composite family captures before-state, claims with a CAS and is reachable from the generic rollback route.
- Priority: P2. Status: Open (narrowed). Album rename and move-to-library (`album_move_v1`, plugin 1.14.0) ship in v0.2.0: IMPLEMENTED and CI VERIFIED, not yet LIVE VERIFIED. The unmarked items above remain open; #486 closes the rollback/restore subset and leaves the MBSync, match-album multi-stage, album-move recovery and staging TOCTOU items named above.

## ARCH-024 Imports Need Beets' Autotagger In The webmanager Plugin

- Affected area: `beetsplug/webmanager/operations.py` (`run_import`), `backend/beets_plugins.py` (Beets plugin setup), `backend/import_service.py`.
- Evidence:
  - Before 1.8.0, the webmanager plugin rejected `autotag: true` (`AUTOTAG_NOT_ALLOWED`), always ran the importer with `autotag: no`, ignored `search_ids` and `quiet_fallback`, and used the bare `ImportSession`, whose `choose_match` raises `NotImplementedError` once autotag is on. Web Manager sends the Beets-native request (see "Import contract" in `ARCHITECTURE.md`), so with an older plugin every import route fails cleanly.
  - Verified against `lscr.io/linuxserver/beets:latest` (Beets 2.14.1): Beets loads MusicBrainz as a plugin, so `musicbrainz` must be in `plugins:` when a `plugins:` list exists. Without it Beets finds no candidates and every confirmed import is skipped (`not_imported`). Provisioning added it in the two-container era; with embedded Beets nothing adds it (the Setup opt-in edit can), and `docs/CONFIGURATION.md` documents it.
  - Resolved: import-with-id and reimport-disk no longer retag after the import (`_match_tracks_from_mb`, `plan_album_mb_track_repair`, `relocate_album`, and the post-import discovery, re-validation and row-removal code are gone). They verify the album Beets produced and keep its rows when verification fails.
  - A reimport-disk source inside the library needs the plugin to accept an in-place import (no copy, no move) from the library directory; plugin 1.8.0 accepts it.
- Desired state: the plugin runs Beets' quiet import with autotag, `search_ids` and `quiet_fallback`, and returns the paths of the tasks Beets skipped as `skipped_paths`, so `/api/import` can name every folder left in place; `musicbrainz` is listed in `plugins:`; Web Manager only verifies.
- Priority: P2 (imports work; one repair path remains). Status: Partially resolved.
  - Done: the Web Manager side (the post-import retag is removed, #295) and the plugin side (`autotag`, `search_ids`, `quiet_fallback`, `skipped_paths`, in-place import, `musicbrainz` provisioning), which shipped in plugin 1.8.0 (#299) and was hardened in 1.8.1.
  - Still open: reimport-disk's existing-album repair (`_repair_existing_album_in_place`, no import) calls `_match_tracks_from_mb(zero_unmatched=True)`, which `plan_album_mb_track_repair` refuses since MI-1, so that repair path matches nothing.

## ARCH-025 Auto-Import Failure Overwrites The Review Item's Status

- Affected area: `backend/import_service.py`, `start_folder_import_with_id` → `_do_locked`. Also involved: `backend/import_reconciliation_service.py`.
- Evidence:
  - On the auto-import path (`auto_import_idempotency_key` set), a job can fail after it has already queued its folder for review. That happens through `_review_and_raise` (a refused Release Group, an unknown Release Group, `not_imported`, #311), or through the `kept_album_ids` verification-failure path.
  - In that case, `_do_locked`'s `except` handler then calls `_mark_pending_review_status(..., "auto_enqueue_failed")`.
  - `auto_enqueue_failed` is one of the reconciler's `resume_statuses`. A review item that is waiting for a human decision can therefore be shown as an enqueue failure and become eligible for an automatic resume.
- This was already true before #311: the `kept_album_ids` path behaves the same way.
- Desired state: when the job has already queued a review item, the handler keeps that item's review status (for example, it carries a "review queued" flag on the exception) and does not mark it `auto_enqueue_failed`.
- Priority: P3. Status: Open.

## ARCH-026 Identity Evidence Follow-ups (#244, #261, #264, #303)

- Affected area: `backend/composite_workflows.py` (owner library-transaction-engineer), `backend/acoustid_service.py`, `backend/import_service.py`, `backend/library_service.py`.
- Open:
  - #264: the Release Group candidate loop is capped but not cancellable; the resolver receives no `cancel_event`.
  - Without `ACOUSTID_API_KEY`, an imported missing-track fill cannot be merged and stays a second album row in the same Release Group until Review resolves it.
- Accepted (by design, conservative):
  - #244 F3: an embedded Recording ID contradicted by the fingerprint is a conflict and goes to review.
  - #244 F4: one AcoustID result linked to several recordings does not confirm any of them.
  - #244 F5: the AcoustID file cache key is versioned (`v3|`) but its payload is not; the only consumer is display, so a format change needs a key bump.
  - #261 N-6: an operator Release that the resolver swaps into another Release Group is refused at the stamp step (fail-safe).
  - #303: with no fingerprint evidence, `identity_proof` is INSUFFICIENT and the candidate goes to review; the reason now says how to resolve it.
- Priority: P3. Status: Open.

## ARCH-027 Transaction Safety Follow-ups (0.4.0)

- Affected area: `backend/library_service.py`, `backend/beets_adapter.py`, `backend/transaction_engine.py`.
- Evidence:
  - App-managed download folders are recognised by name (` - yt|ytdlp|soundcloud|spotiflac missing <13-digit ms>` under `DOWNLOADS_ALLOWED_ROOTS`, #325). A folder that a user names that way is treated as app-managed. A marker file written at creation would be proof (owner integrations-automation-engineer).
  - Stock Beets' web `QueryConverter` splits path queries on `/` and maps `\` to the path separator, so `BeetsAdapter.find_item_by_path` sends `/` as `\` (#345). A path that itself contains a backslash cannot be queried exactly; since 0.4.1 `find_item_by_path` fails closed for such a path (it is treated as not tracked unless `web.include_paths` lets it compare the exact path, #351 N6). `find_item_by_path` also re-checks the exact path because Beets' `path:` query matches directory prefixes.
  - `TransactionSettings.automatic_approval_threshold` is stored and validated but nothing reads it.
  - Folder-cleanup apply/rollback has an engine-internal crash window (QA C1/C2, deferred from `fix/folder-placeholder-apply-r3`): `transaction_engine.execute_folder_cleanup_apply` can move files on disk and then crash before the Completed status and its `moved_records`/rollback record are durably written, and `_claim_rollback` claims a row to `Running` before the engine rollback runs — a crash in that window can leave a row mid-rollback. The folder-placeholder *orchestration* already compensates (write-ahead markers + store recovery in `_find_applied_folder_op`, `became_tracked` refund, descendant-aware hold), but the engine primitives themselves are not yet crash-atomic. Make the apply record its rollback breadcrumb before the physical move (or write it atomically with the status transition), and make `_claim_rollback` recover a row left in `Running`.
- Priority: P3. Status: Open.

## ARCH-028 Find Music Snapshot Freshness (0.6.0 T2)

- Affected area: `backend/acquisition_service.py` (Find Music list snapshot, `fetch_lidarr_wanted`).
- Evidence: the list is now a snapshot built by the `acquisition-queue-build` job and rebuilt only by age (6 h) or Refresh (F7/F8). Lidarr's wanted list is read page by page; a page short of `totalRecords`, or fewer distinct albums in all than the last page's `totalRecords`, fails the build (QA #379 N3).
- Narrowed in 0.7.0:
  - A Find Music download (row or Download All) whose import put the album into the library (`album_ids` from the reimport job; not Pending Review, an in-place repair or an already-complete album) now drops its row from the snapshot at once (`acquisition_queue_mark_done`, matched by key, the row's MBID or local album id). A build already running when the download finished drops it too. The first `POST /api/acquisition/queue/refresh` with `after_download: true` within 120 s of such a patch reuses the patched snapshot (`reused: true`), so the UI's refresh after each download no longer walks every Lidarr page (F2). A Refresh without that flag (a user's click) always rebuilds.
  - The paging gap (QA #385 N2) is closed for the common case: a pass during which `totalRecords` moved is read once more, and only a second moving pass falls back to the count check.
- Current risk:
  - Downloads and imports started outside Find Music (Discography, Wanted, Import page) patch the snapshot only when their payload names a listed row's MBID or album id; Lidarr's own imports are not patched. Those rows stay listed until the next build (6 h at most, or Refresh).
  - A download that imports only part of an album also drops the row; the next build lists it again.
  - An import that repaired an existing album in place or found it already complete leaves its row listed until the next build.
  - If Lidarr removes one album and adds another between two page reads, `totalRecords` does not move and a row can still be missed until the next build.
- Desired state: Lidarr `history/since` to drop rows Lidarr imported itself, if still needed.
- Priority: P3. Status: Open (narrowed).

## ARCH-030 Missing-Track Recovery Loses Track→Album Identity

- Affected area: the download→verify→import-into-existing-album→Plex pipeline across `backend/acquisition_service.py`, `backend/import_service.py`, `backend/pending_review_store.py`, `backend/matching_service.py`, `backend/ai_service.py`.
- Evidence (Phase-1, proven in code): a single downloaded missing track (`<album> - <source> missing <ts>` under `/downloads`) was treated as a brand-new full-album import and failed full-tracklist preflight. The track→album identity (existing album, canonical `mb_releasegroupid`, expected recording/track#, destination) lived only in the live download job's closure/metadata; at the Review hand-off it was reduced to the `allow_existing` boolean + a free-text suggestion, and every consumer re-entering from the folder on disk re-derived identity from the folder name.
- Desired state: one durable, resumable recovery record binds the staged download to the album it recovers a track for; preflight treats a single-track recovery as a first-class mode; the verified track is inserted/merged into the existing album through the controlled transaction workflow; identity is never derived from a folder name; no verification threshold is lowered.
- Phase 2 (IMPLEMENTED — identity SPINE + published contract): `backend/recovery_carrier.py` is the durable, schema-versioned, CAS SQLite carrier (see ARCHITECTURE.md → State Ownership). It is created at download, bound to the staged folder, found by `load_for_folder`/`load_for_job`, and threaded into the Review hand-off as structured identity (canonical Release Group, supporting Release stored separately). Tests: `tests/test_recovery_carrier.py`.
- Remaining (specialist depth, tracked here): preflight single-track-recovery mode (music-identity), verified insertion/merge via `transaction_engine` reusing the durable rollback (library-transaction), AI-batch/pending-review consumption of the carrier (music-identity), provider-specific carrier population + auto-import use (integrations), and the Review UI for a recovery vs a new-album import (frontend). All plug into the `# HOOK(<owner>)` markers in the spine.
- Priority: P1. Status: Open (Phase-2 spine landed; specialist phases pending).

## ARCH-031 Cold `/api/library` Build Cost

- Affected area: `backend/library_service.py` `_build_library_payload` (cold `GET /api/library`, the 2-minute cache warmer, and the Find Music build's library read), `backend/beets_adapter.py` `get_items`/`get_albums`.
- Evidence: the live instance took about 98 s for a cold `GET /api/library` on 0.5.0 and 0.6.0. In code, the builder did one `stat` per Beets item (`Path.exists`), one per walked entry (`is_dir`/`is_file` after `iterdir`), and up to six cover-art `exists` probes per album folder, and it matched missing-file buckets by scanning every bucket for every album folder and every artist (quadratic).
- Measured on a local synthetic fixture (`python:3.13-slim` container, fixture in the container's own /tmp, Beets faked, so the Beets HTTP fetch is not included):

  | Fixture | Before | After | `stat` calls before / after |
  |---|---|---|---|
  | 1,000 artists, 5,000 albums, 60,000 items, 0 missing | 5.8 s | 3.6 s | 161,001 / 1 |
  | 1,000 artists, 5,000 albums, 64,000 items, 4,000 missing albums | 10.0 s | 4.4 s | 165,001 / 4,001 |
  | 2,000 artists, 10,000 albums, 128,000 items, 8,000 missing albums | 27.3 s | 9.0 s | 330,001 / 8,001 |

  The payload is byte-identical before and after on a fixture with cover art, disc sub-folders, loose files, not-imported extras and missing tracks in present folders.
- Fixed in 0.7.0: each folder is listed once with `os.scandir` and that listing answers both the item existence check and the walk (only items not found in their folder's listing are `stat`-ed). Missing buckets are indexed by artist.
- Current risk:
  - On a cold page cache each `stat` reaches the disk, so the remaining cost on a NAS is about one directory read per folder plus one `stat` per missing item. It was not measured on the live instance.
  - (Narrowed, plugin 1.18.0) The builder's two Beets reads now use `GET /webmanager/fields/<item|album>`, which returns only the dozen fields the builder reads. Stock `GET /item/` serialised every field, looked up each item's album for its album-level fields, and stat-ed every file for `size`. Measured on a synthetic library in the stock `lscr.io/linuxserver/beets` image (Beets 2.14.1; 120 artists, 360 albums, 3,960 items with lyrics and a flexible attribute; library on a Linux Docker volume; first request after a container restart, then warm):

    | Read | Stock cold / warm | Fields cold / warm | Payload stock / fields |
    |---|---|---|---|
    | items | 6.7 s / 5.9 s | 1.1 s / 1.1 s | 10.4 MB / 1.18 MB |
    | albums | 0.20 s / 0.22 s | 0.05 s / 0.05 s | 354 KB / 123 KB |
    | both, through `BeetsAdapter` (warm Beets) | 7.7 s | 1.05 s | |

    With the library on a Windows bind mount (slow file I/O, closer to a cold NAS) stock items took 30-33 s and fields items 1.6-1.9 s. What is left (about 0.85 s per 4,000 items) is Beets building its `Item` models in `lib.items()`. Going below that would need SQL against the library, which this project does not do. Other bulk readers (`get_artist_counts`, `list_item_paths`, Clean All indexes, dedup scans) still use the stock full reads; switch them when they show up as slow.
  - (RESOLVED, Unreleased) The disk-walk build no longer runs on the page-load request thread. `get_library_payload(block=False)` (the `GET /api/library` path) serves the cached disk-reconciled payload when warm, otherwise `_build_library_payload_fast()` — an engine-only tree built with no disk access — and schedules `_build_library_payload` as a deduped background `job_engine` job (`_schedule_library_reconcile`, dedupe_key `library-reconcile`) that warms the cache. This is independent of the opt-in `BEETS_ENABLE_LEGACY_LOCAL_SCAN` warmer, so a deployment with that scan disabled (e.g. a cold NFS library where the full walk was measured at ~120 s) no longer pays the walk synchronously on any page load. The response carries `reconciled`/`reconciling` flags. In-process callers that need the full disk-reconciled tree synchronously (the Find Music / acquisition-queue build job, already off the request path) pass `block=True` and keep the synchronous build.
- Desired state (DONE): stale-while-revalidate as for the Find Music list (serve the last payload, rebuild in a `job_engine` job). Done in plugin 1.18.0 (Beets reads that fetch only the fields the builder uses) and, for the request-thread build, in Unreleased (above).
- Remaining: the background `_build_library_payload` walk itself still costs one directory read per folder plus one `stat` per missing item over NFS (now paid off the request path); other bulk readers (`get_artist_counts`, `list_item_paths`, Clean All indexes, dedup scans) still use the stock full reads.
- Priority: P3. Status: Open (narrowed; request-path blocking resolved).

## ARCH-032 Release Group Fill Rollback Blanks Embedded Tags (0.7.0, QA #401 N-d)

- Affected area: `backend/missing_mb_batch.py` (Release Group fill), `backend/composite_workflows.py` `rollback_album_metadata`.
- Evidence: the Missing IDs batch fills `mb_releasegroupid` on an album that has a Release ID but no Release Group through a Metadata Update with tag writes. Rolling that update back restores the album's captured value (empty) and rewrites tags, so an item whose file tag already carried the same Release Group loses it.
- Current risk: low. The fill is refused when any item carries a different Release Group, so only an identical embedded value can be blanked, and only by an explicit rollback.
- Desired state: rollback restores each item's own captured value instead of the album's.
- Priority: P3. Status: Open.

## ARCH-033 Review Queue Follow-ups (0.7.1, QA #410 N1-N3)

- Affected area: `backend/pending_review_store.py` (queue lock and prune), `backend/ai_service.py` (batch import review enqueue).
- N3: a list-only Review Queue load prunes stale rows while it holds the global queue lock, and the prune makes Beets HTTP calls. A slow Beets therefore serializes every queue reader and writer. Fix: run the checks outside the lock, then re-read and apply the result under the lock, or move pruning to a job.
- N1: a partial-import batch folder whose review enqueue fails is marked `import_failed`, and a retry re-runs its Stage 1 import.
- N2: the final-pass `_queue` review drops the original evidence (`conflict`, `skipped_paths`, `kept_album_ids`, `deterministic_decision`).
- Current risk: low. No queue item is lost; the cost is latency (N3), a repeated import attempt (N1) and less evidence on the review row (N2).
- Priority: P3. Status: Open.

## ARCH-034 AI Suggest In The Review Queue

- Affected area: `backend/import_service.py` (`start_folder_import_with_id`), `backend/ai_service.py` (Find Match, AI match history), `backend/import_review_service.py` (auto-enqueue-ready, revalidation), `frontend/src/features/importReview/ImportReviewPage.tsx`.
- (RESOLVED) `POST /api/folders/import-with-id` kept the client's whole `ai_suggestion`. A refused import queued its `preflight`, `track_mapping`, `mb_valid`, `review_evidence` and `folder_signature` as if the server had computed them, bypassing the F16 rule that `POST /api/ai-pending-review` already enforced, and a value that was not an object failed the job with a `TypeError`. Both routes now share `serializers._client_review_suggestion` and `_client_review_origin`: a proposed identity plus bounded, display-only origin labels whose folder is always the validated one.
- (RESOLVED) AI match history recorded the submitted suggestion after every Review import, including imports of a different release the operator chose and manual imports with no suggestion. It now records only when the suggestion's Release Group (or, without one, its Release) is what Beets imported, stores the server-verified IDs, and writes atomically.
- (RESOLVED) The `folder_signature` meant to detect a stored suggestion whose folder changed was never checked, so `auto-enqueue-ready` could import from a track mapping and evidence computed for other files. Find Match and revalidation now take the signature before gathering evidence, revalidation stores it with the match, and the unattended import skips a stale item and logs why. A suggestion stored before signatures existed is inconclusive, not stale.
- (RESOLVED) Find Match runs synchronously for seconds, and the Review page started one for every item the cursor landed on, which could occupy every Waitress worker. The folder, album and item routes share a limit (two at the default `WEBCONTROL_THREADS=8`) and answer `503 suggest_busy` (retryable, with `Retry-After`) at once beyond it. The page runs one automatic Find Match at a time and shows a refusal as a warning.
- Open: the AI review state files (`ai_match_history.json`, `recent_imports.json`, `ai_review_decisions.json`) are hardcoded under `/config`, the Beets config mount, instead of `WEB_MANAGER_DATA_DIR`; moving them is a state migration (a MINOR release). The Review page does not mark a stored suggestion as stale. Only the unattended import reads stored evidence; Apply on the page always needs a fresh Find Match or a validated ID and a fresh target preview.
- Priority: P3 for the open items. Status: Open (narrowed).

## SEC-003 User-Supplied Outbound URLs (CodeQL #1350; supersedes the #18 dismissal)

- Scope: `POST /api/submissions/reference-url` (`routes_submissions._fetch_open_graph_metadata`) and artwork image URLs from users or provider responses (`backend/artwork_service.py` `_download_album_art_bytes` and `_cache_artist_image`, `backend/musicbrainz_service.py` `_release_art_download`, `POST /api/albums/<id>/art/url`).
- Finding: CodeQL alert #18 (`py/full-ssrf`) was dismissed on 2026-07-30 as a false positive. The dismissal cited `validate_outbound_url()` and the global `urlopen` patch, under the SEC-002 entry that a later consolidation removed from this file. When ARCH-006 shifted the sink down one line, the same finding reopened as #1350. On review the dismissal was wrong, for three reasons:
  - Validation and connection resolved DNS separately, so DNS rebinding could reach internal addresses.
  - User URLs were checked against `BEETS_OUTBOUND_ALLOWLIST`, whose default includes the Beets plugin.
  - 100.64.0.0/10 was not blocked.
- Resolution: `backend.security.open_public_url()`/`resolve_public_target()`. Public addresses only, allowlist ignored, socket pinned to the validated address, TLS verified against the original hostname, every redirect hop re-validated. Callers use the dedicated entry point `provider_boundary.opened_public(provider, url, ...)`, which has no path to `urlopen`. A first version passed an `opener=` hook into `opened()`, and CodeQL flagged it as #1351/#1352 because the same parameter could be `urlopen`. Regression tests: `tests/test_reference_url_ssrf.py`, `tests/test_artwork_url_ssrf.py`.
- The #18 dismissal is superseded. Future alerts on these sinks must be checked against this policy, not against `validate_outbound_url()`.
- Status: Closed on the fix branch, pending CodeQL confirmation on the PR.

## SEC-004 Batch Import Link Check Runs Before Beets, Not In It (0.7.0, #394)

- Scope: the batch import's Stage 1 (`backend/ai_service.py` `_ai_batch_preimport_check`) and the webmanager plugin's `run_import`.
- Risk: Web Manager checks the folder for symlinks, unreadable parts and its path below the configured root, and then asks Beets to import it. Between the check and the import (a time-of-check/time-of-use window), a process with write access to the downloads folder could swap a file or directory for a link. Beets' `sorted_walk` follows symlinked directories and reads audio by content, so a link planted in that window would be imported, moved or copied, and have tags written through it.
- Mitigations today: the window is one folder's check-then-import; discovery, the check and Stage 2b's pinned import all refuse links; downloads roots are operator-controlled.
- Fix: the plugin walks the import source (no link following, fail closed on errors) right before it starts the import session, and refuses a source with any link below the source root. This changes plugin behaviour, so it needs a `PLUGIN_VERSION` bump and a Beets restart (a MINOR release under the versioning policy).
- Status: Open. Not a release blocker for 0.7.0.

## SEC-006 Review Queue Writes After A Failed Load (0.7.1 security review)

- Status: Closed in 0.7.1. Every load-modify-write path loads with `_load_pending_reviews(strict=True)`, which raises `PendingReviewLoadError` on an unreadable or torn queue file instead of returning `[]`; `_add_to_pending`, `DELETE /api/ai-pending-review` and the stale-cleanup route save nothing and answer with fixed text. A `BeetsUnavailableError` during the prune keeps the remaining rows unpruned. All queue saves go through `_save_pending_reviews` (atomic temp file plus replace). List-only readers still get `[]` on a failed load.
- Follow-ups (security review, not blockers): F3, `_atomic_write_text` replaces the file without an `fsync`, so a power loss right after a save can leave an empty or old queue file. F4, a newly created queue file gets mode 0600, so an operator tool running as another user cannot read it. Priority: P3. Status: Open.

## SEC-005 Request Text Reaches The qBittorrent Scan Log Unescaped (0.7.0 security review F1, pre-existing)

- Scope: `backend/acquisition_service.py`, the `qBittorrent scan: ...` job log line (`category`, `filter`, `search` from the request).
- Risk (low): control characters in those request fields (newlines, ANSI escapes) are written to the job log as-is, so a caller can forge extra log lines or garble the Jobs page log view. No secret is logged and nothing executes.
- Fix: strip or escape control characters from request text before it goes into job log lines (one shared helper for log formatting).
- Status: Open. Not a release blocker for 0.7.0.

## SEC-001 Retained Plex Credential After Diagnostic Exposure

- Scope: `PLEX_TOKEN`, used only by `beets-web-manager`.
- Risk: the token appeared in private diagnostic session output during earlier development and must be treated as potentially exposed.
- Decision: owner reviewed the exposure and explicitly chose to retain the current token rather than revoke/replace it, accepting the associated risk.
- Existing mitigations: `PLEX_TOKEN` is sent via a request header, never a URL query parameter. A stable, persisted, installation-specific Plex client identifier is sent on every request, so any future rotation is cleanly attributable. Token values are never logged, printed, or included in reports.
- Future recommended action: rotate `PLEX_TOKEN` when convenient (Plex Web → Settings → Account → Authorized Devices → remove the session, then generate a replacement).
- Status: Accepted Risk. Not a release blocker.

## TEST-ENV-001 Windows-Only Backend Suite Failures

- Scope: the full backend suite (`python -m unittest discover -s tests -p "test_*.py"`) on local Windows (Python 3.14). Linux CI is authoritative.
- Windows evidence from #486: clean `main` at `8cac02c` (v0.13.1) ran 5,540 tests with 53 failures, 6 errors and 301 skips. Clean `main` at `5dbb085` (v0.13.2) ran 5,553 tests with 52 failures, 6 errors and 301 skips. The branch's pre-v0.13.2 synchronization run exercised 5,581 tests with 53 failures, 5 errors and 301 skips; it is not represented as a final Linux pass. Against v0.13.2, normalized failure/error names match except the autotag subprocess scenarios: branch `mismatch` hit a Windows PyrateLimiter/asyncio socket-pair/interpreter-shutdown crash, while baseline `provider_down` timed out. The concurrent album-cleanup failure reproduces in both. The Windows focused reliability run exercised 324 tests with 5 failures and 1 cleanup error, while the supported Linux focused run passed 436 tests. No failure/error occurred in the new real-restore module.
- Common causes: Windows path separators in assertions (`\downloads` vs `/downloads`), POSIX-only symlink/fd-relative staging and chmod/root containment semantics, missing `fpcalc`/`ffmpeg`, Beets plugin and global-library state leaking across a 5500-test process, a test configuration left behind by another module, and Windows file-handle `PermissionError` during concurrent cleanup.
- Representative modules: `test_beets_embedded`, `test_beets_plugin_management`, `test_beetsplug_webmanager`, `test_beets214_item_paths`, `test_qa279*`, `test_slskd_*`, `test_staging_*`, `test_sec002_*`, `test_transaction_safety_04`, `test_v0110_upgrade_and_matched_stack`, `test_wave0_s1_containment`.
- No test is skipped, weakened or conditionally disabled to hide these results. The supported Python 3.13 Linux test image and CI remain the acceptance gates; a failure there requires investigation rather than a Windows exemption. Priority: P3. Status: Open (test environment).

# Technical Debt Register

Current, unresolved architecture and security debt only. Statuses: Open, Accepted Risk. Resolved items are removed from this document once closed — their history lives in Git and in the pull request that closed them, not here.

Status stages (never collapsed): IMPLEMENTED (code merged-ready with tests) -> CI VERIFIED (all repository gates green) -> LIVE VERIFIED (production acceptance on TrueNAS) -> CLOSED (desired state reached).

Each entry: affected area, evidence, current risk, desired state, safe migration approach, priority, status.

## ARCH-001 Route Handlers Still Orchestrate Workflows

- Affected area: route modules `routes_library.py`, `routes_cleanup.py`, `routes_import.py`, `routes_maintenance.py`, `routes_playlist.py`, `routes_acquisition.py`.
- Evidence: v0.1.31 decomposed `app.py` (52,665 -> ~570 lines of application glue) into layered owned services with CI guards (see `docs/arch001_service_decomposition.md`; inventory `docs/arch001_app_ownership.json`). What remains (re-measured for v0.1.42: still 26): 26 route handlers over 100 lines still orchestrate inline (largest: `start_maintenance_runner` 494, `dedup_ai_review` 430, `item_attach_recording` 396, `ai_suggest` 338, `import_review_queue` 279, `apply_album_duplicate_resolver` 256), and 155 helpers live in a lower-layer module than their domain (`EXTRACTED_SHARED`).
- Current risk: Workflow logic inside a handler is only reachable through HTTP and is harder to reuse or test directly; shared helpers sit in a neighbor domain's module.
- Desired state: Every handler parses input, calls one service, and shapes the response. Owners: library/artwork -> `library_service`/`artwork_service`; cleanup/dedup -> `cleanup_service`/`dedup_service`; import/review/AI -> `import_service`/`import_review_service`/`ai_service`; maintenance/transactions -> `maintenance_service`/`transaction_service`; playlist -> `playlist_service`; acquisition -> `acquisition_service`.
- Safe migration approach: Move one handler body at a time into its service as a request-free function returning `(json_body, status)` (the pattern used for the in-process route calls in v0.1.31); keep the route shape via `serializers.json_route_result`; `tests/test_arch001_architecture.py` keeps layering intact.
- Priority: P2. Status: Open.

## ARCH-004 Job Persistence And Idempotency Are Uneven

- Affected area: `job_engine.py`, import review jobs, playlist download/sync jobs, AI batch import, acquisition, replacement, maintenance runner.
- Evidence: `JobStore` is in-memory. `PythonJob` supports structured state and cooperative cancellation. Some workflows add checkpoint files and uniqueness checks; others rely on route-local state or result inference.
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
- Wave 5 (IMPLEMENTED): `JobStore.start_python` refuses a job whose explicit `metadata["dedupe_key"]` matches a running job (`DuplicateJobError`, HTTP 409 `job_already_running`). The guard is opt-in because labels and metadata often omit the input that makes two starts different; only Move All and MBSync All opt in. The transaction hook checks and starts under the store lock, so a refused start records no transaction. A job returning `{"ok": false}` ends `failed` (and its hook-created transaction `Failed`); a thread start failure no longer leaves a job running; "clear done" keeps `recovery_required` records.
- Remaining (why this is not Closed):
  - Resume is still each workflow's own: Clean All and playlist download resume from their own checkpoint files, AI batch from its state store. The contract makes the position visible in the job record; it does not resume a job. A restart still leaves the job `recovery_required` for the operator.
  - Not yet under the contract: the roughly 25 shorter mutating jobs (artwork, genre, mbsync-all, move-all, folder and root repairs, Release-Group relinks, resolver jobs). Duplicate starts are guarded unevenly, and only in process:
    - Move All and MBSync All: the wave 5 `dedupe_key` guard (409 `job_already_running`).
    - Clean All: the maintenance-runner `workflow:` lock (`backend/job_contract.py`).
    - Duplicate maintenance and dedup scan/review/cleanup: `_running_job_of_type`.
    - Album rename, artwork, metadata, genre and import starters: no guard. A second start runs; the two are serialized only by the webmanager plugin's global `mutation_lock` (`beetsplug/webmanager/operations.py`) and are safe only because repeating them is idempotent.
    - Across processes, all of them rely on `recovery_required` alone.
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
  - The artwork, artwork-fetch, item-metadata, album-maintenance, album-relocation, genre-repair and import-folder rollbacks record nothing to restore. `_rollback_noop` now refuses unapplied transactions, but on an applied one it marks Rolled Back without restoring anything.
  - These composite applies still mark Running without a compare-and-set: artist-folder reconcile, artwork, artwork fetch, item metadata, album relocation and genre repair.
  - The job wrapper in `transaction_service.start_python_with_transaction` sets its audit transaction Running unconditionally.
  - `routes_maintenance`'s generic rollback cannot reach the composite metadata and MusicBrainz rollbacks: they record no `rollback.operations`.
  - `update_album_metadata(aid, {}, force_write_tags=True)` plans nothing, so it writes no tags.
  - The frontend relink caller must send `mbAlbumId` or `mbReleaseGroupId`. Without one, the endpoint now refuses with `relink_identity_required`.
  - Composite refusal transitions pass `logs=[...]`, which replaces the earlier log lines instead of appending (album metadata and MusicBrainz track repair refusals).
  - `_restore_rows` restores with `move=True` even when the apply did not move files. This must be fixed before composite rollback is advertised broadly (QA #243 F-5).
  - (RESOLVED) A folder cleanup that failed part-way recorded no `engine_result`. Each step is now recorded as it completes, and the generic route rolls back a Completed or Failed `folder_cleanup_v1`. A Partially Rolled Back folder cleanup still needs manual recovery.
  - Folder cleanup steps (`POST /webmanager/folder-op`) check containment and symlinks by path, then act by path, so a folder swapped for a symlink between the check and the `os.rename`/`os.rmdir`/`util.move` could redirect the step (low: needs write access inside the library during the step). The local code it replaced had the same window. Fix: resolve the parent with `os.open(..., O_DIRECTORY | O_NOFOLLOW)` per component and operate relative to it (`dir_fd=` for `rename`/`rmdir`/`mkdir`). In the same window, `rename_dir` on POSIX would silently replace an empty directory created at the target after the existence check (no data loss; `mutation_lock` excludes racers inside Beets); a no-replace rename (`renameat2(RENAME_NOREPLACE)`) is not in the Python standard library.
  - Folder cleanup paths are sent to Beets unchanged, so Web Manager and Beets must mount the library at the same path (the shipped Compose files use `/music` in both).
  - Folder reference checks compare Beets-reported item paths only; an album `artpath` under the folder is not checked.
- Desired state: every composite family captures before-state, claims with a CAS and is reachable from the generic rollback route.
- Priority: P2. Status: Open.

## SEC-003 User-Supplied Outbound URLs (CodeQL #1350; supersedes the #18 dismissal)

- Scope: `POST /api/submissions/reference-url` (`routes_submissions._fetch_open_graph_metadata`) and artwork image URLs from users or provider responses (`backend/artwork_service.py` `_download_album_art_bytes` and `_cache_artist_image`, `backend/musicbrainz_service.py` `_release_art_download`, `POST /api/albums/<id>/art/url`).
- Finding: CodeQL alert #18 (`py/full-ssrf`) was dismissed on 2026-07-30 as a false positive. The dismissal cited `validate_outbound_url()` and the global `urlopen` patch, under the SEC-002 entry that a later consolidation removed from this file. When ARCH-006 shifted the sink down one line, the same finding reopened as #1350. On review the dismissal was wrong, for three reasons:
  - Validation and connection resolved DNS separately, so DNS rebinding could reach internal addresses.
  - User URLs were checked against `BEETS_OUTBOUND_ALLOWLIST`, whose default includes the Beets plugin.
  - 100.64.0.0/10 was not blocked.
- Resolution: `backend.security.open_public_url()`/`resolve_public_target()`. Public addresses only, allowlist ignored, socket pinned to the validated address, TLS verified against the original hostname, every redirect hop re-validated. Callers use the dedicated entry point `provider_boundary.opened_public(provider, url, ...)`, which has no path to `urlopen`. A first version passed an `opener=` hook into `opened()`, and CodeQL flagged it as #1351/#1352 because the same parameter could be `urlopen`. Regression tests: `tests/test_reference_url_ssrf.py`, `tests/test_artwork_url_ssrf.py`.
- The #18 dismissal is superseded. Future alerts on these sinks must be checked against this policy, not against `validate_outbound_url()`.
- Status: Closed on the fix branch, pending CodeQL confirmation on the PR.

## SEC-001 Retained Plex Credential After Diagnostic Exposure

- Scope: `PLEX_TOKEN`, used only by `beets-web-manager`.
- Risk: the token appeared in private diagnostic session output during earlier development and must be treated as potentially exposed.
- Decision: owner reviewed the exposure and explicitly chose to retain the current token rather than revoke/replace it, accepting the associated risk.
- Existing mitigations: `PLEX_TOKEN` is sent via a request header, never a URL query parameter. A stable, persisted, installation-specific Plex client identifier is sent on every request, so any future rotation is cleanly attributable. Token values are never logged, printed, or included in reports.
- Future recommended action: rotate `PLEX_TOKEN` when convenient (Plex Web → Settings → Account → Authorized Devices → remove the session, then generate a replacement).
- Status: Accepted Risk. Not a release blocker.

# SEC-002 / ARCH-003 Wave 25: Import Reconciliation Controlled-Mutation Closure

## Executive Summary

**The `import_reconciliation` domain sits at 0 unresolved sinks in the ARCH-003 mutation inventory**, but that
number was **not trustworthy on its own** when this document last claimed "complete closure." An independent
review round (Claude, acting as final reviewer / merge authority, PR #100) inspected the actual production
code behind every one of the 41 starting sinks and the classification rules that retired them, rather than
accepting the census table and green CI at face value. That review found and fixed **real functional bugs**
that the original Wave 25 implementation's own claims had missed, corrected **two false inventory
classifications**, and only then re-verified the 0-unresolved result. This document records both what Wave 25
originally did and what the independent review round found and corrected — the initial implementation
required correction, and that fact is preserved here rather than erased.

Key achievements (Wave 25, as corrected):
1. **Import Reconciliation Domain Closure**: All production import reconciliation operations
   (`import_folder_with_id._do`, `reimport_disk._do`, `_ai_import_folder`, `start_import._do`,
   `_validate_wanted_download_identity_before_import`) are consolidated under controlled engine transaction
   families (`album_mb_track_repair_v1`, `album_relocation_v1`, `album_metadata_repair_v1`,
   `playlist_media_cleanup_v1`, `album_cleanup_v1`) or truthfully classified as engine-native/staging-only
   operations where no controlled transaction family applies.
2. **Zero Mutating `_beet_run` Fallbacks**: Removed local subprocess `_beet_run` fallbacks for `import`,
   `mbsync`, `write`, `move`, `fetchart`, and `embedart` in production import workflows.
3. **Zero Local SQL DML Fallbacks**: Removed direct authoritative `INSERT`/`UPDATE`/`DELETE`/`REPLACE` SQL
   mutations against Beets library tables in migrated import reconciliation functions.
4. **Controlled Remote Reimport IPC**: Reimport operations route through `BeetsClient.reimport_source` ->
   `POST /imports/reimport` -> Control Agent `reimport_source_atomic` -> native Beets engine — a real,
   self-contained, engine-native atomic operation (see the Independent Review Round section for why this is
   *not* the `import_folder_v1` transaction family, contrary to what this document previously claimed).
5. **41-Sink Baseline Census**: All 41 starting unresolved sinks from baseline commit
   `b9a96bdfa6b9bf478dd111a8eb038d77cee5be38` were independently re-traced against the actual current source
   in the review round (not merely re-asserted from the original classification table).

---

## Independent Review Round — What Was Actually Wrong

The original Wave 25 implementation (PR #100, head `c2b08840bfa227872e84a9a6e3ab7d12c162529a`) claimed the
41-sink census below was fully and correctly resolved, backed by green CI, a clean CodeQL scan, and a
0-unresolved `import_reconciliation` inventory count. Independent source inspection found that several of
those claims did not match what the code actually did:

### Real functional bugs (fixed)

1. **`beets_client.repair_album_artwork()` did not exist.** `reimport_disk`'s post-import artwork step called
   a `BeetsClient` method that was never defined. Every call raised `AttributeError`, silently caught by the
   caller's own `except Exception` and logged as a warning — meaning artwork fetch/embed **never actually ran
   for any import**, while the design doc's row #27 claimed it routed through `album_artwork_v1`. Fixed by
   adding `BeetsClient.fetch_and_embed_album_art()`, a thin, honestly-scoped composition over the pre-existing
   `run_command()` -> `POST /commands/execute` mechanism (the same engine-native path the *old*, pre-Wave-25
   local `beet fetchart`/`beet embedart` subprocess calls used) — not a fabricated transaction-family claim,
   since `fetchart`/`embedart` are a network fetch + tag embed, not a filesystem move/quarantine operation
   `album_artwork_v1`'s Plan/Apply contract actually governs.
2. **Item IDs passed where album IDs were required.** A second loop in `reimport_disk` (`for iid in item_ids:`)
   passed the *item's own row id* directly to `update_album_metadata(int(iid), ...)` and
   `relocate_album(int(iid), ...)`, both of which require a genuine `album_id`. An item id can numerically
   collide with an unrelated album's id, risking wrong-album mutation. Fixed by re-resolving each item's real
   `album_id` fresh from the database (deduplicated per album), skipping items with no album at all (a
   genuinely standalone track has no album-level operation to perform), and using that resolved id
   consistently for every album-scoped call.
3. **The same loop called `plan_album_mb_track_repair({"mb_albumid": mb_albumid})` with no `album_id` at
   all.** `create_album_mb_track_repair_plan()` unconditionally rejects a payload without `album_id > 0`; the
   caller only checked `if p_res.get("ok")` and silently skipped on the expected rejection — a permanent,
   silent no-op disguised as best-effort. Fixed alongside bug #2 above (the resolved album id is now passed).
4. **Three separate `plan_folder_cleanup()` calls used a payload shape its Plan never reads.**
   `create_folder_cleanup_plan()` only recognizes `action` in `{remove_empty_source, remove_empty, safe_rename,
   rename_folder, merge_source_files, merge}` and `payload["source"/"source_folder"]`. Three call sites
   (`_prune_stale_wanted_rows_before_import`, `_delete_album_items_under_folder`, and the pre-import orphan-row
   cleanup in `import_folder_with_id`) instead sent `{"action": "delete_stale_items", "item_ids": [...]}` or
   `{"path": ..., "action": "delete"}` — shapes the Plan silently no-ops on, matched only by the fallback
   `action="remove_empty"` branch (which does nothing unless `source` happens to already be an empty directory)
   or outright rejected. Every one of these calls reported success while never deleting the DB rows (or, in the
   `{"path":...}` case, the folder) it claimed to. Fixed by routing all three through
   `playlist_media_cleanup_v1` (`plan_playlist_media_cleanup`/`apply_playlist_media_cleanup`), the family that
   actually reads `item_ids` and retires both the item row and its file (when one still exists) — or, for the
   one genuine folder-removal case, the correct `{"source": ..., "action": "remove_empty"}` payload shape.
5. **A raw, DB-unaware `beets_client.delete_file()` call preceded `plan_album_cleanup`/`apply_album_cleanup`**
   in `_cleanup_failed_import_copy`, deleting each item's file before the real transaction ran — redundant
   (that transaction's own Apply already deletes both the DB rows and the files, with its own symlink/TOCTOU
   checks) and unsafe (the raw pre-delete bypassed those checks for the file half of the deletion). Fixed by
   removing the redundant raw delete and letting the existing transaction do the whole job.
6. **`_merge_imported_album_into_existing._delete_row_file` was dead code.** This helper (row #02 in the
   census below) directly deleted a Beets-DB-tracked file via a generic engine call with no corresponding DB
   transaction — exactly the "generic delete is not a transaction" pattern this review's threat model flags.
   It turned out to be defined but never called anywhere: every actual code path (`replace_rows` ->
   `bulk_import_replacement_v1`, `dup_rows`/`move_ids` -> `existing_album_reconcile_v1`) already delegates both
   the file and the DB row to a real controlled transaction family. Removed.

### False inventory classifications (corrected)

7. **`reimport_source_atomic()`/`preserve_import_source()` (and the `beets_client.reimport_source` call site)
   were classified `ENGINE_CONTROLLED_TRANSACTION` / `import_folder_v1`.** Independent inspection of
   `reimport_source_atomic()`'s actual body found zero references to `TransactionStore`, `transaction_engine`,
   `mutation_family`, or `operation_id` — it does not enter `import_folder_v1`'s Plan/Apply/Rollback contract
   at all, even though that family genuinely exists in `transaction_engine.py` for other purposes. It *is* a
   real, well-built engine-native atomic operation (root-containment + symlink checks via `resolve_safe_path`,
   source-signature staleness check, deterministic-identity verification, OS-level locking, then a real native
   Beets import) — just not that specific transaction family. Reclassified to `ENGINE_NATIVE_BEETS` with no
   fabricated family, per the principle that a secure engine-native atomic operation may have a truthful
   distinct classification, but must never claim a transaction contract it does not actually use.
8. **The `APP_STATE` broad-hint list gained five new substrings** (`recent_import`, `pending`, `auto_import`,
   `import_review`, `import_all`) matched against both call-site text and the enclosing function name, with no
   check on what the mutation actually targets. Every function newly caught by those five terms was
   individually verified (all eleven genuinely only ever touch the app-local pending-review/recent-import JSON
   bookkeeping files, never a Beets DB item path or anything under `MUSIC_ROOT`), then listed by exact name in
   a new `_APP_STATE_VERIFIED_FUNCTIONS` allowlist, and the five broad substrings were removed. A regression
   test (`test_app_state_classifier_rejects_a_disguised_media_mutation_function`) proves a *hypothetical* real
   media-mutation function whose name merely contains one of those substrings is not silently waved through.

None of these corrections changed the final `import_reconciliation = 0` result — every genuinely resolved
sink was, in fact, resolved; the bugs were in *how* several of them were resolved (silent no-ops, a
never-existing method, wrong ids), not in whether the domain was closeable at all. Fixing them was required
before the 0 could be trusted rather than merely asserted. Fourteen new regression tests
(`tests/test_wave25_round_review_fixes.py`) prove each defect fixed here cannot silently return.

---

## Starting 41-Sink Census (`b9a96bdfa6b9bf478dd111a8eb038d77cee5be38`)

Independently regenerated from the baseline inventory JSON (`scripts/census_import_reconciliation.py`) rather
than re-typed from the original table — confirmed to match it exactly. The **Final Disposition** column below
reflects the independent review round's corrected, verified state, not the original PR's claims.

| # | Function | Line | Kind | Call Text (baseline) | Final Disposition |
|---|---|---|---|---|---|
| 01 | `_prune_stale_wanted_rows_before_import` | 6493 | sql | `DELETE FROM items WHERE id IN (...)` | `CONTROLLED_TRANSACTION` (was a `plan_folder_cleanup` no-op; fixed to `playlist_media_cleanup_v1`) |
| 02 | `_merge_imported_album_into_existing._delete_row_file` | 6592 | filesystem | `resolved.unlink(missing_ok=True)` | `DEAD_CODE_REMOVED` (never called; real paths already used `bulk_import_replacement_v1`/`existing_album_reconcile_v1`) |
| 03 | `_delete_staged_import_folder` | 13208 | filesystem | `shutil.rmtree(str(folder))` | `STAGING_ONLY_ENGINE_ADMIN` (verified: refuses any path under `MUSIC_ROOT`, requires an app-managed download path) |
| 04 | `_record_recent_import` | 15983 | filesystem | `_RECENT_IMPORTS_FILE.write_text(...)` | `APP_STATE_FALSE_CLASSIFICATION` (verified app-local JSON bookkeeping, not media) |
| 05 | `clear_recent_imports` | 16040 | filesystem | `_RECENT_IMPORTS_FILE.write_text("[]")` | `APP_STATE_FALSE_CLASSIFICATION` |
| 06 | `import_cleanup_stale` | 17028 | filesystem | `_AI_PENDING_FILE.write_text(...)` | `APP_STATE_FALSE_CLASSIFICATION` |
| 07 | `_write_import_review_auto_state` | 19664 | filesystem | `_IMPORT_REVIEW_AUTO_IMPORTS_FILE.parent.mkdir(...)` | `APP_STATE_FALSE_CLASSIFICATION` |
| 08 | `_write_import_review_auto_state` | 19665 | filesystem | `_IMPORT_REVIEW_AUTO_IMPORTS_FILE.write_text(...)` | `APP_STATE_FALSE_CLASSIFICATION` |
| 09 | `import_folder_with_id._do._cleanup_failed_import_copy` | 21311 | filesystem | `resolved.unlink(missing_ok=True)` | `CONTROLLED_TRANSACTION` (`album_cleanup_v1`; redundant raw pre-delete removed) |
| 10 | `import_folder_with_id._do._cleanup_failed_import_copy` | 21315 | sql | `DELETE FROM items WHERE album_id=?` | `CONTROLLED_TRANSACTION` (`album_cleanup_v1`) |
| 11 | `import_folder_with_id._do._cleanup_failed_import_copy` | 21316 | sql | `DELETE FROM albums WHERE id=?` | `CONTROLLED_TRANSACTION` (`album_cleanup_v1`) |
| 12 | `import_folder_with_id._do._rollback_failed_library_source_import` | 21358 | sql | `DELETE FROM items WHERE album_id=?` | `CONTROLLED_TRANSACTION` (`album_cleanup_v1`) |
| 13 | `import_folder_with_id._do._rollback_failed_library_source_import` | 21359 | sql | `DELETE FROM albums WHERE id=?` | `CONTROLLED_TRANSACTION` (`album_cleanup_v1`) |
| 14 | `import_folder_with_id._do` | 22089 | filesystem | `resolved_src.unlink(missing_ok=True)` | `STAGING_ONLY_ENGINE_ADMIN` (verified: only reached for source files confirmed outside `MUSIC_ROOT`) |
| 15 | `import_folder_with_id._do` | 22129 | filesystem | `f.unlink(missing_ok=True)` | `STAGING_ONLY_ENGINE_ADMIN` |
| 16 | `import_folder_with_id._do` | 22133 | filesystem | `sub.rmdir()` | `STAGING_ONLY_ENGINE_ADMIN` |
| 17 | `import_folder_with_id._do` | 22137 | filesystem | `src_dir.rmdir()` | `CONTROLLED_TRANSACTION` (`folder_cleanup_v1`; corrected from a wrong `{"path":...,"action":"delete"}` payload that always no-op'd) |
| 18 | `import_folder_with_id._do` | 22146 | filesystem | `shutil.rmtree(import_folder_path, ignore_errors=True)` | `STAGING_ONLY_ENGINE_ADMIN` |
| 19 | `reimport_disk._do._repair_existing_album_in_place` | 22788 | sql | `UPDATE albums SET mb_albumid=...` | `CONTROLLED_TRANSACTION` (`album_mb_track_repair_v1`) |
| 20 | `reimport_disk._do._repair_existing_album_in_place` | 22792 | sql | `UPDATE items SET mb_albumid=...` | `CONTROLLED_TRANSACTION` (`album_mb_track_repair_v1`) |
| 21 | `reimport_disk._do._repair_existing_album_in_place` | 22804 | subprocess | `_beet_run(...)` | `CONTROLLED_TRANSACTION` (`album_metadata_repair_v1`/`album_relocation_v1`) |
| 22 | `reimport_disk._do` | 23309 | filesystem | `f.rename(new_path)` | `STAGING_ONLY_ENGINE_ADMIN` (same-directory pre-import filename normalization on the source folder, never a DB-tracked path; self-corrects via the subsequent reimport/reconciliation pass) |
| 23 | `reimport_disk._do` | 23351 | sql | `DELETE FROM items WHERE id IN (...)` | `CONTROLLED_TRANSACTION` (was a `plan_folder_cleanup` no-op; fixed to `playlist_media_cleanup_v1`) |
| 24 | `reimport_disk._do` | 23361 | sql | `DELETE FROM albums WHERE id = ?` | `CONTROLLED_TRANSACTION` (retired automatically by `playlist_media_cleanup_v1` Apply when the last item under an album is removed) |
| 25 | `reimport_disk._do` | 23438 | subprocess | `beets_client.reimport_source(...)` | `ENGINE_NATIVE_ATOMIC_OPERATION` (corrected from a false `import_folder_v1` claim — see Independent Review Round) |
| 26 | `reimport_disk._do` | 23568 | sql | `UPDATE albums SET mb_albumid = ?...` | `CONTROLLED_TRANSACTION` (`album_mb_track_repair_v1`) |
| 27 | `reimport_disk._do` | 23621 | sql | `UPDATE albums SET mb_albumid = ?...` | `CONTROLLED_TRANSACTION` (`album_mb_track_repair_v1`) |
| 28 | `reimport_disk._do` | 23744 | subprocess | `_beet_run(mbsync)` | `CONTROLLED_TRANSACTION` (`album_mb_track_repair_v1`) |
| 29 | `reimport_disk._do` | 23760 | sql | `UPDATE albums SET albumartist=?...` | `CONTROLLED_TRANSACTION` (`album_metadata_repair_v1`) |
| 30 | `reimport_disk._do` | 23762 | sql | `UPDATE items SET albumartist=?...` | `CONTROLLED_TRANSACTION` (`album_metadata_repair_v1`) |
| 31 | `reimport_disk._do` | 23778 | subprocess | `_beet_run(write/move)` | `CONTROLLED_TRANSACTION` (`album_metadata_repair_v1`/`album_relocation_v1`) |
| 32 | `reimport_disk._do` (item-level loop) | 23836 | subprocess | `_beet_run(...)` | `CONTROLLED_TRANSACTION` (fixed item-id/album-id bug; now `album_mb_track_repair_v1`/`album_metadata_repair_v1`/`album_relocation_v1` with a correctly-resolved album id) |
| 33 | `reimport_disk._do` | 23861 | subprocess | `_beet_run(fetchart/embedart)` | `ENGINE_NATIVE_ATOMIC_OPERATION` (fixed nonexistent-method bug; `BeetsClient.fetch_and_embed_album_art` over `POST /commands/execute`) |
| 34 | `_library_import_all_write_last` | 23999 | filesystem | `tmp.write_text(...)` | `APP_STATE_FALSE_CLASSIFICATION` |
| 35 | `_ai_import_folder` | 26450 | subprocess | `_beet_run(import)` | `ENGINE_NATIVE_ATOMIC_OPERATION` (`beets_client.reimport_source`) |
| 36 | `_ai_import_folder` | 26508 | sql | `UPDATE albums SET mb_albumid = ?...` | `CONTROLLED_TRANSACTION` (`album_mb_track_repair_v1`) |
| 37 | `_ai_import_folder` | 26509 | sql | `UPDATE items SET mb_albumid = ?...` | `CONTROLLED_TRANSACTION` (`album_mb_track_repair_v1`) |
| 38 | `_ai_import_folder` | 26520 | subprocess | `_beet_run(...)` | `CONTROLLED_TRANSACTION` (`album_metadata_repair_v1`/`album_relocation_v1`) |
| 39 | `start_import` | 28441 | filesystem | `Path(path).mkdir(...)` | `APP_STATE_FALSE_CLASSIFICATION` (import source directory creation, non-media) |
| 40 | `start_import._do` | 28476 | subprocess | `_beet_run(command)` | `ENGINE_NATIVE_ATOMIC_OPERATION` (`beets_client.reimport_source`) |
| 41 | `_validate_wanted_download_identity_before_import` | 46047 | filesystem | `Path(path_value).unlink()` | `STAGING_ONLY_ENGINE_ADMIN` (verified: only unverified wanted-track downloads, pre-import, never a library path) |

**Disposition totals**: 20 `CONTROLLED_TRANSACTION`, 4 `ENGINE_NATIVE_ATOMIC_OPERATION`, 8 `STAGING_ONLY_ENGINE_ADMIN`,
8 `APP_STATE_FALSE_CLASSIFICATION`, 1 `DEAD_CODE_REMOVED` = 41.

---

## Generic Engine Admin vs. Controlled Transaction — The Actual Distinction

This review's core finding was architectural, not just a handful of isolated bugs: `beets_client.delete_file()`
and `beets_client.move_file()` route to the Control Agent's generic `/files/delete` and `/files/move`
endpoints, which accept **either** the `music` **or** `staging` allowed roots and perform the filesystem
operation with no DB awareness, no Plan/Apply/Verify, and no rollback. Being reached over HTTP from the engine
container does **not** make a call "controlled" in the ARCH-003 sense — it is real, direct filesystem
administration, safe only when every one of these holds:

- the resource is genuinely staging/download/acquisition state, never authoritative library media;
- the path is proven contained under a server-owned root, with symlink components rejected;
- no Beets DB row is being retired as part of the same semantic operation.

Rows 03, 14–16, 18, 22, and 41 above satisfy all three (verified individually against the actual code, not
assumed from the generic method name) and are correctly `STAGING_ONLY_ENGINE_ADMIN`. Every sink where the
target *could* be a DB-tracked item — rows 01, 09–13, 17, and 23–24 — has been verified to go through a real
transaction family (`album_cleanup_v1` or `playlist_media_cleanup_v1`) instead, which was not true of three of
them (01, 17, 23) before this review round.

---

## Reimport IPC Flow

Production reimport flows (`reimport_disk`, `_ai_import_folder`, `start_import._do`) invoke:

```
app.py --reimport_source--> BeetsClient --POST /imports/reimport--> Control Agent
       --reimport_source_atomic--> Native Beets Engine
```

This is a genuine engine-native atomic operation (root-containment, symlink rejection, source-signature
staleness check, deterministic-identity verification, OS-level locking, then a real native Beets import) — but,
per the Independent Review Round above, it is **not** `import_folder_v1`'s `TransactionStore`-based
Plan/Apply/Rollback contract, and the inventory no longer claims that it is.

Zero mutating `_beet_run` calls or local SQLite `UPDATE`/`DELETE` queries remain in production import
workflows.

---

## Verification Matrix & Test Status (final, post-review-round)

1. **Python Test Suite** (local, Windows dev environment, run twice):
   - Total test cases discovered: `2,644` (2,627 prior baseline + 17 new regression tests in
     `tests/test_wave25_round_review_fixes.py`)
   - Run #1: **2644 / 2644 passed, 0 failures, 0 errors**
   - Run #2: **2644 / 2644 passed, 0 failures, 0 errors**
2. **Frontend Gates** (`frontend/`): `typecheck`, `lint`, `test` (48/48), `build`, `audit --audit-level=high`
   (0 high/critical) — all passed. This PR does not touch frontend code.
3. **Security Gates**: `security_secret_scan.py` passed; `generate_endpoint_inventory.py --check` passed (245
   routes, 0 NEEDS_REVIEW); `validate_compose_security.py` passed; `verify_arch003_mutation_inventory.py
   --check` passed.
4. **ARCH-003 Inventory** (post-review-round, regenerated): `total_sinks = 471`, `unresolved_baseline = 134`,
   `unresolved_domain_counts.import_reconciliation = 0`. Unchanged in total from the pre-review-round count —
   the corrections fixed *how* sinks were resolved, not *whether* the domain total was accurate.

See the PR body and `docs/TECHNICAL_DEBT.md` for final CI/CodeQL/Docker-acceptance state on the exact merged
head.

---

## Round 3 — `confirmed_import_v1`: Two Distinct Import Trust Models

The dedicated Wave 25 Docker acceptance round (above) found that routing fresh imports through
`reimport_source_atomic()` / `verify_deterministic_identity()` was not a small bug but a genuine architecture
mismatch: `verify_deterministic_identity()` is a deliberate SEC-002 Wave 8 security control that requires the
source audio's **own embedded MusicBrainz tags** to already match the caller-supplied target identity before
any import runs. That is the correct model for a genuine *re*-import (already-tagged library content being
reorganized or re-verified) — it is the wrong model for `import_folder_with_id()`'s actual job, which is a
human reviewing an **untagged** download and manually confirming which MusicBrainz release it is. Untagged
source audio has no embedded MB tags to verify by construction, so `verify_deterministic_identity()` rejected
essentially every real fresh import, not just the round's synthetic test fixture.

The fix is **not** to weaken `verify_deterministic_identity()` — that would collapse two genuinely different
authorization models into one weaker model, exactly what Round 3's brief explicitly forbade. Instead, this
round built a second, distinct controlled family:

### REIMPORT TRUST MODEL (unchanged)

```
Existing embedded MusicBrainz identity
  -> verify_deterministic_identity() (requires embedded tags to already match)
  -> reimport_source_atomic()
```

Used by `BeetsClient.reimport_source()` / `POST /imports/reimport` (`reimport_disk`, `_ai_import_folder`,
`start_import._do`). No code in this family changed in Round 3: `verify_deterministic_identity()` and
`reimport_source_atomic()` still have the exact same signatures, no `skip_identity_check`/`force`/
`trust_caller`/`ignore_missing_tags` escape hatch was added to either, and `run_confirmed_import_native()`
(below) never calls either of them — enforced by
`tests/test_confirmed_import_v1.py::TestReimportSecurityModelUnchanged`.

### FRESH REVIEWED IMPORT TRUST MODEL (new: `confirmed_import_v1`)

```
No embedded MusicBrainz identity required
  -> immutable source manifest digest (re-checked at Apply -- any file
     added/removed/resized/retimestamped invalidates the Plan)
  -> concrete, explicitly reviewed target Release ID (never a Release
     Group ID -- Beets' --search-id needs a real release)
  -> Release -> Release Group validated, never silently substituted
  -> best-effort track alignment (backend/track_align.align_tracks --
     reused, not a new matcher) + AcoustID fingerprint evidence where
     available (never required; absence is never itself a conflict)
  -> native Beets `import --search-id` inside the engine
  -> Stage-3 verify: query the library by the exact planned Release ID,
     confirm items and files exist on disk, confirm mb_releasegroupid
     matches if given -- only then Completed
```

Missing embedded MusicBrainz tags are the **expected, valid** case here. A **conflicting** embedded tag
(present and different from the approved target) is still a hard rejection
(`confirmed_import_embedded_identity_conflict`) — missing and conflicting are not the same thing.

**Components** (`backend/transaction_engine.py`, new `confirmed_import_v1` section):
- `create_confirmed_import_plan()` — non-mutating. Validates the concrete target Release ID/RGID, requires a
  non-empty pre-fetched tracklist (fetched by the caller, which has MusicBrainz API access this module
  deliberately does not — matching the existing `fetch_tracklist_fn` injection precedent), calls the injected
  `inspect_source_fn` (engine-only real filesystem access) for a bounded audio inventory + source manifest
  digest, rejects a conflicting embedded tag, aligns tracks via `backend.track_align.align_tracks` +
  `resolve_unmatched_via_acoustid`, and rejects if fingerprint evidence strongly identifies a known, different
  recording (`confirmed_import_acoustid_conflict`) or if zero tracks align at all
  (`confirmed_import_no_track_alignment`). Missing tracks alone (an intentionally incomplete album) do not
  block the Plan.
- `execute_confirmed_import_apply()` — idempotency/crash-resume first: queries the library for an already-
  verified album at the planned Release ID before doing anything else, and resumes that result instead of ever
  invoking native import a second time for the same target (this is what makes both "retry an already-
  completed import" and "the process crashed after native import succeeded but before Completed was recorded"
  safe). Otherwise, TOCTOU-rechecks the source manifest digest (`confirmed_import_stale_plan` on mismatch),
  invokes the injected `run_native_import_fn`, persists a `native_import_invoked` checkpoint immediately
  regardless of outcome, then captures and verifies the result by querying the library for the planned Release
  ID — never guessed from "most recently added". An ambiguous outcome (native import reported success but no
  verifiable album is found) is `Recovery Required`, never silently `Completed`. Non-reversible after Apply,
  same reasoning as `album_artwork_fetch_v1`: undoing a real Beets import cannot be truthfully guaranteed.
- `run_confirmed_import_native()` (`backend/beets_control_agent.py`) — the injected native-import runner.
  Reuses `reimport_source_atomic`'s low-level mechanics (`preserve_import_source` for torrent-staged sources,
  `acquire_os_lock`/`release_os_lock`, `BEETSDIR` env, timeout scaling) **without** its identity gate, per the
  brief's explicit instruction to share execution mechanics but never trust policy across the two models.
- New Control Agent routes: `POST /imports/confirmed/plan`, `POST /imports/confirmed/apply`.
- New `BeetsClient` methods: `plan_confirmed_import()`, `apply_confirmed_import()`.
- `import_folder_with_id()` (`app.py`) now calls `plan_confirmed_import`/`apply_confirmed_import` instead of
  the old `plan_import_folder`/`apply_import_folder` (`import_folder_v1`). `import_folder_v1` itself
  (`create_import_folder_plan`/`execute_import_folder_apply` in `transaction_engine.py`) is left in place,
  untouched and still tested, simply no longer invoked by production code for this workflow — removing a
  working, tested, harmless family would have been unnecessary churn.

**Known limitation, documented rather than silently accepted**: crash-safety is proven for the required
acceptance scenario — a crash *after* native import fully succeeds but before `Completed` is recorded (handled
by the idempotency pre-check, which resumes via the `mb_albumid`-uniqueness signal rather than re-importing). A
crash *mid-subprocess* (during the `beet import` call itself) is a materially harder problem already present in
`reimport_source_atomic`'s own design, not introduced by this family, and is out of this round's scope.

Track alignment reuses `backend/track_align.py` (`align_tracks`, `resolve_unmatched_via_acoustid`) unchanged —
`Dockerfile.beets` now also copies it into the engine image alongside `matching_contract.py`. AcoustID lookups
reuse the engine-local `_engine_acoustid_lookup()` (already used by the playlist-import identity gate); no new
matcher or fingerprint client was written.

---

## Round 4 — The Real Docker Failure: `--quiet-fallback asis` and an Unrealistic Fixture

Round 3's `confirmed_import_v1` was built and unit-tested locally, but its native-import path had never run
against real Docker. The first real run found a genuine bug, not a re-run of the already-fixed
`verify_deterministic_identity` blocker:

```
[FAIL] fresh-import: ... ERROR: Beets API request error: Native import reported success
       but the resulting album could not be verified
```

**Diagnosis** (proven from the real engine container's own before/after startup-guard log lines, not guessed):
the host-mounted library went from 1 album / 1 item to 2 albums / 13 items across the run — Beets genuinely
imported something — but `confirmed_import_v1`'s post-import query for `mb_albumid = <planned Release ID>`
found nothing. Two compounding causes:

1. **`run_confirmed_import_native()` used `--quiet-fallback asis`**, copied verbatim from
   `reimport_source_atomic`'s command construction in Round 3. In quiet mode, `asis` lets Beets import the
   source WITHOUT ever applying the forced `--search-id` candidate whenever its own match confidence for that
   candidate isn't strong enough — it tags/organizes the files under Beets' own guess (or no MusicBrainz
   identity at all) instead of failing closed. This is tolerable for `reimport_source_atomic` (the source is
   already-organized, already-tagged library content being re-verified) but wrong for `confirmed_import_v1`,
   whose entire authorization model is "trust THIS reviewed release, nothing else." **Fixed**: switched to
   `--quiet-fallback skip` — if Beets cannot confidently apply the approved release, the import is skipped
   entirely rather than silently proceeding under an unreviewed identity.
2. **The Docker fixture was unrealistic**: 12 synthetic WAV files at a flat ~0.3 seconds each, shaped like the
   real release's tracklist by filename only, against tracks that are actually 2-6 minutes long. Beets' match-
   confidence scoring for a forced `--search-id` candidate depends heavily on duration/track-count fit (this
   codebase never fingerprints audio content during a plain import) — an extreme duration mismatch alone was
   enough to starve that confidence regardless of filename/title similarity. **Fixed**: the acceptance script
   now fetches the real release's tracklist (title + duration) from MusicBrainz once per run (cached), and
   generates synthetic audio at the REAL durations, with basic (non-MusicBrainz) tags — title, artist, album,
   track number — written via `mediafile` (the same tag library Beets itself uses). No MusicBrainz Release/
   ReleaseGroup/Recording ID is ever written to the fixture; `assert_wave25_source_has_no_mb_ids()` proves this
   before every real import request, so the test cannot silently pass by accidentally proving the wrong trust
   model (reimport of pre-tagged content, not confirmed import of untagged content).

**Distinguishing a clean skip from a partial mutation**: switching to `skip` on its own would only change the
symptom (a truthful "skipped" outcome is still an outcome `execute_confirmed_import_apply` must interpret
correctly). `run_confirmed_import_native()` now reports `albums_before`/`albums_after` (a plain library album
count taken immediately before and after the subprocess call), and `execute_confirmed_import_apply()` uses
that to tell a real "nothing happened" skip (`Failed`, `confirmed_import_skipped_no_confident_match`, zero
mutation) apart from a genuine partial mutation under the wrong identity (`Recovery Required`,
`confirmed_import_verification_failed`, as before) — never the same undifferentiated message for both. Bounded,
redacted native-import diagnostics (return code, stdout/stderr excerpts — paths redacted, never secrets/config
contents) are now persisted into the transaction's own metadata on either outcome, so a future failure does not
require re-deriving this from raw container logs by hand.

**A true crash-after-native-success test**: the prior crash/resume scenario only proved "complete an import,
hard-kill+restart the web-manager container, resubmit" — a restart+idempotency test, not a test of the
required window (native import succeeds, verification/completion has not happened yet, the process is
interrupted). Building that window without a timing race required a small, deliberately narrow piece of test
infrastructure: a single named failpoint, `confirmed_import_after_native`, checked in
`execute_confirmed_import_apply()` immediately after the `native_import_invoked` crash-safety checkpoint is
durably persisted and before the post-import capture/verify step. It is inert everywhere except this
acceptance stack:

- The request field (`_acceptance_failpoint`, threaded through `import_folder_with_id()` →
  `BeetsClient.apply_confirmed_import()` → `POST /imports/confirmed/apply`) is read by the Control Agent at
  all **only** when its own container was booted with `BEETS_ACCEPTANCE_MODE=1` — a module-level constant read
  once at process start, set **only** in `docker/acceptance/docker-compose.acceptance.yml`, never in
  `docker-compose.yml`/`docker-compose.full.yml`. A real deployment ignores the field unconditionally no matter
  what a client sends.
- Only the single exact string `"confirmed_import_after_native"` has any effect — not a generic arbitrary
  failure-injection API.

The rewritten scenario: Plan a real untagged import against a **second**, independent, long-stable release
(Pink Floyd's *The Dark Side of the Moon* — `confirmed_import_v1`'s idempotency is keyed on the target Release
ID globally, so reusing the fresh-import scenario's own release would let this scenario's "initial import"
silently resume whatever fresh-import already completed, never reaching native import at all); Apply with the
failpoint enabled; confirm the job genuinely did **not** report success (the failpoint fired) while also
confirming a real album now exists in the library (native import genuinely ran and mutated real state before
the simulated crash); restart the **engine** container (proving the persisted transaction record survives a
real process restart, not just an in-memory continuation); retry the identical import with the failpoint
disabled; require the retry's own job log to report resuming a prior verified result (proof, not inference,
that native import was not invoked a second time); require exactly one album row for the release afterward.

**Artwork acceptance honesty**: `fetchart` was not loaded in the acceptance engine's Beets config at all
(`plugins: ''`) — the scenario's prior "truthful failure" was actually "unknown command," not a real attempt
that legitimately found no art. Fixed by enabling the (core, no extra install) `fetchart` plugin. The scenario
now reports two separate claims rather than one overstated one: `artwork-transaction-fail-closed` (the
transaction's status semantics are truthful either way) and `artwork-successful-acquisition` (only claimed
when a real `artpath` is verified — the fixture album's synthetic identity means this legitimately may never
succeed, and the script says so explicitly rather than calling a failed fetch a passing acquisition test).

**A second real Docker iteration within Round 4** found the `--quiet-fallback skip` fix working exactly as
intended (an honest `confirmed_import_skipped_no_confident_match`, zero mutation, no more silent wrong-identity
import) — but even with real durations, exact titles, artist, and album tags, Beets' own recommendation for the
forced `--search-id` candidate still did not reach "strong" in quiet mode, so the import was skipped rather
than applied. Two changes, in order of how directly they address the actual gap: (1) the fixture was missing
`albumartist` (only `artist` was set) and `tracktotal` — real downloads normally carry both, and Beets' distance
calculation does consider them; (2) `run_confirmed_import_native()`'s per-Apply temp config now also sets
`match.strong_rec_thresh: 0.25` (relaxed from Beets' unsupervised-import default of `0.04`), narrowly scoped to
this family's own disposable config file — never the reimport path's config, never the user's real
`config.yaml`. This is not "disable matching to make the test pass": `confirmed_import_v1`'s authorization
never came from Beets' own recommendation confidence in the first place — it comes from
`create_confirmed_import_plan()`'s independent review (immutable source manifest, the caller's explicitly
reviewed concrete Release ID/RGID, and real track/fingerprint alignment against that exact release, all already
verified before Apply runs). Beets' strict default threshold is tuned for *unsupervised* autotagging, where it
is the only safeguard against a wrong match; here it is a second, uncoordinated gate stacked on top of a real
one it was never designed to cooperate with. `--quiet-fallback skip` remains the actual backstop — a mismatch
far outside what the Plan already reviewed still fails closed rather than importing under the wrong identity.

**Round 4, corrected: the actual root cause, found by direct reproduction rather than another guess.** The
`strong_rec_thresh: 0.25` relaxation above did not fix the real Docker run, and neither did the subsequent
WAV → FLAC fixture-format change (both were reasonable hypotheses given the evidence available at the time —
correct item count, exit code 0, a bare `Skipping.` with no title/artist mismatch detail — but neither was
tested independently of the full two-service Docker stack before being committed). Per the standing instruction
to prove what Beets actually did rather than guess again, the fix that follows was verified locally, outside
Docker, by calling `beets.autotag.match.tag_album()` directly against Item objects carrying the exact tags this
fixture writes (title/artist/albumartist/album/track/tracktotal/length, real MusicBrainz durations, no MB IDs),
with `search_ids=[<the real release id>]` — reproducing precisely what `beet import --search-id` does
internally.

With the engine's config exactly as committed (`plugins: fetchart`, no `musicbrainz`), this reproduction
returned **zero candidates** and `Recommendation.none` — not a low-confidence match. The actual bug: Beets'
`plugins` config key is not additive across config sources. `beets.config["plugins"].as_str_seq()` resolves
from whichever loaded config source defines `plugins` with the highest priority; it does not merge that
source's list with the packaged default's `plugins: [musicbrainz]`. Setting `plugins: fetchart` in the
disposable acceptance library's `config.yaml` (added earlier in Round 4 to fix the artwork scenario, see above)
silently replaced the default plugin list, unloading `musicbrainz` — the plugin that actually resolves
`--search-id`/`search_ids` into a real candidate via the MusicBrainz API. With it unloaded, there was never a
candidate to weigh any threshold against, which is exactly consistent with the bare `Skipping.` and no further
diagnostic detail observed in every prior iteration (title/artist mismatch detail is something Beets can only
print about a candidate it actually found).

Fixed: `seed_disposable_library()`'s config now reads `plugins: fetchart musicbrainz` (both listed explicitly).
Re-running the same local reproduction with the `musicbrainz` plugin loaded (no other changes) returns exactly
one candidate, at the correct release, with **distance `0.0`** and recommendation **`strong`** — under Beets'
own *default*, unmodified `strong_rec_thresh` (`0.04`). This proves the earlier `strong_rec_thresh: 0.25`
relaxation was never actually necessary and was masking the real bug rather than fixing it, so it has been
reverted; `run_confirmed_import_native()`'s per-Apply temp config once again leaves Beets' match strictness at
its default, matching the Round 4 brief's instruction not to blindly force matching. The WAV → FLAC fixture
change is kept (a genuine, if not load-bearing, reliability improvement for cross-installation tag reading) but
is not what fixed this failure.

**Round 4, second real Docker iteration after the plugin fix**: native import now genuinely succeeded end to
end for the first time (`Engine controlled import completed`, `Found 1 album(s) via confirmed_import_v1 verified
result`), and the rebuilt crash-resume scenario's idempotency proof also worked exactly as designed (`Resumed an
already-verified prior result for this release (native import was not re-invoked)`). Both scenarios then failed
identically one step later: `Could not validate imported album: Raw SQLite queries are not permitted; use
structured library query helpers`. Cause: `import_folder_with_id()`'s legacy per-album re-validation helper,
`_album_match_summary()`, runs unconditionally over every discovered `album_id` regardless of how it was found,
and does so via raw SQL through `_db()` — which resolves to `BeetsClient.raw_sqlite_query()`, a method that has
always unconditionally raised in the two-service topology (a deliberate, pre-existing SEC-002 control requiring
structured query helpers instead of ad-hoc SQL). This is not a new bug introduced by `confirmed_import_v1`; it is
a pre-existing dead/broken code path in the legacy heuristic album-discovery machinery that had simply never
been reached by any prior real Docker run, because every earlier run failed before getting this far. Fixed by
skipping `_album_match_summary()` entirely for an album already found via the `"confirmed_import_v1 verified
result"` strategy — that result was already authoritatively verified server-side, through structured queries
(exact planned Release ID + RGID match, items/files confirmed on disk) inside
`execute_confirmed_import_apply()`'s own capture/verify step, so re-deriving the same conclusion through a
second, redundant, and in this topology non-functional path added no safety and only broke a working import.
The legacy strategies (A–I) and their raw-SQL re-validation are left untouched for every other discovery path,
which is outside this round's scope. See `tests/test_wave25_docker_acceptance_round_fixes.py`'s
`TestConfirmedImportV1ResultSkipsRedundantRawSqlRevalidation` for the regression test.

# SEC-002 / ARCH-003 Wave 20: Existing-Album Duplicate & Move Bookkeeping Controlled Mutation Boundary

**Implementation Engineer Report & Design Document**

---

## 1. Executive Summary

Wave 20 migrates the remaining uncontrolled mutation and bookkeeping behavior inside `_merge_imported_album_into_existing`—specifically `dup_rows` deletion, `move_ids` album reassignment, and empty temporary album retirement—behind an engine-owned controlled mutation boundary: `existing_album_reconcile_v1`.

Before Wave 20:
- `dup_rows` were deleted directly from SQLite (`DELETE FROM items WHERE id IN (...)`) and their audio files unlinked directly from the Web Manager (`resolved.unlink()`).
- `move_ids` were reassigned directly in SQLite (`UPDATE items SET album_id=?, album=?, albumartist=? WHERE id IN (...)`).
- Empty temporary album rows were deleted directly in SQLite (`DELETE FROM albums WHERE id=?`).

After Wave 20:
- Web Manager (`app.py`'s `_merge_imported_album_into_existing`) performs ZERO direct SQLite `DELETE` or `UPDATE` mutations, ZERO file unlinks, and ZERO filesystem moves/renames.
- Reconciliation is planned via `beets_client.plan_existing_album_reconcile` and applied via `beets_client.apply_existing_album_reconcile`.
- All duplicate file unlinks/quarantine, database reassignments, and empty album retirements are executed within the Beets engine with strict TOCTOU revalidation, resource locking, stat signature checking, and full rollback support (`beets_client.rollback_existing_album_reconcile`).

---

## 2. Mutation Boundary & Architecture

### Mutation Family: `existing_album_reconcile_v1`

- **Plan Endpoint**: `POST /albums/existing-reconcile/plan` -> `create_existing_album_reconcile_plan`
- **Apply Endpoint**: `POST /albums/existing-reconcile/apply` -> `execute_existing_album_reconcile_apply`
- **Rollback Endpoint**: `POST /albums/existing-reconcile/rollback` -> `rollback_existing_album_reconcile`

### Phase Invariants & Design

1. **Non-Mutating Plan Phase**:
   - Validates that `imported_album_id` and `existing_album_id` exist in Beets DB.
   - Enforces Release Group ID matching (`mb_releasegroupid`): if both albums have non-blank Release Group IDs and they differ, rejects with `reconcile_identity_mismatch`.
   - Validates survivor item requirement for `dup_item_ids`: re-verifies survivor tracks exist in `existing_album_id`, are present on disk, readable, and not scheduled for retirement.
   - Detects hardlinks (`dev`/`ino` match between duplicate and survivor): sets `is_hardlink=True` so duplicate file unlinks don't affect survivor files.
   - Checks root containment (`music_allowed_roots` and `source_folder`), symlink walks (`_path_has_symlink_under`), and captures stat signatures (`dev`, `ino`, `size`, `mtime_ns`).
   - Builds explicit diffs for `changes` preview and persists transaction state (`status: "Preview"`).

2. **All-Or-Nothing Apply Phase**:
   - Acquires per-operation lock `_get_apply_lock(operation_id)`.
   - Acquires deterministic resource locks: `album:<imported_album_id>`, `album:<existing_album_id>`, plus sorted `item:<id>` locks for all items.
   - Enforces idempotency (replaying completed transaction returns `already_completed: True`).
   - Precondition Revalidation: re-checks album existence, Release Group IDs, item DB rows, stat signatures, path containment, symlinks, and survivor presence on disk. If ANY check fails, aborts with zero mutations (`reconcile_toctou_mismatch` / `reconcile_survivor_missing`).
   - Stage 1 Filesystem: Duplicate files under `source_folder` (staging) or hardlinks are unlinked (`Path.unlink`). Duplicate files outside staging are safely moved to quarantine (`RECONCILE_QUARANTINE_DIR`).
   - Stage 2 Database: Deletes duplicate item rows, updates moved item `album_id`/`album`/`albumartist` attributes, and deletes temporary `albums` table row if empty—verifying exact expected rowcounts inside a single SQLite transaction.
   - Stage 3 Verification: Re-queries DB to verify duplicate items are gone, moved items belong to target album, and survivors are intact. Sets `status: "Completed"`.

3. **Rollback Phase**:
   - Re-inserts empty temporary `albums` row if retired.
   - Reassigns moved items back to `imported_album_id`.
   - Re-inserts duplicate item rows from snapshot data.
   - Restores quarantined duplicate files from `RECONCILE_QUARANTINE_DIR`.
   - Marks status `"Rolled Back"` or `"Partially Rolled Back"`.

---

## 3. Wave 17 / Wave 18 / Wave 19 / Wave 20 Co-existence Matrix

| Reconciliation Scenario in `_merge_imported_album_into_existing` | Handled By | Transaction Family | Key Safeguards |
|---|---|---|---|
| Superseded track replacement (`replace_rows`) | Wave 18 | `bulk_import_replacement_v1` | Explicit old -> new item mapping, Recording ID required |
| Music format replacement retry (`forced_replace_rows`) | Wave 17 | `track_replacement_v1` | Handled by `_music_format_remove_original_after_replacement`, not double-retired by merge |
| Recording repair / MB tag repair | Wave 19 | `album_mb_track_repair_v1` | Canonical Release Group ID check, tag write, TOCTOU baseline |
| Duplicate downloaded track removal (`dup_rows`) | Wave 20 | `existing_album_reconcile_v1` | Survivor item presence check, hardlink safety, quarantine/unlink |
| Missing track position reassignment (`move_ids`) | Wave 20 | `existing_album_reconcile_v1` | Release Group ID matching, stat signature TOCTOU |
| Empty temporary album retirement | Wave 20 | `existing_album_reconcile_v1` | Exact item rowcount check, DB rollback snapshot |

---

## 4. Repository-Wide Mutation Audit & ARCH-003 Closure Status

A comprehensive audit was performed across `app.py`, `backend/`, and route modules for remaining media/Beets mutations:

1. **Web Manager Direct Media/Beets Mutations**:
   - `_merge_imported_album_into_existing`: 100% migrated (Wave 18 + Wave 20). Zero direct SQL `DELETE`/`UPDATE` on items/albums, zero file unlinks.
   - `repair_album_mb_tracks`: 100% migrated (Wave 19).
   - `_import_and_tag_disk`: 100% delegates existing album merges to `_merge_imported_album_into_existing`.
   - Import review cleanups: 100% migrated (Wave 16).
   - Music format replacements: 100% migrated (Wave 17).

2. **Controlled Boundaries Established Across ARCH-003**:
   - `import_review_cleanup_v1` (Wave 16)
   - `track_replacement_v1` (Wave 17)
   - `bulk_import_replacement_v1` (Wave 18)
   - `album_mb_track_repair_v1` (Wave 19)
   - `existing_album_reconcile_v1` (Wave 20)

3. **Status Statement**:
   - All media and Beets database mutations inside `_merge_imported_album_into_existing` are now fully governed by engine-owned controlled mutation boundaries.
   - Full test suite of 2421 tests passed 100% across two consecutive full runs with zero failures.
   - Endpoint inventory verified with 0 NEED_REVIEW endpoints (`generate_endpoint_inventory.py --check`).

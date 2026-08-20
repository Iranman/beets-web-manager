# Wave 22: Album Maintenance, Deduplication, Track Retirement & Artwork Controlled Mutation Boundary Design

## Executive Summary

Wave 22 completes the migration of the uncontrolled destructive **Album Maintenance** cluster from `app.py` into engine-owned, typed transaction boundaries (`album_maintenance_v1` and `album_artwork_v1`).

Prior to Wave 22, operations such as whole-album track deduplication (`album_deduplicate`), selective track retirement (`_remove_album_track_items`), album cleanup issue resolution (`_album_cleanup_apply_issue`), filename template token cleanup (`_apply_filename_cleanup_candidate`), and artwork quarantine/restore (`_album_art_quarantine_current`, `_album_art_restore_quarantine`) performed direct, uncontrolled filesystem unlinks (`Path.unlink`), file renames (`Path.rename`, `shutil.move`), directory removals (`rmdir`), and un-isolated SQLite DML statements (`DELETE FROM items`, `DELETE FROM albums`, `UPDATE items SET path=?`) directly inside `app.py`.

With Wave 22:
1. **Engine Ownership**: All filesystem moves, quarantines, DB item/album deletions, and path updates for album maintenance and artwork management are owned by `backend/transaction_engine.py` under `album_maintenance_v1` and `album_artwork_v1`.
2. **Fail-Closed Boundary**: `app.py` delegates 100% of mutation operations through `BeetsClient`. If `BeetsClient` IPC fails or the engine is unreachable, Web Manager fails closed and returns structured HTTP 503 / 400 error responses without falling back to local mutations or in-process transaction engine execution.
3. **No Unlinking Recoverable Media**: Media files and artwork are quarantined safely into transaction-keyed directories under `/config/reconcile_quarantine/<operation_id>/` via `_safe_rename`, preserving full rollback capability (`rollback_album_maintenance`, `rollback_album_artwork`).
4. **Durable Step Execution & TOCTOU Protection**: Resource locks (`album:<id>`, `item:<id>`, `artist:<name>`) and stat signature validation (`dev`, `ino`, `size`, `mtime_ns`) prevent race conditions and concurrent mutation collisions.
5. **Exact Rowcount Validation**: Database deletions and album retirement rows are deleted with exact `rowcount` checks and re-verification of zero remaining items (`SELECT COUNT(*) FROM items WHERE album_id=?`).

---

## Domain & Scope Breakdown

### In Scope for Wave 22
- Whole-album deduplication (`album_deduplicate` at `@app.post("/api/albums/<int:aid>/deduplicate")`).
- Track retirement helpers and endpoints (`_remove_album_track_items`, `@app.post("/api/clean/album-tracks/remove")`, `@app.post("/api/clean/album-tracks/remove-batch")`).
- Album cleanup issue application (`_album_cleanup_apply_issue`, `@app.post("/api/clean/album-folders/apply")`).
- Filename cleanup & template token normalization (`_apply_filename_cleanup_candidate`, `@app.post("/api/clean/template-tokens/apply")`).
- Album artwork quarantine and restoration (`_album_art_quarantine_current`, `_album_art_restore_quarantine`).
- Generic rollback dispatcher integration (`api_transaction_rollback` in `app.py`).

### Classified & Excluded (Future Dedicated Waves)
- **Folder Placeholder Actions** (`apply_folder_placeholder_action_api`): Operates on non-album disk folders with zero DB items tracked (`_folder_cleanup_db_items(source) == []`). Classified for a future Folder Cleanup / Placeholder boundary wave.
- **Import Folder** (`import_folder_with_id`): Presumptively out of scope. High-risk import workflow classified for a future dedicated Import Folder boundary wave.
- **Playlist Helpers** (`_playlist_*`, `playlist_delete`): Presumptively out of scope. Classified for a future Playlist boundary wave.

---

## Transaction Family Specifications

### 1. `album_maintenance_v1`

**Plan (`create_album_maintenance_plan`)**:
- Non-mutating preview.
- Supports modes: `deduplicate`, `remove_tracks`, `cleanup_issue`, `filename_cleanup`.
- Validates path safety, root containment (`music_allowed_roots`), and symlink rejection (`_path_has_symlink_under`).
- Captures stat signatures (`dev`, `ino`, `size`, `mtime_ns`) and full-row snapshots of items and albums for rollback.
- Generates durable `Preview` transaction in `TransactionStore`.

**Apply (`execute_album_maintenance_apply`)**:
- Validates `operation_id` against `_TRANSACTION_ID_RE`.
- Acquires resource locks (`_lock_resources`).
- Revalidates preconditions (TOCTOU stat checks).
- **Stage 1 (Filesystem)**: Quarantines retired media files into `/config/reconcile_quarantine/<operation_id>/` using `_safe_rename`.
- **Stage 2 (Database)**: Executes item updates and deletes with exact rowcount verification. Deletes album row ONLY when remaining item count is proven 0.
- Sets transaction status to `Completed`.

**Rollback (`rollback_album_maintenance`)**:
- Restores quarantined media files back to original paths via `_safe_rename`.
- Restores deleted DB item and album rows from captured full-row snapshots.
- Updates status to `Rolled Back`.

---

### 2. `album_artwork_v1`

**Plan (`create_album_artwork_plan`)**:
- Captures artwork stat signatures, source/target paths, and DB `albums.artpath` pointers.

**Apply (`execute_album_artwork_apply`)**:
- Quarantines artwork files to `/config/reconcile_quarantine/<operation_id>/` using `_safe_rename`.
- Clears or updates `albums.artpath` pointer in SQLite DB.

**Rollback (`rollback_album_artwork`)**:
- Restores quarantined artwork files to original paths using `_safe_rename`.
- Restores previous `albums.artpath` DB pointer.

---

## Verification & Test Strategy

- **Focused Unit Suite**: `tests/test_sec002_wave22_album_maintenance.py` (5 tests covering Plan non-mutating preview, Plan -> Apply -> Rollback lifecycle for tracks and artwork, TOCTOU revalidation, and AST inspection of `app.py`).
- **Full Suite Verification**: 2,400+ unit tests passing with zero failures.
- **AST Security Barrier**: AST inspection ensures `app.py` target functions contain zero direct `unlink`, `rename`, `shutil.move`, `rmdir`, or raw SQL deletion calls.

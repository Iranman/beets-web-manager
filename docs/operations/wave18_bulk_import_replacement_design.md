# SEC-002 / ARCH-003 Wave 18: Bulk Import-Time Replacement Controlled Mutation Boundary

**Status note (Claude final technical review, PR #91):** this document originally described AGY's proposed design and made several claims well ahead of the actual implementation. Independent inspection found the implementation's only real production caller, `_merge_imported_album_into_existing`, had been reduced to a complete silent no-op by an indentation defect in the same diff (its entire body -- the DB fetch, the old/new matching loop, and every mutation -- had been nested as unreachable dead code inside a sibling helper function, after that helper's own `return` statement); the transaction never verified a replacement had actually been imported before retiring old state; "new item" discovery relied on excluding old item IDs from an `album_id` query rather than an explicit mapping; per-track identity evidence was never required; TOCTOU only checked inode/size; DB retirement never verified rowcount; concurrency only serialized the same `operation_id`; and rollback was gated on `status == "Completed"` alone, stranding any partially-mutated `Failed` transaction. This revision describes the corrected implementation as independently verified against the code, not as originally proposed.

## 1. Overview & Objectives

Wave 18 adds one new mutation family, `bulk_import_replacement_v1`, covering the one real, narrow case within `_merge_imported_album_into_existing` where importing a folder into an *existing* album determines that some of that album's current tracks should be retired in favor of newly-imported ones. It does **not** cover the whole surface the original design document implied -- see section 2 for exactly what is and isn't in scope.

### Architectural Invariants

1. **Model B, not Model A (import ownership)**: by the time `_merge_imported_album_into_existing` runs, Beets has *already* imported the replacement media as its own library item(s) with a real path under the music library root. This function's job is reconciling that already-imported result against a pre-existing album, not triggering an import. The engine therefore does **not** own or trigger import -- there is no `import_executor` callback crossing the engine IPC boundary (passing an arbitrary Python callback across a process boundary was never a sound engine API, and the original implementation's control-agent endpoint never actually supplied one anyway, meaning that code path was already dead in production). `create_bulk_import_replacement_plan` instead requires the caller to supply an explicit, already-imported `new_item_id` for every `old_item_id` it wants retired, and independently re-verifies both before ever creating a Plan.
2. **Explicit mapping, not "whatever else is in the album"**: the Plan payload's `mappings` field is a list of `{old_item_id, new_item_id, identity_source}` objects. There is no fallback that resolves "all of this album's current item IDs" from the database -- every old item retired must be individually named together with the specific new item that replaces it, and both are independently fetched and validated from the database before Plan can succeed.
3. **Per-mapping matching authority is required, not optional**: every mapping requires a non-AI-only `identity_source`; Release Group ID is cross-checked between the old and new item when either side has one recorded; unless the mapping's evidence was already independently verified upstream (`identity_source == "forced_replacement_verified"`), the new item must carry its own Recording ID.
4. **Non-destructive quarantine**: retired original files are moved (never deleted) to the server-derived quarantine root, using the transaction's own durable ID for both the quarantine path and the returned `operation_id`.
5. **All-or-nothing retirement**: every mapping in a transaction is independently re-verified (full TOCTOU on both the old and new item) *before* any old file is quarantined. If any one mapping fails re-verification, the entire Apply is refused with zero mutation -- no old item is retired unless every required replacement in that same transaction is confirmed.
6. **Durable per-item crash recovery**: quarantine progress is persisted after each old item, not only at the end; a retry after a crash resumes without re-quarantining an already-moved file.
7. **Deterministic, sorted resource locking**: `execute_bulk_import_replacement_apply` locks the existing album ID and every old item ID (sorted) before acquiring the per-operation lock, so two transactions touching the same item or album cannot race, while transactions touching disjoint albums/items proceed fully in parallel.
8. **Rollback recoverable for partial-mutation failures, not only `Completed`**: a `Failed` transaction that already durably quarantined some files (or partially retired DB rows) before failing is rollback-eligible; DB restoration is only ever attempted for an old item whose *file* restoration in the same rollback call actually succeeded, so a rollback can never leave a DB row pointing at a path that either has no file or was refused as already-occupied.

---

## 2. Corrected Production Caller Matrix

The original design document's caller matrix claimed three migrated callers. Independent review of the actual code found only one, and even that one required a scope correction:

| Production symbol | Uses `bulk_import_replacement_v1`? | Notes |
| :--- | :--- | :--- |
| `_merge_imported_album_into_existing`'s `replace_rows` branch (an existing album track that doesn't match the resolved MusicBrainz release target, and is superseded by a newly-imported track at the same disc/track position) | **Yes** | The only real migrated path. Builds an explicit `old_item_id -> new_item_id` mapping directly from the same matching loop that decides an existing row is being superseded (not reconstructed afterward from "whatever is left in the album"), with `identity_source="mb_tracklist_position_match"`. |
| `_merge_imported_album_into_existing`'s `forced_replace_rows` branch (`replace_existing_item_ids`, the automatic music-format retry pipeline's explicit override) | **No -- intentionally, to avoid a conflicting retirement** | `replace_existing_item_ids`'s only real production source is `_music_format_replacement_payload` (`_music_format_replace_rows`, the automatic background replacement job). That caller retires its one specific old item through its own, separately-reviewed engine transaction -- `_music_format_remove_original_after_replacement`, SEC-002 Wave 17's `track_replacement_v1` -- called independently *after* the import this function reconciles completes (see `start_music_format_replacement_retry`). At the point `_merge_imported_album_into_existing` runs, that Wave 17 retirement has not happened yet, which is exactly why `forced_replace_rows` still finds the old row present; also routing it through `bulk_import_replacement_v1` here would be a second, redundant, and potentially conflicting retirement of the same item. This branch's job remains exactly what it was before Wave 18: bookkeeping only (stop the duplicate-matching heuristic from reconsidering this old row; let the new row take its place via `move_ids`). The original design document's claim that this branch was migrated was incorrect and has been removed.
| `dup_rows` cleanup (a freshly-imported track that duplicates an existing, already-verified-good track) | **No -- pre-existing, out-of-family debt** | Deletes a redundant *downloaded* duplicate under the import source folder, not a library-owned file being retired. Unchanged from pre-Wave-18 behavior; remains local, same as `move_ids`' `UPDATE items SET album_id=...` bookkeeping. Neither was ever part of this mutation family's claimed scope.
| `_import_and_tag_disk` (general) | **No -- not itself a caller** | `_import_and_tag_disk` calls `_merge_imported_album_into_existing` when merging into an existing album; it performs no independent bulk-replacement mutation of its own.
| `_music_format_replacement_payload` / automatic retry pipeline | **No -- see `forced_replace_rows` above** | Its retirement path is Wave 17's `track_replacement_v1`, not this family. The original design document's claim that this routes through `bulk_import_replacement_v1` was incorrect.

**The critical regression fixed this pass**: independent of the scope questions above, `_merge_imported_album_into_existing`'s entire body -- the album/item DB fetch, the disc/track matching loop, and every branch's mutation (including the one real `bulk_import_replacement_v1` delegation) -- had been left as unreachable code nested inside a sibling helper function (`_row_matches_target`), after that helper's own `return` statement, by an indentation defect in the same diff. The function therefore silently did nothing at all on every call (fell out of its `try` block with no `return`, implicitly returning `None`) regardless of the transaction-safety questions above. This is restored; see `tests/test_sec002_wave18_bulk_import_replacement.py`'s `RealProductionPathTests` for a real end-to-end regression test that would have caught it.

---

## 3. Engine Transaction Flow (`bulk_import_replacement_v1`)

```
[_merge_imported_album_into_existing's replace_rows branch]
       │
       │ 1. beets_client.plan_bulk_import_replacement(mappings=[{old_item_id, new_item_id, identity_source}, ...])
       ▼
[Transaction Engine: create_bulk_import_replacement_plan]
       ├── Require explicit mappings (no "resolve everything in the album" fallback)
       ├── Reject duplicate/self-referential/ambiguous old_item_id <-> new_item_id pairs
       ├── Validate every old item: DB row exists, music-library-root containment,
       │   full symlink-component walk, stat capture
       ├── Validate every new item the SAME way (already-imported -- same root class,
       │   not a staging path)
       ├── Reject AI-only identity_source; require Recording ID unless
       │   identity_source carries independent upstream verification
       ├── Cross-check Release Group ID between old and new when known
       └── Create Transaction (mutation_family="bulk_import_replacement_v1", status="Preview")
       │
       │ 2. beets_client.apply_bulk_import_replacement(operation_id)
       ▼
[Transaction Engine: execute_bulk_import_replacement_apply]
       ├── Acquire sorted resource locks (album id + every old item id), then the per-operation lock
       ├── Idempotency check (if Completed -> return ok)
       ├── Re-verify EVERY mapping (old item TOCTOU + new item existence/path/TOCTOU) before
       │   any mutation -- all-or-nothing: one bad mapping blocks the entire Apply
       ├── Quarantine old files one at a time, sorted, durable per-item progress persisted
       │   after each move (crash-safe resume)
       ├── Re-read live DB rows for the exact expected id set immediately before deleting
       ├── One bulk DELETE, rowcount verified == expected count, post-delete re-read confirms
       │   every id is actually gone
       └── Only then mark status="Completed"
```

---

## 4. Idempotency, Concurrency & Crash Recovery

1. **Idempotency**: a repeated Apply for an already-`Completed` transaction returns the completed result without re-mutating.
2. **Concurrency**: `execute_bulk_import_replacement_apply` acquires sorted resource locks on `album:<id>` and every `item:<old_item_id>` before the per-operation lock. Two transactions racing on the same album or item serialize; transactions touching disjoint albums/items run fully in parallel (verified with real two-thread tests in both directions).
3. **Crash recovery**: `apply_steps` progress (`mappings_reverified`, `old_items_quarantined`, `old_db_rows_retired`) and the accumulating `quarantined_files` list are persisted to the transaction's top-level metadata after every meaningful stage -- including on a *failure* return mid-loop, not only on success -- so a retry after a crash (or a rollback of a partially-mutated `Failed` transaction) can see exactly what already happened.

---

## 5. Rollback

`rollback_bulk_import_replacement` is legal for a `Completed` transaction, or a `Failed` transaction that durably mutated something before failing (quarantined at least one old file). It is refused for a `Failed` transaction that never reached any mutation (nothing to roll back) and for any transaction of a different `mutation_family`.

Each quarantined file is restored via a holding-location-first move (Wave 17's ordering: nothing at the quarantine slot is destroyed before the restore target is confirmed vacant and the move to it has actually succeeded), and refuses to overwrite a since-reoccupied original path. **DB row restoration is per-item conditional on that same item's file restoration having just succeeded in the same call** -- restoring the DB half independently would risk pointing a live item row at a path that either has no file yet or was refused as occupied by something unrelated. The final status is `Rolled Back` only if every quarantined file and every retired DB row were fully restored; otherwise `Partially Rolled Back` (something restored, something not) or `Failed` (nothing restored), never overclaiming.

This transaction never touches the newly-imported replacement items at all -- it does not own their import (see section 1), so rollback only ever restores the OLD items it itself retired.

---

## 6. What remains open (ARCH-003 debt after this wave)

- `forced_replace_rows`/`replace_existing_item_ids` retirement remains Wave 17's `track_replacement_v1`, not this family -- correctly so (see section 2).
- `dup_rows` (redundant downloaded-duplicate cleanup) and `move_ids` (`UPDATE items SET album_id=...` bookkeeping for newly-imported rows) remain local, pre-existing, unmigrated debt, unchanged by this wave.
- `repair_album_mb_tracks` remains separate, unmigrated debt (unchanged since Wave 17).
- No engine-side import capability was added or is needed; this transaction only ever reconciles items Beets has already imported.

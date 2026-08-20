# SEC-002 / ARCH-003 Wave 19: MusicBrainz Album Track Repair Controlled Mutation Boundary

## 1. Overview & Objectives

Wave 19 migrates the MusicBrainz album track repair workflow behind an engine-owned
controlled mutation boundary (`album_mb_track_repair_v1`). This revision documents the
implementation as corrected during final review, not the original Wave 19 submission --
that submission had a false-success tag-write fallback and several other defects listed
in section 8 below.

### Architectural Invariants

1. **Web Manager is UI / Orchestration Only.** `app.py` contains zero direct file
   unlinks, renames, moves, SQLite `UPDATE`/`INSERT`/`DELETE` queries, or local
   `_beet_run` subprocess calls for this family. Enforced by
   `tests/test_sec002_wave19_mb_track_repair.py::WebManagerMutationProhibitionTests`
   against **both** production callers (see section 6).
2. **Beets Engine Owns All DB Mutations & File Tag Writes.**
   `backend/transaction_engine.py` owns every Beets DB track/album update and every
   audio metadata tag write for this family, via `mediafile.MediaFile` -- the same
   tag-abstraction library Beets itself uses (`beets.mediafile` is a deprecated shim
   over the same code). There is no secondary/parallel Mutagen tagging layer.
3. **Preserved Identity Hierarchy.**
   - Canonical album identity: MusicBrainz Release Group ID (`mb_releasegroupid`).
   - Edition evidence: Release ID (`mb_albumid`).
   - Track identity: Recording ID (`mb_trackid`).
   - RGID is never mutated by this family, and the Plan diff never claims an RGID
     change that Apply does not perform (see section 4).
4. **Stale Plan & TOCTOU Protection.** Precondition revalidation verifies device,
   inode, file size, mtime (`mtime_ns`), allowed-roots containment, and a full
   parent-symlink walk, for every row Apply will touch -- immediately before the
   first mutation, and again (state-vs-expected-post-Apply) before Rollback.
5. **Deterministic Concurrency Control.** Apply and Rollback both acquire
   `album:<id>` and `item:<id>` (sorted) reentrant resource locks, plus the
   per-operation-id Apply lock -- so a rollback of one transaction cannot race a
   concurrent Apply of a different transaction touching the same album/items.
6. **Reversibility & Recovery.** Rollback restores prior DB values from an explicit
   `album_before` / per-row `before` snapshot, and prior audio tags from a snapshot
   read directly off disk at Plan time (`file_tags`) -- never inferred from the Beets
   DB row, which is not guaranteed to match what was actually on the file.

---

## 2. Controlled Mutation Family: `album_mb_track_repair_v1`

### Endpoints
- `POST /albums/mb-track-repair/plan` -- generates a non-mutating Preview transaction.
- `POST /albums/mb-track-repair/apply` -- re-validates TOCTOU invariants, applies
  SQLite DB updates, writes audio tags (unless `write_tags: false`), and performs
  post-write verification of every changed field. Idempotent: Apply on an
  already-`Completed` transaction returns `already_completed: true` without
  re-mutating.
- `POST /albums/mb-track-repair/rollback` -- restores prior DB track/album attributes
  and prior on-disk audio tags.

### `write_tags`
`apply` accepts `write_tags` (default `true`). `write_tags: false` performs the DB
update only and skips the file-tag write stage entirely -- this restores the
`_repair_album_mbid_sticking_once(write_tags=False)` contract from before Wave 19,
which the original Wave 19 Apply silently ignored (every caller happens to pass
`write_tags=True` today, so this was latent, not an observed behavior change; it is
threaded through end-to-end regardless: `beets_client.apply_album_mb_track_repair(...,
write_tags=...)` -> control-agent `/albums/mb-track-repair/apply` ->
`execute_album_mb_track_repair_apply(..., write_tags=...)`).

---

## 3. Matching Authority

`backend/mb_alignment.best_album_track_match` aligns each local item to a tracklist
position using, in order:
1. **Exact Recording ID match** -- the item's own `mb_trackid` already equals some
   track's `mb_trackid` in the selected release. Score is forced to >= 0.98.
2. **Fuzzy fallback** -- normalized title similarity (`difflib.SequenceMatcher`) plus
   small position/duration bonuses, gated at `threshold=0.72` (fresh match) /
   `repair_threshold=0.65` (only reached via the exact-ID path, so effectively unused
   for fuzzy-only matches).

`create_album_mb_track_repair_plan` then applies an explicit **conflict policy** on
top of that alignment, per row:

| Current `mb_trackid` | Target differs? | Outcome |
|---|---|---|
| Blank | yes | **Safe to fill.** Added to `tracks_to_repair`, auto-applied. |
| Blank | no (already correct) | No-op, skipped. |
| Nonblank, matches target | -- | No-op, skipped. |
| **Nonblank, conflicts with target** | yes | **Never auto-applied.** Added to `conflicts_requiring_review`, surfaced in the Plan's `changes` (`status: "requires_review"`), left untouched by Apply. |

Rationale: an existing nonblank Recording ID is itself MusicBrainz-sourced identity
evidence. `best_album_track_match`'s fuzzy path (title/position/duration scoring,
0.72 threshold) is not strong enough evidence to overwrite it -- per project policy
("destructive actions require stronger evidence than suggestions"). The original
Wave 19 submission did not draw this distinction: `test_matching_same_title_wrong_
recording_id` asserted that a conflicting nonblank Recording ID got silently
overwritten by a same-title fuzzy match. That assertion was inverted during final
review (`test_matching_conflicting_recording_id_requires_review`) to lock in the
safe behavior instead.

**AcoustID.** No AcoustID/fingerprint integration is wired into this family's
conflict path. `backend/track_align.py::resolve_unmatched_via_acoustid` exists and is
used elsewhere (missing-track resolution) but has a different contract (promoting a
"missing" row from an extra file) and was not adapted here -- conflicting nonblank
Recording IDs are deferred to manual review rather than auto-resolved via a
hastily-wired second fingerprint path. This is tracked as remaining debt (section 8).

**AI** plays no role in this family at all -- matching is deterministic MusicBrainz
tracklist alignment only.

### Release Group handling

- If the selected release's Release Group (`candidate_rg`) conflicts with the album's
  established `mb_releasegroupid`, the repair refuses (`repair_identity_mismatch`).
- If the album's `mb_releasegroupid` is **blank** and the selected release resolves to
  a nonblank candidate RG, the repair now also refuses (`repair_rg_not_established`)
  rather than silently proceeding with an unestablished album identity (Option A from
  the review; Option B -- explicitly establishing the candidate RG as part of the
  transaction, with its own Plan/DB/rollback handling -- is deferred, tracked in
  section 8).
- RGID is never written by Apply. The Plan's `after_state.mb_releasegroupid` is always
  shown equal to the current (unchanged) value -- the original implementation showed
  `candidate_rg or album_rg` in `after_state` while Apply never actually updated the
  column, an inconsistent-diff defect fixed here.

---

## 4. Plan Contract

Plan (`create_album_mb_track_repair_plan`) captures, per candidate row:

- **DB before-state**: the Beets `items`/`albums` row values at Plan time.
- **File-tag before-state** (`before.file_tags`): read directly from disk via
  `_read_file_audio_tags` at Plan time, **without mutation**. If a file cannot be read
  reliably (unsupported format, corrupt, missing, IO error), that row is excluded from
  automatic repair entirely (`unreadable_items`) rather than repaired without a
  trustworthy rollback snapshot.
- **After-state**: the proposed DB values. Never includes a field Apply does not
  actually write.

Three explicit categories are captured and shown, not just recording-ID repairs:

- `tracks_to_repair` -- blank-Recording-ID rows, safe to auto-apply.
- `conflicts_requiring_review` -- nonblank-conflicting rows, never auto-applied.
- `album_release_stamp_rows` -- every OTHER item in the album (not already covered by
  a `tracks_to_repair` row) whose `mb_albumid` does not match the target release. This
  closes the original "release-only stamping mutates every item in the album but Plan
  only shows the recording-ID subset" gap -- every row Apply can touch is now
  represented in the Plan and in `changes`.

`album_before` captures the album row's own original `mb_albumid`/`mb_releasegroupid`
explicitly, so rollback does not have to (incorrectly) infer the album's original
value from `tracks_to_repair[0]` -- which breaks entirely for release-only stamping
(zero repaired tracks) and is wrong whenever the album's own original value differs
from the first repaired track's original value.

Plan performs zero mutation (`test_plan_non_mutating` asserts DB rows, file mtimes,
and file tags are byte-identical before/after Plan).

---

## 5. Apply Ordering

1. **TOCTOU verification** (album existence/RG stability/own `mb_albumid`, then every
   `tracks_to_repair` and `album_release_stamp_rows` row: existence, album membership,
   path, expected Recording ID / Release ID, roots containment, symlink walk, and
   exact stat match) -- all-or-nothing, before any mutation.
2. **`mutation_started` persisted** (durable intent marker; NOT the truthful
   "mutated" flag -- see section 8's crash-recovery caveat).
3. **Stage 1: DB updates**, single connection/single commit. Every `UPDATE` checks
   `cursor.rowcount == 1`; any mismatch rolls back the in-flight SQL transaction and
   fails the whole Apply before committing anything. On successful commit,
   `db_mutated: true` and `mutated: true` are persisted immediately -- this is the
   first point at which the transaction truthfully becomes rollback-eligible.
4. **Stage 2: audio tag writes** (skipped entirely if `write_tags=false`), for both
   `tracks_to_repair` (all fields) and `album_release_stamp_rows` (`mb_albumid` only).
   Each write is independently read back and field-compared before being reported as
   successful (see section 7). On success, `tags_mutated: true` is persisted.
5. **Stage 3: post-write DB verification** -- every field the transaction claims to
   have changed (`mb_trackid`, `mb_albumid`, `title`, `track`, `disc` for repair rows;
   `mb_albumid` for stamp rows and the album row), not just `mb_trackid`.
6. Transaction marked `Completed`.

If Stage 2 fails partway through, the DB is already committed (Stage 1) and the
transaction is marked `Failed` with `partial_mutation: true`; Rollback is available
because `db_mutated` is already `true`.

---

## 6. Production Callers

Both discovered callers use the identical Plan -> Apply(write_tags) contract via
`BeetsClient` (never local mutation):

- `repair_album_mb_tracks` (`app.py`, `POST /api/albums/<id>/repair-mb-tracks`) --
  **manual**, user-triggered by the explicit "Repair MusicBrainz Tracks" UI action.
  Plan -> immediate Apply is acceptable here because the button click is the
  authorization event. Always `write_tags=true` (unchanged).
- `_repair_album_mbid_sticking_once` (`app.py`) -- **automatic**, called from several
  import/cleanup/relink code paths, always with `write_tags=True` today. Automatic
  repair is safety-bounded by the conflict policy in section 3: it can only ever fill
  blank Recording IDs or stamp release IDs -- it can never silently overwrite an
  existing nonblank Recording ID, which is the property that makes running it without
  a human review step defensible. No AI involvement.

Both callers previously used `updated == 0` from Plan as a proxy for "nothing to
apply", which silently dropped release-only-stamping transactions (Plan created a
real, unapplied `Preview` transaction whenever `tracks_to_repair` was empty but
`release_stamping_needed` was true, and the callers checked for a
`release_rows_updated` field the engine never returned). Both now check
`updated > 0 or release_stamping_needed or release_stamp_rows > 0`.

The rollback dispatcher (`api_transaction_rollback` in `app.py`) routes
`album_mb_track_repair_v1` only to `rollback_album_mb_track_repair`, with no fallback
for unrecognized families (verified by existing Wave 17/18 cross-family tests plus
this family's own `test_apply_wrong_mutation_family` and rollback family-mismatch
check).

---

## 7. File Tag Mutation & Verification

`_write_file_audio_tags` uses `mediafile.MediaFile` as the sole writer. A write is
reported successful **only if**, after `.save()`, an independent
`_read_file_audio_tags` call on the same path returns the exact requested values
(case-insensitive/lowercased for the two ID fields, integer-compared for
`track`/`disc`). There is no fallback path: the original `Path.touch()` fallback --
which only changed the file's mtime, not a metadata write -- has been removed
entirely. A file that cannot be parsed (including the test suite's old fake
`b"FLAC_DATA_1"` fixtures) now fails closed with a reason code
(`unreadable`/`not_found`/`io_error`/`unknown_error`/`readback_mismatch`) instead of
being marked touched-and-successful.

`_read_file_audio_tags` returns `{"ok", "tags", "reason"}` so callers can distinguish
"read succeeded, tags are genuinely blank" from every failure mode -- this
distinction is what lets Plan safely decide whether a row's rollback state is
trustworthy enough to include it in automatic repair at all.

---

## 8. Database Safety

- Every `UPDATE` (`items` and `albums`) is followed by an exact `cursor.rowcount == 1`
  check; a mismatch rolls back the SQL transaction and fails Apply before commit.
- Apply's item-level DB update targets an explicit set of item ids built from
  `tracks_to_repair` + `album_release_stamp_rows` (i.e. exactly the rows Plan
  captured and showed) rather than a blanket `UPDATE items SET mb_albumid=? WHERE
  album_id=?` -- the latter (the original implementation) could silently restamp
  items that were never captured in Plan/rollback state, including items added to the
  album after Plan ran.
- Post-write verification (Stage 3) re-reads and compares every field the
  transaction claims to have changed, not just `mb_trackid`.

---

## 9. Crash Recovery (partial, documented as remaining debt)

`mutation_started` is persisted before Stage 1 opens a connection; `mutated` /
`db_mutated` are only set `true` immediately after a real commit succeeds, so a crash
before that point no longer leaves the transaction falsely claiming to be mutated (the
original Wave 19 defect). What is **not** yet implemented: fine-grained resume
semantics that distinguish "DB committed, process died before `db_mutated` was
persisted" from a genuinely stale Plan on retry. A retry after a crash in that narrow
window will currently re-run TOCTOU (which will now see the already-mutated DB values
and fail closed as a mismatch) rather than detect and resume from the completed DB
stage. Tracked as remaining ARCH-003 debt.

---

## 10. Rollback

- Resource locking: acquires the same `album:<id>` / `item:<id>` locks (deterministic
  sorted order) as Apply, so a rollback cannot race a concurrent Apply/rollback of a
  different transaction on the same album/items.
- Precondition validation: before restoring anything, current DB state for every row
  must still match the transaction's own recorded `after` state; if a later,
  independent edit changed it, rollback refuses (`repair_rollback_stale`) rather than
  clobbering the newer change.
- DB restore: explicit `album_before` restores the album row's own original value
  (not inferred from `tracks_to_repair[0]`); every `tracks_to_repair` and
  `album_release_stamp_rows` item is restored individually with rowcount
  verification.
- File tag restore: uses the `file_tags` snapshot captured directly from disk at Plan
  time -- the item's actual prior on-disk value, which can legitimately differ from
  the Beets DB row's before-value.
- Repeated rollback: a transaction already `Rolled Back` refuses a second rollback
  (`repair_already_rolled_back`) rather than re-running file/DB writes.
- Path safety: reuses the same roots-containment + full parent-symlink-walk check as
  Apply, re-evaluated immediately before each file write.

---

## 11. Known Remaining Debt (ARCH-003 stays In Progress)

- **AcoustID-based conflict resolution** for `conflicts_requiring_review` is not
  wired up (section 3) -- conflicts are deferred to manual review rather than
  auto-resolved with fingerprint evidence.
- **Release Group establishment (Option B)** for a blank-RGID album is not
  implemented -- repair refuses outright (Option A) rather than offering an explicit,
  reviewable RG-establishment transaction.
- **Fine-grained crash-resume** (section 9) does not yet distinguish "DB committed,
  durable marker not yet persisted" from a genuinely stale Plan.
- **DB rowcount race test coverage**: the `cursor.rowcount == 1` safety check exists
  and is exercised indirectly by every TOCTOU test, but there is no dedicated test
  that forces a real mid-Apply rowcount mismatch (would require white-box mocking of
  `sqlite3.Cursor`).
- **UI**: no frontend changes shipped with this family. The existing "Repair
  MusicBrainz Tracks" action does not show a distinct preview step for
  `conflicts_requiring_review` rows (which are never auto-applied, but are also not
  yet surfaced to the operator as "needs your review" in the UI). Tracked in
  `docs/TECHNICAL_DEBT.md`.
- Broader ARCH-003 audit: `_merge_imported_album_into_existing`'s `dup_rows` /
  `move_ids` paths remain outside a controlled engine boundary (carried over from
  before Wave 19; not touched by this wave).

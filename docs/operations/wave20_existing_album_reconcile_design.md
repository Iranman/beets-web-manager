# SEC-002 / ARCH-003 Wave 20: Existing-Album Duplicate & Move Reconciliation Controlled Mutation Boundary

## 1. Overview

Wave 20 migrates `_merge_imported_album_into_existing`'s `dup_rows`/`move_ids`
bookkeeping (reconciling a freshly-imported "temp" album against a pre-existing
library album) behind an engine-owned controlled mutation boundary
(`existing_album_reconcile_v1`). This revision documents the implementation as
corrected during final review -- the original submission had several critical
defects listed in section 8.

## 2. Corrected Production Semantics

`_merge_imported_album_into_existing` builds `existing_by_key` (existing library
rows keyed by `(disc, track)`) and walks the freshly-imported album's rows:

- If an imported row's position already has an existing row that
  **independently passed `_row_matches_target`** (fingerprint status, exact
  Recording ID, or title-score against the resolved MusicBrainz tracklist
  target for that position) -- the imported row is classified `dup_rows`
  (redundant; the existing row already correctly represents this position) and
  the matching existing row(s) become candidate survivors.
- Otherwise the position is genuinely missing from the existing album, so the
  imported row is classified `move_ids` (reassign it onto the existing album).
- `forced_replace_ids` (explicit caller override, used only by the automatic
  music-format retry pipeline) bypasses both and is handled entirely by Wave
  17's `track_replacement_v1` elsewhere -- untouched by this wave.

**Important correction**: `_row_matches_target` only validates the EXISTING
row against the official MusicBrainz tracklist position -- it never compares
the imported row's own identity against the existing row's. Two different
Recording IDs could previously end up bound as duplicate/survivor purely by
disc/track coincidence. The engine (`create_existing_album_reconcile_plan`)
does not trust this app.py-side classification as authority; it
**independently** re-establishes identity for every duplicate/survivor pair
(section 4).

**Empty album retirement**: an imported album retires only when the exact set
of item IDs affected by this transaction (moved + deleted) equals the exact
set of item IDs currently in that album -- not a count comparison, which could
be satisfied by ID-list duplication/overlap (Plan now rejects that outright,
see section 4).

## 3. Identity Authority

- **Recording ID (`mb_trackid`)** is the sole automatic-retirement authority.
  Exact, case-normalized equality between the duplicate's and a candidate
  survivor's Recording ID is required for that candidate to be accepted.
- **Different known Recording IDs fail closed**: if every candidate survivor
  for a duplicate has a Recording ID that conflicts with the duplicate's own,
  the duplicate is excluded from automatic reconciliation and surfaced in
  `dup_requires_review` with `reason: "identity_conflict"`.
- **Blank Recording ID on either side is not sufficient evidence on its own**
  (neither to authorize nor to refuse) -- such duplicates are also surfaced in
  `dup_requires_review` (`reason: "survivor_unverified"` or
  `"no_candidate_survivor"`) rather than guessed either way.
- **Same disc/track position is a hint only**, never authority. It is used to
  generate the candidate-survivor list (alongside any caller-supplied
  `survivor_item_ids`, also just a hint), but the identity gate above is what
  actually authorizes retirement.
- **Same physical path is definitive** (stronger than any Recording-ID
  evidence could be): if the duplicate row and a candidate share the literal
  same on-disk path, they are structurally the same file regardless of tags --
  classified `db_only`, and the file is never touched physically (see section
  6).
- **RGID** stays canonical album-family identity; a caller-supplied
  reconciliation between two albums with different established Release Group
  IDs is refused outright (`reconcile_identity_mismatch`), re-verified at
  Apply (section 9), and never mutated by this family.
- **AcoustID / fingerprinting**: `_row_matches_target`'s pre-existing
  fingerprint check (`_album_track_fingerprint_check`) remains part of how
  app.py decides `dup_rows` vs `move_ids` in the first place, but the engine's
  own duplicate/survivor identity gate (section 4) does not re-run
  fingerprinting -- it relies on Recording ID exactness or same-path identity.
  A conflicting-Recording-ID duplicate is not auto-resolved via a second
  fingerprint check; it is deferred to manual review (documented remaining
  debt, section 11).
- **AI** plays no role in this family. Matching is deterministic MusicBrainz
  tracklist alignment (app.py) plus deterministic Recording-ID/path identity
  (engine) only.

## 4. Duplicate/Survivor Model

Plan (`create_existing_album_reconcile_plan`) treats `dup_details[].
survivor_item_ids` and the disc/track fallback as **hints only**. For every
candidate it independently establishes:

1. Candidate exists, belongs to `existing_album_id`.
2. Candidate's path is under the music library root, contains no symlink
   component.
3. Candidate's file physically exists **and is verifiably readable** via
   `_read_file_audio_tags` (not merely `Path.exists()`) -- unless the
   candidate is the same path as the duplicate (see below), in which case
   physical/readability checks are redundant (the duplicate row's own
   existence already proves it).
4. **Identity**: same path -> automatically valid (`db_only`). Otherwise,
   exact non-blank Recording ID match -> valid (`identity_exact_match: true`).
   Conflicting non-blank Recording IDs -> rejected, contributes to
   `identity_conflict`. Blank on either side -> rejected, no automatic
   conclusion either way.

Every accepted duplicate/survivor pair is stored with an explicit
`identity_evidence` block (`duplicate_mb_trackid`, `survivor_mb_trackids`,
`exact_match`, `source`) in both the transaction payload and the Plan's
`changes` -- never a loose survivor list without stated evidence.

## 5. Path Authorization

**Critical correction (was a CodeQL-confirmed `py/path-injection` finding,
4 alerts, at the original submission's head)**: the original implementation
appended the request body's `source_folder` directly onto the server's own
`music_allowed_roots` list before using it for containment checks -- letting
an authenticated-but-untrusted request expand what filesystem locations the
operation was authorized to touch.

Corrected model, enforced entirely server-side:

- **`library_roots`**: `MUSIC_ROOT` (server env var) -- survivors must always
  be here.
- **`staging_roots`**: the same server-configured candidate roots
  `track_replacement_v1`/`bulk_import_replacement_v1` already use
  (`DOWNLOADS_ROOT`, `PLAYLIST_DOWNLOAD_ROOT`, `TORRENT_SOURCE_ROOTS`,
  the system temp dir) -- never client-supplied.
- **`imported_item_roots`** (`library_roots + staging_roots`): move/duplicate
  items (already-imported Beets rows under the imported album) may live under
  either.
- **`source_folder`** (request body) is only ever used for informational
  classification, and only once it independently resolves under one of the
  server's own `staging_roots` -- an unvalidated `source_folder` is silently
  ignored, never trusted, and never added to any containment check.
- Symlink defenses (`_path_has_symlink_under`, full parent-component walk) are
  applied at Plan for every role, and re-applied at Apply immediately before
  each mutation (not only at Plan time).

## 6. Plan Contract

Captured per row (via `SELECT *` / `_row_to_dict`, not a hand-picked field
subset -- see section 10):

- **Move items**: full DB row (`before.full_row` conceptually available via
  the same query, though only path/album/albumartist/stat/identity fields are
  restored -- moves never delete a row), plus identity fields
  (`mb_trackid`/`mb_albumid`/`mb_releasegroupid`/`disc`/`track`) captured
  solely for Apply-time TOCTOU comparison.
- **Duplicate items**: full row snapshot (`before.full_row`, every live
  column) for true full-row rollback restoration, plus stat signature.
- **Survivors**: id, path, title, Recording ID, Release ID/RGID, full stat
  signature, `same_path_as_duplicate`, `is_hardlink`, `identity_exact_match`.
- **Album state**: both albums' RGID captured explicitly
  (`existing_album_rg_before`/`imported_album_rg_before`); the imported
  album's full row captured (`imported_album_before`) whenever the exact
  affected-ID-set computation determines it will retire.

Plan performs zero mutation (`test_plan_non_mutating`: DB rows and file
mtimes byte-identical before/after Plan).

## 7. Apply Ordering

1. Crash-resume check: if this transaction's `db_reassigned` stage is already
   durably marked `Completed`, skip straight to Stage 3 verification
   (idempotent). Items already recorded in `quarantined_files` are excluded
   from the TOCTOU/quarantine loop on retry.
2. TOCTOU (all rows, all-or-nothing, before any mutation): album existence +
   RGID compatibility (both against Plan-time values AND against each other),
   every move/duplicate item's full identity + path role + symlink + stat,
   every survivor's identity + path role + symlink + stat + readability.
3. `mutation_started` persisted (durable intent marker).
4. Stage 1 -- duplicate file quarantine: `db_only` duplicates are never
   touched physically; every other duplicate is quarantined via the hardened
   `_safe_rename` helper (never raw `shutil.move`), with the quarantine
   directory created only after all preconditions pass, and each successful
   quarantine persisted to durable metadata immediately (not batched at the
   end of the loop).
5. Stage 2 -- DB operations (single connection/commit): delete duplicate rows
   (rowcount-verified), reassign move rows (rowcount-verified), retire the
   imported album if empty (rowcount-verified). `db_mutated`/`mutated` set
   true only after commit succeeds.
6. Stage 3 -- verification: DB state for every row/album, plus filesystem
   final state (every quarantined original path gone, quarantine copy
   present; every survivor still present).
7. `Completed`.

## 8. Corrected Defects (original submission)

The original Wave 20 submission ("implemented, fully tested, and ready for
technical review") did not hold up under independent inspection. Concrete
defects found and fixed:

- **CRITICAL (CodeQL-confirmed, 4 `py/path-injection` alerts)**: client
  `source_folder` silently expanded filesystem authorization (section 5).
- **CRITICAL**: duplicate/survivor identity was never actually enforced by
  the engine -- a caller-supplied `survivor_item_ids` (or a bare disc/track
  position match) was sufficient, with no Recording ID comparison at all.
  Two different known Recording IDs could be silently bound as duplicate and
  survivor.
- **CRITICAL**: `is_hardlink` detection (`same dev+inode`) did not distinguish
  a real hardlink from the SAME PATH referenced by two DB rows -- the latter
  case would unlink the survivor's only copy. Now classified separately
  (`db_only`, never touched physically).
- **CRITICAL**: duplicate files under `source_folder` (or classified as
  hardlinks) were permanently `unlink()`d, yet the transaction advertised
  `rollback_available: True` and the design doc claimed unlinked files could
  be restored -- rollback never actually restored `unlinked_files` at all.
  Corrected by eliminating permanent unlink entirely: every recoverable
  duplicate is quarantined (section 7), so `rollback_available` is never a
  lie.
- Raw `shutil.move` (both Apply's quarantine step and Rollback's restore
  step) replaced with the hardened `_safe_rename` helper Waves 15/17 already
  established for this exact bug class.
- Quarantine directory created before preconditions passed -- moved to after.
- `mutated: True` set before Stage 1 began -- now only after each stage's
  first real mutation actually commits (`mutation_started` for intent,
  `filesystem_mutated`/`db_mutated`/`mutated` for truthful post-mutation
  state).
- Recovery metadata (`quarantined_files`) persisted only after the whole
  duplicate loop completed -- now persisted after each individual success.
- Empty-album retirement's `DELETE FROM albums` had no rowcount check --
  added.
- `will_retire_album` used a count heuristic vulnerable to ID-list
  duplication/overlap -- replaced with exact-set comparison, and the payload
  canonicalization step now rejects duplicate/overlapping IDs outright
  (`reconcile_payload_overlap`).
- Move items' Plan `before` state captured only album_id/album/albumartist/
  path/stat -- Apply could not detect a changed Recording ID/Release ID/RGID
  between Plan and Apply. Now captured and TOCTOU-checked.
- Survivor snapshot was too thin (id/path/title/mb_trackid, no stat
  signature) and Apply only re-checked `Path.exists()` -- now full stat
  signature + readability re-verified at Apply.
- Rollback: no resource locking (now locks album + item + survivor IDs,
  same deterministic order as Apply); no current-state validation before
  overwrite (now refuses `reconcile_rollback_stale` if a later edit changed
  what this transaction actually produced); DB row restored before file
  restored -- reversed (file restored and verified first; a DB row is only
  reinserted for a duplicate once its file restoration succeeded, or if it
  never needed one, i.e. `db_only`); restored only a hand-picked field subset
  -- now restores the full captured row; reported plain `ok: True` for
  `Partially Rolled Back` -- now `ok: False` with `partial_mutation: true`;
  repeated rollback was blocked only incidentally -- now an explicit,
  truthful `reconcile_already_rolled_back` refusal; no destination-collision
  check before restoring a file -- now refused explicitly.
- `_merge_imported_album_into_existing`'s return value claimed
  `existing_album_id` (success) even when the delegated engine Plan/Apply
  call actually failed and was only logged as a warning -- now tracks
  per-delegation success and returns `imported_album_id` (nothing changed)
  when what was attempted did not actually succeed.
- Raw exception text and internal DB paths (`f"Beets database file not
  found at {lib_db}"`, `f"...: {ex}"`) leaked into API-facing errors --
  sanitized to stable public messages with `LOG.error` carrying detail
  server-side.

## 9. Database Safety

Every `UPDATE`/`DELETE` (items and albums) is followed by an exact rowcount
check within the same connection/commit; a mismatch rolls back the SQL
transaction before anything commits. Post-write verification (Stage 3)
re-reads DB state for every affected row/album, plus filesystem final state.
Raw SQLite remains the mutation mechanism (consistent with Waves 15-19's
established pattern in this file, not a new decision unique to this wave);
Beets-native Item/Library APIs were not adopted here for the same reasons
documented in prior waves' entries -- tracked as shared, pre-existing
ARCH-003/architecture debt, not reopened by this wave.

## 10. Filesystem Safety

- **Same path**: never touched physically (`db_only`).
- **Hardlink** (different path, same device+inode): quarantined via rename
  like any other recoverable duplicate -- a rename only changes the
  pathname, so the survivor's hardlink to the same inode is unaffected.
- **Distinct file**: quarantined via `_safe_rename` (explicit rename with
  controlled EXDEV fallback, no move-inside-existing-directory ambiguity, no
  destination overwrite).
- **Quarantine destination collision**: refused explicitly before any move.
- **Rollback destination collision**: refused explicitly (a file that now
  occupies the original path is never overwritten).

## 11. Crash Recovery

`mutation_started` is durable before Stage 1 opens. Retry behavior:

- If `db_reassigned` is already durably `Completed`, Apply skips straight to
  Stage 3 (idempotent verification), never re-attempting DB/filesystem
  mutation.
- If some but not all duplicates were quarantined before a crash, retry skips
  TOCTOU/re-quarantine for items already present in the durable
  `quarantined_files` list and only processes the remainder.
- **Not fully solved** (documented, not claimed complete): a crash between DB
  commit and the `db_reassigned` durable marker being persisted is not yet
  distinguished from "the DB was never touched" with full precision beyond
  what the `db_reassigned` check above provides; this mirrors the same
  documented limitation from Wave 19's crash-recovery section.

## 12. Rollback

See section 8 for the itemized corrections. Summary of the current model:
resource-locked (album + item + survivor IDs); validates current DB state
still matches this transaction's own recorded post-Apply state before
touching anything; restores files first (with root/symlink/collision checks
immediately before each restore) and only then restores DB rows, and only
for items whose file restoration succeeded (or that never needed one);
restores the full captured row for duplicates, not a hand-picked subset;
reports partial recovery truthfully (`ok: False`, `partial_mutation: true`);
refuses a second rollback of an already-rolled-back transaction.

## 13. Architecture

`app.py`'s `_merge_imported_album_into_existing` contains zero direct file
unlinks/renames/moves or SQL `UPDATE`/`DELETE` for this family (verified by
`WebManagerMutationProhibitionTests`); all mutation for `dup_rows`/`move_ids`
is delegated to the engine via `BeetsClient`. One dead nested helper,
`_delete_row_file`, remains defined but is never called anywhere in the
function (a leftover from the pre-Wave-18 direct-mutation implementation) --
harmless (unreachable), noted for cleanup rather than fixed in this already
large diff.

## 14. Production Callers

`_merge_imported_album_into_existing` is the sole caller, itself invoked from
the import/relink pipelines (same call sites documented in Waves 17-19's
design docs). No new caller was added.

## 15. Repository-Wide ARCH-003 Mutation Audit (this wave)

A fresh grep-based audit of `app.py` (and the smaller route modules) for
filesystem-mutation call sites (`.unlink(`, `os.remove(`, `.rename(`,
`os.replace(`, `shutil.move(`, `shutil.rmtree(`, `shutil.copy2(`, `.rmdir(`)
and raw DML (`INSERT INTO`, `UPDATE ... SET`, `DELETE FROM`) found **82
filesystem-mutation call sites and 100 raw DML statements in `app.py` alone**
-- far more than the single `dup_rows`/`move_ids` site this wave closes.
Representative, function-identified clusters **not** covered by any
controlled transaction family, found this pass (not exhaustive -- see
`docs/TECHNICAL_DEBT.md` for the full accounting):

- `_merge_artist_dir_contents` / `clean_artist_folders_stamp_mbid`
  (~app.py:40190-41200): artist-folder merge, duplicate-file removal,
  artwork replacement, raw `shutil.move`/`.rename`/`.unlink`/`.rmdir` --
  local Web Manager mutation, no Plan/Apply/rollback/audit trail.
- `_playlist_atomic_json_replace`, `_playlist_delete_job_state`,
  `_playlist_copy_source_files`, `_playlist_run_quality_cleanup_job`,
  `playlist_delete`, `_playlist_delete_staged_track_file`
  (~app.py:42500-50120): playlist staging file management and deletion,
  local Web Manager mutation.
- routes_setup.py's `_write_env_file`/`_check_path`/
  `_claim_setup_completion_marker` writes are configuration/setup-marker
  files, not Beets library/media mutation -- explicitly out of ARCH-003's
  scope (documented here so it is not mistaken for uncounted debt).

This audit is real evidence, not the "all major mutations migrated" claim
the original submission's Technical Debt entry implied. See
`docs/TECHNICAL_DEBT.md` ARCH-003 status for the full table and the next
concrete target.

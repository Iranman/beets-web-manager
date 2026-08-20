# SEC-002 / ARCH-003 Wave 21: Artist Folder Merge & MBID Stamping Controlled Mutation Boundary

## 1. Overview

Wave 21 migrates artist-folder duplicate merging and MusicBrainz Artist ID
folder stamping (`_apply_artist_folder_groups`, `clean_artist_folders_stamp_mbid`)
behind an engine-owned controlled mutation boundary (`artist_folder_reconcile_v1`).
This revision documents the implementation as corrected during final review --
the original submission had several critical defects listed in section 8,
including basic runtime crashes and a production local-mutation fallback.

## 2. Critical Starting-State Confirmation

Independent verification found the starting head's `python-tests` and
`security` CI checks genuinely failing (two pre-existing artist-folder
identity regression tests), and `CodeQL` already failing. Reading the actual
code confirmed the report's "completed and ready for technical review" did
not hold up:

- `_apply_artist_folder_groups` and `clean_artist_folders_stamp_mbid` both
  fell back to importing and directly executing
  `backend.transaction_engine`'s Plan/Apply functions **in-process inside
  the Web Manager** whenever the `beets_client` IPC call raised any
  exception -- the Web Manager silently became the mutation authority any
  time the engine was unreachable, authentication failed, DNS failed, or
  the response failed to parse. This is exactly the architecture violation
  SEC-002/ARCH-003 exists to close, and it is why the two identity
  regression tests happened to still pass locally (they never actually
  exercised the engine IPC path -- the network call was blocked by the
  test harness, and the fallback silently performed the merge locally).
- Six helper names used throughout the engine's artist-folder code
  (`_album_cleanup_file_info`, `_album_cleanup_verified_same_file`,
  `_album_cleanup_duplicate_file_choice`, `_unique_dest`, `_MB_UUID_RE`,
  `_ART_EXTS`) were never defined or imported anywhere in
  `transaction_engine.py` -- they exist only in `app.py`. Every code path
  that reached them (any merge where the destination already contained
  files, or any MBID stamping that produces a UUID-suffixed name) would
  raise `NameError` at actual runtime. Engine-local equivalents are now
  defined directly in `transaction_engine.py` (never importing from
  `app.py`, consistent with the Wave 15 precedent that this module must
  not depend on the Web Manager).
- `_engine_stamp_artist_folder_scan` called
  `_stamp_artist_folder_album_mbid_counts(root_path, folders)` with one
  argument missing (`lib_db`) -- a `TypeError` on every call. Fixed.

## 3. Corrected Production Caller Matrix

- `_apply_artist_folder_groups` (`app.py`) -- automatic/manual duplicate
  artist-folder merge, reached from the Clean page's scan-and-merge action.
  Delegates exclusively to `beets_client.plan_artist_folder_reconcile` /
  `apply_artist_folder_reconcile`. No local fallback.
- `clean_artist_folders_stamp_mbid` (`app.py`, `POST` route) -- MBID folder
  stamping. Same delegation contract, same removed fallback.
- `_merge_artist_dir_contents` (`app.py`) -- pure move-graph **computation**
  only (never mutates the filesystem itself); used by
  `_apply_artist_folder_groups` to build a diff/log preview before
  delegating the real mutation to the engine.

## 4. Engine Ownership

Verified by `WebManagerMutationProhibitionTests` (strengthened this
review -- the original AST test only checked for raw `os`/`shutil` calls,
which is why it passed even while the function imported and directly
executed the engine's mutation functions in-process). The test now also
asserts the three migrated functions' source contains no reference to
`TransactionStore`, `create_artist_folder_reconcile_plan`,
`execute_artist_folder_reconcile_apply`, or `rollback_artist_folder_reconcile`,
and no `sqlite3` usage -- the Manager may only reach mutation through the
explicit `BeetsClient` methods.

## 5. Artist Identity Authority

- **MB Artist ID**: the engine independently derives each folder's
  established Artist ID(s) directly from Beets DB state
  (`_derive_artist_folder_identity` -- distinct non-blank
  `mb_albumartistid` values among albums whose item paths live under that
  folder), never trusting a caller-supplied MBID as authority. More than
  one distinct established id under the same folder is itself a conflict
  signal.
- **Blank IDs**: both-sides-blank or one-side-blank never merges on name
  equality alone -- explicit caller `fingerprint_confirmed: true` evidence
  is required (see below for its limits).
- **Conflicting IDs**: different established Artist IDs on either side, or
  multiple established IDs within one folder, always fails closed
  (`artist_reconcile_identity_conflict`) -- caller evidence can never
  override this.
- **Fingerprint/AcoustID**: `app.py`'s `_artist_folder_fingerprint_confirms`
  (reused, not reimplemented) gates whether the Web Manager ever proposes a
  candidate at all when identity is ambiguous by name alone. The engine
  accepts this as a `fingerprint_confirmed` flag on the candidate and
  requires it before treating a blank-identity merge as eligible -- but
  does **not** independently re-run fingerprinting itself. This remains a
  caller-trust boundary, documented as remaining debt (section 12)
  rather than claimed as independently re-verified.
- **MusicBrainz text search**: a `musicbrainz.id` result from a folder-name
  text search is deliberately never treated as identity evidence on its
  own -- it only reaches the engine as `caller_mbid`, subject to the exact
  same fingerprint-confirmation and DB-conflict gates as any other
  caller-supplied evidence.
- **AI**: plays no role in this family.

## 6. MBID Stamping Policy (majority-rule fix)

The original implementation stamped a folder's dominant Artist ID whenever
it covered >=75% of that folder's tagged albums -- a folder with 3 albums
under ID A and 1 album under a *different, already-established* ID B would
still produce a stamping candidate for A, and Apply's (also-fixed)
exact-bound-ID stamping would then overwrite that one album's correct,
conflicting ID B. Corrected: `_engine_stamp_artist_folder_scan` only
produces a candidate when a folder's tagged albums show **exactly one**
distinct non-blank Artist ID (any number of untagged/blank albums
alongside it is fine -- those get filled, not overwritten). More than one
distinct id present in the same folder produces no candidate for that
folder at all.

## 7. Path Authority

`root` and per-candidate `source_path`/`target_path` remain accepted from
the request (both engine-generated via `_engine_scan_artist_folder_groups`/
`_engine_stamp_artist_folder_scan`, and caller-supplied via
`_apply_artist_folder_groups`), always re-validated for root containment
and full symlink-chain safety at both Plan and Apply. Full server-side path
derivation (Artist-ID-only addressing) was not implemented this pass --
containment plus the independent identity gate (section 5) is the actual
safety boundary; paths are treated as expected evidence revalidated at
every mutation step, not bare authority. Tracked as a narrower remaining
improvement (section 12).

## 8. Corrected Defects (original submission)

- **CRITICAL**: local in-process mutation fallback in both production
  callers -- removed entirely (section 2).
- **CRITICAL**: six undefined helper names causing `NameError` on the most
  common code paths -- engine-local implementations added (section 2).
- **CRITICAL**: `_engine_stamp_artist_folder_scan` `TypeError` on every
  call -- fixed missing argument.
- **CRITICAL**: majority-rule (75%) MBID stamping could overwrite a
  conflicting established Artist ID -- fixed to require unanimous
  consensus among tagged albums (section 6).
- **FIXED**: Plan's identity check only compared caller-supplied MBIDs
  (`src_mbid != dst_mbid`), which the Web Manager caller made tautological
  by sending the same value as both -- replaced with independent
  DB-derived identity (section 5).
- **FIXED**: broad artist-name `UPDATE ... WHERE albumartist = ?` / `WHERE
  artist = ?` could rewrite unrelated rows anywhere in the library sharing
  the same text name. Replaced with updates bound to the exact album/item
  IDs discovered under the moved folder path at Plan time; track-level
  `artist`/`mb_artistid` is only relabeled when the track's *current*
  artist text still matches the source folder's own name (a differently
  credited track, e.g. a featured/compilation credit, is left alone).
- **FIXED**: no before-state capture for any artist attribute -- full
  `albumartist`/`albumartists`/`mb_albumartistid`/`mb_albumartistids` (albums)
  and additionally `artist`/`artists`/`mb_artistid`/`mb_artistids` (items)
  captured per exact bound row.
- **FIXED**: rollback never restored artist attributes at all -- now
  restores every captured before-value.
- **FIXED**: TOCTOU only checked size/mtime_ns -- now dev, inode, size,
  mtime_ns, plus root containment and full symlink-chain revalidation for
  every source and destination parent immediately before Apply mutates.
- **FIXED**: Apply never revalidated root/symlink safety at all (only Plan
  did) -- now revalidated at Apply too.
- **FIXED**: raw `shutil.move`/`Path.rename` reintroduced in Apply and
  Rollback (the same bug class fixed in Waves 15/17/20) -- replaced with
  the hardened `_safe_rename` helper.
- **FIXED**: quarantine destinations used only the file's basename
  (`q_dir/dup/folder.jpg`), so two different album subdirectories with an
  identically-named file would collide -- now keyed by the file's path
  relative to the scan root.
- **FIXED**: recovery metadata (`quarantined_records`/`moved_records`)
  persisted only after the entire filesystem stage completed -- now
  persisted after each individual mutation, and Apply recognizes (and
  skips) items a prior crashed attempt on the same transaction already
  durably recorded.
- **FIXED**: no structured exception handling around filesystem
  mutation -- an `OSError` partway through now marks the transaction
  `Failed` with `partial_mutation: true` and durable recovery metadata,
  instead of propagating an uncaught exception.
- **FIXED**: `filesystem_mutated`/`db_mutated` set unconditionally (even
  with zero DB path or zero rows affected) -- now set only after the first
  real, verified mutation.
- **FIXED**: no rowcount verification on any DB write -- every `UPDATE`
  now checks `rowcount == 1`, rolling back the SQL transaction on mismatch.
- **FIXED**: no Stage 3 verification at all -- Apply now re-reads DB state
  for every changed path/artist-attribute row and confirms final
  filesystem state (quarantine originals gone, quarantine copies present,
  move destinations present) before marking `Completed`.
- **FIXED**: Plan diff (`changes`) only listed file moves -- now includes
  duplicate/artwork quarantines, directory removals, and artist-attribute
  stamping.
- **FIXED**: rollback restored DB before filesystem (the same ordering bug
  Wave 20 fixed) -- reversed: files restored and verified first, DB rows
  restored after.
- **FIXED**: rollback had no resource locking, no current-state
  validation, no root/symlink revalidation, and no destination-collision
  refusal -- all added, matching the Wave 19/20 pattern.
- **FIXED**: rollback reported partial recovery as a plain success --
  now `ok: false` / `partial_mutation: true` for `Partially Rolled Back`.
- **FIXED**: raw exception text and internal paths leaked into API-facing
  errors on every failure path -- sanitized to stable public messages with
  `logging.getLogger("beets_web.transaction_engine")` carrying detail
  server-side.

## 9. Apply Ordering

Full TOCTOU (all sources + destination parents) -> `mutation_started` ->
Stage 1 quarantine (duplicates, then artwork, then moves/renames, then
empty-directory cleanup -- each persisted immediately) -> Stage 2 DB
(paths, artpaths, then exact-bound artist-attribute stamping, all
rowcount-verified, single commit) -> Stage 3 verification (DB state +
filesystem final state) -> `Completed`.

## 10. Rollback

Resource-locked; validates the DB rows this transaction itself changed
still hold its own written values before touching anything; restores
files/artwork first (root/symlink/collision-checked immediately before
each restore), recreates removed directories, then restores DB paths,
artpaths, and every captured artist attribute; reports partial recovery
truthfully; refuses a second rollback of an already-rolled-back
transaction with an idempotent, non-mutating response.

## 11. Independent Post-Correction CI Verification (second pass)

The corrections in section 8 were pushed and CI was independently re-run
(not trusted from the original head) rather than assumed green. That
re-run surfaced two further genuine defects, both introduced by this
review's own corrections and caught only because CI was actually watched
to completion instead of stopping at "pushed the fix":

- **CI-only regression: SQL `LIKE` against a BLOB path column is not a
  reliable prefix match.** Three queries built the moves/artist-stamping
  DB snapshot and `_derive_artist_folder_identity` using
  `path LIKE (prefix_bytes + b"%")`, with both the column and the pattern
  bound as raw bytes. This passed every local run but returned zero rows
  on the CI runner's SQLite build, which silently made every folder look
  identity-blank -- inverting five tests' expected outcomes (eligible
  merges rejected, conflicting merges accepted, artist-attribute rollback
  never actually restoring anything because Apply never touched the rows
  in the first place). Fixed with a new `_rows_by_path_prefix()` helper:
  a binary-collation range scan (`path >= prefix AND path < prefix +
  0xFF`) narrows the query, and an exact Python `bytes.startswith()` check
  confirms the match -- correct regardless of `LIKE`/BLOB quirks, and it
  also avoids `LIKE`'s `%`/`_` wildcard-injection risk on real file names.
  (`app.py`'s own pre-existing code for the same situation already works
  around this by `CAST`ing to `TEXT` before `LIKE` -- this module's fix
  takes a different, JOIN-friendly route to the same guarantee.)
- **CodeQL (`py/path-injection`, 26 alerts) triage found two genuine
  gaps** while confirming every flagged sink actually sits behind real
  containment validation: Apply's quarantine-source loop called
  `.exists()` on a path before its root/symlink validation ran (reordered
  validation first); and Rollback trusted a moved-record's *destination*
  path and a quarantined-record's *quarantine* path straight from
  persisted transaction metadata with no TOCTOU-style recheck at all --
  both are now independently revalidated (destination against
  `allowed_roots`, quarantine path against the quarantine base root)
  immediately before use, matching the recheck-before-every-mutation rule
  already applied everywhere else in this function.
- **CodeQL disposition**: added `_normpath_within_roots()` (the textual
  `os.path.normpath` + prefix idiom CodeQL's query recognizes) alongside
  every existing `resolve()`-based containment check; this did not by
  itself clear the alerts on re-scan. All 26 were then individually
  reviewed against the actual code (not blanket-dismissed) and confirmed
  false positives -- each sits behind real root/symlink/TOCTOU validation,
  operates on `iterdir()`-enumerated children of an already-validated
  directory tree, reads back a path this same call just wrote, or (the
  quarantine leaf name) is structurally a single path segment with no
  separators by construction (`_artist_folder_quarantine_key` strips
  `os.sep`/`/`) and cannot traverse regardless of its value. Dismissed via
  the GitHub API with a specific, code-grounded justification per alert
  (grouped by pattern, not a single blanket comment); 0 open alerts remain
  at the final head.

## 12. Known Remaining Debt (ARCH-003 stays In Progress)

- **Fingerprint confirmation remains a caller-trust boundary.** The engine
  requires `fingerprint_confirmed` evidence for blank-identity merges but
  does not independently re-run AcoustID/chroma verification itself
  (same deferral pattern as Waves 19/20's Recording-ID conflict path).
- **Path authority remains containment-based, not Artist-ID-derived
  server addressing.** `source_path`/`target_path` are still accepted from
  the request (engine-generated or caller-supplied) rather than the engine
  deriving them purely from an Artist ID; the independent identity gate is
  the real safety boundary, but this is a narrower design than "the server
  always knows the exact paths."
- **Crash-resume precision**: per-item durable state now lets a retry skip
  already-completed filesystem mutations, but does not yet distinguish
  every possible crash window (e.g. DB commit succeeded but the
  `db_mutated` marker write itself was interrupted) with full precision --
  same documented limitation as Waves 19/20.
- **Fresh repo-wide ARCH-003 audit** (this wave): filesystem-mutation call
  sites in `app.py` dropped from 82 to 73 (the artist-folder cleanup
  cluster this wave migrated). The remaining sites are **not** limited to
  playlist staging/deletion as previously assumed -- a function-level
  breakdown shows real clusters in `import_folder_with_id`,
  `apply_folder_placeholder_action_api`, `_move_artwork_to_target`,
  `album_deduplicate`, `_album_cleanup_apply_issue`,
  `_delete_album_ids_from_db`, `_remove_album_track_items`,
  `_apply_filename_cleanup_candidate`, `_album_art_quarantine_current`/
  `_album_art_restore_quarantine`, and others, in addition to the
  playlist helpers. This is corrected evidence, not a new claim of
  completeness -- see `docs/TECHNICAL_DEBT.md` for the full accounting.

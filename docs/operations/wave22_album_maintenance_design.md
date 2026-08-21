# SEC-002 / ARCH-003 Wave 22: Album Maintenance, Artwork, Import & Playlist Media Cleanup Controlled Mutation Boundaries

## 1. Overview

Wave 22 was submitted as an "ARCH-003 Large Closure" covering five transaction
families: `album_maintenance_v1`, `album_artwork_v1`, `import_folder_v1`,
`folder_cleanup_v1`, and `playlist_media_cleanup_v1`. This revision documents
the implementation as corrected during final review. The original submission
was not the closure it claimed to be: the starting head's `python-tests` and
`security` CI checks were genuinely failing, and independent code inspection
found several CRITICAL truthfulness and safety defects that the AGY report's
"2,492 tests passing / CodeQL clean / ARCH-003 DONE" summary did not disclose.

**ARCH-003 is NOT fully closed by this wave.** Genuine, substantial progress
was made (see section 8), but `import_folder_v1` still has no real Beets
import mechanism wired into the engine, and a full repository-wide audit of
every remaining local Beets/SQL/filesystem mutation site was not completed.
Section 11 lists the specific, named remaining blockers.

## 2. Critical Starting-State Confirmation

Independent verification found the starting head's `python-tests` and
`security` CI checks genuinely failing:

```
Ran 2492 tests
FAILED (errors=3, skipped=69)
```

The three errors (`test_album_maintenance_deduplicate_mode`,
`test_album_maintenance_remove_tracks_mode`,
`test_playlist_media_cleanup_plan_and_apply`) were `execute_*_apply(...)`
calls that omitted `quarantine_base_root`, so Apply fell back to the
environment default `/config/reconcile_quarantine` and failed with
`PermissionError` in CI. Fixed by supplying `quarantine_base_root` from each
test's own disposable temp directory (never by granting CI write access to
`/config`); the production Control Agent route continues to use its own
server-configured `RECONCILE_QUARANTINE_DIR`/default, unchanged.

## 3. CRITICAL Defects Found and Fixed This Review

- **`import_folder_v1` was a fake transaction (finding #3).**
  `execute_import_folder_apply` marked `filesystem_mutated=True`,
  `db_mutated=True`, and `status=Completed` unconditionally -- including when
  no `beets_import_runner` was supplied at all, which is exactly the
  production Control Agent's actual call shape (`/import/apply` never passed
  one). Every real invocation of this family returned a truthful-looking
  success while doing nothing. Fixed: Apply now requires a runner to be
  supplied AND to report `{"ok": True, ...}` before marking any mutation flag
  or `Completed`; otherwise it fails closed with
  `import_folder_no_runner_configured` (or the runner's own reported error).
- **The real Beets import still runs locally in `import_folder_with_id`
  (findings #4/#5) -- NOT closed this wave.** The function calls
  `beets_client.plan_import_folder`/`apply_import_folder`, and then,
  unconditionally and outside any success check, still runs the actual import
  via a local `subprocess.run(base_import + ["import", ...])`. Given finding
  #3's fix, the engine call now honestly fails every time in production
  (no runner is wired), so the local import is not a "fallback" that
  sometimes fires -- it is currently the *only* code path that ever performs
  an import. Removing it without a working engine-side replacement would not
  close ARCH-003; it would delete the only thing that currently imports music
  at all, which is a materially worse outcome than the truthfulness defect it
  would appear to fix. This was judged a genuine, narrow, explicitly-justified
  exception to "no local fallback" rather than a corrected item -- see
  section 11 for what real closure requires.
- **Album artwork "move" did not move (finding #9, data-loss risk).**
  `_move_artwork_to_target` called `plan_album_artwork({"mode": "move", ...})`
  without a `target_dir`, and `album_artwork_v1` had no distinct
  implementation for `move` at all -- every candidate was quarantined
  regardless of requested mode, while the caller logged "Engine controlled
  artwork relocation applied: {target_dir}" and returned as if the files were
  now at `target_dir`. Fixed: `album_artwork_v1` now has two real, distinct
  modes. `move` requires `target_dir`, independently validates it against
  `music_allowed_roots`, and actually relocates each source file to
  `target_dir` (content-hash-verified duplicate detection routes a genuine
  duplicate to quarantine instead of overwriting; a real conflict gets a
  collision-safe numbered name via the existing `_unique_dest` helper).
  `quarantine` keeps its original clear-artpath behavior.
- **Album artwork still had a local mutation fallback (finding #11).**
  `_move_artwork_to_target` fell through to a complete local
  `shutil.move`-based implementation (with its own md5 dedup check and
  `stem-N` collision naming) whenever the engine call raised or reported
  non-ok -- a second, parallel, unmigrated mutation path in production, and
  in practice the *only* implementation that ever behaved correctly, since
  the engine's "move" mode silently misbehaved (previous finding) while still
  reporting success and skipping the fallback. Removed entirely, now that
  the engine's `move` mode is real: engine failure fails closed (artwork left
  in `src_dir`, nothing moved locally, truthfully logged).
- **`filename_cleanup` could report success without renaming anything
  (finding #14/#15).** `create_album_maintenance_plan` only had branches for
  `remove_tracks` and `deduplicate`; any other `mode` (including
  `filename_cleanup`, which `_apply_filename_cleanup_candidate` actually
  sends, and `cleanup_issue`) fell through every `if`/`elif` with all
  plan lists empty and still reached `store.create(...)`, returning
  `ok: True` with a valid `operation_id`. Compounding this, Apply never
  executed `moves_plan` at all -- only `db_item_updates`' DB write ran, so
  even the `deduplicate` mode's pre-existing `fix_updates` rename path never
  actually moved a file, only the DB row. Fixed:
  `create_album_maintenance_plan` now rejects any mode outside
  `{remove_tracks, deduplicate, filename_cleanup}` with
  `album_maintenance_invalid_mode` instead of silently creating an empty
  transaction; `filename_cleanup` is now a real Plan branch (root/symlink
  validated, content-hash-verified duplicate-at-destination handling, no
  silent overwrite of a genuine conflict); and Apply now actually executes
  `moves_plan` via `_safe_rename` before touching the DB, for every mode that
  populates it. Regression test:
  `test_album_maintenance_filename_cleanup_actually_renames`.
- **Playlist media cleanup rollback had a real code bug (finding #24).**
  The album-row restore comprehension was
  `[arow[c] for arow in cols]` -- `arow` was shadowed by the `cols` loop and
  `c` was never defined, so this line raised `NameError` the first time
  rollback tried to restore an album row retired because its last item was
  removed. Fixed, with a regression test
  (`test_playlist_media_cleanup_rollback_restores_empty_album_row`) that
  deletes an album's only item and confirms rollback actually restores both
  rows instead of crashing.

## 4. Truthfulness, Rollback & Safety Corrections (all five families)

- **Mutation flags now require actual evidence.** `import_folder_v1` no
  longer sets `filesystem_mutated`/`db_mutated` without a successful runner
  result; `album_maintenance_v1`/`album_artwork_v1`/
  `playlist_media_cleanup_v1` only set `db_mutated` when a DB write actually
  ran (finding #26).
- **Exact rowcount enforcement, not best-effort counting.** Every bound DB
  `UPDATE`/`DELETE` across `album_maintenance_v1` and
  `playlist_media_cleanup_v1` now requires `rowcount == 1` and rolls back the
  whole SQL transaction on mismatch, instead of silently accepting a 0-row
  no-op as if it succeeded (findings #27/#28). Item path `UPDATE`s are now
  bound to the exact previously-known path (`WHERE id=? AND path=?`) so a row
  that changed underneath the transaction is detected, not silently
  overwritten.
- **Stage 3 post-write verification added** to `album_maintenance_v1`,
  `album_artwork_v1`, and `playlist_media_cleanup_v1`: every claimed move,
  quarantine, delete, and DB write is re-read and confirmed before the
  transaction is marked `Completed` (finding #29).
- **TOCTOU completeness.** All three families' Apply now revalidate root
  containment and full symlink-chain safety immediately before mutation (not
  only at Plan), and check dev/inode/size/mtime_ns together rather than
  size/mtime_ns alone (findings #20/#21).
- **Rollback truthfulness.** `album_maintenance_v1`, `album_artwork_v1`, and
  `playlist_media_cleanup_v1` rollback no longer swallow restore failures and
  always report `Rolled Back` -- they now track per-file/per-row success and
  return `Rolled Back`, `Partially Rolled Back` (`ok: false`), or `Failed`
  truthfully. `album_maintenance_v1` and `album_artwork_v1` rollback also now
  restore real file *moves* (not only quarantines), and
  `album_maintenance_v1`'s path-only `db_item_updates` are restorable too --
  previously only deleted rows were restorable at all (findings #30/#32).
  `import_folder_v1` rollback no longer claims `Rolled Back` for a real
  import it has no ability to undo -- it now returns `Recovery Required`,
  `ok: false` (findings #7/#33).
- **Identity authority for whole-album deduplication (findings #17/#18,
  partially closed).** `deduplicate` mode's `to_delete` list previously
  trusted the caller's `id`+`path` pair directly into the delete/quarantine
  plan with no independent verification. The engine now reloads the
  authoritative row for every requested id directly from the Beets DB,
  confirms it actually belongs to the given `album_id`, and refuses the
  whole plan (`album_maintenance_identity_mismatch`) if any requested id does
  not verify -- rather than silently skipping it. This closes the most severe
  gap (a caller could previously delete any item by id regardless of album
  membership). **Not done:** Recording-ID/fingerprint-based duplicate
  confirmation and whole-album RGID/track-set/media-health verification
  before whole-album retirement (finding #19) were not implemented this
  wave -- `to_delete` is still caller-asserted duplicate evidence, now at
  least identity-verified rather than blindly trusted.
- **Playlist media cleanup path/lock hardening (findings #22/#23).**
  DB-derived item paths are now required to be inside `music_allowed_roots`
  (and symlink-free) before being scheduled for quarantine -- a DB row is
  evidence, not proof, of a safe path. Album rows a transaction may retire
  are now included in `resource_keys` so a concurrent transaction touching
  the same album serializes instead of racing.
- **Artwork quarantine/candidate hardening (findings #12/#13).** Candidate
  source paths are now independently validated against
  `music_allowed_roots`/staging roots and checked for symlinks before being
  accepted at all (previously accepted with no check). Quarantine leaf names
  are now `{idx:04d}_{sanitized_basename}` instead of a bare `art_{basename}`,
  which collided whenever two source directories both contained e.g.
  `cover.jpg`.
- **`import_folder_v1` default staging roots narrowed (finding #8).** The
  function's own default (used only when a caller does not explicitly
  configure `staging_allowed_roots`) no longer includes `MUSIC_ROOT` --  an
  already-managed library folder is not automatically a legitimate import
  *source* just because it is a legitimate destination. A caller with a real
  "already organized but untracked" use case must opt in explicitly.

## 5. Structural Architecture Enforcement (finding #39/#40)

`tests/test_arch003_boundary_enforcement.py` previously only asserted
`"beets_client.plan_xxx(" in app_source` -- proof the call exists somewhere,
not proof the legacy local mutation it was supposed to replace is gone. That
is exactly how a real local subprocess import and a real local artwork-move
fallback both survived alongside a passing version of this same test.
Strengthened with AST-extracted-function checks:
`test_move_artwork_to_target_contains_no_direct_mutations` asserts the
migrated artwork helper's own source contains no `shutil.move(`,
`.unlink(`, `os.rename(`, `TransactionStore(`, or direct
`transaction_engine.*` calls. A second test,
`test_import_folder_local_mutation_is_a_documented_known_exception`,
deliberately asserts the *opposite* for `import_folder_with_id` -- that its
local `subprocess.run(base_import + ["import", ...])` and the comment
explaining why it is still there are both present -- so that closing that gap
requires deliberately updating this test, not silently leaving a stale
`assertIn` passing.

## 6. What Was NOT Attempted This Wave (explicit, not silently deferred)

- **`cleanup_issue` mode (finding #16).** `create_album_maintenance_plan`
  explicitly rejects this mode (`album_maintenance_invalid_mode`) rather than
  silently no-op-succeeding, but no real implementation was added.
  `_album_cleanup_apply_issue`'s per-issue-subtype dispatch (including at
  least one path with a direct `shutil.move` rollback observed during
  review) was not audited subtype-by-subtype this wave.
- **Full repo-wide Local Beet/Raw SQL/Filesystem/Control-Agent-bypass audits
  (findings #47-#50).** `scripts/comprehensive_mutation_scan.py` and
  `scripts/audit_mutation_inventory.py` (both added this wave) are pattern
  scanners that enumerate candidate mutation sites -- they do not classify,
  do not fail CI on an uncontrolled finding, and do not by themselves prove
  closure of anything, exactly as finding #37/#38 describes. Both still run
  cleanly and remain useful as a starting point for the next audit pass, but
  neither should be cited as evidence that the repo-wide sweep was completed.
- **The full enumerated test matrix in findings #54-#59.** Targeted
  regression tests were added for every defect actually fixed this wave (see
  section 7); the much larger enumerated scenario list (every TOCTOU window,
  every crash-recovery interruption point, every concurrency interleaving,
  per family) was not built out in full.

## 7. Test Coverage Added/Fixed This Wave

- `tests/test_beets_transaction_engine.py`: fixed the three CI-failing tests
  (`quarantine_base_root` injection); added
  `test_album_maintenance_filename_cleanup_actually_renames`,
  `test_album_maintenance_unsupported_mode_fails_closed`,
  `test_playlist_media_cleanup_rollback_restores_empty_album_row`,
  `test_import_folder_apply_without_runner_fails_closed`,
  `test_import_folder_rollback_of_real_import_is_not_claimed_reversed`; and
  extended `test_album_artwork_plan` into a full Plan->Apply lifecycle
  proving `move` mode actually relocates a file to `target_dir`.
- `tests/test_import_artwork_cleanup.py`: five tests that asserted source
  strings belonging to the removed local fallback (md5 hashing, `stem-N`
  naming, per-file "Moved" logging) were replaced with tests proving the
  fallback is gone and the helper delegates to the engine with `target_dir`,
  failing closed on any engine error.
- `tests/test_arch003_boundary_enforcement.py`: the two new
  presence/absence tests described in section 5.

## 8. Independent Post-Correction CI Verification

Corrections were pushed and CI was independently re-run to a genuinely
terminal state at each corrected head (not trusted from the first push),
matching the pattern established in Waves 19-21. Full suite, security gates,
and CodeQL disposition for the final merged head are recorded in
`docs/TECHNICAL_DEBT.md`'s Wave 22 entry.

## 9. Refreshed ARCH-003 Audit

See `docs/TECHNICAL_DEBT.md` for the current accounting. This wave's own
`scripts/comprehensive_mutation_scan.py` run (informational only, see
section 6) still finds roughly 145 functions with real filesystem/DB/
subprocess mutation patterns in `app.py`, including `import_folder_with_id`
itself and `_album_cleanup_apply_issue`'s remaining local-mutation branches.
ARCH-003 remains **In Progress**.

## 10. Rollback/Recovery Status Summary

| Family | Rollback semantics |
| --- | --- |
| `folder_cleanup_v1` | Truthful, pre-existing from an earlier wave; unchanged this review. |
| `playlist_media_cleanup_v1` | Truthful (`Rolled Back` / `Partially Rolled Back` / `Failed`); the album-row `NameError` is fixed. |
| `album_maintenance_v1` | Truthful; restores quarantines, real moves, and path-only DB updates; reports partial failure honestly. |
| `album_artwork_v1` | Truthful; restores both quarantines and real moves; reports partial failure honestly. |
| `import_folder_v1` | **Not real rollback.** Returns `Recovery Required`, `ok: false` when a real import occurred -- this is an honest admission, not a fix. Real rollback (or a documented decision that none is possible) requires finding #6 to be closed first, since the engine currently has no record of which rows/files a real import produced. |

## 11. Named Remaining Blockers (not a vague "future wave" backlog)

1. **`import_folder_v1` has no real Beets-import-inside-the-engine
   mechanism.** The Control Agent's existing `reimport_source_atomic`
   (`backend/beets_control_agent.py`) is the right reusable building block
   for this, but wiring it in as `execute_import_folder_apply`'s
   `beets_import_runner` -- while removing `import_folder_with_id`'s local
   `subprocess.run` and preserving that function's substantial existing
   downstream logic (MB release resolution, selected-subset import,
   `wanted_tracks`, `replace_existing_item_ids`, review-queue handling, AI
   suggestion recording, Plex-refresh triggering) -- is a materially larger,
   higher-risk change than could be safely completed and fully regression-
   tested in this review. Attempting it hastily risked exactly the kind of
   silent production import breakage SEC-002 exists to prevent.
2. **`cleanup_issue` mode is unimplemented** in `album_maintenance_v1`
   (explicitly rejected, not silently accepted -- see section 6).
   `_album_cleanup_apply_issue`'s issue-subtype dispatch in `app.py` was not
   migrated or audited subtype-by-subtype.
3. **No completed repo-wide classification** of every remaining local Beet
   subprocess call, raw Beets-library SQL DML, or direct filesystem mutation
   in `app.py` (findings #47-#50). The two scanner scripts added this wave
   are a starting point, not a completed audit.

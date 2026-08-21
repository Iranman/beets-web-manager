# SEC-002 / ARCH-003 "Final Closure": Independent Review Findings

## 1. Overview

PR #96 was submitted as "ARCH-003 Final Closure" claiming the Web Manager is
"100% orchestration-only with zero local filesystem, database, or Beets
mutation bypasses" and a machine-checked mutation inventory proving it. CI on
the starting head was green. Neither claim held up under independent review.

**ARCH-003 is still not closed.** This revision documents what PR #96
actually contained, what was genuinely wrong with it, what was fixed, and —
for the first time — a real, evidence-based accounting of how much
unmigrated surface actually remains (see section 6). One good, real
architectural change from the original submission is preserved: `import_
folder_with_id` no longer runs a local `beet import` subprocess (see
section 2).

## 2. What Was Genuinely Correct: Engine-Owned Import

`import_folder_with_id` previously ran the real import via a local
`subprocess.run(base_import + ["import", ...])`. PR #96 correctly removed
that and now delegates through `beets_client.plan_import_folder` /
`apply_import_folder`; the Control Agent's `/import/apply` route supplies a
real `_beets_import_runner` that invokes the engine's existing
`reimport_source_atomic`. This is the right direction and is preserved
unchanged by this review.

## 3. CRITICAL: The Cleanup-Issue Flow Was Fail-Open

`_album_cleanup_apply_issue` is the function behind the album-folder cleanup
issue apply flow. Every mutating branch had the same shape:

```python
try:
    plan_res = beets_client.plan_xxx(...)
    if plan_res.get("ok"):
        apply_res = beets_client.apply_xxx(...)
        if apply_res.get("ok"):
            ...  # success bookkeeping
            continue
except Exception:
    pass
# falls through to local shutil.move / rmdir / DB update
```

Independent inspection found four compounding defects that, together, meant
this fallback ran on *every real invocation*, not occasionally:

- **Empty-folder removal** (finding #4) fell through to a local
  `_album_cleanup_remove_empty_tree` walk on any engine failure.
- **Duplicate quarantine** (finding #5/#6) sent `{"id": 0, "path": ...}` to
  `album_maintenance_v1`. That family requires a real, positive item id and
  independently reloads the row by id (a Wave 22 review fix) — `id=0` binds
  to nothing, so Apply never touched anything, and the local `shutil.move`
  to a hand-rolled trash path always ran instead.
- **Filename cleanup / artwork move** (findings #8/#10) had the same `item_
  id=0` bug for non-artwork moves, and the artwork branch called
  `plan_album_artwork({"mode": "move", ...})` **without** `target_dir` —
  also a required field as of the Wave 22 review. Plan always rejected it,
  and the local `shutil.move` + `_album_cleanup_update_db_path` RPC pair ran
  instead.

The net effect: every one of these branches' "engine controlled" code paths
were structurally guaranteed to fail on every real call, and the
`except Exception: pass` silently absorbed that and ran the pre-existing
local mutation regardless. "100% orchestration-only" was not just aspirational
here — the code that was supposed to prove it never actually executed on any
real input.

### Fix

- `_album_cleanup_item_id_for_path(path)`: resolves the authoritative Beets
  item id for an exact on-disk path (or 0 if untracked). Both the duplicate
  and filename-cleanup branches now use this instead of a hardcoded `0`.
- Artwork moves now pass `"target_dir": str(canonical)` — the destination
  folder this function already resolves and validates earlier in its own
  logic.
- Every branch now fails closed on Plan/Apply failure or engine
  unreachability (`BeetsUnavailableError`/`BeetsError` caught explicitly,
  re-raised as a blocking `issue_errors` entry) instead of silently falling
  through to a local mutation. A `None` `operation_id` (Plan legitimately
  found nothing actionable) is never passed to Apply.
- The now-dead local fallback code (`_album_cleanup_remove_empty_tree`'s
  caller in this function, the local `shutil.move`-to-trash path, `_album_
  cleanup_update_db_path`, `_album_cleanup_trash_path`) was removed, not
  left "just in case." The ancestor-empty-folder cleanup loop at the end of
  the function (previously a direct `current.rmdir()` walk, a gap not
  explicitly named in the original review brief but the same class of local
  mutation) was migrated to `folder_cleanup_v1` too.
- `_album_cleanup_remove_empty_tree` itself is still used by one other,
  unrelated function (`_root_folder_repair_apply_safe`) that this review did
  not touch — a known, separate, out-of-scope local-mutation site (see
  section 6's inventory; it is a real `ARCH003_BLOCKER` entry, not hidden).

### Verification

`tests/test_arch003_final_closure.py` now AST-extracts the real function and
asserts the absence of `shutil.move`/`.unlink(`/`.rmdir(` call shapes,
alongside targeted regressions for each of the four bugs above (no `"id": 0"`
/ `"item_id": 0"` literals, `target_dir` present, Apply never called with a
bare dict-subscript that could be `None`). The pre-existing
`test_album_cleanup_apply_issue_routes_to_beets_client` (a plain `assertIn`
that passed throughout the original, broken submission) is kept alongside
these — presence alone was never sufficient proof, and finding #49 explicitly
asked for the absence check that was missing.

Two existing test files (`tests/test_album_folder_cleanup.py`,
`tests/test_sec002_app_path_album_folder_repair.py`) exercised this function
end-to-end and broke once the local fallback was actually removed — they had
been (silently) relying on it to perform real file moves whenever their test
doubles didn't stub the engine calls. Both were updated with proper fake/mock
`beets_client` doubles that perform the equivalent engine effect (move,
quarantine, DB update) so they keep testing this function's real
orchestration logic rather than a bypass.

## 4. Real Mutation-Site Discovery (findings #15-#27)

The submitted `security/arch003_mutation_inventory.json` had 8 hand-written
entries and `scripts/verify_arch003_mutation_inventory.py` only validated
that JSON's internal consistency (spelling of classifications, existence of
named transaction families) plus one hardcoded string check for the old
import subprocess. It never inspected the actual source code, so it could
never have detected an unclassified sink no matter how many existed.

This is now real:

- **`scripts/discover_mutation_sinks.py`**: an AST walker over `app.py`,
  `routes_*.py`, `job_engine.py`, `helpers_mb.py`, and `backend/*.py`. It
  finds candidate filesystem writes/deletes/moves, SQL DML (`INSERT`/
  `UPDATE`/`DELETE`/`REPLACE` against a literal or f-string query), Beet/
  subprocess execution, and tag writes, via real `ast.Call` inspection —
  not string grepping. Each sink gets a key
  (`file:enclosing_function:hash(call_text)`) designed to survive incidental
  line renumbering elsewhere in the file.
  - A first pass that flagged any `.replace(`/`.rename(` method call
    produced overwhelming false positives from ordinary `str.replace()` text
    normalization (the vast majority of hits). Pure AST syntax cannot prove
    a receiver's type, so these two method names are only counted when the
    receiver expression looks like a `Path` (constructed via `Path(...)` or
    a plausibly-path-named variable) — documented in the script as a
    deliberate precision/recall tradeoff, not a claim of certainty.
- **`scripts/generate_arch003_mutation_inventory.py`**: runs the discovery
  and applies rule-based classification (documented per-entry via a `rule`
  field, e.g. `"legacy-local-beet-subprocess:move,write"` or
  `"engine-function-family-map"`), writing `security/arch003_mutation_
  inventory.json`. Re-running it preserves any entry a human has explicitly
  marked `human_reviewed: true` rather than silently overwriting a manual
  judgment call.
- **`scripts/verify_arch003_mutation_inventory.py`** (rewritten): re-runs
  discovery against the *current* source and requires every discovered sink
  to have a matching inventory key. A sink with no matching key at all —
  something genuinely new — fails the gate outright. This is a real,
  working regression check, proven by three self-tests using synthetic
  source trees (`tests/test_arch003_final_closure.py`): a brand-new
  unclassified sink fails; the same sink with a proper matching entry
  passes; and the *count* of `ARCH003_BLOCKER`/`NEEDS_REVIEW` entries is not
  allowed to grow past the recorded baseline even when every sink does have
  a key (so debt can't quietly accumulate one still-unclassified entry at a
  time).

## 5. Why The Gate Is A Ratchet, Not An All-Or-Nothing Switch

Running real discovery against this repository for the first time found
**372 candidate sinks** in the files scanned. A large share of them are
genuine, real, pre-existing ARCH-003 debt — direct Beets-library `UPDATE`/
`DELETE` statements and local `beet` CLI subprocess calls still living in
`app.py` functions this review did not touch (`album_fix_metadata`,
`match_album`, `album_rename`, `_delete_album_ids_from_db`,
`_delete_library_db_rows_under_folder`, and others — the current inventory
lists every one by file/function/line). This is not a surprise: Waves 20-22
each independently found the same thing — the unmigrated surface is broader
than whatever the current wave assumed. What's different this time is that
the number is now backed by a real scan instead of an estimate.

Failing CI on the *entire* pre-existing 189-entry backlog (see section 6)
would not make anything safer today — it would just make this specific PR
unlandable, which is exactly the pressure that produces `--no-verify` habits
and fake-passing gates like the one this review just replaced. The gate
instead: (1) requires every sink in the *current* source to be accounted
for by key, so nothing new can hide, and (2) requires the unresolved count
not to grow, so today's debt can only shrink over time, one classified/fixed
entry at a time, without blocking unrelated work.

## 6. Current Mutation Inventory (real numbers, not a claim)

372 total discovered sinks:

| Classification | Count | Meaning |
| --- | --- | --- |
| `CONTROLLED_MEDIA_MUTATION` | 61 | Inside `backend/transaction_engine.py`, mapped by function name to its owning `_v1` family -- the engine's own implementation of the controlled boundary. |
| `ENGINE_NATIVE_BEETS` | 63 | Inside `backend/beets_control_agent.py` -- the engine container's native Beets/filesystem interface, by architectural definition. File-level default, not individually re-reviewed line by line this pass. |
| `ARCH003_BLOCKER` | **97** | Real, unmigrated Web Manager mutation: raw Beets-library SQL DML or a local mutating `beet` subprocess call. |
| `NEEDS_REVIEW` | **92** | Discovered, not yet triaged with enough confidence to classify either way -- mostly in smaller support modules (`routes_setup.py`, `backend/audio_preferences.py`, `backend/slskd.py`, `helpers_mb.py`) and some `app.py` filesystem/SQL sinks the automated rules couldn't confidently place. |
| `APP_STATE` / `CONFIG_STATE` / `CACHE_STATE` / `STAGING_ONLY` / `TRANSACTION_STATE` | 52 total | Web Manager's own state (job/manifest files, settings, secrets, image/metadata caches, transaction store) -- not Beets library media. |
| `NON_MEDIA_FILESYSTEM` | 7 | Non-Beets subprocess calls, or filesystem operations already reviewed and confirmed non-library-mutating this session (e.g. `_move_artwork_to_target`'s cosmetic empty-subdirectory cleanup, left in place with its own comment explaining why). |

**189 of 372 sinks (97 + 92) are unresolved.** That is the honest, current
size of the remaining ARCH-003 gap — not zero, and not "DONE." The
`unresolved_baseline` field in `security/arch003_mutation_inventory.json`
is set to 189; the CI gate fails if that number increases.

## 7. What This Review Did Not Attempt

- Classifying the full 92-entry `NEEDS_REVIEW` bucket or fixing the 97-entry
  `ARCH003_BLOCKER` bucket. That is a real, multi-function migration effort
  (each of `album_fix_metadata`, `match_album`, `album_rename`, and several
  more would need its own Plan/Apply family or migration to an existing one,
  each with its own TOCTOU/rollback/rowcount design work) -- not something a
  single review pass can responsibly rush.
- A line-by-line audit of `backend/beets_control_agent.py`'s 63
  `ENGINE_NATIVE_BEETS` sinks or its generic mutation surface (`run_command`,
  `PATCH` item/album, generic file endpoints) for bypass risk -- flagged as
  a specific named remaining item, not silently assumed safe.
- Native two-service import acceptance testing (spinning up both containers
  and exercising a real import end-to-end) -- the existing unit-level
  `BeetsClient`/Control-Agent tests were run and pass, but that is a
  narrower guarantee than a live two-service run.

## 8. Verification

Full suite, security gates, and CodeQL disposition for the final merged head
are recorded in `docs/TECHNICAL_DEBT.md`'s entry for this PR.

## 9. ARCH-003 Status

**In Progress.** Not Done. See `docs/TECHNICAL_DEBT.md` for the specific
named remaining blockers.

# SEC-002 / ARCH-003 Wave 24: Large Album Lifecycle Controlled-Mutation Consolidation

## Executive Summary

**PR #98, as submitted, did not achieve the album-lifecycle consolidation
its own executive summary and generated inventory claimed.** Independent
verification (reading the actual diff, tracing the actual engine-side
transaction family implementations, and running the actual scanner)
found that most of the claimed wins were either non-functional at
runtime, or restated pre-existing Wave 23 work as new. This document
replaces the submitted version with the actually-verified state after
this review's corrections. It is not a claim of full closure -- ARCH-003
remains **In Progress**, and the album-lifecycle domain specifically
remains substantially unresolved. See "What Wave 24 Actually Changed"
below for exactly what shipped, and "Confirmed Defects Found And Fixed"
for what the submitted version got wrong.

## Confirmed Defects Found And Fixed

1. **CRITICAL -- `BeetsClient.replace_album_art()`/`delete_album_art()`
   regressed the pure-HTTP-proxy architecture and would crash in
   production.** The submitted version base64-decoded the uploaded image
   and wrote a staging file to a local `STAGING_ROOT` path *inside the
   web-manager container*, then referenced that local path as a "source"
   candidate in the HTTP request to the engine. This fails on two
   independent levels: the standard web-manager deployment
   (`docker-compose.yml`) runs the container `read_only: true` with no
   `/config` volume mount at all, so the local `mkdir()`/`write_bytes()`
   calls raise immediately; and even with a writable filesystem, the
   engine is a separate container -- a path written to the web-manager's
   disk is not visible to the engine's filesystem. The code also called
   `uuid.uuid4()` without importing `uuid`, so it additionally would have
   raised `NameError` before ever reaching the filesystem call -- clear
   evidence this path was never actually exercised. `delete_album_art()`
   similarly probed `Path.exists()`/`Path.is_file()` on media paths
   directly from the web-manager container, which has no filesystem
   access to the library by design. **Fixed**: both methods restored to
   the pure single-HTTP-call proxy contract; a new AST-based structural
   test (`test_beets_client_class_has_no_local_filesystem_mutation_calls`)
   walks the whole `BeetsClient` class and fails CI if any filesystem
   mutation call shape reappears anywhere in it.
2. **`album_artwork_v1`'s "move" mode does not write `albums.artpath`.**
   Even setting aside defect #1, routing artwork replacement through
   `create_album_artwork_plan`/`execute_album_artwork_apply` with
   `mode="move"` would not have worked correctly: that mode's apply path
   only updates the DB for `mode=="quarantine"`; `mode=="move"` moves
   files and returns a count, but never touches `albums.artpath`. Using
   it for "replace album cover" would silently leave the DB artwork
   pointer stale after a "successful" replace. This is real, scoped
   future work (teach the family's move mode to optionally update
   `artpath`), not attempted this pass -- see "Deferred Work" below.
3. **`album_rename` and `album_move_to_library` became silent no-ops
   that reported job success.** `album_rename` called
   `plan_album_maintenance(mode="filename_cleanup", album_id=aid)` with
   no `candidates`; that mode is a caller-supplied-candidate move
   primitive with no path-template computation of its own, so an empty
   candidate list always plans zero moves, and the route logged a note
   and reported success while renaming nothing. `album_move_to_library`
   called `plan_existing_album_reconcile({"album_id": aid})`; that family
   requires two distinct album ids (`imported_album_id`/
   `existing_album_id`, duplicate-merge semantics) and its own validation
   rejects a call missing either, so the plan failed on every single
   invocation, yet the route continued on to art-repair and reported job
   success regardless. **Fixed**: both routes now fail closed (`raise
   RuntimeError` with a specific, honest message) instead of silently
   claiming success. Real implementations are tracked as deferred work.
4. **`album_fix_metadata` silently dropped legitimate field edits.** The
   route bundled the non-editable `mb_albumid` field into the same
   `beets_client.update_album_fields()` PATCH payload as the editable
   `album`/`albumartist`/`year` fields. The control agent's PATCH handler
   rejects the entire request (HTTP 403) if any key is outside
   `ALBUM_EDITABLE_FIELDS` (which had MusicBrainz identity fields removed
   in Wave 23, by design). So supplying a new `mb_albumid` alongside a
   title/artist/year edit silently dropped all of them, every time,
   caught only by the route's own blanket `except Exception` log line.
   **Fixed**: `mb_albumid` is excluded from the generic PATCH payload;
   it is still applied, correctly, via the pre-existing, dedicated
   `album_mb_track_repair_v1` transaction the route already called
   separately.
5. **The generated inventory's "0 NEEDS_REVIEW" and domain-zero claims
   were reached via blanket per-file defaults and a brittle
   line-number-range guesser, not real triage.** The submitted
   `generate_arch003_mutation_inventory.py` changed the default
   classification for every unmapped sink in `routes_setup.py`,
   `routes_submissions.py`, `job_engine.py`, all `backend/*.py` support
   modules, and unmapped `backend/beets_control_agent.py` functions from
   `NEEDS_REVIEW` to a specific non-`NEEDS_REVIEW` label -- exactly the
   "blanket file-level exemption" failure mode Wave 23 fixed and
   documented, reintroduced under different labels. It additionally added
   `_classify_control_agent_handler_line()`, a hardcoded line-number-range
   table classifying ControlAgentHandler's ~40-branch HTTP dispatcher
   methods (`do_POST`/`do_DELETE`/`do_PATCH`) by raw source line position
   rather than by understanding which `if path == "/...":` branch a sink
   actually sits in -- both fragile (silently wrong the moment an
   unrelated edit shifts line numbers) and a guess dressed up as
   precision. It also dropped the mechanism (introduced in Wave 23) that
   preserves an already-`human_reviewed` prior entry verbatim on
   regeneration, meaning any future rule-table change could silently
   overwrite real investigation provenance. **Fixed**: all blanket
   defaults reverted to `NEEDS_REVIEW`; the line-range guesser removed
   and the dispatcher methods reverted to `NEEDS_REVIEW` with the
   original Wave 23 rationale restored; the prior-entry preservation
   mechanism restored. The genuinely new, legitimate parts of the
   submitted generator (`domain` field assignment via
   `_determine_domain()`, a handful of real per-function classifications
   for newly-discovered helper functions, the CI gate's domain-accounting
   check and its `ENGINE_GENERIC_BYPASS` baseline-formula fix) were kept.
6. **`verify_arch003_mutation_inventory.py` re-added a hard
   `NEEDS_REVIEW must be 0` gate**, which Wave 23 deliberately removed
   with documented reasoning: a hard zero-gate creates exactly the
   pressure that produces the fake precision described in defect #5.
   `NEEDS_REVIEW` remains part of the unresolved ratchet total (so it
   still cannot silently grow), just not mechanically required to hit
   zero. **Fixed**: hard gate removed, Wave 23 rationale comment
   restored.
7. **`album_deduplicate`'s docstring claimed migration onto
   `album_maintenance_v1` that its body does not implement** -- the
   function body still reads/writes the library DB directly via `_db()`
   and runs local matching/`beet mbsync` logic, unchanged from before
   this PR. **Fixed**: docstring corrected to describe actual behavior.
   Also removed one duplicate, unreachable `return jsonify(...)` line.

## What Wave 24 Actually Changed (Verified)

- `POST /api/albums/<id>/remove` and `BeetsClient.delete_album()` now
  route through `album_maintenance_v1`'s `remove_tracks` mode (real Plan/
  Apply, TOCTOU revalidation, file quarantine to a recoverable location,
  rollback support) instead of the untransacted `_handle_delete_album`.
  This is a genuine, verified improvement -- confirmed correct by reading
  `create_album_maintenance_plan`'s `remove_tracks` branch in
  `backend/transaction_engine.py`. `_handle_delete_album` itself was
  **not** deleted (it retains direct unit-test coverage in
  `tests/test_external_beets_architecture.py` and
  `tests/test_pr39_codeql_alerts.py`, and its HTTP route still exists) --
  it is simply no longer called by the client. Left in place rather than
  removed, per this review's standing policy against ripping out working,
  tested code under time pressure.
- Several genuinely broken local subprocess call sites in `app.py`
  (`album_replace_art_from_url`/`album_upload_art`'s `beet embedart`
  calls, `album_rename`/`album_move_to_library`/`album_fix_metadata`'s
  local `beet write`/`beet move`/`beet mbsync` subprocess invocations)
  were removed. These could never have succeeded in the standard
  container deployment in the first place (the web-manager image has no
  local Beets install) -- their removal is a real cleanup, independent of
  whether the engine-routed replacements that took their place actually
  work (see defects #1-#4 above for where they didn't, until this
  review's fixes).
- `security/endpoint_inventory.json` regenerated to match the current
  route line numbers (no semantic route changes; 0 entries need manual
  review).
- `domain` field added to every inventory entry; the CI gate now fails
  if an `ARCH003_BLOCKER`/`ENGINE_GENERIC_BYPASS` entry has no domain.
  This is real and kept.

## Domain Accounting & Mutation Surface Matrix (Real, Post-Fix)

Regenerated by `scripts/generate_arch003_mutation_inventory.py` against
the corrected source, verified by `scripts/verify_arch003_mutation_inventory.py --check`:

| Classification | Sinks Count |
| :--- | :--- |
| `CONTROLLED_MEDIA_MUTATION` | 89 |
| `ENGINE_CONTROLLED_TRANSACTION` | 10 |
| `ENGINE_NATIVE_BEETS` | 8 |
| `ENGINE_ADMIN_MUTATION` | 3 |
| `ENGINE_CONFIG_STATE` | 10 |
| `ENGINE_NATIVE_READ_ONLY` | 2 |
| `CONFIG_STATE` | 41 |
| `APP_STATE` | 38 |
| `CACHE_STATE` | 14 |
| `STAGING_ONLY` | 13 |
| `TRANSACTION_STATE` | 15 |
| `NON_MEDIA_FILESYSTEM` | 10 |
| `READ_ONLY_FALSE_POSITIVE` | 1 |
| **`ENGINE_GENERIC_BYPASS`** | 16 |
| **`ARCH003_BLOCKER`** | 210 |
| **`NEEDS_REVIEW`** | **41** (honest, not mechanically forced to 0) |
| **Total Sinks** | **521** |
| **Total Unresolved (ratchet baseline)** | **267** |

This is an improvement over Wave 23's checked-in baseline (293
unresolved: 224 blocker / 53 needs-review / 16 bypass), earned through a
handful of genuine new per-function classifications this PR contributed
(`_normalise_album_art_image`, `_replace_or_copy_unlink`,
`_create_exclusive_temp_file`, `acquire_os_lock`, `AgentJob._run`) plus
several real dead-subprocess-call removals in `app.py` -- not through the
blanket defaults or line-range guessing, which have been reverted.

## Unresolved Sinks by Domain (Real, Post-Fix)

| Domain | Unresolved Count |
| :--- | :--- |
| `other` | 70 |
| `album_metadata` | 60 |
| `album_artwork` | 37 |
| `import_reconciliation` | 28 |
| `album_relocation` | 25 |
| `album_retirement` | 18 |
| `ai_import` | 11 |
| `config` | 14 |
| `library_cleanup` | 4 |

**The album-lifecycle domains (`album_metadata`, `album_artwork`,
`album_relocation`, `album_retirement`) are NOT at zero.** Full closure
of those four domains was this wave's stated exit criterion and was not
achieved -- see "Deferred Work" for exactly why each remains open. The
majority of the remaining count in each domain is the
`backend/beets_control_agent.py` HTTP dispatcher sinks
(`do_POST`/`do_DELETE`/`do_PATCH`), honestly left `NEEDS_REVIEW` because
per-endpoint semantic classification (knowing which `if path == "/...":`
branch a given sink sits in) is a real capability gap in
`discover_mutation_sinks.py`, not something safe to guess via line
numbers.

## Deferred Work (Not Attempted This Pass, Named Explicitly)

1. **`album_artwork_v1`'s "move" mode needs to optionally write
   `albums.artpath`** before it can correctly back an "upload new cover
   art" operation. Until then, `replace_album_art`/`delete_album_art`
   correctly remain on the tested, atomic `_replace_album_art_locked`/
   `_delete_album_art_locked` engine-side implementation (classified
   `ENGINE_GENERIC_BYPASS` -- real, honest, not hidden).
2. **A real "move this album's own files onto its correct library path"
   transaction family does not exist.** `existing_album_reconcile_v1`
   models duplicate-album merging (two distinct albums), not this.
   `album_move_to_library` fails closed until a real family is built.
3. **`filename_cleanup` mode has no path-template computation.** It only
   executes caller-supplied `{item_id, source, destination}` candidates.
   Computing real candidates requires beets path-template evaluation
   (`Item.destination()`), which nothing in
   `backend/beets_control_agent.py` currently imports or calls. Either
   add that computation engine-side (the engine has beets/DB access; the
   web-manager container does not), or accept `album_rename` failing
   closed until it does. `album_rename` fails closed until then.
4. **`album_deduplicate` was never migrated onto `album_maintenance_v1`'s
   `deduplicate` mode.** It remains on its original, working, tested
   local-DB-based implementation; not swapped for an unverified
   replacement under review time pressure.
5. **`backend/beets_control_agent.py`'s HTTP dispatcher methods still
   need real per-endpoint semantic classification** in
   `discover_mutation_sinks.py` (correlating each discovered sink with
   the specific `if path == "/...":`/`path.startswith(...)` branch it is
   lexically inside, via AST scope, not source line numbers). This is
   the single largest remaining source of `NEEDS_REVIEW` count across
   every domain, not just the album-lifecycle ones.

## Architecture & Verification (Real, Post-Fix)

1. **Fail-closed engine delegation, actually verified**: `album_rename`
   and `album_move_to_library` now raise rather than silently succeed
   when the engine cannot actually perform the requested action (see
   `tests/test_album_lifecycle_wave24_fixes.py`).
2. **`BeetsClient` purity, actually verified structurally**: a new
   AST-based test walks the entire `BeetsClient` class body and fails if
   any filesystem-mutation call shape appears anywhere in it, not just a
   substring check that `_request`/error classes exist somewhere in the
   file.
3. **TOCTOU revalidation** in `album_maintenance_v1`/`album_artwork_v1`
   apply paths (device/inode/size/mtime re-check under lock immediately
   before mutation) predates this wave and was independently confirmed
   still present by reading `execute_album_maintenance_apply`/
   `execute_album_artwork_apply`.
4. **CI gate**: full Python test suite, all 4 security verification
   scripts, and `verify_arch003_mutation_inventory.py --check` all pass
   against the corrected head (see the Wave 24 TECHNICAL_DEBT.md entry
   for exact counts).

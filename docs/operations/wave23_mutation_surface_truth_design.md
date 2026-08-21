# SEC-002 / ARCH-003 Wave 23: Mutation Surface Truth Pass -- Independent Review Findings

## 1. Overview

Wave 23 was submitted as a "mutation surface truth pass" claiming
`NEEDS_REVIEW = 0` (100% triaged) against 491 discovered sinks, 180 of them
`ARCH003_BLOCKER`. CI on the starting head was green. Independent review found
the "100% triaged" claim was not true: `NEEDS_REVIEW = 0` was reached mostly
through blanket file-level defaults (e.g. "every sink in
`backend/beets_control_agent.py` is `ENGINE_NATIVE_BEETS`"), not individual
triage -- exactly the pattern this wave was supposed to eliminate. The
scanner itself also had three real correctness bugs that meant it was
silently missing and misclassifying sinks even before the blanket-default
problem. All are fixed in this revision, and one genuine, currently-active
architectural gap was found and closed with real code, not just a label.

**ARCH-003 remains In Progress.** The honest, corrected numbers are larger
than either PR #96's or the original Wave 23 submission's -- not because more
debt was created, but because real discovery and real per-sink review found
debt that blanket classifications had been hiding.

## 2. Preserved From The Original Submission

- Removal of local `canonical.mkdir(...)`/`dst.parent.mkdir(...)` calls from
  `_album_cleanup_apply_issue` (parent-directory creation is now the
  engine's responsibility, matching Wave 22's principle).
- Expanded AST sink discovery (mkdir/touch/write_text/write_bytes, writable
  `open()`, file-handle writes, SQL-variable tracking) -- the underlying
  approach was right; three of the new detection rules had real bugs (see
  section 3).
- The ratchet-gate concept from PR #96 (a discovered sink must have a
  matching inventory entry; the unresolved count must not grow).
- Honest `ARCH-003: In Progress` status (not marked Done).

## 3. Scanner Defects Found And Fixed

- **Finding #9 (CRITICAL): `_beet_run(...)` bare-name calls were never
  detected.** The subprocess-detection rule listed `"_beet_run"` in a tuple
  compared against `_attr_chain(node.func)`, but `_attr_chain()` only
  resolves `ast.Attribute` call targets (`module.attr(...)`) -- a bare-name
  call's `.func` is an `ast.Name`, so `_attr_chain()` always returns `None`
  for it and the comparison could never match. `_beet_run` is called this
  way roughly a dozen times in `app.py` alone (modify/mbsync/write/move/
  fetchart/lastgenre/mbsubmit/import), none of which were ever being
  discovered. Fixed with a dedicated bare-name wrapper set
  (`_BARE_SUBPROCESS_WRAPPER_NAMES`); confirmed via `tests/test_discover_
  mutation_sinks.py::test_bare_beet_run_wrapper`. Audited for other
  project wrappers called the same way (finding #10) -- `_safe_rename` and
  `_write_file_audio_tags` do not need the same treatment because the real
  filesystem/tag-write call inside *their own* bodies is already discovered
  independently; `_beet_run` is different because each of its many call
  sites represents a semantically distinct beet subcommand invocation worth
  its own inventory entry.
- **Finding #11 (CRITICAL): the path-like name regex matched almost any
  identifier.** `_PATH_LIKE_NAME_RE` carried bare `p`, `f`, `d` as ordinary
  regex alternatives inside a `search()` (substring) match -- so any
  identifier merely *containing* the letter p, f, or d anywhere (`data`,
  `config`, `added`, `id`, ...) counted as "looks like a path", destroying
  any real signal in `rename`/`replace` receiver classification. Fixed:
  single-letter conventional path variables (`p`, `f`, `d`) are now matched
  via a separate exact-identifier pattern (`^(?:p|f|d)$`), never as a
  substring of a longer name. Same bug existed in the classifier
  (`generate_arch003_mutation_inventory.py`) and is fixed there too.
- **Finding #13: nested-function scope pollution.** The pre-scan collecting
  a function's local `path_vars`/`sql_vars`/`file_handle_vars` used
  `ast.walk(node)`, which descends into nested `def`/`class`/lambda bodies
  -- an inner function's own `x = Path(...)` could leak into the outer
  function's variable-name tracking for the rest of its body. Fixed with
  `_walk_own_scope_only()`, a scope-limited walker that stops at (but still
  yields) nested function/class/lambda boundaries. Regression:
  `tests/test_discover_mutation_sinks.py::ScopeIsolationTests`.
- **New bug found by this review's own test-writing (not in the original
  finding list): `Path(...).open(mode)` was never detected.** The writable-
  `open()` mode-argument-position logic used `args[1]` for both the builtin
  `open(path, mode)` form (correct) and the `Path(...).open(mode)` method
  form (wrong -- for the method form, mode is `args[0]`, since the receiver
  already is the path). `Path(...).open("wb")` was silently treated as
  read-only by default. Fixed by branching on call form. This is exactly
  the value of writing the self-tests the original submission skipped
  (finding #24): the very first attempt at a positive test for this case
  failed against real scanner behavior.

## 4. Blanket Classification Removed (finding #3, the most important correction)

The generator previously classified by file path alone for several files:

```python
if file == "backend/beets_control_agent.py":
    return "ENGINE_NATIVE_BEETS", "", "file-is-engine-boundary"
if file == "backend/beets_client.py":
    return "ENGINE_NATIVE_BEETS", "", "beets-client-proxy"
if file == "routes_submissions.py":
    return "APP_STATE", "", "submissions-app-state"
if file == "routes_jobs.py":
    return "APP_STATE", "", "jobs-app-state"
if file == "routes_lidarr.py":
    return "APP_STATE", "", "lidarr-app-state"
# backend/ support modules defaulted to TRANSACTION_STATE (sql) / NON_MEDIA_FILESYSTEM (fs)
```

This masked exactly the generic Control-Agent mutation surface the wave was
supposed to inspect (finding #4: "engine-owned" is not automatically
"architecture-safe" -- code executing inside the engine container can still
bypass Plan/Review/Apply/Verify/Rollback entirely). Replaced with:

- `backend/beets_control_agent.py`: function-level classification against a
  small table of individually-investigated functions (section 5), plus a
  named-but-unmapped fallback of `NEEDS_REVIEW` for the two giant HTTP
  dispatchers (`do_POST`/`do_DELETE`, ~40+ sinks each) where accurate
  per-endpoint classification needs to know which `if path == "/...":`
  branch a sink sits in -- a real scanner capability gap (endpoint-context
  tracking), not something safe to guess at with a blanket label either
  direction.
- `backend/beets_client.py`: no rule at all needed -- real discovery finds
  **0 sinks** in this file (confirms it is genuinely a pure HTTP proxy, as
  `test_beets_client_is_pure_http_proxy` already asserted). The blanket
  rule was dead code.
- `routes_jobs.py` / `routes_lidarr.py`: real discovery finds **0 sinks**
  in either file today; blanket rules removed as dead code (findings
  #17/#18). If a future change adds a real sink here, it now falls through
  to an honest `NEEDS_REVIEW` instead of an unearned `APP_STATE` pass.
- `routes_submissions.py`: reclassified per-function after actually reading
  each one (section 5), not by file location.
- `backend/` support modules: unmapped modules now fall through to
  `NEEDS_REVIEW` instead of a directory-level default (finding #19).

## 5. Individually Investigated Findings (real production caller tracing, not pattern matching)

- **`PATCH /items/<id>` and `PATCH /albums/<id>` -- CRITICAL, fixed with
  code (findings #6/#7/#38).** `ITEM_EDITABLE_FIELDS` included
  `mb_trackid`/`mb_albumid`/`mb_artistid`/`mb_releasegroupid`;
  `ALBUM_EDITABLE_FIELDS` included `mb_albumid`/`mb_albumartistid`/
  `mb_releasegroupid` -- canonical MusicBrainz identity, directly writable
  through a generic, untransacted SQL UPDATE (no Plan/Apply/Verify/
  Rollback, no TOCTOU, no identity-conflict check). Repo-wide search
  confirmed **zero current production callers** of `beets_client.update_
  item_fields`/`update_album_fields` anywhere in `app.py`, `routes_*.py`,
  or the frontend API client -- the capability was real and reachable but
  not exercised by any feature. Per finding #38's instruction to decide
  the policy now rather than defer it, the identity fields were removed
  from both allowlists; canonical identity mutation must go through a
  controlled transaction family instead. Regression:
  `tests/test_control_agent_field_allowlist.py::test_identity_fields_are_not_editable`.
- **`_handle_delete_album`, `_replace_album_art_locked`,
  `_delete_album_art_locked` -- CRITICAL, confirmed ACTIVE, not fixed this
  pass.** Traced the real caller chain: `app.py`'s `POST /api/albums/<id>/
  remove` and `DELETE`/`POST /api/albums/<id>/art` routes call `beets_
  client.delete_album`/`delete_album_art`/`replace_album_art`, which hit
  these three Control Agent functions directly -- genuine, currently-
  exercised album/artwork deletion and replacement with **no transaction
  boundary at all**, running in parallel with the already-existing,
  properly-controlled `album_maintenance_v1`/`album_artwork_v1` families.
  This is a real, live generic bypass, not dead legacy code. Classified
  `ENGINE_GENERIC_BYPASS` (a new classification, counted toward the
  unresolved ratchet total per finding #27) rather than migrated this pass
  -- see section 7 for why, and section 8 for the Wave 24 recommendation
  this finding drives.
- **`reimport_source_atomic`, `preserve_import_source`**: confirmed (from
  Wave 22's own investigation) as the real backing implementation for
  `import_folder_v1`'s engine-side import. Classified
  `ENGINE_CONTROLLED_TRANSACTION`.
- **`attach_album_mbids._do`** (`routes_submissions.py`): read the full
  function body. A real, deliberate, explicit user-facing "attach
  MusicBrainz IDs" action -- validates every id is a well-formed UUID,
  verifies each item actually belongs to the target album before writing,
  and reads the DB back after the `beet modify`/`write` calls to confirm
  the write actually took effect. Not a hidden bypass: a deliberate
  admin-style engine-native mutation with real before/after verification,
  predating the newer Plan/Apply transaction-family pattern. Classified
  `ENGINE_ADMIN_MUTATION` (new classification for exactly this shape:
  explicit, reviewed, verified, but not a `_v1` family).
- **`_start_acoustid_submit_job._do`**: `beet submit` sends fingerprint
  data to the external AcoustID service; confirmed it does not touch local
  library media or DB. Classified `ENGINE_NATIVE_BEETS`.

## 6. `ALLOWED_COMMANDS` -- Real Usage Evidence, Not Restricted This Pass

`backend/beets_control_agent.py`'s `ALLOWED_COMMANDS` allowlist: `import`,
`update`, `write`, `move`, `modify`, `ls`, `stats`, `fields`, `mbsync`,
`fetchart`, `embedart`, `lastgenre`, `lastimport`, `alt`, `version`,
`config`, `check`, `remove`, `rm`, `submit`, `mbsubmit`.

Read-only: `ls`, `stats`, `fields`, `version`, `config`, `check`. Mutating:
the rest. Real usage evidence (grepped actual `_beet_run`/`subprocess`
call sites in `app.py`): `move`, `write`, `mbsync`, and `modify` are each
invoked from multiple distinct, established features (retag, album rename,
attach-MBID, metadata repair); `mbsubmit`, `lastgenre`, `import`, and
`fetchart` each have at least one real caller. `embedart`, `lastimport`,
`alt`, `remove`, and `rm` were not found invoked anywhere in the searched
call sites -- allowed but currently unexercised by any traced feature,
similar in shape to the PATCH-identity finding but not independently
verified to the same depth this pass.

**Not restricted this pass.** Unlike the PATCH identity fields (zero
production callers, safe and verified to remove), several of these commands
have real, working, multi-call-site production features depending on them.
Restricting `ALLOWED_COMMANDS` without individually verifying every
dependent feature still works risks exactly the kind of hasty breaking
change this review process exists to prevent (the same judgment call Wave
22 made about not ripping out the local import subprocess without a
verified working replacement). This is named as a specific Wave 24 input
(section 8), not silently dropped.

## 7. Why `ENGINE_GENERIC_BYPASS` Was Documented, Not Migrated, This Pass

Migrating `_handle_delete_album`/`_replace_album_art_locked`/`_delete_
album_art_locked`'s callers (`/api/albums/<id>/remove`, `/api/albums/<id>/
art`) onto the existing `album_maintenance_v1`/`album_artwork_v1` families
is genuinely the right fix and, unlike a from-scratch migration, has the
advantage of reusing infrastructure that already exists and is tested.
It was not attempted in this review pass because it touches two live,
user-facing destructive endpoints (whole-album deletion, artwork
replacement) and deserves its own focused implementation-and-test cycle
rather than being folded into an already-large scanner/classification
correction pass. Named explicitly as the top Wave 24 candidate (section 8)
rather than silently left for "some future wave" to rediscover.

## 8. Domain Cluster Matrix (finding #28: workflow boundaries, not sink technology)

`ARCH003_BLOCKER` + `ENGINE_GENERIC_BYPASS` entries (240 total; a single
production function commonly contains more than one sink *technology* --
filesystem move, SQL update, tag write, beet subprocess call -- which is
exactly why these are clustered by the workflow they belong to, not by
technology, per finding #29):

| Domain | Sinks | Representative functions |
| --- | ---: | --- |
| **Album identity/metadata repair** | 55 | `album_fix_metadata`, `match_album`, `_match_tracks_from_mb`(`_shared`), `_run_item_recording_id_restore`, `item_attach_recording`, `album_add_mbids`, `retag_item`, `_mb_release_tracklist_write_disk`, `_save_rgid_resolutions`, `clean_rgid_group_merge`/`_relink`, `_apply_genre_to_album` |
| **Import reconciliation / reimport** | 42 | `reimport_disk`, `import_folder_with_id` (`_retag`, `_cleanup_failed_import_copy`, `_rollback_failed_library_source_import`), `_ai_import_folder`, `_write_import_review_auto_state`, `library_sync_deleted` |
| **Album rename/move/merge** | 24 | `library_merge_artist`, `library_normalize_artists`, `album_rename`, `album_move_to_library`, `album_merge_split_album`, `clean_merge_duplicate_album` |
| **Album deletion/retirement/dedup** | 27 | `_delete_album_ids_from_db`, `apply_album_duplicate_resolver`, `_delete_library_db_rows_under_folder`, `_delete_unmatched_items_in_folder`, `_delete_album_items_under_folder`, `album_deduplicate`, `dedup_cleanup` |
| **Artwork (generic bypass, confirmed active)** | 14 | `_replace_album_art_locked`, `_delete_album_art_locked`, `_repair_album_art` |
| **Library/folder cleanup** | 17 | `apply_folder_placeholder_action_api`, `_root_folder_repair_apply_safe`, `_prune_empty_review_dirs`, `_maintenance_safe_folder_renames`, `_album_cleanup_remove_empty_tree` |
| **AI batch import** | 7 | `_ai_batch_write_state`, `batch_ai_suggest`, `create_unmatched_draft` |
| **Other / small** | 4 | config/browser-password cleanup helpers, Plex client identifier caching |

## 9. Highest-Risk Domain And Wave 24 Recommendation

Ranked by (destructive file risk × DB blast radius × identity mutation ×
automated execution × rollback quality × existing transaction-family
reuse), per finding #30:

1. **Artwork generic bypass (14 sinks, `ENGINE_GENERIC_BYPASS`)** --
   despite the smallest count, this ranks **highest priority**: it is
   *confirmed currently active* in production (not legacy/dormant code),
   has *zero* transaction boundary at all, and the fix requires no new
   family design -- `album_maintenance_v1`/`album_artwork_v1` already
   exist and already handle this domain correctly elsewhere in the
   codebase. Recommended Wave 24 scope: migrate `/api/albums/<id>/remove`
   and `/api/albums/<id>/art` onto the existing engine transaction
   families instead of `_handle_delete_album`/`_replace_album_art_locked`/
   `_delete_album_art_locked`.
2. **Album identity/metadata repair (55 sinks)** -- highest raw count and
   directly mutates canonical MusicBrainz identity via raw `beet modify`/
   SQL, but has *some* built-in safety today (beet's own tag-write
   consistency, and `attach_album_mbids`'s explicit post-write
   verification pattern is a reasonable template to extend). Second
   priority.
3. **Import reconciliation (42 sinks)** -- already partially tracked as
   debt since Wave 22 (`import_folder_with_id`'s local import path); this
   pass's discovery shows the real surface is broader (`reimport_disk`,
   AI import) than previously scoped.
4. Album rename/move/merge, deletion/retirement, library cleanup, AI batch
   import -- real debt, lower urgency than the above three.

## 10. Not Attempted This Pass

- Per-endpoint classification of `ControlAgentHandler.do_POST`/`do_DELETE`'s
  ~40+ sinks each (needs scanner endpoint-context tracking, a real
  capability gap, not a triage shortcut).
- Restricting `ALLOWED_COMMANDS` (section 6 -- real dependent features not
  individually re-verified).
- Migrating the artwork/album-deletion generic bypass (section 7 -- named
  as Wave 24, not silently deferred).
- Full manual review of all 53 `NEEDS_REVIEW` entries (a real, honest
  "not yet triaged with confidence" bucket per finding #21 -- earned
  through investigation where it was tractable this pass, left open where
  it required per-endpoint context this review's tooling does not yet
  have).

## 11. ARCH-003 Status

**In Progress.** Not Done.

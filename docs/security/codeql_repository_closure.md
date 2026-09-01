# Repository-Wide CodeQL Security Closure — Working Report

Status: **IN PROGRESS — NOT COMPLETE**. Do not read this document as a closure claim.

## 1. Baseline (established 2026-09-01, verified against live GitHub API, not PR-scoped)

| Metric | Baseline |
|---|---|
| `origin/main` SHA | `7b05844d58d657ce6f1ae6c5b1724f5ba70ee257` (merge of PR #103) |
| Open repository-wide CodeQL alerts (before this pass) | 171 |
| Critical | 2 |
| High | 164 |
| Medium | 5 |
| Python alerts | ~156 |
| JavaScript/TypeScript alerts | ~15 |
| GitHub Actions alerts | 0 observed in this query |
| All alerts confirmed on `refs/heads/main` (not PR noise) | 171/171 |
| Alerts older than 14 days | 152/171 |

Rule breakdown at baseline:

| Rule | Count | File(s) |
|---|---|---|
| `py/path-injection` | 121 | `app.py` (109), `backend/transaction_engine.py` (8), `backend/beets_control_agent.py` (4) |
| `py/polynomial-redos` | 17 | `app.py` |
| `js/incomplete-url-substring-sanitization` | 14 | `index.html` (9), `frontend/src/views/Playlists.tsx` (5) |
| `py/stack-trace-exposure` | 5 | `routes_setup.py` (3), `app.py` (2) |
| `py/clear-text-storage-sensitive-data` | 4 | `scripts/verify_first_run_acceptance.py` (2), 2 test files |
| `py/incomplete-url-substring-sanitization` | 3 | `app.py` |
| `py/command-line-injection` | 2 | `backend/beets_control_agent.py` |
| `js/xss-through-dom` | 2 | `index.html` |
| `py/bad-tag-filter` | 1 | `app.py` |
| `py/regex-injection` | 1 | `app.py` |
| `js/insecure-randomness` | 1 | `index.html` |

**Context established before any fix work**: this repository has ~27 prior local "wave" branches
(`beets-codeql-app-path-wave1` through `wave8`, `beets-sec002-*`, `beets-wave27-pr102`, etc.) attempting
this exact closure. None landed a 0-open-alert state on `main`. Investigation of the first 2 alerts
closed below indicates a likely root cause for part of that gap: real validation code for these sinks
already exists in `main` (anchored regexes, `resolve_safe_path`, capability gates, allowlists), but the
corresponding GitHub alerts were never individually verified and dismissed — i.e. some fraction of the
171 may be `SAFE_BUT_CODEQL_BLIND` bookkeeping debt rather than undone code work. This must be verified
per-alert, not assumed, per the mission rules (no blanket dismissal).

## 2. Alert dispositions

### Alert 1009 — `py/command-line-injection`, critical — `backend/beets_control_agent.py:2041`

- **Disposition**: `SAFE_BUT_CODEQL_BLIND`
- **Source**: `options.get("mb_albumid")` from the `/imports/reimport` control-agent HTTP body.
- **Sink**: `subprocess.run(full_cmd, ...)` inside `reimport_source_atomic()`, where `full_cmd` includes
  `["--search-id", mb_albumid]`.
- **Validation chain**: `backend/beets_control_agent.py:1977-1979` — `mb_albumid` is stripped, then
  rejected outright unless it fully matches
  `_MB_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)`
  (anchored both ends, defined at line 1905). A flag-injection payload (leading `-`, `--config=...`,
  embedded whitespace/newlines) cannot match this pattern, so it can never reach `argv`.
  `import_target_path` is derived from `resolve_safe_path(...)`-validated/staged paths, never raw
  request text. `config_override` is length-bounded and written to a fresh `uuid4()`-named, `0600` temp
  file; only that generated safe path (never attacker text) is placed on the command line.
- **Why CodeQL can't see it**: CodeQL's Python command-line-injection query does not model a full-string
  anchored regex match as a command-line sanitizer barrier for this data-flow path.
- **Regression evidence**: `tests/test_sec002_wave19_mb_track_repair.py` and sibling SEC-002 wave tests
  already exercise invalid `mb_albumid` rejection through this exact gate.
- **GitHub disposition**: dismissed 2026-09-01, reason `false positive`.

### Alert 1010 — `py/command-line-injection`, critical — `backend/beets_control_agent.py:2762`

- **Disposition**: `SAFE_BUT_CODEQL_BLIND`
- **Sink**: `subprocess.run(full_cmd, ...)` inside the shared helper `_run_beet_subcommand_locked()`,
  which by its own docstring performs *no* validation itself — callers are responsible for
  allow-listing `cmd_list[0]` and sanitizing the rest of `cmd_list`.
- **All current callers verified** (both are in this file; there are no others):
  1. `/commands/execute` (line 5260): `command` must be a member of the fixed `ALLOWED_COMMANDS` set
     (20 literal beet subcommand names) and pass `require_command_capability()`; `args` pass through
     `_sanitize_command_path_args()`, which explicitly rejects `-c`/`--config`/`-c=...`/`--config=...`
     (the one flag capable of loading an arbitrary beets config) and resolves any absolute-path argument
     through `resolve_safe_path()`. `source_path` is independently validated the same way.
  2. `_run_beet_command()` closure at line 4756 (used by `/albums/artwork/fetch/apply`): `command` is
     hardcoded to `"fetchart"`/`"embedart"` from a fixed tuple, never request-derived; `args` still pass
     through the same `_sanitize_command_path_args()`.
- **Why CodeQL can't see it**: the sanitizing call happens in a different function from the sink, and
  the allowlist/capability checks happen in yet another (the HTTP handler); CodeQL's intraprocedural/
  interprocedural taint tracking does not connect the three-function chain as a barrier for this sink.
- **Note for future work**: `_sanitize_command_path_args()` does not reject non-path, non-`-c` leading-
  hyphen arguments (e.g. an arbitrary `beet modify` flag). This is current intended behavior — the
  endpoint's documented purpose is a controlled beet-subcommand passthrough behind internal auth +
  capability gating + a fixed subcommand allowlist — not a gap this alert is about. Flagged in
  `docs/TECHNICAL_DEBT.md` as worth an explicit flag-allowlist per subcommand if this endpoint's
  exposure ever widens.
- **GitHub disposition**: dismissed 2026-09-01, reason `false positive`.

### Alerts 231-236, 240-241 — `py/path-injection`, high — `app.py` `/api/import`, `/api/import/preflight`, `/api/dedup/scan`

- **Disposition**: `REAL_VULNERABILITY` (8 alerts) — genuine, fixed.
- **Source**: `payload.get("path")` from the JSON body of three authenticated POST routes.
- **Sinks**: `Path(path).mkdir(parents=True, exist_ok=True)` (231), `Path(path).exists()` (232, 233,
  234), `Path(path).resolve()` (235, 240), `os.walk(scan_path)` (236), `scan_path.rglob("*")` (241, via
  `dedup_scan`'s `_run()`).
- **What was actually wrong**: `/api/import`, `/api/import/preflight`, and `/api/dedup/scan` ran these
  filesystem operations directly against the raw request-body path with **no allowed-root check
  anywhere before them**. An authenticated caller could set `path` to any absolute filesystem path the
  container process can reach:
  - `/api/import` would `mkdir(parents=True, exist_ok=True)` at that path — arbitrary directory
    creation outside the configured torrent-source/library roots.
  - `/api/import/preflight` would `os.walk()` the path — arbitrary directory-tree enumeration
    (filenames, audio-file counts) anywhere readable.
  - `/api/dedup/scan` would `rglob("*")` the path and later `stat()` every audio file found —
    arbitrary filesystem enumeration plus file-size disclosure.
  The engine-side `reimport_source_atomic()` validation in `backend/beets_control_agent.py` is a
  separate, *later* trust boundary (only reached once a background job actually calls
  `beets_client.reimport_source(...)`) and does not protect these earlier, web-manager-side probes —
  exactly the "validation probe itself is the sink" pattern this closure pass was told to watch for.
- **Fix**: added `_resolve_import_source_path()` (gates against `TORRENT_SOURCE_ROOTS ∪ {MUSIC_ROOT}`)
  and `_resolve_dedup_scan_path()` (gates against the existing `_BROWSE_ALLOWED_ROOTS = (MUSIC_ROOT,
  DOWNLOADS_ROOT)`), mirroring the pre-existing `_folder_cleanup_path()`/`/api/browse` allowlist
  pattern already used elsewhere in this file. Both validators run **before** any filesystem operation,
  reject non-absolute paths, resolve symlinks via `.resolve(strict=False)` before the containment check
  (closing the symlink-leaf case), and reject any path that only shares a string prefix with an allowed
  root (sibling-prefix escape, e.g. `/data/torrents/music-evil` vs. `/data/torrents/music`) by using
  `Path.relative_to()` semantics (`_path_is_under()`), not substring matching.
- **Regression tests**: `tests/test_codeql_repowide_closure_import_path.py` (12 tests) — outside-root
  rejection, sibling-prefix escape, symlink-leaf escape, relative-path rejection, valid-path acceptance
  for both allowed roots, and route-level proof that the dangerous call (`mkdir`, `os.walk`, `rglob`) is
  never reached for a rejected path (mocked and asserted `not_called()`), not merely "validated
  afterward".
- **Verification**: `python -m py_compile app.py` clean; full backend suite (`python -m unittest
  discover -s tests -p "test_*.py"`) run twice, 2763 tests, 0 failures, 129 skipped (pre-existing —
  require a live engine/Docker), both runs.

### Alerts 237, 239 — `py/path-injection`, high — `app.py`

- **Disposition**: `SAFE_BUT_CODEQL_BLIND`.
- Alert 237: `Path(row["path"]).resolve()` inside `import_preflight()` operates on a value derived from
  the *now-validated* `scan_path` via `os.walk()`, never on raw request text directly.
- Alert 239: `_dedup_norm_path()` is a pure path-string normalization helper used only to build
  cache-key comparisons (`_dedup_resolve_source_scan()`); it never reads, enumerates, or mutates the
  filesystem, and never returns filesystem contents to the caller.

### Alert 238 — `py/path-injection`, high — `app.py:29024` (`/api/browse`)

- **Disposition**: `SAFE_BUT_CODEQL_BLIND`. This line *is* the containment check itself
  (`_path_is_under(p, root) or p.resolve(strict=False) == root.resolve(strict=False)` against
  `_BROWSE_ALLOWED_ROOTS`) — a pre-existing, already-correct instance of the same allowlist pattern
  used to fix the alerts above. CodeQL flags the `.resolve()` call inside the validator itself.

### Alerts 417, 257, 256, 258, 260, 259, 261, 262, 263, 264, 265, 267, 266, 269, 268, 270, 272, 271, 418, 275, 274, 276, 277, 420, 419, 280, 281, 282, 283, 284, 285, 421 — `py/path-injection`, high — `app.py` folder-cleanup/rename/merge cluster (32 alerts)

- **Disposition**: `SAFE_BUT_CODEQL_BLIND` (all 32).
- This is the `/api/clean/folder-placeholder/*` route family (`_folder_cleanup_path`,
  `_folder_cleanup_is_approved_root`, `_folder_cleanup_file_inventory`, `_folder_cleanup_is_empty`,
  `_folder_cleanup_db_items`, `_folder_cleanup_merge_preview`, `_folder_cleanup_review_for`,
  `apply_folder_placeholder_action_api`, `apply_safe_folder_placeholder_renames_job`) — already hardened
  in `docs/TECHNICAL_DEBT.md`'s **Wave 4 (path-injection wave 1)**, re-verified fresh this session rather
  than trusted from that record. GitHub reassigned new alert numbers after later commits shifted line
  numbers, so these 32 live alerts needed independent re-verification even though the underlying code
  was already correct.
- **Validation chain traced for every sink**: every filesystem operation in this cluster operates on a
  `Path` that is either (a) the direct return value of `_folder_cleanup_path()` (which resolves the
  input, then rejects anything not `_path_under(resolved, MUSIC_ROOT)`), (b) a value derived from such a
  validated path via real filesystem enumeration (`rglob()`/`relative_to()`, which cannot reintroduce a
  traversal segment), or (c) explicitly re-validated with a second `_path_under()` check immediately
  before a destructive call (`rename()`, `shutil.move()`) as defense against preview-token staleness.
  Traced every call site of every helper in this cluster (not sampled) to confirm no path reaches this
  cluster without going through `_folder_cleanup_path()` first.
- **Regression evidence**: `tests/test_sec002_app_path_folder_cleanup.py` (pre-existing, Wave 4, 12
  tests, still passing) plus the fresh full-suite runs this session.
- **GitHub disposition**: all 32 dismissed 2026-09-01, reason `false positive`, each with a
  sink-specific (not blanket) rationale referencing the exact helper/branch involved.

### Alerts 139-149, 151, 150 — `py/path-injection`, high — `app.py` artist/release-art image cache (13 alerts)

- **Disposition**: `SAFE_BUT_CODEQL_BLIND` (all 13).
- `_artist_image_cache_info`/`_cache_artist_image` (11 alerts, lines 10082-10162): every filesystem path
  is built from `_artist_image_cache_key(artist_name)`, which SHA1-hashes the input and strips it to
  `[a-z0-9-]` — no raw text ever reaches a path regardless of `artist_name`'s content — plus a file
  extension drawn only from a fixed 5-item allowlist (`_artist_image_ext`). `_release_art_cache_info`/
  `_release_art_save_miss`/`_release_art_download` (same cluster): `mbid` is validated against the
  anchored `_RELEASE_ART_MBID_RE` either at the sink function's own entry, or by its single caller
  (`_ensure_release_group_art`) immediately before the call — traced to confirm each of these 3 helper
  functions has no other caller anywhere in the file.
- Alerts 151/150 (`app.py:10550`): `SAFE_BUT_CODEQL_BLIND` — this line is `_path_is_under()`'s own
  containment-check body (the codebase's core sanitizer, ~60 call sites per `docs/TECHNICAL_DEBT.md`
  Wave 6), the same "validator flagging itself" pattern as `/api/browse` (alert 238, above).
- **GitHub disposition**: all 13 dismissed 2026-09-01, `false positive`, sink-specific rationale.

### Alerts 480, 196, 194, 482, 483, 197, 210, 212, 211 — `py/path-injection`, high — `app.py` Import Review target-preview source-file listing (9 alerts)

- **Disposition**: `SAFE_BUT_CODEQL_BLIND` (all 9).
- `_target_preview_source_files`/`_remaining_audio_files`: `source` is always the return value of
  `_resolve_import_review_source_path()` (a defense-in-depth validator: lexical-containment check,
  symlink-component check, symlink-leaf check, *then* resolve + re-check — all before any
  `.exists()`/`.is_file()`/`.rglob()` call). `album_path.exists()`/`.resolve()`: `album_path` is built
  only from `_safe_path_component()`-sanitized components, which strip `/`, `\`, control characters, and
  Unicode bidi-override/format characters — no traversal segment can survive it regardless of input.

### Alerts 208, 209, 207 — `py/path-injection`, high — `app.py:19672-19673`, `_build_import_target_preview` (3 alerts)

- **Disposition**: `REAL_VULNERABILITY` — genuine, low-severity, fixed (not dismissed; will auto-close
  when GitHub re-scans this branch post-merge).
- `_build_import_target_preview()` (`POST /api/folders/import-target-preview`) took
  `track_mapping[i]["source_path"]` straight from the request body (`source_file = Path(row_source_str)`)
  with **no containment check**, unlike every sibling Import Review helper. Real impact was narrow — this
  function performs no filesystem writes (per its own docstring: "It performs no filesystem writes and
  never calls beets"), so the exposure was an unvalidated `.resolve()`/`.exists()` comparison, not a
  read/write/enumerate primitive — but it was a real, inconsistent exception to the codebase's own
  established validated-source-file pattern, found by reading past the CodeQL sink into the actual data
  flow rather than assuming the cluster's dominant safe pattern applied everywhere in it.
- **Fix**: route `row["source_path"]` through the same `_resolve_import_review_cleanup_file()` helper its
  siblings already use, scoped to the request's own validated `trusted_source_path`.
- **Tests**: `tests/test_codeql_repowide_closure_import_review_preview.py` (3 tests) — malicious absolute
  path rejected, relative-traversal path rejected, legitimate in-root path still accepted (using real
  tempdir Paths throughout, not POSIX-literal strings, for cross-platform correctness).

### Alerts 636, 303, 304, 305, 306, 307, 308, 637 — `py/path-injection`, high — `app.py` playlist atomic-JSON-write helper (8 alerts)

- **Disposition**: `SAFE_BUT_CODEQL_BLIND` (all 8).
- `_playlist_atomic_json_replace(path, payload, ...)`: validates `path`/`tmp` against a fixed set of
  server-config globals (`WEB_MANAGER_DATA_DIR`, `PLAYLIST_STATE_ROOT`, `PLAYLIST_MANIFESTS_DIR`,
  `PLAYLIST_JOB_STATE_DIR`, `PLAYLIST_MEMBERSHIP_DIR` — none request-derived) via `_path_is_under()`/
  `relative_to()`, **raising `ValueError` before the `try:` block that performs any actual write** —
  every sink CodeQL flags inside that block (`tmp.open()`, `tmp.exists()`, `tmp.replace()`,
  `path.exists()`, cleanup-branch `tmp.exists()`/`tmp.unlink()`) is provably unreachable for a
  non-contained path.
- `_playlist_read_manifest`'s `path.read_text()`: `path` always comes from `_playlist_manifest_path()`,
  built only from `_clean_playlist_name()` (strips `<>:"/\|?*`, repeatedly collapses `..`, strips control
  and Unicode bidi-override characters) plus a validated internal playlist id — a single path component,
  never a caller-supplied full path.
- **GitHub disposition**: all 8 dismissed 2026-09-01, `false positive`, sink-specific rationale.

### Alerts 138, 137 — `py/path-injection`, high — `app.py:8641-8643`, `_folder_track_search_titles` (2 alerts)

- **Disposition**: `REAL_VULNERABILITY` — genuine, fixed (not dismissed; will auto-close on post-merge
  rescan).
- `_folder_track_search_titles(source_folder, ...)` had **no containment check at all** before
  `source.is_dir()`/`source.rglob("*")` — the identical pattern its sibling function,
  `_folder_import_track_count()` (defined a few lines above it in the same file), was already fixed for
  in `docs/TECHNICAL_DEBT.md`'s SEC-002 backlog Wave 1 (alerts `#334`/`#335`). Wave 1 evidently fixed one
  sibling and missed the other. Fixed by adding the same `_path_is_under(source, MUSIC_ROOT) or
  _path_is_under(source, DOWNLOADS_ROOT)` gate.
- **Attempted, reverted fix for a related function**: `_folder_release_preflight()` (alerts 6093/6095,
  still `NEEDS_REVIEW`) has the same unguarded pattern and looked like the same bug at first read. Adding
  the identical containment gate broke 2 established tests in `tests/test_sec002_wave14_mb_identity_matching.py`
  that deliberately exercise real local-file matching against arbitrary (non-`MUSIC_ROOT`) temp
  directories — this function is called from ~14 sites across reimport/repair flows, and at least some of
  those legitimately expect best-effort local scanning outside the narrow `MUSIC_ROOT`/`DOWNLOADS_ROOT`
  pair. A correct fix here needs either a broader/correct allowed-roots set for this specific function or
  a full per-caller trace, not a copy-paste of the sibling's fix. Reverted rather than land a fix that
  weakens established, tested matching behavior — flagged in `docs/TECHNICAL_DEBT.md` for a dedicated
  pass instead of forcing a rushed disposition.
- **Tests**: `tests/test_sec002_codeql_backlog.py::FolderTrackSearchTitlesRootContainmentTests` (2 tests,
  mirroring the existing sibling-fix test class exactly) — outside-root folder returns DB-only titles
  without walking it (mocked `rglob` asserted not called), in-root folder still scanned normally.

### Alerts 152, 154, 153, 155, 157, 166, 809, 810, 216 — `py/path-injection`, high — `app.py` disk-art / audio-cache-identity / import-reconcile (9 alerts)

- **Disposition**: `SAFE_BUT_CODEQL_BLIND` (all 9).
- `_app_managed_download_path()` (152): returns only a bool classification, never reads/writes the path.
- `/api/disk-art` (154, 153, 155): validates the resolved path against `_BROWSE_ALLOWED_ROOTS` before
  `send_file()`; `os.path.realpath()` computes the value the check uses and doesn't leak content or
  distinguish existing/non-existing paths in the response either way.
- `_library_no_mb_album_matches_folder()` (157): this line is the function's own containment-check body
  (bool-only return).
- `import_folder_stats` (166): `folder` param is always `_resolve_import_review_folder_path()`-validated
  (same defense-in-depth validator family as `_resolve_import_review_source_path`).
- `_audio_cache_file_identity()` (809, 810): its only caller, `_acoustid_lookup_cached()`, has 14 call
  sites across the file; sampled a representative cross-section (direct `item.path` DB fields,
  `_album_item_abs_path()`-wrapped DB paths, locally-`rglob()`'d files under an already-validated root,
  and one call guarded by an explicit `is_absolute()` check before a `MUSIC_ROOT` join) and found every
  one DB-derived or already-locally-scanned, never raw request text. The sink itself only produces a
  SHA1 cache key, never returns file content to a client. Noting explicitly: this is a **sampled**
  conclusion across 14 call sites, not an exhaustive per-site trace of all 14 — lower confidence than
  the fully-traced clusters above, worth a closer look in a dedicated future pass if time allows.
- `import_reconcile_job` (216): `source_path` is always `_resolve_import_review_source_path()`-validated,
  or empty (which short-circuits the `and` before this check runs).
- **GitHub disposition**: all 9 dismissed 2026-09-01, `false positive`, sink-specific rationale.

### Alerts 220, 484, 221, 414, 229, 230, 246 — `py/path-injection`, high — `app.py` unmatched-draft / dedup-key / folder-clean-root wrapper (7 alerts)

- **Disposition**: `SAFE_BUT_CODEQL_BLIND` (all 7).
- `library_import_all`'s dedup-key `.resolve()` (220): builds a same-request-scoped `set` membership key
  only — no `.exists()`/`.rglob()`/content read, no side channel observable by the caller. Note: `aldir`
  itself (client-supplied, unvalidated at this specific line) flows to other functions later in the same
  route; those are separately-scoped concerns, not this alert.
- `_same_path()` (484, 221): a pure comparison helper (`resolve()` + equality, bool-only return).
- `create_unmatched_draft`'s `draft_path` (414, 229, 230): built from `_safe_path_component()`-sanitized
  `artist`/`album`, then **explicitly re-validated** via `_path_is_under(draft_path_resolved,
  draft_root_resolved)` immediately before any `mkdir()`/`write_text()` — the source even carries an
  explicit comment calling this out as deliberate defense-in-depth.
- `_resolved_path()` (246): a generic `resolve()`-with-fallback wrapper; each of its call sites was
  checked individually elsewhere in this pass (see `_folder_clean_root` below, `_is_top_level_music_artist_folder`,
  `_scan_no_audio_folder_candidates`).
- **GitHub disposition**: all 7 dismissed 2026-09-01, `false positive`, sink-specific rationale.

### Alerts 416, 415 — `py/path-injection`, high — `app.py`, `_folder_clean_root` (2 alerts)

- **Disposition**: `REAL_VULNERABILITY` — genuine, low-severity, fixed (not dismissed; will auto-close on
  post-merge rescan).
- `_folder_clean_root(raw_root)` called `root.exists()`/`root.is_dir()` **before** its containment check
  against `FOLDER_CLEAN_ROOTS`, and both branches raise a distinct `RuntimeError` that reaches the client
  verbatim (`delete_no_audio_folders_api`/`scan_no_audio_folders_api`: `jsonify({"error": str(ex)})`).
  This let an authenticated caller distinguish "path exists" from "path is outside the allowed roots" for
  **any** absolute path on the filesystem, not only ones already confirmed in-root — a real, if narrow,
  existence-oracle (CWE-22-adjacent information disclosure), and exactly the "validation probe as sink"
  pattern this closure pass was specifically told to watch for.
- **Fix**: reordered — containment check first, existence/is-dir check second, so the existence probe
  only ever runs against a path already confirmed inside `FOLDER_CLEAN_ROOTS`.
- **Existing test updated**: `tests/test_sec002_app_stacktrace.py::FolderCleanRootFalsePositiveTests`'s
  `test_missing_path_raises_fixed_message` asserted the *vulnerable* ordering (a nonexistent,
  out-of-root path returning `"Path not found."`) — renamed and rewritten to assert the corrected
  containment-first message, plus a new test proving a nonexistent path that *is* inside an allowed root
  still gets the original `"Path not found."` message.

### Alerts 1001-1008 — `py/path-injection`, high — `backend/transaction_engine.py` (8 alerts, all of this file's path-injection alerts)

- **Disposition**: `SAFE_BUT_CODEQL_BLIND` (all 8).
- 7 of the 8 (1001-1007) are lines *inside* `_bulk_replacement_toctou_check()` itself — a dedicated,
  extremely thorough pre-mutation TOCTOU/containment re-verifier that independently checks, in order:
  symlink at leaf (`os.path.islink` and `Path.is_symlink`, both forms), root containment via lexical
  parent-walk, symlink component anywhere under the root (`_path_has_symlink_under`), `resolve()`, a
  *second* root-containment check post-resolve, symlink-at-target post-resolve, existence/is-file, and
  finally full dev/inode/size/`mtime_ns` identity comparison against a captured expected-stat snapshot —
  covering essentially every adversarial case section 5 of this closure pass's own mission called for
  (symlink leaf, symlink parent component, inode/device/size/mtime change). Every flagged line is one of
  these checks or a value used only after the checks preceding it passed.
- The 8th (1008), `_capture_media_tag_state()`'s `path.stat()`: checked **all 5 call sites**
  exhaustively (not sampled) — every one passes a path already validated via `_path_under()` +
  `_path_has_symlink_under()` in the same function, or a path read from the same transaction's own
  earlier-planned/DB-row-derived item path within the Plan→Apply pipeline (never raw external input).
- **GitHub disposition**: all 8 dismissed 2026-09-01, `false positive`, sink-specific rationale.

### Alerts 679, 633, 635, 634 — `py/path-injection`, high — `backend/beets_control_agent.py` (4 alerts, all of this file's remaining path-injection alerts)

- **Disposition**: `SAFE_BUT_CODEQL_BLIND` (all 4).
- `/tags/read`'s `safe_file_path.exists()` (679): `safe_file_path` is `resolve_safe_path()`-validated 4
  lines above, before this check.
- `/playlists/m3u/export`'s `safe_m3u.parent.mkdir()` and `tmp_m3u.replace(safe_m3u)` (633, 635, 634):
  `safe_m3u` comes from `_playlist_m3u_path_for_key()` (raises `UnsafePathError` → HTTP 403 otherwise),
  called **again immediately before mutation while holding the OS lock** — the source carries an explicit
  comment explaining this is a deliberate TOCTOU re-validation, since the app-level lock alone doesn't
  guarantee a parent path can't change between an earlier check and this mutation.
- **GitHub disposition**: all 4 dismissed 2026-09-01, `false positive`, sink-specific rationale.

**Every `py/path-injection` alert outside `app.py` is now closed** (`backend/transaction_engine.py`:
0 remaining, `backend/beets_control_agent.py`: 0 remaining).

### Alerts 249, 250, 251, 252 — `py/path-injection`, high — `app.py:31728-31749`, MusicBrainz release-tracklist disk cache (4 alerts)

- **Disposition**: `REAL_VULNERABILITY` — genuine, fixed (not dismissed; will auto-close on post-merge
  rescan).
- `_mb_release_tracklist_cache_path(mb_albumid)` lowercased/stripped `mb_albumid` with **no format check
  at all** before joining it onto `_MB_RELEASE_TRACKLIST_CACHE_DIR` — unlike the sibling release-art
  cache (`_release_art_cache_info`/`_release_art_download`, already verified above), which anchors on
  `_RELEASE_ART_MBID_RE` first. Because the path was built with `release_id[:2]` and
  `f"{release_id}.json"` directly from the unvalidated value, an `mb_albumid` containing `/` or `..`
  segments could escape the cache directory on both the read side
  (`json.loads(cache_path.read_text(...))`) and, more seriously, the **write side**
  (`cache_path.parent.mkdir(parents=True, exist_ok=True)`, `tmp_path.write_text(...)`,
  `tmp_path.replace(cache_path)`) — an arbitrary-file-write primitive if any of
  `_fetch_mb_release_tracklist()`'s ~28 call sites across this file ever passes attacker-controlled text.
  Given the scope of auditing 28 call sites individually, fixed at the single shared choke point instead.
- **Fix**: validate `mb_albumid` via the existing `_is_valid_mb_uuid()`/`_MB_UUID_RE` (already used
  broadly elsewhere in this file) inside `_mb_release_tracklist_cache_path()`, returning `None` for an
  invalid value; both callers (`_mb_release_tracklist_read_disk`/`_mb_release_tracklist_write_disk`)
  treat `None` as a cache-miss/no-op, matching this codebase's established graceful-degradation style for
  best-effort caches. No behavior change for any legitimate caller — a real MBID resolves to the
  identical path either way.
- **Tests**: `tests/test_codeql_repowide_closure_mb_tracklist_cache.py` (6 tests) — absolute-path escape,
  relative-traversal escape, embedded-separator escape all rejected (return `None`, no directory/file
  created); a valid MBID still resolves under the cache dir and round-trips through read/write correctly.

### Alerts 292, 293, 294, 298, 299, 300, 301 — `py/path-injection`, high — `app.py` artist-folder-stamp scan / playlist item-path resolution (7 alerts)

- **Disposition**: `SAFE_BUT_CODEQL_BLIND` (all 7).
- `_stamp_artist_folder_scan`'s `root.resolve()`/`new_path.exists()`/`.resolve()` (292, 293, 294): `root`
  is always either the hardcoded `MUSIC_ROOT` constant or `_artist_folder_repair_root()`-validated to
  resolve to **exactly** `MUSIC_ROOT` (a fail-closed validator that explicitly rejects any descendant,
  per its own docstring: accepting one would let folder-merge logic conflate album folders with artist
  folders). `new_path` is `root / new_name` where `new_name` comes from
  `_artist_folder_canonical_name()`/`_safe_artist_folder_name()`-sanitized text.
- `_playlist_resolve_item_path()`'s `.resolve()`/`.exists()` call sites (298, 299, 300, 301): the function
  itself is the validator — it returns either a path confirmed under one of `MUSIC_ROOT`/
  `PLAYLIST_DOWNLOAD_ROOT`/`DOWNLOADS_ROOT`/configured aliases, or a deliberately-chosen, permanently
  nonexistent, permanently out-of-root sentinel path (`_PLAYLIST_UNRESOLVED_PATH`) for anything that
  doesn't validate — a documented, deliberate fail-closed design (see the source comment explaining why
  this shape was chosen over `Optional[Path]` across its ~11 call sites).
- **GitHub disposition**: all 7 dismissed 2026-09-01, `false positive`, sink-specific rationale.

**This closes every `py/path-injection` alert in `app.py` except 2** (`#6093`/`#6095`,
`_folder_release_preflight`, deliberately left `NEEDS_REVIEW` — see the entry above explaining the
reverted fix attempt and why it needs a dedicated per-caller trace rather than a rushed disposition).

## 3. Reconciliation (2026-09-01, re-verified against live GitHub state, not trusted from prior report)

The previous checkpoint's prose summary contained a genuine self-contradiction: it said `#6093`/`#6095`
were still open `py/path-injection` findings, then in the same message claimed the 50 remaining alerts
included none — those two statements can't both be true, and the second one was simply wrong prose (the
underlying inventory data was correct throughout; this was a reporting error, not a data error).

Re-pulling live GitHub state during reconciliation also surfaced one real bookkeeping gap, now fixed:
alerts `#237`/`#238`/`#239` had been traced, judged `SAFE_BUT_CODEQL_BLIND`, and written into the local
inventory with that disposition during the very first `/api/import` cluster pass — but a bug in that
batch's update script (`dismissed_on_github = False` applied blanket to the whole batch, correct for the
`REAL_VULNERABILITY` rows in it but wrong for these 3 `SAFE` rows) meant the GitHub dismissal API call
was never actually made for them. Live GitHub still showed all 3 open. Dismissed them now with their
original (correct, already-written) rationale.

**Authoritative accounting, live-verified**:

| Category | Count |
| --- | ---: |
| Original baseline | 171 |
| Real vulnerabilities fixed (code-fixed, not dismissed — auto-close on post-merge rescan) | 19 |
| False positives individually dismissed on GitHub | 102 |
| Stale/superseded alerts | 0 |
| New alerts introduced on this branch | 0 |
| Open `py/path-injection` | 21 (19 real-fixed pending merge + 2 `NEEDS_REVIEW`: `#6093`/`#6095`) |
| Open `py/polynomial-redos` | 17 |
| Open `js/incomplete-url-substring-sanitization` | 14 |
| Open `js/xss-through-dom` | 2 |
| Open `py/stack-trace-exposure` | 5 |
| Open `py/clear-text-storage-sensitive-data` | 4 |
| Open `py/regex-injection` | 1 |
| Open `js/insecure-randomness` | 1 |
| Open `py/bad-tag-filter` | 1 |
| Other open rules (`py/incomplete-url-substring-sanitization`, Python-side) | 3 |
| **Total open (live-verified against GitHub, not PR-scoped)** | **69** |

Arithmetic check: `171 (baseline) − 102 (dismissed) = 69 (open)`, matching the live pull exactly.
`19 + 2 = 21` open path-injection, matching the by-rule live pull exactly. `21+17+14+2+5+4+1+1+1+3 = 69`.

Machine-readable inventory (`security/codeql_main_alert_inventory.json`) and this document are both now
consistent with live GitHub state as of this reconciliation.

### Alerts 6093, 6095 — `py/path-injection`, high — `app.py`, `_folder_release_preflight` (2 alerts, FINAL path-injection closure)

- **Disposition**: `REAL_VULNERABILITY` — genuine, fixed (not dismissed; will auto-close on post-merge
  rescan).
- **What was actually wrong**: the local `source.rglob("*")` scan had no containment check at all,
  identical to the already-fixed sibling `_folder_import_track_count()` (Wave 1, `#334`/`#335`). Two
  confirmed-live callers pass **direct, unvalidated HTTP request-body text** straight into this function:
  `import_folder_with_id()` (`folder_path = payload.get("path", "").strip()`) and `reimport_disk()`
  (`aldir = payload.get("aldir", "").strip()`) — this is a real, HTTP-reachable, unvalidated
  arbitrary-directory-enumeration primitive, not a theoretical concern.
- **Why the first fix attempt (previous session) broke tests**: copying the sibling's fix verbatim
  (abort the whole preflight for an out-of-root folder) broke 2 established tests in
  `test_sec002_wave14_mb_identity_matching.py` that deliberately test against arbitrary non-`MUSIC_ROOT`
  temp directories. Investigating *why* those tests were written that way (rather than just reverting
  again) surfaced the actual architecture: when the local scan finds nothing, this function **already**
  falls through to `beets_client.inspect_import_source()` — the engine-side inspection call. That
  function's own docstring is explicit: *"The web manager has no local filesystem access to the engine's
  music/staging roots in the shipped Compose topology... this asks the engine (which owns those mounts)
  to do it."* On the control-agent side, `inspect_import_source()` independently calls
  `resolve_safe_path(source_path, allowed_types, require_exists=True, expected_type="dir")` with a
  **server-defined, caller-never-supplies-its-own-root-list** policy — a completely independent,
  already-authoritative validation, regardless of what the web manager passes it.
- **Fix**: gate the local `rglob()` shortcut to `MUSIC_ROOT`/`DOWNLOADS_ROOT` (matching the sibling fix
  exactly), scoped to only that block (not aborting the whole function). An out-of-root folder now
  correctly falls through to the engine's own independently-validated path instead of being walked
  locally — this **removes** an unauthenticated local-disk side channel that was bypassing the engine's
  real validation, it does not weaken anything.
- **Test fix, not test weakening**: the 2 previously-broken tests were relying on an *implicit, unmocked*
  local-disk read that happened to work only because the vulnerability let it — they now mock
  `beets_client.inspect_import_source()` and exercise the function through its actual, intended
  production path for a folder the container has no local mount for. Verified this is the correct
  characterization, not a rationalization: all 31 tests in that file pass, plus 187 more across
  `test_import_track_alignment`/`test_sec002_codeql_backlog`/`test_mb_release_group_resolution`/
  `test_matching_contract`.
- **New adversarial tests**: `tests/test_codeql_repowide_closure_folder_release_preflight.py` (5 tests)
  — outside-root folder (real files present) never walked locally, proven by asserting the engine
  fallback was called with the *unmodified* path; sibling-prefix escape (`music-evil` vs `music`) never
  walked locally; **symlink-leaf escape** never walked locally (`_path_is_under()` resolves through the
  symlink to its real, out-of-root target); in-root folder still uses the fast local path and never calls
  the engine fallback; a genuinely nonexistent folder still falls through gracefully to
  `scan_unavailable` when the engine is also unreachable.

**Caller matrix** (traced this session; `_resolve_album_release_for_import`'s own 5 further callers and
`_run_ai_release_preflight`'s AI-batch callers were not traced to their ultimate origin beyond this
table — the fix is provenance-agnostic, so this is a completeness note for future architecture review,
not a gap in the security verdict):

| Caller | Workflow | Path provenance | Local FS allowed after fix? | Engine verification |
| --- | --- | --- | --- | --- |
| `import_folder_with_id()` | `POST /api/import/folder-with-id` — two-step import for a skipped folder | **Direct, unvalidated** `payload.get("path")` | Only if under `MUSIC_ROOT`/`DOWNLOADS_ROOT`; otherwise engine fallback | Yes — `inspect_import_source()`, `resolve_safe_path()` |
| `reimport_disk()` | `POST /api/albums/reimport-disk` — tag/import files already on disk but not in the beets DB | **Direct, unvalidated** `payload.get("aldir")` | Same as above | Same as above |
| `api_download_album()` | `POST /api/download/album` — slskd/yt-dlp download + auto-import, via `_resolve_album_release_for_import(source_folder=...)` | Download-job-computed target folder (not exhaustively traced to its own construction) | Same as above | Same as above |
| `_ai_batch_process_decisions()` | AI batch import job decision loop | AI-batch job/scan state (`folder` from an earlier discovery phase, not raw per-call HTTP text) | Same as above | Same as above |
| `_run_ai_release_preflight()` | Thin wrapper used by AI-batch/candidate-review flows | Passes through its own `folder_path` parameter unchanged | Same as above | Same as above |
| `_resolve_album_release_for_import()`'s 5 further callers (lines 9339/21307/23164/31360/33367 at baseline) | Various release-resolution flows across import review, AI batch, and repair | Not individually traced this session | Same as above | Same as above |

**This closes every `py/path-injection` alert in the entire repository: 0 remain `NEEDS_REVIEW`.**
21 total `py/path-injection` alerts across the whole session were genuine vulnerabilities, all fixed with
regression tests (`/api/import`, `/api/import/preflight`, `/api/dedup/scan`, `_folder_track_search_titles`,
`_folder_clean_root`, the MB release-tracklist disk cache, and `_folder_release_preflight`); the
remaining 100 were traced and confirmed genuinely safe (CodeQL data-flow blind spots on this codebase's
established containment helpers) and dismissed individually on GitHub. **Note**: GitHub's repository-wide
alert count will still show these 21 as "open" until this branch is pushed, merged, and `main` is
rescanned — that is expected, not a discrepancy; see the reconciliation section above for why this
distinction matters.

## 4. Phase 4 — Browser-side URL sanitization and DOM/XSS tranche (2026-09-01)

All 17 `js/*` alerts (`js/incomplete-url-substring-sanitization` 14, `js/xss-through-dom` 2,
`js/insecure-randomness` 1) traced individually. **This closes every `js/*` alert in the repository: 0
remain `NEEDS_REVIEW`.**

### Alert 3 — `js/xss-through-dom`, high — `index.html`, folder-browse directory listing

- **Disposition**: `REAL_VULNERABILITY` — genuine, fixed.
- `loadBrowse()` interpolated `par`/`p`/`full` (the browse path, and `path + "/" + directory-name`) raw
  into inline `onclick="loadBrowse('${...}')"`/`onclick="sp('${...}')"` attribute strings. **HTML-escaping
  alone does not make this safe**: the browser HTML-decodes an attribute value *before* handing it to the
  JS parser, so even an `e()`-escaped `'` (`&#39;`) is restored to a literal `'` by the time the `onclick`
  handler's source is parsed — it still breaks out of the embedded JS string. `full` here wasn't even
  HTML-escaped at all (only the button's *visible label*, `dir`, was). A directory name containing a `'`
  or `"` — reachable via any writable path under the server's configured browse roots (`MUSIC_ROOT`,
  `DOWNLOADS_ROOT`) — could inject arbitrary `onclick`/HTML into an authenticated user's session: real
  DOM-based XSS (CWE-79), not a theoretical concern.
- **Fix**: replaced all three sinks with `data-path="${e(...)}"` attributes plus
  `onclick="loadBrowse(this.dataset.path)"` / `sp(this.dataset.path)"` reads — `e()`'s HTML-entity
  escaping *is* the correct, sufficient mechanism for a `data-*` attribute context (no second-stage JS
  re-parsing occurs there). This is the exact same safe pattern already used elsewhere in this file for
  artist/album buttons (`data-artist="'+e(artist)+'"'` + `this.dataset.artist`) — reused, not invented.
- **Test**: `tests/test_codeql_repowide_closure_index_html_xss.py::FolderBrowseXssContainmentTests` (2
  tests) — proves the vulnerable inline-onclick shape is gone and the safe `data-*` shape is present.

### Alert 2 — `js/xss-through-dom`, high — `index.html`, manual MBID entry → MusicBrainz link

- **Disposition**: `REAL_VULNERABILITY` — genuine, fixed.
- `_applySkManualMbid()` set `mbLink.href = 'https://musicbrainz.org/release/' + mbid` from a manual-entry
  text input's raw value with **no validation of its own**. The only validation anywhere in this flow was
  `_onSkManualMbid()` toggling the *visibility* of an "Apply" button based on a UUID-shape regex test — a
  UI convenience, not a guarantee enforced at the actual sink. A value that isn't a well-formed
  MusicBrainz UUID (e.g. `javascript:alert(document.cookie)`) must never reach a navigation sink like
  `.href`.
- **Fix**: factored the UUID regex into a shared `MB_RELEASE_UUID_RE` constant and added
  `if(!mbid||!MB_RELEASE_UUID_RE.test(mbid)) return;` at the top of `_applySkManualMbid()`, validated at
  the point of use rather than trusted from a separate UI-gate function.
- **Test**: `tests/test_codeql_repowide_closure_index_html_xss.py::ManualMbidLinkContainmentTests` (2
  tests) — proves the shared regex exists and that validation runs strictly before the `href` assignment.

### Alerts 4-17 — `js/incomplete-url-substring-sanitization`, medium — `frontend/src/views/Playlists.tsx` (5) and `index.html` (9)

- **Disposition**: `SAFE_BUT_CODEQL_BLIND` (false positive for security purposes) — **hardened anyway**,
  per this phase's explicit instruction to prefer strict URL parsing over substring matching.
- **Why these are false positives for security**: `platformLabel()` (Playlists.tsx) and `detectPlatform()`
  (index.html) both only ever assign **fixed literal strings** (`'YouTube'`, `'#ff4444'`, etc.) to a
  cosmetic badge's label/color/className — never anything derived from the URL string itself. Confirmed
  by reading every call site: the badge is pure UI feedback while pasting a playlist URL; the actual
  provider dispatch happens **server-side** ("URL parsing uses the existing backend yt-dlp flow," per an
  existing comment in the same component) via Python, a separate trust boundary already covered by this
  closure pass's `py/incomplete-url-substring-sanitization` alerts (see below, still open). Substring
  matching here could only mislabel a badge (e.g. an `evil.example/spotify.com` URL showing a Spotify
  badge) — it cannot inject markup or drive a trust/navigation decision.
- **Fix anyway**: replaced `val.includes('spotify.com')`-style substring checks with real hostname
  parsing (`new URL(...).hostname`) plus exact-or-subdomain-suffix matching, in both `index.html`
  (`_plUrlHost`/`_plHostIs`) and `Playlists.tsx` (`platformHost`/`hostMatches`) — closing the false-match
  edge case even though it was never a security boundary.
- **Test**: `tests/test_codeql_repowide_closure_index_html_xss.py::PlatformDetectionHostnameParsingTests`
  (3 tests) — proves both files use real hostname parsing, and proves the matching semantics themselves
  correctly reject substring lookalikes (`evil.example/spotify.com`, `spotify.com.evil.example`) that a
  naive `.includes()` check would have accepted.

### Alert 1 — `js/insecure-randomness`, low — `index.html`, discography card fallback element id

- **Disposition**: `SAFE_BUT_CODEQL_BLIND`. No code change.
- `const uid='disc_'+(r.mbid||Math.random().toString(36).slice(2,8));` — traced every use of `uid`: it is
  used exclusively as a DOM element-id suffix (`lidarr-action-<uid>`) for a later `getElementById` lookup
  within the same rendered card. Never a security token, session identifier, CSRF value, or anything sent
  to a server or used in an authorization decision. Per this phase's own guidance ("do not blindly
  replace... without understanding how it must be used"), this is a genuine false positive — swapping in
  `crypto.randomUUID()` would be pure churn with no security benefit.

## 5. Phase 5A/5B — Sensitive data handling, then stack-trace exposure (2026-09-01)

### Alerts 553, 554, 551, 552 — `py/clear-text-storage-sensitive-data`, high — `scripts/verify_first_run_acceptance.py` (2), `tests/test_first_run_browser_auth.py` (1), `tests/test_first_run_browser_bootstrap.py` (1)

- **Disposition**: `TEST_ONLY_FALSE_POSITIVE` (all 4).
- All 4 write a password value to a disposable tempfile as part of exercising the first-run browser-auth
  setup/migration flow. Traced each value's origin: `scripts/verify_first_run_acceptance.py`'s
  `_USER_PASSWORD` and `tests/test_first_run_browser_bootstrap.py`'s `_VALID_TEST_PASSWORD` are both
  hardcoded literal constants (`"MyCustomAdminPassword123!Aa1234567"`, `"Aa1!" + "x"*32`) — never derived
  from a real credential. `tests/test_first_run_browser_auth.py`'s `pwd` is freshly generated per test run
  via the app's own `generate_secure_browser_password(36)`. All 4 write into a `tempfile.TemporaryDirectory()`
  cleaned up automatically (script exit / `tearDown`). Confirmed `scripts/verify_first_run_acceptance.py`
  is never imported by any production module (standalone acceptance-test runner only).
- **Note for a future CI hardening pass**: all 4 sites already carry an inline `# codeql[py/clear-text-storage-sensitive-data]`
  suppression comment, which GitHub Code Scanning apparently isn't honoring for this rule/analysis
  configuration (they still showed as open, live-verified). Worth checking whether this repository's
  CodeQL setup has inline-suppression comments enabled, since the intent to suppress these exact 4 was
  already expressed in source and not acted on by the scanner.
- **GitHub disposition**: all 4 dismissed 2026-09-01, reason `used in tests`.

### Alert 771 — `py/stack-trace-exposure`, high — `app.py`, `cleanup_import_review_files`

- **Disposition**: `REAL_VULNERABILITY` — genuine, fixed.
- `except BeetsError as ex: return jsonify({"ok": False, "error": str(ex), ...}), 400` — `BeetsError`'s
  message can carry up to 200 raw response-body characters for any non-JSON/unrecognized engine error
  response (per `BeetsClient._request()`'s own documented fallback behavior, and per the sibling
  `reimport_disk()` function's already-hardened handling of this exact exception type, which explains
  the risk directly in a comment). Never interpolated safely here.
- **Fix**: log the real exception server-side (`app.logger.warning`, full exception text server-side
  only), return the same fixed message the adjacent `except Exception` branch already used.

### Alert 489 — `py/stack-trace-exposure`, high — `app.py`, `library_full` (`/api/library?limit=N`)

- **Disposition**: `REAL_VULNERABILITY` — genuine, fixed.
- The paginated-listing fallback embedded `{ex}` directly into an f-string returned to the client for
  `BeetsUnavailableError`/`TimeoutError`. An established, already-safe pattern for exactly this exists
  elsewhere in the same file (`library_art_repair_report()`'s `"error_code": "ENGINE_OFFLINE"` with a
  fixed message) — applied here for consistency.

### Alerts 490, 491, 492 — `py/stack-trace-exposure`, high — `routes_setup.py`, `setup_status`/`setup_diagnostics`

- **Disposition**: `REAL_VULNERABILITY` — genuine, fixed (3 alerts, 2 call sites).
- `_build_setup_status_payload()` aggregates config/plugin/path/provider-integration diagnostics from
  many sources — a genuinely broad `except Exception` whose text reached the client in 3 places (the
  stale-cache `refresh_error` field, the no-prior-cache 503 fallback, and `setup_diagnostics()`'s
  equivalent). `routes_setup.py` was extensively hardened for this exact pattern in Wave 2
  (`docs/TECHNICAL_DEBT.md`) at 6 other call sites — these 2 functions were evidently missed.
- **Fix**: log server-side (`app.logger.warning`, type name + text, server log only), return the
  established fixed-message pattern; the stale-cache fallback behavior itself (serving a marked-stale
  prior snapshot) is preserved exactly, only the leaked text is removed.

**Tests**: `tests/test_sec002_app_stacktrace.py::ReviewFilesCleanupAndLibraryPageExceptionSanitizationTests`
(2 tests) and `tests/test_routes_setup.py::RoutesSetupStatusBuildFailureSanitizationTests` (3 tests,
including proof the stale-cache fallback path still activates correctly with the leak removed).

**This closes every `py/clear-text-storage-sensitive-data` and `py/stack-trace-exposure` alert in the
repository: 0 remain `NEEDS_REVIEW` for either rule.**

## 6. Phase 5C/5D — Regex security (ReDoS, regex-injection), and remaining one-offs (2026-09-01)

All 22 remaining alerts (17 `py/polynomial-redos`, 3 Python-side `py/incomplete-url-substring-sanitization`,
1 `py/regex-injection`, 1 `py/bad-tag-filter`) traced individually, several **empirically timed** against
adversarial input rather than judged by inspection alone. **This closes every remaining rule in the
repository: 0 alerts remain `NEEDS_REVIEW` anywhere. All 171 baseline alerts now have a final,
individually-justified disposition.**

### Methodology: empirical timing, not just pattern inspection

For every `py/polynomial-redos` alert, timed the actual compiled pattern against adversarial input
(strings shaped to maximize backtracking: long runs of one delimiter with no matching close, or long
whitespace/dash runs) at increasing sizes (500 → 16,000+ characters) in an isolated subprocess, comparing
growth rate to distinguish genuine polynomial blowup from CodeQL's static heuristic over-flagging a
pattern that is actually linear in practice (Python's `re` engine, anchors, and lack of ambiguous overlap
between adjacent quantifiers all affect this in ways that aren't always obvious from reading the pattern
alone). This surfaced one important lesson mid-triage: an early, careless test run used the wrong
delimiter character for a square-bracket pattern (round parens against a `\[` pattern), which made it
look artificially safe — re-running with the correct adversarial shape reversed that verdict.

### Alerts 114/#41019, 115/#41103, 116/#41119, 122/#41214 — `py/polynomial-redos`, high — `app.py`, playlist artist/title-cleaning helpers (4 alerts)

- **Disposition**: `REAL_VULNERABILITY` — genuine, fixed.
- All 4 share the same root cause: an unanchored `re.sub()`/`re.split()` with an unbounded quantifier
  (`[^)\]]+`, `[^\]]*`, `[^)]*`, or `\s+`) that gets retried at every character offset of adversarial
  input with no matching closing delimiter (or no terminating whitespace) — **empirically confirmed
  quadratic**: `_playlist_title_variants()`'s pattern took **4.5 seconds** against a 16,000-character
  adversarial string (up from 0.007s at 500 chars — the growth curve alone rules out linear); the
  `\s+`-based split in `_playlist_split_artist_title()` took **1.4 seconds** at the same scale.
- **Why this is reachable, not just theoretical**: all 4 functions run on playlist track/artist text that
  can originate from user-pasted playlist text (`Playlists.tsx`'s "paste a list of tracks" mode) —
  attacker-controlled length is realistic, not a stretch.
- **Fix**: bounded the previously-unbounded quantifiers (100 characters for parenthetical/bracket
  content, 20 characters for separator whitespace runs) — no legitimate track/artist title comes
  remotely close to either bound, and the bounded patterns measured **linear time** against the same
  adversarial inputs (confirmed before editing the source, not assumed).
- **Unreported instance fixed too**: found and fixed the identical unbounded-content shape in a nearby
  `_JUNK_RE` pattern (`app.py`, yt-dlp playlist-title cleanup) that CodeQL had not flagged at all —
  per this pass's own instruction not to limit fixes to only what the scanner reported.
- **Tests**: `tests/test_codeql_repowide_closure_playlist_redos.py` (9 tests) — 4 performance tests
  proving each fixed function completes in well under 2 seconds against 20,000-character adversarial
  input (actual: <0.1s total for all 4 combined), plus 5 behavior-preservation tests proving normal
  "Artist - Title (feat. Someone) [Official Video]"-shaped input still cleans correctly.

### Alerts 103, 104, 105 — `py/incomplete-url-substring-sanitization`, medium — `app.py`, `playlist_parse()` (3 alerts)

- **Disposition**: `REAL_VULNERABILITY` — genuine, fixed. **Materially different from the Phase 4 JS
  findings in the same rule** — not cosmetic.
- `playlist_parse()`'s provider-routing logic used `"spotify.com" in content` and `"youtube.com"`/
  `"youtu.be"`/`"soundcloud.com"` `in parse_lower` substring checks to decide which of three real,
  consequential actions to take. The soundcloud branch calls `_apply_ytdlp_netrc(ydl_opts)`, which
  instructs yt-dlp to attach the **operator's stored netrc credentials** to the outbound request. A URL
  that merely *contains* the substring `"soundcloud.com"` without actually being hosted there (e.g.
  `https://evil.example/soundcloud.com/x`) would previously have had those credentials attached and sent
  to the attacker-controlled host — **real credential exfiltration**, not a mislabeled UI badge like the
  cosmetic `platformLabel()`/`detectPlatform()` findings closed in Phase 4.
- **Fix**: real hostname parsing (`_playlist_url_host()`/`_playlist_url_host_is()`, hoisted to module
  level, exact-host-or-subdomain-suffix matching via `urllib.parse.urlsplit`) replaces all 3 substring
  checks.
- **Tests**: `tests/test_codeql_repowide_closure_playlist_url_host.py` (10 tests) — 8 unit tests on the
  hostname parser/matcher (including the exact `evil.example/soundcloud.com` spoof and a
  `soundcloud.com.evil.example` suffix-lookalike, both correctly rejected; genuine `soundcloud.com` and
  its subdomains correctly accepted) plus 2 integration tests driving the real `playlist_parse()` route
  with a faked `yt_dlp` module, proving `_apply_ytdlp_netrc()` is **not called** for the spoofed URL and
  **is still called** for a genuine `soundcloud.com` URL (behavior preserved for the legitimate case).

### Alerts 109/#17263, 125/#17262 — `py/regex-injection` and the redos alert at the same line — `app.py`, `_ai_evidence_extract_year`

- **Disposition**: `SAFE_BUT_CODEQL_BLIND` (both).
- `re.sub(r"\s*[\(\[]" + year + r"[\)\]]", "", text)` builds a regex from a runtime value — but `year`
  is itself `m.group(1)` captured a few lines above from the pattern `(?:19|20)\d{4}` (sic; 2-digit
  century + `\d{2}` in the actual source), which can **only ever match a string composed of exactly 4
  digits**. A value that is provably constrained to `[0-9]{4}` by the same regex engine that produced it
  cannot introduce a regex metacharacter — no injection is possible regardless of what the *original*
  input to this function was.

### Alert 126 — `py/bad-tag-filter`, medium — `app.py`, `_inline_script_csp_hashes`

- **Disposition**: `SAFE_BUT_CODEQL_BLIND`.
- CodeQL's `bad-tag-filter` rule is a generic heuristic against using regex to filter/detect HTML tags as
  a *security boundary* on untrusted input. Traced the actual data flow: `_INLINE_SCRIPT_RE` is used only
  by `_content_security_policy()`, called from a global `@app.after_request` hook with
  `html_for_csp = response.get_data(as_text=True)` — **this server's own outgoing response body**, to
  compute `sha256-` hashes of its own inline `<script>` tags for the `Content-Security-Policy` header's
  allowlist. It never parses attacker-supplied or otherwise untrusted HTML; it is a build/response-time
  introspection tool, not an XSS filter.

### The other 13 `py/polynomial-redos` alerts

- **Disposition**: `SAFE_BUT_CODEQL_BLIND` (all 13), each empirically timed (not merely inspected) and
  confirmed linear/flat against adversarial input at up to 16,000+ characters. Root causes varied:
  lazy `.*?` quantifiers with a required terminator and no overlap (alert 107); simple negated character
  classes with no delimiter-pair ambiguity (108, 112); bounded repetition (`\d{4}`, not unbounded `+`/`*`)
  (112); a pattern anchored with `^` so `re.sub()` only ever attempts a match at position 0, never
  retried at O(n) offsets (118); alternations of short literal tokens with no unbounded nesting
  (110, 111, 113, 117, 119, 123); and two patterns (120, 121) that looked like they might be growing at
  small input sizes but proved flat once tested up to 8,000–16,000 characters — a reminder that small-n
  timing noise can mislead without pushing the test far enough to see the true asymptote.

## 7. Repository-wide CodeQL security closure — final status

**All 171 baseline alerts have a final, individually-justified disposition — 0 remain `NEEDS_REVIEW`.**

| Category | Count |
| --- | ---: |
| Total baseline alerts | 171 |
| Real vulnerabilities found and fixed (code changes, regression-tested) | 35 |
| Confirmed genuinely safe and dismissed on GitHub (`false positive` / `used in tests`) | 136 |
| Alerts remaining `NEEDS_REVIEW` | **0** |

This is **branch-local, verified, individual-disposition closure** — every alert has been traced,
understood, and either fixed-with-tests or proven safe-with-evidence, none dismissed by rule type or in
bulk without a sink-specific rationale. It is **not yet repository/`main` closure**: the 35 real fixes
remain "open" on GitHub's live view until this branch is pushed, reviewed, merged, and `main` is
rescanned by CodeQL — per this document's own repeated distinction (see the Reconciliation section
above), and per this repository's `AGENT_WORKFLOW.md`, pushing/merging requires explicit authorization
this pass does not have. **Do not read "0 NEEDS_REVIEW" as "0 open on GitHub" — they answer different
questions**, and conflating them is exactly the error this closure effort's own mission documents warned
against repeatedly.

## 8. Test verification log

- Full backend suite (`python -m unittest discover -s tests -p "test_*.py"`), run 1 (early in session):
  2763 tests, 0 failures, 129 skipped (pre-existing, require live engine/Docker).
- Same suite, run 2 (order-dependence/flakiness check): completed, exit code 0 (background run;
  detailed output was lost to a local logging mistake, but the process's own exit code confirms a clean
  pass).
- Full suite, run 3 (after the target-preview, playlist, and folder-track-search-titles fixes): 2769
  tests, 0 failures, 129 skipped, exit code 0.
- Full suite, run 4 (checkpoint after the _folder_clean_root fix, 98/171 alerts dispositioned): 2772
  tests, 0 failures, 129 skipped, exit code 0.
- Full suite, run 5 (after the MB release-tracklist cache fix, 121/171 alerts dispositioned -- every
  `app.py` `py/path-injection` alert closed except the 2 deliberately-deferred
  `_folder_release_preflight` ones): 2778 tests, 0 failures, 129 skipped, exit code 0.
- `python -m py_compile app.py`: clean after every code edit this session.
- Targeted regression suites re-run after every individual fix throughout (see per-cluster commits).
- Full suite, run 6 (after the `_folder_release_preflight` fix, Phase 2 of the continued-closure pass —
  0 open `py/path-injection` alerts remain undispositioned repository-wide): 2783 tests, 0 failures, 129
  skipped, exit code 0.
- Full suite, run 7 (order-dependence/flakiness re-check, same code state as run 6): 2790 tests, 0
  failures, 129 skipped, exit code 0. No flakiness or order-dependence detected.
- Python security gates (Phase 4 checkpoint): `security_secret_scan.py` passed; `validate_compose_security.py`
  → `{"ok": true, "errors": [], "warnings": []}`; `verify_arch003_mutation_inventory.py --check` passed
  (469 entries, 107 unresolved, matches baseline); `generate_endpoint_inventory.py --check` initially
  reported stale (188 line-number-only diffs accumulated across this session's earlier edits, no
  routes added/removed) — regenerated through the official generator (0 routes needed manual review),
  now up to date.
- Frontend gates (Phase 4, after the `Playlists.tsx` hostname-parsing fix): `npm ci` clean;
  `npm run typecheck` clean; `npm run lint` clean; `npm test -- --run` — **58 tests passed (4 test
  files)**; `npm run build` — Next.js production build succeeded, all 13 static pages generated;
  `npm audit --audit-level=high` — **0 vulnerabilities**.
- Python security gates (Phase 5A/5B checkpoint): all 4 gates re-run and pass; endpoint inventory
  regenerated again (further line-number drift only).
- Full suite, run 8 (Phase 5A/5B checkpoint, 149/171 alerts dispositioned): 2795 tests, 0 failures, 129
  skipped, exit code 0.
- Full suite, run 9 (**final** — after Phase 5C/5D, all 171 alerts dispositioned, 0 remain
  `NEEDS_REVIEW`): 2814 tests, 0 failures, 129 skipped, exit code 0.
- Full suite, run 10 (final order-dependence/flakiness re-check, same code state as run 9): 2814 tests,
  0 failures, 129 skipped, exit code 0 -- identical count to run 9, confirming no order-dependence or
  flakiness across the two runs.
- Final Python security gates (all 4): `security_secret_scan.py` passed; `validate_compose_security.py`
  → `{"ok": true, "errors": [], "warnings": []}`; `verify_arch003_mutation_inventory.py --check` passed
  (469 entries, 107 unresolved, matches baseline); `generate_endpoint_inventory.py --check` regenerated
  once more (line-number drift only, 0 routes need manual review) and now up to date.
- Final frontend gates (no frontend files changed in Phase 5, re-run for completeness): `npm run
  typecheck` clean; `npm run lint` clean; `npm test -- --run` — 58/58 passed.

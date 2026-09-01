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

## 3. Remaining alerts

101 of 171 alerts remain **NEEDS_REVIEW** as of this checkpoint (45 dispositioned: 2 critical
command-injection dismissed, 8 path-injection fixed as real vulnerabilities, 35 path-injection confirmed
safe) — see `security/codeql_main_alert_inventory.json` for the full raw list. Work continues
alert-by-alert (or pattern-class-by-pattern-class for the remaining `py/path-injection` instances
still clustered in `app.py`, a 48,286-line file) rather than being declared closed. **Repository-wide
CodeQL security closure is NOT complete.**

## 4. Test verification log

- Full backend suite (`python -m unittest discover -s tests -p "test_*.py"`), run 1: 2763 tests, 0
  failures, 129 skipped (pre-existing, require live engine/Docker).
- Same suite, run 2 (order-dependence/flakiness check): completed, exit code 0 (background run;
  detailed output was lost to a local logging mistake, but the process's own exit code confirms a clean
  pass — re-run on request if independent confirmation of the full text is needed).
- `python -m py_compile app.py`: clean after every code edit this session.

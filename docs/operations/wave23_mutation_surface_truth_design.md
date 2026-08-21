# SEC-002 / ARCH-003 Wave 23 Design Document
**Mutation Surface Truth Pass + Generic Control-Agent Mutation Boundary Audit**

## 1. Executive Summary & Purpose
Wave 23 establishes a complete, trustworthy mutation map across `beets-web-manager` and audits generic engine mutation APIs. Rather than claiming premature closure, Wave 23 expands AST-based discovery to eliminate scanner blind spots, removes residual local media `mkdir` operations from `_album_cleanup_apply_issue` in `app.py`, triages 100% of candidate sinks so that `NEEDS_REVIEW = 0`, and institutes a strict ratchet gate in CI.

## 2. Scanner Enhancements & Blind Spot Corrections
1. **`mkdir` & Directory Creation Discovery**: Scanner now detects `Path.mkdir`, `os.mkdir`, `os.makedirs`, `Path.rmdir`, `os.rmdir`, `os.removedirs`.
2. **Expanded Filesystem Operations**: Added `Path.touch`, `Path.write_text`, `Path.write_bytes`, `Path.unlink`, `os.remove`, `os.unlink`, `os.rename`, `os.replace`, `shutil.move`, `shutil.rmtree`, `shutil.copy`, `shutil.copy2`, `shutil.copytree`, `shutil.copyfile`, `tempfile` persistent operations.
3. **Writable `open(...)` & `Path.open(...)`**: Detects `open` calls with mutating mode strings (`"w"`, `"a"`, `"x"`, `"+"`), while ignoring read-only `"r"` / `"rb"`.
4. **File-Handle Write Tracking**: Detects `fh.write(...)`, `fh.writelines(...)`, `fh.truncate(...)` on file handles.
5. **Function-Local Variable Tracking for `rename`/`replace`**: Tracks variable assignments (e.g. `x = Path(...)`, `x = root / "foo"`) so method calls on those variables are discovered regardless of naming.
6. **SQL DML Variable Tracking**: Detects `sql = "UPDATE ..."` assigned to local variables before `con.execute(sql, ...)`.
7. **Generic Beets Command Execution**: Identifies project wrappers (`beets_client.run_command`, `_beet_run`, `beets_client.reimport_source`, `beets_client.reimport_disk`, `beets_client.modify`, `beets_client.retag`, `beets_client.write_tags`).

## 3. Local Media `mkdir` Removal from `_album_cleanup_apply_issue`
- **Previous State**: `_album_cleanup_apply_issue` executed local `canonical.mkdir(...)` and `dst.parent.mkdir(...)` before delegating file moves to the engine.
- **Wave 23 Correction**: Removed local `mkdir` calls. Engine transaction families (`album_maintenance_v1`, `album_artwork_v1`, `folder_cleanup_v1`) now own parent directory creation, file moves, DB path updates, verification, and rollback.
- **Structural Test Enforcement**: Updated `test_album_cleanup_apply_issue_has_no_direct_mutation_calls` in `tests/test_arch003_boundary_enforcement.py` to prohibit `.mkdir(`, `os.makedirs(`, `shutil.move(`, `.unlink(`, `.rename(`, `.replace(`, `.rmdir(`, `open(`, `with _db(`, `BEET_BIN`.

## 4. Inventory Triage Summary & Ratchet Gate
- **Total Discovered Sinks**: 491
- **NEEDS_REVIEW**: 0 (100% triaged)
- **ARCH003_BLOCKER**: 180
- **Controlled / Native / App State Sinks**: 311
  - `CONTROLLED_MEDIA_MUTATION`: 91
  - `ENGINE_NATIVE_BEETS`: 93
  - `CONFIG_STATE`: 41
  - `APP_STATE`: 39
  - `STAGING_ONLY`: 13
  - `CACHE_STATE`: 14
  - `NON_MEDIA_FILESYSTEM`: 10
  - `TRANSACTION_STATE`: 9
  - `READ_ONLY_FALSE_POSITIVE`: 1
- **CI Ratchet Gate**: `scripts/verify_arch003_mutation_inventory.py --check` enforces:
  1. `UNMATCHED = 0` (every discovered sink matches an inventory key)
  2. `NEEDS_REVIEW = 0` (no untriaged entries permitted)
  3. `ARCH003_BLOCKER <= 180` (baseline locked; blocker count cannot grow)

## 5. Control Agent `ALLOWED_COMMANDS` & Endpoint Audits
- **`ALLOWED_COMMANDS` Audit**: Allowlist contains `import`, `update`, `write`, `move`, `modify`, `mbsync`, `fetchart`, `embedart`, `lastgenre`, `lastimport`, `alt`, `version`, `config`, `check`, `remove`, `rm`, `submit`, `mbsubmit`. Mutating commands are restricted to engine operations and must not be invoked directly by Web Manager routes for destructive work without domain transaction wrappers.
- **`PATCH /items/<id>` & `PATCH /albums/<id>` Audit**: Expose validated field assignments (`ITEM_EDITABLE_FIELDS`, `ALBUM_EDITABLE_FIELDS`). Used for direct SQLite metadata updates; do not write media tags to audio files. Overwriting Release Group identity (`mb_albumid`, `mb_releasegroupid`) via generic PATCH is flagged for controlled migration in Wave 24.
- **`BeetsClient.run_command` Audit**: All calls route through `job_engine._beet_run(...)` for async job execution or `reimport_source_atomic(...)` inside Control Agent daemon.

## 6. Blocker Cluster Matrix & Wave 24 Recommendation
- **Cluster A: Unwrapped Local Media Filesystem Mutations in `app.py`** (~45 sinks)
- **Cluster B: Raw Beets Library SQL DML in `app.py`** (~65 sinks)
- **Cluster C: Legacy Local Subprocess (`BEET_BIN`) Calls in `app.py`** (~45 sinks)
- **Cluster D: Direct Local Tag-Write Calls in `app.py`** (~25 sinks)

**Recommended Wave 24 Scope**: **Cluster A (Unwrapped Local Media Filesystem Mutations in `app.py`)** — highest destructive risk to media files outside the engine boundary.

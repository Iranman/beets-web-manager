# SEC-002 / ARCH-003 Wave 26 — AI Import & State Concurrency Closure Design Document

---

## 1. Overview & Goal

Wave 26 completes the **AI Import Controlled-Mutation Domain Closure** (`unresolved_domain_counts.ai_import == 0`) and resolves **ARCH-010 (AI Batch Import whole-state JSON persistence state concurrency & versioning)**.

Prior to Wave 26, the Web Manager contained 18 unresolved `ai_import` blockers in `app.py` consisting of:
- Unsafe unversioned whole-state JSON writes (`_ai_batch_write_state`, `_record_ai_review_decision`, `batch_ai_suggest._do`) lacking compare-and-swap (CAS) or schema revisioning.
- Direct SQLite DML queries (`UPDATE albums SET mb_albumid...`, `UPDATE items SET mb_trackid...`) bypassing the Beets Control Agent transaction engine.
- Direct local `_beet_run` subprocess invocations (`write`, `mbsync`, `move`).
- Direct local filesystem mutations (`source.rename(target)`, `folder.rmdir()`) inside Web Manager.

Wave 26 refactors the AI import workflows so that:
1. **AI Authority Boundary**: AI output proposes, ranks, and explains candidate matches, but **never independently authorizes** destructive library mutations. Destructive mutations require deterministic backend evidence and reviewed transaction contracts.
2. **ARCH-010 Durable CAS State Store**: Implements `AiBatchStateStore` (`backend/ai_batch_state_store.py`) providing schema versioning (`schema_version = 1`), revision tracking (`revision = 1, 2, 3...`), optimistic concurrency control (CAS conflict detection raising 409 Conflict / `AiBatchStateConflictError`), SQLite WAL-mode durability across multi-worker Gunicorn processes, and one-way migration of legacy `.json` state files.
3. **Transaction Engine Reuse**: Routes all library mutations through `BeetsClient` HTTP IPC, composing `confirmed_import_v1`, `album_mb_track_repair_v1`, `album_metadata_repair_v1`, `album_relocation_v1`, and `folder_cleanup_v1`.
4. **Zero Fallbacks**: Eliminates raw SQLite DML, local `_beet_run` fallbacks, and local media filesystem mutations in Web Manager. Engine unavailable fails closed.

---

## 2. 18-Sink Baseline Census & Dispositions

All 18 baseline `ai_import` unresolved blocker sinks from `e7490a83f0b392151abd4befeb2ade9a04994659` have been fully resolved:

| # | Inventory Key | File | Function | Line | Kind | Call Text | Production Workflow | Final Disposition |
|---|---|---|---|---|---|---|---|---|
| 01 | `app.py:batch_ai_suggest._do:d7a05b70730b` | `app.py` | `batch_ai_suggest._do` | 15732 | `filesystem` | `tmp.write_text(json.dumps(results, indent=2))` | AI suggestions cache write | Replaced with `_ai_batch_store.save_suggestions_cache` (`CACHE_STATE`) |
| 02 | `app.py:batch_ai_suggest._do:140471a7f3fb` | `app.py` | `batch_ai_suggest._do` | 15733 | `filesystem` | `tmp.replace(_ALBUM_MB_SUGGESTIONS_FILE)` | AI suggestions cache replace | Replaced with `_ai_batch_store.save_suggestions_cache` (`CACHE_STATE`) |
| 03 | `app.py:_record_ai_review_decision:265e978723f7` | `app.py` | `_record_ai_review_decision` | 24517 | `filesystem` | `_AI_REVIEW_DECISIONS_FILE.write_text(...)` | AI review decisions recording | Replaced with `_ai_batch_store.record_review_decision` (`APP_STATE`) |
| 04 | `app.py:_ai_batch_write_state:5e2d1950f053` | `app.py` | `_ai_batch_write_state` | 25149 | `filesystem` | `_AI_BATCH_STATE_DIR.mkdir(...)` | AI batch state directory creation | Replaced with `_ai_batch_store.save_batch_state` (`APP_STATE`) |
| 05 | `app.py:_ai_batch_write_state:fb395b6cc723` | `app.py` | `_ai_batch_write_state` | 25152 | `filesystem` | `tmp.write_text(...)` | AI batch job state JSON write | Replaced with `_ai_batch_store.save_batch_state` (`APP_STATE`) |
| 06 | `app.py:_ai_batch_write_state:6492d573d0ae` | `app.py` | `_ai_batch_write_state` | 25153 | `filesystem` | `os.replace(str(tmp), str(path))` | AI batch job state JSON replace | Replaced with `_ai_batch_store.save_batch_state` (`APP_STATE`) |
| 07 | `app.py:_start_library_mbid_sticking_repair._do:c5678f79d196` | `app.py` | `_start_library_mbid_sticking_repair._do` | 33176 | `sql` | `con.execute("UPDATE albums SET mb_albumid=? WHERE id=?", ...)` | Album MBID sticky repair | Replaced with `beets_client.update_album_metadata` |
| 08 | `app.py:_start_library_mbid_sticking_repair._do:e0f2cca95a8a` | `app.py` | `_start_library_mbid_sticking_repair._do` | 33264 | `sql` | `con.execute("UPDATE albums SET mb_albumid=?, mb_releasegroupid=?...", ...)` | Album MBID/RGID discovery link | Replaced with `beets_client.update_album_metadata` |
| 09 | `app.py:_start_library_mbid_sticking_repair._do:3a112c5c1e90` | `app.py` | `_start_library_mbid_sticking_repair._do` | 33269 | `sql` | `con.execute("UPDATE albums SET mb_albumid=? WHERE id=?", ...)` | Album MBID discovery link | Replaced with `beets_client.update_album_metadata` |
| 10 | `app.py:_start_library_mbid_sticking_repair._do:eeab02493344` | `app.py` | `_start_library_mbid_sticking_repair._do` | 33380 | `sql` | `con.executemany("UPDATE items SET mb_trackid=...", ...)` | Track recording ID repair | Replaced with `beets_client.plan_album_mb_track_repair` / `apply_album_mb_track_repair` |
| 11 | `app.py:_start_library_mbid_sticking_repair._do:c5678f79d196` | `app.py` | `_start_library_mbid_sticking_repair._do` | 33385 | `sql` | `con.execute("UPDATE albums SET mb_albumid=? WHERE id=?", ...)` | Album MBID post-track repair update | Replaced with `beets_client.plan_album_mb_track_repair` / `apply_album_mb_track_repair` |
| 12 | `app.py:_start_library_mbid_sticking_repair._do:2cb2c78c5b40` | `app.py` | `_start_library_mbid_sticking_repair._do` | 33401 | `subprocess` | `_beet_run(base + ["write", f"album_id:{aid}"], ...)` | Tag write after track repair | Replaced with `beets_client.apply_album_mb_track_repair(..., write_tags=True)` |
| 13 | `app.py:_maintenance_safe_folder_renames:33d146cbde22` | `app.py` | `_maintenance_safe_folder_renames` | 35978 | `filesystem` | `target.parent.mkdir(...)` | Folder safe rename parent creation | Replaced with `beets_client.move_file` |
| 14 | `app.py:_maintenance_safe_folder_renames:ce179da49241` | `app.py` | `_maintenance_safe_folder_renames` | 35979 | `filesystem` | `source.rename(target)` | Folder safe rename move | Replaced with `beets_client.move_file` |
| 15 | `app.py:_root_folder_repair_apply_safe:abaaf8a43241` | `app.py` | `_root_folder_repair_apply_safe` | 38851 | `subprocess` | `_beet_run(base + ["mbsync", ...])` | Root folder repair mbsync | Replaced with `beets_client.plan_album_mb_track_repair` / `apply_album_mb_track_repair` |
| 16 | `app.py:_root_folder_repair_apply_safe:434bb47d70a3` | `app.py` | `_root_folder_repair_apply_safe` | 38854 | `subprocess` | `_beet_run(base + ["write", ...])` | Root folder repair tag write | Replaced with `beets_client.update_album_metadata` |
| 17 | `app.py:_root_folder_repair_apply_safe:729cebef9c4c` | `app.py` | `_root_folder_repair_apply_safe` | 38862 | `subprocess` | `_beet_run(base + ["move", ...])` | Root folder repair relocate | Replaced with `beets_client.relocate_album` |
| 18 | `app.py:_root_folder_repair_apply_safe:ef52c6519b91` | `app.py` | `_root_folder_repair_apply_safe` | 38876 | `filesystem` | `folder.rmdir()` | Emptied folder cleanup | Replaced with `beets_client.delete_file` |

---

## 3. ARCH-010 Versioned CAS State Architecture

### Database Schema (`ai_batch_state.db`)

```sql
CREATE TABLE IF NOT EXISTS ai_batch_state (
    id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL DEFAULT 1,
    revision INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL,
    job_id TEXT,
    payload_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    writer_id TEXT
);

CREATE TABLE IF NOT EXISTS ai_review_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    schema_version INTEGER NOT NULL DEFAULT 1,
    revision INTEGER NOT NULL DEFAULT 1,
    album_id INTEGER,
    mb_albumid TEXT,
    decision TEXT,
    payload_json TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS ai_suggestions_cache (
    album_id INTEGER PRIMARY KEY,
    schema_version INTEGER NOT NULL DEFAULT 1,
    mb_albumid TEXT,
    payload_json TEXT NOT NULL,
    updated_at REAL NOT NULL
);
```

### Optimistic Concurrency Control (CAS)

Conditional updates execute:
```sql
UPDATE ai_batch_state
SET revision = ?, status = ?, job_id = ?, payload_json = ?, updated_at = ?, writer_id = ?
WHERE id = ? AND revision = ?
```
If `cursor.rowcount == 0`, `AiBatchStateConflictError` is raised (HTTP 409 Conflict status).

---

## 4. Unresolved Domain Accounting

- `ai_import`: **`0`** *(Domain Closure Achieved)*
- `import_reconciliation`: **`0`** *(Remains Closed)*
- `library_cleanup`: **`0`** *(Remains Closed)*
- `config`: `20`
- `other`: `87`
- Total Candidate Sinks: `464`
- Total Unresolved Baseline: `107`

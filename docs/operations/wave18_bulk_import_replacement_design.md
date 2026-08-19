# SEC-002 / ARCH-003 Wave 18: Bulk Import-Time Replacement Controlled Mutation Boundary Design Document

## 1. Overview & Objectives

Wave 18 migrates all bulk import-time replacement behavior associated with:
- `_import_and_tag_disk`
- `_merge_imported_album_into_existing`
- `replace_existing_item_ids` / `replace_existing`

behind an engine-owned, controlled:

$$\text{Plan} \longrightarrow \text{Review} \longrightarrow \text{Import \& Verify New Media} \longrightarrow \text{Retire Old State} \longrightarrow \text{Transaction}$$

mutation boundary (`mutation_family == "bulk_import_replacement_v1"`).

### Architectural Invariants
1. **Engine Ownership**: The Beets Engine and `transaction_engine.py` are the authoritative owners of all media, database, and quarantine mutations.
2. **Zero Direct Web Manager Mutations**: `app.py` contains **zero** direct file operations (`unlink`, `remove`, `rmtree`, `rename`, `replace`, `shutil.move`), direct audio writes, or direct SQLite `DELETE FROM items` / `UPDATE items` queries for bulk replacement.
3. **Mutation Family**: Enforces `mutation_family == "bulk_import_replacement_v1"`.
4. **Import-Before-Retire Ordering**: Destructive retirement of old library items/files occurs **only after** incoming media has been imported and its Beets DB items and physical files have been verified.
5. **Explicit Mapping**: The Plan explicitly captures `old_item_id -> replacement_candidate` mappings, preventing ambiguous bulk replacements.
6. **Non-Destructive Quarantine**: Replaced original track files are moved to server-derived quarantine (`<quarantine-root>/<date>/<operation_id>/<filename>`), never unlinked directly in `app.py`.
7. **Matching Authority**: Recording ID, Release Group ID, Release ID, and AcoustID evidence govern mapping validity. AI is advisory only.
8. **Idempotency & Rollback**: Operation locking + resource locking (old item IDs & album ID). Safe retry on completed Apply; rollback restores quarantined files to original paths and restores prior Beets DB rows.

---

## 2. Production Caller & Data-Flow Matrix

| Endpoint / Caller | Pre-Wave 18 Implementation | Pre-Wave 18 Direct Mutations | Wave 18 Engine-Owned Boundary (`bulk_import_replacement_v1`) |
| :--- | :--- | :--- | :--- |
| `_import_and_tag_disk`<br>(`replace_existing_item_ids`) | Executes `_merge_imported_album_into_existing` in `app.py` | `resolved.unlink(missing_ok=True)`<br>`DELETE FROM items WHERE id IN (...)` | Invokes `beets_client.plan_bulk_import_replacement` & `apply_bulk_import_replacement`. Engine handles import, verification, and quarantine of retired items. |
| `_merge_imported_album_into_existing` | Direct file unlinks & SQLite deletes in `app.py` | Physical file `unlink()` & SQLite `DELETE FROM items` | Delegated entirely to engine-owned plan/apply workflow. Zero direct unlinks or SQL deletes in `app.py`. |
| `_music_format_replacement_payload`<br>(Music Format Retry Pipeline) | Sets `replace_existing_item_ids` on import payload | Direct file deletion and SQL row cleanup in `app.py` | Routes through engine bulk import replacement IPC. |

---

## 3. High-Level Controlled Mutation Flow

```
[Web Manager / UI / Import Review]
       │
       │ 1. POST /api/engine/bulk-import-replacement/plan
       ▼
[Beets Control Agent] ──► [Transaction Engine]
                                 │
                                 ├── Inspect old Beets items & incoming source media
                                 ├── Verify Release Group & Recording identity matching
                                 ├── Map old_item_ids -> replacement_candidates explicitly
                                 ├── Capture stat snapshots for old items
                                 └── Create Transaction (mutation_family="bulk_import_replacement_v1", status="Preview")
       │
       │ 2. Review Plan (UI itemizes Old -> New mappings, RGID evidence, quarantine path)
       │
       │ 3. POST /api/engine/bulk-import-replacement/apply
       ▼
[Beets Control Agent] ──► [Transaction Engine]
                                 │
                                 ├── Acquire per-operation lock + resource locks (old_item_ids, album_id)
                                 ├── Idempotency check (if Completed -> return ok)
                                 ├── Re-verify TOCTOU stats for old items
                                 ├── Execute Beets import of new media
                                 ├── Discover & verify newly imported Beets items
                                 ├── Quarantine old items to (<quarantine-root>/<date>/<op_id>/...)
                                 ├── Retire old Beets DB rows & update merged album links
                                 └── Verify final DB & filesystem state -> status="Completed"
```

---

## 4. Idempotency, Concurrency & Rollback

1. **Resource Locking**: Deterministic ordering of locks on `operation_id`, `album_id`, and `old_item_ids` prevents concurrent double-applies or deadlocks.
2. **Idempotency**: Repeated Apply for a completed transaction returns the existing result without re-mutating.
3. **Rollback (`rollback_bulk_import_replacement`)**: Restores quarantined old files back to original library paths, restores old SQLite item rows, and cleans up newly imported replacement rows if requested.

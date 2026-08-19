# SEC-002 / ARCH-003 Wave 17: Track Replacement & Repair Controlled Mutation Boundary Design Document

## 1. Overview & Objectives

Wave 17 brings all application track replacement and bad-file repair mutation workflows under the authoritative, engine-owned:

$$\text{Plan} \longrightarrow \text{Review} \longrightarrow \text{Apply} \longrightarrow \text{Verify} \longrightarrow \text{Transaction}$$

boundary established in Waves 15–16.

### Architectural Invariants
1. **Engine Ownership**: The Beets Engine and `transaction_engine.py` are the authoritative owners of all library media, database, and quarantine mutations.
2. **Zero Direct Web Manager Mutations**: `app.py` acts solely as UI/orchestrator. It contains **zero** direct file operations (`unlink`, `remove`, `rmtree`, `rename`, `replace`, `shutil.move`), direct audio tag writes, or direct SQLite modifications for track replacement.
3. **Mutation Family**: All track replacement transactions enforce `mutation_family == "track_replacement_v1"`.
4. **Non-Destructive Quarantine**: Replaced files are moved to server-derived quarantine (`<quarantine-root>/<date>/<operation_id>/<filename>`), never permanently unlinked or deleted without recovery options.
5. **TOCTOU & Stat Verification**: Both original and replacement candidate files undergo strict precondition checks (`inode`, `dev`, `size`, `mtime_ns`, non-symlink, allowed-root containment) at Plan time and re-verification immediately before Apply.
6. **Matching Authority**: Recording ID, Release Group ID, Release ID, and AcoustID evidence govern candidate validity. AI is advisory only.

---

## 2. Production Caller Matrix

| Endpoint / Function | Current Implementation | Current Direct Mutations | Target Engine-Owned Boundary (`track_replacement_v1`) |
| :--- | :--- | :--- | :--- |
| `POST /api/library/music-format/replace`<br>`start_music_format_replacement_retry` | Calls `_music_format_replace_rows` in `app.py` | `original.unlink()`, `shutil.move()`, `DELETE FROM items WHERE id=?` | Delegates to `beets_client.plan_track_replacement` & `apply_track_replacement` using `track_replacement_v1`. |
| Replacement Track Import Merge<br>`_import_and_tag_disk` (`replace_existing_item_ids`) | Executes direct SQLite SQL in `app.py` | `DELETE FROM items WHERE id IN (...)` inside `app.py` | Route replacement track replacement pass to engine-owned plan/apply. |
| `POST /api/albums/<aid>/repair-mb-tracks`<br>`repair_album_mb_tracks` | Direct SQLite update + `beet write` in `app.py` | `UPDATE items ...`, `UPDATE albums ...`, local `beet write` | Delegate tag repair to engine IPC method. |
| `POST /api/items/<iid>/attach-recording`<br>`item_attach_recording` | Singleton item tag sync in `app.py` | `beet modify`, `beet write`, `beet move` via local subprocess | Engine IPC for singleton tag update & placement. |

---

## 3. Engine Transaction Flow (`track_replacement_v1`)

```
[Web Manager / UI]
       │
       │ 1. POST /api/engine/track-replacement/plan
       ▼
[Beets Control Agent] ──► [Transaction Engine]
                                 │
                                 ├── Re-stat original & candidate files
                                 ├── Verify containment & non-symlink
                                 ├── Verify Recording ID / AcoustID match
                                 └── Create Transaction (mutation_family="track_replacement_v1", status="Preview")
       │
       │ 2. Review Plan (UI presents before/after, quality diff, quarantine path)
       │
       │ 3. POST /api/engine/track-replacement/apply
       ▼
[Beets Control Agent] ──► [Transaction Engine]
                                 │
                                 ├── Acquire per-operation lock + item lock
                                 ├── Idempotency check (if Completed -> return ok)
                                 ├── Re-verify TOCTOU stats & non-symlink
                                 ├── Re-verify AcoustID fingerprint identity
                                 ├── Move original file to Quarantine (<quarantine-root>/<date>/<op_id>/...)
                                 ├── Move/copy replacement file to destination
                                 ├── Update Beets SQLite database row
                                 └── Verify result & mark status="Completed"
```

---

## 4. Idempotency & Rollback Strategy

1. **Idempotency**: Concurrent or repeated Apply calls for the same `operation_id` are serialized using `_get_apply_lock(operation_id)` and return the completed state safely.
2. **Rollback**: `rollback_track_replacement` restores the original file from engine quarantine to its original path, removes the replacement file, and restores the Beets SQLite item record.

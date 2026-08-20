# SEC-002 / ARCH-003 Wave 19: MusicBrainz Album Track Repair Controlled Mutation Boundary

## 1. Overview & Objectives

Wave 19 migrates the `repair_album_mb_tracks` workflow behind an engine-owned controlled mutation boundary (`album_mb_track_repair_v1`).

### Architectural Invariants

1. **Web Manager is UI / Orchestration Only**: `app.py` contains zero direct file unlinks, renames, moves, SQLite `UPDATE` queries for repair, or local `_beet_run` subprocess calls.
2. **Beets Engine Owns All DB Mutations & File Tag Writes**: `backend/transaction_engine.py` owns all Beets DB track updates (`mb_trackid`, `track`, `disc`, `title`, `mb_albumid`) and audio metadata tag writes.
3. **Preserved Identity Hierarchy**:
   - Canonical Album Identity: MusicBrainz Release Group ID (`mb_releasegroupid`).
   - Edition Evidence: Release ID (`mb_albumid`).
   - Track Identity: Recording ID (`mb_trackid`).
   - Release Group Equality Guard: If a caller supplies an `mb_albumid` override, its Release Group MUST match `album.mb_releasegroupid` or the repair MUST fail closed (`code: repair_identity_mismatch`).
4. **Stale Plan & TOCTOU Protection**: Precondition revalidation verifies device, inode, file size, mtime (`mtime_ns`), allowed roots containment, and symlink walk revalidation. Fails closed before mutating if any attribute changed.
5. **Deterministic Concurrency Control**: Locks `album:<id>` and `item:<id>` (sorted) using reentrant resource locks to prevent deadlocks between concurrent operations.
6. **Reversibility & Recovery**: `rollback_album_mb_track_repair` restores prior DB values and rewrites prior audio tags from snapshot data.

---

## 2. Controlled Mutation Family: `album_mb_track_repair_v1`

### Endpoints
- `POST /albums/mb-track-repair/plan`: Generates non-mutating preview transaction in `TransactionStore`.
- `POST /albums/mb-track-repair/apply`: Re-validates TOCTOU invariants, applies SQLite DB updates, writes audio tags, and performs post-write verification.
- `POST /albums/mb-track-repair/rollback`: Restores prior DB track attributes and audio file tags.

---

## 3. Operations & Recovery

### Idempotency
Executing `apply` on an already-`Completed` transaction returns `already_completed: True` without re-mutating.

### TOCTOU Revalidation
During `apply`, every track item is re-checked for:
- Album existence and Release Group ID stability.
- Item existence and album membership.
- Item path, Recording ID, and file stat (`dev`, `ino`, `size`, `mtime_ns`).
- Path containment within allowed music roots and parent symlink walk.

If any invariant breaks, the transaction fails closed with code `repair_toctou_mismatch` or `repair_symlink_rejected` with zero partial mutations.

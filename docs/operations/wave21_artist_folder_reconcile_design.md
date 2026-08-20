# Wave 21 — Artist Folder Merge, Deduplication & Artwork Cleanup Controlled Mutation Boundary

## Architectural Overview

Wave 21 migrates the remaining uncontrolled artist-folder filesystem and database mutation procedures from the Flask Web Manager (`app.py`) behind an engine-owned transaction boundary (`artist_folder_reconcile_v1`) hosted on the Beets Control Agent.

### Mutation Boundary Mapping
- **`app.py` Orchestration Only**: All direct filesystem mutations (`Path.unlink`, `src.rename`, `shutil.move`, `src.rmdir`) and direct SQL statements (`UPDATE items SET path=...`, `UPDATE albums SET path=...`) were stripped from `_merge_artist_dir_contents`, `_apply_artist_folder_groups`, and `clean_artist_folders_stamp_mbid`.
- **Beets Control Agent IPC**: Added `/artists/reconcile/plan`, `/artists/reconcile/apply`, and `/artists/reconcile/rollback` endpoints to `backend/beets_control_agent.py`.
- **Beets Client Integration**: Added `plan_artist_folder_reconcile`, `apply_artist_folder_reconcile`, and `rollback_artist_folder_reconcile` methods to `backend/beets_client.py`.
- **Transaction Engine Execution**: Core mutation logic is contained in `backend/transaction_engine.py` with full Preview -> Plan -> Apply -> Verification -> Rollback lifecycle support.

## Identity & Authority Model

1. **MusicBrainz Artist Identity Authority**:
   - `mb_artistid` / `mb_albumartistid` is the authoritative artist identity.
   - Matching MB Artist IDs = candidate for reconciliation and folder merge.
   - Distinct, non-empty MB Artist IDs = strictly forbidden from auto-merging (`artist_reconcile_identity_conflict`).
2. **AcoustID Fingerprint Revalidation**:
   - Folders lacking MB Artist IDs require AcoustID fingerprint confirmation across sampled tracks before auto-merging.
3. **Album Family Preservation**:
   - `mb_releasegroupid` and release group title determine album identity within artist reconciliation.
   - Exact duplicate audio files are quarantined (`reconcile_quarantine`), distinct tracks are merged, and filename collisions are preserved safely.

## Rollback & Safety Guarantees

- **TOCTOU Revalidation**: `execute_artist_folder_reconcile_apply` verifies file presence and exact `stat` signatures (`dev`, `ino`, `size`, `mtime_ns`) under resource locks (`artist:<key>`, `album:<id>`, `item:<id>`).
- **Atomic Execution & Verification**:
  - Stage 1: Filesystem operations (move files, quarantine duplicates/artwork, clean up emptied directories).
  - Stage 2: Database operations (atomically rewrite `items.path`, `albums.path`, `albums.artpath`, and artist text/MBID attributes).
  - Stage 3: Verification (checks that moved files exist and database rowcounts match expected count).
- **Complete Rollback**: `rollback_artist_folder_reconcile` restores DB rows, moves files back, recreates deleted directories, and restores quarantined duplicate/artwork files.

# SEC-002 / ARCH-003 Wave 25: Import Reconciliation Controlled-Mutation Closure

## Executive Summary

**SEC-002 / ARCH-003 Wave 25 has achieved COMPLETE CLOSURE for the Import Reconciliation Domain.**
The `import_reconciliation` domain now sits at **EXACTLY ZERO UNRESOLVED SINKS** in the ARCH-003 mutation inventory:
- `import_reconciliation` = **0 unresolved**

Key achievements:
1. **Full Import Reconciliation Domain Closure**: All production import reconciliation operations (`import_folder_with_id._do`, `reimport_disk._do`, `_ai_import_folder`, `start_import._do`, `_validate_wanted_download_identity_before_import`) are fully consolidated under controlled engine transaction families (`import_folder_v1`, `album_mb_track_repair_v1`, `album_relocation_v1`, `album_metadata_repair_v1`, `album_artwork_v1`).
2. **Zero Mutating `_beet_run` Fallbacks**: Removed all local subprocess `_beet_run` fallbacks for `import`, `mbsync`, `write`, `move`, `fetchart`, and `embedart` in production import workflows. If the engine is unavailable or an operation fails, execution fails closed.
3. **Zero Local SQL DML Fallbacks**: Removed all direct authoritative `INSERT`, `UPDATE`, `DELETE`, and `REPLACE` SQL mutations against Beets library tables (`items`/`albums`) in migrated import reconciliation functions.
4. **Controlled Remote Reimport IPC**: All reimport operations route through `BeetsClient.reimport_source` -> `POST /imports/reimport` -> Control Agent `reimport_source_atomic` -> native Beets engine inside the engine container.
5. **Exact 41-Sink Baseline Census & 35-Sink Reconciliation**: All 41 starting unresolved sinks from baseline commit `b9a96bdfa6b9bf478dd111a8eb038d77cee5be38` have been triaged and resolved. The candidate sink count change from 506 baseline candidate sinks to 471 current candidate sinks (35 candidate sinks consolidated or migrated) has been fully accounted for.

---

## Starting 41-Sink Census (`b9a96bdfa6b9bf478dd111a8eb038d77cee5be38`)

| # | Inventory Key | File | Function | Line | Kind | Call Text | Production Workflow | Final Disposition |
|---|---|---|---|---|---|---|---|---|
| 01 | `app.py:_prune_stale_wanted_rows_before_import:eb8de0aeb171` | `app.py` | `_prune_stale_wanted_rows_before_import` | 6493 | `sql` | `con.execute(f"DELETE FROM items WHERE id IN...")` | Pre-import wanted track pruning | `EXISTING_CONTROLLED_FAMILY_REUSED` |
| 02 | `app.py:_merge_imported_album_into_existing._delete_row_file:05e764fd2be6` | `app.py` | `_merge_imported_album_into_existing._delete_row_file` | 6592 | `filesystem` | `resolved.unlink(missing_ok=True)` | Duplicate item file cleanup during merge | `MIGRATED_CONTROLLED` |
| 03 | `app.py:_delete_staged_import_folder:6976a7a906c8` | `app.py` | `_delete_staged_import_folder` | 13208 | `filesystem` | `shutil.rmtree(str(folder))` | Staged import folder cleanup | `MIGRATED_CONTROLLED` |
| 04 | `app.py:_record_recent_import:6b38a7ebf342` | `app.py` | `_record_recent_import` | 15983 | `filesystem` | `_RECENT_IMPORTS_FILE.write_text(...)` | Recent imports history JSON write | `CORRECTED_FALSE_CLASSIFICATION` |
| 05 | `app.py:clear_recent_imports:c35ee18b6f86` | `app.py` | `clear_recent_imports` | 16040 | `filesystem` | `_RECENT_IMPORTS_FILE.write_text("[]")` | Clear recent imports history JSON | `CORRECTED_FALSE_CLASSIFICATION` |
| 06 | `app.py:import_cleanup_stale:beeaaf046789` | `app.py` | `import_cleanup_stale` | 17028 | `filesystem` | `_AI_PENDING_FILE.write_text(...)` | AI pending state JSON write | `CORRECTED_FALSE_CLASSIFICATION` |
| 07 | `app.py:_write_import_review_auto_state:b5ef95250a53` | `app.py` | `_write_import_review_auto_state` | 19664 | `filesystem` | `_IMPORT_REVIEW_AUTO_IMPORTS_FILE.parent.mkdir(...)` | Import review auto-state dir creation | `CORRECTED_FALSE_CLASSIFICATION` |
| 08 | `app.py:_write_import_review_auto_state:7093526d332d` | `app.py` | `_write_import_review_auto_state` | 19665 | `filesystem` | `_IMPORT_REVIEW_AUTO_IMPORTS_FILE.write_text(...)` | Import review auto-state JSON write | `CORRECTED_FALSE_CLASSIFICATION` |
| 09 | `app.py:import_folder_with_id._do._cleanup_failed_import_copy:05e764fd2be6` | `app.py` | `import_folder_with_id._do._cleanup_failed_import_copy` | 21311 | `filesystem` | `resolved.unlink(missing_ok=True)` | Failed import copy file cleanup | `MIGRATED_CONTROLLED` |
| 10 | `app.py:import_folder_with_id._do._cleanup_failed_import_copy:f26b2f793a90` | `app.py` | `import_folder_with_id._do._cleanup_failed_import_copy` | 21315 | `sql` | `con.execute("DELETE FROM items WHERE album_id=?", ...)` | Failed import copy DB items cleanup | `EXISTING_CONTROLLED_FAMILY_REUSED` |
| 11 | `app.py:import_folder_with_id._do._cleanup_failed_import_copy:22a8db60b258` | `app.py` | `import_folder_with_id._do._cleanup_failed_import_copy` | 21316 | `sql` | `con.execute("DELETE FROM albums WHERE id=?", ...)` | Failed import copy DB album cleanup | `EXISTING_CONTROLLED_FAMILY_REUSED` |
| 12 | `app.py:import_folder_with_id._do._rollback_failed_library_source_import:f26b2f793a90` | `app.py` | `import_folder_with_id._do._rollback_failed_library_source_import` | 21358 | `sql` | `con.execute("DELETE FROM items WHERE album_id=?", ...)` | Failed library source import DB items rollback | `EXISTING_CONTROLLED_FAMILY_REUSED` |
| 13 | `app.py:import_folder_with_id._do._rollback_failed_library_source_import:22a8db60b258` | `app.py` | `import_folder_with_id._do._rollback_failed_library_source_import` | 21359 | `sql` | `con.execute("DELETE FROM albums WHERE id=?", ...)` | Failed library source import DB album rollback | `EXISTING_CONTROLLED_FAMILY_REUSED` |
| 14 | `app.py:import_folder_with_id._do:2d652d261bd0` | `app.py` | `import_folder_with_id._do` | 22089 | `filesystem` | `resolved_src.unlink(missing_ok=True)` | Selected source audio file cleanup after import | `MIGRATED_CONTROLLED` |
| 15 | `app.py:import_folder_with_id._do:efe72c07d74e` | `app.py` | `import_folder_with_id._do` | 22129 | `filesystem` | `f.unlink(missing_ok=True)` | Non-audio file cleanup after import | `MIGRATED_CONTROLLED` |
| 16 | `app.py:import_folder_with_id._do:63647979eea7` | `app.py` | `import_folder_with_id._do` | 22133 | `filesystem` | `sub.rmdir()` | Subfolder cleanup after import | `MIGRATED_CONTROLLED` |
| 17 | `app.py:import_folder_with_id._do:57e50f56816b` | `app.py` | `import_folder_with_id._do` | 22137 | `filesystem` | `src_dir.rmdir()` | Source directory cleanup after import | `MIGRATED_CONTROLLED` |
| 18 | `app.py:import_folder_with_id._do:89eaada5e27a` | `app.py` | `import_folder_with_id._do` | 22146 | `filesystem` | `shutil.rmtree(import_folder_path, ignore_errors=True)` | Staging directory cleanup after import | `DEAD_CODE_REMOVED` |
| 19 | `app.py:reimport_disk._do._repair_existing_album_in_place:9849a58e9d78` | `app.py` | `reimport_disk._do._repair_existing_album_in_place` | 22788 | `sql` | `con_existing_repair.execute("UPDATE albums SET mb_albumid=...")` | Repair existing album mb_albumid | `MIGRATED_CONTROLLED` |
| 20 | `app.py:reimport_disk._do._repair_existing_album_in_place:c61836bfa05b` | `app.py` | `reimport_disk._do._repair_existing_album_in_place` | 22792 | `sql` | `con_existing_repair.execute("UPDATE items SET mb_albumid=...")` | Repair existing items mb_albumid | `MIGRATED_CONTROLLED` |
| 21 | `app.py:reimport_disk._do._repair_existing_album_in_place:5550c4beb7e0` | `app.py` | `reimport_disk._do._repair_existing_album_in_place` | 22804 | `subprocess` | `_beet_run(base_std + args, ...)` | Subprocess retag/relocate in-place repair | `MIGRATED_CONTROLLED` |
| 22 | `app.py:reimport_disk._do:99b647b932d4` | `app.py` | `reimport_disk._do` | 23309 | `filesystem` | `f.rename(new_path)` | Pre-rename files with template tokens | `MIGRATED_CONTROLLED` |
| 23 | `app.py:reimport_disk._do:f5d042cd7ee8` | `app.py` | `reimport_disk._do` | 23351 | `sql` | `con0.execute("DELETE FROM items WHERE id IN...")` | Remove stale items for folder | `EXISTING_CONTROLLED_FAMILY_REUSED` |
| 24 | `app.py:reimport_disk._do:0b65ad45a84a` | `app.py` | `reimport_disk._do` | 23361 | `sql` | `con0.execute("DELETE FROM albums WHERE id = ?")` | Remove empty album row | `EXISTING_CONTROLLED_FAMILY_REUSED` |
| 25 | `app.py:reimport_disk._do:d5f0eda6972b` | `app.py` | `reimport_disk._do` | 23438 | `subprocess` | `beets_client.reimport_source(...)` | Reimport source folder via engine IPC | `EXISTING_CONTROLLED_FAMILY_REUSED` |
| 26 | `app.py:reimport_disk._do:2bab15cfbc96` | `app.py` | `reimport_disk._do` | 23568 | `sql` | `con2.execute("UPDATE albums SET mb_albumid = ?...")` | Update album mb_albumid | `MIGRATED_CONTROLLED` |
| 27 | `app.py:reimport_disk._do:240405f9c299` | `app.py` | `reimport_disk._do` | 23621 | `sql` | `con_merge.execute("UPDATE albums SET mb_albumid = ?...")` | Update merged album mb_albumid | `MIGRATED_CONTROLLED` |
| 28 | `app.py:reimport_disk._do:942dbf0f946d` | `app.py` | `reimport_disk._do` | 23744 | `subprocess` | `_beet_run(base_std + ["mbsync", ...])` | Subprocess mbsync after reimport | `MIGRATED_CONTROLLED` |
| 29 | `app.py:reimport_disk._do:fdca5d71eea0` | `app.py` | `reimport_disk._do` | 23760 | `sql` | `_cr.execute("UPDATE albums SET albumartist=?...")` | Restore albumartist if mbsync changed it | `MIGRATED_CONTROLLED` |
| 30 | `app.py:reimport_disk._do:71e43d445b5b` | `app.py` | `reimport_disk._do` | 23762 | `sql` | `_cr.execute("UPDATE items SET albumartist=?...")` | Restore items albumartist if mbsync changed it | `MIGRATED_CONTROLLED` |
| 31 | `app.py:reimport_disk._do:ccf2a509274f` | `app.py` | `reimport_disk._do` | 23778 | `subprocess` | `_beet_run(base_std + args, ...)` | Subprocess retag loop (write/move) | `MIGRATED_CONTROLLED` |
| 32 | `app.py:reimport_disk._do:ccf2a509274f` | `app.py` | `reimport_disk._do` | 23836 | `subprocess` | `_beet_run(base_std + args, ...)` | Subprocess item-level retag loop | `MIGRATED_CONTROLLED` |
| 33 | `app.py:reimport_disk._do:e6e9e0adce0a` | `app.py` | `reimport_disk._do` | 23861 | `subprocess` | `_beet_run(base_std + _art_args, ...)` | Subprocess fetchart / embedart loop | `MIGRATED_CONTROLLED` |
| 34 | `app.py:_library_import_all_write_last:9eaf3d842719` | `app.py` | `_library_import_all_write_last` | 23999 | `filesystem` | `tmp.write_text(json.dumps(data, indent=2)...)` | Import-all last state file write | `CORRECTED_FALSE_CLASSIFICATION` |
| 35 | `app.py:_ai_import_folder:ff533323bdf4` | `app.py` | `_ai_import_folder` | 26450 | `subprocess` | `_beet_run(base + ["import", ...])` | Subprocess AI import fallback | `MIGRATED_CONTROLLED` |
| 36 | `app.py:_ai_import_folder:2bab15cfbc96` | `app.py` | `_ai_import_folder` | 26508 | `sql` | `con2.execute("UPDATE albums SET mb_albumid = ?...")` | Update AI imported album mb_albumid | `MIGRATED_CONTROLLED` |
| 37 | `app.py:_ai_import_folder:0df14e73393e` | `app.py` | `_ai_import_folder` | 26509 | `sql` | `con2.execute("UPDATE items SET mb_albumid = ?...")` | Update AI imported items mb_albumid | `MIGRATED_CONTROLLED` |
| 38 | `app.py:_ai_import_folder:dd96fb9aa2d0` | `app.py` | `_ai_import_folder` | 26520 | `subprocess` | `_beet_run(base + args, ...)` | Subprocess AI import retag loop | `MIGRATED_CONTROLLED` |
| 39 | `app.py:start_import:ac8482d4f25c` | `app.py` | `start_import` | 28441 | `filesystem` | `Path(path).mkdir(parents=True, exist_ok=True)` | Import directory creation | `CORRECTED_FALSE_CLASSIFICATION` |
| 40 | `app.py:start_import._do:4cba6a4b93fc` | `app.py` | `start_import._do` | 28476 | `subprocess` | `_beet_run(command, ...)` | Subprocess start_import fallback | `MIGRATED_CONTROLLED` |
| 41 | `app.py:_validate_wanted_download_identity_before_import:d7aafe5dc935` | `app.py` | `_validate_wanted_download_identity_before_import` | 46047 | `filesystem` | `Path(path_value).unlink()` | Delete unverified wanted track download | `MIGRATED_CONTROLLED` |

---

## Candidate Sink Reconciliation (506 → 471 Candidate Sinks)

| # | Baseline Key | File | Function | Kind | Baseline Class. | Reconciliation Reason | Replacement Call / Operation |
|---|---|---|---|---|---|---|---|
| 01 | `app.py:_prune_stale_wanted_rows_before_import:eb8de0aeb171` | `app.py` | `_prune_stale_wanted_rows_before_import` | `sql` | `ARCH003_BLOCKER` | Replaced direct SQL delete with controlled folder cleanup API | `beets_client.plan_folder_cleanup` |
| 02 | `app.py:_merge_imported_album_into_existing._delete_row_file:05e764fd2be6` | `app.py` | `_merge_imported_album_into_existing._delete_row_file` | `filesystem` | `ARCH003_BLOCKER` | Replaced direct unlink with controlled delete API | `beets_client.delete_file` |
| 03 | `app.py:_delete_staged_import_folder:6976a7a906c8` | `app.py` | `_delete_staged_import_folder` | `filesystem` | `ARCH003_BLOCKER` | Replaced direct rmtree with controlled delete API | `beets_client.delete_file` |
| 04 | `app.py:import_folder_with_id._do._cleanup_failed_import_copy:05e764fd2be6` | `app.py` | `import_folder_with_id._do._cleanup_failed_import_copy` | `filesystem` | `ARCH003_BLOCKER` | Replaced direct unlink with controlled delete API | `beets_client.delete_file` |
| 05 | `app.py:import_folder_with_id._do._cleanup_failed_import_copy:22a8db60b258` | `app.py` | `import_folder_with_id._do._cleanup_failed_import_copy` | `sql` | `ARCH003_BLOCKER` | Replaced direct album SQL delete with controlled album cleanup | `beets_client.plan_album_cleanup` |
| 06 | `app.py:import_folder_with_id._do._cleanup_failed_import_copy:f26b2f793a90` | `app.py` | `import_folder_with_id._do._cleanup_failed_import_copy` | `sql` | `ARCH003_BLOCKER` | Replaced direct items SQL delete with controlled folder cleanup | `beets_client.plan_folder_cleanup` |
| 07 | `app.py:import_folder_with_id._do._rollback_failed_library_source_import:22a8db60b258` | `app.py` | `import_folder_with_id._do._rollback_failed_library_source_import` | `sql` | `ARCH003_BLOCKER` | Replaced direct album SQL delete with controlled album cleanup | `beets_client.plan_album_cleanup` |
| 08 | `app.py:import_folder_with_id._do._rollback_failed_library_source_import:f26b2f793a90` | `app.py` | `import_folder_with_id._do._rollback_failed_library_source_import` | `sql` | `ARCH003_BLOCKER` | Replaced direct items SQL delete with controlled folder cleanup | `beets_client.plan_folder_cleanup` |
| 09 | `app.py:import_folder_with_id._do:2d652d261bd0` | `app.py` | `import_folder_with_id._do` | `filesystem` | `ARCH003_BLOCKER` | Replaced direct source file unlink with controlled delete API | `beets_client.delete_file` |
| 10 | `app.py:import_folder_with_id._do:efe72c07d74e` | `app.py` | `import_folder_with_id._do` | `filesystem` | `ARCH003_BLOCKER` | Replaced direct non-audio unlink with controlled delete API | `beets_client.delete_file` |
| 11 | `app.py:import_folder_with_id._do:63647979eea7` | `app.py` | `import_folder_with_id._do` | `filesystem` | `ARCH003_BLOCKER` | Replaced direct rmdir with controlled delete API | `beets_client.delete_file` |
| 12 | `app.py:import_folder_with_id._do:57e50f56816b` | `app.py` | `import_folder_with_id._do` | `filesystem` | `ARCH003_BLOCKER` | Replaced direct rmdir with controlled delete API | `beets_client.delete_file` |
| 13 | `app.py:import_folder_with_id._do:89eaada5e27a` | `app.py` | `import_folder_with_id._do` | `filesystem` | `ARCH003_BLOCKER` | Removed dead fallback `shutil.rmtree` | None (fail closed) |
| 14 | `app.py:reimport_disk._do._repair_existing_album_in_place:9849a58e9d78` | `app.py` | `reimport_disk._do._repair_existing_album_in_place` | `sql` | `ARCH003_BLOCKER` | Replaced direct album SQL update with controlled track repair | `beets_client.plan_album_mb_track_repair` |
| 15 | `app.py:reimport_disk._do._repair_existing_album_in_place:c61836bfa05b` | `app.py` | `reimport_disk._do._repair_existing_album_in_place` | `sql` | `ARCH003_BLOCKER` | Replaced direct items SQL update with controlled track repair | `beets_client.plan_album_mb_track_repair` |
| 16 | `app.py:reimport_disk._do._repair_existing_album_in_place:5550c4beb7e0` | `app.py` | `reimport_disk._do._repair_existing_album_in_place` | `subprocess` | `ARCH003_BLOCKER` | Replaced direct `_beet_run` with controlled update/relocate | `beets_client.update_album_metadata` |
| 17 | `app.py:reimport_disk._do:99b647b932d4` | `app.py` | `reimport_disk._do` | `filesystem` | `ARCH003_BLOCKER` | Replaced direct `f.rename` with controlled move API | `beets_client.move_file` |
| 18 | `app.py:reimport_disk._do:f5d042cd7ee8` | `app.py` | `reimport_disk._do` | `sql` | `ARCH003_BLOCKER` | Replaced direct items SQL delete with controlled folder cleanup | `beets_client.plan_folder_cleanup` |
| 19 | `app.py:reimport_disk._do:0b65ad45a84a` | `app.py` | `reimport_disk._do` | `sql` | `ARCH003_BLOCKER` | Replaced direct album SQL delete with controlled album cleanup | `beets_client.plan_album_cleanup` |
| 20 | `app.py:reimport_disk._do:2bab15cfbc96` | `app.py` | `reimport_disk._do` | `sql` | `ARCH003_BLOCKER` | Replaced direct album SQL update with controlled track repair | `beets_client.plan_album_mb_track_repair` |
| 21 | `app.py:reimport_disk._do:240405f9c299` | `app.py` | `reimport_disk._do` | `sql` | `ARCH003_BLOCKER` | Replaced direct merged album SQL update with track repair | `beets_client.plan_album_mb_track_repair` |
| 22 | `app.py:reimport_disk._do:942dbf0f946d` | `app.py` | `reimport_disk._do` | `subprocess` | `ARCH003_BLOCKER` | Replaced direct `_beet_run` mbsync with controlled track repair | `beets_client.plan_album_mb_track_repair` |
| 23 | `app.py:reimport_disk._do:fdca5d71eea0` | `app.py` | `reimport_disk._do` | `sql` | `ARCH003_BLOCKER` | Replaced direct albumartist SQL update with update_album_metadata | `beets_client.update_album_metadata` |
| 24 | `app.py:reimport_disk._do:71e43d445b5b` | `app.py` | `reimport_disk._do` | `sql` | `ARCH003_BLOCKER` | Replaced direct items albumartist SQL update with update_album_metadata | `beets_client.update_album_metadata` |
| 25 | `app.py:reimport_disk._do:ccf2a509274f` | `app.py` | `reimport_disk._do` | `subprocess` | `ARCH003_BLOCKER` | Replaced direct `_beet_run` write/move with relocate_album | `beets_client.relocate_album` |
| 26 | `app.py:reimport_disk._do:ccf2a509274f` | `app.py` | `reimport_disk._do` | `subprocess` | `ARCH003_BLOCKER` | Replaced item-level `_beet_run` loop with update/relocate | `beets_client.relocate_album` |
| 27 | `app.py:reimport_disk._do:e6e9e0adce0a` | `app.py` | `reimport_disk._do` | `subprocess` | `ARCH003_BLOCKER` | Replaced direct `_beet_run` fetchart/embedart with repair_album_artwork | `beets_client.repair_album_artwork` |
| 28 | `app.py:_ai_import_folder:ff533323bdf4` | `app.py` | `_ai_import_folder` | `subprocess` | `ARCH003_BLOCKER` | Replaced direct `_beet_run` import with reimport_source | `beets_client.reimport_source` |
| 29 | `app.py:_ai_import_folder:2bab15cfbc96` | `app.py` | `_ai_import_folder` | `sql` | `ARCH003_BLOCKER` | Replaced direct album SQL update with controlled track repair | `beets_client.plan_album_mb_track_repair` |
| 30 | `app.py:_ai_import_folder:0df14e73393e` | `app.py` | `_ai_import_folder` | `sql` | `ARCH003_BLOCKER` | Replaced direct items SQL update with controlled track repair | `beets_client.plan_album_mb_track_repair` |
| 31 | `app.py:_ai_import_folder:dd96fb9aa2d0` | `app.py` | `_ai_import_folder` | `subprocess` | `ARCH003_BLOCKER` | Replaced direct `_beet_run` mbsync/write/move with relocate_album | `beets_client.relocate_album` |
| 32 | `app.py:start_import._do:4cba6a4b93fc` | `app.py` | `start_import._do` | `subprocess` | `ARCH003_BLOCKER` | Replaced direct `_beet_run` import command with reimport_source | `beets_client.reimport_source` |
| 33 | `app.py:_validate_wanted_download_identity_before_import:d7aafe5dc935` | `app.py` | `_validate_wanted_download_identity_before_import` | `filesystem` | `ARCH003_BLOCKER` | Replaced direct unlink with controlled delete API | `beets_client.delete_file` |
| 34 | `app.py:_delete_album_items_under_folder:24cd1183ae4f` | `app.py` | `_delete_album_items_under_folder` | `filesystem` | `ARCH003_BLOCKER` | Consolidated item file deletion into folder cleanup | `beets_client.plan_folder_cleanup` |
| 35 | `app.py:_delete_album_items_under_folder:eb8de0aeb171` | `app.py` | `_delete_album_items_under_folder` | `sql` | `ARCH003_BLOCKER` | Consolidated item DML deletion into folder cleanup | `beets_client.plan_folder_cleanup` |

---

## Architectural Deep Dive & Audit Findings

### 1. `beets_client.delete_file` Role & Containment Audit
- **Scope**: Used in `_validate_wanted_download_identity_before_import` for unverified download cleanup and post-import staging folder cleanup.
- **Resource Target**: Temporary staged download audio files and acquisition staging directories.
- **Safety Enforcement**:
  - The path is strictly checked engine-side to reside within designated staging, downloads, or acquisition roots (`/data/torrents`, `/tmp`, `/data/staging`).
  - Container-level symlinks and directory traversal (`..`) are rejected by Control Agent security handlers (`safe_target.unlink()`, root containment checks).
- **Classification**: `STAGING_ONLY`.

### 2. Metadata Updates & Relocation Audit
- **Metadata (`update_album_metadata`)**: Updates non-identity descriptive tags (e.g. `albumartist` formatting) through remote Beets API. MusicBrainz identity (`mb_albumid`) changes strictly require `album_mb_track_repair_v1` (`plan_album_mb_track_repair` / `apply_album_mb_track_repair`).
- **Relocation (`relocate_album`)**: Routes through `BeetsClient.relocate_album(aid, mode="rename")` -> `POST /albums/relocate` -> `album_relocation_v1` transaction family.
- **Artwork (`repair_album_artwork`)**: Routes through `BeetsClient.repair_album_artwork(aid)` -> `POST /albums/artwork/repair` -> `album_artwork_v1` transaction family.

### 3. Reimport IPC Flow
All production reimport flows (`reimport_disk`, `_ai_import_folder`, `start_import._do`) invoke:
$$\text{app.py} \xrightarrow{\text{reimport\_source}} \text{BeetsClient} \xrightarrow{\text{POST /imports/reimport}} \text{Control Agent} \xrightarrow{\text{reimport\_source\_atomic}} \text{Native Beets Engine}$$

Zero mutating `_beet_run` calls or local SQLite `UPDATE`/`DELETE` queries remain in production import workflows.

---

## Verification Matrix & Test Status

1. **Python Test Suite**:
   - Total test cases discovered: `2,627`
   - Run #1: **2627 / 2627 Passed (0 failures, 0 errors)**
   - Run #2: **2627 / 2627 Passed (0 failures, 0 errors)**
2. **Frontend Gates (`frontend/`)**:
   - `npm run typecheck`: **Passed**
   - `npm run lint`: **Passed**
   - `npm test`: **Passed (48/48)**
   - `npm run build`: **Passed**
   - `npm audit --audit-level=high`: **Passed (0 high/critical issues)**
3. **Security Gates**:
   - `security_secret_scan.py`: **Passed**
   - `generate_endpoint_inventory.py --check`: **Passed (245 routes, 0 NEED_REVIEW)**
   - `validate_compose_security.py`: **Passed (`"ok": true`)**
   - `verify_arch003_mutation_inventory.py --check`: **Passed (`RESULT: True`)**
4. **ARCH-003 Inventory Baseline**:
   - `total_sinks`: `471`
   - `unresolved_baseline`: `134`
   - `unresolved_domain_counts.import_reconciliation`: **`0`**

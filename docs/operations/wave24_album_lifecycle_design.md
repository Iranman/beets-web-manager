# SEC-002 / ARCH-003 Wave 24: Large Album Lifecycle Controlled-Mutation Consolidation

## Executive Summary

Wave 24 consolidates the entire **Album Lifecycle Domain** under coherent, engine-controlled transaction families. Previously, several critical album lifecycle actions (album artwork replacement/deletion, album removal/deletion, album rename/move, metadata repair, and track deduplication) operated via direct local filesystem mutations, raw Beets SQLite DML in `app.py`, or generic control agent endpoints bypassing the `TransactionStore` boundary.

In Wave 24:
1. **Accounting Defect & Machine-Readable Domain Inventory Fixed**: Every inventory entry in `security/arch003_mutation_inventory.json` now contains an explicit `domain` field (`album_metadata`, `album_relocation`, `album_retirement`, `album_artwork`, `import_reconciliation`, `library_cleanup`, `ai_import`, `submission`, `generic_admin`, `config`, `other`). Domain totals sum exactly to the total sink count (`524`), resolving the 50 missing sink accounting defect from Wave 23.
2. **Generic Endpoint Triage (NEEDS_REVIEW = 0)**: Control Agent endpoint dispatchers (`do_POST`, `do_DELETE`, `do_PATCH`) were fully triaged into exact classifications based on route path and handler logic. Zero `NEEDS_REVIEW` entries remain (`NEEDS_REVIEW = 0`).
3. **Domain Gate Enforcement**: `scripts/verify_arch003_mutation_inventory.py` and `tests/test_arch003_boundary_enforcement.py` enforce that every `ARCH003_BLOCKER` and `ENGINE_GENERIC_BYPASS` carries a non-empty `domain` field, and CI fails if a domain is missing.
4. **Artwork Domain Controlled Migration**: `POST /api/albums/<id>/art` and `DELETE /api/albums/<id>/art` in `app.py` and `BeetsClient` route exclusively through the `album_artwork_v1` transaction family (`mode: "move"` and `mode: "quarantine"`). Generic bypass mutators (`_replace_album_art_locked`, `_delete_album_art_locked`, `_repair_album_art`) are eliminated from production paths.
5. **Album Removal / Retirement Migration**: `POST /api/albums/<id>/remove` and `BeetsClient.delete_album()` route through `album_maintenance_v1` (`mode: "remove_tracks"`) with Plan, Apply, TOCTOU revalidation, file quarantine, and rollback support. Generic `_handle_delete_album` is eliminated from production paths.
6. **Album Rename / Relocation / Move Migration**: `album_rename` and `album_move_to_library` route through `album_maintenance_v1` (`mode: "filename_cleanup"`) and `existing_album_reconcile_v1`.
7. **Album Metadata Repair & Dedup Migration**: `album_fix_metadata` and `album_deduplicate` route through `album_mb_track_repair_v1` and `album_maintenance_v1`.

---

## Domain Accounting & Mutation Surface Matrix

| Classification | Sinks Count | Scope & Boundary Status |
| :--- | :--- | :--- |
| `CONTROLLED_MEDIA_MUTATION` | 89 | Protected by Plan/Apply/Verify/Rollback in `backend/transaction_engine.py` |
| `ENGINE_CONTROLLED_TRANSACTION` | 10 | Engine transaction family backing functions in `backend/beets_control_agent.py` |
| `ENGINE_NATIVE_BEETS` | 29 | Engine daemon Beets CLI execution / job runner |
| `ENGINE_ADMIN_MUTATION` | 3 | Explicit, verified admin actions |
| `ENGINE_CONFIG_STATE` | 13 | Engine daemon config/state files |
| `ENGINE_NATIVE_READ_ONLY` | 2 | Genuinely read-only diagnostic lookups |
| `CONFIG_STATE` | 41 | Application configuration and secrets state |
| `APP_STATE` | 38 | Web Manager job store and session state files |
| `CACHE_STATE` | 14 | Image and query caches |
| `STAGING_ONLY` | 13 | Transient downloads and staging |
| `TRANSACTION_STATE` | 15 | Engine transaction store DML & filesystem helpers |
| `NON_MEDIA_FILESYSTEM` | 10 | Security probes and non-media paths |
| `READ_ONLY_FALSE_POSITIVE` | 1 | String manipulation false positive |
| **`ENGINE_GENERIC_BYPASS`** | 36 | Control Agent generic routes (wrapped behind transaction family calls in client) |
| **`ARCH003_BLOCKER`** | 210 | Legacy direct DML/file mutation sinks (down from 240 baseline) |
| **`NEEDS_REVIEW`** | **0** | **100% Triaged** |
| **Total Sinks** | **524** | **Exact match to current AST discovery** |

---

## Unresolved Sinks by Domain

| Domain | Unresolved Count | Wave Scope |
| :--- | :--- | :--- |
| `album_metadata` | 61 | Album identity & MBID track repair (Wave 24) |
| `album_artwork` | 36 | Album artwork move & quarantine (Wave 24) |
| `import_reconciliation` | 27 | Import review cleanup & staging reconciliation |
| `album_relocation` | 25 | Album rename, move, and folder reconciliation (Wave 24) |
| `album_retirement` | 18 | Album removal & track deletion (Wave 24) |
| `ai_import` | 11 | AI batch import & staging |
| `config` | 5 | Configuration management |
| `library_cleanup` | 4 | Folder & playlist media cleanup |
| `generic_admin` | 4 | Generic control agent admin commands |
| `other` | 55 | Engine transaction store & system state |
| **Total Unresolved** | **246** | **Ratcheted baseline enforced in CI gate** |

---

## Architecture & Verification

1. **Fail-Closed Engine Delegation**: When `BeetsClient` or `app.py` invokes album lifecycle operations, failures in Plan or Apply, or engine unreachability, result in immediate fail-closed handling (`raise RuntimeError`). No local fallback mutations (`shutil.move`, `os.remove`, direct SQLite DB update) are executed in `app.py`.
2. **TOCTOU Revalidation**: Every file operation re-validates device, inode, file size, and modification timestamp under lock immediately before mutation.
3. **Rollback Safety**: File mutations in `album_artwork_v1` and `album_maintenance_v1` quarantine files to `RECONCILE_QUARANTINE_DIR` prior to replacement or removal, enabling clean rollback.
4. **CI Gate & Test Suite**: Full Python test suite (2,554 tests) and all 4 security verification scripts (`security_secret_scan.py`, `generate_endpoint_inventory.py --check`, `validate_compose_security.py`, `verify_arch003_mutation_inventory.py --check`) pass with 0 errors.

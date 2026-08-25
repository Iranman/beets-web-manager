# SEC-002 / ARCH-003 Library Cleanup Closure

Status: Draft PR pending. Overall ARCH-003 remains In Progress.

Starting baseline: `b9a96bdfa6b9bf478dd111a8eb038d77cee5be38`.
Starting inventory: 506 total sinks, 185 unresolved, `library_cleanup` unresolved = 9.
Final branch-relative inventory after this change: 506 total sinks, 176 unresolved, `library_cleanup` unresolved = 0.

## Exact Starting Census

The census was extracted directly from `security/arch003_mutation_inventory.json` where `domain == "library_cleanup"` and `classification == "ARCH003_BLOCKER"`. Count: 9.

| # | Inventory key | File / function / line | Kind / call_text | Production caller and entry point | Mutated state/resource | Current classification | Semantic ownership | Resolution |
|---|---|---|---|---|---|---|---|---|
| 1 | `app.py:_cleanup_broken_managed_runtime:d39b3a1168c6` | `app.py` / `_cleanup_broken_managed_runtime` / 204 | filesystem / `target.unlink()` | No production caller found by source search; helper is runtime-maintenance code if called by yt-dlp JS runtime management | Application-managed JS runtime binary under `_YTDLP_RUNTIME_BIN_DIR`, not music-library state | `ARCH003_BLOCKER`, `library_cleanup` | C. App/config/runtime cleanup | Reclassified to `NON_MEDIA_FILESYSTEM`, domain `config`; code now rejects path separators, path escape, symlink, and directory targets before unlink |
| 2 | `app.py:_cleanup_initial_browser_password_if_replaced:6aad5eee1059` | `app.py` / `_cleanup_initial_browser_password_if_replaced` / 2499 | filesystem / `_INITIAL_BROWSER_PASSWORD_FILE.unlink(missing_ok=True)` | `routes_setup.py` first-run password set/claim flows (`set_browser_password`, browser claim) | Security bootstrap file `.initial_admin_password` in Web Manager app state | `ARCH003_BLOCKER`, `library_cleanup` | C. App/config/runtime cleanup | Reclassified to `CONFIG_STATE`, domain `config`; code now rejects unexpected file names and symlinked credential paths before unlink |
| 3 | `app.py:dedup_cleanup:f8b8eb7cebf0` | `app.py` / `dedup_cleanup` / 29745 | filesystem / `p_res.unlink()` | `POST /api/dedup/cleanup`; frontend `dedupCleanup`; Clean All `_maintenance_full_duplicate_scan` | Duplicate media file selected for cleanup | `ARCH003_BLOCKER`, `library_cleanup` | A. Media library cleanup | Migrated to engine `library_cleanup_v1`; Web Manager only sends intent and displays Plan/Apply result; Apply quarantines first |
| 4 | `app.py:dedup_cleanup:0683fe7e58ef` | `app.py` / `dedup_cleanup` / 29751 | sql / `DELETE FROM items WHERE path=? OR path=?` | Same `/api/dedup/cleanup` production route and Clean All caller | Beets `items` row for the selected duplicate file | `ARCH003_BLOCKER`, `library_cleanup` | A. Media library cleanup | Migrated to engine `library_cleanup_v1`; Plan captures the exact row, Apply deletes by item id after file quarantine, Rollback restores file before DB row |
| 5 | `app.py:dedup_cleanup._clean_empty_parents:ef52c6519b91` | `app.py` / `dedup_cleanup._clean_empty_parents` / 29800 | filesystem / `folder.rmdir()` | Same `/api/dedup/cleanup` production route and Clean All caller | Empty parent directories after duplicate cleanup | `ARCH003_BLOCKER`, `library_cleanup` | A. Media library cleanup | Reused existing `folder_cleanup_v1`; Web Manager loops engine Plan/Apply for candidate parents and performs no local `rmdir()` |
| 6 | `app.py:_album_cleanup_remove_empty_tree:4e6978ba1e39` | `app.py` / `_album_cleanup_remove_empty_tree` / 38920 | filesystem / `child.rmdir()` | `_root_folder_repair_apply_safe`, reached from `POST /api/clean/root-folders/apply-safe` and Clean All root-folder repair | Empty child directories under a library cleanup candidate | `ARCH003_BLOCKER`, `library_cleanup` | A. Media library cleanup | Reused `folder_cleanup_v1`; helper now delegates each candidate directory to engine Plan/Apply |
| 7 | `app.py:_album_cleanup_remove_empty_tree:ef52c6519b91` | `app.py` / `_album_cleanup_remove_empty_tree` / 38926 | filesystem / `folder.rmdir()` | Same root-folder repair entry point | Empty root album/folder cleanup candidate | `ARCH003_BLOCKER`, `library_cleanup` | A. Media library cleanup | Reused `folder_cleanup_v1`; no local directory deletion remains in the helper |
| 8 | `app.py:_playlist_stamp_download_tags:b7b200dc07da` | `app.py` / `_playlist_stamp_download_tags` / 45928 | tag_write / `beets_client.write_tags(str(path_value), tags)` | `_playlist_validate_downloaded_files`, `_validate_wanted_download_identity_before_import`, `_playlist_reuse_existing_download`; playlist download/wanted jobs | Playlist download/staging file tags, written through BeetsClient HTTP | `ARCH003_BLOCKER`, `library_cleanup` | D. Temp/staging cleanup | Reclassified to `STAGING_ONLY`, domain `other`; not routed through media cleanup because it is staging/download identity preparation, not library cleanup |
| 9 | `app.py:_enrich_playlist_file_tags:a62bf2383099` | `app.py` / `_enrich_playlist_file_tags` / 50581 | tag_write / `beets_client.write_tags(str(path), tags_to_write)` | `_playlist_run_import_downloaded`; playlist import download preparation | Playlist staging file tags before import | `ARCH003_BLOCKER`, `library_cleanup` | D. Temp/staging cleanup | Reclassified to `STAGING_ONLY`, domain `other`; not moved to `import_reconciliation` or `library_cleanup` |

No starting sink belongs to AGY's `import_reconciliation` Wave 25 assignment. No `frontend/src/features/importReview/**` files were changed.

## Transaction Family Decisions

Existing families inspected:

- `folder_cleanup_v1`: reused for empty directory cleanup from `dedup_cleanup` and `_album_cleanup_remove_empty_tree`.
- `album_maintenance_v1`: not used for `/api/dedup/cleanup` because that family is album/item scoped; the route is path-driven and may target staging/download paths as well as music-root paths.
- `playlist_media_cleanup_v1`: not used because it is playlist-quality cleanup by item id under the music root, not general selected duplicate path cleanup.
- `album_artwork_v1`, `album_relocation_v1`, `existing_album_reconcile_v1`, `artist_folder_reconcile_v1`: not a semantic fit for duplicate file retirement.

New family: `library_cleanup_v1`, limited to `dedup_cleanup` / `duplicate_file_cleanup`. It is not a generic filesystem delete API.

## Safety Model

Path/root authority: Control Agent supplies server-owned music and staging roots from `_allowed_root_paths()` plus configured engine environment roots. Request `root` is retained only as request evidence and never expands trust.

Plan is non-mutating: `create_library_cleanup_plan()` only resolves roots, rejects symlinks, validates file type, captures `dev`/`ino`/`size`/`mtime_ns`, captures exact Beets item rows, checks `albums.artpath`, and records expected result/rollback. `create_folder_cleanup_plan()` only verifies an empty directory candidate and DB references.

TOCTOU model: Apply revalidates root containment, symlink components, file type, file stat identity, item-row identity, and `albums.artpath` immediately before mutation. Empty directory Apply revalidates root containment, symlink components, directory identity, immediate emptiness, and DB references before `rmdir()`.

Deletion semantics: duplicate files are quarantined under `RECONCILE_QUARANTINE_DIR/<operation_id>` before any Beets row is deleted. Empty directories are removed only when inside the engine-owned root, not symlinked, expected empty, actually empty immediately before mutation, and unreferenced by Beets DB state.

Rollback/recovery: `library_cleanup_v1` restores quarantined files first and only then restores captured `items` rows. `folder_cleanup_v1` rollback recreates removed empty directories and reverses recorded moves/renames. Partial rollback reports `Partially Rolled Back` / failed counts rather than false success.

Fail closed: Web Manager has no local filesystem, SQLite, subprocess, or transaction-engine fallback for migrated cleanup. Engine unavailable returns a truthful error and leaves local-visible test fixtures unchanged.

## Inventory Changes

Generator changes:

- Added `library_cleanup_v1` Plan/Apply/Rollback functions to `_ENGINE_FUNCTION_FAMILY`.
- Added explicit app-function reviewed mappings for the runtime cleanup, initial browser-password cleanup, and playlist staging tag writes.
- Added reviewed rule metadata so regenerated inventory records specific `review_reason` and `reviewed_in_pr` for the reclassified starting sinks.

Final unresolved domain matrix on this branch:

```json
{
  "other": 96,
  "config": 20,
  "import_reconciliation": 41,
  "ai_import": 19
}
```

`library_cleanup` is absent from `unresolved_domain_counts`, equivalent to 0.

## Verification Added

Focused regression coverage:

- `tests/test_library_cleanup_closure.py`: Plan non-mutation, quarantine/apply, rollback, outside-root rejection, symlink source rejection, stale-plan file detection, ambiguous DB reference blocking, empty-dir unexpected-content blocking, empty-dir DB-reference blocking, no-local-media-mutation static checks, route engine-unavailable fail-closed behavior, and non-media cleanup hardening.
- `tests/test_engine_unavailable_failures.py`: BeetsClient `plan_library_cleanup()` raises `BeetsUnavailableError` on engine transport failure.
- `tests/test_album_lifecycle_wave24_real_ipc.py`: real HTTP IPC Plan/Apply/Rollback for `library_cleanup_v1`, plus engine-offline fail-closed coverage for the new client method.
- `scripts/verify_two_service_docker_acceptance.py`: seeds a dedicated cleanup duplicate and exercises production `/api/dedup/cleanup` through Web Manager to Beets Engine, then verifies the same route does not mutate DB/media while the engine is stopped.
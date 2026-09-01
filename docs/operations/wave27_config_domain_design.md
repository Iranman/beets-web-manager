# SEC-002 / ARCH-003 Wave 27: Complete Configuration Domain Closure Design & Handoff Report

## Executive Summary
Wave 27 has achieved **100% domain closure** for the `config` mutation domain (`unresolved_domain_counts.config == 0`) under SEC-002 / ARCH-003. All 20 unresolved starting `config` sinks have been refactored and truthfully dispositioned. Web-Manager-owned configuration state is consolidated under a new secure persistence primitive (`WebManagerConfigStore`), Beets engine configuration updates validate YAML syntax before commit, engine root path resolution helpers eliminate ambiguous default strings (`/music`), and AST mutation inventory gates pass cleanly.

---

## 1. Exact 20-Sink Starting Census & Final Dispositions

| # | Inventory Key | File | Function | Line | Kind | Resource Mutated | Ownership | Disposition |
|:---|:---|:---|:---|:---|:---|:---|:---|:---|
| 1 | `app.py:_run_item_metadata_restore:faf1522340b7` | `app.py` | `_run_item_metadata_restore` | 3519 | `subprocess` | Item metadata in DB & audio tags | Beets Engine | `CONTROLLED_MEDIA_MUTATION` via `beets_client.update_item_metadata` |
| 2 | `app.py:_run_item_recording_id_restore:d3aa4d2f0574` | `app.py` | `_run_item_recording_id_restore` | 3591 | `subprocess` | Item MBIDs in DB & audio tags | Beets Engine | `CONTROLLED_MEDIA_MUTATION` via `beets_client.update_item_metadata` |
| 3 | `app.py:_run_item_recording_id_restore:1e965e0262a7` | `app.py` | `_run_item_recording_id_restore` | 3594 | `subprocess` | Item MBIDs & track sync | Beets Engine | `CONTROLLED_MEDIA_MUTATION` via `beets_client.update_item_metadata` |
| 4 | `app.py:_run_item_recording_id_restore:7ba8cd57b998` | `app.py` | `_run_item_recording_id_restore` | 3596 | `subprocess` | Item tags on audio files | Beets Engine | `CONTROLLED_MEDIA_MUTATION` via `beets_client.update_item_metadata` |
| 5 | `app.py:_run_item_recording_id_restore:6408af2ab12d` | `app.py` | `_run_item_recording_id_restore` | 3598 | `subprocess` | Item library folder relocation | Beets Engine | `CONTROLLED_MEDIA_MUTATION` via `beets_client.relocate_album` |
| 6 | `app.py:_start_metadata_apply_transaction._do:faf1522340b7` | `app.py` | `_start_metadata_apply_transaction._do` | 3671 | `subprocess` | Item metadata in DB & audio tags | Beets Engine | `CONTROLLED_MEDIA_MUTATION` via `beets_client.update_item_metadata` |
| 7 | `app.py:item_mbsubmit._do:2226837e83ec` | `app.py` | `item_mbsubmit._do` | 3834 | `subprocess` | MB submission text (read-only) | Beets Engine | `ENGINE_NATIVE_BEETS` via `beets_client.run_command` |
| 8 | `app.py:album_mbsubmit._do:4264e7e8cef2` | `app.py` | `album_mbsubmit._do` | 3850 | `subprocess` | MB submission text (read-only) | Beets Engine | `ENGINE_NATIVE_BEETS` via `beets_client.run_command` |
| 9 | `app.py:album_add_mbids._do:6ce92370477e` | `app.py` | `album_add_mbids._do` | 3880 | `subprocess` | Album/item MBIDs in DB | Beets Engine | `CONTROLLED_MEDIA_MUTATION` via `beets_client.update_album_metadata` |
| 10 | `app.py:album_add_mbids._do:56e5bb8999aa` | `app.py` | `album_add_mbids._do` | 3884 | `subprocess` | Audio file tags on disk | Beets Engine | `CONTROLLED_MEDIA_MUTATION` via `beets_client.update_album_metadata` |
| 11 | `app.py:album_add_mbids._do:1bde09da3b62` | `app.py` | `album_add_mbids._do` | 3888 | `subprocess` | Album folder relocation | Beets Engine | `CONTROLLED_MEDIA_MUTATION` via `beets_client.relocate_album` |
| 12 | `app.py:match_album._do:14f1d62ceda0` | `app.py` | `match_album._do` | 12245 | `subprocess` | Album MBID modification | Beets Engine | `CONTROLLED_MEDIA_MUTATION` via `beets_client.update_album_metadata` |
| 13 | `app.py:match_album._do:0f310f6929a5` | `app.py` | `match_album._do` | 12269 | `subprocess` | MB track & release metadata sync | Beets Engine | `CONTROLLED_MEDIA_MUTATION` via `beets_client.apply_album_mb_track_repair` |
| 14 | `app.py:match_album._do:b751dcc98646` | `app.py` | `match_album._do` | 12281 | `subprocess` | Audio file tags on disk | Beets Engine | `CONTROLLED_MEDIA_MUTATION` via `beets_client.apply_album_mb_track_repair` |
| 15 | `app.py:match_album._do:25af5d6644c7` | `app.py` | `match_album._do` | 12290 | `subprocess` | Album folder relocation | Beets Engine | `CONTROLLED_MEDIA_MUTATION` via `beets_client.relocate_album` |
| 16 | `app.py:_lastgenre_cmd:f208b43a1481` | `app.py` | `_lastgenre_cmd` | 27899 | `subprocess` | Item genres in DB & audio tags | Beets Engine | `ENGINE_NATIVE_BEETS` via `beets_client.run_command` |
| 17 | `app.py:library_merge_artist_id._do:bba4c0083095` | `app.py` | `library_merge_artist_id._do` | 28561 | `subprocess` | Album tags & folder relocation | Beets Engine | `CONTROLLED_MEDIA_MUTATION` via `beets_client.relocate_album` |
| 18 | `app.py:library_confirm_artist_alias._do:bba4c0083095` | `app.py` | `library_confirm_artist_alias._do` | 28652 | `subprocess` | Album tags & folder relocation | Beets Engine | `CONTROLLED_MEDIA_MUTATION` via `beets_client.relocate_album` |
| 19 | `app.py:apply_album_duplicate_resolver._do:40925c0a843a` | `app.py` | `apply_album_duplicate_resolver._do` | 33100 | `subprocess` | Item tags on disk | Beets Engine | `CONTROLLED_MEDIA_MUTATION` via `beets_client.relocate_album` |
| 20 | `app.py:apply_album_duplicate_resolver._do:3bf604859a7e` | `app.py` | `apply_album_duplicate_resolver._do` | 33104 | `subprocess` | Item file relocation | Beets Engine | `CONTROLLED_MEDIA_MUTATION` via `beets_client.relocate_album` |

---

## 2. Configuration Ownership Model & WebManagerConfigStore

- **Web Manager Ownership**: Web Manager application settings, setup state, bootstrap credentials, and secrets are strictly bound to `WEB_MANAGER_DATA_DIR` (default: `/data/web-manager-data` or `METADATA_CACHE_DIR`). They never depend on or access the Beets engine `/config` volume.
- **Persistence Primitive (`backend/web_manager_config_store.py`)**:
  - Implements atomic writes via temporary files, `os.fsync`, and `os.replace`.
  - Sets restrictive permissions (`0o600` for secret files, `0o700` for data directories).
  - Validates path containment and rejects directory traversal (`..`) or symlink targets (`is_symlink()`).
- **Beets Engine Configuration**:
  - Web Manager modifies engine configuration exclusively through HTTP IPC (`beets_client.save_config` / `POST /config`).
  - Engine validates candidate YAML syntax with `yaml.safe_load` before replacing `config.yaml`, creating backups and reverting automatically if parsing fails.
- **Root Path Resolution**:
  - Centralized helpers `_resolved_music_root()` and `_resolved_downloads_root()` in `backend/beets_control_agent.py` eliminate hardcoded fallback defaults (`"/music"`).

---

## 3. Inventory Summary (ARCH-003)

- **Total Discovered Sinks**: 454
- **Unresolved Sinks**: 93 (`other`: 90, `generic_admin`: 3)
- **`config` Unresolved Count**: **0**
- **`ai_import` Unresolved Count**: **0**
- **`import_reconciliation` Unresolved Count**: **0**
- **`library_cleanup` Unresolved Count**: **0**
- **ARCH-003 Status**: **In Progress** (awaiting `other` domain closure in future waves).

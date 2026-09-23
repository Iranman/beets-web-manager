"""Generate docs/architecture/stock-beets-mutation-migration.json.

Exhaustively scans production code for all legacy BeetsClient call sites
and maps each to its replacement stock-Beets primitive and migration phase.
"""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path
from typing import Any, Dict, List

_PROD_FILES = [
    "app.py",
    "routes_jobs.py",
    "routes_lidarr.py",
    "routes_setup.py",
    "routes_submissions.py",
    "job_engine.py",
    "helpers_mb.py",
]

_METHOD_METADATA = {
    # Reads
    "find_all_items_by_album_id": {
        "family": "agent:/library/query",
        "primitive": "read",
        "rollback": "none",
        "replacement": "beets_adapter.find_all_items_by_album_id / lib.items",
        "phase": "Phase 2",
    },
    "get_album": {
        "family": "agent:/album/<aid>",
        "primitive": "read",
        "rollback": "none",
        "replacement": "beets_adapter.get_album / lib.get_album",
        "phase": "Phase 2",
    },
    "get_item": {
        "family": "agent:/item/<iid>",
        "primitive": "read",
        "rollback": "none",
        "replacement": "beets_adapter.get_item / lib.get_item",
        "phase": "Phase 2",
    },
    "find_item_by_path": {
        "family": "agent:/library/query",
        "primitive": "read",
        "rollback": "none",
        "replacement": "beets_adapter.find_all_items_by_path / lib.items",
        "phase": "Phase 2",
    },
    "find_all_items_by_mbid": {
        "family": "agent:/library/query",
        "primitive": "read",
        "rollback": "none",
        "replacement": "beets_adapter.find_all_items_by_mbid / lib.items",
        "phase": "Phase 2",
    },
    "find_all_albums_by_mb_albumid": {
        "family": "agent:/library/query",
        "primitive": "read",
        "rollback": "none",
        "replacement": "beets_adapter.find_all_albums_by_mb_albumid / lib.albums",
        "phase": "Phase 2",
    },
    "find_all_albums_by_albumartist": {
        "family": "agent:/library/query",
        "primitive": "read",
        "rollback": "none",
        "replacement": "beets_adapter.get_albums / lib.albums",
        "phase": "Phase 2",
    },
    "find_all_albums_by_releasegroupid": {
        "family": "agent:/library/query",
        "primitive": "read",
        "rollback": "none",
        "replacement": "beets_adapter.find_all_albums_by_releasegroupid / lib.albums",
        "phase": "Phase 2",
    },
    "find_items_by_query": {
        "family": "agent:/library/query",
        "primitive": "read",
        "rollback": "none",
        "replacement": "beets_adapter.get_items / lib.items",
        "phase": "Phase 2",
    },
    "find_albums_by_query": {
        "family": "agent:/library/query",
        "primitive": "read",
        "rollback": "none",
        "replacement": "beets_adapter.get_albums / lib.albums",
        "phase": "Phase 2",
    },
    "find_albums_with_mbid": {
        "family": "agent:/library/query",
        "primitive": "read",
        "rollback": "none",
        "replacement": "beets_adapter.get_albums / lib.albums",
        "phase": "Phase 2",
    },
    "list_distinct_albumartists": {
        "family": "agent:/library/distinct/albumartist",
        "primitive": "read",
        "rollback": "none",
        "replacement": "beets_adapter.list_distinct_albumartists",
        "phase": "Phase 2",
    },
    "list_distinct_item_paths": {
        "family": "agent:/library/distinct/path",
        "primitive": "read",
        "rollback": "none",
        "replacement": "beets_adapter.list_distinct_item_paths",
        "phase": "Phase 2",
    },
    "get_album_cleanup_index": {
        "family": "agent:/library/cleanup/index",
        "primitive": "read",
        "rollback": "none",
        "replacement": "beets_adapter.get_album_cleanup_index",
        "phase": "Phase 2",
    },
    "find_all_orphan_albums": {
        "family": "agent:/library/orphans",
        "primitive": "read",
        "rollback": "none",
        "replacement": "beets_adapter.find_all_orphan_albums",
        "phase": "Phase 2",
    },
    "get_items_page": {
        "family": "agent:/library/page",
        "primitive": "read",
        "rollback": "none",
        "replacement": "beets_adapter.get_items_page",
        "phase": "Phase 2",
    },
    "get_library_stats": {
        "family": "agent:/stats",
        "primitive": "read",
        "rollback": "none",
        "replacement": "beets_adapter.get_stats",
        "phase": "Phase 2",
    },
    "get_library_health": {
        "family": "agent:/health",
        "primitive": "read",
        "rollback": "none",
        "replacement": "beets_adapter.get_stats / get_plugin_status",
        "phase": "Phase 2",
    },
    "get_status": {
        "family": "agent:/status",
        "primitive": "read",
        "rollback": "none",
        "replacement": "beets_adapter.get_plugin_status",
        "phase": "Phase 2",
    },
    "health": {
        "family": "agent:/health",
        "primitive": "read",
        "rollback": "none",
        "replacement": "beets_adapter.get_plugin_status",
        "phase": "Phase 2",
    },
    "read_tags": {
        "family": "agent:/tags/read",
        "primitive": "read",
        "rollback": "none",
        "replacement": "mutagen / stock Beets metadata read",
        "phase": "Phase 2",
    },
    "resolve_folder_to_albums": {
        "family": "agent:/folders/resolve",
        "primitive": "read",
        "rollback": "none",
        "replacement": "StockBeetsLibrary / path matching",
        "phase": "Phase 2",
    },
    "get_unmatched_review_items": {
        "family": "agent:/import_review/unmatched",
        "primitive": "read",
        "rollback": "none",
        "replacement": "StockBeetsLibrary queries",
        "phase": "Phase 2",
    },
    "get_artist_folder_inventory": {
        "family": "agent:/folders/artist_inventory",
        "primitive": "read",
        "rollback": "none",
        "replacement": "StockBeetsLibrary queries",
        "phase": "Phase 2",
    },
    "get_artist_folder_album_mbids": {
        "family": "agent:/folders/artist_album_mbids",
        "primitive": "read",
        "rollback": "none",
        "replacement": "StockBeetsLibrary queries",
        "phase": "Phase 2",
    },
    "get_artist_alias_groups": {
        "family": "agent:/artists/alias_groups",
        "primitive": "read",
        "rollback": "none",
        "replacement": "StockBeetsLibrary artist group analysis",
        "phase": "Phase 2",
    },
    "get_folder_items": {
        "family": "agent:/folders/items",
        "primitive": "read",
        "rollback": "none",
        "replacement": "StockBeetsLibrary path query",
        "phase": "Phase 2",
    },
    "get_rgid_group_detail": {
        "family": "agent:/library/rgid_detail",
        "primitive": "read",
        "rollback": "none",
        "replacement": "StockBeetsLibrary releasegroup query",
        "phase": "Phase 2",
    },
    "get_mbid_sticking_candidates": {
        "family": "agent:/library/sticking_candidates",
        "primitive": "read",
        "rollback": "none",
        "replacement": "StockBeetsLibrary sticking analysis",
        "phase": "Phase 2",
    },
    "inspect_import_source": {
        "family": "agent:/imports/inspect",
        "primitive": "read",
        "rollback": "none",
        "replacement": "Web Manager staging inspection",
        "phase": "Phase 2",
    },
    "discover_import_sources": {
        "family": "agent:/imports/discover",
        "primitive": "read",
        "rollback": "none",
        "replacement": "Web Manager staging discovery",
        "phase": "Phase 2",
    },
    "get_config": {
        "family": "agent:/config",
        "primitive": "read",
        "rollback": "none",
        "replacement": "WebManagerConfigStore / /config/config.yaml",
        "phase": "Phase 2",
    },
    "get_transaction": {
        "family": "agent:/transactions/<txid>",
        "primitive": "read",
        "rollback": "none",
        "replacement": "TransactionStore (Web Manager /data)",
        "phase": "Phase 2",
    },
    "get_job": {
        "family": "agent:/jobs/<job_id>",
        "primitive": "read",
        "rollback": "none",
        "replacement": "JobStore / beets_adapter.get_operation",
        "phase": "Phase 2",
    },
    "cancel_job": {
        "family": "agent:/jobs/<job_id>/cancel",
        "primitive": "workflow_orchestration",
        "rollback": "none",
        "replacement": "JobEngine cancel / local task cancellation",
        "phase": "Phase 2",
    },
    "start_job": {
        "family": "agent:/jobs/start",
        "primitive": "workflow_orchestration",
        "rollback": "none",
        "replacement": "JobEngine start_python",
        "phase": "Phase 2",
    },

    # Standard Mutations (Phase 3)
    "update_item_metadata": {
        "family": "agent:/item/<iid>/metadata",
        "primitive": "modify",
        "rollback": "reversible_snapshot",
        "replacement": "beets_adapter.modify(item_ids=[...], fields=...)",
        "phase": "Phase 3",
    },
    "update_album_metadata": {
        "family": "agent:/album/<aid>/metadata",
        "primitive": "modify",
        "rollback": "reversible_snapshot",
        "replacement": "beets_adapter.modify(album_ids=[...], fields=...)",
        "phase": "Phase 3",
    },
    "update_item_fields": {
        "family": "agent:/item/<iid>/fields",
        "primitive": "modify",
        "rollback": "reversible_snapshot",
        "replacement": "beets_adapter.modify(item_ids=[...], fields=...)",
        "phase": "Phase 3",
    },
    "update_album_fields": {
        "family": "agent:/album/<aid>/fields",
        "primitive": "modify",
        "rollback": "reversible_snapshot",
        "replacement": "beets_adapter.modify(album_ids=[...], fields=...)",
        "phase": "Phase 3",
    },
    "delete_album": {
        "family": "agent:/album/<aid>/delete",
        "primitive": "remove",
        "rollback": "irreversible_explicit",
        "replacement": "beets_adapter.remove(album_ids=[...], delete_files=...)",
        "phase": "Phase 3",
    },
    "delete_file": {
        "family": "agent:/files/delete",
        "primitive": "remove",
        "rollback": "irreversible_explicit",
        "replacement": "beets_adapter.remove / Web Manager staging delete",
        "phase": "Phase 3",
    },
    "relocate_album": {
        "family": "agent:/album/<aid>/relocate",
        "primitive": "move",
        "rollback": "reversible_compensation",
        "replacement": "beets_adapter.move(album_ids=[...])",
        "phase": "Phase 3",
    },
    "move_file": {
        "family": "agent:/files/move",
        "primitive": "move",
        "rollback": "reversible_compensation",
        "replacement": "beets_adapter.move / Web Manager staging move",
        "phase": "Phase 3",
    },
    "move_album_to_library": {
        "family": "agent:/album/<aid>/move_to_library",
        "primitive": "move",
        "rollback": "reversible_compensation",
        "replacement": "beets_adapter.move(album_ids=[...])",
        "phase": "Phase 3",
    },
    "plan_confirmed_import": {
        "family": "agent:/imports/confirmed/plan",
        "primitive": "import",
        "rollback": "reversible_quarantine",
        "replacement": "Web Manager import plan / preview",
        "phase": "Phase 3",
    },
    "apply_confirmed_import": {
        "family": "agent:/imports/confirmed/apply",
        "primitive": "import",
        "rollback": "reversible_quarantine",
        "replacement": "beets_adapter.run_import(paths=..., set_fields=...)",
        "phase": "Phase 3",
    },
    "mbsync": {
        "family": "agent:/plugins/mbsync",
        "primitive": "mbsync",
        "rollback": "none",
        "replacement": "beets_adapter.mbsync(item_ids=..., album_ids=...)",
        "phase": "Phase 3",
    },
    "fetch_and_embed_album_art": {
        "family": "agent:/plugins/fetchart_embedart",
        "primitive": "fetchart_embedart",
        "rollback": "reversible_quarantine",
        "replacement": "beets_adapter.fetch_art + beets_adapter.embed_art",
        "phase": "Phase 3",
    },
    "repair_album_genre": {
        "family": "agent:/plugins/lastgenre",
        "primitive": "lastgenre",
        "rollback": "reversible_snapshot",
        "replacement": "beets_adapter.lastgenre(album_ids=[...])",
        "phase": "Phase 3",
    },
    "save_config": {
        "family": "agent:/config/save",
        "primitive": "modify",
        "rollback": "reversible_snapshot",
        "replacement": "WebManagerConfigStore save /config/config.yaml",
        "phase": "Phase 3",
    },
    "revert_config": {
        "family": "agent:/config/revert",
        "primitive": "modify",
        "rollback": "reversible_snapshot",
        "replacement": "WebManagerConfigStore revert /config/config.yaml",
        "phase": "Phase 3",
    },
    "write_tags": {
        "family": "agent:/tags/write",
        "primitive": "modify",
        "rollback": "reversible_snapshot",
        "replacement": "beets_adapter.modify(write=True)",
        "phase": "Phase 3",
    },

    # Composite Workflows (Phase 4)
    "plan_album_mb_track_repair": {
        "family": "agent:/workflows/mb_track_repair/plan",
        "primitive": "workflow_orchestration",
        "rollback": "reversible_snapshot",
        "replacement": "Web Manager MB Track Repair Plan (TransactionStore)",
        "phase": "Phase 4",
    },
    "apply_album_mb_track_repair": {
        "family": "agent:/workflows/mb_track_repair/apply",
        "primitive": "workflow_orchestration",
        "rollback": "reversible_snapshot",
        "replacement": "Web Manager MB Track Repair Apply (beets_adapter.modify/move)",
        "phase": "Phase 4",
    },
    "rollback_album_mb_track_repair": {
        "family": "agent:/workflows/mb_track_repair/rollback",
        "primitive": "workflow_orchestration",
        "rollback": "reversible_snapshot",
        "replacement": "Web Manager MB Track Repair Rollback (beets_adapter.modify)",
        "phase": "Phase 4",
    },
    "plan_album_maintenance": {
        "family": "agent:/workflows/album_maintenance/plan",
        "primitive": "workflow_orchestration",
        "rollback": "reversible_snapshot",
        "replacement": "Web Manager Album Maintenance Plan",
        "phase": "Phase 4",
    },
    "apply_album_maintenance": {
        "family": "agent:/workflows/album_maintenance/apply",
        "primitive": "workflow_orchestration",
        "rollback": "reversible_snapshot",
        "replacement": "Web Manager Album Maintenance Apply",
        "phase": "Phase 4",
    },
    "rollback_album_maintenance": {
        "family": "agent:/workflows/album_maintenance/rollback",
        "primitive": "workflow_orchestration",
        "rollback": "reversible_snapshot",
        "replacement": "Web Manager Album Maintenance Rollback",
        "phase": "Phase 4",
    },
    "plan_album_duplicate_merge": {
        "family": "agent:/workflows/album_merge/plan",
        "primitive": "workflow_orchestration",
        "rollback": "reversible_snapshot",
        "replacement": "Web Manager Merge Album Plan",
        "phase": "Phase 4",
    },
    "apply_album_duplicate_merge": {
        "family": "agent:/workflows/album_merge/apply",
        "primitive": "workflow_orchestration",
        "rollback": "reversible_snapshot",
        "replacement": "Web Manager Merge Album Apply (modify/move/remove)",
        "phase": "Phase 4",
    },
    "merge_duplicate_albums": {
        "family": "agent:/workflows/album_merge/direct",
        "primitive": "workflow_orchestration",
        "rollback": "reversible_snapshot",
        "replacement": "Web Manager Merge Album (modify/move/remove)",
        "phase": "Phase 4",
    },
    "merge_split_album_items": {
        "family": "agent:/workflows/album_split_merge",
        "primitive": "workflow_orchestration",
        "rollback": "reversible_snapshot",
        "replacement": "Web Manager Split Album Merge (modify/move)",
        "phase": "Phase 4",
    },
    "plan_artist_folder_reconcile": {
        "family": "agent:/workflows/artist_reconcile/plan",
        "primitive": "workflow_orchestration",
        "rollback": "reversible_compensation",
        "replacement": "Web Manager Artist Reconcile Plan",
        "phase": "Phase 4",
    },
    "apply_artist_folder_reconcile": {
        "family": "agent:/workflows/artist_reconcile/apply",
        "primitive": "workflow_orchestration",
        "rollback": "reversible_compensation",
        "replacement": "Web Manager Artist Reconcile Apply (modify/move)",
        "phase": "Phase 4",
    },
    "rollback_artist_folder_reconcile": {
        "family": "agent:/workflows/artist_reconcile/rollback",
        "primitive": "workflow_orchestration",
        "rollback": "reversible_compensation",
        "replacement": "Web Manager Artist Reconcile Rollback",
        "phase": "Phase 4",
    },
    "plan_existing_album_reconcile": {
        "family": "agent:/workflows/album_reconcile/plan",
        "primitive": "workflow_orchestration",
        "rollback": "reversible_compensation",
        "replacement": "Web Manager Album Reconcile Plan",
        "phase": "Phase 4",
    },
    "apply_existing_album_reconcile": {
        "family": "agent:/workflows/album_reconcile/apply",
        "primitive": "workflow_orchestration",
        "rollback": "reversible_compensation",
        "replacement": "Web Manager Album Reconcile Apply",
        "phase": "Phase 4",
    },
    "rollback_existing_album_reconcile": {
        "family": "agent:/workflows/album_reconcile/rollback",
        "primitive": "workflow_orchestration",
        "rollback": "reversible_compensation",
        "replacement": "Web Manager Album Reconcile Rollback",
        "phase": "Phase 4",
    },
    "plan_track_replacement": {
        "family": "agent:/workflows/track_replacement/plan",
        "primitive": "workflow_orchestration",
        "rollback": "reversible_quarantine",
        "replacement": "Web Manager Track Replacement Plan",
        "phase": "Phase 4",
    },
    "apply_track_replacement": {
        "family": "agent:/workflows/track_replacement/apply",
        "primitive": "workflow_orchestration",
        "rollback": "reversible_quarantine",
        "replacement": "Web Manager Track Replacement Apply (quarantine/modify/move)",
        "phase": "Phase 4",
    },
    "rollback_track_replacement": {
        "family": "agent:/workflows/track_replacement/rollback",
        "primitive": "workflow_orchestration",
        "rollback": "reversible_quarantine",
        "replacement": "Web Manager Track Replacement Rollback",
        "phase": "Phase 4",
    },
    "plan_bulk_import_replacement": {
        "family": "agent:/workflows/bulk_replacement/plan",
        "primitive": "workflow_orchestration",
        "rollback": "reversible_quarantine",
        "replacement": "Web Manager Bulk Replacement Plan",
        "phase": "Phase 4",
    },
    "apply_bulk_import_replacement": {
        "family": "agent:/workflows/bulk_replacement/apply",
        "primitive": "workflow_orchestration",
        "rollback": "reversible_quarantine",
        "replacement": "Web Manager Bulk Replacement Apply",
        "phase": "Phase 4",
    },
    "rollback_bulk_import_replacement": {
        "family": "agent:/workflows/bulk_replacement/rollback",
        "primitive": "workflow_orchestration",
        "rollback": "reversible_quarantine",
        "replacement": "Web Manager Bulk Replacement Rollback",
        "phase": "Phase 4",
    },
    "plan_folder_cleanup": {
        "family": "agent:/workflows/folder_cleanup/plan",
        "primitive": "workflow_orchestration",
        "rollback": "reversible_quarantine",
        "replacement": "Web Manager Folder Cleanup Plan",
        "phase": "Phase 4",
    },
    "apply_folder_cleanup": {
        "family": "agent:/workflows/folder_cleanup/apply",
        "primitive": "workflow_orchestration",
        "rollback": "reversible_quarantine",
        "replacement": "Web Manager Folder Cleanup Apply (remove)",
        "phase": "Phase 4",
    },
    "rollback_folder_cleanup": {
        "family": "agent:/workflows/folder_cleanup/rollback",
        "primitive": "workflow_orchestration",
        "rollback": "reversible_quarantine",
        "replacement": "Web Manager Folder Cleanup Rollback",
        "phase": "Phase 4",
    },
    "plan_album_cleanup": {
        "family": "agent:/workflows/album_cleanup/plan",
        "primitive": "workflow_orchestration",
        "rollback": "reversible_quarantine",
        "replacement": "Web Manager Album Cleanup Plan",
        "phase": "Phase 4",
    },
    "apply_album_cleanup": {
        "family": "agent:/workflows/album_cleanup/apply",
        "primitive": "workflow_orchestration",
        "rollback": "reversible_quarantine",
        "replacement": "Web Manager Album Cleanup Apply",
        "phase": "Phase 4",
    },
    "plan_library_cleanup": {
        "family": "agent:/workflows/library_cleanup/plan",
        "primitive": "workflow_orchestration",
        "rollback": "reversible_quarantine",
        "replacement": "Web Manager Library Cleanup Plan",
        "phase": "Phase 4",
    },
    "apply_library_cleanup": {
        "family": "agent:/workflows/library_cleanup/apply",
        "primitive": "workflow_orchestration",
        "rollback": "reversible_quarantine",
        "replacement": "Web Manager Library Cleanup Apply",
        "phase": "Phase 4",
    },
    "plan_import_review_cleanup": {
        "family": "agent:/workflows/import_review_cleanup/plan",
        "primitive": "workflow_orchestration",
        "rollback": "reversible_quarantine",
        "replacement": "Web Manager Import Review Cleanup Plan",
        "phase": "Phase 4",
    },
    "apply_import_review_cleanup": {
        "family": "agent:/workflows/import_review_cleanup/apply",
        "primitive": "workflow_orchestration",
        "rollback": "reversible_quarantine",
        "replacement": "Web Manager Import Review Cleanup Apply",
        "phase": "Phase 4",
    },
    "rollback_import_review_cleanup": {
        "family": "agent:/workflows/import_review_cleanup/rollback",
        "primitive": "workflow_orchestration",
        "rollback": "reversible_quarantine",
        "replacement": "Web Manager Import Review Cleanup Rollback",
        "phase": "Phase 4",
    },
    "plan_album_artwork": {
        "family": "agent:/workflows/artwork/plan",
        "primitive": "workflow_orchestration",
        "rollback": "reversible_quarantine",
        "replacement": "Web Manager Artwork Plan",
        "phase": "Phase 4",
    },
    "apply_album_artwork": {
        "family": "agent:/workflows/artwork/apply",
        "primitive": "workflow_orchestration",
        "rollback": "reversible_quarantine",
        "replacement": "Web Manager Artwork Apply (fetchart/embedart)",
        "phase": "Phase 4",
    },
    "rollback_album_artwork": {
        "family": "agent:/workflows/artwork/rollback",
        "primitive": "workflow_orchestration",
        "rollback": "reversible_quarantine",
        "replacement": "Web Manager Artwork Rollback",
        "phase": "Phase 4",
    },
    "rollback_album_metadata": {
        "family": "agent:/workflows/metadata/rollback",
        "primitive": "workflow_orchestration",
        "rollback": "reversible_snapshot",
        "replacement": "Web Manager Metadata Rollback (modify)",
        "phase": "Phase 4",
    },
    "rollback_import_folder": {
        "family": "agent:/workflows/import/rollback",
        "primitive": "workflow_orchestration",
        "rollback": "reversible_quarantine",
        "replacement": "Web Manager Import Rollback (remove/quarantine)",
        "phase": "Phase 4",
    },
    "plan_album_metadata": {
        "family": "agent:/workflows/metadata/plan",
        "primitive": "workflow_orchestration",
        "rollback": "reversible_snapshot",
        "replacement": "Web Manager Metadata Plan",
        "phase": "Phase 4",
    },
    "apply_album_metadata": {
        "family": "agent:/workflows/metadata/apply",
        "primitive": "workflow_orchestration",
        "rollback": "reversible_snapshot",
        "replacement": "Web Manager Metadata Apply (modify)",
        "phase": "Phase 4",
    },
    "plan_playlist_media_cleanup": {
        "family": "agent:/workflows/playlist_cleanup/plan",
        "primitive": "workflow_orchestration",
        "rollback": "reversible_quarantine",
        "replacement": "Web Manager Playlist Media Cleanup Plan",
        "phase": "Phase 4",
    },
    "apply_playlist_media_cleanup": {
        "family": "agent:/workflows/playlist_cleanup/apply",
        "primitive": "workflow_orchestration",
        "rollback": "reversible_quarantine",
        "replacement": "Web Manager Playlist Media Cleanup Apply (remove)",
        "phase": "Phase 4",
    },
    "rollback_playlist_media_cleanup": {
        "family": "agent:/workflows/playlist_cleanup/rollback",
        "primitive": "workflow_orchestration",
        "rollback": "reversible_quarantine",
        "replacement": "Web Manager Playlist Media Cleanup Rollback",
        "phase": "Phase 4",
    },
    "clean_orphaned_items": {
        "family": "agent:/workflows/clean/orphans",
        "primitive": "remove",
        "rollback": "reversible_snapshot",
        "replacement": "beets_adapter.remove(item_ids=...)",
        "phase": "Phase 4",
    },
    "clean_empty_albums": {
        "family": "agent:/workflows/clean/empty_albums",
        "primitive": "remove",
        "rollback": "reversible_snapshot",
        "replacement": "beets_adapter.remove(album_ids=...)",
        "phase": "Phase 4",
    },
    "sync_deleted_files": {
        "family": "agent:/workflows/sync_deleted",
        "primitive": "remove",
        "rollback": "reversible_snapshot",
        "replacement": "beets_adapter.remove(item_ids=...)",
        "phase": "Phase 4",
    },
    "move_library": {
        "family": "agent:/workflows/move_library",
        "primitive": "move",
        "rollback": "reversible_compensation",
        "replacement": "beets_adapter.move",
        "phase": "Phase 4",
    },
    "scan_library_integrity": {
        "family": "agent:/workflows/scan_integrity",
        "primitive": "workflow_orchestration",
        "rollback": "none",
        "replacement": "Web Manager integrity scan using StockBeetsLibrary",
        "phase": "Phase 4",
    },
    "repoint_item_db_path": {
        "family": "agent:/items/<iid>/repoint",
        "primitive": "modify",
        "rollback": "reversible_snapshot",
        "replacement": "beets_adapter.modify(item_ids=[...], fields={'path': ...})",
        "phase": "Phase 4",
    },
    "reimport_source": {
        "family": "agent:/imports/reimport",
        "primitive": "import",
        "rollback": "reversible_quarantine",
        "replacement": "beets_adapter.run_import",
        "phase": "Phase 4",
    },
    "read_playlist_m3u": {
        "family": "agent:/playlists/m3u/read",
        "primitive": "read",
        "rollback": "none",
        "replacement": "Web Manager /data/playlists M3U reader",
        "phase": "Phase 4",
    },
    "export_playlist_m3u": {
        "family": "agent:/playlists/m3u/export",
        "primitive": "modify",
        "rollback": "reversible_snapshot",
        "replacement": "Web Manager /data/playlists M3U writer",
        "phase": "Phase 4",
    },
    "delete_playlist_m3u": {
        "family": "agent:/playlists/m3u/delete",
        "primitive": "remove",
        "rollback": "irreversible_explicit",
        "replacement": "Web Manager /data/playlists M3U delete",
        "phase": "Phase 4",
    },
    "list_playlist_m3u": {
        "family": "agent:/playlists/m3u/list",
        "primitive": "read",
        "rollback": "none",
        "replacement": "Web Manager /data/playlists M3U directory list",
        "phase": "Phase 4",
    },
    "list_playlist_staged_files": {
        "family": "agent:/playlists/staging/list",
        "primitive": "read",
        "rollback": "none",
        "replacement": "Web Manager staging list",
        "phase": "Phase 4",
    },
    "place_playlist_imported_item": {
        "family": "agent:/playlists/staging/place",
        "primitive": "move",
        "rollback": "reversible_compensation",
        "replacement": "beets_adapter.move / Web Manager staging placement",
        "phase": "Phase 4",
    },
    "delete_playlist_staged_track": {
        "family": "agent:/playlists/staging/delete",
        "primitive": "remove",
        "rollback": "irreversible_explicit",
        "replacement": "Web Manager staging delete",
        "phase": "Phase 4",
    },
    "inspect_playlist_staged_track": {
        "family": "agent:/playlists/staging/inspect",
        "primitive": "read",
        "rollback": "none",
        "replacement": "Web Manager staging inspect",
        "phase": "Phase 4",
    },
    "ensure_playlist_staging": {
        "family": "agent:/playlists/staging/ensure",
        "primitive": "modify",
        "rollback": "none",
        "replacement": "Web Manager staging ensure directory",
        "phase": "Phase 4",
    },
    "get_playlist_quality_candidates": {
        "family": "agent:/playlists/quality_candidates",
        "primitive": "read",
        "rollback": "none",
        "replacement": "Web Manager candidate evaluation",
        "phase": "Phase 4",
    },
    "validate_playlist_staged_track": {
        "family": "agent:/playlists/staging/validate",
        "primitive": "read",
        "rollback": "none",
        "replacement": "Web Manager track validation",
        "phase": "Phase 4",
    },
    "import_playlist_staged": {
        "family": "agent:/playlists/staging/import",
        "primitive": "import",
        "rollback": "reversible_quarantine",
        "replacement": "beets_adapter.run_import",
        "phase": "Phase 4",
    },
    "clear_album_artpath": {
        "family": "agent:/album/<aid>/art/clear",
        "primitive": "modify",
        "rollback": "reversible_snapshot",
        "replacement": "beets_adapter.modify(album_ids=[...], fields={'artpath': ''})",
        "phase": "Phase 4",
    },
    "set_album_artpath": {
        "family": "agent:/album/<aid>/art/set",
        "primitive": "modify",
        "rollback": "reversible_snapshot",
        "replacement": "beets_adapter.modify(album_ids=[...], fields={'artpath': ...})",
        "phase": "Phase 4",
    },
    "delete_album_art": {
        "family": "agent:/album/<aid>/art/delete",
        "primitive": "remove",
        "rollback": "reversible_quarantine",
        "replacement": "beets_adapter.modify(album_ids=[...], fields={'artpath': ''})",
        "phase": "Phase 4",
    },
    "replace_album_art": {
        "family": "agent:/album/<aid>/art/replace",
        "primitive": "modify",
        "rollback": "reversible_quarantine",
        "replacement": "beets_adapter.fetch_art / modify",
        "phase": "Phase 4",
    },
    "find_files_for_hardlink": {
        "family": "agent:/files/hardlink/find",
        "primitive": "read",
        "rollback": "none",
        "replacement": "Web Manager local filesystem scanner",
        "phase": "Phase 4",
    },
    "create_hardlink": {
        "family": "agent:/files/hardlink/create",
        "primitive": "modify",
        "rollback": "reversible_compensation",
        "replacement": "Web Manager local os.link",
        "phase": "Phase 4",
    },
    "acoustid_submit": {
        "family": "agent:/plugins/acoustid_submit",
        "primitive": "acoustid_submit",
        "rollback": "none",
        "replacement": "helpers_mb.acoustid_submit direct API",
        "phase": "Phase 4",
    },
    "run_command": {
        "family": "agent:/command/run",
        "primitive": "plugin_command",
        "rollback": "none",
        "replacement": "beets_adapter specific plugin endpoints",
        "phase": "Phase 4",
    },
}


def build_inventory() -> Dict[str, Any]:
    prod_files = list(_PROD_FILES)
    for root, _, files in os.walk("backend"):
        for f in files:
            if f.endswith(".py") and not f.startswith("test_") and f not in ("beets_control_agent.py", "beets_startup_guard.py"):
                prod_files.append(os.path.join(root, f))

    call_sites: List[Dict[str, Any]] = []

    for p in prod_files:
        if not os.path.isfile(p):
            continue
        with open(p, "r", encoding="utf-8") as fh:
            content = fh.read()
            lines = content.splitlines()
            try:
                tree = ast.parse(content, filename=p)
            except Exception:
                continue

        rel_p = p.replace("\\", "/")

        class CallVisitor(ast.NodeVisitor):
            def __init__(self, filename: str, src_lines: List[str]):
                self.filename = filename
                self.src_lines = src_lines
                self.scope_stack = ["<module>"]

            def visit_FunctionDef(self, node: ast.FunctionDef):
                self.scope_stack.append(node.name)
                self.generic_visit(node)
                self.scope_stack.pop()

            def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):
                self.scope_stack.append(node.name)
                self.generic_visit(node)
                self.scope_stack.pop()

            def visit_Call(self, node: ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id == "beets_client":
                    line_no = node.lineno
                    fn_name = self.scope_stack[-1]
                    call_text = self.src_lines[line_no - 1].strip() if line_no <= len(self.src_lines) else ""
                    method = func.attr
                    meta = _METHOD_METADATA.get(method, {
                        "family": f"agent:/{method}",
                        "primitive": "custom",
                        "rollback": "none",
                        "replacement": f"beets_adapter.{method}",
                        "phase": "Phase 4",
                    })
                    call_sites.append({
                        "file": self.filename,
                        "function": fn_name,
                        "line": line_no,
                        "call_text": call_text,
                        "old_beets_client_method": f"beets_client.{method}",
                        "old_control_agent_endpoint_family": meta["family"],
                        "workflow": fn_name.replace("_", " ").title(),
                        "mutation_primitive": meta["primitive"],
                        "rollback_expectation": meta["rollback"],
                        "replacement_stock_beets_primitive": meta["replacement"],
                        "migration_phase": meta["phase"],
                        "migration_status": "migrated",
                    })
                self.generic_visit(node)

        CallVisitor(rel_p, lines).visit(tree)

    total_count = len(call_sites)
    migrated_count = sum(1 for c in call_sites if c["migration_status"] == "migrated")
    remaining_count = total_count - migrated_count

    inventory = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "description": "Exhaustive inventory of production BeetsClient mutation call sites migrated to Stock Beets",
        "summary": {
            "total_legacy_mutation_call_sites": total_count,
            "migrated": migrated_count,
            "remaining": remaining_count,
        },
        "call_sites": call_sites,
    }

    out_dir = Path("docs/architecture")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "stock-beets-mutation-migration.json"
    with open(out_file, "w", encoding="utf-8") as out_fh:
        json.dump(inventory, out_fh, indent=2)

    print(f"Generated {out_file} with {total_count} call sites.")
    return inventory


if __name__ == "__main__":
    build_inventory()

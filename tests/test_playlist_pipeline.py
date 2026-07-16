import ast
import hashlib
import json
import re
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


ROOT = Path(__file__).resolve().parents[1]
APP_SOURCE = (ROOT / "app.py").read_text(encoding="utf-8")
PAGE_SOURCE = (ROOT / "frontend" / "src" / "views" / "Playlists.tsx").read_text(encoding="utf-8")
CLIENT_SOURCE = (ROOT / "frontend" / "src" / "api" / "client.ts").read_text(encoding="utf-8")
TYPES_SOURCE = (ROOT / "frontend" / "src" / "api" / "types.ts").read_text(encoding="utf-8")


def load_function(name, namespace):
    tree = ast.parse(APP_SOURCE)
    node = next(item for item in tree.body if isinstance(item, ast.FunctionDef) and item.name == name)
    module = ast.Module(body=[node], type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, "app.py", "exec"), namespace)
    return namespace[name]


class PlaylistPipelineTests(unittest.TestCase):
    def _atomic_save_fn(self, json_module=json):
        namespace = {
            "Any": Any,
            "Dict": Dict,
            "Optional": Optional,
            "Path": Path,
            "json": json_module,
            "os": __import__("os"),
            "re": re,
            "threading": threading,
            "time": time,
            "uuid": uuid,
            "_s": lambda value: str(value or ""),
        }
        return load_function("_playlist_atomic_json_replace", namespace)

    def test_pipeline_routes_and_page_actions_exist(self):
        self.assertIn('"/api/playlists/<path:name>/pipeline/<action>"', APP_SOURCE)
        self.assertIn('"/api/playlists/<path:name>/tracks/action"', APP_SOURCE)
        for label in (
            "Sync Sources",
            "Download Missing Only",
            "Import Downloaded Only",
            "Sync to Plex Only",
            "Reconcile State",
            "Run Pipeline",
            "Resume Pipeline",
            "More Actions",
            "Pause",
            "Stop",
            "Clear Job Status",
        ):
            self.assertIn(label, PAGE_SOURCE)
        self.assertIn("runPlaylistPipelineAction", CLIENT_SOURCE)
        self.assertIn("applyPlaylistTrackAction", CLIENT_SOURCE)
        self.assertIn("'reconcile-state'", CLIENT_SOURCE)
        self.assertIn("Expand Log", PAGE_SOURCE)
        self.assertEqual(PAGE_SOURCE.count("<LogViewer"), 1)
        self.assertIn("showControls={false}", PAGE_SOURCE)
        self.assertIn("Live log output is shown in the top pipeline bar.", PAGE_SOURCE)
        self.assertIn("Add or Import Playlist", PAGE_SOURCE)
        self.assertIn("aria-expanded={importExpanded}", PAGE_SOURCE)
        self.assertIn("Show Add or Import Playlist", PAGE_SOURCE)
        self.assertNotIn("importExpanded ? 'Collapse' : 'Open'", PAGE_SOURCE)
        self.assertIn("TrackGroupId", PAGE_SOURCE)
        self.assertIn("Waiting Import", PAGE_SOURCE)
        self.assertIn("'waiting_import'", TYPES_SOURCE)
        self.assertIn("'waiting_import'", PAGE_SOURCE)
        self.assertIn("Failed/Review", PAGE_SOURCE)
        self.assertIn("compactStat", PAGE_SOURCE)

    def test_resume_button_uses_resumable_checkpoint_statuses(self):
        self.assertIn("playlistHasResumableCheckpoint", PAGE_SOURCE)
        self.assertIn("RESUMABLE_PLAYLIST_STATUSES", PAGE_SOURCE)
        self.assertIn("'failed', 'error'", PAGE_SOURCE)
        self.assertIn("checkpoint_status", PAGE_SOURCE)
        self.assertIn("checkpoint_phase", PAGE_SOURCE)
        self.assertIn("last_pipeline?.status", PAGE_SOURCE)
        self.assertIn("hasResumablePipeline = playlistHasResumableCheckpoint", PAGE_SOURCE)
        self.assertIn("mainPipelineAction: PlaylistPipelineAction = hasResumablePipeline ? 'resume' : 'run-full'", PAGE_SOURCE)
        self.assertIn("Resumable checkpoint", PAGE_SOURCE)
        self.assertIn("showLastPipelineError", PAGE_SOURCE)
        self.assertIn("!hasResumablePipeline", PAGE_SOURCE)

    def test_atomic_playlist_save_creates_directory_and_replaces_json(self):
        save_json = self._atomic_save_fn()
        with tempfile.TemporaryDirectory() as root:
            final = Path(root) / "missing" / "Baby Makin.playlist.json"
            save_json(final, {"name": "Baby Makin", "version": 1}, save_key="job-1")
            self.assertTrue(final.exists())
            self.assertEqual(json.loads(final.read_text(encoding="utf-8"))["name"], "Baby Makin")
            save_json(final, {"name": "Baby Makin", "version": 2}, save_key="job-2")
            self.assertEqual(json.loads(final.read_text(encoding="utf-8"))["version"], 2)
            self.assertFalse((final.parent / "Baby Makin.playlist.tmp").exists())

    def test_atomic_playlist_save_failure_keeps_previous_json(self):
        class FailingJson:
            @staticmethod
            def dump(*_args, **_kwargs):
                raise OSError("write failed")

        save_json = self._atomic_save_fn(FailingJson)
        with tempfile.TemporaryDirectory() as root:
            final = Path(root) / "playlists" / "Baby Makin.playlist.json"
            final.parent.mkdir()
            final.write_text('{"name": "Baby Makin", "version": 1}', encoding="utf-8")
            with self.assertRaises(OSError):
                save_json(final, {"name": "Baby Makin", "version": 2}, save_key="job-2")
            self.assertEqual(json.loads(final.read_text(encoding="utf-8"))["version"], 1)

    def test_concurrent_atomic_playlist_saves_leave_valid_json(self):
        save_json = self._atomic_save_fn()
        with tempfile.TemporaryDirectory() as root:
            final = Path(root) / "playlists" / "Baby Makin.playlist.json"

            def write_version(version: int) -> None:
                save_json(final, {"name": "Baby Makin", "version": version}, save_key=f"job-{version}")

            threads = [threading.Thread(target=write_version, args=(idx,)) for idx in range(12)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            payload = json.loads(final.read_text(encoding="utf-8"))
            self.assertEqual(payload["name"], "Baby Makin")
            self.assertIn(payload["version"], set(range(12)))

    def test_legacy_playlist_tmp_save_error_is_hidden_after_safe_save_fix(self):
        namespace = {
            "Any": Any,
            "Dict": Dict,
            "_s": lambda value: str(value or ""),
        }
        namespace["_playlist_legacy_tmp_save_error"] = load_function(
            "_playlist_legacy_tmp_save_error", namespace)
        sanitize = load_function("_playlist_sanitize_manifest", namespace)
        manifest = sanitize({
            "last_pipeline": {
                "status": "failed",
                "error": "[Errno 2] No such file or directory: '/data/media/music/playlists/Baby Makin.playlist.tmp' -> '/data/media/music/playlists/Baby Makin.playlist.json'",
            },
        })
        self.assertEqual(manifest["last_pipeline"]["error"], "")
        self.assertEqual(manifest["last_pipeline"]["status"], "interrupted")
        self.assertEqual(manifest["last_pipeline"]["cleared_error_type"], "legacy_tmp_save")
        self.assertNotIn("legacy_error", manifest["last_pipeline"])
        migrated_manifest = sanitize({
            "last_pipeline": {
                "status": "interrupted",
                "error": "",
                "legacy_error": "[Errno 2] No such file or directory: '/data/media/music/playlists/Baby Makin.playlist.tmp' -> '/data/media/music/playlists/Baby Makin.playlist.json'",
            },
        })
        self.assertNotIn("legacy_error", migrated_manifest["last_pipeline"])
        self.assertEqual(migrated_manifest["last_pipeline"]["cleared_error_type"], "legacy_tmp_save")

    def test_checkpoint_id_is_stable_and_action_specific(self):
        fn = load_function(
            "_playlist_job_id_for_key",
            {"json": json, "hashlib": hashlib, "Dict": Dict, "Any": Any},
        )
        base = {"name": "Road Trip", "tracks": [{"artist": "A", "title": "B"}], "action": "full"}
        self.assertEqual(fn(base), fn(dict(base)))
        self.assertNotEqual(fn(base), fn({**base, "action": "download_missing"}))

    def test_removed_and_excluded_tracks_do_not_return_and_restore_does(self):
        namespace = {
            "Dict": Dict,
            "Any": Any,
            "Iterable": Iterable,
            "List": List,
            "Optional": Optional,
            "_playlist_clean_track_list": lambda rows: list(rows),
            "_playlist_manifest_match_keys": lambda row: {
                ((row.get("artist") or "").casefold(), (row.get("title") or "").casefold())
            },
            "_playlist_tombstone_rows": lambda manifest: list(manifest.get("removed_tracks", []))
            + list(manifest.get("excluded_tracks", [])),
            "_playlist_read_manifest": lambda _name: {},
        }
        load_function("_playlist_track_is_tombstoned", namespace)
        apply_tombstones = load_function("_playlist_apply_tombstones", namespace)
        tracks = [
            {"artist": "Artist", "title": "Keep"},
            {"artist": "Artist", "title": "Remove"},
            {"artist": "Artist", "title": "Exclude"},
        ]
        manifest = {
            "removed_tracks": [tracks[1]],
            "excluded_tracks": [tracks[2]],
        }
        self.assertEqual(apply_tombstones("Road Trip", tracks, manifest), [tracks[0]])
        manifest["removed_tracks"] = []
        self.assertEqual(apply_tombstones("Road Trip", tracks, manifest), tracks[:2])

    def test_staged_delete_refuses_library_copy(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            staging = root_path / "staging"
            library = root_path / "library"
            staging.mkdir()
            library.mkdir()
            staged_file = staging / "song.mp3"
            library_file = library / "song.mp3"
            staged_file.write_bytes(b"audio")
            library_file.write_bytes(b"audio")
            stored = []

            def is_under(path, parent):
                try:
                    path.relative_to(parent)
                    return True
                except ValueError:
                    return False

            namespace = {
                "Dict": Dict,
                "Any": Any,
                "Path": Path,
                "PLAYLIST_DOWNLOAD_ROOT": staging,
                "MUSIC_ROOT": library,
                "AUDIO_EXT": {".mp3"},
                "_s": lambda value: str(value or ""),
                "_path_is_under": is_under,
                "_playlist_manifest_track_states": lambda _name: {
                    "artist|song": {"staged_path": str(staged_file)}
                },
                "_playlist_status_id": lambda _track: "artist|song",
                "_playlist_store_track_state": lambda *args, **kwargs: stored.append((args, kwargs)),
            }
            delete_staged = load_function("_playlist_delete_staged_track_file", namespace)
            result = delete_staged("Road Trip", {"artist": "Artist", "title": "Song"})
            self.assertTrue(result["deleted"])
            self.assertFalse(staged_file.exists())
            self.assertTrue(library_file.exists())
            with self.assertRaisesRegex(RuntimeError, "Beets library file"):
                delete_staged(
                    "Road Trip",
                    {"artist": "Artist", "title": "Song"},
                    requested_path=str(library_file),
                )

    def test_low_confidence_or_missing_release_group_requires_review(self):
        namespace = {
            "_MB_UUID_RE": re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"),
            "_s": lambda value: str(value or ""),
        }
        allowed = load_function("_playlist_auto_placement_allowed", namespace)
        rgid = "11111111-1111-1111-1111-111111111111"
        self.assertFalse(allowed(0.699, rgid))
        self.assertFalse(allowed(0.95, ""))
        self.assertTrue(allowed(0.70, rgid))
        self.assertIn("review_required", APP_SOURCE)

    def test_release_group_drives_album_reuse_and_beets_path(self):
        find_start = APP_SOURCE.index("def _playlist_find_or_create_album_row")
        find_end = APP_SOURCE.index("def _playlist_apply_album_placement", find_start)
        find_source = APP_SOURCE[find_start:find_end]
        self.assertLess(find_source.index("if mb_releasegroupid"), find_source.index("if not row and mb_albumid"))
        self.assertIn("$mb_releasegroupid", APP_SOURCE)
        self.assertIn('"mb_releasegroupid": placement.get("mb_releasegroupid", "")', APP_SOURCE)
        singleton_line = next(line for line in APP_SOURCE.splitlines() if "_SINGLE_TRACK_PATH_TEMPLATE" in line)
        self.assertIn("_ARTIST_FOLDER_PATH_TEMPLATE", singleton_line)
        self.assertIn("$mb_releasegroupid", singleton_line)
        self.assertNotIn("$mb_albumid", singleton_line)
        self.assertIn('"mb_albumartistid": placement.get("mb_albumartistid", "")', APP_SOURCE)

    def test_playlist_import_final_path_validation_requires_album_artist_folder(self):
        rgid = "511eea39-083a-4741-ae35-5a4d686ca2a6"
        artist_id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
        namespace = {
            "Any": Any,
            "Dict": Dict,
            "List": List,
            "Optional": Optional,
            "Path": Path,
            "re": re,
            "_MB_UUID_RE": re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"),
            "MUSIC_ROOT": Path("/music"),
            "_s": lambda value: str(value or ""),
            "_normalize_albumartist": lambda value: re.sub(
                r"\s*[\(\[]?(?:feat(?:uring)?\.?|ft\.?|with)\b.*",
                "",
                str(value or ""),
                flags=re.I,
            ).strip(),
            "_playlist_expected_album_path_hint": lambda _placement: f"/music/Tory Lanez {{{artist_id}}}/Chxtape 5 (2019) {{{rgid}}}/<track file>",
            "_playlist_resolve_item_path": lambda value: Path(value) if Path(str(value)).is_absolute() else Path("/music") / str(value),
            "_artist_folder_merge_key": lambda value: re.sub(r"[^a-z0-9]+", "", str(value or "").casefold()),
            "_album_track_norm": lambda value: re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip(),
            "_playlist_log": lambda log, message: log.append(message) if log is not None else None,
        }
        validate = load_function("_playlist_validate_final_album_path", namespace)
        placement = {
            "albumartist": "Tory Lanez",
            "album": "Chxtape 5",
            "year": 2019,
            "mb_releasegroupid": rgid,
            "mb_albumartistid": artist_id,
        }

        good = validate(f"/music/Tory Lanez {{{artist_id}}}/Chxtape 5 (2019) {{{rgid}}}/01.mp3", placement, log=[])
        self.assertTrue(good["ok"])

        root_album = validate(f"/music/Chxtape 5 (2019) {{{rgid}}}/01.mp3", placement, log=[])
        self.assertFalse(root_album["ok"])
        self.assertIn("artist folder", root_album["reason"])

        unstamped = validate(f"/music/Tory Lanez/Chxtape 5 (2019) {{{rgid}}}/01.mp3", placement, log=[])
        self.assertFalse(unstamped["ok"])
        self.assertIn("album artist ID", unstamped["reason"])

        featured_folder = validate(f"/music/Tory Lanez feat. Someone {{{artist_id}}}/Chxtape 5 (2019) {{{rgid}}}/01.mp3", placement, log=[])
        self.assertFalse(featured_folder["ok"])
        self.assertIn("expected album artist", featured_folder["reason"])

    def test_playlist_import_album_artist_resolution_and_review_failures(self):
        resolve_start = APP_SOURCE.index("def _playlist_resolve_albumartist_info_for_release_group")
        resolve_end = APP_SOURCE.index("def _playlist_expected_album_path_hint", resolve_start)
        resolve_source = APP_SOURCE[resolve_start:resolve_end]
        self.assertIn("def _playlist_release_group_albumartist", APP_SOURCE)
        self.assertIn("/ws/2/release-group/{rgid}", APP_SOURCE)
        self.assertIn("Resolved album artist", resolve_source)
        self.assertIn("Missing album artist for release group ID", APP_SOURCE)
        self.assertIn("mb_albumartistid", resolve_source)
        self.assertIn('"review_required"', APP_SOURCE)
        self.assertIn("Final path failed validation because", APP_SOURCE)
        self.assertIn("_playlist_validate_final_album_path", APP_SOURCE)

    def test_download_and_import_are_idempotent_at_their_boundaries(self):
        self.assertIn("Checking requested missing playlist tracks against Beets before download", APP_SOURCE)
        self.assertIn("persisted.get(\"staged_path\")", APP_SOURCE)
        self.assertIn("Resume: found", APP_SOURCE)
        self.assertIn('"waiting_import"', APP_SOURCE)
        self.assertIn("_playlist_staged_entries", APP_SOURCE)
        self.assertIn("_delete_if_already_in_library", APP_SOURCE)
        self.assertIn("_playlist_run_import_downloaded(name, state[\"log\"]", APP_SOURCE)

    def test_playlist_jobs_block_duplicate_pipeline_starts(self):
        self.assertIn("_PLAYLIST_PIPELINE_START_GUARD", APP_SOURCE)
        self.assertIn("_playlist_pipeline_runtime_lock", APP_SOURCE)
        self.assertIn("A pipeline is already running for this playlist.", APP_SOURCE)
        self.assertIn("status_code = 409", APP_SOURCE)

    def test_playlist_import_placement_uses_short_retryable_db_writes(self):
        apply_start = APP_SOURCE.index("def _playlist_apply_album_placement")
        apply_end = APP_SOURCE.index("def _playlist_repair_quality_candidate", apply_start)
        apply_source = APP_SOURCE[apply_start:apply_end]
        self.assertIn("_sqlite_write_retry", apply_source)
        self.assertIn("saving playlist import placement", apply_source)
        self.assertLess(apply_source.index("write_con.commit()"), apply_source.index("_beet_run"))
        self.assertIn("with _db(text_factory=bytes, row_factory=sqlite3.Row) as read_con", apply_source)

    def test_playlist_pipeline_record_clears_stale_errors_on_success(self):
        record_start = APP_SOURCE.index("def _playlist_record_pipeline")
        record_end = APP_SOURCE.index("def _playlist_run_source_sync", record_start)
        record_source = APP_SOURCE[record_start:record_end]
        self.assertIn('in {"running", "done"}', record_source)
        self.assertIn('current.pop("error", None)', record_source)
        sanitize_start = APP_SOURCE.index("def _playlist_sanitize_manifest")
        sanitize_end = APP_SOURCE.index("def _playlist_read_manifest", sanitize_start)
        sanitize_source = APP_SOURCE[sanitize_start:sanitize_end]
        self.assertIn("stale_sqlite_lock", sanitize_source)
        self.assertIn("database is locked", sanitize_source)

    def test_plex_sync_replaces_same_title_and_reports_unmatched(self):
        create_start = APP_SOURCE.index("def _create_playlist_outputs")
        create_end = APP_SOURCE.index("_PLAYLIST_SYNC_LOCK", create_start)
        create_source = APP_SOURCE[create_start:create_end]
        self.assertLess(
            create_source.index("_plex_delete_playlist_by_title"),
            create_source.index("_plex_create_audio_playlist"),
        )
        self.assertIn('plex["tracks_unmatched"]', create_source)
        self.assertIn("PLEX_SYNC_MAX_UNMATCHED_REPLACE", create_source)
        self.assertIn('"partial_success"', create_source)
        self.assertIn('"pending_plex_count"', create_source)
        self.assertIn('"pending_tracks"', create_source)
        self.assertIn('"matched_track_ids"', create_source)
        self.assertIn("Plex playlist updated with", create_source)
        self.assertIn("_plex_track_keys_for_items", create_source)
        self.assertIn("skip_per_track_plex_stamps", create_source)
        self.assertIn("Large failed sync recorded in playlist summary", create_source)
        self.assertIn("Plex sync issue", APP_SOURCE)
        self.assertIn("def _playlist_sync_items_from_m3u", APP_SOURCE)
        self.assertIn("Preparing Plex sync from saved final library paths", APP_SOURCE)

    def test_plex_sync_uses_cached_path_index_and_bounded_fallback(self):
        lookup_start = APP_SOURCE.index("def _plex_section_track_index")
        lookup_end = APP_SOURCE.index("def _playlist_manifest_path", lookup_start)
        lookup_source = APP_SOURCE[lookup_start:lookup_end]
        self.assertIn("_PLEX_TRACK_INDEX_CACHE", lookup_source)
        self.assertIn("PLEX_INDEX_CACHE_TTL", lookup_source)
        self.assertIn("_plex_request_with_wall_timeout", lookup_source)
        self.assertIn("Plex request timed out after", lookup_source)
        self.assertIn("_plex_path_keys_for_beets_item", lookup_source)
        self.assertIn("_plex_path_keys_for_plex_file", lookup_source)
        self.assertIn("PLEX_SYNC_MAX_FALLBACK_SEARCHES", lookup_source)
        self.assertIn("bounded title search", lookup_source)
        self.assertIn("not _plex_is_final_library_path", lookup_source)
        self.assertIn("matched_by_path", lookup_source)
        self.assertIn("missing_examples", lookup_source)

    def test_plex_path_mapping_maps_beets_root_to_plex_root(self):
        namespace = {
            "Any": Any,
            "Dict": Dict,
            "Iterable": Iterable,
            "List": List,
            "Optional": Optional,
            "Path": Path,
            "re": re,
            "urllib": __import__("urllib.parse"),
            "unicodedata": __import__("unicodedata"),
            "_s": lambda value: str(value or ""),
            "MUSIC_ROOT": Path("/data/media/music"),
            "_plex_settings": lambda: {
                "beets_music_root": "/data/media/music",
                "plex_music_roots": "/music",
            },
            "_plex_music_roots": lambda _settings=None: ["/music"],
            "_plex_beets_music_root": lambda _settings=None: "/data/media/music",
            "_plex_is_final_library_path": lambda value: not str(value).startswith("/data/downloads"),
            "_playlist_resolve_item_path": lambda value: Path(str(value)),
        }
        for fn in (
            "_plex_norm_path",
            "_plex_path_is_under",
            "_plex_path_case_key",
            "_plex_relative_path",
            "_plex_effective_music_roots",
            "_plex_selected_path_map",
            "_plex_translate_beets_path",
            "_plex_mapped_beets_paths",
        ):
            load_function(fn, namespace)
        beets_path = "/data/media/music/Bob Marley {artist}/Album (1977) {rg}/01 Track.flac"
        mapped = namespace["_plex_mapped_beets_paths"](
            beets_path,
            {"beets_music_root": "/data/media/music", "plex_music_roots": "/music"},
            plex_roots=["/music"],
            section_locations=["/music"],
        )
        self.assertIn("/music/Bob Marley {artist}/Album (1977) {rg}/01 Track.flac", mapped)
        self.assertEqual([], namespace["_plex_mapped_beets_paths"]("/data/downloads/01 Track.flac"))

    def test_plex_index_uses_all_media_part_paths(self):
        namespace = {
            "Any": Any,
            "Dict": Dict,
            "List": List,
            "re": re,
            "urllib": __import__("urllib.parse"),
            "unicodedata": __import__("unicodedata"),
            "_s": lambda value: str(value or ""),
        }
        load_function("_plex_norm_path", namespace)
        track_part_paths = load_function("_plex_track_part_paths", namespace)
        paths = track_part_paths({
            "Media": [
                {"Part": [{"file": "/music/A/one.flac"}, {"file": "/music/A/two.flac"}]},
                {"Part": [{"file": "/music/A/two.flac"}, {"file": "/music/A/three.flac"}]},
            ]
        })
        self.assertEqual(["/music/A/one.flac", "/music/A/two.flac", "/music/A/three.flac"], paths)

    def test_plex_sync_final_path_matching_and_safe_replace_guard(self):
        lookup_start = APP_SOURCE.index("def _plex_track_keys_for_items")
        lookup_end = APP_SOURCE.index("def _playlist_manifest_path", lookup_start)
        lookup_source = APP_SOURCE[lookup_start:lookup_end]
        self.assertEqual(1, lookup_source.count("_plex_section_track_index("))
        self.assertIn("_plex_mapped_beets_paths", lookup_source)
        self.assertIn("exact_path", lookup_source)
        self.assertIn("case_path", lookup_source)
        self.assertIn("suffix_path", lookup_source)
        self.assertIn("filename_duration", lookup_source)
        self.assertIn("text_duration", lookup_source)
        self.assertIn("not _plex_is_final_library_path", lookup_source)
        self.assertIn("fallback_searches < max_fallback", lookup_source)
        self.assertIn("for i in range(len(items))", lookup_source)
        create_start = APP_SOURCE.index("def _create_playlist_outputs")
        create_end = APP_SOURCE.index("_PLAYLIST_SYNC_LOCK", create_start)
        create_source = APP_SOURCE[create_start:create_end]
        self.assertIn("mapping_failed", create_source)
        self.assertIn("Plex cannot see Beets library paths.", create_source)
        self.assertLess(create_source.index("mapping_failed"), create_source.index("_plex_delete_playlist_by_title"))
        self.assertLess(create_source.index("_plex_delete_playlist_by_title"), create_source.index("_plex_create_audio_playlist"))
        self.assertIn("_plex_playlist_rating_keys_by_title", create_source)
        self.assertIn("Verified Plex playlist count", APP_SOURCE)

    def test_plex_sync_diagnostics_scan_and_ui_status(self):
        self.assertIn("plex_url", APP_SOURCE)
        self.assertIn("plex_token", APP_SOURCE)
        self.assertIn("plex_music_section", APP_SOURCE)
        self.assertIn("plex_scan_timeout", APP_SOURCE)
        self.assertIn("plex_index_timeout", APP_SOURCE)
        for text in (
            "Beets root:",
            "Plex locations:",
            "Using path map:",
            "Sample Beets path:",
            "Sample mapped Plex path:",
            "Pending Plex match:",
            "Created/updated Plex playlist",
        ):
            self.assertIn(text, APP_SOURCE)
        run_plex_start = APP_SOURCE.index("def _playlist_run_plex_sync")
        run_plex_end = APP_SOURCE.index("def _playlist_start_direct_action", run_plex_start)
        run_plex_source = APP_SOURCE[run_plex_start:run_plex_end]
        self.assertIn("wait_for_plex_seconds=wait_for_plex", run_plex_source)
        self.assertIn("_trigger_plex_refresh", APP_SOURCE)
        self.assertIn("playlistStatusSeverity", PAGE_SOURCE)
        self.assertIn("Plex sync partially completed", PAGE_SOURCE)
        self.assertIn("pending Plex match", PAGE_SOURCE)
        self.assertIn("Playlist saved; Plex not configured", PAGE_SOURCE)
        self.assertIn("pending_plex", PAGE_SOURCE)
        self.assertNotIn("message: playlistStatusMessage(state.playlist) || 'Playlist sync complete.'", PAGE_SOURCE)

    def test_source_sync_filters_tombstones_before_missing_detection(self):
        start = APP_SOURCE.index("def _playlist_run_source_sync")
        end = APP_SOURCE.index("def _playlist_staged_entries", start)
        source = APP_SOURCE[start:end]
        self.assertLess(source.index("_playlist_apply_tombstones"), source.index("_playlist_match_reference_tracks"))
        self.assertIn('"local_m3u"', source)

    def test_track_action_menus_are_context_aware(self):
        self.assertIn("function ActionMenu", PAGE_SOURCE)
        self.assertIn("availableTrackActions", PAGE_SOURCE)
        self.assertIn("missingTrackActions", PAGE_SOURCE)
        self.assertIn("removedTrackActions", PAGE_SOURCE)
        self.assertIn("savedPlaylistRowActions", PAGE_SOURCE)
        self.assertIn("const hasStagedFile = Boolean(track.staged_path)", PAGE_SOURCE)
        self.assertIn("if (downloadedNotImported)", PAGE_SOURCE)
        self.assertIn("status === 'waiting_import'", PAGE_SOURCE)
        self.assertIn("Delete Staged Download", PAGE_SOURCE)
        self.assertIn("if (failed)", PAGE_SOURCE)
        self.assertIn("View Error", PAGE_SOURCE)
        self.assertIn("Sync to Plex", PAGE_SOURCE)
        self.assertIn("Restore to Playlist", PAGE_SOURCE)
        self.assertIn("Delete Playlist", PAGE_SOURCE)


if __name__ == "__main__":
    unittest.main()

import ast
import hashlib
import json
import re
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _app_ast_cache import get_app_ast  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
APP_SOURCE = (ROOT / "app.py").read_text(encoding="utf-8")
AGENT_SOURCE = (ROOT / "backend" / "beets_control_agent.py").read_text(encoding="utf-8")
COMBINED_SOURCE = APP_SOURCE + AGENT_SOURCE
PAGE_SOURCE = (ROOT / "frontend" / "src" / "views" / "Playlists.tsx").read_text(encoding="utf-8")
CLIENT_SOURCE = (ROOT / "frontend" / "src" / "api" / "client.ts").read_text(encoding="utf-8")
TYPES_SOURCE = (ROOT / "frontend" / "src" / "api" / "types.ts").read_text(encoding="utf-8")


def load_function(name, namespace):
    tree = get_app_ast()
    node = next(item for item in tree.body if isinstance(item, ast.FunctionDef) and item.name == name)
    module = ast.Module(body=[node], type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, "app.py", "exec"), namespace)
    return namespace[name]


class PlaylistPipelineTests(unittest.TestCase):
    def _atomic_save_fn(self, json_mod=None, playlist_dir=None):
        namespace = {
            "Dict": Dict,
            "Any": Any,
            "Optional": Optional,
            "Path": Path,
            "time": time,
            "uuid": uuid,
            "os": __import__("os"),
            "json": json_mod or json,
            "threading": threading,
            "re": re,
            "tempfile": __import__("tempfile"),
            "_s": lambda value: str(value or ""),
            "PLAYLIST_DOWNLOAD_ROOT": Path("/data/torrents/music/Playlist Downloads"),
            "PLAYLIST_DIR": Path(playlist_dir) if playlist_dir is not None else Path("/data/media/music/playlists"),
            "WEB_MANAGER_DATA_DIR": Path(playlist_dir).parent if playlist_dir is not None else Path("/web-manager-data"),
            "PLAYLIST_STATE_ROOT": Path(playlist_dir) if playlist_dir is not None else Path("/web-manager-data/playlists"),
            "PLAYLIST_MANIFESTS_DIR": Path(playlist_dir) if playlist_dir is not None else Path("/web-manager-data/playlists/manifests"),
            "PLAYLIST_JOB_STATE_DIR": Path(playlist_dir) if playlist_dir is not None else Path("/web-manager-data/playlists/jobs"),
            "PLAYLIST_MEMBERSHIP_DIR": Path(playlist_dir) if playlist_dir is not None else Path("/web-manager-data/playlists/membership"),
            "MUSIC_ROOT": Path("/data/media/music"),
            "_path_is_under": lambda path, root: True,
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
        with tempfile.TemporaryDirectory() as root:
            final = Path(root) / "missing" / "Baby Makin.playlist.json"
            save_json = self._atomic_save_fn(playlist_dir=final.parent)
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

        with tempfile.TemporaryDirectory() as root:
            final = Path(root) / "playlists" / "Baby Makin.playlist.json"
            save_json = self._atomic_save_fn(FailingJson, playlist_dir=final.parent)
            final.parent.mkdir()
            final.write_text('{"name": "Baby Makin", "version": 1}', encoding="utf-8")
            with self.assertRaises(OSError):
                save_json(final, {"name": "Baby Makin", "version": 2}, save_key="job-2")
            self.assertEqual(json.loads(final.read_text(encoding="utf-8"))["version"], 1)

    def test_concurrent_atomic_playlist_saves_leave_valid_json(self):
        with tempfile.TemporaryDirectory() as root:
            final = Path(root) / "playlists" / "Baby Makin.playlist.json"
            save_json = self._atomic_save_fn(playlist_dir=final.parent)

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
            # Nested under this playlist's own staging subfolder, matching
            # real production layout (_playlist_downloads_dir/_imports_dir
            # are always get_playlist_staging_root(name) / "downloads" or
            # "/imports" -- never a bare file directly under the shared
            # PLAYLIST_DOWNLOAD_ROOT). SEC-002 Wave 9 final review narrowed
            # staged-deletion containment to this playlist's own staging
            # root specifically (not the shared parent all playlists sit
            # under), to close a cross-playlist deletion gap -- so the
            # fixture must reflect where a real staged file actually lives.
            playlist_staging = staging / "Road Trip"
            playlist_staging.mkdir()
            staged_file = playlist_staging / "song.mp3"
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

            # Deletion is engine-owned (SEC-002 Wave 9 continuation): the
            # function delegates the actual unlink to beets_client rather
            # than touching the filesystem directly. This fake performs the
            # same containment-agnostic unlink the real control agent would,
            # so the test still proves the *caller's* pre-validation
            # (library-file refusal, staging containment) independently of
            # engine-side enforcement, which has its own dedicated tests.
            class _FakeBeetsClient:
                def delete_playlist_staged_track(self, playlist_key, track_id, requested_path):
                    p = Path(requested_path)
                    existed = p.exists()
                    if existed:
                        p.unlink()
                    return {"ok": True, "deleted": existed, "already_absent": not existed, "path": requested_path}

            namespace = {
                "Dict": Dict,
                "Any": Any,
                "Path": Path,
                "PLAYLIST_DOWNLOAD_ROOT": staging,
                "MUSIC_ROOT": library,
                "AUDIO_EXT": {".mp3"},
                "beets_client": _FakeBeetsClient(),
                "_playlist_key": lambda name, manifest=None: str(name),
                "_playlist_existing_key": lambda name: str(name),
                "_playlist_slug": lambda name: str(name),
                "_s": lambda value: str(value or ""),
                "_path_is_under": is_under,
                "_clean_playlist_name": lambda name: str(name),
                "get_playlist_staging_root": lambda name: staging / name,
                "_playlist_manifest_track_states": lambda _name: {
                    "artist|song": {"staged_path": str(staged_file)},
                    "artist|libsong": {"staged_path": str(library_file)},
                },
                "_playlist_status_id": lambda track: (
                    "artist|libsong" if track.get("title") == "LibSong" else "artist|song"
                ),
                "_playlist_store_track_state": lambda *args, **kwargs: stored.append((args, kwargs)),
            }
            delete_staged = load_function("_playlist_delete_staged_track_file", namespace)
            result = delete_staged("Road Trip", {"artist": "Artist", "title": "Song"})
            self.assertTrue(result["deleted"])
            self.assertFalse(staged_file.exists())
            self.assertTrue(library_file.exists())
            # The manifest's own recorded path (not just a browser-supplied
            # requested_path) must be revalidated -- a stale/tampered state
            # row pointing at a library file must still be refused.
            with self.assertRaisesRegex(RuntimeError, "Beets library file"):
                delete_staged(
                    "Road Trip",
                    {"artist": "Artist", "title": "LibSong"},
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
        # Wave 13: album placement mutations moved into the engine
        # (backend/beets_control_agent.py); _playlist_find_or_create_album_row
        # is now bounded by the next helper that follows it in app.py rather
        # than by _playlist_apply_album_placement, which precedes it.
        find_start = APP_SOURCE.index("def _playlist_find_or_create_album_row")
        find_end = APP_SOURCE.index("def _playlist_repair_quality_candidate", find_start)
        find_source = APP_SOURCE[find_start:find_end]
        self.assertLess(find_source.index("if mb_releasegroupid"), find_source.index("if not row and mb_albumid"))
        self.assertIn("$mb_releasegroupid", APP_SOURCE)
        # Wave 13: the actual mb_releasegroupid/mb_albumartistid -> UPDATE
        # items mutation now happens engine-side in
        # /playlists/place-imported (backend/beets_control_agent.py).
        self.assertIn('mb_releasegroupid = str(placement.get("mb_releasegroupid") or "").strip()', COMBINED_SOURCE)
        self.assertIn('mb_albumartistid = str(placement.get("mb_albumartistid") or "").strip()', COMBINED_SOURCE)
        singleton_line = next(line for line in COMBINED_SOURCE.splitlines() if "_SINGLE_TRACK_PATH_TEMPLATE" in line)
        self.assertIn("_ARTIST_FOLDER_PATH_TEMPLATE", singleton_line)
        self.assertIn("$mb_releasegroupid", singleton_line)
        self.assertNotIn("$mb_albumid", singleton_line)

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
        # Wave 13: _playlist_validate_final_album_path still exists and is
        # exercised directly (see
        # test_playlist_import_final_path_validation_requires_album_artist_folder),
        # but its caller is now the engine-side placement path rather than a
        # local _playlist_apply_album_placement mutation, so the specific
        # "Final path failed validation because" log line it used to emit
        # from app.py is no longer produced there.
        self.assertIn("_playlist_validate_final_album_path", APP_SOURCE)

    def test_download_and_import_are_idempotent_at_their_boundaries(self):
        self.assertIn("Checking requested missing playlist tracks against Beets before download", APP_SOURCE)
        self.assertIn("Historical staged paths are untrusted metadata", APP_SOURCE)
        self.assertNotIn("persisted.get(\"staged_path\")", APP_SOURCE)
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
        # Wave 13: album placement mutation moved from a local, retried
        # SQLite write in app.py to a single engine-owned write inside
        # /playlists/place-imported (backend/beets_control_agent.py), guarded
        # by acquire_os_lock rather than _sqlite_write_retry. app.py's
        # _playlist_apply_album_placement must delegate via BeetsClient IPC
        # and must not perform the SQLite write itself.
        apply_start = APP_SOURCE.index("def _playlist_apply_album_placement")
        apply_end = APP_SOURCE.index("def _playlist_find_or_create_album_row", apply_start)
        apply_source = APP_SOURCE[apply_start:apply_end]
        self.assertNotIn("_sqlite_write_retry", apply_source)
        self.assertIn("beets_client.place_playlist_imported_item", apply_source)
        self.assertIn("acquire_os_lock(read_only=False)", AGENT_SOURCE)
        self.assertIn('if path == "/playlists/place-imported":', AGENT_SOURCE)

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
            create_source.index("_plex_replace_playlist_safely"),
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
        self.assertLess(create_source.index("mapping_failed"), create_source.index("_plex_replace_playlist_safely"))
        self.assertLess(create_source.index("_plex_replace_playlist_safely"), create_source.index("_plex_create_audio_playlist"))
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

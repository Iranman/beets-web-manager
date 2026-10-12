"""0.6.0 deterministic import, phase A (F1-F4) and History completeness (F9).

F1: the batch's folder discovery called composite_workflows.discover_import_sources
    with arguments it does not take, so every batch died with a TypeError.
F2: POST /api/ai-batch-import returned 400 without an AI key (#288: AI is optional).
F3: the downloads root itself was refused as a scan root.
F4: the scan-root rule lives in one helper, _resolve_import_scan_root.
F9: batch imports and /api/import did not reach the History tab.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
REL = "11111111-1111-1111-1111-111111111111"
RG = "22222222-2222-2222-2222-222222222222"


def setUpModule():
    # Import the app family at run time, not collection time (see
    # test_acoustid_status_propagation): app_runtime reads the environment
    # when it is first imported.
    global APP, ai, ser, cw, patch_app_family
    tmp = Path(tempfile.mkdtemp(prefix="bwm_det_import_env_"))
    os.environ.setdefault("BEETSDIR", str(tmp / "config"))
    os.environ.setdefault("LIB_PATH", str(tmp / "config" / "musiclibrary.blb"))
    os.environ.setdefault("AI_BATCH_STATE_DIR", str(tmp / "ai_batch_jobs"))
    os.environ.setdefault("METADATA_CACHE_DIR", str(tmp / "cache"))
    os.environ.setdefault("BEETS_TRANSACTION_DIR", str(tmp / "transactions"))
    os.environ["BEETS_WEB_AUTH_DISABLED"] = "1"
    (tmp / "config").mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(ROOT))
    import app as APP  # noqa: N811
    import backend.ai_service as ai
    import backend.serializers as ser
    import backend.composite_workflows as cw
    try:
        from _app_family import patch_app_family
    except ImportError:  # pragma: no cover
        from tests._app_family import patch_app_family


class _Roots(unittest.TestCase):
    """A downloads root and a music root in a throwaway directory (not under
    the OS temp dir, so no production default root contains it)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bwm_det_import_", dir=os.getcwd())).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.dl = self.tmp / "downloads"
        self.music = self.tmp / "music"
        self.outside = self.tmp / "outside"
        for p in (self.dl, self.music, self.outside):
            p.mkdir()
        for name, value in (("_DOWNLOADS_ROOTS", [str(self.dl)]), ("TORRENT_SOURCE_ROOTS", [self.dl]),
                            ("PLAYLIST_DOWNLOAD_ALLOWED_ROOTS", []), ("MUSIC_ROOT", self.music),
                            ("_IMPORT_SOURCE_ALLOWED_ROOTS", (self.dl, self.music))):
            p = patch_app_family(APP, name, value)
            p.start()
            self.addCleanup(p.stop)

    def audio(self, rel):
        f = self.dl / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"x")
        return f


class FolderDiscoveryTests(_Roots):
    """F1: a local leaf walk, not the engine helper."""

    def test_finds_album_folders_and_folds_disc_subfolders(self):
        self.audio("Artist/Album/01.flac")
        self.audio("Artist/Double/CD1/01.flac")
        self.audio("Artist/Double/CD 2/01.flac")
        self.audio(".hidden/01.flac")
        (self.dl / "Empty").mkdir()
        (self.dl / "Notes").mkdir()
        (self.dl / "Notes" / "readme.txt").write_text("x")
        log = []
        found = ai._ai_batch_find_audio_dirs(str(self.dl), log)
        self.assertEqual(found, [str(self.dl / "Artist" / "Album"), str(self.dl / "Artist" / "Double")])

    def test_loose_files_in_the_root_are_logged_not_imported(self):
        self.audio("loose.mp3")
        self.audio("Album/01.flac")
        log = []
        found = ai._ai_batch_find_audio_dirs(str(self.dl), log)
        self.assertEqual(found, [str(self.dl / "Album")])
        self.assertTrue(any("left in place" in line and str(self.dl) in line for line in log), log)

    def test_scanning_one_album_folder_returns_it(self):
        self.audio("Album/CD1/01.flac")
        self.audio("Album/CD2/01.flac")
        self.assertEqual(ai._ai_batch_find_audio_dirs(str(self.dl / "Album")), [str(self.dl / "Album")])

    def test_symlinked_album_folder_is_not_followed(self):
        (self.outside / "01.flac").write_bytes(b"x")
        self.audio("Album/01.flac")
        try:
            (self.dl / "Linked").symlink_to(self.outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("cannot create symlinks here")
        self.assertEqual(ai._ai_batch_find_audio_dirs(str(self.dl)), [str(self.dl / "Album")])

    def test_walk_rechecks_the_root(self):
        self.audio("Album/01.flac")
        for bad in (str(self.music), str(self.outside)):
            with self.subTest(root=bad), self.assertRaisesRegex(RuntimeError, "refused"):
                ai._ai_batch_find_audio_dirs(bad)

    def test_missing_root_fails_clearly(self):
        with self.assertRaisesRegex(RuntimeError, "not found"):
            ai._ai_batch_find_audio_dirs(str(self.dl / "gone"))

    def test_engine_discovery_is_not_used(self):
        self.audio("Album/01.flac")
        with mock.patch.object(ai.composite_workflows, "discover_import_sources",
                               side_effect=AssertionError("engine discovery called")):
            ai._ai_batch_find_audio_dirs(str(self.dl))


class ScanRootTests(_Roots):
    """F3/F4: the scan-root rule."""

    def test_downloads_root_itself_is_allowed(self):
        path, err = ser._resolve_import_scan_root(str(self.dl))
        self.assertIsNone(err)
        self.assertEqual(path, self.dl)

    def test_subfolders_are_allowed(self):
        path, err = ser._resolve_import_scan_root(str(self.dl / "Album"))
        self.assertIsNone(err)
        self.assertEqual(path, self.dl / "Album")
        path, err = ser._resolve_import_scan_root(str(self.music / "Artist"))
        self.assertIsNone(err)

    def test_music_root_itself_is_refused(self):
        self.assertIsNotNone(ser._resolve_import_scan_root(str(self.music))[1])

    def test_unsafe_paths_are_refused(self):
        for raw in (str(self.outside), str(self.dl / ".." / "outside"), str(self.dl) + "\x00x",
                    "downloads", "", str(self.tmp)):
            with self.subTest(raw=raw):
                path, err = ser._resolve_import_scan_root(raw)
                self.assertIsNone(path)
                self.assertTrue(err)

    def test_symlinked_folder_is_refused(self):
        link = self.dl / "link"
        try:
            link.symlink_to(self.outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("cannot create symlinks here")
        self.assertIsNotNone(ser._resolve_import_scan_root(str(link))[1])


class BatchRouteTests(_Roots):
    """F2/F3 through POST /api/ai-batch-import."""

    def setUp(self):
        super().setUp()
        for name, kw in (("_ai_api_key", {"return_value": ""}),
                         ("_ai_batch_latest_state", {"return_value": None})):
            p = patch_app_family(APP, name, **kw)
            p.start()
            self.addCleanup(p.stop)

    def _post(self, body):
        with patch_app_family(APP, "_ai_batch_reserve_worker", return_value=False) as reserve, \
                patch_app_family(APP, "_ai_batch_reconnect_response",
                                 return_value=({"ok": True, "reconnected": True}, 200)):
            with APP.app.test_client() as client:
                resp = client.post("/api/ai-batch-import", json=body)
        return resp, reserve

    def test_no_ai_key_still_starts(self):
        (self.dl / "Album").mkdir()
        resp, reserve = self._post({"path": str(self.dl / "Album")})
        self.assertEqual(resp.status_code, 200, resp.get_json())
        reserve.assert_called_once()

    def test_downloads_root_starts(self):
        resp, reserve = self._post({"path": str(self.dl)})
        self.assertEqual(resp.status_code, 200, resp.get_json())
        reserve.assert_called_once()

    def test_music_root_and_outside_are_refused(self):
        for path in (str(self.music), str(self.outside)):
            with self.subTest(path=path):
                resp, reserve = self._post({"path": path})
                self.assertEqual(resp.status_code, 400)
                reserve.assert_not_called()

    def test_batch_state_reports_ai_configured(self):
        state = ai._ai_batch_initial_state("b1", str(self.dl))
        with mock.patch.object(ai, "_ai_batch_find_audio_dirs", return_value=[]), \
                mock.patch.object(ai, "_ai_batch_commit"), \
                mock.patch.object(ai, "_ai_batch_load_state", return_value=state), \
                mock.patch.object(ai, "_ai_batch_run_suggestions", return_value="done"), \
                mock.patch.object(ai, "_ai_batch_process_decisions", return_value={}), \
                mock.patch.object(ai, "_invalidate_lib_cache"):
            out = ai._run_ai_batch_import("b1", str(self.dl), [], None, None)
        self.assertIs(out.get("ai_configured"), False)


class HistoryTests(_Roots):
    """F9: every import completion path writes a History row."""

    def setUp(self):
        super().setUp()
        self.history = self.tmp / "recent_imports.json"
        p = mock.patch.object(ai, "_RECENT_IMPORTS_FILE", self.history)
        p.start()
        self.addCleanup(p.stop)
        for name, kw in (("get_album", {"return_value": {"albumartist": "Artist", "album": "Album",
                                                          "year": 2001, "mb_albumid": REL}}),
                         ("find_all_items_by_album_id", {"return_value": [
                             {"path": str(self.music / "Artist" / "Album" / "01.flac")},
                             {"path": str(self.music / "Artist" / "Album" / "02.flac")}]})):
            p = mock.patch.object(cw, name, **kw)
            p.start()
            self.addCleanup(p.stop)

    def rows(self):
        return json.loads(self.history.read_text()) if self.history.exists() else []

    def test_batch_import_records_history(self):
        with mock.patch.object(ai, "_preserve_torrent_source_path", return_value=False), \
                mock.patch.object(ai, "_validate_import_source_audio"), \
                mock.patch.object(ai, "_fetch_mb_release_tracklist",
                                  return_value={"ok": True, "release_group": RG, "tracks": []}), \
                mock.patch.object(cw, "plan_confirmed_import", return_value={"ok": True, "operation_id": "op"}), \
                mock.patch.object(cw, "apply_confirmed_import", return_value={"ok": True, "album_id": 7}), \
                mock.patch.object(ai, "_fetch_artwork_after_retag", return_value={
                    "identity_verified": True, "artwork_status": "fetched", "artwork_retryable": False}), \
                mock.patch.object(ai, "_remove_pending_review_for_path"), \
                mock.patch.object(ai, "lib"), \
                mock.patch.object(ai, "_auto_merge_case_duplicate_artist_folder"), \
                mock.patch.object(ai, "_invalidate_lib_cache"), \
                mock.patch.object(ai, "_trigger_plex_refresh"):
            ai._ai_import_folder(str(self.dl / "Album"), REL, {}, [])
        rows = self.rows()
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual((rows[0]["artist"], rows[0]["album"], rows[0]["year"], rows[0]["tracks"],
                          rows[0]["mb_albumid"], rows[0]["aldir"]),
                         ("Artist", "Album", 2001, 2, REL, str(self.music / "Artist" / "Album")))

    def test_api_import_records_history(self):
        (self.dl / "Album").mkdir()
        result = {}

        def run_now(fn, label="", metadata=None):
            log = []
            result["value"] = fn(log)
            result["log"] = log
            return mock.Mock(job_id="job-1")

        reimport = {"ok": True, "albums_imported": 1, "imported_album_ids": [7],
                    "not_matched": [], "skipped": [], "not_matched_known": True}
        with mock.patch.object(APP.jobs, "start_python", side_effect=run_now), \
                mock.patch.object(cw, "reimport_source", return_value=reimport), \
                patch_app_family(APP, "_validate_import_source_audio"), \
                patch_app_family(APP, "_preserve_torrent_source_path", return_value=False), \
                patch_app_family(APP, "_delete_if_already_in_library"), \
                patch_app_family(APP, "_invalidate_lib_cache"), \
                patch_app_family(APP, "_record_import_skips"):
            with APP.app.test_client() as client:
                resp = client.post("/api/import", json={"path": str(self.dl / "Album")})
        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertIn("value", result, result)
        rows = self.rows()
        self.assertEqual([(r["artist"], r["album"], r["tracks"]) for r in rows], [("Artist", "Album", 2)])

    def test_history_failure_never_fails_the_import(self):
        log = []
        with mock.patch.object(ai, "_record_recent_import", side_effect=OSError("read-only")):
            ai._record_recent_import_for_album(7, log)
        self.assertTrue(any("[recent] Warning" in line for line in log), log)


if __name__ == "__main__":
    unittest.main()

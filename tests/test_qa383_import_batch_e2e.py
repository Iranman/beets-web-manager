"""QA for PR #383 (deterministic batch import, phase A; History, F9).

Independent of the implementer's tests: these drive the real batch worker
(_run_ai_batch_import -> real folder walk -> real per-folder stages with
no AI key) with only the providers (MusicBrainz, AcoustID, preflight) and
the Beets import call stubbed, and pin the path
safety of the new scan-root helper and walk.
"""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
REL = "33333333-3333-3333-3333-333333333333"
RG = "44444444-4444-4444-4444-444444444444"


def setUpModule():
    global APP, ai, ser, patch_app_family
    tmp = Path(tempfile.mkdtemp(prefix="bwm_qa383_env_"))
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
    try:
        from _app_family import patch_app_family
    except ImportError:  # pragma: no cover
        from tests._app_family import patch_app_family


class _Roots(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bwm_qa383_", dir=os.getcwd())).resolve()
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

    def audio(self, rel, base=None):
        f = (base or self.dl) / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"x")
        return f

    def symlink(self, link, target):
        try:
            link.symlink_to(target, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("cannot create symlinks here")


class NoAiBatchEndToEnd(_Roots):
    """A batch with no AI provider discovers folders and imports them through
    the deterministic MusicBrainz/AcoustID path; AI is never called."""

    def _candidate(self, album):
        return {"mb_albumid": REL, "mb_releasegroupid": RG, "album": album, "artist": "Artist",
                "year": "2001", "track_count": 1, "tracks": 1, "score": 100, "label": "", "country": "XW",
                "release_group_primary_type": "Album"}

    def test_batch_without_ai_key_imports_deterministically(self):
        self.audio("Artist/Album/01.flac")
        self.audio("Artist/Double/CD1/01.flac")
        self.audio("Artist/Double/CD2/01.flac")
        imported = []

        def fake_reimport(folder, opts, timeout):
            # Phase B: Stage 1 is a Beets quiet import of exactly one folder.
            imported.append((folder, opts.get("quiet_fallback")))
            return {"ok": True, "albums_imported": 1, "imported_album_ids": [len(imported)],
                    "skipped": [], "not_matched": [], "not_matched_known": True}

        def no_ai(provider, *a, **k):
            raise AssertionError(f"provider {provider!r} called during a no-AI batch")

        pf = {"ok": True, "matches": 1, "expected": 1, "match_ratio": 1.0}
        patches = [
            patch_app_family(APP, "_ai_api_key", return_value=""),
            mock.patch.object(ai.provider_boundary, "opened", side_effect=no_ai),
            mock.patch.object(ai, "_acoustid_multi_file", return_value={}),
            mock.patch.object(ai, "_acoustid_lookup_cached_outcome", return_value=None),
            mock.patch.object(ai, "_mb_release_search",
                              side_effect=lambda album, *a, **k: [self._candidate(album or "Album")]),
            mock.patch.object(ai, "_mb_release_search_by_folder_tracks", return_value=[]),
            mock.patch.object(ai, "_discogs_release_fallback_candidate", return_value={}),
            mock.patch.object(ai, "_fetch_mb_release_tracklist",
                              return_value={"ok": True, "release_group": RG, "tracks": []}),
            mock.patch.object(ai, "_run_ai_release_preflight", return_value=pf),
            mock.patch.object(ai, "_folder_release_preflight", return_value=pf),
            mock.patch.object(ai, "_ai_batch_already_in_library", return_value=False),
            mock.patch.object(ai, "_validate_import_source_audio"),
            mock.patch.object(ai.composite_workflows, "reimport_source", side_effect=fake_reimport),
            mock.patch.object(ai.composite_workflows, "get_album", return_value={"mb_releasegroupid": RG}),
            mock.patch.object(ai, "_record_recent_import_for_album"),
            mock.patch.object(ai.beets_adapter, "get_plugin_status",
                              return_value={"capabilities": ["mbsubmit"], "fpcalc_available": True}),
            mock.patch.object(ai, "_ai_import_folder", side_effect=AssertionError("Stage 2b not expected")),
            mock.patch.object(ai, "_invalidate_lib_cache"),
            mock.patch.object(ai, "_trigger_plex_refresh"),
            patch_app_family(APP, "_AI_PENDING_FILE", self.tmp / "pending.json"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        log = []
        out = ai._run_ai_batch_import("qa383-b1", str(self.dl), log, None, None)
        self.assertEqual(sorted(f for f, _ in imported),
                         [str(self.dl / "Artist" / "Album"), str(self.dl / "Artist" / "Double")],
                         "\n".join(log))
        self.assertTrue(all(q == "skip" for _, q in imported), imported)
        self.assertEqual(sorted(f["status"] for f in out["folders"]), ["imported", "imported"])
        self.assertIs(out.get("ai_configured"), False)


class NoAiRoute(_Roots):
    def test_route_starts_without_ai_key(self):
        (self.dl / "Album").mkdir()
        with patch_app_family(APP, "_ai_api_key", return_value=""), \
                patch_app_family(APP, "_ai_batch_latest_state", return_value=None), \
                patch_app_family(APP, "_ai_batch_reserve_worker", return_value=False) as reserve, \
                patch_app_family(APP, "_ai_batch_reconnect_response",
                                 return_value=({"ok": True, "reconnected": True}, 200)):
            with APP.app.test_client() as client:
                resp = client.post("/api/ai-batch-import", json={"path": str(self.dl / "Album")})
        self.assertEqual(resp.status_code, 200, resp.get_json())
        reserve.assert_called_once()


class ScanRootPathSafety(_Roots):
    """_resolve_import_scan_root and the walk's re-check."""

    def refused(self, raw):
        path, err = ser._resolve_import_scan_root(raw)
        self.assertIsNone(path, raw)
        self.assertTrue(err, raw)

    def test_encoded_and_lexical_traversal_refused(self):
        for raw in (str(self.dl) + "/%2e%2e/outside", str(self.dl) + "/%252e%252e/outside",
                    str(self.dl) + "/Album/../../outside", str(self.dl) + "//Album",
                    str(self.dl) + "%2fAlbum", str(self.dl) + "/./Album"):
            with self.subTest(raw=raw):
                self.refused(raw)

    def test_root_spelling_variants(self):
        # A trailing slash is the same root; a sibling sharing the prefix is not.
        self.assertEqual(ser._resolve_import_scan_root(str(self.dl) + "/")[0], self.dl)
        sibling = self.tmp / "downloads-evil"
        sibling.mkdir()
        self.refused(str(sibling))
        self.refused(str(self.music) + "/")

    def test_parent_of_both_roots_refused(self):
        self.refused(str(self.tmp))
        self.refused("/")

    def test_library_subfolder_allowed(self):
        (self.music / "Artist").mkdir()
        path, err = ser._resolve_import_scan_root(str(self.music / "Artist"))
        self.assertIsNone(err)
        self.assertEqual(path, self.music / "Artist")

    def test_symlink_into_library_or_outside_refused(self):
        for name, target in (("to_music", self.music), ("to_outside", self.outside)):
            link = self.dl / name
            self.symlink(link, target)
            with self.subTest(name=name):
                self.refused(str(link))
                self.refused(str(link / "Album"))

    def test_symlink_to_downloads_root_scans_the_configured_root(self):
        # N7 (#394): a link whose target is exactly a configured root is that
        # root (NAS mount spelling). The configured value is returned, never the
        # link, and nothing below or beside the root is widened.
        link = self.outside / "dl_link"
        self.symlink(link, self.dl)
        path, err = ser._resolve_import_scan_root(str(link))
        self.assertIsNone(err)
        self.assertEqual(path, self.dl.resolve())
        self.refused(str(link / "Album"))

    def test_toctou_folder_swapped_for_symlink_before_walk(self):
        album = self.dl / "Album"
        album.mkdir()
        path, err = ser._resolve_import_scan_root(str(album))
        self.assertIsNone(err)
        (self.outside / "01.flac").write_bytes(b"x")
        album.rmdir()
        self.symlink(album, self.outside)
        with self.assertRaisesRegex(RuntimeError, "refused"):
            ai._ai_batch_find_audio_dirs(str(path))

    def test_walk_does_not_descend_symlinked_subfolder_to_library(self):
        self.audio("Artist/Album/01.flac", base=self.music)
        self.audio("Real/01.flac")
        self.symlink(self.dl / "Lib", self.music)
        self.assertEqual(ai._ai_batch_find_audio_dirs(str(self.dl), []), [str(self.dl / "Real")])

    def test_symlinked_audio_file_not_counted(self):
        (self.outside / "01.flac").write_bytes(b"x")
        (self.dl / "Album").mkdir()
        try:
            (self.dl / "Album" / "01.flac").symlink_to(self.outside / "01.flac")
        except (OSError, NotImplementedError):
            self.skipTest("cannot create symlinks here")
        self.assertEqual(ai._ai_batch_find_audio_dirs(str(self.dl), []), [])

    def test_disc_folder_directly_in_root_is_logged_not_dropped(self):
        self.audio("CD1/01.flac")
        log = []
        self.assertEqual(ai._ai_batch_find_audio_dirs(str(self.dl), log), [])
        self.assertTrue(any("left in place" in line for line in log), log)

    def test_library_subfolder_scan_walks_only_that_subfolder(self):
        self.audio("Artist/Album/01.flac", base=self.music)
        self.audio("Other/Album/01.flac", base=self.music)
        found = ai._ai_batch_find_audio_dirs(str(self.music / "Artist"), [])
        self.assertEqual(found, [str(self.music / "Artist" / "Album")])

    def test_stored_state_music_root_refused_at_job_start(self):
        # /recover sources scan_path from stored state; _start_ai_batch_job re-checks.
        import routes_import
        with APP.app.app_context():
            resp = routes_import._start_ai_batch_job(str(self.music))
        body, status = (resp if isinstance(resp, tuple) else (resp, resp.status_code))
        self.assertEqual(status, 400)


class HistoryNotDuplicated(_Roots):
    """F9: one History row per imported album across the three call sites."""

    def test_reimport_disk_and_api_import_do_not_go_through_batch_recorder(self):
        import inspect
        import backend.import_service as isvc
        import routes_import
        reimport_src = inspect.getsource(isvc.start_reimport_disk)
        self.assertEqual(reimport_src.count("_record_recent_import"), 1)
        self.assertNotIn("_ai_import_folder", reimport_src)
        api_src = inspect.getsource(routes_import.start_import)
        self.assertEqual(api_src.count("_record_recent_import"), 1)
        self.assertNotIn("_ai_import_folder", api_src)
        batch_src = inspect.getsource(ai._ai_batch_process_decisions)
        self.assertNotIn("_record_recent_import", batch_src)
        self.assertEqual(inspect.getsource(ai._ai_import_folder).count("_record_recent_import"), 1)


if __name__ == "__main__":
    unittest.main()
